"""Learning timeline and map data: nodes, delete, edit and the CLI (P09-23)."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from indus.cli.commands import journey as journey_cmd
from indus.cli.slash.learning_cmds import journey_cmd as slash_journey
from indus.foundation.scope import Scope
from indus.learning.curator.archive import list_archived
from indus.learning.journey import (
    JourneyError,
    delete_node,
    edit_node,
    memory_id,
    node_text,
    nodes,
)
from indus.learning.memory.store import MemoryStore, Op, Target
from tests.fakes.clock import FakeClock
from tests.unit.cli.commands.cmdrun import run_cli
from tests.unit.cli.slash._fakes import make_ctx
from tests.unit.learning.curator._helpers_f import (
    add_skill,
    clean_registries,  # noqa: F401  (autouse fixture)
    mark_bundled,
    skills_root,
)
from tests.unit.learning.skills.helpers import skill_md


def remember(scope: Scope, target: Target, *texts: str) -> None:
    store = MemoryStore.for_scope(scope)
    result = store.apply(target, [Op("add", content=t) for t in texts])
    assert result.ok, result.message


@pytest.fixture
def home(scope: Scope) -> Scope:
    add_skill(scope, "review-made", origin="agent", created_days=3)
    add_skill(scope, "taught", origin="learn", created_days=2)
    add_skill(scope, "hand-used", origin=None, created_days=30, active_days=1, use_count=4)
    add_skill(scope, "hand-unused", origin=None, created_days=30)
    add_skill(scope, "never-recorded", record=False)
    add_skill(scope, "shipped", origin=None, use_count=9, created_days=1, active_days=1)
    mark_bundled(scope, "shipped")
    remember(scope, "memory", "The repo uses uv.", "Tests run with pytest -q.")
    remember(scope, "user", "Prefers short answers.")
    return scope


def test_node_selection(home: Scope) -> None:
    found = nodes(home)
    skills = {n.id for n in found if n.kind == "skill"}
    assert skills == {"review-made", "taught", "hand-used"}
    memories = [n for n in found if n.kind == "memory"]
    assert {(n.store, n.label) for n in memories} == {
        ("memory", "The repo uses uv."),
        ("memory", "Tests run with pytest -q."),
        ("user", "Prefers short answers."),
    }
    by_id = {n.id: n for n in found}
    assert by_id["hand-used"].use_count == 4 and by_id["review-made"].origin == "agent"
    stamps = [n.timestamp for n in found if n.timestamp is not None]
    assert stamps == sorted(stamps, reverse=True)  # newest first


def test_memory_id_stable(home: Scope) -> None:
    first = [n.id for n in nodes(home) if n.kind == "memory"]
    again = [n.id for n in nodes(home) if n.kind == "memory"]
    assert first == again
    assert memory_id("memory", 0, "The repo uses uv.") in first
    assert all(len(i.split(":")[3]) == 8 for i in first)


def test_delete_memory_entry(home: Scope) -> None:
    target = memory_id("memory", 0, "The repo uses uv.")
    message = delete_node(home, target)
    assert "removed the memory entry" in message
    store = MemoryStore.for_scope(home)
    assert store.entries("memory") == ["Tests run with pytest -q."]
    assert store.entries("user") == ["Prefers short answers."]
    with pytest.raises(JourneyError, match="no longer matches"):
        delete_node(home, target)


def test_memory_id_survives_a_shift(home: Scope) -> None:
    second = memory_id("memory", 1, "Tests run with pytest -q.")
    delete_node(home, memory_id("memory", 0, "The repo uses uv."))
    assert node_text(home, second) == ("memory", "Tests run with pytest -q.")


def test_bad_ids(home: Scope) -> None:
    for bad in ("memory:x:0:abcd1234", "memory:memory:zero:abcd1234", "memory:memory:0"):
        with pytest.raises(JourneyError):
            delete_node(home, bad)
    with pytest.raises(JourneyError, match="no skill named"):
        delete_node(home, "ghost")
    with pytest.raises(JourneyError, match="not something this profile learned"):
        delete_node(home, "shipped")


def test_delete_skill_archives(home: Scope) -> None:
    message = delete_node(home, "taught")
    assert "curator restore taught" in message
    assert not (skills_root(home) / "taught").exists()
    assert [a.name for a in list_archived(home)] == ["taught"]
    assert "taught" not in {n.id for n in nodes(home)}


def test_delete_pinned_refused(scope: Scope) -> None:
    add_skill(scope, "keeper", pinned=True)
    with pytest.raises(JourneyError, match="pinned"):
        delete_node(scope, "keeper")


def _writer(text: str, *, ok: bool = True) -> Callable[[Path], bool]:
    def edit(path: Path) -> bool:
        path.write_text(text, encoding="utf-8")
        return ok

    return edit


def test_edit_invalid_restores(home: Scope) -> None:
    path = skills_root(home) / "taught" / "SKILL.md"
    original = path.read_bytes()
    with pytest.raises(JourneyError, match="would not load"):
        edit_node(home, "taught", _writer("---\nname: [broken\n---\nbody\n"))
    assert path.read_bytes() == original
    with pytest.raises(JourneyError, match="does not match"):
        edit_node(home, "taught", _writer(skill_md("renamed")))
    assert path.read_bytes() == original
    with pytest.raises(JourneyError, match="did not finish"):
        edit_node(home, "taught", _writer("half written", ok=False))
    assert path.read_bytes() == original


def test_edit_valid_saved(home: Scope) -> None:
    new = skill_md("taught", "A better description", body="Better steps.\n")
    assert edit_node(home, "taught", _writer(new)) == "updated taught"
    assert (skills_root(home) / "taught" / "SKILL.md").read_text() == new
    assert edit_node(home, "taught", _writer(new)) == "no changes"


def test_edit_memory(home: Scope) -> None:
    target = memory_id("user", 0, "Prefers short answers.")
    message = edit_node(home, target, _writer("Prefers short answers with examples.\n"))
    assert "updated the memory entry" in message
    assert MemoryStore.for_scope(home).entries("user") == ["Prefers short answers with examples."]
    fresh = memory_id("user", 0, "Prefers short answers with examples.")
    with pytest.raises(JourneyError, match="empty entry"):
        edit_node(home, fresh, _writer("   \n"))
    assert not list((home.home / "memories").glob(".journey-edit-*"))


# -- the CLI ------------------------------------------------------------------------------------


def test_cli_json_schema(capsys: pytest.CaptureFixture[str], home: Scope) -> None:
    ran = run_cli(capsys, home, "journey", "list", "--json")
    assert ran.code == 0
    data = json.loads(ran.out)
    assert set(data) == {"nodes", "counts"}
    assert data["counts"] == {"skills": 3, "memories": 3}
    for node in data["nodes"]:
        assert set(node) == {
            "id",
            "kind",
            "label",
            "category",
            "timestamp",
            "when",
            "use_count",
            "state",
            "origin",
            "pinned",
            "store",
        }
    assert json.loads(run_cli(capsys, home, "journey", "--json").out) == data


def test_cli_timeline_and_alias(capsys: pytest.CaptureFixture[str], home: Scope) -> None:
    ran = run_cli(capsys, home, "journey", "--no-color", "--width", "60")
    assert ran.code == 0
    lines = ran.out.splitlines()
    assert lines[0].startswith("Journey: 3 skill(s), 3 memory")
    assert any("review-made" in line for line in lines)
    assert "\x1b[" not in ran.out
    alias = run_cli(capsys, home, "learning", "--no-color", "--width", "60")
    assert alias.out == ran.out


def test_cli_empty(capsys: pytest.CaptureFixture[str], scope: Scope) -> None:
    assert "Nothing learned yet" in run_cli(capsys, scope, "journey").out


def test_cli_animate(
    capsys: pytest.CaptureFixture[str], home: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    waits: list[float] = []
    monkeypatch.setattr(journey_cmd, "SLEEP", waits.append)
    ran = run_cli(capsys, home, "journey", "--animate", "--fps", "4", "--no-color")
    assert ran.code == 0
    frames = [line for line in ran.out.splitlines() if line.startswith("-- ")]
    assert frames[0] == "-- 1/6 --" and frames[-1] == "-- 6/6 --"
    assert waits == [0.25] * (len(frames) - 1)


def test_cli_delete(capsys: pytest.CaptureFixture[str], home: Scope) -> None:
    assert run_cli(capsys, home, "journey", "delete", "taught").code == 2  # no terminal, no -y
    ran = run_cli(capsys, home, "journey", "delete", "taught", "-y")
    assert ran.code == 0 and "archived taught" in ran.out
    missing = run_cli(capsys, home, "journey", "delete", "ghost", "-y")
    assert missing.code == 1 and "no skill named" in missing.err


def test_cli_edit_with_fake_editor(
    capsys: pytest.CaptureFixture[str], scope: Scope, tmp_path: Path, fake_clock: FakeClock
) -> None:
    add_skill(scope, "taught", origin="learn")
    script = tmp_path / "editor.py"
    script.write_text(
        "import sys\nopen(sys.argv[1], 'w').write('---\\nname: taught\\n---\\n')\n",
        encoding="utf-8",
    )
    path = skills_root(scope) / "taught" / "SKILL.md"
    original = path.read_bytes()
    editing = scope.with_env(scope.env.with_overlay({"EDITOR": f"{sys.executable} {script}"}))
    ran = run_cli(capsys, editing, "journey", "edit", "taught")
    assert ran.code == 1 and "would not load" in ran.err
    assert path.read_bytes() == original


def test_slash_journey(home: Scope) -> None:
    ctx = make_ctx(home)
    slash_journey(ctx, "list")
    assert any(line.startswith("review-made") for line in ctx.lines)
    slash_journey(ctx, "delete taught")
    assert "archived taught" in ctx.lines[-1]
    slash_journey(ctx, "edit taught")
    assert "indus journey edit taught" in ctx.lines[-1]
    slash_journey(ctx, "frobnicate")
    assert ctx.lines[-1].startswith("Usage: /journey")
