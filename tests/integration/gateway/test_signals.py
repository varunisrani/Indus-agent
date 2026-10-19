"""P15-25 (+ P15-26 across real restarts): signals to a real gateway process in a tmp home.

The model is the scripted fake on a loopback port; the platform is the spool plugin. SIGTERM in
the middle of a turn interrupts it, tells the chat and exits 0; the next boot resumes it once (and
the one after does nothing). SIGUSR1 lets the running turn finish, refuses new ones, exits 75.
SIGUSR2 writes the stack log and the daemon keeps running.
"""

from __future__ import annotations

import asyncio
import os
import signal
import threading
from pathlib import Path

import pytest

from indus.foundation.clock import SystemClock
from indus.foundation.scope import make_scope
from indus.gateway.daemon.drain import clear_drain_marker, write_drain_marker
from indus.gateway.daemon.lifecycle import (
    INTERRUPTED_REASON,
    MAINTENANCE_NOTICE,
    RESTARTING_NOTICE,
    STOPPED_TURN_NOTICE,
)
from indus.gateway.daemon.parts import DaemonPart
from indus.gateway.daemon.process import GatewayDaemon
from indus.gateway.daemon.recovery import HEADS_UP
from indus.safety.child_env import child_env
from indus.state import gateway_routes as routes
from indus.state import registry as store_registry
from indus.state.gateway_runtime import (
    EXIT_OK,
    EXIT_RESTART,
    GATEWAY_GUARD_ENV,
    faulthandler_log_path,
    read_status,
    record_planned_stop,
)
from tests.fakes.model import Finish, Stall, Text, Turn, script_of, text_turn
from tests.fakes.model.http_serve import serving
from tests.integration.gateway.daemon_kit import (
    ALLOWED,
    daemon_scope,
    fake_entry,
    fake_platform,
    fake_registry,
    fake_service,
    fly,
    model_server,
    quick,
)
from tests.integration.gateway.spool import SpoolHome

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX signals")


def _routes(home: SpoolHome) -> list[routes.RouteEntry]:
    scope = make_scope(home.home, env={"INDUS_HOME": str(home.home)}, clock=SystemClock())
    with store_registry.shared(scope) as store:
        return routes.list_routes(store)


@pytest.mark.slow
def test_sigterm_mid_turn_interrupts_notifies_and_the_next_boot_resumes_once(
    tmp_path: Path,
) -> None:
    script = script_of(
        Turn((Text(("thinking",)), Stall(120.0), Finish("stop"))),
        text_turn("Resumed and finished"),
    )
    with serving(script) as model:
        home = SpoolHome(tmp_path, model.port)
        first = home.start()
        try:
            first.running()
            home.send("run the long job")
            first.until(lambda: model.model_turns >= 1, "the model call")
            first.signal(signal.SIGTERM)
            assert first.wait() == EXIT_OK
        finally:
            first.kill()
        assert home.outbound() == [STOPPED_TURN_NOTICE]
        (entry,) = _routes(home)
        assert entry.resume_pending and entry.resume_reason == INTERRUPTED_REASON
        assert entry.active_turn_token is None

        second = home.start()
        try:
            second.running()
            second.until(lambda: len(home.outbound()) == 3, "the resumed reply")
            assert second.stop() == EXIT_OK
        finally:
            second.kill()
        assert home.outbound()[1:] == [HEADS_UP, "Resumed and finished"]
        (entry,) = _routes(home)
        assert not entry.resume_pending and entry.active_turn_token is None

        third = home.start()
        try:
            status = third.running()
            record_planned_stop(home.home, status.pid, "update")
            assert third.stop() == EXIT_OK
        finally:
            third.kill()
        assert len(home.outbound()) == 3  # never a second resume
        assert model.model_turns == 2
        final = home.status()
        assert final is not None and final.exit_reason == "planned_stop:update"
    assert home.stray_processes() == []


@pytest.mark.slow
def test_sigusr1_lets_the_turn_finish_refuses_new_ones_and_exits_75(tmp_path: Path) -> None:
    script = script_of(Turn((Text(("slow ",)), Stall(2.0), Text(("answer",)), Finish("stop"))))
    with serving(script) as model:
        home = SpoolHome(tmp_path, model.port, {"INDUS_SPOOL_ALLOWED_USERS": "200,201"})
        daemon = home.start()
        try:
            daemon.running()
            home.send("please take your time")
            daemon.until(lambda: model.model_turns >= 1, "the model call")
            daemon.signal(signal.SIGUSR1)
            daemon.until(
                lambda: (s := home.status()) is not None and s.state == "draining", "draining"
            )
            home.send("me too", user="201")
            daemon.until(lambda: home.outbound("201") == [RESTARTING_NOTICE], "the refusal")
            assert daemon.wait() == EXIT_RESTART
        finally:
            daemon.kill()
    assert home.outbound() == ["slow answer"]
    final = home.status()
    assert final is not None and final.state == "stopped"
    assert final.exit_reason == "signal:SIGUSR1" and final.exit_code == EXIT_RESTART
    assert all(not entry.resume_pending for entry in _routes(home))
    assert home.stray_processes() == []


