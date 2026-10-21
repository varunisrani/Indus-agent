"""P24-13: ``indus kanban`` without a terminal (R04.19) and the review flow from the CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from indus.foundation.scope import Scope
from indus.state.board import BoardStore, current_board
from tests.unit.cli.commands.cmdrun import Ran, run_cli


def _run(capsys: pytest.CaptureFixture[str], scope: Scope, *args: str) -> Ran:
    return run_cli(capsys, scope, "kanban", *args)


def _created(ran: Ran) -> str:
    assert ran.code == 0, ran.err
    return json.loads(ran.out)["id"]


def test_bare_command_lists_the_empty_board(
    capsys: pytest.CaptureFixture[str], scope: Scope
) -> None:
    ran = _run(capsys, scope)
    assert ran.code == 0 and "Board: default" in ran.out and "(no tasks)" in ran.out
    assert _run(capsys, scope, "list").out == ran.out
    assert json.loads(_run(capsys, scope, "list", "--json").out) == []


def test_missing_arguments_are_usage_errors(
    capsys: pytest.CaptureFixture[str], scope: Scope
) -> None:
    for verb in ("create", "show", "move", "comment", "attach", "assign", "review", "archive"):
        assert _run(capsys, scope, verb).code == 2, verb


def test_create_list_show_move_round_trip(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    parent = _created(_run(capsys, scope, "create", "plan it", "--assignee", "work", "--json"))
    child = _created(
        _run(capsys, scope, "create", "do it", "--parent", parent, "--priority", "5", "--json")
    )
    listed = _run(capsys, scope, "list")
    assert parent in listed.out and child in listed.out
    assert listed.out.index(child) < listed.out.index(parent)  # priority first
    shown = json.loads(_run(capsys, scope, "show", child, "--json").out)
    assert shown["task"]["status"] == "todo" and shown["parents"] == [parent]
    text = _run(capsys, scope, "show", child)
    assert "parents:   " + parent in text.out
    done = _run(capsys, scope, "move", parent, "done", "--reason", "planned")
    assert done.code == 0 and f"{parent} is now done" in done.out
    assert json.loads(_run(capsys, scope, "show", child, "--json").out)["task"]["status"] == "ready"
    blocked = _run(capsys, scope, "move", child, "blocked", "--reason", "waiting on legal")
    assert "blocked" in blocked.out
    assert "ready" in _run(capsys, scope, "move", child, "ready").out
    refused = _run(capsys, scope, "move", child, "todo")
    assert refused.code == 1 and "only blocked or review" in refused.err
    assert _run(capsys, scope, "show", "t_missing").code == 1
    assert _run(capsys, scope, "move", child, "running").code == 2


def test_comment_attach_assign_archive(
    capsys: pytest.CaptureFixture[str], scope: Scope, tmp_path: Path
) -> None:
    task = _created(_run(capsys, scope, "create", "t", "--json"))
    assert _run(capsys, scope, "comment", task, "first note").code == 0
    note = tmp_path / "notes.txt"
    note.write_text("hello", encoding="utf-8")
    attached = _run(capsys, scope, "attach", task, str(note))
    assert attached.code == 0 and "notes.txt (5 bytes)" in attached.out
    assert _run(capsys, scope, "attach", task, str(tmp_path / "nope")).code == 1
    assert "assigned to work" in _run(capsys, scope, "assign", task, "work").out
    assert "assigned to nobody" in _run(capsys, scope, "assign", task, "none").out
    shown = json.loads(_run(capsys, scope, "show", task, "--json").out)
    assert shown["comments"][0]["body"] == "first note"
    assert shown["attachments"][0]["size"] == 5
    assert _run(capsys, scope, "archive", task).code == 0
    assert task not in _run(capsys, scope, "list").out
    assert task in _run(capsys, scope, "list", "--archived").out


def test_review_approve_and_changes(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    with BoardStore(scope) as store:
        one = store.create("one", assignee="dev")
        two = store.create("two", assignee="dev")
        store.request_review(one.id, summary="ready", reviewer="lead", force=True)
        store.request_review(two.id, summary="ready", reviewer="lead", force=True)
    assert _run(capsys, scope, "review", one.id).code == 2
    assert _run(capsys, scope, "review", one.id, "--approve", "--changes", "x").code == 2
    approved = _run(capsys, scope, "review", one.id, "--approve")
    assert approved.code == 0 and "approved: done" in approved.out
    back = _run(capsys, scope, "review", two.id, "--changes", "rename the flag")
    assert back.code == 0 and "sent back to dev (ready)" in back.out
    again = _run(capsys, scope, "review", two.id, "--approve")
    assert again.code == 1 and "not in review" in again.err


def test_boards_and_the_board_option(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    listed = _run(capsys, scope, "board")
    assert listed.code == 0 and "* default" in listed.out
    switched = _run(capsys, scope, "board", "ops")
    assert switched.code == 0 and current_board(scope.root) == "ops"
    task = _created(_run(capsys, scope, "create", "on ops", "--json"))
    assert task in _run(capsys, scope, "list").out
    assert task not in _run(capsys, scope, "--board", "default", "list").out
    assert _run(capsys, scope, "--board", "Bad Name!", "list").code == 2
    assert _run(capsys, scope, "board", "../x").code == 1


def test_dispatch_dry_run_and_json(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    empty = _run(capsys, scope, "dispatch")
    assert empty.code == 0 and "Nothing to start." in empty.out
    task = _created(_run(capsys, scope, "create", "go", "--assignee", "work", "--json"))
    _created(_run(capsys, scope, "create", "nobody", "--json"))
    dry = _run(capsys, scope, "dispatch", "--dry-run")
    assert f"Would start {task} for work" in dry.out and "Unassigned:" in dry.out
    report = json.loads(_run(capsys, scope, "dispatch", "--dry-run", "--json").out)
    assert report["spawned"] == [[task, "work"]]
    with BoardStore(scope) as store:
        assert store.require(task).status == "ready"  # a dry run claims nothing
