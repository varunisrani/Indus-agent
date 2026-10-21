"""P24-23: unpacking, the content-addressed store, ``ensure`` and the boundary rule."""

from __future__ import annotations

import builtins
import io
import os
import subprocess
import sys
import tarfile
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from indus.foundation.scope import Scope
from indus.pm import core
from indus.pm.core import Pm, parse_definitions
from indus.pm.lock import Facts, Pin, PmError
from indus.pm.store import Store, UnpackRefused, entry_name, tree_digest, unpack
from indus.safety.child_env import child_env
from tests.fakes.pm_origin import FakeOrigin, definitions_text, sha256, tar_gz, zip_bytes
from tests.unit.pm.conftest import (
    TARGET,
    publish_tool,
    tool_rows,
    write_lock,
)

REPO = Path(__file__).resolve().parents[3]


# -- unpacking -------------------------------------------------------------------------------------


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_tar_gz_unpacks_with_strip(tmp_path: Path) -> None:
    archive = _write(tmp_path, "a.tar.gz", tar_gz({"bin/x": b"x", "doc/r": b"r"}, top="pkg-1"))
    dest = tmp_path / "out"
    dest.mkdir()
    unpack(archive, dest, "tar.gz", strip=1)
    assert (dest / "bin" / "x").read_bytes() == b"x"
    assert (dest / "doc" / "r").read_bytes() == b"r"
    assert not (dest / "pkg-1").exists()


def test_zip_keeps_the_executable_bit(tmp_path: Path) -> None:
    data = zip_bytes({"tool": b"run", "notes.txt": b"n"}, top="top", exec_names=("tool",))
    archive = _write(tmp_path, "a.zip", data)
    dest = tmp_path / "out"
    dest.mkdir()
    unpack(archive, dest, "zip", strip=1)
    assert os.access(dest / "tool", os.X_OK)
    assert not os.access(dest / "notes.txt", os.X_OK)


def test_a_single_file_package(tmp_path: Path) -> None:
    archive = _write(tmp_path, "blob", b"#!/bin/sh\n")
    dest = tmp_path / "out"
    dest.mkdir()
    unpack(archive, dest, "file", file_name="ffmpeg")
    assert os.access(dest / "ffmpeg", os.X_OK)


def _tar_with(member: tarfile.TarInfo, data: bytes = b"") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        member.size = len(data)
        tar.addfile(member, io.BytesIO(data) if data else None)
    return buffer.getvalue()


@pytest.mark.parametrize(
    "member",
    [
        tarfile.TarInfo("../escape"),
        tarfile.TarInfo("/etc/owned"),
        tarfile.TarInfo("link"),
        tarfile.TarInfo("dev"),
    ],
    ids=["dotdot", "absolute", "symlink-out", "device"],
)
def test_unsafe_tar_members_are_refused(tmp_path: Path, member: tarfile.TarInfo) -> None:
    if member.name == "link":
        member.type = tarfile.SYMTYPE
        member.linkname = "../../outside"
    elif member.name == "dev":
        member.type = tarfile.CHRTYPE
    data = b"x" if member.type == tarfile.REGTYPE else b""
    archive = _write(tmp_path, "a.tar.gz", _tar_with(member, data))
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(UnpackRefused):
        unpack(archive, dest, "tar.gz")
    assert not (tmp_path / "escape").exists()


def test_unsafe_zip_members_are_refused(tmp_path: Path) -> None:
    archive = _write(tmp_path, "a.zip", zip_bytes({"../escape": b"x"}))
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(UnpackRefused):
        unpack(archive, dest, "zip")
    assert not (tmp_path / "escape").exists()


def test_a_broken_archive_is_refused(tmp_path: Path) -> None:
    archive = _write(tmp_path, "a.tar.gz", b"not an archive")
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(UnpackRefused):
        unpack(archive, dest, "tar.gz")


def test_tree_digest_sees_content_changes(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "f").write_text("1")
    first = tree_digest(tmp_path)
    assert tree_digest(tmp_path) == first
    (tmp_path / "a" / "f").write_text("2")
    assert tree_digest(tmp_path) != first


