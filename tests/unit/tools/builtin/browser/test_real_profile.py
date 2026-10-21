"""Real-profile mode (P24-38): the user's own folder, never copied, one holder, 30 s wait."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.tools.builtin.browser.real_profile import (
    LOCK_WAIT_S,
    REAL_PROFILE_SESSION,
    RealProfileRefused,
    default_profile_dirs,
    in_use,
    profile_dir_for,
)
from tests.fakes.clock import checkpoint
from tests.fakes.secret_store import MemorySecretStore
from tests.unit.tools.builtin.browser._rig import make_rig, make_scope_for

PROVIDER_KEY = "OPENAI_API_" + "KEY"
FAKE_VALUE = "sk-" + "y" * 24


def user_profile(tmp_path: Path) -> Path:
    folder = tmp_path / "user-chrome"
    (folder / "Default").mkdir(parents=True)
    (folder / "Default" / "Cookies").write_bytes(b"cookie-db")
    return folder


def real_scope(tmp_path: Path, clock: Any, folder: Path | None, **extra: Any) -> Scope:
    browser: dict[str, Any] = {"real_profile": True, **extra}
    if folder is not None:
        browser["real_profile_dir"] = str(folder)
    return make_scope_for(tmp_path, clock, {"browser": browser})


def test_launches_on_the_users_folder_and_never_removes_it(tmp_path: Path) -> None:
    rig = make_rig()
    folder = user_profile(tmp_path)
    scope = real_scope(tmp_path, rig.clock, folder)

    async def go() -> None:
        made = await rig.manager.session("t", scope)
        assert made.engine == "real-profile"
        assert made.profile_dir is None and made.user_dir == folder
        assert rig.manager.real_profiles.holder == "t"
        await rig.manager.end("t")
        assert not rig.manager.real_profiles.held

    asyncio.run(go())
    [spec] = rig.driver.launches
    assert spec.profile_dir == folder  # the folder itself: no copy anywhere
    assert (folder / "Default" / "Cookies").read_bytes() == b"cookie-db"
    profiles = tmp_path / "home" / "cache" / "browser" / "profiles"
    assert not profiles.exists() or not any(profiles.iterdir())


def test_force_reap_kills_but_keeps_the_users_folder(tmp_path: Path) -> None:
    rig = make_rig()
    folder = user_profile(tmp_path)
    rig.processes.by_marker[f"--user-data-dir={folder}"] = [4242]
    scope = real_scope(tmp_path, rig.clock, folder)

    async def go() -> None:
        await rig.manager.session("t", scope)
        await rig.manager.force_reap("t")
        assert not rig.manager.real_profiles.held

    asyncio.run(go())
    assert rig.processes.killed == [4242]
    assert folder.is_dir() and (folder / "Default" / "Cookies").exists()


def test_held_profile_is_refused_after_thirty_seconds(tmp_path: Path) -> None:
    rig = make_rig()
    folder = user_profile(tmp_path)
    scope = real_scope(tmp_path, rig.clock, folder)

    async def go() -> None:
        await rig.manager.session("first", scope)
        waiting = asyncio.ensure_future(rig.manager.session("second", scope))
        for _ in range(5):
            await checkpoint()
        assert not waiting.done()
        rig.clock.advance(LOCK_WAIT_S - 1)
        for _ in range(5):
            await checkpoint()
        assert not waiting.done()  # still within the 30 s
        rig.clock.advance(1)
        with pytest.raises(RealProfileRefused) as refused:
            await waiting
        text = str(refused.value)
        assert REAL_PROFILE_SESSION in text and "first" in text and "30 s" in text
        assert rig.manager.real_profiles.holder == "first"
        await rig.manager.end("first")

    asyncio.run(go())
    assert len(rig.driver.launches) == 1


def test_refusal_reaches_the_tool_as_a_clear_error(tmp_path: Path) -> None:
    rig = make_rig()
    folder = user_profile(tmp_path)
    scope = real_scope(tmp_path, rig.clock, folder)

    async def go() -> None:
        await rig.manager.session("other", scope)
        call = asyncio.ensure_future(rig.tools.pages.snapshot({}, task_id="mine", scope=scope))
        for _ in range(5):
            await checkpoint()
        rig.clock.advance(LOCK_WAIT_S)
        out = json.loads(await call)
        assert out["success"] is False
        assert "held by other" in out["error"]
        await rig.manager.end("other")

    asyncio.run(go())


def test_holder_closing_hands_the_profile_over(tmp_path: Path) -> None:
    rig = make_rig()
    folder = user_profile(tmp_path)
    scope = real_scope(tmp_path, rig.clock, folder)

    async def go() -> None:
        await rig.manager.session("first", scope)
        waiting = asyncio.ensure_future(rig.manager.session("second", scope))
        for _ in range(5):
            await checkpoint()
        rig.clock.advance(10)
        await rig.manager.end("first")
        made = await waiting
        assert made.engine == "real-profile"
        assert rig.manager.real_profiles.holder == "second"
        await rig.manager.end("second")

    asyncio.run(go())
    assert len(rig.driver.launches) == 2


def test_folder_held_by_a_running_browser_is_refused(tmp_path: Path) -> None:
    rig = make_rig()
    folder = user_profile(tmp_path)
    os.symlink("somehost-4242", folder / "SingletonLock")  # dangling, as Chromium leaves it
    assert in_use(folder)
    scope = real_scope(tmp_path, rig.clock, folder)

    async def go() -> None:
        with pytest.raises(RealProfileRefused, match="Close that browser"):
            await rig.manager.session("t", scope)
        assert not rig.manager.real_profiles.held  # released after the refusal

    asyncio.run(go())
    assert rig.driver.launches == []
    assert os.path.lexists(folder / "SingletonLock")  # never removed


def test_missing_folder_is_refused(tmp_path: Path) -> None:
    rig = make_rig()
    scope = real_scope(tmp_path, rig.clock, tmp_path / "nowhere")

    async def go() -> None:
        with pytest.raises(RealProfileRefused, match="does not exist"):
            await rig.manager.session("t", scope)

    asyncio.run(go())


def test_consent_off_never_uses_the_profile(tmp_path: Path) -> None:
    rig = make_rig()
    folder = user_profile(tmp_path)
    scope = make_scope_for(
        tmp_path, rig.clock, {"browser": {"real_profile": False, "real_profile_dir": str(folder)}}
    )

    async def go() -> None:
        made = await rig.manager.session("t", scope)
        assert made.engine == "local"
        await rig.manager.end("t")

    asyncio.run(go())
    assert rig.driver.launches[0].profile_dir != folder


def test_launch_env_comes_from_the_env_builder(tmp_path: Path) -> None:
    rig = make_rig()
    folder = user_profile(tmp_path)
    scope = real_scope(tmp_path, rig.clock, folder).with_secrets(
        MemorySecretStore({PROVIDER_KEY: FAKE_VALUE})
    )

    async def go() -> None:
        await rig.manager.session("t", scope)
        await rig.manager.end("t")

    asyncio.run(go())
    env = rig.driver.launches[0].env
    assert PROVIDER_KEY not in env and FAKE_VALUE not in env.values()


def test_default_folders_per_platform(tmp_path: Path) -> None:
    linux = default_profile_dirs({"HOME": "/home/u"}, "linux")
    assert linux[0] == Path("/home/u/.config/google-chrome")
    mac = default_profile_dirs({"HOME": "/Users/u"}, "darwin")
    assert mac[0] == Path("/Users/u/Library/Application Support/Google/Chrome")
    win = default_profile_dirs({"LOCALAPPDATA": r"C:\Users\u\AppData\Local"}, "win32")
    assert win[0].name == "User Data"
    assert default_profile_dirs({}, "linux") == []


def test_profile_dir_detects_an_existing_default(tmp_path: Path) -> None:
    rig = make_rig()
    home = tmp_path / "home"
    (home / ".config" / "chromium").mkdir(parents=True)
    scope = make_scope_for(tmp_path, rig.clock, {"browser": {"real_profile": True}})
    assert profile_dir_for(scope) == home / ".config" / "chromium"
