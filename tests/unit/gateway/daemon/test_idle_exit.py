"""P17-23: an auto-started daemon exits after its idle window (fake clock), and only then."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from indus.cron.grammar import parse_schedule
from indus.cron.models import CronJob, JobSpec
from indus.cron.store import CronStore
from indus.cron.ticker import pause_all
from indus.foundation.scope import Scope
from indus.gateway.daemon.idle_exit import (
    DEFAULT_IDLE_EXIT_S,
    IDLE_EXIT_REASON,
    DaemonProbe,
    IdleWatch,
    arm,
    autostarted,
    cron_busy,
    idle_exit_seconds,
    poll_interval,
)
from indus.gateway.daemon.lifecycle import Lifecycle
from indus.gateway.daemon.process import GatewayDaemon
from indus.service import gateway_ensure
from indus.service.gateway_ensure import AUTOSTART_ENV, planned_stop_reason
from indus.service.viewers import Viewer, ViewerRegistry
from indus.service.ws_auth import WS_INTERACTIVE, TicketBook
from indus.state import registry
from indus.state import runtime_ledger as ledger
from indus.state.gateway_runtime import (
    EXIT_OK,
    ProcessIdentity,
    RuntimeStatus,
    read_status,
    write_status,
)
from indus.state.registry import StoreHandle
from tests.conftest import FAKE_CLOCK_START
from tests.fakes.clock import FakeClock
from tests.unit.backends._aio import aio

# -- the watch on a fake clock ----------------------------------------------------------------


@dataclass
class Scripted:
    """A probe whose answers the test sets."""

    busy: str | None = None
    stored: str | None = None
    print_: int = 0
    finals: list[str | None] = field(default_factory=list[str | None])
    raise_slow: bool = False

    def quick(self) -> str | None:
        if self.finals and self.busy is None:
            return self.finals.pop(0)
        return self.busy

    async def slow(self) -> str | None:
        if self.raise_slow:
            raise OSError("store unreadable")
        return self.stored

    def fingerprint(self) -> int:
        return self.print_


class Rig:
    def __init__(self, window: float = 600.0) -> None:
        self.clock = FakeClock(FAKE_CLOCK_START)
        self.probe = Scripted()
        self.window = window
        self.fired: list[float] = []
        self.stopping = False
        self.watch = IdleWatch(
            self.probe,
            clock=self.clock,
            window=lambda: self.window,
            on_idle=self.fired.append,
            finished=lambda: self.stopping,
        )

    async def start(self) -> asyncio.Task[None]:
        task = asyncio.ensure_future(self.watch.run())
        await self.clock.run_until_idle()
        return task

    async def to(self, second: float, step: float = 30.0) -> None:
        """Move the clock to ``second`` (from start), stopping at every multiple of ``step`` (the
        polls of a 600 s window) so each poll sees the time it was due at."""
        while True:
            now = self.clock.monotonic()
            boundary = (now // step + 1) * step
            if boundary > second:
                break
            await self.clock.advance_and_settle(boundary - now)
        left = second - self.clock.monotonic()
        if left > 0:
            await self.clock.advance_and_settle(left)


class OneValue:
    """A config source answering every key with one value."""

    def __init__(self, value: object) -> None:
        self.value = value

    def get(self, path: str, /) -> object:
        return self.value


def test_the_window_setting_and_the_poll_interval(scope: Scope) -> None:
    assert idle_exit_seconds(scope) == DEFAULT_IDLE_EXIT_S == 600.0
    for value, expected in [(0, 0.0), (-5, 0.0), (90.5, 90.5), ("soon", 600.0), (True, 600.0)]:
        changed = scope.with_config(OneValue(value))
        assert idle_exit_seconds(changed) == expected
    assert poll_interval(600) == 30.0
    assert poll_interval(40) == 10.0
    assert poll_interval(0.2) == 0.1


@aio
async def test_exits_at_600_s_of_idle() -> None:
    rig = Rig()
    task = await rig.start()
    await rig.to(599)
    assert rig.fired == []
    await rig.to(600)
    assert rig.fired == [600.0]
    await asyncio.wait_for(task, 1)
    assert rig.watch.fired


@aio
async def test_a_busy_poll_restarts_the_window() -> None:
    rig = Rig()
    task = await rig.start()
    await rig.to(580)
    rig.probe.busy = "a viewer is attached to s1"
    await rig.to(600)
    assert rig.watch.busy == "a viewer is attached to s1" and rig.watch.idle_since == 600
    rig.probe.busy = None
    await rig.to(1199)
    assert rig.fired == []
    await rig.to(1200)
    assert rig.fired == [600.0]
    await task


@aio
async def test_activity_between_two_polls_counts() -> None:
    rig = Rig()
    task = await rig.start()
    await rig.to(590)
    rig.probe.print_ += 1  # a client came and went between the polls at 570 and 600
    await rig.to(1199)
    assert rig.fired == []
    await rig.to(1200)
    assert rig.fired == [600.0]
    await task


@aio
async def test_a_failing_probe_keeps_the_daemon_up() -> None:
    rig = Rig()
    rig.probe.raise_slow = True
    task = await rig.start()
    await rig.to(3000)
    assert rig.fired == [] and rig.watch.busy == "idle probe failed (OSError)"
    rig.probe.raise_slow = False
    await rig.to(3600)
    assert rig.fired == [600.0]
    await task


@aio
async def test_a_stored_reason_keeps_the_daemon_up() -> None:
    rig = Rig()
    rig.probe.stored = "cron job j1 is due"
    task = await rig.start()
    await rig.to(1800)
    assert rig.fired == [] and rig.watch.busy == "cron job j1 is due"
    rig.stopping = True
    await rig.to(1830)
    await asyncio.wait_for(task, 1)
    assert rig.fired == []


@aio
async def test_window_zero_disarms_and_a_new_window_counts_from_then() -> None:
    rig = Rig(window=0)
    task = await rig.start()
    await rig.to(6000)
    assert rig.fired == []
    rig.window = 600
    await rig.to(6599)
    assert rig.fired == []
    await rig.to(6600)
    assert rig.fired == [600.0]
    await task


@aio
async def test_the_last_look_vetoes_the_exit() -> None:
    rig = Rig()
    task = await rig.start()
    await rig.to(570)
    rig.probe.finals = [None, "a client is attaching"]  # idle at the poll, busy on the re-check
    await rig.to(600)
    assert rig.fired == [] and rig.watch.busy == "a client is attaching"
    await rig.to(1200)
    assert rig.fired == [600.0]
    await task


# -- the daemon probe ---------------------------------------------------------------------------


NOW = FAKE_CLOCK_START


def _job(schedule: str = "every 1h", **fields: Any) -> CronJob:
    parsed = parse_schedule(schedule, UTC, now=NOW)
    data: dict[str, Any] = {
        "id": fields.pop("id", "a1b2c3d4e5f6"),
        "created_at": NOW - timedelta(days=1),
        "prompt": "check",
        "schedule": parsed,
        "schedule_display": parsed.display,
        "next_run_at": NOW + timedelta(hours=1),
    }
    data.update(fields)
    return CronJob.model_validate(data)


def test_cron_due_within_the_next_tick_or_running_keeps_the_daemon(tmp_path: Path) -> None:
    def busy(*jobs: CronJob, paused: bool = False) -> str | None:
        return cron_busy(jobs, home=tmp_path, now=NOW, paused=paused)

    assert busy() is None
    assert busy(_job()) is None  # due in an hour
    assert busy(_job(next_run_at=NOW + timedelta(seconds=61))) is None
    assert busy(_job(next_run_at=NOW + timedelta(seconds=60))) == "cron job a1b2c3d4e5f6 is due"
    assert busy(_job(next_run_at=NOW - timedelta(minutes=5))) is not None  # overdue
    assert busy(_job(next_run_at=None)) is not None  # the next tick plans it
    soon = NOW + timedelta(seconds=30)
    assert busy(_job(next_run_at=soon, enabled=False)) is None
    assert busy(_job(next_run_at=soon, state="paused", paused_at=NOW)) is None
    assert busy(_job(next_run_at=soon, state="completed")) is None
    assert busy(_job(next_run_at=soon, quota_hold_until=NOW + timedelta(hours=2))) is None
    assert busy(_job(state="error")) == "cron job a1b2c3d4e5f6 is due for repair"
    assert busy(_job(next_run_at=soon), paused=True) is None  # cron paused globally
    claim = {"owner": "gw-1", "claimed_at": NOW.isoformat(), "heartbeat_at": NOW.isoformat()}
    running = _job(fire_claim=claim)
    assert busy(running, paused=True) == "cron job a1b2c3d4e5f6 is running"
    stale = {**claim, "heartbeat_at": (NOW - timedelta(hours=1)).isoformat()}
    assert busy(_job(fire_claim=stale)) is None


def _viewer(connection_id: str, surface: str) -> Viewer:
    return Viewer.of(connection_id, surface, send=lambda _event: None)


class FakeHost:
    def __init__(self) -> None:
        self.viewers = ViewerRegistry()
        self.keys: list[str] = []
        self.running_gen: dict[str, int] = {}
        self.seq: dict[str, int] = {}
        self.fences = SimpleNamespace(running=self.running_gen.get)
        self.admissions = SimpleNamespace(running=lambda _key: None)
        self.event_log = SimpleNamespace(last_seq=lambda key: self.seq.get(key, 0))

    def hosted(self) -> list[str]:
        return list(self.keys)


class FakeService:
    def __init__(self) -> None:
        self.held: dict[str, bool] = {}

    def attached(self) -> list[str]:
        return list(self.held)

    def is_idle(self, session_id: str) -> bool:
        return self.held[session_id]


class StandIn:
    """The parts of ``GatewayDaemon`` the probe reads."""

    def __init__(self, scope: Scope, store: StoreHandle | None = None) -> None:
        self.scope = scope
        self.clock = scope.clock
        self.lifecycle = Lifecycle()
        self.enabled: list[str] = []
        self.settings = SimpleNamespace(enabled=lambda: list(self.enabled))
        self.adapters: dict[str, object] = {}
        self.parts: list[Any] = []
        self.tickets = TicketBook.for_home(scope.home, monotonic=scope.clock.monotonic)
        self.ws: Any = None
        self.host = FakeHost()
        self.service = FakeService()
        self.pipeline = SimpleNamespace(claims=[])
        self.terminal_processes: Any = None
        self.automation: Any = None
        if store is not None:
            self.store = store

    def probe(self) -> DaemonProbe:
        return DaemonProbe(cast(GatewayDaemon, self))


@pytest.fixture
def store(scope: Scope) -> Iterator[StoreHandle]:
    handle = registry.open_unshared(scope)
    yield handle
    handle.close()


def test_every_quick_reason(scope: Scope) -> None:
    daemon = StandIn(scope)
    probe = daemon.probe()
    assert probe.quick() is None
    checks: list[tuple[Callable[[], object], Callable[[], object], str]] = [
        (
            lambda: daemon.enabled.append("telegram"),
            daemon.enabled.clear,
            "messaging platform enabled: telegram",
        ),
        (
            lambda: daemon.parts.append(SimpleNamespace(name="http")),
            daemon.parts.clear,
            "HTTP surfaces enabled",
        ),
        (
            lambda: daemon.tickets.mint(WS_INTERACTIVE),
            daemon.tickets.revoke_all,
            "a client is attaching",
        ),
        (
            lambda: setattr(daemon, "ws", SimpleNamespace(connections={"ws-1": object()})),
            lambda: setattr(daemon, "ws", None),
            "1 client connection(s)",
        ),
        (
            lambda: daemon.host.viewers.attach("s1", _viewer("ws-2", "cli")),
            lambda: daemon.host.viewers.forget("s1"),
            "a viewer is attached to s1",
        ),
        (
            lambda: (daemon.host.keys.append("s1"), daemon.host.running_gen.update(s1=3)),
            daemon.host.running_gen.clear,
            "a turn runs in s1",
        ),
        (
            lambda: daemon.service.held.update(s2=False),
            lambda: daemon.service.held.update(s2=True),
            "session s2 is busy",
        ),
        (
            lambda: daemon.pipeline.claims.append("k"),
            daemon.pipeline.claims.clear,
            "a chat turn runs",
        ),
        (
            lambda: daemon.lifecycle.request_stop("test"),
            lambda: None,
            "already stopping",
        ),
    ]
    for make, undo, reason in checks:
        make()
        assert probe.quick() == reason
        undo()
        if reason != "already stopping":
            assert probe.quick() is None, reason


def test_the_messaging_intake_is_not_a_client(scope: Scope) -> None:
    daemon = StandIn(scope)
    daemon.host.viewers.attach("chat-1", _viewer("intake", "messaging"))
    assert daemon.probe().quick() is None
    daemon.host.viewers.attach("chat-1", _viewer("ws-9", "tui"))
    assert daemon.probe().quick() == "a viewer is attached to chat-1"


def test_a_background_process_in_a_held_session_keeps_the_daemon(scope: Scope) -> None:
    daemon = StandIn(scope)
    daemon.service.held["s1"] = True
    records = {"s1": [SimpleNamespace(status="exited")]}
    daemon.terminal_processes = SimpleNamespace(list=lambda _home, sid: records.get(sid, []))
    assert daemon.probe().quick() is None
    records["s1"].append(SimpleNamespace(status="running"))
    assert daemon.probe().quick() == "a background process runs"


def test_the_fingerprint_moves_with_connections_sessions_and_events(scope: Scope) -> None:
    daemon = StandIn(scope)
    probe = daemon.probe()
    first = probe.fingerprint()
    daemon.host.keys.append("s1")
    second = probe.fingerprint()
    daemon.host.seq["s1"] = 4
    third = probe.fingerprint()
    daemon.ws = SimpleNamespace(connections={"ws-1": object()})
    fourth = probe.fingerprint()
    assert len({first, second, third, fourth}) == 4
    assert probe.fingerprint() == fourth


@aio
async def test_queued_input_keeps_the_daemon_unless_it_waits_behind_an_unknown_turn(
    scope: Scope, store: StoreHandle
) -> None:
    daemon = StandIn(scope, store)
    probe = daemon.probe()
    assert await probe.slow() is None
    epoch = ledger.begin_owner_epoch(store, "daemon-1").epoch

    def admit(session_id: str, request_id: str) -> None:
        ledger.admit(
            store,
            epoch=epoch,
            principal_id="attach:cli",
            session_id=session_id,
            request_id=request_id,
            payload={"text": request_id},
        )

    admit("s1", "r1")
    assert await probe.slow() == "input is queued for s1"
    assert ledger.claim_head(store, epoch=epoch, session_id="s1").kind == "claimed"
    admit("s1", "r2")  # queued behind the started turn
    epoch = ledger.begin_owner_epoch(store, "daemon-2").epoch  # the started turn becomes unknown
    assert await probe.slow() is None  # that queue is paused until a person resolves it
    admit("s2", "r3")
    assert await probe.slow() == "input is queued for s2"


@aio
async def test_cron_from_the_store_and_due_loops(scope: Scope) -> None:
    daemon = StandIn(scope)
    probe = daemon.probe()
    cron = CronStore(scope)
    made = cron.create(JobSpec(schedule="every 1m", prompt="ping"))
    assert await probe.slow() == f"cron job {made.id} is due"
    pause_all(scope.home, "holiday", scope.clock.now())
    assert await probe.slow() is None
    due: list[Any] = []

    async def due_rows(namespace: str, before: float) -> list[Any]:
        return [row for row in due if row.namespace == namespace and row.due_at <= before]

    daemon.automation = SimpleNamespace(
        scheduler=SimpleNamespace(sources=[SimpleNamespace(namespace="loops")]),
        meta=SimpleNamespace(due=due_rows),
    )
    soon = scope.clock.now().timestamp() + 30
    due.append(SimpleNamespace(namespace="loops", session_id="s9", due_at=soon))
    assert await probe.slow() is None  # not a session this daemon holds
    daemon.service.held["s9"] = True
    assert await probe.slow() == "loops due in s9"


# -- arming and the real daemon -------------------------------------------------------------------


def test_only_the_ensure_marker_arms() -> None:
    assert autostarted({AUTOSTART_ENV: "1"})
    assert not autostarted({AUTOSTART_ENV: "0"})
    assert not autostarted({})


def test_a_supervised_or_hand_started_daemon_is_not_armed(scope: Scope) -> None:
    spawned: list[object] = []

    def keep(coro: Any) -> None:
        spawned.append(coro)
        coro.close()

    for started, supervised in [(False, False), (True, True)]:
        daemon = SimpleNamespace(autostarted=started, is_supervised=supervised)
        assert arm(cast(GatewayDaemon, daemon), spawn=keep) is None
    daemon = SimpleNamespace(
        autostarted=True,
        is_supervised=False,
        scope=scope,
        clock=scope.clock,
        lifecycle=Lifecycle(),
    )
    watch = arm(cast(GatewayDaemon, daemon), probe=Scripted(), spawn=keep)
    assert watch is not None and len(spawned) == 1 and watch.window() == 600.0


def _kit() -> Any:
    from tests.integration.gateway import daemon_kit

    return daemon_kit


def _daemon(tmp_path: Path, *, supervised: bool, window: float, parts: list[Any]) -> GatewayDaemon:
    kit = _kit()
    scope = kit.daemon_scope(
        tmp_path,
        env={"TELEGRAM_BOT_TOKEN": None, AUTOSTART_ENV: "1"},
    )
    return GatewayDaemon(
        scope,
        options=kit.quick(supervised=supervised, idle_exit_seconds=window, attach_ws=False),
        registry=kit.fake_registry(),
        service_factory=kit.fake_service(kit.model_server(), tmp_path / "svc-locks"),
        parts=parts,
    )


posix = pytest.mark.skipif(os.name != "posix", reason="POSIX locks")


@posix
def test_an_auto_started_daemon_stops_itself_as_idle_exit(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path, supervised=False, window=0.4, parts=[])
    code = asyncio.run(asyncio.wait_for(daemon.run(), 30))
    assert code == EXIT_OK
    assert daemon.autostarted and daemon.idle_watch is not None and daemon.idle_watch.fired
    assert daemon.scope.env.get(AUTOSTART_ENV) is None  # children do not inherit the marker
    assert daemon.status.history[-4:] == ["running", "draining", "stopping", "stopped"]
    status = read_status(daemon.scope.home)
    assert status is not None and status.state == "stopped"
    assert status.exit_reason == IDLE_EXIT_REASON and status.exit_code == 0
    assert planned_stop_reason(daemon.scope.home) is None  # the next ensure starts it again


@posix
def test_a_supervised_daemon_never_idles_out(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path, supervised=True, window=0.2, parts=[])

    async def main() -> None:
        flight = await _kit().fly(daemon)
        assert daemon.idle_watch is None
        await daemon.clock.sleep(1.0)
        assert daemon.status.state == "running"
        assert await flight.stop() == EXIT_OK

    asyncio.run(main())
    status = read_status(daemon.scope.home)
    assert status is not None and status.exit_reason == "test"


@posix
def test_a_due_cron_job_keeps_the_daemon(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path, supervised=False, window=0.3, parts=[])
    job = CronStore(daemon.scope).create(JobSpec(schedule="every 1m", prompt="ping"))

    async def main() -> None:
        flight = await _kit().fly(daemon)
        watch = daemon.idle_watch
        assert watch is not None
        await daemon.clock.sleep(1.2)
        assert daemon.status.state == "running" and not watch.fired
        assert watch.busy == f"cron job {job.id} is due"
        assert await flight.stop() == EXIT_OK

    asyncio.run(main())


# -- ensure after an idle exit -------------------------------------------------------------------


def _status(home: Path, state: str, reason: str | None) -> None:
    identity = ProcessIdentity.current()
    status = RuntimeStatus(cast(Any, state), "", identity, profile="work", exit_reason=reason)
    write_status(home, status.stamped(datetime.now(UTC)))


def test_ensure_treats_an_idle_exit_as_not_deliberate(tmp_path: Path) -> None:
    _status(tmp_path, "stopped", IDLE_EXIT_REASON)
    assert planned_stop_reason(tmp_path) is None
    _status(tmp_path, "stopped", "planned_stop:stop")
    assert planned_stop_reason(tmp_path) == "stop"


def test_ensure_waits_out_an_idle_exiting_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "profile"
    home.mkdir(mode=0o700)
    _status(home, "draining", IDLE_EXIT_REASON)
    me = os.getpid()
    answers: list[dict[str, Any] | None] = [{"pid": me}, None]
    monkeypatch.setattr(gateway_ensure.gateway_control, "identify", lambda _h: answers.pop(0))
    assert gateway_ensure._idle_exiting(home) is not None  # pyright: ignore[reportPrivateUsage]
    found = gateway_ensure.ensure(home, {}, mode="discover")
    assert (found.state, found.reason) == ("absent", IDLE_EXIT_REASON)

    # start: wait while the old pid lives, then start a new daemon.
    answers[:] = [{"pid": me}, None]
    alive = iter([True, True, False])
    monkeypatch.setattr(gateway_ensure.gateway_runtime, "alive", lambda _p, _s: next(alive))
    started: list[str] = []

    def fake_start(run: Any, service: Any) -> gateway_ensure.Ensured:
        started.append("start")
        return gateway_ensure.Ensured("ready", started=True)

    monkeypatch.setattr(gateway_ensure, "_start", fake_start)
    paused: list[float] = []
    found = gateway_ensure.ensure(home, {}, mode="start", pause=paused.append)
    assert found.started and started == ["start"] and len(paused) == 2

    # the deadline passes while it is still leaving: not attachable, nothing started.
    answers[:] = [{"pid": me}]
    monkeypatch.setattr(gateway_ensure.gateway_runtime, "alive", lambda _p, _s: True)
    clock = iter([0.0, 0.0, 5.0])
    found = gateway_ensure.ensure(
        home, {}, mode="recover", timeout=1.0, monotonic=lambda: next(clock), pause=paused.append
    )
    assert (found.state, found.reason) == ("starting", "timeout") and started == ["start"]
