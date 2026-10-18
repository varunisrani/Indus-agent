"""The SSH environment against the fake ssh CLI (P11-09, with P11-10/11 on open)."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from indus.backends.contract import EnvironmentConnectionError
from indus.backends.registry import BackendEntry, BackendStatus, CreateRequest, EnvironmentRegistry
from indus.backends.ssh.client import SshTarget
from indus.backends.ssh.environment import (
    HOME_RETRY_S,
    SSH_FLAGS,
    SshEnvironment,
    connection_problem,
    open_ssh,
    settings_from,
    setup_findings,
)
from indus.backends.ssh.runner import Runner, RunResult, run_client
from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope
from tests.fakes.backend.ssh_cli import FakeSsh, install, is_home_lookup
from tests.fakes.clock import FakeClock
from tests.fakes.terminal import forge_token, terminal_scope
from tests.unit.backends._aio import aio
from tests.unit.backends.conftest import drive

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


@pytest.fixture
def fake(tmp_path: Path) -> FakeSsh:
    return install(tmp_path / "bin", tmp_path / "remote-home")


@pytest.fixture
def runtime_dir() -> Iterator[str]:
    path = tempfile.mkdtemp(prefix="isr", dir="/tmp")
    yield path
    shutil.rmtree(path, ignore_errors=True)


def ssh_scope(
    tmp_path: Path,
    fake: FakeSsh,
    runtime_dir: str,
    terminal: Mapping[str, Any] | None = None,
    *,
    secrets: Mapping[str, str] | None = None,
) -> Scope:
    config = {"backend": "ssh", "ssh_host": "box.test", "ssh_user": "dev", **(terminal or {})}
    scope = terminal_scope(tmp_path, {"terminal": config}, secrets=secrets)
    view = scope.env.with_overlay(
        {"PATH": fake.path_env(scope.env.get("PATH", "")), "XDG_RUNTIME_DIR": runtime_dir}
    )
    return scope.with_env(view)


def remote_commands(fake: FakeSsh) -> list[str]:
    return [c["argv"][-1] for c in fake.calls() if c["tool"] == "ssh"]


@aio
async def test_runs_commands_and_keeps_cwd_and_exports(
    tmp_path: Path, fake: FakeSsh, runtime_dir: str
) -> None:
    scope = ssh_scope(tmp_path, fake, runtime_dir)
    env = await open_ssh(scope, clock=SystemClock())
    try:
        assert env.home_detected and env.remote_home == str(fake.remote_home)
        assert env.cwd == "~" or env.cwd == str(fake.remote_home)
        first = await env.execute("mkdir -p sub && cd sub && export GREETING=hi && pwd")
        assert first.returncode == 0
        assert first.output.strip() == str(fake.remote_home / "sub")
        again = await env.execute('printf "%s %s" "$(pwd)" "$GREETING"')
        assert again.output == f"{fake.remote_home / 'sub'} hi"
        piped = await env.execute("cat", stdin="line one\nline two\n")
        assert piped.output == "line one\nline two\n"
        own = await env.execute("exit 255")  # a command's own 255 is not a lost connection
        assert own.returncode == 255
        assert (fake.remote_home / ".indus" / "credentials").is_dir()
    finally:
        await env.cleanup()
    assert env.flags.isolated is False and SSH_FLAGS.host_local is False


@aio
async def test_passthrough_value_rides_the_client_env_only(
    tmp_path: Path, fake: FakeSsh, runtime_dir: str
) -> None:
    secret = forge_token()
    scope = ssh_scope(
        tmp_path,
        fake,
        runtime_dir,
        {"env_passthrough": ["DEPLOY_TOKEN"]},
        secrets={"DEPLOY_TOKEN": secret},
    )
    env = await open_ssh(scope, clock=SystemClock())
    try:
        result = await env.execute('printf %s "$DEPLOY_TOKEN"')
        assert result.output == secret
    finally:
        await env.cleanup()
    calls = fake.calls()
    assert calls, "the fake ssh recorded nothing"
    for call in calls:
        assert not any(secret in word for word in call["argv"])
    sent = [c for c in calls if c["sent_env"].get("DEPLOY_TOKEN") == secret]
    assert sent and all("-o" in c["argv"] and "SendEnv=DEPLOY_TOKEN" in c["argv"] for c in sent)
    # The command with SendEnv names rides its own master (a different control socket).
    plain = {
        w for c in calls for w in c["argv"] if w.startswith("ControlPath=") and not c["sent_env"]
    }
    withenv = {w for c in sent for w in c["argv"] if w.startswith("ControlPath=")}
    assert plain and withenv and not (plain & withenv)
    # The snapshot never froze the value.
    for snap in Path("/tmp").glob("indus-snap-*.sh"):
        assert secret not in snap.read_text(errors="replace")


def ssh_registry(runner: Runner) -> EnvironmentRegistry:
    async def create(request: CreateRequest) -> Any:
        return await open_ssh(
            request.scope, session_id=request.session_id, clock=SystemClock(), runner=runner
        )

    return EnvironmentRegistry([BackendEntry("ssh", create, SSH_FLAGS)])


@aio
async def test_probe_timeout_is_degraded(tmp_path: Path, fake: FakeSsh, runtime_dir: str) -> None:
    async def slow_probe(argv: Sequence[str], **kw: Any) -> RunResult:
        if "indus-ssh-ok" in argv[-1]:
            raise TimeoutError("probe")
        return await run_client(argv, **kw)

    registry = ssh_registry(slow_probe)
    scope = ssh_scope(tmp_path, fake, runtime_dir)
    answer = await registry.run("true", scope=scope)
    assert isinstance(answer, BackendStatus) and answer.status == "degraded"
    assert "timed out" in answer.reason and "box.test" in answer.reason
    assert answer.retry_hint
    await registry.shutdown(fence=False)


@aio
async def test_refused_connection_is_degraded(
    tmp_path: Path, fake: FakeSsh, runtime_dir: str
) -> None:
    fake.set_mode("refused", home=str(fake.remote_home))
    registry = EnvironmentRegistry()
    scope = ssh_scope(tmp_path, fake, runtime_dir)
    answer = await registry.run("true", scope=scope)
    assert isinstance(answer, BackendStatus) and answer.status == "degraded"
    assert "Connection refused" in answer.reason
    await registry.shutdown(fence=False)


@aio
async def test_host_key_change_is_degraded_never_accepted(
    tmp_path: Path, fake: FakeSsh, runtime_dir: str
) -> None:
    registry = EnvironmentRegistry()
    scope = ssh_scope(tmp_path, fake, runtime_dir)
    first = await registry.run("echo up", scope=scope)
    assert not isinstance(first, BackendStatus) and first.output.strip() == "up"
    fake.set_mode("hostkey", home=str(fake.remote_home))
    second = await registry.run("echo again", scope=scope)
    assert isinstance(second, BackendStatus) and second.status == "degraded"
    assert "never accepts a changed host key" in second.reason
    assert "ssh-keygen -R box.test" in second.retry_hint
    assert registry.live() == {}  # evicted
    fake.set_mode("ok", home=str(fake.remote_home))
    third = await registry.run("echo back", scope=scope)
    assert not isinstance(third, BackendStatus) and third.output.strip() == "back"
    await registry.shutdown(fence=False)


@aio
async def test_auth_failure_names_the_key_not_its_contents(
    tmp_path: Path, fake: FakeSsh, runtime_dir: str
) -> None:
    key = tmp_path / "id_test"
    key.write_text("PRIVATE KEY MATERIAL\n")
    fake.set_mode("auth", home=str(fake.remote_home))
    scope = ssh_scope(tmp_path, fake, runtime_dir, {"ssh_key": str(key)})
    with pytest.raises(EnvironmentConnectionError) as caught:
        await open_ssh(scope, clock=SystemClock())
    assert str(key) in caught.value.reason and "MATERIAL" not in caught.value.reason
    no_key = connection_problem("dev@h: Permission denied (publickey).", SshTarget("h", "dev"))
    assert no_key is not None and "ssh agent" in no_key[0]


def test_connection_problem_table() -> None:
    target = SshTarget("h", "u", 22)
    assert connection_problem(
        "ssh: Could not resolve hostname h: Name or service not known", target
    )
    assert connection_problem("Connection closed by 10.0.0.1 port 22", target)
    assert connection_problem("kex_exchange_identification: read: Connection reset", target)
    assert connection_problem("make: *** [all] Error 1", target) is None


@aio
async def test_home_detection_retried_no_sooner_than_30s(
    tmp_path: Path, fake: FakeSsh, runtime_dir: str
) -> None:
    fake.set_mode("home_fail", home=str(fake.remote_home), home_fail=2)
    scope = ssh_scope(tmp_path, fake, runtime_dir)
    clock = FakeClock(datetime(2026, 10, 18, tzinfo=UTC))
    env = SshEnvironment(
        scope,
        settings_from(scope),
        ssh=str(fake.bin_dir / "ssh"),
        scp=str(fake.bin_dir / "scp"),
        clock=clock,
    )

    def home_lookups() -> int:
        return sum(1 for c in remote_commands(fake) if is_home_lookup(c))

    try:
        await drive(clock, env.connect())
        assert not env.home_detected and env.remote_home == "/home/dev"
        assert home_lookups() == 1
        clock.advance(HOME_RETRY_S - 5)
        assert not env.home_retry_due()
        await drive(clock, env.execute("true"))
        assert home_lookups() == 1
        clock.advance(HOME_RETRY_S)
        assert env.home_retry_due()
        await drive(clock, env.execute("true"))
        assert home_lookups() == 2 and not env.home_detected  # failed again: spacing restarts
        assert not env.home_retry_due()
        clock.advance(HOME_RETRY_S + 1)
        await drive(clock, env.execute("true"))
        assert home_lookups() == 3
        assert env.home_detected and env.remote_home == str(fake.remote_home)
    finally:
        await drive(clock, env.cleanup())


@aio
async def test_cleanup_closes_every_master(tmp_path: Path, fake: FakeSsh, runtime_dir: str) -> None:
    scope = ssh_scope(
        tmp_path,
        fake,
        runtime_dir,
        {"env_passthrough": ["DEPLOY_TOKEN"]},
        secrets={"DEPLOY_TOKEN": forge_token()},
    )
    env = await open_ssh(scope, clock=SystemClock())
    await env.execute("true")
    sockets = sorted(env.sockets)
    assert len(sockets) == 2
    for socket in sockets:  # the fake client makes no real master; stand in for its socket
        Path(socket).touch()
    await env.cleanup()
    exits = [c["argv"] for c in fake.calls() if "-O" in c["argv"]]
    assert sorted(a[a.index("-o") + 1].removeprefix("ControlPath=") for a in exits) == sockets
    assert all(a[a.index("-O") + 1] == "exit" for a in exits)
    assert not any(Path(s).exists() for s in sockets)
    await env.cleanup()  # twice is fine


@aio
async def test_missing_host_is_disabled(tmp_path: Path, fake: FakeSsh, runtime_dir: str) -> None:
    scope = ssh_scope(tmp_path, fake, runtime_dir, {"ssh_host": ""})
    registry = EnvironmentRegistry()
    answer = await registry.run("true", scope=scope)
    assert isinstance(answer, BackendStatus) and answer.status == "disabled"
    assert "ssh_host" in answer.error
    await registry.shutdown(fence=False)


@aio
async def test_open_pushes_skills_and_credential_files(
    tmp_path: Path, fake: FakeSsh, runtime_dir: str
) -> None:
    secret_file = tmp_path / "user-home" / ".config" / "tool" / "token.json"
    secret_file.parent.mkdir(parents=True)
    secret_file.write_text('{"token": "x"}')
    secret_file.chmod(0o600)
    scope = ssh_scope(tmp_path, fake, runtime_dir, {"credential_files": [str(secret_file)]})
    scope = scope.with_env(scope.env.with_overlay({"HOME": str(tmp_path / "user-home")}))
    skill = scope.home / "skills" / "deploy" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# deploy\n")
    (scope.home / "skills" / "deploy" / "run.sh").write_text("echo run\n")
    env = await open_ssh(scope, clock=SystemClock())
    try:
        base = fake.remote_home / ".indus"
        assert (base / "skills" / "deploy" / "SKILL.md").read_text() == "# deploy\n"
        assert (base / "skills" / "deploy" / "run.sh").read_text() == "echo run\n"
        pushed = base / "credentials" / ".config" / "tool" / "token.json"
        assert pushed.read_text() == '{"token": "x"}'
        assert env.sync is not None and str(pushed) in env.sync.upload_only
        listed = await env.execute("ls ~/.indus/skills/deploy")
        assert listed.output.split() == ["SKILL.md", "run.sh"]
    finally:
        await env.cleanup()


def test_doctor_rows(tmp_path: Path, fake: FakeSsh, runtime_dir: str) -> None:
    ok = setup_findings(ssh_scope(tmp_path, fake, runtime_dir))
    assert ok == [("ok", "commands run on dev@box.test:22 over ssh", None)]
    no_host = setup_findings(ssh_scope(tmp_path, fake, runtime_dir, {"ssh_host": ""}))
    assert no_host[0][0] == "error" and "ssh_host" in no_host[0][1]
    bad_key = setup_findings(ssh_scope(tmp_path, fake, runtime_dir, {"ssh_key": "/no/such/key"}))
    assert bad_key == [
        ("error", "terminal.ssh_key /no/such/key does not exist", "fix terminal.ssh_key")
    ]
    bare = ssh_scope(tmp_path, fake, runtime_dir)
    bare = bare.with_env(bare.env.with_overlay({"PATH": str(tmp_path / "empty")}))
    assert setup_findings(bare)[0][1] == "ssh and scp not found on PATH"


@aio
async def test_local_sudo_password_never_reaches_the_remote_host(
    tmp_path: Path, fake: FakeSsh, runtime_dir: str
) -> None:
    local_pw = "test-local-" + "pw22"
    remote_pw = "test-remote-" + "pw33"
    scope = ssh_scope(tmp_path, fake, runtime_dir, secrets={"sudo_password": local_pw})
    env = await open_ssh(scope, clock=SystemClock())
    try:
        assert env.sudo is None  # no ssh_sudo_password: the rewrite is off on this backend
        await env.execute("sudo -n true 2>/dev/null; echo done")
    finally:
        await env.cleanup()
    for call in fake.calls():
        assert local_pw not in " ".join(call["argv"])
        assert local_pw not in str(call["sent_env"])
    scope = ssh_scope(
        tmp_path,
        fake,
        runtime_dir,
        secrets={"sudo_password": local_pw, "ssh_sudo_password": remote_pw},
    )
    env = await open_ssh(scope, clock=SystemClock())
    try:
        assert env.sudo is not None
        assert env.sudo.target == "ssh:dev@box.test:22" and not env.sudo.can_prompt
        assert env.sudo.secret_names == ("ssh_sudo_password", "SSH_SUDO_PASSWORD")
    finally:
        await env.cleanup()


@aio
async def test_timeout_also_stops_the_remote_command(
    tmp_path: Path, fake: FakeSsh, runtime_dir: str
) -> None:
    scope = ssh_scope(tmp_path, fake, runtime_dir)
    env = await open_ssh(scope, clock=SystemClock())
    try:
        result = await env.execute("sleep 30", timeout=1)
    finally:
        await env.cleanup()
    assert result.timed_out
    kills = [c for c in remote_commands(fake) if "kill -TERM" in c or "kill -KILL" in c]
    assert kills and "kill -TERM" in kills[0]
    assert "indus-exec-" in kills[0] and ".pid" in kills[0]
