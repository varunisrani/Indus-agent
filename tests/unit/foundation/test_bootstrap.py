"""foundation.bootstrap: the first call of every entry point."""

from __future__ import annotations

import io
import logging
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from indus.foundation import bootstrap, layout
from indus.foundation import log as logmod


@pytest.fixture(autouse=True)
def clean_process_state() -> Iterator[None]:
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    factory = logging.getLogRecordFactory()
    path = list(sys.path)
    layout.reset_memo()
    yield
    logmod.shutdown()
    root.handlers[:] = handlers
    root.setLevel(level)
    logging.setLogRecordFactory(factory)
    sys.path[:] = path
    layout.reset_memo()


def _root_with_work(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    work = root / "profiles" / "work"
    work.mkdir(parents=True)
    (work / "config.yaml").write_text("{}\n", encoding="utf-8")
    return root


def test_profile_flag_gives_stripped_argv_and_a_work_scope(tmp_path: Path) -> None:
    root = _root_with_work(tmp_path)
    env = {"INDUS_HOME": str(root)}
    scope, argv = bootstrap.run(["-p", "work", "chat", "-q", "hi"], env)
    work = root / "profiles" / "work"
    assert argv == ["chat", "-q", "hi"]
    assert scope.profile_id == "work"
    assert Path(scope.home) == work
    assert Path(scope.root) == root
    assert env["INDUS_HOME"] == str(work)
    assert (work / "PERSONA.md").exists()
    assert (work / "cache" / "scratch").is_dir()

    logging.getLogger("indus.cli.boot").info("hello from work")
    logmod.flush()
    assert "hello from work" in (work / "logs" / "agent.log").read_text(encoding="utf-8")
    assert not (root / "logs" / "agent.log").exists()


def test_scratch_vars_exported_unless_user_set(tmp_path: Path) -> None:
    root = tmp_path / "root"
    env = {"INDUS_HOME": str(root), "TMP": "/mine"}
    scope, _ = bootstrap.run(["chat"], env)
    scratch = str(Path(scope.home) / "cache" / "scratch")
    assert env["TMPDIR"] == scratch
    assert env["TEMP"] == scratch
    assert env["TMP"] == "/mine"
    assert scope.env["TMPDIR"] == scratch


def test_cwd_shadowing_package_is_removed_from_sys_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    (project / "indus").mkdir(parents=True)
    (project / "indus" / "__init__.py").write_text("")
    monkeypatch.chdir(project)
    sys.path[:0] = ["", ".", str(project)]
    removed = bootstrap.harden_sys_path()
    assert set(removed) == {"", ".", str(project)}
    assert "" not in sys.path and "." not in sys.path
    assert str(project) not in sys.path


def test_cwd_without_shadowing_package_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    sys.path.insert(0, "")
    assert bootstrap.harden_sys_path() == []
    assert sys.path[0] == ""


def test_non_utf8_stdio_is_reconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    legacy = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    already = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    monkeypatch.setattr(sys, "stdout", legacy)
    monkeypatch.setattr(sys, "stderr", already)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # no reconfigure: skipped
    assert bootstrap.ensure_utf8_stdio() == ["stdout"]
    assert legacy.encoding == "utf-8"
    assert legacy.errors == "replace"
    legacy.write("\udcff")  # a lone surrogate no longer raises


def test_stdio_mode_bootstrap_keeps_stdout_clean(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    env = {"INDUS_HOME": str(tmp_path / "root")}
    bootstrap.run(["serve"], env, mode="stdio")
    logging.getLogger("indus.mcp_serve.boot").warning("diagnostic")
    logmod.flush()
    out, err = capfd.readouterr()
    assert out == ""
    assert "diagnostic" in err
