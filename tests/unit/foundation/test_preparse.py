"""Profile selection before any app import (P01-05, R04.2)."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from indus.foundation import preparse, profiles
from indus.foundation.errors import InvalidProfileId, ProfileNotFound, StickyProfileMissing

VALUE_FLAGS = frozenset({"-m", "--model", "--resume"})


@pytest.fixture(autouse=True)
def _reset_once() -> Iterator[None]:
    preparse.reset_once_flags()
    yield
    preparse.reset_once_flags()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "root"
    for pid in ("work", "x"):
        home = r / "profiles" / pid
        home.mkdir(parents=True)
        (home / "config.yaml").write_text("{}\n")
    return r


def _env(root: Path, **extra: str) -> dict[str, str]:
    return {"INDUS_HOME": str(root), **extra}


def _select(argv: list[str], env: dict[str, str], **kw: object) -> preparse.Selection:
    return preparse.select_profile(argv, env, value_flags=VALUE_FLAGS, **kw)  # type: ignore[arg-type]


def test_short_flag(root: Path) -> None:
    sel = _select(["-p", "work", "chat"], _env(root))
    assert sel.argv == ["chat"]
    assert sel.source == "flag"
    assert sel.profile_id == "work"
    assert sel.home == root / "profiles" / "work"


def test_long_flag_with_equals_anywhere(root: Path) -> None:
    sel = _select(["chat", "--profile=Work", "-q", "hi"], _env(root))
    assert (sel.profile_id, sel.argv, sel.source) == ("work", ["chat", "-q", "hi"], "flag")
    sel = _select(["chat", "--profile", "x"], _env(root))
    assert (sel.profile_id, sel.argv) == ("x", ["chat"])


def test_value_flag_hides_profile(root: Path) -> None:
    sel = _select(["-m", "-p", "x"], _env(root))
    assert sel.source == "default"
    assert sel.argv == ["-m", "-p", "x"]
    assert sel.home == root


def test_nested_tail_untouched(root: Path) -> None:
    argv = ["mcp", "add", "--args", "-p", "x"]
    sel = _select(argv, _env(root))
    assert sel.argv == argv and sel.source == "default"
    argv = ["run", "--", "-p", "x"]
    assert _select(argv, _env(root)).argv == argv


def test_default_flag_selects_root(root: Path) -> None:
    sel = _select(["-p", "Default"], _env(root))
    assert (sel.home, sel.profile_id, sel.argv) == (root, "default", [])


def test_env_home_under_profiles_trusted(root: Path) -> None:
    profiles.write_active(root, "x")
    sel = _select(["chat"], {"INDUS_HOME": str(root / "profiles" / "work")})
    assert (sel.source, sel.profile_id, sel.home) == ("env", "work", root / "profiles" / "work")


def test_sticky_bom_file(root: Path) -> None:
    (root / "active_profile").write_bytes(b"\xef\xbb\xbfwork\r\n")
    sel = _select(["chat"], _env(root))
    assert (sel.source, sel.profile_id) == ("sticky", "work")
    assert sel.warning is None  # INDUS_HOME was set


def test_sticky_note_once_when_home_unset(tmp_path: Path) -> None:
    user = tmp_path / "user"
    root = user / ".indus"
    home = root / "profiles" / "work"
    home.mkdir(parents=True)
    (home / "PERSONA.md").write_text("hi\n")
    profiles.write_active(root, "work")
    first = _select([], {}, user_home=lambda: user, euid=lambda: 1000)
    second = _select([], {}, user_home=lambda: user, euid=lambda: 1000)
    assert first.source == "sticky" and first.warning and "work" in first.warning
    assert second.source == "sticky" and second.warning is None


def test_supervised_child_ignores_sticky(root: Path) -> None:
    profiles.write_active(root, "work")
    sel = _select(["gateway", "run"], _env(root, INDUS_GATEWAY_SUPERVISED="1"))
    assert (sel.source, sel.home) == ("default", root)


def test_tombstoned_flag_profile(root: Path) -> None:
    profiles.tombstone(root, "work")
    with pytest.raises(ProfileNotFound) as err:
        _select(["-p", "work"], _env(root))
    assert "indus profile list" in (err.value.hint or "")
    assert "deleted" in str(err.value)


def test_unknown_flag_profile(root: Path) -> None:
    with pytest.raises(ProfileNotFound):
        _select(["-p", "nobody"], _env(root))


def test_invalid_flag_value(root: Path) -> None:
    with pytest.raises(InvalidProfileId):
        _select(["-p", "../etc"], _env(root))


def test_sticky_missing(root: Path) -> None:
    (root / "active_profile").write_text("gone\n")
    with pytest.raises(StickyProfileMissing):
        _select(["chat"], _env(root))


def test_sticky_missing_recovery_command(root: Path) -> None:
    (root / "active_profile").write_text("gone\n")
    sel = _select(
        ["-m", "x", "profile", "use", "default"], _env(root), recovery_commands={"profile"}
    )
    assert sel.source == "default" and sel.home == root
    assert sel.warning and "gone" in sel.warning


def test_unexpected_error_falls_back_to_default(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_root: Path) -> str:
        raise RuntimeError("bug in profiles")

    monkeypatch.setattr(profiles, "read_active", boom)
    sel = _select(["chat"], _env(root))
    assert sel.profile_id == "default"
    assert sel.home == root
    assert sel.warning and "bug in profiles" in sel.warning


def test_sudo_profile_from_invoking_user(tmp_path: Path) -> None:
    alex = tmp_path / "alex"
    home = alex / ".indus" / "profiles" / "work"
    home.mkdir(parents=True)
    (home / "auth.json").write_text("{}")
    sel = preparse.select_profile(
        ["-p", "work"],
        {"SUDO_USER": "alex"},
        user_home=lambda: tmp_path / "rootuser",
        euid=lambda: 0,
        home_of_user=lambda _name: alex,
    )
    assert sel.home == home


def test_selection_writes_nothing(root: Path) -> None:
    import os

    before = dict(os.environ)
    _select(["-p", "work"], _env(root))
    assert dict(os.environ) == before


ALLOWED = {
    "indus",
    "indus.foundation",
    "indus.foundation.preparse",
    "indus.foundation.home",
    "indus.foundation.profiles",
    "indus.foundation.errors",
}


def test_fresh_interpreter_imports_nothing_else() -> None:
    code = (
        "import json, sys; import indus.foundation.preparse; "
        "names = [m for m in sys.modules if m == 'indus' or m.startswith('indus.')]; "
        "print(json.dumps(sorted(names)))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
    )
    loaded = set(json.loads(out.stdout))
    assert loaded <= ALLOWED, loaded - ALLOWED
    assert "indus.foundation.preparse" in loaded
