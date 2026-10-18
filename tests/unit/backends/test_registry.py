"""Environment registry and lifecycle with fake backends and a fake clock (P05-04)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from indus.backends.contract import (
    BackendFlags,
    Environment,
    EnvironmentConnectionError,
    ExecResult,
)
from indus.backends.registry import (
    BackendEntry,
    BackendStatus,
    CreateRequest,
    EnvironmentRegistry,
    EnvKey,
)
from indus.foundation.errors import ScopeUnresolved
from indus.foundation.scope import Scope, make_scope
from tests.fakes.backend.environment import FakeEnvironment, Rule
from tests.fakes.clock import FakeClock


class Config:
    def __init__(self, values: dict[str, object]) -> None:
        self.values = values

    def get(self, path: str, /) -> object:
        return self.values.get(path)


class FakeBackend:
    """A registry row whose environments are ``FakeEnvironment``s, counted."""

    def __init__(self) -> None:
        self.created: list[FakeEnvironment] = []
        self.requests: list[CreateRequest] = []
        self.gate: asyncio.Event | None = None
        self.fail_with: BaseException | None = None

    async def __call__(self, request: CreateRequest) -> Environment:
        self.requests.append(request)
        if self.gate is not None:
            await self.gate.wait()
        if self.fail_with is not None:
            raise self.fail_with
        env = FakeEnvironment(cwd="/w", capture_home=request.scope.home)
        env.script("boom", Rule(connection_error="ssh: connection refused"))
        self.created.append(env)
        return env

    def entry(self, name: str = "fake") -> BackendEntry:
        return BackendEntry(name, self, BackendFlags(isolated=True))


def _scope(tmp_path: Path, name: str, clock: FakeClock, **config: object) -> Scope:
    home = tmp_path / "root" / "profiles" / name
    home.mkdir(parents=True, exist_ok=True)
    values: dict[str, object] = {"terminal.backend": "fake", **config}
    return make_scope(home, env={"PATH": "/usr/bin"}, clock=clock).with_config(Config(values))


def _registry(backend: FakeBackend, clock: FakeClock, **kwargs: object) -> EnvironmentRegistry:
    return EnvironmentRegistry([backend.entry()], clock=clock, **kwargs)  # type: ignore[arg-type]


def test_two_profiles_never_share(tmp_path: Path, fake_clock: FakeClock) -> None:
    backend = FakeBackend()
    one = _scope(tmp_path, "one", fake_clock)
    two = _scope(tmp_path, "two", fake_clock)

    async def go() -> list[EnvKey]:
        registry = _registry(backend, fake_clock)
        try:
            a, env_a = await registry.acquire(one, session_id="s1")
            b, env_b = await registry.acquire(two, session_id="s1")
            again, env_again = await registry.acquire(one, session_id="s1")
            assert env_a is not env_b and env_a is env_again
            return [a, b, again]
        finally:
            await registry.shutdown(fence=False)

    a, b, again = asyncio.run(go())
    assert a != b and a == again
    assert a.home == str(one.home) and b.home == str(two.home)
    assert len(backend.created) == 2


def test_key_kinds(tmp_path: Path, fake_clock: FakeClock) -> None:
    registry = _registry(FakeBackend(), fake_clock)
    scope = _scope(tmp_path, "p", fake_clock)
    session = registry.key_for(scope, task_id="t1", session_id="s1")
    task = registry.key_for(scope, task_id="t1", session_id="s1", overrides={"image": "x"})
    host = registry.key_for(scope, task_id="t1", session_id="s1", host_local=True)
    bare = registry.key_for(scope)
    assert (session.kind, session.ident) == ("session", "s1")
    assert (task.kind, task.ident) == ("task", "t1")
    assert (host.kind, host.backend) == ("host", "local")
    assert (bare.kind, bare.ident) == ("session", "default")
    assert len({session, task, host, bare}) == 4
    assert str(session).startswith(str(scope.home))


def test_backend_comes_from_scope_config_never_env_vars(
    tmp_path: Path, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TERMINAL_ENV", "fake")
    monkeypatch.setenv("INDUS_TERMINAL_BACKEND", "fake")
    home = tmp_path / "plain"
    home.mkdir()
    plain = make_scope(home, env={"TERMINAL_ENV": "fake"}, clock=fake_clock)
    assert EnvironmentRegistry.backend_name(plain) == "local"
    chosen = plain.with_config(Config({"terminal.backend": " Fake "}))
    assert EnvironmentRegistry.backend_name(chosen) == "fake"
    assert EnvironmentRegistry.backend_name(chosen, {"backend": "docker"}) == "docker"


def test_one_creation_under_a_race(tmp_path: Path, fake_clock: FakeClock) -> None:
    backend = FakeBackend()
    scope = _scope(tmp_path, "p", fake_clock)

    async def go() -> tuple[Environment, Environment]:
        backend.gate = asyncio.Event()
        registry = _registry(backend, fake_clock)
        try:
            first = asyncio.ensure_future(registry.acquire(scope, session_id="s"))
            second = asyncio.ensure_future(registry.acquire(scope, session_id="s"))
            await asyncio.wait({first, second}, timeout=0.01)
            backend.gate.set()
            (_, a), (_, b) = await asyncio.gather(first, second)
            return a, b
        finally:
            await registry.shutdown(fence=False)

    a, b = asyncio.run(go())
    assert a is b and len(backend.requests) == 1


def test_idle_env_reaped_after_its_lifetime(tmp_path: Path, fake_clock: FakeClock) -> None:
    backend = FakeBackend()
    scope = _scope(tmp_path, "p", fake_clock)

    async def go() -> None:
        registry = _registry(backend, fake_clock)
        try:
            await registry.acquire(scope, session_id="s")
            fake_clock.advance(200)
            await registry.run("printf %s used", scope=scope, session_id="s")  # stamps activity
            fake_clock.advance(300)
            assert await registry.reap_idle() == []
            fake_clock.advance(1)
            reaped = await registry.reap_idle()
            assert [k.ident for k in reaped] == ["s"] and registry.live() == {}
        finally:
            await registry.shutdown(fence=False)

    asyncio.run(go())
    assert backend.created[0].cleaned == 1


def test_env_with_a_live_background_process_is_kept(tmp_path: Path, fake_clock: FakeClock) -> None:
    backend = FakeBackend()
    scope = _scope(tmp_path, "p", fake_clock)
    alive = {"yes": True}

    async def go() -> None:
        registry = _registry(backend, fake_clock, background_alive=lambda key: alive["yes"])
        try:
            await registry.acquire(scope, session_id="s")
            fake_clock.advance(301)
            assert await registry.reap_idle() == []  # refreshed instead
            alive["yes"] = False
            fake_clock.advance(300)
            assert await registry.reap_idle() == []  # refreshed at 301, idle 300 now
            fake_clock.advance(1)
            assert len(await registry.reap_idle()) == 1
        finally:
            await registry.shutdown(fence=False)

    asyncio.run(go())


def test_reaper_task_wakes_every_minute(tmp_path: Path, fake_clock: FakeClock) -> None:
    backend = FakeBackend()
    scope = _scope(tmp_path, "p", fake_clock, **{"terminal.lifetime_s": 90})

    async def go() -> list[int]:
        registry = _registry(backend, fake_clock)
        live: list[int] = []
        try:
            await registry.acquire(scope, session_id="s")
            for _ in range(3):
                await fake_clock.advance_and_settle(60)
                await asyncio.wait({asyncio.ensure_future(fake_clock.run_until_idle())})
                live.append(len(registry.live()))
            return live
        finally:
            await registry.shutdown(fence=False)

    # idle 60 (kept), idle 120 > 90 (reaped at the second wake-up)
    assert asyncio.run(go()) == [1, 0, 0]


def test_degraded_result_evicts_and_the_next_call_recreates(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    backend = FakeBackend()
    scope = _scope(tmp_path, "p", fake_clock)

    async def go() -> tuple[ExecResult | BackendStatus, ExecResult | BackendStatus]:
        registry = _registry(backend, fake_clock)
        try:
            degraded = await registry.run("boom", scope=scope, session_id="s")
            assert registry.live() == {}
            later = await registry.run("printf %s back", scope=scope, session_id="s")
            return degraded, later
        finally:
            await registry.shutdown(fence=False)

    degraded, later = asyncio.run(go())
    assert isinstance(degraded, BackendStatus)
    assert degraded.to_dict() == {
        "output": "",
        "exit_code": -1,
        "status": "degraded",
        "reason": "ssh: connection refused",
        "retry_hint": degraded.retry_hint,
        "error": "Terminal backend degraded: ssh: connection refused",
    }
    assert degraded.retry_hint
    assert isinstance(later, ExecResult) and later.output == "back"
    assert len(backend.created) == 2 and backend.created[0].cleaned == 1


def test_fail_mode_returns_the_error(tmp_path: Path, fake_clock: FakeClock) -> None:
    backend = FakeBackend()
    scope = _scope(tmp_path, "p", fake_clock, **{"terminal.degraded_mode": "fail"})

    async def go() -> ExecResult | BackendStatus:
        registry = _registry(backend, fake_clock)
        try:
            result = await registry.run("boom", scope=scope, session_id="s")
            assert registry.live() == {}
            return result
        finally:
            await registry.shutdown(fence=False)

    result = asyncio.run(go())
    assert isinstance(result, BackendStatus) and result.status == "error"
    assert result.to_dict() == {
        "output": "",
        "exit_code": -1,
        "status": "error",
        "error": "Terminal backend failed: ssh: connection refused",
    }


def test_connection_failure_while_creating_caches_nothing(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    backend = FakeBackend()
    backend.fail_with = EnvironmentConnectionError("docker daemon is not running")
    scope = _scope(tmp_path, "p", fake_clock)

    async def go() -> ExecResult | BackendStatus:
        registry = _registry(backend, fake_clock)
        try:
            result = await registry.run("printf %s x", scope=scope, session_id="s")
            assert registry.live() == {}
            return result
        finally:
            await registry.shutdown(fence=False)

    result = asyncio.run(go())
    assert isinstance(result, BackendStatus) and result.status == "degraded"
    assert result.reason == "docker daemon is not running"


@pytest.mark.parametrize("problem", ["missing package", "unknown backend", "probe"])
def test_disabled_backends(tmp_path: Path, fake_clock: FakeClock, problem: str) -> None:
    backend = FakeBackend()
    entries = [backend.entry()]
    scope = _scope(tmp_path, "p", fake_clock)
    if problem == "missing package":
        backend.fail_with = ImportError("No module named 'vendor_sdk'")
    elif problem == "unknown backend":
        scope = scope.with_config(Config({"terminal.backend": "nowhere"}))
    else:
        entries = [BackendEntry("fake", backend, BackendFlags(), probe=lambda s: "no daemon")]

    async def go() -> ExecResult | BackendStatus:
        registry = EnvironmentRegistry(entries, clock=fake_clock)
        try:
            return await registry.run("printf %s x", scope=scope, session_id="s")
        finally:
            await registry.shutdown(fence=False)

    result = asyncio.run(go())
    assert isinstance(result, BackendStatus) and result.status == "disabled"
    assert result.error.startswith("Terminal backend disabled: ")
    expected = {
        "missing package": "vendor_sdk",
        "unknown backend": "'nowhere' (available: fake)",
        "probe": "no daemon",
    }[problem]
    assert expected in result.error
    assert result.to_dict()["status"] == "disabled"


def test_unresolved_scope_refuses(fake_clock: FakeClock) -> None:
    async def go() -> None:
        registry = EnvironmentRegistry([FakeBackend().entry()], clock=fake_clock)
        try:
            with pytest.raises(ScopeUnresolved):
                await registry.run("printf %s x")
        finally:
            await registry.shutdown(fence=False)

    asyncio.run(go())


def test_duplicate_backend_refused(fake_clock: FakeClock) -> None:
    backend = FakeBackend()
    registry = EnvironmentRegistry([backend.entry()], clock=fake_clock)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(backend.entry("FAKE"))
    assert registry.names() == ["fake"]
    assert {"local", "docker", "ssh"} <= set(EnvironmentRegistry(clock=fake_clock).names())


def test_release_and_shutdown_clean_with_a_bound(tmp_path: Path, fake_clock: FakeClock) -> None:
    backend = FakeBackend()
    scope = _scope(tmp_path, "p", fake_clock)

    class Stuck(FakeEnvironment):
        async def cleanup(self) -> None:
            await asyncio.Event().wait()

    async def stuck_factory(request: CreateRequest) -> Environment:
        return Stuck(cwd="/w")

    async def go() -> tuple[bool, list[FakeEnvironment]]:
        registry = EnvironmentRegistry(
            [backend.entry(), BackendEntry("stuck", stuck_factory, BackendFlags())],
            clock=fake_clock,
        )
        await registry.acquire(scope, session_id="a")
        await registry.acquire(scope, session_id="b")
        await registry.acquire(scope, task_id="t", overrides={"backend": "stuck"})
        released = await registry.release(scope, session_id="a")
        closing = asyncio.ensure_future(registry.shutdown(fence=False))
        for _ in range(100):
            await asyncio.wait({closing}, timeout=0.002)
            if closing.done():
                break
            fake_clock.advance(1)
        assert closing.done() and registry.live() == {}
        with pytest.raises(Exception, match="shut down"):
            await registry.acquire(scope, session_id="c")
        return released, backend.created

    released, created = asyncio.run(go())
    assert released and [env.cleaned for env in created] == [1, 1]
    assert fake_clock.monotonic() >= 15  # the stuck cleanup was given its 15 s, then abandoned


def test_failed_config_read_refuses_instead_of_guessing(
    tmp_path: Path, fake_clock: FakeClock
) -> None:
    class Failed(Config):
        failed = True

    backend = FakeBackend()
    scope = _scope(tmp_path, "p", fake_clock).with_config(Failed({}))

    async def go() -> ExecResult | BackendStatus:
        registry = _registry(backend, fake_clock)
        try:
            return await registry.run("printf %s x", scope=scope, session_id="s")
        finally:
            await registry.shutdown(fence=False)

    result = asyncio.run(go())
    assert isinstance(result, BackendStatus) and result.status == "disabled"
    assert "could not be read" in result.error and backend.requests == []
