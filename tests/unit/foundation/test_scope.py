"""Scope context, propagation and child env (P01-06, R03.23, R01.16)."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from indus.foundation.errors import ScopeUnresolved
from indus.foundation.scope import (
    EnvView,
    Scope,
    bind_scope,
    child_env,
    current_scope,
    make_scope,
    scope_or_none,
    spawn_with_scope,
    thread_with_scope,
    use_scope,
)
from tests.fakes.clock import FakeClock, checkpoint


def _scope(tmp_path: Path, pid: str, clock: FakeClock) -> Scope:
    return make_scope(tmp_path / "profiles" / pid, env={"A": "1"}, clock=clock)


def test_unresolved_raises() -> None:
    with pytest.raises(ScopeUnresolved) as err:
        current_scope()
    assert err.value.code == "scope.unresolved"
    assert scope_or_none() is None


def test_make_scope_fields(tmp_path: Path, fake_clock: FakeClock) -> None:
    s = make_scope(tmp_path / "profiles" / "work", clock=fake_clock)
    assert (s.profile_id, s.root, s.routed) == ("work", tmp_path, False)
    d = make_scope(tmp_path, clock=fake_clock)
    assert (d.profile_id, d.root) == ("default", tmp_path)
    assert d.config is None and d.secrets is None and dict(d.terminal_policy) == {}


def test_use_scope_restores(tmp_path: Path, fake_clock: FakeClock) -> None:
    a = _scope(tmp_path, "a", fake_clock)
    b = _scope(tmp_path, "b", fake_clock)
    with use_scope(a):
        assert current_scope() is a
        with use_scope(b):
            assert current_scope() is b
        assert current_scope() is a
    assert scope_or_none() is None


def test_concurrent_tasks_see_their_own(tmp_path: Path, fake_clock: FakeClock) -> None:
    a = _scope(tmp_path, "a", fake_clock)
    b = _scope(tmp_path, "b", fake_clock)

    async def worker() -> list[str]:
        seen: list[str] = []
        for _ in range(3):
            seen.append(current_scope().profile_id)
            await checkpoint()
        return seen

    async def main() -> tuple[list[str], list[str]]:
        ta = spawn_with_scope(worker(), scope=a)
        tb = spawn_with_scope(worker(), scope=b)
        return await ta, await tb

    assert asyncio.run(main()) == (["a"] * 3, ["b"] * 3)


def test_spawn_inherits_current(tmp_path: Path, fake_clock: FakeClock) -> None:
    a = _scope(tmp_path, "a", fake_clock)

    async def main() -> str:
        with use_scope(a):
            task = spawn_with_scope(_profile_id())
        return await task

    assert asyncio.run(main()) == "a"


async def _profile_id() -> str:
    return current_scope().profile_id


def test_thread_with_scope_vs_bare_thread(tmp_path: Path, fake_clock: FakeClock) -> None:
    a = _scope(tmp_path, "a", fake_clock)
    seen: dict[str, str] = {}

    def record(name: str) -> None:
        current = scope_or_none()
        seen[name] = current.profile_id if current else "none"

    with use_scope(a):
        carried = thread_with_scope(record, "carried")
        bare = threading.Thread(target=record, args=("bare",))
    carried.start()
    bare.start()
    carried.join()
    bare.join()
    assert seen == {"carried": "a", "bare": "none"}


def test_bind_scope_in_executor(tmp_path: Path, fake_clock: FakeClock) -> None:
    a = _scope(tmp_path, "a", fake_clock)
    with use_scope(a):
        fn = bind_scope(lambda x: (current_scope().profile_id, x))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(fn, range(8)))
    assert results == [("a", i) for i in range(8)]


def test_child_env_pins_home(tmp_path: Path, fake_clock: FakeClock) -> None:
    s = make_scope(tmp_path / "profiles" / "work", env={}, clock=fake_clock)
    assert "INDUS_HOME" not in s.env
    assert child_env(s) == {"INDUS_HOME": str(tmp_path / "profiles" / "work")}


def test_env_view_overlay() -> None:
    view = EnvView({"A": "1", "B": "2"})
    newer = view.with_overlay({"B": None, "C": "3"})
    assert dict(view) == {"A": "1", "B": "2"}
    assert dict(newer) == {"A": "1", "C": "3"}
    assert newer.overlay == {"B": None, "C": "3"}
    assert newer.base == {"A": "1", "B": "2"}
    assert newer.with_overlay({"B": "again"})["B"] == "again"
    assert "secret-value" not in repr(EnvView({"K": "secret-value"}))
    with pytest.raises(TypeError):
        view["A"] = "x"  # type: ignore[index]


def test_env_view_snapshot_is_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INDUS_SCOPE_PROBE", "before")
    view = EnvView.from_process()
    monkeypatch.setenv("INDUS_SCOPE_PROBE", "after")
    assert view["INDUS_SCOPE_PROBE"] == "before"


def test_scope_is_frozen(tmp_path: Path, fake_clock: FakeClock) -> None:
    s = _scope(tmp_path, "a", fake_clock)
    with pytest.raises(AttributeError):
        s.profile_id = "b"  # type: ignore[misc]
    assert s.with_config(None) == s


def test_conftest_scope_fixture(scope: Scope, isolated_home: Path) -> None:
    assert scope.profile_id == "work"
    assert scope.root == isolated_home
    assert scope.home == isolated_home / "profiles" / "work"
    assert scope.home.is_dir()
    assert isinstance(scope.clock, FakeClock)
    assert child_env(scope)["INDUS_HOME"] == str(scope.home)
