"""The shared spawn-per-command flow: snapshot fallbacks and execute plumbing (P05-01)."""

from __future__ import annotations

import asyncio

import pytest

from indus.backends.contract import BackendError, BackendFlags, ExecResult
from indus.backends.wait import ProcessHandle
from tests.fakes.backend.environment import FakeEnvironment, FakeProcess, Rule
from tests.fakes.clock import FakeClock
from tests.unit.backends.conftest import drive


class Booting(FakeEnvironment):
    """Runs the real ``SpawnedEnvironment.start``: the bootstrap and the probe are scripted."""

    def __init__(self, *, bootstrap_rc: int, probe_rc: int, clock: FakeClock) -> None:
        super().__init__(cwd="/configured", clock=clock)
        self.snapshot_ready = False
        self.bootstrap_rc = bootstrap_rc
        self.probe_rc = probe_rc
        self.logins: list[tuple[str, bool]] = []

    async def start(self) -> None:
        await super(FakeEnvironment, self).start()

    async def _spawn(self, script: str, *, login: bool, stdin: str | None) -> ProcessHandle:
        if script.startswith("umask 077"):
            self.logins.append(("bootstrap", login))
            marker = self.paths.marker
            return FakeProcess([f"\n{marker}/real/cwd{marker}\n"], self.bootstrap_rc)
        if script == "true":
            self.logins.append(("probe", login))
            return FakeProcess([], self.probe_rc)
        self.logins.append(("command", login))
        return await super()._spawn(script, login=login, stdin=stdin)


def test_snapshot_ready_reads_the_starting_cwd(fake_clock: FakeClock) -> None:
    env = Booting(bootstrap_rc=0, probe_rc=0, clock=fake_clock)
    asyncio.run(drive(fake_clock, env.start()))
    assert env.snapshot_ready and not env.prefer_nonlogin and env.cwd == "/real/cwd"
    assert env.logins == [("bootstrap", True)]


def test_broken_login_shell_falls_back_to_plain_bash(fake_clock: FakeClock) -> None:
    env = Booting(bootstrap_rc=1, probe_rc=0, clock=fake_clock)

    async def go() -> ExecResult:
        await env.start()
        return await env.execute("echo hi")

    result = asyncio.run(drive(fake_clock, go()))
    assert not env.snapshot_ready and env.prefer_nonlogin
    assert env.logins == [("bootstrap", True), ("probe", False), ("command", False)]
    assert result.output == "hi\n"


def test_both_broken_runs_every_command_under_a_login_shell(fake_clock: FakeClock) -> None:
    env = Booting(bootstrap_rc=1, probe_rc=2, clock=fake_clock)

    async def go() -> None:
        await env.start()
        await env.execute("echo hi")

    asyncio.run(drive(fake_clock, go()))
    assert not env.snapshot_ready and not env.prefer_nonlogin
    assert env.logins[-1] == ("command", True)


def test_compound_background_is_rewritten_before_spawning(fake_clock: FakeClock) -> None:
    env = FakeEnvironment(clock=fake_clock)
    env.script("make && { ./serve & }", Rule(output="started\n"))
    result = asyncio.run(drive(fake_clock, env.execute("make && ./serve &")))
    assert result.output == "started\n" and env.calls[0].command == "make && { ./serve & }"


def test_heredoc_backends_get_stdin_inside_the_script(fake_clock: FakeClock) -> None:
    command_seen: list[str] = []

    class Capture(FakeEnvironment):
        async def _spawn(self, script: str, *, login: bool, stdin: str | None) -> ProcessHandle:
            command_seen.append(script)
            assert stdin is None
            return FakeProcess(["ok"], 0)

    env = Capture(clock=fake_clock, flags=BackendFlags(stdin_mode="heredoc"))
    asyncio.run(drive(fake_clock, env.execute("cat", stdin="payload")))
    assert "INDUS_STDIN_" in command_seen[0] and "payload." in command_seen[0]


def test_per_call_workdir_is_reported_but_never_adopted(fake_clock: FakeClock) -> None:
    env = FakeEnvironment(cwd="/home/w", clock=fake_clock)
    result = asyncio.run(drive(fake_clock, env.execute("printf %s x", cwd="/elsewhere")))
    assert result.cwd_observed == "/elsewhere" and env.cwd == "/home/w"


def test_recreated_sandbox_is_reported_once(fake_clock: FakeClock) -> None:
    env = FakeEnvironment(clock=fake_clock)
    env.script("first", Rule(output="a", recreate=True))

    async def go() -> tuple[ExecResult, ExecResult]:
        return await env.execute("first"), await env.execute("printf %s b")

    first, second = asyncio.run(drive(fake_clock, go()))
    assert first.environment_recreated and not second.environment_recreated


def test_non_positive_timeout_uses_the_default(fake_clock: FakeClock) -> None:
    env = FakeEnvironment(clock=fake_clock, timeout=4)
    result = asyncio.run(drive(fake_clock, env.execute("sleep 9", timeout=0), step=0.1))
    assert result.timed_out and result.output.endswith("after 4s]")


def test_cleaned_environment_refuses_commands(fake_clock: FakeClock) -> None:
    env = FakeEnvironment(clock=fake_clock)

    async def go() -> None:
        await env.cleanup()
        await env.execute("echo late")

    with pytest.raises(BackendError, match="cleaned up"):
        asyncio.run(go())