def test_publish_keeps_an_entry_that_won_the_race(tmp_path: Path) -> None:
    store = Store(tmp_path / "store")
    with store.staging() as staged:
        (staged / "f").write_text("mine")
        winner = store.entry("pkg-1-abc")
        winner.mkdir(parents=True)
        (winner / "f").write_text("theirs")
        assert store.publish(staged, "pkg-1-abc") == winner
    assert (winner / "f").read_text() == "theirs"
    assert store.staging_dirs() == []


# -- ensure ----------------------------------------------------------------------------------------


def test_ensure_installs_into_a_content_addressed_entry(locked_pm: Pm, origin: FakeOrigin) -> None:
    runner = locked_pm.ensure("tool")
    pin = locked_pm.pin("tool")
    sha = pin.artifacts[TARGET].sha256
    assert runner.entry == locked_pm.paths.store / entry_name("tool", "1.0", sha)
    assert runner.executable == runner.entry / "bin" / "tool"
    assert os.access(runner.entry / "bin" / "tool", os.X_OK)
    assert runner.path_dirs == (runner.entry / "bin",)
    fact = Facts.read(locked_pm.paths.facts).packages["tool"]
    assert (fact.version, fact.sha256, fact.target, fact.exe) == ("1.0", sha, TARGET, "bin/tool")
    assert fact.digest == tree_digest(runner.entry)
    assert fact.installed_at.startswith("2026-01-01T09:00")
    assert locked_pm.store.staging_dirs() == []


def test_ensure_without_a_pin_or_artifact_says_what_to_run(
    make_pm: Callable[..., Pm], origin: FakeOrigin
) -> None:
    pm = make_pm()
    with pytest.raises(PmError, match="indus pm lock"):
        pm.ensure("tool")
    write_lock(pm, {"tool": Pin("1.0", {"darwin-arm64": publish_tool(origin)})})
    with pytest.raises(PmError, match="no artifact for linux-x64"):
        pm.ensure("tool")
    assert origin.seen == []


