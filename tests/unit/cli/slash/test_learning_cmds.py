"""P09-24: ``/memory``, ``/skills``, ``/reload-skills`` and ``/bundles`` in the classic CLI."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from indus.cli.slash.dispatch import dispatch
from indus.cli.slash.skill_cmds import skill_commands
from indus.foundation.config import loader
from indus.foundation.scope import Scope
from indus.learning import pending
from indus.learning.memory.store import MemoryStore
from indus.learning.skills.bundles import reload_bundles, save_bundle
from indus.learning.skills.resolver import clear_scan_cache
from indus.learning.skills.view_tools import clear_list_cache
from indus.service.commands.registry import REGISTRY
from tests.unit.cli.slash._fakes import Ctx, FakePort, make_ctx
from tests.unit.learning.skills.helpers import make_skill


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    clear_scan_cache()
    clear_list_cache()
    reload_bundles()
    yield
    clear_scan_cache()


@pytest.fixture
def ctx(scope: Scope) -> Ctx:
    made = make_ctx(scope)
    made.extensions.skill_commands = skill_commands(scope, made.session_id)
    return made


def test_registry_entries() -> None:
    for name in ("memory", "skills", "reload-skills", "bundles"):
        cmd = REGISTRY.resolve(name)
        assert cmd is not None and cmd.owner == "P09"
    skills = REGISTRY.resolve("skills")
    assert skills is not None and "reload" in skills.subcommands


def test_reload_skills_registers_a_new_command_without_touching_the_session(
    scope: Scope, ctx: Ctx
) -> None:
    make_skill(scope.home / "skills", "first", body="First body.\n")
    assert "fresh" not in ctx.extensions.skill_commands
    dispatch("/reload-skills", ctx)
    assert "first" in ctx.extensions.skill_commands
    assert any(line.startswith("New: /first") for line in ctx.lines)
    make_skill(scope.home / "skills", "fresh", body="Fresh body.\n")
    ctx.lines.clear()
    dispatch("/skills reload", ctx)
    assert "New: /fresh" in ctx.lines
    assert "The system prompt is unchanged" in ctx.text
    port = ctx.port
    assert isinstance(port, FakePort) and port.calls == []
    result = dispatch("/fresh tidy the notes", ctx)
    assert result.submit is not None
    assert "Fresh body." in result.submit and "tidy the notes" in result.submit


def test_skill_commands_stack(scope: Scope) -> None:
    make_skill(scope.home / "skills", "alpha", body="Alpha body.\n")
    make_skill(scope.home / "skills", "beta", body="Beta body.\n")
    made = make_ctx(scope)
    made.extensions.skill_commands = skill_commands(scope, made.session_id)
    result = dispatch("/alpha /beta go", made)
    assert result.submit is not None
    assert "Alpha body." in result.submit and "Beta body." in result.submit


def test_bundles(scope: Scope, ctx: Ctx) -> None:
    dispatch("/bundles", ctx)
    assert "No skill bundles yet" in ctx.text
    make_skill(scope.home / "skills", "alpha", body="Alpha body.\n")
    save_bundle(scope.home, "morning", ["alpha", "absent"], description="Start of day")
    ctx.lines.clear()
    dispatch("/bundles", ctx)
    assert "/morning" in ctx.text and "alpha, absent" in ctx.text
    dispatch("/reload-skills", ctx)
    result = dispatch("/morning now", ctx)
    assert result.submit is not None and "Alpha body." in result.submit
    assert "Not loaded: absent." in ctx.text


def test_skills_list_and_view(scope: Scope, ctx: Ctx) -> None:
    dispatch("/skills", ctx)
    assert "No skills yet" in ctx.text
    make_skill(scope.home / "skills", "notes", description="Keep notes tidy.")
    ctx.lines.clear()
    dispatch("/skills list", ctx)
    assert "notes  Keep notes tidy." in ctx.text
    ctx.lines.clear()
    dispatch("/skills view notes", ctx)
    assert "Steps go here." in ctx.text
    ctx.lines.clear()
    dispatch("/skills view nope", ctx)
    assert "no skill named 'nope'" in ctx.text
    ctx.lines.clear()
    dispatch("/skills frobnicate", ctx)
    assert ctx.lines[0].startswith("Usage: /skills")


def test_skills_pending_and_approve(scope: Scope, ctx: Ctx) -> None:
    make_skill(scope.home / "skills", "notes")
    store = pending.PendingStore(scope.home, scope.clock)
    op = {"action": "patch", "name": "notes", "old_string": "Steps", "new_string": "Moves"}
    record = store.stage("skills", {"operations": [op]}, summary="patch notes", origin="learn")
    dispatch("/skills pending", ctx)
    assert record.id in ctx.text
    dispatch(f"/skills approve {record.id}", ctx)
    assert "+Moves go here." in ctx.text
    assert store.list("skills") == []


def test_memory_pending_approve_reject(scope: Scope, ctx: Ctx) -> None:
    store = pending.PendingStore(scope.home, scope.clock)

    def stage(text: str) -> pending.PendingWrite:
        payload = {"target": "memory", "operations": [{"action": "add", "content": text}]}
        return store.stage("memory", payload, summary=f"add {text}", origin="background_review")

    keep, drop = stage("Keep this."), stage("Drop this.")
    dispatch("/memory", ctx)
    assert keep.id in ctx.text and drop.id in ctx.text
    dispatch(f"/memory reject {drop.id}", ctx)
    assert f"Rejected {drop.id}." in ctx.lines
    dispatch("/memory approve", ctx)
    assert "Name the writes to approve" in ctx.lines[-1]
    dispatch("/memory approve all", ctx)
    assert MemoryStore(scope.home).entries("memory") == ["Keep this."]


def test_memory_approval_switch_and_status(scope: Scope, ctx: Ctx) -> None:
    dispatch("/memory approval", ctx)
    assert "Memory write approval is off" in ctx.lines[-1]
    dispatch("/memory approval on", ctx)
    assert loader.load(scope).get("memory.write_approval") is True
    dispatch("/memory status", ctx)
    assert "Write approval: on" in ctx.text
    dispatch("/memory approval maybe", ctx)
    assert ctx.lines[-1].startswith("Usage: /memory")