@pytest.mark.slow
def test_sigusr2_dumps_stacks_and_keeps_running(tmp_path: Path) -> None:
    with serving(script_of()) as model:
        home = SpoolHome(tmp_path, model.port)
        daemon = home.start()
        try:
            daemon.running()
            log = faulthandler_log_path(home.home)
            daemon.signal(signal.SIGUSR2)
            daemon.until(
                lambda: log.exists() and "asyncio task(s)" in log.read_text(), "the stack dump"
            )
            text = log.read_text(encoding="utf-8")
            assert "stack dump: SIGUSR2" in text and "gateway housekeeping" in text
            assert daemon.proc.poll() is None
            status = home.status()
            assert status is not None and status.state == "running"
            assert daemon.stop() == EXIT_OK
        finally:
            daemon.kill()
    assert home.stray_processes() == []


# -- in-process: the drain marker and the watchdog exits -------------------------------------------


def test_an_external_drain_marker_refuses_new_turns_until_it_is_removed(tmp_path: Path) -> None:
    scope = daemon_scope(tmp_path)
    fake = fake_platform()
    server = model_server(text_turn("back to work"))
    daemon = GatewayDaemon(
        scope,
        options=quick(drain_poll=0.05),
        registry=fake_registry(fake_entry(fake)),
        service_factory=fake_service(server, tmp_path / "svc-locks"),
        parts=[],
    )

    async def main() -> None:
        flight = await fly(daemon)
        write_drain_marker(scope.home, principal="dashboard")
        await flight.until(lambda: daemon.status.state == "draining")
        fake.dm(ALLOWED, "anyone home?")
        await flight.until(lambda: fake.texts(ALLOWED) == [MAINTENANCE_NOTICE])
        assert server.requests == []
        clear_drain_marker(scope.home)
        await flight.until(lambda: daemon.status.state == "running")
        fake.dm(ALLOWED, "now?")
        await flight.until(lambda: fake.texts(ALLOWED)[-1:] == ["back to work"])
        assert await flight.stop() == EXIT_OK

    asyncio.run(main())
    assert daemon.status.history[:4] == ["starting", "running", "draining", "running"]


def test_a_wedged_loop_trips_the_watchdog_writes_degraded_and_exits_75(tmp_path: Path) -> None:
    scope = daemon_scope(tmp_path)
    exits: list[tuple[int, str | None]] = []
    tripped = threading.Event()

    def hard_exit(code: int) -> None:
        status = read_status(scope.home)
        exits.append((code, None if status is None else f"{status.state}:{status.exit_reason}"))
        tripped.set()  # a real daemon would be gone here; the test's carries on

    daemon = GatewayDaemon(
        scope,
        options=quick(
            watchdogs=True,
            loop_watchdog_interval=0.05,
            loop_watchdog_timeout=0.05,
            loop_watchdog_strikes=3,
            hard_exit=hard_exit,
        ),
        registry=fake_registry(fake_entry(fake_platform())),
        service_factory=fake_service(model_server(), tmp_path / "svc-locks"),
        parts=[],
    )

    async def main() -> None:
        flight = await fly(daemon)
        tripped.wait(10)  # blocks the loop itself: the watchdog's probes go unanswered
        assert await flight.stop() == EXIT_OK

    asyncio.run(main())
    assert exits == [(EXIT_RESTART, "degraded:loop_watchdog")]
    dump = faulthandler_log_path(scope.home).read_text(encoding="utf-8")
    assert "stack dump: loop watchdog" in dump


def test_the_daemon_marks_its_children_for_their_own_lifecycle_commands(tmp_path: Path) -> None:
    scope = daemon_scope(tmp_path)
    seen: list[dict[str, str]] = []

    class Peek(DaemonPart):
        async def start(self, daemon: GatewayDaemon) -> None:
            seen.append(child_env(daemon.scope))

    daemon = GatewayDaemon(
        scope,
        options=quick(export_guard_env=True),
        registry=fake_registry(fake_entry(fake_platform())),
        service_factory=fake_service(model_server(), tmp_path / "svc-locks"),
        parts=[Peek()],
    )

    async def main() -> None:
        flight = await fly(daemon)
        assert await flight.stop() == EXIT_OK

    asyncio.run(main())
    (env,) = seen
    assert env[GATEWAY_GUARD_ENV] == "1"  # a tool subprocess of this gateway sees the marker
    assert not any(name.startswith("_INDUS_FORCE_") for name in env)
    assert GATEWAY_GUARD_ENV not in child_env(scope)  # nothing outside the daemon has it
