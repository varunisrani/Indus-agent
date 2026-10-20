"""P15-24: the gateway daemon starts, serves, refuses a second copy and stops.

In-process daemons run over the fake platform and the scripted fake model; the last tests run the
real daemon as a child process in a tmp home (a plugin platform over spool files and the fake model
on a loopback port), signal it and check that nothing is left behind.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import ClassVar

import pytest

from indus.gateway.adapters.base import AdapterConnectError
from indus.gateway.daemon.parts import DaemonPart, Stage
from indus.gateway.daemon.process import GatewayDaemon
from indus.gateway.token_locks import TokenLocks
from indus.state.gateway_runtime import (
    EXIT_CONFIG,
    EXIT_OK,
    EXIT_RESTART,
    PidRecord,
    ProcessIdentity,
    RuntimeStatus,
    exits_log_path,
    pid_path,
    read_pid_record,
    read_status,
    write_status,
)
from tests.fakes.model import script_of, text_turn
from tests.fakes.model.http_serve import serving
from tests.integration.gateway.daemon_kit import (
    ALLOWED,
    HangingAdapter,
    RecordingPart,
    bot_token,
    daemon_scope,
    dead_pid,
    fake_entry,
    fake_platform,
    fake_registry,
    fake_service,
    fly,
    model_server,
    quick,
)
from tests.integration.gateway.spool import SpoolHome

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX locks and signals")


def run(coro: object) -> None:
    asyncio.run(coro)  # type: ignore[arg-type]


class ControlPart(RecordingPart):
    stage: ClassVar[Stage] = Stage.CONTROL
    required: ClassVar[bool] = True


class SweepPart(RecordingPart):
    stage: ClassVar[Stage] = Stage.RECOVERY


class TickerPart(RecordingPart):
    stage: ClassVar[Stage] = Stage.BACKGROUND


def test_a_dm_is_answered_and_the_status_file_follows_the_lifecycle(tmp_path: Path) -> None:
    scope = daemon_scope(tmp_path)
    fake = fake_platform()
    server = model_server(text_turn("Hello ", "there"))
    daemon = GatewayDaemon(
        scope,
        options=quick(),
        registry=fake_registry(fake_entry(fake)),
        service_factory=fake_service(server, tmp_path / "svc-locks"),
        parts=[],
    )

    async def main() -> None:
        flight = await fly(daemon)
        assert daemon.status.state == "running"
        record = read_pid_record(scope.home)
        assert record is not None and record.pid == os.getpid()
        status = read_status(scope.home)
        assert status is not None and status.state == "running"
        assert status.platforms["telegram"].state == "connected"
        assert pid_path(scope.home).stat().st_mode & 0o777 == 0o600
        fake.dm(ALLOWED, "hi bot")
        await flight.until(lambda: fake.texts(ALLOWED) == ["Hello there"])
        assert await flight.stop() == EXIT_OK

    run(main())
    assert daemon.status.history == ["starting", "running", "draining", "stopping", "stopped"]
    status = read_status(scope.home)
    assert status is not None and status.state == "stopped" and status.exit_reason == "test"
    assert status.exit_code == 0
    assert (scope.home / "gateway_state.json").stat().st_mode & 0o777 == 0o600
    assert not pid_path(scope.home).exists()
    assert TokenLocks(tmp_path / "token-locks").held() == []
    assert list((tmp_path / "token-locks").glob("telegram-*.lock")) == []


@pytest.mark.parametrize(("supervised", "expected"), [(False, EXIT_OK), (True, EXIT_RESTART)])
def test_a_second_daemon_for_the_same_profile_refuses(
    tmp_path: Path, supervised: bool, expected: int
) -> None:
    scope = daemon_scope(tmp_path)
    fake = fake_platform()
    first = GatewayDaemon(
        scope,
        options=quick(),
        registry=fake_registry(fake_entry(fake)),
        service_factory=fake_service(model_server(), tmp_path / "svc-locks"),
        parts=[],
    )
    built: list[object] = []

    async def never(daemon: GatewayDaemon) -> object:
        built.append(daemon)
        raise AssertionError("the second daemon must not assemble anything")

    async def main() -> None:
        flight = await fly(first)
        second = GatewayDaemon(
            scope,
            options=quick(supervised=supervised),
            registry=fake_registry(),
            service_factory=never,  # type: ignore[arg-type]
            parts=[],
        )
        assert await second.run() == expected
        assert second.holder is not None and second.holder.pid == os.getpid()
        assert first.status.state == "running"
        assert read_pid_record(scope.home) is not None
        assert await flight.stop() == EXIT_OK

    run(main())
    assert built == []


def test_a_token_collision_with_nothing_connected_exits_78(tmp_path: Path) -> None:
    scope = daemon_scope(tmp_path)
    other = TokenLocks(tmp_path / "token-locks", profile="other", pid=os.getppid())
    assert other.acquire("telegram", bot_token(), startup=True).acquired
    daemon = GatewayDaemon(
        scope,
        options=quick(),
        registry=fake_registry(fake_entry(fake_platform())),
        service_factory=fake_service(model_server(), tmp_path / "svc-locks"),
        parts=[],
    )
    assert asyncio.run(daemon.run()) == EXIT_CONFIG
    status = read_status(scope.home)
    assert status is not None and status.state == "startup_failed"
    assert status.exit_code == EXIT_CONFIG and "token_conflict" in (status.exit_reason or "")
    assert status.platforms["telegram"].state == "fatal"
    assert not pid_path(scope.home).exists()


def test_the_lock_key_is_the_entrys_lock_env_when_it_names_one(tmp_path: Path) -> None:
    """IRC locks on server + nick (neither a credential): the same pair held by another
    profile parks IRC as a conflict; another nick on the same server connects (R06.16)."""
    other = TokenLocks(tmp_path / "token-locks", profile="other", pid=os.getppid())
    assert other.acquire("irc", "irc.example.test\nindus", startup=True).acquired

    def daemon_for(profile: str, nick: str) -> GatewayDaemon:
        env = {"IRC_SERVER": "irc.example.test", "IRC_NICKNAME": nick}
        scope = daemon_scope(tmp_path, profile=profile, env=env)
        irc = replace(
            fake_entry(fake_platform("irc"), name="irc"), lock_env=("IRC_SERVER", "IRC_NICKNAME")
        )
        return GatewayDaemon(
            scope,
            options=quick(),
            registry=fake_registry(fake_entry(fake_platform()), irc),
            service_factory=fake_service(model_server(), tmp_path / f"svc-{profile}"),
            parts=[],
        )

    async def main() -> None:
        same = daemon_for("work", "indus")
        flight = await fly(same)
        assert same.platforms["telegram"].state == "connected"
        irc = same.platforms["irc"]
        assert irc.state == "fatal" and irc.error_code == "token_conflict"
        assert await flight.stop() == EXIT_OK
        renamed = daemon_for("play", "indus2")
        flight = await fly(renamed)
        assert renamed.platforms["irc"].state == "connected"
        assert await flight.stop() == EXIT_OK

    run(main())


def test_every_connected_adapter_gets_the_drain_probe_until_it_goes_down(tmp_path: Path) -> None:
    """The base ``set_drain_probe`` seam: the daemon installs ``lifecycle.notice`` on platform up
    (Google Chat answers 503 and pauses Pub/Sub through it) and removes it on the way down."""
    scope = daemon_scope(tmp_path)
    daemon = GatewayDaemon(
        scope,
        options=quick(),
        registry=fake_registry(fake_entry(fake_platform())),
        service_factory=fake_service(model_server(), tmp_path / "svc-locks"),
        parts=[],
    )

    async def main() -> None:
        flight = await fly(daemon)
        adapter = daemon.adapters["telegram"]
        assert adapter.draining() is False
        daemon.lifecycle.set_draining("maintenance")
        assert adapter.draining() is True
        daemon.lifecycle.clear_draining()
        assert adapter.draining() is False
        assert await flight.stop() == EXIT_OK
        # stopping drains the lifecycle; the probe left with the platform
        assert daemon.lifecycle.draining and adapter.draining() is False

    run(main())


def test_a_missing_library_is_fatal_for_that_platform_while_the_others_connect(
    tmp_path: Path,
) -> None:
    scope = daemon_scope(tmp_path, env={"DISCORD_BOT_TOKEN": bot_token("discord")})
    fake = fake_platform()
    registry = fake_registry(
        fake_entry(fake), fake_entry(fake_platform("discord"), name="discord", installed=False)
    )
    daemon = GatewayDaemon(
        scope,
        options=quick(),
        registry=registry,
        service_factory=fake_service(model_server(), tmp_path / "svc-locks"),
        parts=[],
    )

    async def main() -> None:
        flight = await fly(daemon)
        assert daemon.platforms["telegram"].state == "connected"
        discord = daemon.platforms["discord"]
        assert discord.state == "fatal" and discord.error_code == "missing_dependency"
        assert "discord extra" in (discord.error or "")
        assert daemon.status.state == "degraded"  # a parked platform never heals on its own
        assert await flight.stop() == EXIT_OK

    run(main())


def test_nothing_installed_is_not_a_fatal_config_the_daemon_keeps_running(tmp_path: Path) -> None:
    scope = daemon_scope(tmp_path)
    daemon = GatewayDaemon(
        scope,
        options=quick(),
        registry=fake_registry(fake_entry(fake_platform(), installed=False)),
        service_factory=fake_service(model_server(), tmp_path / "svc-locks"),
        parts=[],
    )

    async def main() -> None:
        flight = await fly(daemon)
        assert daemon.status.state == "degraded" and not flight.task.done()
        assert await flight.stop() == EXIT_OK

    run(main())


def test_parts_start_by_stage_and_stop_in_reverse(tmp_path: Path) -> None:
    scope = daemon_scope(tmp_path)
    log: list[str] = []
    ups: list[str] = []
    downs: list[tuple[str, bool, str]] = []

    class Listener(TickerPart):
        async def prepare(self, daemon: GatewayDaemon) -> None:
            await super().prepare(daemon)
            daemon.on_platform_up(lambda name, adapter: ups.append(name))
            daemon.on_platform_down(lambda name, fatal, why: downs.append((name, fatal, why)))

    parts: list[DaemonPart] = [
        Listener(log, "ticker"),
        SweepPart(log, "sweep"),
        ControlPart(log, "socket"),
    ]
    daemon = GatewayDaemon(
        scope,
        options=quick(),
        registry=fake_registry(fake_entry(fake_platform())),
        service_factory=fake_service(model_server(), tmp_path / "svc-locks"),
        parts=parts,
    )

    async def main() -> None:
        flight = await fly(daemon)
        assert await flight.stop() == EXIT_OK

    run(main())
    assert log == [
        "ticker:prepare:AdmissionPipeline",
        "sweep:prepare:AdmissionPipeline",
        "socket:prepare:AdmissionPipeline",
        "socket:start:[]",  # the control socket starts before any platform
        "sweep:start:['telegram']",
        "ticker:start:['telegram']",
        "ticker:stop",
        "sweep:stop",
        "socket:stop",  # last: it answers while the daemon drains
    ]
    assert ups == ["telegram"]
    assert downs == [("telegram", False, "stopping")]


def test_a_required_control_part_that_cannot_start_exits_75(tmp_path: Path) -> None:
    scope = daemon_scope(tmp_path)
    log: list[str] = []
    fake = fake_platform()
    daemon = GatewayDaemon(
        scope,
        options=quick(),
        registry=fake_registry(fake_entry(fake)),
        service_factory=fake_service(model_server(), tmp_path / "svc-locks"),
        parts=[ControlPart(log, "socket", fail_start=True)],
    )
    assert asyncio.run(daemon.run()) == EXIT_RESTART
    status = read_status(scope.home)
    assert status is not None and status.state == "startup_failed"
    assert "cannot start" in (status.exit_reason or "")
    assert fake.ops_of("connect") == []  # nothing connected without the control socket
    assert "socket:stop" not in log


def test_a_connect_timeout_leaves_the_platform_retrying(tmp_path: Path) -> None:
    scope = daemon_scope(tmp_path, env={"DISCORD_BOT_TOKEN": bot_token("discord")})
    downs: list[tuple[str, bool, str]] = []

    class Down(TickerPart):
        async def prepare(self, daemon: GatewayDaemon) -> None:
            daemon.on_platform_down(lambda name, fatal, why: downs.append((name, fatal, why)))

    class Refused(HangingAdapter):
        error = AdapterConnectError("bad token", retryable=False, code="unauthorized")

    registry = fake_registry(
        fake_entry(fake_platform(), adapter=HangingAdapter),
        fake_entry(fake_platform("discord"), name="discord", adapter=Refused),
    )
    daemon = GatewayDaemon(
        scope,
        options=quick(connect_timeout=0.3, platform_connect_timeouts={}),
        registry=registry,
        service_factory=fake_service(model_server(), tmp_path / "svc-locks"),
        parts=[Down([], "down")],
    )

    async def main() -> None:
        flight = await fly(daemon)
        assert daemon.status.state == "degraded"  # discord is parked; telegram may still come back
        telegram = daemon.platforms["telegram"]
        assert telegram.state == "retrying" and telegram.error_code == "connect_timeout"
        assert telegram.retrying_since
        assert daemon.platforms["discord"].state == "fatal"
        assert await flight.stop() == EXIT_OK

    run(main())
    assert ("telegram", False, "connect_timeout") in downs
    assert ("discord", True, "unauthorized") in downs


def test_an_unclean_previous_death_is_detected(tmp_path: Path) -> None:
    scope = daemon_scope(tmp_path)
    gone = ProcessIdentity(dead_pid(), 12345, None, "")
    write_status(scope.home, RuntimeStatus("running", "2026-01-01T09:00:00+00:00", gone))
    daemon = GatewayDaemon(
        scope,
        options=quick(),
        registry=fake_registry(fake_entry(fake_platform())),
        service_factory=fake_service(model_server(), tmp_path / "svc-locks"),
        parts=[],
    )

    async def main() -> None:
        flight = await fly(daemon)
        status = read_status(scope.home)
        assert status is not None and status.previous_exit == "unclean"
        assert daemon.boot.unclean_previous and daemon.boot.previous_run
        assert await flight.stop() == EXIT_OK

    run(main())
    lines = exits_log_path(scope.home).read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[-1])
    assert entry["event"] == "previous_unclean_exit" and entry["pid"] == gone.pid


def test_a_stale_pid_record_without_a_lock_does_not_block_a_start(tmp_path: Path) -> None:
    scope = daemon_scope(tmp_path)
    stale = PidRecord(ProcessIdentity(os.getpid(), 1, None, ""), home=str(scope.home))
    pid_path(scope.home).write_text(json.dumps(stale.to_json()), encoding="utf-8")
    daemon = GatewayDaemon(
        scope,
        options=quick(),
        registry=fake_registry(fake_entry(fake_platform())),
        service_factory=fake_service(model_server(), tmp_path / "svc-locks"),
        parts=[],
    )

    async def main() -> None:
        flight = await fly(daemon)
        record = read_pid_record(scope.home)
        assert record is not None and record.process == daemon.process
        assert await flight.stop() == EXIT_OK

    run(main())


# -- the real process ------------------------------------------------------------------------------


@pytest.mark.slow
def test_the_daemon_process_serves_refuses_a_second_and_drains_on_sigterm(tmp_path: Path) -> None:
    script = script_of(text_turn("Hello from the gateway"))
    with serving(script) as model:
        home = SpoolHome(tmp_path, model.port)
        daemon = home.start()
        try:
            status = daemon.running()
            assert status.platforms["spool"].state == "connected"  # a plugin platform
            record = read_pid_record(home.home)
            assert record is not None and record.pid == daemon.proc.pid
            home.send("hi there")
            daemon.until(lambda: home.outbound() == ["Hello from the gateway"], "the reply")

            second = home.start()
            assert second.wait() == EXIT_OK
            assert "already running" in second.log_path.read_text(encoding="utf-8")
            supervised = home.start(INDUS_GATEWAY_SUPERVISED="1")
            assert supervised.wait() == EXIT_RESTART
            assert daemon.proc.poll() is None  # the first one never noticed

            assert daemon.stop() == EXIT_OK
        finally:
            daemon.kill()
    final = home.status()
    assert final is not None and final.state == "stopped"
    assert final.exit_reason == "signal:SIGTERM" and final.exit_code == 0
    assert not pid_path(home.home).exists()
    assert home.stray_processes() == []
    assert list((home.spool / "inbound").iterdir()) == []
    assert list((tmp_path / "token-locks").glob("*.lock")) == []
    assert not list(home.home.glob("*.sock"))
