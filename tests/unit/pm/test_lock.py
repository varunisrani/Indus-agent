"""P24-23: lock and facts shapes, the stat cache, our package list and the machine target."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from indus.pm import core
from indus.pm.core import current_target, definitions, parse_definitions
from indus.pm.lock import (
    ANY,
    TARGETS,
    Artifact,
    Fact,
    Facts,
    Lock,
    LockInvalid,
    Pin,
    PmError,
    StatCache,
)

SHA_A = "a" * 64
SHA_B = "b" * 64


def test_targets_are_the_ten_of_the_row() -> None:
    assert set(TARGETS) == {
        "any",
        "darwin-arm64",
        "darwin-x64",
        "linux-arm64",
        "linux-arm64-bionic",
        "linux-arm64-musl",
        "linux-x64",
        "linux-x64-musl",
        "win32-arm64",
        "win32-x64",
    }


def test_lock_round_trip_has_the_row_shape(tmp_path: Path) -> None:
    lock = Lock(
        {
            "tool": Pin(
                "1.0",
                {"linux-x64": Artifact("https://dl.example/t", SHA_A), ANY: Artifact("u", SHA_B)},
            )
        }
    )
    path = tmp_path / "lock.json"
    lock.write(path)
    data = json.loads(path.read_text())
    assert data == {
        "schema": 1,
        "packages": {
            "tool": {
                "version": "1.0",
                "artifacts": {
                    "any": {"url": "u", "sha256": SHA_B},
                    "linux-x64": {"url": "https://dl.example/t", "sha256": SHA_A},
                },
            }
        },
    }
    assert Lock.read(path) == lock


def test_a_target_without_its_own_row_uses_any() -> None:
    pin = Pin("1", {ANY: Artifact("u", SHA_A), "win32-x64": Artifact("w", SHA_B)})
    assert pin.artifact("linux-x64") == Artifact("u", SHA_A)
    assert pin.artifact("win32-x64") == Artifact("w", SHA_B)
    assert Pin("1", {"win32-x64": Artifact("w", SHA_B)}).artifact("linux-x64") is None


def test_a_missing_lock_is_empty(tmp_path: Path) -> None:
    assert Lock.read(tmp_path / "absent.json").packages == {}


@pytest.mark.parametrize(
    "data",
    [
        {"schema": 2, "packages": {}},
        {"schema": 1},
        {"schema": 1, "packages": {"t": {"version": "1", "artifacts": {"mars-x64": {}}}}},
        {"schema": 1, "packages": {"t": {"version": "1", "artifacts": {"any": {"url": "u"}}}}},
        {
            "schema": 1,
            "packages": {"t": {"version": "1", "artifacts": {"any": {"url": "u", "sha256": "ab"}}}},
        },
        [],
    ],
)
def test_a_lock_this_build_cannot_read_is_an_error(tmp_path: Path, data: object) -> None:
    path = tmp_path / "lock.json"
    path.write_text(json.dumps(data))
    with pytest.raises(LockInvalid) as caught:
        Lock.read(path)
    assert "indus pm lock" in str(caught.value)


def test_a_broken_facts_file_reads_empty_and_keeps_its_bytes(tmp_path: Path) -> None:
    path = tmp_path / "facts.json"
    path.write_text("{not json")
    assert Facts.read(path).packages == {}
    assert (tmp_path / "facts.json.corrupt").read_text() == "{not json"


def test_facts_round_trip(tmp_path: Path) -> None:
    fact = Fact("1.0", "linux-x64", "https://x", SHA_A, "tool-1.0-aaaa", SHA_B, "bin/t", ("bin",))
    facts = Facts({"tool": fact}, ["extra"])
    path = tmp_path / "facts.json"
    facts.write(path)
    again = Facts.read(path)
    assert again.packages == {"tool": fact}
    assert again.declined == ["extra"]
    assert again.entries_in_use() == {"tool-1.0-aaaa"}
    assert again.blobs_in_use() == {SHA_A}


def test_a_fact_with_a_path_like_entry_is_dropped() -> None:
    raw = Fact("1", "t", "u", SHA_A, "x", SHA_B, None, ()).to_json() | {"entry": "../x"}
    assert Fact.from_json(raw) is None


def test_stat_cache_reads_only_when_the_file_changes(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_text("1")
    reads: list[str] = []

    def load(p: Path) -> str:
        reads.append(p.read_text())
        return reads[-1]

    cache: StatCache[str] = StatCache(load)
    assert cache.get(path) == "1"
    assert cache.get(path) == "1"
    assert reads == ["1"]
    path.write_text("22")
    assert cache.get(path) == "22"
    assert reads == ["1", "22"]


# -- our package list ------------------------------------------------------------------------------


def test_our_package_list_parses_and_covers_the_row() -> None:
    packages = definitions()
    assert {"ripgrep", "ffmpeg", "uv", "node", "gh", "chromium", "whispercpp", "llamacpp"} <= set(
        packages
    )
    for package in packages.values():
        for target in package.targets:
            url = package.url_for(package.version, target)
            assert url.startswith("https://"), url
            assert package.format_for(target) in ("tar.gz", "tar.xz", "zip", "file")
            exe = package.exe_for(package.version, target)
            assert exe is None or (exe.endswith(".exe") == target.startswith("win32"))


def test_definitions_refuse_unknown_targets_kinds_and_dependencies() -> None:
    base = '[packages.x]\nkind = "required"\nversion = "1"\nurl = "https://a/{version}"\n'
    base += 'format = "file"\n'
    with pytest.raises(PmError, match="unknown target"):
        parse_definitions(base + "[packages.x.targets]\nmars = {}\n")
    with pytest.raises(PmError, match="kind"):
        parse_definitions(base.replace("required", "maybe") + "[packages.x.targets]\nany = {}\n")
    with pytest.raises(PmError, match="depends on unknown"):
        parse_definitions(base + 'depends = ["y"]\n[packages.x.targets]\nany = {}\n')


def test_windows_targets_get_an_exe_suffix() -> None:
    node = definitions()["node"]
    assert node.exe_for("22.11.0", "win32-x64") == "node.exe"
    assert node.exe_for("22.11.0", "linux-x64") == "bin/node"
    assert node.path_for("22.11.0", "linux-x64") == ("bin",)


# -- target ----------------------------------------------------------------------------------------


def test_current_target(monkeypatch: pytest.MonkeyPatch) -> None:
    from indus.platform import facts

    monkeypatch.setattr(facts, "os_family", lambda: "linux")
    monkeypatch.setattr(facts, "process_arch", lambda: "amd64")
    assert current_target({}, exists=lambda _p: False) == "linux-x64"
    assert current_target({}, exists=lambda p: p in core.MUSL_LOADERS) == "linux-x64-musl"
    monkeypatch.setattr(facts, "process_arch", lambda: "arm64")
    termux = {"TERMUX_VERSION": "0.118"}
    assert current_target(termux, exists=lambda _p: False) == "linux-arm64-bionic"
    monkeypatch.setattr(facts, "os_family", lambda: "darwin")
    assert current_target({}) == "darwin-arm64"
    monkeypatch.setattr(facts, "os_family", lambda: "win32")
    monkeypatch.setattr(facts, "process_arch", lambda: "amd64")
    assert current_target({}) == "win32-x64"
    monkeypatch.setattr(facts, "process_arch", lambda: "x86")
    with pytest.raises(PmError):
        current_target({})
