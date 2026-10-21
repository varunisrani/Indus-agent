"""P24-14: ``indus project`` without a terminal; the CLI list equals the RPC list."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from indus.foundation.scope import Scope
from indus.protocol.projects import ProjectsListParams
from indus.service import projects_rpc
from tests.unit.cli.commands.cmdrun import Ran, run_cli


def _run(capsys: pytest.CaptureFixture[str], scope: Scope, *args: str) -> Ran:
    return run_cli(capsys, scope, "project", *args)


@dataclass
class _Ctx:
    scope: Scope


def test_bare_command_says_there_are_none(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    ran = _run(capsys, scope)
    assert ran.code == 0 and "No projects yet" in ran.out
    assert _run(capsys, scope, "list").out == ran.out
    for verb in ("create", "add-path", "remove-path", "archive"):
        assert _run(capsys, scope, verb).code == 2, verb
    assert "No active project." in _run(capsys, scope, "use").out


def test_create_use_paths_archive(
    capsys: pytest.CaptureFixture[str], scope: Scope, tmp_path: Path
) -> None:
    app = tmp_path / "app"
    docs = tmp_path / "docs"
    app.mkdir()
    docs.mkdir()
    made = _run(capsys, scope, "create", "My App", str(app), "--board", "ops", "--use")
    assert made.code == 0 and "Created project my-app" in made.out and str(app) in made.out
    listed = _run(capsys, scope, "list")
    assert "* my-app" in listed.out and "[1 folder(s)]" in listed.out
    added = _run(capsys, scope, "add-path", "my-app", str(docs), "--kind", "repo")
    assert added.code == 0 and str(docs) in added.out
    assert "[2 folder(s)]" in _run(capsys, scope, "list").out
    assert _run(capsys, scope, "remove-path", "my-app", str(tmp_path / "nope")).code == 1
    assert _run(capsys, scope, "remove-path", "my-app", str(docs)).code == 0
    dup = _run(capsys, scope, "create", "Other", str(app))
    assert dup.code == 1 and "already the main folder" in dup.err
    assert _run(capsys, scope, "use", "ghost").code == 1
    assert _run(capsys, scope, "archive", "my-app").code == 0
    assert "No projects yet" in _run(capsys, scope, "list").out
    assert "(archived)" in _run(capsys, scope, "list", "--all").out
    assert "restored" in _run(capsys, scope, "archive", "my-app", "--restore").out


def test_rpc_list_matches_cli_list(
    capsys: pytest.CaptureFixture[str], scope: Scope, tmp_path: Path
) -> None:
    for name in ("one", "two"):
        (tmp_path / name).mkdir()
        assert _run(capsys, scope, "create", name, str(tmp_path / name)).code == 0
    _run(capsys, scope, "use", "two")
    cli = json.loads(_run(capsys, scope, "list", "--json").out)
    rpc = asyncio.run(projects_rpc.projects_list(_Ctx(scope), ProjectsListParams())).to_wire()
    assert cli == rpc
    assert [p["slug"] for p in cli["projects"]] == ["one", "two"]
    assert cli["active_id"] == cli["projects"][1]["id"]
