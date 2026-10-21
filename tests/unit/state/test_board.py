"""P24-11: the task board store — claims, blocks, links, review, attachments."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from indus.foundation.scope import Scope
from indus.state import board as board_mod
from indus.state.board import (
    ATTACHMENT_MAX_BYTES,
    CLAIM_TTL_S,
    HEARTBEAT_STALE_S,
    RECLAIM_GRACE_S,
    AttachmentTooLarge,
    BoardInputError,
    BoardRefused,
    BoardStore,
    LiveClaim,
)
from tests.fakes.clock import FakeClock


@pytest.fixture
def store(scope: Scope) -> Iterator[BoardStore]:
    opened = BoardStore(scope)
    yield opened
    opened.close()


def test_board_lives_under_the_root_and_is_shared_by_profiles(scope: Scope) -> None:
    with BoardStore(scope, "ops") as one:
        made = one.create("shared work")
    assert (scope.root / "boards" / "ops.db").is_file()
    assert board_mod.list_boards(scope.root) == ["default", "ops"]
    other_home = scope.root / "profiles" / "other"
    other_home.mkdir(parents=True)
    from indus.foundation.scope import make_scope

    other = make_scope(other_home, env=scope.env, clock=scope.clock)
    with BoardStore(other, "ops") as two:
        assert two.require(made.id).title == "shared work"
    assert board_mod.current_board(scope.root) == "default"
    board_mod.set_current_board(scope.root, "ops")
    assert board_mod.current_board(scope.root) == "ops"
    with pytest.raises(BoardInputError):
        board_mod.normalize_slug("../etc")


def test_new_task_is_ready_or_waits_on_open_parents(store: BoardStore) -> None:
    parent = store.create("parent")
    child = store.create("child", parents=[parent.id])
    assert parent.status == "ready"
    assert child.status == "todo"
    assert [e.kind for e in store.events(child.id)] == ["created", "dependency_wait"]
    store.complete(parent.id, summary="done it", force=True)
    assert store.require(child.id).status == "ready"


def test_initial_status_only_for_workers(store: BoardStore) -> None:
    with pytest.raises(BoardInputError):
        store.create("x", initial_status="blocked")
    with pytest.raises(BoardInputError):
        store.create("x", initial_status="paused", by_worker=True)
    parked = store.create("x", initial_status="blocked", by_worker=True)
    assert parked.status == "blocked"
    mine = store.create("y", initial_status="running", by_worker=True, claimer="w1")
    assert mine.status == "running" and mine.claim_lock == "w1" and mine.current_run_id


def test_idempotency_key_returns_the_existing_task(store: BoardStore) -> None:
    first = store.create("a", idempotency_key="k1")
    again = store.create("a again", idempotency_key="k1")
    assert again.id == first.id


def test_claim_race_and_parent_demotion(store: BoardStore) -> None:
    task = store.create("work", assignee="alice")
    won = store.claim(task.id, "w1")
    assert won is not None and won.status == "running" and won.claim_lock == "w1"
    assert store.claim(task.id, "w2") is None
    run = store.latest_run(task.id)
    assert run is not None and run.profile == "alice" and run.source == "implement"
    # A ready task whose parent was reopened by hand is demoted at claim time.
    parent = store.create("p")
    child = store.create("c")
    store.link(parent.id, child.id)
    assert store.require(child.id).status == "todo"
    store._write(
        lambda conn: conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child.id,))
    )
    assert store.claim(child.id, "w3") is None
    assert store.require(child.id).status == "todo"


def test_claim_expiry_and_reclaim_with_fake_clock(store: BoardStore, fake_clock: FakeClock) -> None:
    a = store.create("live worker")
    b = store.create("dead worker")
    c = store.create("wedged worker")
    d = store.create("survivor")
    for task, pid in ((a, 101), (b, 102), (c, 103), (d, 104)):
        claimed = store.claim(task.id, f"lock-{pid}")
        assert claimed is not None and claimed.current_run_id is not None
        store.set_worker(task.id, claimed.current_run_id, pid, f"fp{pid}")
    alive = {101, 103, 104}
    stopped: list[int] = []

    def is_alive(pid: int, fp: str | None) -> bool:
        return pid in alive

    def terminate(pid: int, fp: str | None) -> bool:
        stopped.append(pid)
        if pid == 104:
            return False  # parked in uninterruptible sleep: still there
        alive.discard(pid)
        return True

    # Nothing expires before the TTL.
    fake_clock.advance(CLAIM_TTL_S - 1)
    assert store.release_stale_claims(alive=is_alive, terminate=terminate) == []
    # c keeps heartbeating... until it does not; a and d stay "fresh" by heartbeat.
    store.heartbeat(c.id, claimer="lock-103")
    fake_clock.advance(2)
    released = store.release_stale_claims(alive=is_alive, terminate=terminate)
    assert released == [b.id]
    assert store.require(b.id).status == "ready"
    assert store.require(b.id).consecutive_failures == 1
    assert store.require(a.id).status == "running"  # extended: alive and heartbeating
    kinds = [e.kind for e in store.events(a.id)]
    assert "claim_extended" in kinds
    # An hour without a heartbeat: live but wedged → stopped and released.
    fake_clock.advance(HEARTBEAT_STALE_S + 1)
    released = store.release_stale_claims(alive=is_alive, terminate=terminate)
    assert set(released) == {a.id, c.id}
    assert 101 in stopped and 103 in stopped
    # The survivor keeps its claim for the reclaim grace, not a full TTL.
    survivor = store.require(d.id)
    assert survivor.status == "running"
    assert survivor.claim_expires == store.now() + RECLAIM_GRACE_S
    assert "reclaim_deferred" in [e.kind for e in store.events(d.id)]


def test_reclaim_counts_toward_the_failure_limit(store: BoardStore, fake_clock: FakeClock) -> None:
    task = store.create("flaky")
    for _ in range(2):
        claimed = store.claim(task.id, "w")
        assert claimed is not None
        fake_clock.advance(CLAIM_TTL_S + 1)
        store.release_stale_claims(alive=lambda pid, fp: False)
    final = store.require(task.id)
    assert final.status == "blocked" and final.consecutive_failures == 2
    assert store.events(task.id)[-1].kind == "gave_up"


def test_third_recurrence_of_the_same_kind_escalates(store: BoardStore) -> None:
    task = store.create("needs a key")
    for round_ in (1, 2, 3):
        claimed = store.claim(task.id, f"w{round_}")
        assert claimed is not None
        blocked = store.block(
            task.id, reason="no API key", kind="capability", expected_run_id=claimed.current_run_id
        )
        assert blocked.block_recurrences == round_
        assert blocked.status == "blocked"
        assert blocked.escalated is (round_ == 3)
        if round_ < 3:
            store.unblock(task.id)
    assert store.events(task.id)[-1].kind == "block_loop"
    # A different kind starts counting again; a completion clears the count.
    store.unblock(task.id)
    other = store.block(task.id, reason="flaky network", kind="transient", force=True)
    assert other.block_recurrences == 1 and not other.escalated
    store.unblock(task.id)
    done = store.complete(task.id, summary="fixed")
    assert done.block_kind is None and done.block_recurrences == 0


def test_dependency_block_waits_in_todo_or_becomes_needs_input(store: BoardStore) -> None:
    parent = store.create("upstream")
    task = store.create("downstream")
    claimed = store.claim(task.id, "w")
    assert claimed is not None
    # No open parent: recorded as needs_input, so it is not respawned without context.
    landed = store.block(task.id, reason="waiting", kind="dependency", force=True)
    assert landed.status == "blocked" and landed.block_kind == "needs_input"
    store.unblock(task.id)
    store.link(parent.id, task.id)
    store._write(
        lambda conn: conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task.id,))
    )
    store.recompute_ready()
    waiting = store.block(task.id, reason="upstream first", kind="dependency")
    # A dependency wait is not a recurrence: the count of the earlier block stays as it was.
    assert waiting.status == "todo" and waiting.block_kind == "dependency"
    assert waiting.block_recurrences == 1
    store.complete(parent.id, summary="upstream ready")
    assert store.require(task.id).status == "ready"


def test_unblock_with_one_unfinished_parent_goes_to_todo(store: BoardStore) -> None:
    p1 = store.create("p1")
    p2 = store.create("p2")
    task = store.create("t")
    store.block(task.id, reason="hold", kind="needs_input")
    store.link(p1.id, task.id)
    store.link(p2.id, task.id)
    store.complete(p1.id, summary="ok")
    assert store.unblock(task.id).status == "todo"
    store.complete(p2.id, summary="ok")
    assert store.require(task.id).status == "ready"
    store.block(task.id, reason="hold again", kind="needs_input")
    assert store.unblock(task.id).status == "ready"
    with pytest.raises(BoardRefused):
        store.unblock(task.id)


def test_complete_needs_a_handoff_and_finished_parents(store: BoardStore) -> None:
    parent = store.create("p")
    task = store.create("t", parents=[parent.id])
    with pytest.raises(BoardRefused, match="unfinished parents"):
        store.complete(task.id, summary="x", force=True)
    store.complete(parent.id, summary="p done")
    with pytest.raises(BoardInputError, match="handoff"):
        store.complete(task.id, summary="   ")
    done = store.complete(task.id, summary="wrote the thing", metadata={"files": ["a.py"]})
    assert done.status == "done" and done.completed_at is not None
    run = store.latest_run(task.id)
    assert run is not None and run.summary == "wrote the thing"


def test_complete_refuses_a_live_claim_without_its_run(store: BoardStore) -> None:
    task = store.create("t")
    claimed = store.claim(task.id, "w")
    assert claimed is not None
    with pytest.raises(LiveClaim):
        store.complete(task.id, summary="not mine")
    with pytest.raises(BoardRefused, match="no longer owns"):
        store.complete(task.id, summary="stale", expected_run_id=(claimed.current_run_id or 0) + 99)
    done = store.complete(task.id, summary="mine", expected_run_id=claimed.current_run_id)
    assert done.status == "done"


def test_review_then_changes_returns_to_the_implementer(store: BoardStore) -> None:
    task = store.create("feature", assignee="dev")
    run = store.claim(task.id, "w-dev")
    assert run is not None
    in_review = store.request_review(
        task.id,
        summary="implemented, tests pass",
        reviewer="qa",
        expected_run_id=run.current_run_id,
    )
    assert in_review.status == "review" and in_review.assignee == "qa"
    review_run = store.claim_review(task.id, "w-qa")
    assert review_run is not None and store.latest_run(task.id).source == "review"  # type: ignore[union-attr]
    back = store.request_changes(
        task.id, reason="missing docs", expected_run_id=review_run.current_run_id
    )
    assert back.status == "ready" and back.assignee == "dev"
    # Second round: a person approves straight from review without a summary.
    again = store.claim(task.id, "w-dev-2")
    assert again is not None
    store.request_review(
        task.id, summary="docs added", reviewer="qa", expected_run_id=again.current_run_id
    )
    approved = store.complete(task.id)
    assert approved.status == "done"
    # A person can also send a task in review back with a comment.
    other = store.create("other", assignee="dev")
    store.request_review(other.id, summary="ready", reviewer="lead", force=True)
    reopened = store.reopen_review(other.id, reason="rename the flag", author="lead")
    assert reopened.status == "ready" and reopened.assignee == "dev"
    assert store.comments(other.id)[-1].body == "rename the flag"


def test_attachment_cap_is_25_mib(store: BoardStore) -> None:
    task = store.create("t")
    with pytest.raises(AttachmentTooLarge):
        store.attach_bytes(task.id, "big.bin", b"\0" * (ATTACHMENT_MAX_BYTES + 1))
    assert not store.attachments_dir(task.id).exists() or not any(
        store.attachments_dir(task.id).iterdir()
    )
    kept = store.attach_bytes(task.id, "../../etc/passwd", b"\0" * ATTACHMENT_MAX_BYTES)
    assert kept.filename == "passwd" and kept.size == ATTACHMENT_MAX_BYTES
    second = store.attach_bytes(task.id, "passwd", b"x")
    assert second.filename == "passwd (1)"
    assert [a.filename for a in store.attachments(task.id)] == ["passwd", "passwd (1)"]
    with pytest.raises(BoardInputError):
        store.attach_bytes(task.id, "...", b"x")


def test_links_refuse_cycles(store: BoardStore) -> None:
    a = store.create("a")
    b = store.create("b")
    c = store.create("c")
    assert store.link(a.id, b.id) is True
    store.link(b.id, c.id)
    with pytest.raises(BoardInputError, match="cycle"):
        store.link(c.id, a.id)
    with pytest.raises(BoardInputError):
        store.link(a.id, a.id)
    assert store.parents(c.id) == [b.id]
    assert store.children(a.id) == [b.id]


def test_schedule_parks_until_its_time(store: BoardStore, fake_clock: FakeClock) -> None:
    task = store.create("later")
    parked = store.schedule(task.id, reason="after the release", wake_at=store.now() + 600)
    assert parked.status == "blocked" and parked.scheduled
    assert store.wake_scheduled() == []
    fake_clock.advance(601)
    assert store.wake_scheduled() == [task.id]
    assert store.require(task.id).status == "ready"


def test_archive_returns_the_worker_and_releases_children(store: BoardStore) -> None:
    parent = store.create("p")
    child = store.create("c", parents=[parent.id])
    claimed = store.claim(parent.id, "w")
    assert claimed is not None and claimed.current_run_id is not None
    store.set_worker(parent.id, claimed.current_run_id, 4242, "fp")
    before = store.archive(parent.id)
    assert before.worker_pid == 4242
    assert store.require(parent.id).status == "archived"
    assert store.require(child.id).status == "ready"


def test_subscription_cursor_starts_at_the_newest_event(store: BoardStore) -> None:
    task = store.create("t")
    assert store.subscribe(task.id, platform="telegram", chat_id="42")
    assert not store.subscribe(task.id, platform="telegram", chat_id="42")
    sub = store.subscriptions(task.id)[0]
    assert store.events_after(sub.cursor, task_id=task.id) == []
    store.comment(task.id, "alice", "hello")
    assert [e.kind for e in store.events_after(sub.cursor, task_id=task.id)] == ["commented"]
