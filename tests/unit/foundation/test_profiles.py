"""Profile ids, markers, tombstones and the sticky profile (P01-04, R04.25)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from indus.foundation import profiles
from indus.foundation.errors import InvalidProfileId, ProfileNotFound, ProfileTombstoned
from tests.fakes.clock import FakeClock


def _make(root: Path, pid: str, marker: str = "config.yaml", body: str = "x: 1\n") -> Path:
    home = root / "profiles" / pid
    home.mkdir(parents=True, exist_ok=True)
    (home / marker).write_text(body)
    return home


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Work", "work"),
        ("  work  ", "work"),
        ("0abc", "0abc"),
        ("a_b-c", "a_b-c"),
        ("DEFAULT", "default"),
        ("Default", "default"),
        ("x" * 64, "x" * 64),
    ],
)
def test_id_valid(raw: str, expected: str) -> None:
    assert profiles.normalize_id(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "-x",
        "_x",
        "x" * 65,
        "",
        "   ",
        "a b",
        "a/b",
        "../x",
        "indus",
        "test",
        "tmp",
        "root",
        "sudo",
        7,
        None,
    ],
)
def test_id_invalid(raw: object) -> None:
    with pytest.raises(InvalidProfileId) as err:
        profiles.normalize_id(raw)
    assert err.value.code == "profile.invalid_id"


def test_id_table() -> None:
    assert profiles.is_valid_id("Work") and not profiles.is_valid_id(7)
    assert not profiles.is_valid_id("indus")


@pytest.mark.parametrize(
    ("text", "suggestion"),
    [("My Work", "my-work"), ("Team #1!", "team-1"), ("!!!", "my-work"), ("indus", "my-work")],
)
def test_suggest_id(text: str, suggestion: str) -> None:
    assert profiles.suggest_id(text) == suggestion


def test_markers(tmp_path: Path) -> None:
    home = tmp_path / "profiles" / "w"
    (home / "logs").mkdir(parents=True)
    assert not profiles.has_identity(home)  # only logs/: not a profile
    for marker in profiles.IDENTITY_MARKERS:
        (home / marker).write_text("1")
        assert profiles.has_identity(home)
        (home / marker).unlink()
    (home / "PERSONA.md").symlink_to(tmp_path / "missing-target")
    assert profiles.has_identity(home) and profiles.servable(home)


def test_empty_env_marker_not_servable(tmp_path: Path) -> None:
    home = tmp_path / "profiles" / "w"
    home.mkdir(parents=True)
    (home / ".env").write_text("")
    assert profiles.has_identity(home)
    assert not profiles.servable(home)
    (home / ".env").write_text("K=v\n")
    assert profiles.servable(home)
    (home / ".env").write_text("")
    (home / "state.db").write_bytes(b"")
    assert profiles.servable(home)


def test_list_profiles(tmp_path: Path) -> None:
    _make(tmp_path, "zeta")
    _make(tmp_path, "alpha", ".env", "")
    (tmp_path / "profiles" / "ghost" / "logs").mkdir(parents=True)
    (tmp_path / "profiles" / "Bad Name").mkdir()
    assert profiles.list_profiles(tmp_path) == ["default", "alpha", "zeta"]
    assert profiles.list_profiles(tmp_path / "nowhere") == ["default"]


def test_tombstoned_profile(tmp_path: Path) -> None:
    home = _make(tmp_path, "work")
    clock = FakeClock(datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC))
    stone = profiles.tombstone(tmp_path, "work", clock=clock)
    assert stone == tmp_path / "profiles" / ".deleted" / "work"
    assert stone.read_text().startswith("2026-03-04T05:06:07")
    assert profiles.is_tombstoned(tmp_path, "work")
    assert not profiles.is_live(tmp_path, "work")
    assert profiles.list_profiles(tmp_path) == ["default"]
    with pytest.raises(ProfileTombstoned):
        profiles.mkdir_in_profile(home, "sessions")
    assert not (home / "sessions").exists()
    # removing the dir does not remove the tombstone; a stray mkdir still refused
    for child in home.iterdir():
        child.unlink()
    home.rmdir()
    assert profiles.is_tombstoned(tmp_path, "work")
    with pytest.raises(ProfileTombstoned):
        profiles.mkdir_in_profile(home, "logs")
    assert not home.exists()
    profiles.clear_tombstone(tmp_path, "work")
    assert not profiles.is_tombstoned(tmp_path, "work")


def test_mkdir_in_missing_profile_refused(tmp_path: Path) -> None:
    with pytest.raises(ProfileNotFound):
        profiles.mkdir_in_profile(tmp_path / "profiles" / "nope", "logs")
    assert not (tmp_path / "profiles" / "nope").exists()


def test_mkdir_in_live_profile_and_root(tmp_path: Path) -> None:
    home = _make(tmp_path, "work")
    assert profiles.mkdir_in_profile(home, "logs/curator").is_dir()
    assert profiles.mkdir_in_profile(tmp_path, "cache").is_dir()


def test_default_cannot_be_tombstoned(tmp_path: Path) -> None:
    with pytest.raises(InvalidProfileId):
        profiles.tombstone(tmp_path, "default")


def test_read_active_bom(tmp_path: Path) -> None:
    _make(tmp_path, "work")
    (tmp_path / "active_profile").write_bytes(b"\xef\xbb\xbfWork \n")
    assert profiles.read_active(tmp_path) == "work"


@pytest.mark.parametrize(
    "content", [None, b"", b"  \n", b"default\n", b"not a valid id!\n", b"\xff\xfe"]
)
def test_read_active_defaults(tmp_path: Path, content: bytes | None) -> None:
    if content is not None:
        (tmp_path / "active_profile").write_bytes(content)
    assert profiles.read_active(tmp_path) == "default"


def test_write_active(tmp_path: Path) -> None:
    _make(tmp_path, "work")
    profiles.write_active(tmp_path, "Work")
    assert (tmp_path / "active_profile").read_text() == "work\n"
    assert profiles.read_active(tmp_path) == "work"
    with pytest.raises(ProfileNotFound):
        profiles.write_active(tmp_path, "missing")


def test_write_active_default_removes_file(tmp_path: Path) -> None:
    _make(tmp_path, "work")
    profiles.write_active(tmp_path, "work")
    profiles.write_active(tmp_path, "default")
    assert not (tmp_path / "active_profile").exists()
    profiles.write_active(tmp_path, "default")  # idempotent


def test_profile_home_and_named_of(tmp_path: Path) -> None:
    assert profiles.profile_home(tmp_path, "default") == tmp_path
    assert profiles.profile_home(tmp_path, "Work") == tmp_path / "profiles" / "work"
    assert profiles.named_profile_of(tmp_path / "profiles" / "work") == (tmp_path, "work")
    assert profiles.named_profile_of(tmp_path) is None
    assert profiles.profile_id_of(tmp_path / "profiles" / "work") == "work"
    assert profiles.profile_id_of(tmp_path) == "default"
