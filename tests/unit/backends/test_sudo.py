"""Sudo rewrite, password sources and failure annotations (P05-10)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

import pytest

from indus.backends.contract import BackendFlags, ExecResult
from indus.backends.local import LocalEnvironment, open_local
from indus.backends.sudo import (
    HOST_LOCAL,
    MASK,
    SudoPasswordCache,
    SudoSupport,
    backend_secret_names,
    backend_sudo,
    rewrite_sudo,
    use_sudo_caller,
)
from indus.foundation.scope import Scope
from tests.fakes.backend.environment import FakeEnvironment, Rule
from tests.fakes.broker import FakeBroker, Reply
from tests.fakes.clock import FakeClock
from tests.unit.backends.conftest import drive

PASSWORD = "test-sudo-" + "pw00"
NEEDS = Rule(output="sudo: a password is required\n", returncode=1)
CMD = "sudo apt-get update"
REWRITTEN = "sudo -S -p '' apt-get update"


@pytest.mark.parametrize(
    ("command", "expected", "count"),
    [
        ("sudo apt-get update", "sudo -S -p '' apt-get update", 1),
        ("/usr/bin/sudo ls /root", "/usr/bin/sudo -S -p '' ls /root", 1),
        ("FOO=1 sudo id", "FOO=1 sudo -S -p '' id", 1),
        ("env LANG=C sudo id", "env LANG=C sudo -S -p '' id", 1),
        ("env -u HOME -i sudo id", "env -u HOME -i sudo -S -p '' id", 1),
        ("env --chdir /tmp sudo id", "env --chdir /tmp sudo -S -p '' id", 1),
        ("sudo a && sudo b; sudo c", "sudo -S -p '' a && sudo -S -p '' b; sudo -S -p '' c", 3),
        ("if true; then sudo z; fi", "if true; then sudo -S -p '' z; fi", 1),
        ("(sudo q) || time sudo r", "(sudo -S -p '' q) || time sudo -S -p '' r", 2),
        ("sudo make 2>&1 | tee log", "sudo -S -p '' make 2>&1 | tee log", 1),
        ("cat <<EOF\nsudo x\nEOF\nsudo y", "cat <<EOF\nsudo x\nEOF\nsudo -S -p '' y", 1),
        # never rewritten
        ("bash -c 'sudo id'", "bash -c 'sudo id'", 0),
        ('env -S "sudo id"', 'env -S "sudo id"', 0),
        ("$SUDO id", "$SUDO id", 0),
        ("$(which sudo) id", "$(which sudo) id", 0),
        ("`which sudo` id", "`which sudo` id", 0),
        ("~/bin/sudo id", "~/bin/sudo id", 0),
        ("/opt/*/sudo id", "/opt/*/sudo id", 0),
        ('"sudo" id', '"sudo" id', 0),
        ("echo sudo; echo $(sudo id)", "echo sudo; echo $(sudo id)", 0),
        ("ls # sudo id", "ls # sudo id", 0),
        ("nohup sudo id", "nohup sudo id", 0),
        ("make | sudo tee /etc/x", "make | sudo tee /etc/x", 0),
        ("sudo tee /etc/x < in.txt", "sudo tee /etc/x < in.txt", 0),
        ("sudo cmd <<< text", "sudo cmd <<< text", 0),
        ("sudoku --solve", "sudoku --solve", 0),
    ],
)
def test_rewrite_table(command: str, expected: str, count: int) -> None:
    assert rewrite_sudo(command) == (expected, count)


@pytest.fixture
def cache() -> SudoPasswordCache:
    return SudoPasswordCache()


@pytest.fixture
def env(scope: Scope, fake_clock: FakeClock, cache: SudoPasswordCache) -> FakeEnvironment:
    environment = FakeEnvironment(clock=fake_clock, flags=BackendFlags(host_local=True))
    environment.sudo = SudoSupport(scope, session_id="s1", cache=cache)
    environment.script("sudo -n true", NEEDS)
    return environment


def run[T](clock: FakeClock, work: Awaitable[T]) -> T:
    return asyncio.run(drive(clock, work))


def key(scope: Scope) -> tuple[str, str, str]:
    return (str(scope.home), "s1", HOST_LOCAL)


def test_cached_password_rewrites_and_feeds_stdin(
    env: FakeEnvironment, cache: SudoPasswordCache, scope: Scope, fake_clock: FakeClock
) -> None:
    cache.put(key(scope), PASSWORD)
    both = f"{CMD} && sudo apt-get upgrade -y"
    env.script(
        f"{REWRITTEN} && sudo -S -p '' apt-get upgrade -y",
        Rule(output="done\n", echo_stdin=True),
    )
    result = run(fake_clock, env.execute(both, stdin="extra input\n"))
    call = env.calls[-1]
    assert call.stdin == f"{PASSWORD}\n{PASSWORD}\nextra input\n"
    assert PASSWORD not in call.command
    assert PASSWORD not in result.output and MASK in result.output
    assert not result.sudo_auth_failed and not result.sudo_cache_cleared


def test_passwordless_sudo_is_left_alone(
    env: FakeEnvironment, cache: SudoPasswordCache, scope: Scope, fake_clock: FakeClock
) -> None:
    cache.put(key(scope), PASSWORD)
    env.script("sudo -n true", Rule(returncode=0))
    env.script(CMD, Rule(output="ok\n"))
    result = run(fake_clock, env.execute(CMD))
    assert env.calls[-1].command == CMD and env.calls[-1].stdin is None
    assert result.output.startswith("ok")


@pytest.mark.parametrize(
    "probe",
    [
        Rule(output="bash: sudo: command not found\n", returncode=127),
        Rule(hang=True),
        Rule(connection_error="link lost"),
    ],
)
def test_probe_failure_means_no_rewrite(
    probe: Rule,
    env: FakeEnvironment,
    cache: SudoPasswordCache,
    scope: Scope,
    fake_clock: FakeClock,
) -> None:
    cache.put(key(scope), PASSWORD)
    env.script("sudo -n true", probe)
    env.script(CMD, Rule(output="bash: sudo: command not found\n", returncode=127))
    result = run(fake_clock, env.execute(CMD))
    assert env.calls[-1].command == CMD and env.calls[-1].stdin is None
    assert result.returncode == 127 and not result.sudo_auth_failed


def test_secret_store_source(env: FakeEnvironment, scope: Scope, fake_clock: FakeClock) -> None:
    assert scope.secrets is not None
    scope.secrets.set("sudo_password", PASSWORD)  # type: ignore[attr-defined]
    env.script(REWRITTEN, Rule(output="ok\n"))
    run(fake_clock, env.execute(CMD))
    assert env.calls[-1].command == REWRITTEN
    assert env.calls[-1].stdin == PASSWORD + "\n"


def test_prompt_via_broker_caches_the_answer(
    env: FakeEnvironment, cache: SudoPasswordCache, scope: Scope, fake_clock: FakeClock
) -> None:
    broker = FakeBroker(fake_clock, default=Reply(password=PASSWORD))
    env.script(REWRITTEN, Rule(output="ok\n"))

    async def go() -> None:
        with use_sudo_caller(broker=broker, turn_id="t1"):
            await env.execute(CMD)
            await env.execute(CMD)

    run(fake_clock, go())
    assert [r.kind for r in broker.requests] == ["sudo"]  # asked once, then cached
    request = broker.requests[0]
    assert request.turn_id == "t1" and request.deadline_s == 45
    assert PASSWORD not in request.model_dump_json()
    assert cache.get(key(scope)) == PASSWORD
    assert [c.stdin for c in env.calls if c.command == REWRITTEN] == [PASSWORD + "\n"] * 2


def test_prompt_declined_runs_unchanged(
    env: FakeEnvironment, cache: SudoPasswordCache, scope: Scope, fake_clock: FakeClock
) -> None:
    broker = FakeBroker(fake_clock, default=Reply(password=None))
    env.script(CMD, NEEDS)

    async def go() -> ExecResult:
        with use_sudo_caller(broker=broker):
            return await env.execute(CMD)

    result = run(fake_clock, go())
    assert env.calls[-1].command == CMD
    assert "none was given" in result.output and "secret store" not in result.output
    assert cache.get(key(scope)) is None


def test_headless_failure_gets_tip(env: FakeEnvironment, fake_clock: FakeClock) -> None:
    env.script(CMD, NEEDS)
    result = run(fake_clock, env.execute(CMD))
    assert env.calls[-1].command == CMD
    assert "Tip: this session cannot ask for a sudo password" in result.output
    assert "never paste it into chat" in result.output


def test_non_interactive_caller_is_headless(env: FakeEnvironment, fake_clock: FakeClock) -> None:
    broker = FakeBroker(fake_clock, default=Reply(password=PASSWORD))
    env.script(CMD, NEEDS)

    async def go() -> ExecResult:
        with use_sudo_caller(broker=broker, interactive=False):
            return await env.execute(CMD)

    result = run(fake_clock, go())
    assert broker.requests == [] and "Tip:" in result.output


WRONG = Rule(output="Sorry, try again.\nsudo: 1 incorrect password attempt\n", returncode=1)


def test_wrong_cached_password_clears_cache(
    env: FakeEnvironment, cache: SudoPasswordCache, scope: Scope, fake_clock: FakeClock
) -> None:
    cache.put(key(scope), PASSWORD)
    env.script(REWRITTEN, WRONG)
    broker = FakeBroker(fake_clock)

    async def go() -> ExecResult:
        with use_sudo_caller(broker=broker):
            return await env.execute(CMD)

    result = run(fake_clock, go())
    assert result.sudo_auth_failed and result.sudo_cache_cleared
    assert cache.get(key(scope)) is None
    assert "asked for it again" in result.output


def test_wrong_configured_password_is_flagged_not_cleared(
    env: FakeEnvironment, scope: Scope, fake_clock: FakeClock
) -> None:
    assert scope.secrets is not None
    scope.secrets.set("sudo_password", PASSWORD)  # type: ignore[attr-defined]
    env.script(REWRITTEN, WRONG)
    result = run(fake_clock, env.execute(CMD))
    assert result.sudo_auth_failed and not result.sudo_cache_cleared
    assert scope.secrets.get("sudo_password") == PASSWORD
    assert "update it" in result.output and "asked for it again" not in result.output


def test_account_without_sudo(
    env: FakeEnvironment, cache: SudoPasswordCache, scope: Scope, fake_clock: FakeClock
) -> None:
    cache.put(key(scope), PASSWORD)
    no_sudo = "w is not in the sudoers file.  This incident will be reported.\n"
    env.script("sudo -n true", Rule(output=no_sudo, returncode=1))
    env.script(CMD, Rule(output=no_sudo, returncode=1))
    result = run(fake_clock, env.execute(CMD))
    assert env.calls[-1].command == CMD and env.calls[-1].stdin is None
    assert "not allowed to use sudo" in result.output
    assert "again" not in result.output and not result.sudo_auth_failed


def test_heredoc_backend_is_not_rewritten(
    scope: Scope, cache: SudoPasswordCache, fake_clock: FakeClock
) -> None:
    environment = FakeEnvironment(
        clock=fake_clock, flags=BackendFlags(stdin_mode="heredoc", host_local=True)
    )
    environment.sudo = SudoSupport(scope, session_id="s1", cache=cache)
    cache.put(key(scope), PASSWORD)
    environment.script(CMD, NEEDS)
    result = run(fake_clock, environment.execute(CMD))
    assert [c.command for c in environment.calls] == [CMD]  # no probe either
    assert "Tip:" in result.output


def test_password_never_logged_or_in_scripts(
    env: FakeEnvironment,
    cache: SudoPasswordCache,
    scope: Scope,
    fake_clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cache.put(key(scope), PASSWORD)
    env.script(REWRITTEN, Rule(output=f"echoed {PASSWORD}\n"))
    with caplog.at_level(logging.DEBUG):
        result = run(fake_clock, env.execute(CMD))
    assert PASSWORD not in caplog.text
    assert all(PASSWORD not in call.command for call in env.calls)
    assert PASSWORD not in result.output and f"echoed {MASK}" in result.output


def test_sessions_and_profiles_have_separate_caches(
    scope: Scope, tmp_path: Path, cache: SudoPasswordCache
) -> None:
    one = SudoSupport(scope, session_id="s1", cache=cache)
    two = SudoSupport(scope, session_id="s2", cache=cache)
    assert one.key != two.key
    cache.put(one.key, PASSWORD)
    assert cache.get(two.key) is None


def test_open_local_installs_the_hook(scope: Scope) -> None:
    async def go() -> LocalEnvironment:
        environment = await open_local(scope, session_id="s7")
        await environment.cleanup()
        return environment

    environment = asyncio.run(go())
    assert isinstance(environment.sudo, SudoSupport)
    assert environment.sudo.key == (str(scope.home), "s7", HOST_LOCAL)
    assert environment.sudo.target == HOST_LOCAL


# -- the local password never leaves this machine (P11 integration, security) -----------------


def _put_secret(scope: Scope, name: str, value: str) -> None:
    store: Any = scope.secrets
    store.set(name, value)


def _remote_env(fake_clock: FakeClock) -> FakeEnvironment:
    environment = FakeEnvironment(clock=fake_clock, flags=BackendFlags(host_local=False))
    environment.script("sudo -n true", NEEDS)
    return environment


def test_host_hook_never_rewrites_on_a_remote_environment(
    scope: Scope, cache: SudoPasswordCache, fake_clock: FakeClock
) -> None:
    _put_secret(scope, "sudo_password", PASSWORD)
    cache.put(key(scope), PASSWORD)
    remote = _remote_env(fake_clock)
    remote.sudo = SudoSupport(scope, session_id="s1", cache=cache)  # wrongly attached
    broker = FakeBroker(fake_clock, default=Reply(password=PASSWORD))
    remote.script(CMD, Rule(output="sudo: a password is required\n", returncode=1))

    async def go() -> None:
        with use_sudo_caller(broker=broker, turn_id="t1"):
            await remote.execute(CMD)

    run(fake_clock, go())
    assert all(call.command != "sudo -n true" for call in remote.calls)  # not even probed
    assert all(PASSWORD not in (call.stdin or "") for call in remote.calls)
    assert all(call.command == CMD for call in remote.calls)
    assert broker.requests == []


def test_cache_is_keyed_by_backend_environment(scope: Scope, cache: SudoPasswordCache) -> None:
    local = SudoSupport(scope, session_id="s1", cache=cache)
    remote = SudoSupport(scope, session_id="s1", cache=cache, target="ssh:dev@box:22")
    assert local.key != remote.key and local.key[2] == HOST_LOCAL
    cache.put(local.key, PASSWORD)
    assert cache.get(remote.key) is None


def test_no_backend_hook_without_a_backend_password(scope: Scope) -> None:
    _put_secret(scope, "sudo_password", PASSWORD)
    for backend in ("ssh", "docker"):
        assert backend_sudo(scope, backend=backend, target=f"{backend}:x") is None
    assert backend_sudo(scope, backend="ssh", target=HOST_LOCAL) is None


def test_backend_password_is_the_only_source_and_never_prompts(
    scope: Scope, cache: SudoPasswordCache, fake_clock: FakeClock
) -> None:
    remote_pw = "test-remote-" + "pw11"
    _put_secret(scope, "sudo_password", PASSWORD)
    _put_secret(scope, backend_secret_names("ssh")[0], remote_pw)
    cache.put(key(scope), PASSWORD)
    hook = backend_sudo(scope, backend="ssh", target="ssh:dev@box:22", session_id="s1")
    assert hook is not None and hook.target == "ssh:dev@box:22" and not hook.can_prompt
    hook.cache = cache
    remote = _remote_env(fake_clock)
    remote.sudo = hook
    remote.script(REWRITTEN, Rule(output="ok\n"))
    broker = FakeBroker(fake_clock, default=Reply(password=PASSWORD))

    async def go() -> None:
        with use_sudo_caller(broker=broker, turn_id="t1"):
            await remote.execute(CMD)

    run(fake_clock, go())
    assert remote.calls[-1].command == REWRITTEN
    assert remote.calls[-1].stdin == remote_pw + "\n"
    assert all(PASSWORD not in (call.stdin or "") for call in remote.calls)
    assert broker.requests == []


def test_backend_hook_without_answer_does_not_prompt(scope: Scope, fake_clock: FakeClock) -> None:
    hook = SudoSupport(scope, session_id="s1", target="docker:k", secret_names=("x_pw",))
    assert not hook.can_prompt
    remote = _remote_env(fake_clock)
    remote.sudo = hook
    remote.script(CMD, Rule(output="sudo: a password is required\n", returncode=1))
    broker = FakeBroker(fake_clock, default=Reply(password=PASSWORD))

    async def go() -> None:
        with use_sudo_caller(broker=broker, turn_id="t1"):
            await remote.execute(CMD)

    run(fake_clock, go())
    assert broker.requests == []
    assert all(PASSWORD not in (call.stdin or "") for call in remote.calls)
