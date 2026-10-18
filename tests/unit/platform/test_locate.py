"""Command lookup tiers (R09.18)."""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

import pytest

from indus.platform import known_dirs
from indus.platform.locate import inspect, locate, probe
from indus.platform.shapes import CheckState, Effort, LookupContext, PathSource, Probe, Resolution


def _exe(path: Path, body: str = "#!/bin/sh\necho hi\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def _plain(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not executable\n")
    path.chmod(0o644)
    return path


def _home(tmp_path: Path) -> Path:
    return tmp_path / "user"


def test_executable_wins_over_non_executable(tmp_path: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    _plain(first / "tool")
    good = _exe(second / "tool")
    res = locate("tool", LookupContext(path=f"{first}{os.pathsep}{second}"), known=(), env={})
    assert res.kind == "path_executable"
    assert res.path == str(good)
    assert [c.source for c in res.candidates] == ["PATH"]


def test_relative_explicit_path_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _exe(tmp_path / "bin" / "tool")
    monkeypatch.chdir(tmp_path)
    res = locate("bin/tool", known=(), env={})
    assert res.kind == "missing"
    assert res.candidates[0].source == "explicit"
    assert not res.candidates[0].present


def test_absolute_explicit_path(tmp_path: Path) -> None:
    tool = _exe(tmp_path / "bin" / "tool")
    assert locate(str(tool), known=(), env={}).kind == "explicit_path"
    _plain(tmp_path / "bin" / "data")
    assert locate(str(tmp_path / "bin" / "data"), known=(), env={}).kind == "missing"


def test_tilde_explicit_path_uses_injected_home(tmp_path: Path) -> None:
    tool = _exe(_home(tmp_path) / "tools" / "x")
    res = locate("~/tools/x", known=(), env={}, user_home=lambda: _home(tmp_path))
    assert res.kind == "explicit_path" and res.path == str(tool)


def test_empty_path_searches_nothing(tmp_path: Path) -> None:
    _exe(tmp_path / "bin" / "tool")
    res = locate("tool", LookupContext(path=""), known=(), env={"PATH": str(tmp_path / "bin")})
    assert res.kind == "missing"


def test_ambient_path_from_env_mapping(tmp_path: Path) -> None:
    tool = _exe(tmp_path / "bin" / "tool")
    env = {"PATH": str(tmp_path / "bin")}
    for ctx in (LookupContext(), LookupContext(path=PathSource.AMBIENT)):
        assert locate("tool", ctx, known=(), env=env).path == str(tool)


def test_known_dir_after_path_miss(tmp_path: Path) -> None:
    home = _home(tmp_path)
    found = _exe(home / ".cargo" / "bin" / "tool")
    res = locate(
        "tool",
        LookupContext(path=str(tmp_path / "empty")),
        known=("~/.local/bin", "~/.cargo/bin"),
        env={},
        user_home=lambda: home,
    )
    assert res.kind == "known_path"
    assert res.path == str(found)
    assert [(c.source, c.present) for c in res.candidates] == [
        ("PATH", False),
        ("known_dir:~/.local/bin", False),
        ("known_dir:~/.cargo/bin", True),
    ]


def test_known_dir_with_unset_variable_is_skipped(tmp_path: Path) -> None:
    res = locate("tool", LookupContext(path=""), known=("%APPDATA%/npm",), env={})
    assert [c.source for c in res.candidates] == ["PATH"]


def test_extra_dirs_after_known(tmp_path: Path) -> None:
    vendored = known_dirs.vendored_bin(tmp_path / "home")
    tool = _exe(vendored / "helper")
    res = locate("helper", LookupContext(path=""), known=(), extra_dirs=(vendored,), env={})
    assert res.kind == "known_path" and res.path == str(tool)


def test_windows_pathext_from_context_only(tmp_path: Path) -> None:
    tool = _exe(tmp_path / "bin" / "tool.CMD")
    ctx = LookupContext(path=str(tmp_path / "bin"), pathext=".EXE;.CMD")
    res = locate("tool", ctx, known=(), env={"PATHEXT": ".NOPE"}, family="win32")
    assert res.path == str(tool)


def test_lookup_does_not_mutate_env(tmp_path: Path) -> None:
    before = dict(os.environ)
    locate("definitely-not-a-command-xyz")
    assert dict(os.environ) == before


def test_inspect_reads_shebang(tmp_path: Path) -> None:
    tool = _exe(tmp_path / "tool", "#!/usr/bin/env python3\nprint(1)\n")
    info = inspect(locate(str(tool), known=(), env={}))
    assert info.interpreter.state is CheckState.PRESENT
    assert info.interpreter.value == "/usr/bin/env python3"
    assert info.size.value == tool.stat().st_size


def test_inspect_missing() -> None:
    info = inspect(Resolution("missing"))
    assert info.interpreter.state is CheckState.ABSENT


def test_probe_is_not_truthy() -> None:
    p = probe(Resolution("missing"), effort=Effort.LOCAL)
    assert isinstance(p, Probe)
    with pytest.raises(TypeError):
        bool(p)


def test_local_probe_never_connects_or_spawns(tmp_path: Path) -> None:
    calls: list[str] = []

    def connector(host: str, port: int, deadline: float) -> bool:
        calls.append("connect")
        return True

    def runner(argv: Sequence[str], deadline: float) -> bool:
        calls.append("run")
        return True

    res = locate(str(_exe(tmp_path / "tool")), known=(), env={})
    p = probe(
        res, effort=Effort.LOCAL, endpoint=("127.0.0.1", 9), connector=connector, runner=runner
    )
    assert calls == []
    assert p.running.state is CheckState.NOT_CHECKED
    assert p.answering.state is CheckState.NOT_CHECKED
    assert p.endpoint.value == "127.0.0.1:9"


def test_network_probe_connects_but_never_spawns(tmp_path: Path) -> None:
    seen: list[tuple[str, int, float]] = []
    spawned: list[Sequence[str]] = []
    res = locate(str(_exe(tmp_path / "tool")), known=(), env={})
    p = probe(
        res,
        effort=Effort.NETWORK,
        endpoint=("127.0.0.1", 8080),
        connector=lambda h, port, d: seen.append((h, port, d)) is None,
        runner=lambda argv, d: spawned.append(argv) is None,
    )
    assert seen == [("127.0.0.1", 8080, 3.0)]
    assert spawned == []
    assert p.answering.value is True
    assert p.running.state is CheckState.NOT_CHECKED


def test_diagnostic_probe_may_spawn(tmp_path: Path) -> None:
    spawned: list[list[str]] = []
    tool = _exe(tmp_path / "tool")
    res = locate(str(tool), known=(), env={})
    p = probe(
        res,
        effort=Effort.DIAGNOSTIC,
        deadline=1.5,
        runner=lambda argv, d: spawned.append([*argv, str(d)]) is None,
    )
    assert spawned == [[str(tool), "--version", "1.5"]]
    assert p.running.value is True


def test_known_dir_tables_per_os() -> None:
    assert known_dirs.homebrew_bins("linux") == ()
    assert "/opt/homebrew/bin" in known_dirs.homebrew_bins("darwin")
    assert known_dirs.snap_bins("darwin") == ()
    assert known_dirs.mac_protected_dirs("linux") == ()
    assert "~/Documents" in known_dirs.mac_protected_dirs("darwin")
    assert all(not d.startswith("~") for d in known_dirs.command_dirs("win32"))
    assert "/snap/bin" in known_dirs.all_literals()
