"""The browser session manager over a fake driver and a fake clock (P10-12)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from indus.foundation.scope import scope_or_none
from indus.tools.builtin.browser import session as session_mod
from indus.tools.builtin.browser.driver import BrowserMissing, PageDriver
from indus.tools.builtin.browser.session import (
    CDP_URL_ENV,
    Choice,
    CommandTimeout,
    browser_env,
    inactivity_for,
    needs_sandbox_bypass,
    orphan_grace_s,
    timeouts_for,
)
from tests.fakes.clock import checkpoint
from tests.fakes.secret_store import MemorySecretStore
from tests.unit.tools.builtin.browser._rig import make_clock, make_rig, make_scope_for

PROVIDER_KEY = "OPENAI_API_" + "KEY"
FAKE_VALUE = "sk-" + "x" * 24


def test_timeouts_and_floors(tmp_path: Path) -> None:
    clock = make_clock()
    plain = timeouts_for(make_scope_for(tmp_path, clock))
    assert (plain.command, plain.open, plain.first_open) == (30, 60, 120)
    assert plain.navigation(True) == 120 and plain.navigation(False) == 60
    tiny = timeouts_for(make_scope_for(tmp_path, clock, {"browser": {"command_timeout": 1}}))
    assert tiny.command == 5
    big = timeouts_for(make_scope_for(tmp_path, clock, {"browser": {"command_timeout": 200}}))
    assert (big.command, big.open, big.first_open) == (200, 200, 200)
    assert inactivity_for(make_scope_for(tmp_path, clock)) == 120
    assert (
        inactivity_for(make_scope_for(tmp_path, clock, {"browser": {"inactivity_timeout": 5}}))
        == 30
    )
    assert orphan_grace_s(120) == 3600
    assert orphan_grace_s(600) == 12_000


def test_two_task_ids_get_two_browsers_and_one_id_reuses(tmp_path: Path) -> None:
    rig = make_rig()
    scope = make_scope_for(tmp_path, rig.clock)

    async def go() -> None:
        first = await rig.manager.session("a", scope)
        again = await rig.manager.session("a", scope)
        other = await rig.manager.session("b", scope)
        assert first is again
        assert other is not first
        assert first.ref_attr != other.ref_attr

    asyncio.run(go())
    assert len(rig.driver.launches) == 2
    dirs = {spec.profile_dir for spec in rig.driver.launches}
    assert len(dirs) == 2
    for folder in dirs:
        owner = json.loads((folder / "owner.json").read_text())
        assert owner["pid"] == os.getpid()


def test_concurrent_first_use_launches_once(tmp_path: Path) -> None:
    rig = make_rig()
    scope = make_scope_for(tmp_path, rig.clock)

    async def go() -> None:
        made = await asyncio.gather(*(rig.manager.session("t", scope) for _ in range(5)))
        assert len({id(m) for m in made}) == 1

    asyncio.run(go())
    assert len(rig.driver.launches) == 1


def test_launch_env_lacks_provider_keys_and_credentials(tmp_path: Path) -> None:
    rig = make_rig()
    env = {
        PROVIDER_KEY: FAKE_VALUE,
        "MY_SERVICE_TOKEN": "t0ken-value",
        "DISPLAY": ":5",
        "LANG": "C.UTF-8",
        "BROWSER_PROXY_HINT": "plain",
    }
    secrets = MemorySecretStore({"BROWSER_PROXY_HINT": "from-the-store"})
    scope = make_scope_for(
        tmp_path,
        rig.clock,
        {"browser": {"env_passthrough": ["BROWSER_PROXY_HINT", PROVIDER_KEY]}},
        env=env,
    ).with_secrets(secrets)

    async def go() -> None:
        await rig.manager.session("t", scope)

    asyncio.run(go())
    launched = dict(rig.driver.launches[0].env)
    assert PROVIDER_KEY not in launched
    assert "MY_SERVICE_TOKEN" not in launched
    assert FAKE_VALUE not in launched.values()
    assert launched["DISPLAY"] == ":5"
    assert launched["LANG"] == "C.UTF-8"
    assert launched["BROWSER_PROXY_HINT"] == "from-the-store"
    assert launched["PATH"] == "/usr/bin:/bin"


def test_browser_env_is_built_from_the_scope_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHOULD_NOT_LEAK", "process-value")
    scope = make_scope_for(tmp_path, make_clock())
    assert "SHOULD_NOT_LEAK" not in browser_env(scope)


def test_sandbox_decision_table(tmp_path: Path) -> None:
    scope = make_scope_for(tmp_path, make_clock())
    if not sys.platform.startswith("linux"):
        pytest.skip("the user-namespace rule is Linux only")
    assert needs_sandbox_bypass(scope, euid=0, container=False, userns_restricted=False)
    assert needs_sandbox_bypass(scope, euid=1000, container=True, userns_restricted=False)
    assert needs_sandbox_bypass(scope, euid=1000, container=False, userns_restricted=True)
    assert not needs_sandbox_bypass(scope, euid=1000, container=False, userns_restricted=False)


def test_sandbox_bypass_turns_sandbox_off_and_adds_the_shm_flag(tmp_path: Path) -> None:
    rig = make_rig()
    rig.manager.sandbox_check = lambda scope: True
    scope = make_scope_for(tmp_path, rig.clock, {"browser": {"executable_path": "/opt/x/chrome"}})
    asyncio.run(rig.manager.session("t", scope))
    spec = rig.driver.launches[0]
    assert spec.sandbox is False
    assert spec.args == ("--disable-dev-shm-usage",)
    assert spec.executable == "/opt/x/chrome"
    assert spec.headless is True
    assert spec.timeout_s == 120


def test_sandbox_stays_on_when_it_can_work(tmp_path: Path) -> None:
    rig = make_rig()
    asyncio.run(rig.manager.session("t", make_scope_for(tmp_path, rig.clock)))
    spec = rig.driver.launches[0]
    assert spec.sandbox is True and spec.args == () and spec.executable is None


def test_missing_browser_names_the_install_command(tmp_path: Path) -> None:
    rig = make_rig()
    rig.driver.missing_browser = True
    scope = make_scope_for(tmp_path, rig.clock)
    with pytest.raises(BrowserMissing, match="indus tools install browser"):
        asyncio.run(rig.manager.session("t", scope))
    assert not list((scope.home / "cache" / "browser" / "profiles").iterdir())


def test_idle_session_is_reaped_after_the_inactivity_limit(tmp_path: Path) -> None:
    rig = make_rig()
    scope = make_scope_for(tmp_path, rig.clock)

    async def go() -> None:
        made = await rig.manager.session("t", scope)
        rig.clock.advance(119)
        assert await rig.manager.janitor_tick() == []
        assert rig.manager.get("t") is made
        rig.clock.advance(2)  # 121 s idle
        assert await rig.manager.janitor_tick() == ["t"]
        assert rig.manager.get("t") is None
        assert made.profile_dir is not None and not made.profile_dir.exists()

    asyncio.run(go())
    assert len(rig.driver.closed) == 1


def test_use_keeps_a_session_alive(tmp_path: Path) -> None:
    rig = make_rig()
    scope = make_scope_for(tmp_path, rig.clock)

    async def go() -> None:
        made = await rig.manager.session("t", scope)
        rig.clock.advance(100)
        await rig.manager.run(made, lambda p: p.scroll(10))
        rig.clock.advance(100)
        assert await rig.manager.janitor_tick() == []

    asyncio.run(go())


def test_teardown_re_enters_the_owning_scope(tmp_path: Path) -> None:
    rig = make_rig()
    scope = make_scope_for(tmp_path, rig.clock, name="owner")
    seen: list[object] = []

    async def go() -> None:
        made = await rig.manager.session("t", scope)
        original = made.handle.close

        async def close() -> None:
            seen.append(scope_or_none())
            await original()

        made.handle.close = close  # type: ignore[method-assign]
        rig.clock.advance(500)
        await rig.manager.janitor_tick()

    asyncio.run(go())
    assert seen == [scope]


def test_three_failed_closes_force_reap(tmp_path: Path) -> None:
    rig = make_rig()
    scope = make_scope_for(tmp_path, rig.clock)
    rig.driver.close_failures = 10

    async def go() -> Path:
        made = await rig.manager.session("t", scope)
        assert made.profile_dir is not None
        rig.processes.by_marker[f"--user-data-dir={made.profile_dir}"] = [4242, 4243]
        rig.clock.advance(200)
        await rig.manager.janitor_tick()
        assert rig.manager.failures["t"] == 1 and rig.manager.get("t") is made
        await rig.manager.janitor_tick()
        assert rig.manager.failures["t"] == 2 and rig.manager.get("t") is made
        await rig.manager.janitor_tick()
        assert rig.manager.get("t") is None
        assert "t" not in rig.manager.failures
        return made.profile_dir

    folder = asyncio.run(go())
    assert rig.processes.killed == [4242, 4243]
    assert not folder.exists()


def test_a_successful_close_resets_the_failure_count(tmp_path: Path) -> None:
    rig = make_rig()
    scope = make_scope_for(tmp_path, rig.clock)
    rig.driver.close_failures = 2

    async def go() -> None:
        await rig.manager.session("t", scope)
        rig.clock.advance(200)
        await rig.manager.janitor_tick()
        await rig.manager.janitor_tick()
        assert rig.manager.failures["t"] == 2
        await rig.manager.janitor_tick()  # third try works
        assert rig.manager.get("t") is None and "t" not in rig.manager.failures

    asyncio.run(go())
    assert rig.processes.killed == []


def test_timed_out_session_is_recycled(tmp_path: Path) -> None:
    rig = make_rig()
    rig.driver.hang_on.add("outline")
    scope = make_scope_for(tmp_path, rig.clock)

    async def go() -> None:
        made = await rig.manager.session("t", scope)
        with pytest.raises(CommandTimeout, match="restarted"):
            await rig.manager.run(made, lambda p: p.outline(False, "a"), timeout_s=0.05)
        assert made.suspect is not None
        fresh = await rig.manager.session("t", scope)
        assert fresh is not made
        assert made.handle in rig.driver.closed  # type: ignore[comparison-overlap]

    asyncio.run(go())
    assert len(rig.driver.launches) == 2


def test_timeout_without_recycle_keeps_the_session(tmp_path: Path) -> None:
    rig = make_rig()
    rig.driver.hang_on.add("cdp")
    scope = make_scope_for(tmp_path, rig.clock)

    async def go() -> None:
        made = await rig.manager.session("t", scope)

        async def op(page: PageDriver) -> dict[str, object]:
            return await page.cdp("Page.reload", {})

        with pytest.raises(CommandTimeout):
            await rig.manager.run(made, op, timeout_s=0.05, recycle_on_timeout=False)
        assert made.suspect is None
        assert await rig.manager.session("t", scope) is made

    asyncio.run(go())


def test_choice_order_connect_then_env_then_config(tmp_path: Path) -> None:
    rig = make_rig()
    clock = rig.clock
    plain = make_scope_for(tmp_path, clock, name="plain")
    assert rig.manager.choice_for(plain) == Choice("local")
    configured = make_scope_for(
        tmp_path, clock, {"browser": {"cdp_url": "http://127.0.0.1:9222"}}, name="c"
    )
    assert rig.manager.choice_for(configured) == Choice("cdp", "http://127.0.0.1:9222", "config")
    env = make_scope_for(
        tmp_path,
        clock,
        {"browser": {"cdp_url": "http://127.0.0.1:9222"}},
        name="e",
        env={CDP_URL_ENV: "http://127.0.0.1:9333"},
    )
    assert rig.manager.choice_for(env).cdp_url == "http://127.0.0.1:9333"
    rig.manager.choices.set(env, Choice("cdp", "http://127.0.0.1:9444", "connect"))
    assert rig.manager.choice_for(env).cdp_url == "http://127.0.0.1:9444"
    assert rig.manager.choice_for(configured).cdp_url == "http://127.0.0.1:9222"  # per profile


def test_a_changed_choice_replaces_the_session(tmp_path: Path) -> None:
    rig = make_rig()
    scope = make_scope_for(tmp_path, rig.clock)

    async def go() -> None:
        local = await rig.manager.session("t", scope)
        rig.manager.choices.set(scope, Choice("cdp", "http://127.0.0.1:9222", "connect"))
        attached = await rig.manager.session("t", scope)
        assert attached is not local and attached.choice.mode == "cdp"
        assert attached.profile_dir is None

    asyncio.run(go())
    assert rig.driver.attaches == ["http://127.0.0.1:9222"]


def _profile(
    home: Path, name: str, owner: dict[str, object] | None, age_s: float, now: float
) -> Path:
    folder = home / "cache" / "browser" / "profiles" / name
    folder.mkdir(parents=True)
    if owner is not None:
        (folder / "owner.json").write_text(json.dumps(owner))
        os.utime(folder / "owner.json", (now - age_s, now - age_s))
    os.utime(folder, (now - age_s, now - age_s))
    return folder


def test_orphan_reaper(tmp_path: Path) -> None:
    now = 2_000_000_000.0
    rig = make_rig(wall_time=lambda: now)
    scope = make_scope_for(tmp_path, rig.clock)
    rig.processes.live.add(777)

    async def go() -> None:
        made = await rig.manager.session("t", scope)
        assert made.profile_dir is not None

    asyncio.run(go())
    home = scope.home
    dead = _profile(home, "s-dead", {"pid": 999}, 10, now)
    live_recent = _profile(home, "s-live", {"pid": 777}, 3000, now)
    live_old = _profile(home, "s-leaked", {"pid": 777}, 3700, now)
    no_owner_new = _profile(home, "s-new", None, 5, now)
    rig.processes.by_marker[f"--user-data-dir={dead}"] = [31]
    reaped = rig.manager.reap_orphans()
    assert set(reaped) == {dead, live_old}
    assert not dead.exists() and not live_old.exists()
    assert live_recent.exists() and no_owner_new.exists()
    assert rig.processes.killed == [31]
    tracked = rig.manager.get("t")
    assert tracked is not None and tracked.profile_dir is not None and tracked.profile_dir.exists()


def test_release_from_the_loop_closes(tmp_path: Path) -> None:
    rig = make_rig()
    scope = make_scope_for(tmp_path, rig.clock)

    async def go() -> None:
        await rig.manager.session("t", scope)
        rig.manager.release("t")
        for _ in range(5):
            await checkpoint()
        assert rig.manager.get("t") is None

    asyncio.run(go())


def test_shutdown_closes_everything_and_stops_the_driver(tmp_path: Path) -> None:
    rig = make_rig()
    scope = make_scope_for(tmp_path, rig.clock)

    async def go() -> None:
        await rig.manager.session("a", scope)
        await rig.manager.session("b", scope)
        await rig.manager.shutdown()

    asyncio.run(go())
    assert rig.manager.sessions == {}
    assert len(rig.driver.closed) == 2
    assert rig.driver.stopped


def test_janitor_loop_runs_on_the_fake_clock(tmp_path: Path) -> None:
    rig = make_rig()
    rig.manager.janitor_enabled = True
    scope = make_scope_for(tmp_path, rig.clock)

    async def go() -> None:
        await rig.manager.session("t", scope)
        for _ in range(5):
            await checkpoint()
        for _ in range(6):  # 180 s in janitor steps
            rig.clock.advance(session_mod.JANITOR_INTERVAL_S)
            for _ in range(5):
                await checkpoint()
        assert rig.manager.get("t") is None
        await rig.manager.shutdown()

    asyncio.run(go())
