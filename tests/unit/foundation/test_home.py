"""Home resolution, root rule and owner-only modes (P01-03)."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import stat
from pathlib import Path

import pytest

from indus.foundation import home
from indus.foundation.errors import UnsafeHomeDir
from tests.fakes.clock import checkpoint

USER = Path("/users/alex")


def _user() -> Path:
    return USER


def _not_root() -> int:
    return 1000


@pytest.mark.parametrize(
    ("env", "windows", "expected"),
    [
        ({}, False, USER / ".indus"),
        ({"INDUS_HOME": "/srv/indus"}, False, Path("/srv/indus")),
        ({"INDUS_HOME": "   "}, False, USER / ".indus"),
        ({"INDUS_HOME": "~/x"}, False, USER / "x"),
        ({"INDUS_HOME": "$BASE/h", "BASE": "/data"}, False, Path("/data/h")),
        ({"INDUS_HOME": "${BASE}/h", "BASE": "/data"}, False, Path("/data/h")),
        ({"INDUS_HOME": "$NOPE/h"}, False, Path("$NOPE/h")),
        ({"INDUS_DATA_DIR_SUFFIX": "-canary"}, False, USER / ".indus-canary"),
        ({"INDUS_DATA_DIR_SUFFIX": "../evil"}, False, USER / ".indus"),
        ({"LOCALAPPDATA": "/win/local"}, True, Path("/win/local/indus")),
        ({}, True, USER / "AppData" / "Local" / "indus"),
        (
            {"LOCALAPPDATA": "/win/local", "INDUS_DATA_DIR_SUFFIX": "-dev"},
            True,
            Path("/win/local/indus-dev"),
        ),
    ],
)
def test_resolution_table(env: dict[str, str], windows: bool, expected: Path) -> None:
    assert home.resolve_home(env, _user, windows=windows, euid=_not_root) == expected


def test_resolution_order() -> None:
    env = {"INDUS_HOME": "/from/env"}
    assert home.resolve_home(env, _user) == Path("/from/env")
    token = home.set_home_override("/from/override")
    try:
        assert home.resolve_home(env, _user) == Path("/from/override")
        assert "INDUS_HOME" in env and env["INDUS_HOME"] == "/from/env"
    finally:
        home.reset_home_override(token)
    assert home.resolve_home(env, _user) == Path("/from/env")


def test_override_does_not_leak_between_contexts() -> None:
    seen: dict[str, Path] = {}

    def task(name: str, path: str | None) -> None:
        if path is not None:
            home.set_home_override(path)
        seen[name] = home.resolve_home({}, _user, euid=_not_root)

    contextvars.copy_context().run(task, "a", "/a")
    contextvars.copy_context().run(task, "b", None)
    assert seen == {"a": Path("/a"), "b": USER / ".indus"}
    assert home.home_override() is None


def test_override_is_per_task() -> None:
    async def worker(path: str) -> Path:
        with home.home_override_set(path):
            await checkpoint()
            return home.resolve_home({}, _user)

    async def main() -> list[Path]:
        return list(await asyncio.gather(worker("/one"), worker("/two")))

    assert asyncio.run(main()) == [Path("/one"), Path("/two")]


def test_suffix_appended() -> None:
    assert home.default_home({"INDUS_DATA_DIR_SUFFIX": "-canary"}, _user, euid=_not_root) == (
        USER / ".indus-canary"
    )


def test_sudo_uses_invoking_user() -> None:
    lookups: list[str] = []

    def passwd(name: str) -> Path | None:
        lookups.append(name)
        return Path("/home/alex")

    env = {"SUDO_USER": "alex"}
    got = home.default_home(env, lambda: Path("/root"), euid=lambda: 0, home_of_user=passwd)
    assert got == Path("/home/alex/.indus")
    assert lookups == ["alex"]
    # not root, or SUDO_USER=root, or unknown user: the normal default
    assert home.default_home(env, lambda: Path("/root"), euid=_not_root) == Path("/root/.indus")
    root_env = {"SUDO_USER": "root"}
    assert home.default_home(root_env, lambda: Path("/root"), euid=lambda: 0) == Path(
        "/root/.indus"
    )
    assert home.default_home(
        env, lambda: Path("/root"), euid=lambda: 0, home_of_user=lambda _: None
    ) == Path("/root/.indus")


@pytest.mark.parametrize(
    ("given", "root"),
    [
        (Path("/r/profiles/work"), Path("/r")),
        (Path("/r"), Path("/r")),
        (Path("/r/profiles/.deleted"), Path("/r/profiles/.deleted")),
        (Path("/r/other/work"), Path("/r/other/work")),
    ],
)
def test_root_of(given: Path, root: Path) -> None:
    assert home.root_of(given) == root


@pytest.mark.parametrize(
    ("raw", "mode"),
    [
        ("", 0o700),
        ("0701", 0o701),
        ("701", 0o701),
        ("0o750", 0o750),
        ("abc", 0o700),
        ("0999", 0o700),
        ("7777", 0o700),
    ],
)
def test_dir_mode(raw: str, mode: int) -> None:
    assert home.dir_mode({"INDUS_HOME_MODE": raw}) == mode


def test_invalid_mode_warns(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="indus.foundation.home"):
        assert home.dir_mode({"INDUS_HOME_MODE": "rwx"}) == 0o700
    assert "INDUS_HOME_MODE" in caplog.text


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_secure_dir_applies_mode(tmp_path: Path) -> None:
    target = tmp_path / "h" / "logs"
    home.secure_dir(target, env={"INDUS_HOME_MODE": "0701"})
    assert _mode(target) == 0o701
    home.secure_dir(target, env={})
    assert _mode(target) == 0o700


def test_managed_install_keeps_modes(tmp_path: Path) -> None:
    root = tmp_path / "h"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    (root / ".managed").write_text("")
    target = root / "cache"
    target.mkdir()
    target.chmod(0o750)
    home.secure_dir(target, home=root, env={})
    assert _mode(target) == 0o750
    assert home.is_managed(None, {"INDUS_MANAGED": "nix"})
    assert not home.is_managed(None, {"INDUS_MANAGED": "false"})


def test_secure_file(tmp_path: Path) -> None:
    f = tmp_path / "secret"
    f.write_text("x")
    f.chmod(0o644)
    home.secure_file(f, env={}, in_container=lambda: True)
    assert _mode(f) == 0o644
    home.secure_file(f, env={}, in_container=lambda: False)
    assert _mode(f) == 0o600


@pytest.mark.parametrize("path", [Path("/"), Path("/home"), Path("/home/x"), Path("/usr/x")])
def test_secure_parent_dir_refuses_top_level(path: Path) -> None:
    with pytest.raises(UnsafeHomeDir) as err:
        home.secure_parent_dir(path, env={})
    assert err.value.code == "home.unsafe_dir"


def test_secure_parent_dir_restricts(tmp_path: Path) -> None:
    parent = tmp_path / "h"
    parent.mkdir()
    parent.chmod(0o755)
    home.secure_parent_dir(parent / "auth.json", env={})
    assert _mode(parent) == 0o700
