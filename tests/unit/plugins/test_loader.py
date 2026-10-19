"""P13-15: loading plugins — deadline, rollback, dependency order, version gate, overrides."""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.blocking import BlockingPool
from indus.foundation.scope import Scope
from indus.plugins.context import PluginHost
from indus.plugins.discovery import discover
from indus.plugins.hooks import HookBus
from indus.plugins.loader import MODULE_ROOT, PluginLoader, load_timeout
from indus.tools.registry import ToolRegistry
from tests.fakes.agent import eventually
from tests.fakes.clock import FakeClock
from tests.unit.plugins._helpers import install_fixture, with_config, write_plugin

APPEND_CODE = "def register(ctx):\n    ctx.setting('log').append({name!r})\n"


@pytest.fixture
def user(scope: Scope) -> Path:
    folder = scope.home / "plugins"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@pytest.fixture
def bundled(tmp_path: Path) -> Path:
    folder = tmp_path / "bundled"
    folder.mkdir()
    return folder


@pytest.fixture
def pool() -> Iterator[BlockingPool]:
    made = BlockingPool("plugin-load-test", 2)
    yield made
    made.shutdown()


@pytest.fixture(autouse=True)
def forget_plugin_modules() -> Iterator[None]:
    yield
    for name in [n for n in sys.modules if n == MODULE_ROOT or n.startswith(MODULE_ROOT + ".")]:
        sys.modules.pop(name, None)


def builtin_registry() -> ToolRegistry:
    registry = ToolRegistry()

    def read_file(args: Any) -> str:
        return "built-in"

    registry.register("read_file", "file", {"description": "read", "parameters": {}}, read_file)
    return registry


def make(
    scope: Scope,
    fake_clock: FakeClock,
    pool: BlockingPool,
    registry: ToolRegistry | None = None,
    **options: Any,
) -> tuple[PluginHost, PluginLoader]:
    host = PluginHost(
        scope,
        registry if registry is not None else ToolRegistry(),
        HookBus(clock=fake_clock),
        clock=fake_clock,
    )
    return host, PluginLoader(host, pool=pool, **options)


def config(enabled: list[str], **entries: dict[str, Any]) -> dict[str, Any]:
    return {"plugins": {"enabled": enabled, "entries": entries}}


def test_load_timeout_setting(scope: Scope) -> None:
    def value(raw: object) -> float:
        return load_timeout(with_config(scope, {"plugins": {"load_timeout_seconds": raw}}))

    assert load_timeout(scope) == 10.0
    assert value(0) == 0.0
    assert value(25) == 25.0
    assert value(9999) == 600.0
    assert value(-1) == 10.0
    assert value("soon") == 10.0


def test_hanging_register_is_skipped_and_the_next_plugin_loads(
    scope: Scope, fake_clock: FakeClock, pool: BlockingPool, user: Path, bundled: Path
) -> None:
    install_fixture(user, "hanging")
    write_plugin(
        user,
        "zz-after",
        {"name": "zz-after"},
        "def register(ctx):\n    ctx.register_command('after', lambda raw_args: 'ok')\n",
    )
    hold, started = threading.Event(), threading.Event()
    cfg = config(["hanging", "zz-after"], hanging={"settings": {"hold": hold, "started": started}})
    scoped = with_config(scope, cfg)
    host, loader = make(scoped, fake_clock, pool)

    async def go() -> Any:
        task = asyncio.ensure_future(loader.load(discover(scoped, bundled_dir=bundled)))
        await eventually(lambda: started.is_set() and fake_clock.pending >= 1)
        fake_clock.advance(10)
        report = await task
        hold.set()
        await eventually(lambda: pool.busy == 0)
        return report

    try:
        report = asyncio.run(go())
    finally:
        hold.set()
    assert report.order() == ["hanging", "zz-after"]
    hung = report.get("hanging")
    assert hung is not None and not hung.loaded
    assert hung.reason == "load timed out after 10 s"
    after = report.get("zz-after")
    assert after is not None and after.loaded
    # The abandoned worker registered after its deadline: refused, nothing kept.
    assert sorted(host.commands) == ["after"]
    assert "hanging" not in host.provided