def test_an_interrupted_unpack_leaves_no_half_entry(
    locked_pm: Pm, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(archive: Path, dest: Path, fmt: str, **_: object) -> None:
        (dest / "bin").mkdir()
        (dest / "bin" / "tool").write_text("half")
        raise OSError("disk full")

    monkeypatch.setattr(core, "unpack", broken)
    with pytest.raises(OSError, match="disk full"):
        locked_pm.ensure("tool")
    assert locked_pm.store.entries() == []
    assert locked_pm.store.staging_dirs() == []
    assert Facts.read(locked_pm.paths.facts).packages == {}
    monkeypatch.undo()
    assert locked_pm.ensure("tool").executable is not None


def test_a_killed_unpack_leaves_only_a_staging_folder(locked_pm: Pm) -> None:
    leftover = locked_pm.paths.store / ".staging-999-dead"
    (leftover / "bin").mkdir(parents=True)
    runner = locked_pm.ensure("tool")
    assert runner.entry.is_dir()
    assert [p.name for p in locked_pm.store.entries()] == [runner.entry.name]


def test_ensure_on_a_satisfied_facts_file_only_stats(
    locked_pm: Pm, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = locked_pm.ensure("tool")

    def forbidden(*_a: object, **_k: object) -> object:
        raise AssertionError("ensure did I/O beyond a stat")

    class NoNetwork(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            raise AssertionError("ensure used the network")

    locked_pm.close()
    locked_pm.transport = NoNetwork()
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(os, "open", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    again = locked_pm.ensure("tool")
    monkeypatch.undo()
    assert again == first


def test_a_new_pin_installs_a_new_entry(locked_pm: Pm, origin: FakeOrigin) -> None:
    old = locked_pm.ensure("tool")
    write_lock(
        locked_pm,
        {"tool": Pin("1.1", {TARGET: publish_tool(origin, "1.1")})},
    )
    new = locked_pm.ensure("tool")
    assert new.version == "1.1"
    assert new.entry != old.entry
    assert b"tool-1.1" in (new.entry / "bin" / "tool").read_bytes()


def test_dependencies_install_first_and_join_the_path(
    make_pm: Callable[..., Pm], origin: FakeOrigin
) -> None:
    rows = {
        "tool": tool_rows(),
        "wrapper": {
            **tool_rows(),
            "url": "https://dl.example/wrapper/{version}/w-{plat}.tar.gz",
            "exe": "bin/wrapper",
            "depends": ["tool"],
        },
    }
    pm = make_pm(parse_definitions(definitions_text(rows)))
    wrapper = tar_gz({"bin/wrapper": b"#!/bin/sh\n"})
    url = origin.add("wrapper/1.0/w-linux-x64.tar.gz", wrapper)
    from indus.pm.lock import Artifact

    write_lock(
        pm,
        {
            "tool": Pin("1.0", {TARGET: publish_tool(origin)}),
            "wrapper": Pin("1.0", {TARGET: Artifact(url, sha256(wrapper))}),
        },
    )
    runner = pm.ensure("wrapper")
    tool = pm.runner("tool")
    assert tool is not None
    assert runner.path_dirs == (runner.entry / "bin", tool.entry / "bin")
    assert pm.runner("missing") is None


def test_a_dependency_cycle_is_an_error(make_pm: Callable[..., Pm]) -> None:
    rows = {"a": {**tool_rows(), "depends": ["b"]}, "b": {**tool_rows(), "depends": ["a"]}}
    pm = make_pm(parse_definitions(definitions_text(rows)))
    with pytest.raises(PmError, match="cycle"):
        pm.ensure("a")


def test_the_runner_environment_comes_from_the_env_builder(locked_pm: Pm, scope: Scope) -> None:
    runner = locked_pm.ensure("tool")
    secret_name = "OPENAI_API_KEY"
    view = scope.env.with_overlay({secret_name: "x" * 8, "PATH": "/usr/bin"})
    env = runner.env(scope.with_env(view), child_env)
    assert env["PATH"].split(os.pathsep) == [str(runner.entry / "bin"), "/usr/bin"]
    assert secret_name not in env
    assert env["INDUS_HOME"] == str(scope.home)


def test_the_runner_runs_with_a_timeout(locked_pm: Pm, scope: Scope) -> None:
    runner = locked_pm.ensure("tool")
    done = runner.run([], scope=scope, builder=child_env, timeout=10)
    assert done.stdout.strip() == b"tool-1.0"


# -- facade and boundary ---------------------------------------------------------------------------


def test_importing_the_facade_loads_no_internals() -> None:
    probe = (
        "import sys, indus.pm.api\n"
        "print(sorted(m for m in sys.modules if m.startswith('indus.pm.') or m == 'httpx'))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60, check=True
    )
    assert done.stdout.strip() == "['indus.pm.api']"


def test_the_facade_ensures_through_one_manager_per_root(
    scope: Scope, origin: FakeOrigin, monkeypatch: pytest.MonkeyPatch
) -> None:
    import indus.pm.api as facade

    monkeypatch.setattr(facade, "_managers", {})
    pm = facade.manager(scope)
    assert facade.manager(scope) is pm
    pm.target = TARGET
    pm.packages = parse_definitions(definitions_text({"tool": tool_rows()}))
    pm.transport = origin.transport()
    write_lock(pm, {"tool": Pin("1.0", {TARGET: publish_tool(origin)})})
    assert facade.tool_path(scope, "tool") is None
    runner = facade.ensure(scope, "tool")
    assert facade.tool_path(scope, "tool") == runner.executable
    assert facade.env_for(scope, "tool") == {"PATH": [str(runner.entry / "bin")]}
    pm.close()


def _ruff(source: str, filename: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--no-cache", "--stdin-filename", filename, "-"],
        input=source,
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=120,
        check=False,
    )


def test_banned_api_refuses_pm_internals_outside_pm() -> None:
    seeded = "from indus.pm import fetch\n\n__all__ = ['fetch']\n"
    outside = _ruff(seeded, "src/indus/tools/seeded.py")
    assert outside.returncode == 1
    assert "TID251" in outside.stdout and "indus.pm.fetch" in outside.stdout
    inside = _ruff(seeded, "src/indus/pm/seeded.py")
    assert inside.returncode == 0, inside.stdout
    facade = _ruff(
        "from indus.pm.api import ensure\n\n__all__ = ['ensure']\n", "src/indus/tools/x.py"
    )
    assert facade.returncode == 0, facade.stdout
