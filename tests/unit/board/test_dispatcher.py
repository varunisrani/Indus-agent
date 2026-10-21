"""P24-12: the board dispatcher (exit codes, lanes, caps) and the daemon watchers."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from indus.board.dispatcher import (
    LOG_ROTATE_BYTES,
    RATE_LIMIT_COOLDOWN_S,
    REAP_GRACE_S,
    TERMINAL_GRACE_S,
    Dispatcher,
    DispatchSettings,
    WorkerHandle,
    WorkerSpec,
    derive_concurrency,
    rotate_log,
)
from indus.board.watchers import MAX_SEND_FAILURES, BoardWatchers, format_event
from indus.foundation.scope import Scope
from indus.state.board import BoardStore, Task
from tests.fakes.clock import FakeClock


class FakeSpawner:
    """Workers that exist only as numbers; tests decide when and how they end."""

    def __init__(self) -> None:
        self.specs: list[WorkerSpec] = []
        self.running: set[int] = set()
        self.ended: dict[int, int] = {}
        self.terminated: list[int] = []
        self.unkillable: set[int] = set()
        self.fail_next = False
        self._next = 1000

    def spawn(self, spec: WorkerSpec) -> WorkerHandle:
        if self.fail_next:
            self.fail_next = False
            raise FileNotFoundError("no such program")
        self._next += 1
        self.specs.append(spec)
        self.running.add(self._next)
        return WorkerHandle(self._next, f"fp{self._next}")

    def end(self, pid: int, code: int) -> None:
        self.running.discard(pid)
        self.ended[pid] = code

    def exited(self) -> dict[int, int]:
        done, self.ended = self.ended, {}
        return done

    def alive(self, pid: int, fingerprint: str | None) -> bool:
        return pid in self.running

    def terminate(self, pid: int, fingerprint: str | None) -> bool:
        self.terminated.append(pid)
        if pid in self.unkillable:
            return False
        self.running.discard(pid)
        return True


@pytest.fixture
def store(scope: Scope) -> Iterator[BoardStore]:
    opened = BoardStore(scope)
    yield opened
    opened.close()


@pytest.fixture
def spawner() -> FakeSpawner:
    return FakeSpawner()


def _profile(scope: Scope, name: str) -> None:
    home = scope.root / "profiles" / name
    home.mkdir(parents=True, exist_ok=True)
    (home / "profile.yaml").touch()


def _dispatcher(
    store: BoardStore, scope: Scope, spawner: FakeSpawner, **settings: int
) -> Dispatcher:
    return Dispatcher(
        store,
        scope=scope,
        spawner=spawner,
        settings=DispatchSettings(**settings),  # type: ignore[arg-type]
        memory_mib=lambda: 8192,
    )


def _pid(store: BoardStore, task: Task) -> int:
    pid = store.require(task.id).worker_pid
    assert pid is not None
    return pid


def test_exit_75_cools_the_lane_without_a_failure(
    store: BoardStore, scope: Scope, spawner: FakeSpawner, fake_clock: FakeClock
) -> None:
    _profile(scope, "bob")
    first = store.create("a", assignee="work")
    second = store.create("b", assignee="work")
    other = store.create("c", assignee="bob")
    dispatcher = _dispatcher(store, scope, spawner)
    report = dispatcher.tick()
    assert {t for t, _ in report.spawned} == {first.id, second.id, other.id}
    spawner.end(_pid(store, first), 75)
    report = dispatcher.tick()
    assert report.rate_limited == [first.id]
    task = store.require(first.id)
    assert task.status == "ready" and task.consecutive_failures == 0
    assert store.lane_cooled_until("work") == store.now() + RATE_LIMIT_COOLDOWN_S
    assert first.id in report.skipped_cooled
    assert store.lane_cooled_until("bob") is None
    fake_clock.advance(RATE_LIMIT_COOLDOWN_S - 1)
    assert first.id in dispatcher.tick().skipped_cooled
    fake_clock.advance(2)
    assert (first.id, "work") in dispatcher.tick().spawned
    assert store.require(first.id).consecutive_failures == 0


def test_exit_78_blocks_as_capability(
    store: BoardStore, scope: Scope, spawner: FakeSpawner
) -> None:
    task = store.create("needs a model", assignee="work")
    dispatcher = _dispatcher(store, scope, spawner)
    dispatcher.tick()
    spawner.end(_pid(store, task), 78)
    report = dispatcher.tick()
    landed = store.require(task.id)
    assert report.blocked == [task.id]
    assert landed.status == "blocked" and landed.block_kind == "capability"
    assert report.spawned == []


def test_silent_end_counts_a_violation_and_three_block(
    store: BoardStore, scope: Scope, spawner: FakeSpawner
) -> None:
    task = store.create("forgetful", assignee="work")
    dispatcher = _dispatcher(store, scope, spawner)
    for round_ in (1, 2, 3):
        dispatcher.tick()
        assert store.require(task.id).status == "running"
        spawner.end(_pid(store, task), 0)
        report = dispatcher.tick()
        assert task.id in report.violations
        landed = store.require(task.id)
        assert landed.protocol_violations == round_
        if round_ < 3:
            # released and respawned in the same tick
            assert landed.status == "running"
    final = store.require(task.id)
    assert final.status == "blocked"
    assert store.events(task.id)[-1].kind == "gave_up"


def test_two_crashes_block_the_task(store: BoardStore, scope: Scope, spawner: FakeSpawner) -> None:
    task = store.create("crashy", assignee="work")
    dispatcher = _dispatcher(store, scope, spawner)
    dispatcher.tick()
    spawner.end(_pid(store, task), 1)
    report = dispatcher.tick()
    assert report.crashed == [task.id]
    assert store.require(task.id).consecutive_failures == 1
    spawner.end(_pid(store, task), 137)
    report = dispatcher.tick()
    landed = store.require(task.id)
    assert landed.status == "blocked" and landed.consecutive_failures == 2
    assert report.blocked == [task.id]


def test_spawn_failure_counts_as_a_failure(
    store: BoardStore, scope: Scope, spawner: FakeSpawner
) -> None:
    task = store.create("no program", assignee="work")
    dispatcher = _dispatcher(store, scope, spawner)
    spawner.fail_next = True
    report = dispatcher.tick()
    assert report.errors and report.spawned == []
    assert store.require(task.id).status == "ready"
    assert store.require(task.id).consecutive_failures == 1


@pytest.mark.parametrize(
    ("total_mib", "expected"),
    [(None, 2), (0, 2), (512, 2), (1023, 2), (1536, 3), (2048, 4), (4096, 8), (65536, 8)],
)
def test_concurrency_clamp_table(total_mib: int | None, expected: int) -> None:
    assert derive_concurrency(total_mib) == expected


def test_capacity_and_skips(store: BoardStore, scope: Scope, spawner: FakeSpawner) -> None:
    for n in range(5):
        store.create(f"t{n}", assignee="work")
    lonely = store.create("nobody's")
    stranger = store.create("ghost's", assignee="ghost")
    dispatcher = Dispatcher(store, scope=scope, spawner=spawner, memory_mib=lambda: 1536)
    report = dispatcher.tick()
    assert len(report.spawned) == 3
    assert report.skipped_unassigned == [lonely.id]
    assert report.skipped_unknown_profile == [stranger.id]
    assert len(dispatcher.tick().spawned) == 0  # still full
    configured = Dispatcher(
        store, scope=scope, spawner=spawner, settings=DispatchSettings(max_in_progress=4)
    )
    assert configured.concurrency() == 4
    assert len(configured.tick().spawned) == 1


def test_max_runtime_terminates_after_the_grace(
    store: BoardStore, scope: Scope, spawner: FakeSpawner, fake_clock: FakeClock
) -> None:
    task = store.create("slow", assignee="work", max_runtime_s=60)
    dispatcher = _dispatcher(store, scope, spawner)
    dispatcher.tick()
    pid = _pid(store, task)
    fake_clock.advance(60 + TERMINAL_GRACE_S)
    assert dispatcher.tick().timed_out == []
    fake_clock.advance(1)
    report = dispatcher.tick()
    assert report.timed_out == [task.id]
    assert pid in spawner.terminated
    assert store.require(task.id).consecutive_failures == 1


def test_worker_outliving_its_run_is_reaped(
    store: BoardStore, scope: Scope, spawner: FakeSpawner, fake_clock: FakeClock
) -> None:
    task = store.create("done but lingering", assignee="work")
    dispatcher = _dispatcher(store, scope, spawner)
    dispatcher.tick()
    pid = _pid(store, task)
    run_id = store.require(task.id).current_run_id
    store.complete(task.id, summary="all good", expected_run_id=run_id)
    fake_clock.advance(REAP_GRACE_S - 1)
    assert dispatcher.tick().reaped == []
    fake_clock.advance(2)
    assert dispatcher.tick().reaped == [task.id]
    assert pid in spawner.terminated
    assert dispatcher.tick().reaped == []  # forgotten after the reap


def test_vanished_worker_is_a_crash_after_the_grace(
    store: BoardStore, scope: Scope, spawner: FakeSpawner, fake_clock: FakeClock
) -> None:
    task = store.create("lost", assignee="work")
    dispatcher = _dispatcher(store, scope, spawner)
    dispatcher.tick()
    spawner.running.discard(_pid(store, task))  # gone, and no exit code to read
    assert dispatcher.tick().crashed == []  # within the 30 s crash grace
    fake_clock.advance(31)
    assert dispatcher.tick().crashed == [task.id]


def test_worker_log_rotates_at_2_mib(tmp_path: Path) -> None:
    log = tmp_path / "t.log"
    log.write_bytes(b"x" * LOG_ROTATE_BYTES)
    assert not rotate_log(log)
    log.write_bytes(b"y" * (LOG_ROTATE_BYTES + 1))
    assert rotate_log(log)
    assert not log.exists() and (tmp_path / "t.log.1").stat().st_size == LOG_ROTATE_BYTES + 1
    log.write_bytes(b"z" * (LOG_ROTATE_BYTES + 1))
    assert rotate_log(log)
    assert (tmp_path / "t.log.1").read_bytes()[:1] == b"z"
    assert not (tmp_path / "t.log.2").exists()


def test_worker_env_has_pins_and_no_provider_key(
    store: BoardStore, scope: Scope, spawner: FakeSpawner
) -> None:
    key_name = "OPENAI" + "_API_KEY"
    fake_value = "sk" + "-" + "x" * 24
    leaky = scope.with_env(scope.env.with_overlay({key_name: fake_value}))
    task = store.create("check env", assignee="work", body="Do the thing.")
    dispatcher = Dispatcher(store, scope=leaky, spawner=spawner, memory_mib=lambda: 4096)
    dispatcher.tick()
    spec = spawner.specs[0]
    env: Mapping[str, str] = spec.env
    assert key_name not in env and fake_value not in env.values()
    assert env["INDUS_BOARD_TASK"] == task.id
    assert env["INDUS_BOARD_RUN"] == str(store.require(task.id).current_run_id)
    assert env["INDUS_BOARD_SLUG"] == "default"
    assert env["INDUS_BOARD_CLAIM"] == store.require(task.id).claim_lock
    assert env["INDUS_HOME"] == str(scope.root / "profiles" / "work")
    # A one-shot run on the worker toolset: nothing can deliver a background completion (R02.17).
    assert "--oneshot" in spec.argv and "--local" in spec.argv
    assert spec.argv[spec.argv.index("-t") + 1] == "core,kanban-worker"
    assert spec.argv[spec.argv.index("--source") + 1] == "kanban"
    prompt = Path(spec.argv[spec.argv.index("--query-file") + 1]).read_text(encoding="utf-8")
    assert "check env" in prompt and "Do the thing." in prompt and "kanban_complete" in prompt
    assert spec.cwd == store.workspaces_dir() / task.id and spec.cwd.is_dir()


def test_dir_workspace_must_exist(
    store: BoardStore, scope: Scope, spawner: FakeSpawner, tmp_path: Path
) -> None:
    missing = store.create("x", assignee="work", workspace_kind="dir", workspace_path="/nope/x")
    present = store.create("y", assignee="work", workspace_kind="dir", workspace_path=str(tmp_path))
    report = _dispatcher(store, scope, spawner).tick()
    assert (present.id, "work") in report.spawned
    assert any(missing.id in e for e in report.errors)


def test_review_tasks_get_a_reviewer_run(
    store: BoardStore, scope: Scope, spawner: FakeSpawner
) -> None:
    _profile(scope, "qa")
    task = store.create("feature", assignee="work")
    store.request_review(task.id, summary="done", reviewer="qa", force=True)
    report = _dispatcher(store, scope, spawner).tick()
    assert report.spawned == [(task.id, "qa")]
    run = store.latest_run(task.id)
    assert run is not None and run.source == "review" and run.profile == "qa"


# -- watchers -----------------------------------------------------------------------------------


class Outbox:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = False

    async def __call__(self, target: Mapping[str, str | None], text: str) -> bool:
        if self.fail:
            return False
        self.sent.append((str(target["chat_id"]), text))
        return True


def test_notifier_posts_outcomes_once(store: BoardStore) -> None:
    outbox = Outbox()
    watchers = BoardWatchers(store, deliver=outbox)
    task = store.create("report", assignee="work")
    store.subscribe(task.id, platform="telegram", chat_id="42")
    store.comment(task.id, "a", "not posted")
    store.block(task.id, reason="need the API key", kind="needs_input")
    asyncio.run(watchers.notify_once())
    assert outbox.sent == [("42", f"[{task.id}] report: blocked (needs_input): need the API key")]
    asyncio.run(watchers.notify_once())
    assert len(outbox.sent) == 1
    store.unblock(task.id)
    store.complete(task.id, summary="sent it")
    asyncio.run(watchers.notify_once())
    assert outbox.sent[-1] == ("42", f"[{task.id}] report: done. sent it")


def test_notifier_keeps_the_cursor_on_failure_and_drops_after_the_limit(
    store: BoardStore,
) -> None:
    outbox = Outbox()
    watchers = BoardWatchers(store, deliver=outbox)
    task = store.create("t")
    store.subscribe(task.id, platform="slack", chat_id="C1", thread_id="T1")
    store.complete(task.id, summary="ok")
    outbox.fail = True
    asyncio.run(watchers.notify_once())
    sub = store.subscriptions(task.id)[0]
    assert sub.failures == 1
    outbox.fail = False
    asyncio.run(watchers.notify_once())
    assert len(outbox.sent) == 1 and store.subscriptions(task.id)[0].failures == 0
    other = store.create("u")
    store.subscribe(other.id, platform="slack", chat_id="C2")
    store.complete(other.id, summary="ok")
    outbox.fail = True
    for _ in range(MAX_SEND_FAILURES):
        asyncio.run(watchers.notify_once())
    assert store.subscriptions(other.id) == []


def test_notifier_unsubscribes_archived_tasks(store: BoardStore) -> None:
    watchers = BoardWatchers(store, deliver=Outbox())
    task = store.create("t")
    store.subscribe(task.id, platform="telegram", chat_id="1")
    store.archive(task.id)
    report = asyncio.run(watchers.notify_once())
    assert len(report.dropped) == 1 and store.subscriptions() == []


def test_gc_forgets_old_workers_and_files(
    store: BoardStore, scope: Scope, spawner: FakeSpawner, fake_clock: FakeClock
) -> None:
    task = store.create("old", assignee="work")
    dispatcher = _dispatcher(store, scope, spawner)
    dispatcher.tick()
    run_id = store.require(task.id).current_run_id
    spawner.running.clear()
    store.complete(task.id, summary="ok", expected_run_id=run_id)
    log = store.logs_dir() / f"{task.id}.log"
    log.write_text("output", encoding="utf-8")
    watchers = BoardWatchers(store, deliver=Outbox())
    assert watchers.gc_once()["files_removed"] == 0
    fake_clock.advance(31 * 24 * 3600)
    result = watchers.gc_once()
    assert result["files_removed"] >= 1 and result["workers_forgotten"] == 1
    assert not log.exists()


def test_format_event_skips_quiet_kinds(store: BoardStore) -> None:
    task = store.create("t")
    created = store.events(task.id)[0]
    assert format_event(created, "t") is None