def test_late_registration_after_timeout_is_refused(
    scope: Scope,
    fake_clock: FakeClock,
    pool: BlockingPool,
    user: Path,
    bundled: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    install_fixture(user, "hanging")
    hold, started = threading.Event(), threading.Event()
    scoped = with_config(
        scope, config(["hanging"], hanging={"settings": {"hold": hold, "started": started}})
    )
    host, loader = make(scoped, fake_clock, pool)

    async def go() -> None:
        task = asyncio.ensure_future(loader.load(discover(scoped, bundled_dir=bundled)))
        await eventually(lambda: started.is_set() and fake_clock.pending >= 1)
        fake_clock.advance(10)
        await task
        hold.set()
        await eventually(lambda: pool.busy == 0)

    try:
        asyncio.run(go())
    finally:
        hold.set()
    assert host.commands == {}
    assert "after its load was abandoned" in caplog.text


def test_raising_register_is_skipped_and_undone(
    scope: Scope, fake_clock: FakeClock, pool: BlockingPool, user: Path, bundled: Path
) -> None:
    install_fixture(user, "raising")
    install_fixture(user, "good")
    scoped = with_config(scope, config(["raising", "good"]))
    host, loader = make(scoped, fake_clock, pool)
    report = asyncio.run(loader.load(discover(scoped, bundled_dir=bundled)))
    raised = report.get("raising")
    assert raised is not None and not raised.loaded
    assert raised.reason.startswith("RuntimeError: sample failure")
    good = report.get("good")
    assert good is not None and good.loaded
    # The hook the failing plugin added before raising is gone; good's own hook stays.
    assert [e.owner for e in host.bus._snapshot("post_tool_call")] == ["good"]  # pyright: ignore[reportPrivateUsage]
    assert "raising" not in host.provided
    assert not any(n.startswith(f"{MODULE_ROOT}.raising") for n in sys.modules)


def test_system_exit_in_register_is_contained(
    scope: Scope, fake_clock: FakeClock, pool: BlockingPool, user: Path, bundled: Path
) -> None:
    write_plugin(
        user, "quitter", {"name": "quitter"}, "def register(ctx):\n    raise SystemExit(3)\n"
    )
    scoped = with_config(scope, config(["quitter"]))
    _, loader = make(scoped, fake_clock, pool)
    report = asyncio.run(loader.load(discover(scoped, bundled_dir=bundled)))
    outcome = report.get("quitter")
    assert outcome is not None and not outcome.loaded and "SystemExit(3)" in outcome.reason


def test_missing_register_is_a_reason(
    scope: Scope, fake_clock: FakeClock, pool: BlockingPool, user: Path, bundled: Path
) -> None:
    write_plugin(user, "inert", {"name": "inert"}, "VALUE = 1\n")
    scoped = with_config(scope, config(["inert"]))
    _, loader = make(scoped, fake_clock, pool)
    report = asyncio.run(loader.load(discover(scoped, bundled_dir=bundled)))
    assert report.skipped() == {
        "inert": "PluginLoadFailed: the plugin has no register(ctx) function"
    }


def test_full_load_pool_refuses(
    scope: Scope, fake_clock: FakeClock, user: Path, bundled: Path
) -> None:
    single = BlockingPool("plugin-load-single", 1)
    install_fixture(user, "hanging")
    second = install_fixture(user, "hanging", as_name="hanging2")
    (second / "plugin.yaml").write_text("name: hanging2\n", encoding="utf-8")
    hold, started = threading.Event(), threading.Event()
    settings = {"settings": {"hold": hold, "started": started}}
    scoped = with_config(
        scope, config(["hanging", "hanging2"], hanging=settings, hanging2=settings)
    )
    _, loader = make(scoped, fake_clock, single)

    async def go() -> Any:
        task = asyncio.ensure_future(loader.load(discover(scoped, bundled_dir=bundled)))
        await eventually(lambda: started.is_set() and fake_clock.pending >= 1)
        fake_clock.advance(10)
        return await task

    try:
        report = asyncio.run(go())
    finally:
        hold.set()
        single.shutdown()
    assert report.skipped()["hanging"] == "load timed out after 10 s"
    assert "still stuck" in report.skipped()["hanging2"]


def test_dependency_order(
    scope: Scope, fake_clock: FakeClock, pool: BlockingPool, user: Path, bundled: Path
) -> None:
    order: list[str] = []
    write_plugin(
        user,
        "alpha",
        {"name": "alpha", "requires_plugins": ["beta"]},
        APPEND_CODE.format(name="alpha"),
    )
    write_plugin(
        user,
        "beta",
        {"name": "beta", "requires_plugins": [{"name": "gamma", "version": ">=1.0"}]},
        APPEND_CODE.format(name="beta"),
    )
    write_plugin(
        user, "gamma", {"name": "gamma", "version": "1.4"}, APPEND_CODE.format(name="gamma")
    )
    write_plugin(user, "delta", {"name": "delta"}, APPEND_CODE.format(name="delta"))
    entries = {k: {"settings": {"log": order}} for k in ("alpha", "beta", "gamma", "delta")}
    scoped = with_config(scope, config(["alpha", "beta", "gamma", "delta"], **entries))
    _, loader = make(scoped, fake_clock, pool)
    report = asyncio.run(loader.load(discover(scoped, bundled_dir=bundled)))
    assert report.loaded() == ["delta", "gamma", "beta", "alpha"]
    assert order == ["delta", "gamma", "beta", "alpha"]


def test_missing_or_failing_requirement_skips_dependents(
    scope: Scope, fake_clock: FakeClock, pool: BlockingPool, user: Path, bundled: Path
) -> None:
    install_fixture(user, "raising")
    install_fixture(user, "good")
    write_plugin(user, "orphan", {"name": "orphan", "requires_plugins": ["ghost"]})
    write_plugin(user, "follower", {"name": "follower", "requires_plugins": ["raising"]})
    write_plugin(user, "chained", {"name": "chained", "requires_plugins": ["follower"]})
    write_plugin(
        user, "picky", {"name": "picky", "requires_plugins": [{"name": "good", "version": ">=2"}]}
    )
    write_plugin(user, "ping", {"name": "ping", "requires_plugins": ["pong"]})
    write_plugin(user, "pong", {"name": "pong", "requires_plugins": ["ping"]})
    names = ["raising", "good", "orphan", "follower", "chained", "picky", "ping", "pong"]
    scoped = with_config(scope, config(names))
    _, loader = make(scoped, fake_clock, pool)
    report = asyncio.run(loader.load(discover(scoped, bundled_dir=bundled)))
    skipped = report.skipped()
    assert report.loaded() == ["good"]
    assert skipped["orphan"] == "needs plugin ghost, which is not installed or not enabled"
    assert skipped["follower"] == "its requirement raising did not load"
    assert skipped["chained"] == "its requirement follower did not load"
    assert skipped["picky"] == "needs good >=2, found 1.2.0"
    assert skipped["ping"].startswith("dependency cycle")
    assert skipped["pong"].startswith("dependency cycle")


def test_requires_indus_above_running_skips_without_import(
    scope: Scope, fake_clock: FakeClock, pool: BlockingPool, user: Path, bundled: Path
) -> None:
    code = (
        "import os, pathlib\n"
        "pathlib.Path(os.environ['INDUS_HOME'], 'imported-future').touch()\n"
        "def register(ctx):\n    pass\n"
    )
    write_plugin(user, "future", {"name": "future", "requires_indus": ">=99.0"}, code)
    write_plugin(
        user,
        "present",
        {"name": "present", "requires_indus": ">=0.0.1"},
        code.replace("future", "present"),
    )
    scoped = with_config(scope, config(["future", "present"]))
    _, loader = make(scoped, fake_clock, pool, running_version="0.5.0")
    report = asyncio.run(loader.load(discover(scoped, bundled_dir=bundled)))
    assert report.skipped() == {"future": "needs Indus >=99.0, this is 0.5.0"}
    assert not Path(os.environ["INDUS_HOME"], "imported-future").exists()
    assert Path(os.environ["INDUS_HOME"], "imported-present").exists()


def test_tool_shadowing_needs_operator_opt_in(
    scope: Scope, fake_clock: FakeClock, pool: BlockingPool, user: Path, bundled: Path
) -> None:
    install_fixture(user, "shadowing")
    registry = builtin_registry()
    refused_scope = with_config(scope, config(["shadowing"]))
    _, loader = make(refused_scope, fake_clock, pool, registry)
    report = asyncio.run(loader.load(discover(refused_scope, bundled_dir=bundled)))
    reason = report.skipped()["shadowing"]
    assert "plugins.entries.shadowing.allow_tool_override" in reason
    entry = registry.get("read_file", refused_scope)
    assert entry is not None and entry.handler({}) == "built-in"

    allowed = with_config(scope, config(["shadowing"], shadowing={"allow_tool_override": True}))
    _, loader = make(allowed, fake_clock, pool, registry)
    report = asyncio.run(loader.load(discover(allowed, bundled_dir=bundled)))
    assert report.loaded() == ["shadowing"]
    mine = registry.get("read_file", allowed)
    assert mine is not None and mine.handler({}) == "replaced"
    # Another profile still sees the built-in: the replacement lives in this profile's overlay.
    assert registry._global["read_file"].handler({}) == "built-in"  # pyright: ignore[reportPrivateUsage]


def test_shadowing_without_override_is_refused_but_plugin_loads(
    scope: Scope, fake_clock: FakeClock, pool: BlockingPool, user: Path, bundled: Path
) -> None:
    code = (
        "def register(ctx):\n"
        "    ctx.register_tool('read_file', 'mine', {'description': 'x'}, lambda args: 'mine')\n"
        "    ctx.register_tool('own_tool', 'mine', {'description': 'y'}, lambda args: 'own')\n"
    )
    write_plugin(user, "sneaky", {"name": "sneaky"}, code)
    registry = builtin_registry()
    scoped = with_config(scope, config(["sneaky"]))
    host, loader = make(scoped, fake_clock, pool, registry)
    report = asyncio.run(loader.load(discover(scoped, bundled_dir=bundled)))
    assert report.loaded() == ["sneaky"]
    entry = registry.get("read_file", scoped)
    assert entry is not None and entry.handler({}) == "built-in"
    assert host.provided["sneaky"].tools == ["own_tool"]


def test_activation_summary_live_vs_next_session(
    scope: Scope, fake_clock: FakeClock, pool: BlockingPool, user: Path, bundled: Path
) -> None:
    install_fixture(user, "good")
    scoped = with_config(scope, config(["good"]))
    host, loader = make(scoped, fake_clock, pool)
    report = asyncio.run(loader.load(discover(scoped, bundled_dir=bundled)))
    outcome = report.get("good")
    assert outcome is not None and outcome.loaded
    assert outcome.summary == {
        "live_now": {
            "commands": ["hello"],
            "hooks": ["post_tool_call"],
            "providers": ["tts:sample-voice"],
            "events": ["indus.session_started"],
            "skills": ["good:greet"],
        },
        "next_session": {"tools": ["sample_echo"], "prompt_sections": ["sample.notes"]},
    }
    assert any(n.startswith(f"{MODULE_ROOT}.good__") for n in sys.modules)
    assert host.registry.get("sample_echo", scoped) is not None
    assert host.registry.get("sample_echo") is None  # not global: the profile overlay


def test_capability_probe_degrades(
    scope: Scope, fake_clock: FakeClock, pool: BlockingPool, user: Path, bundled: Path
) -> None:
    install_fixture(user, "capabilities")
    plain = with_config(scope, config(["capabilities"]))
    host, loader = make(plain, fake_clock, pool)
    asyncio.run(loader.load(discover(plain, bundled_dir=bundled)))
    assert host.commands == {}
    granted = with_config(
        scope,
        config(
            ["capabilities"], capabilities={"granted_capabilities": [{"name": "platform.actions"}]}
        ),
    )
    host, loader = make(granted, fake_clock, pool)
    asyncio.run(loader.load(discover(granted, bundled_dir=bundled)))
    assert list(host.commands) == ["act"]


def test_no_deadline_when_timeout_is_zero(
    scope: Scope, fake_clock: FakeClock, pool: BlockingPool, user: Path, bundled: Path
) -> None:
    install_fixture(user, "good")
    scoped = with_config(scope, {"plugins": {"enabled": ["good"], "load_timeout_seconds": 0}})
    _, loader = make(scoped, fake_clock, pool)
    assert loader.timeout_s == 0
    report = asyncio.run(loader.load(discover(scoped, bundled_dir=bundled)))
    assert report.loaded() == ["good"]
    assert fake_clock.pending == 0


@dataclass
class LoadableEntryPoint:
    name: str
    value: str
    seen: list[str]

    def load(self) -> Any:
        def register(ctx: Any) -> None:
            self.seen.append(ctx.plugin_id)
            ctx.register_command("pkg", lambda raw_args: "from a package")

        return register


def test_entry_point_plugin_loads(
    scope: Scope, fake_clock: FakeClock, pool: BlockingPool, bundled: Path
) -> None:
    seen: list[str] = []
    eps = [LoadableEntryPoint("pkgplugin", "pkg:register", seen)]
    scoped = with_config(scope, config(["pkgplugin"]))
    host, loader = make(scoped, fake_clock, pool, entry_points=eps)
    report = asyncio.run(loader.load(discover(scoped, bundled_dir=bundled, entry_points=eps)))
    assert report.loaded() == ["pkgplugin"]
    assert seen == ["pkgplugin"]
    assert list(host.commands) == ["pkg"]
