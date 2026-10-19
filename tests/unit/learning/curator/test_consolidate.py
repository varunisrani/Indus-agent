"""Curator phase 2: the consolidation pass with a scripted model (P09-19, R07.8)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from typing import Any

from indus.foundation.scope import Scope
from indus.learning import curator
from indus.learning.curator import consolidate
from indus.learning.curator.backup import list_backups
from indus.learning.curator.consolidate import (
    AuxStep,
    RecordedCall,
    classify_removed,
    run_consolidation,
)
from indus.learning.curator.lifecycle import run_curator
from indus.learning.pending import current_origin
from indus.providers.types import Message, NormalizedResponse, ToolCall
from tests.fakes.skill_refs import FakeReferenceSource
from tests.unit.learning.curator._helpers_f import (
    add_skill,
    clean_registries,  # noqa: F401  (autouse fixture)
    ledger_rows,
    skills_root,
)
from tests.unit.learning.skills.helpers import skill_md


def call(tool: str, n: int, **args: Any) -> NormalizedResponse:
    return NormalizedResponse(
        content=None,
        tool_calls=(ToolCall(f"c{n}", tool, json.dumps(args)),),
        finish_reason="tool_calls",
    )


def final(text: str) -> NormalizedResponse:
    return NormalizedResponse(content=text)


class ScriptedStep:
    """Plays replies in order; remembers what it was sent and the write origin at each call."""

    def __init__(self, *replies: NormalizedResponse | Exception) -> None:
        self.replies = list(replies)
        self.sent: list[list[Message]] = []
        self.tool_names: list[list[str]] = []
        self.origins: list[str] = []
        self.model = "fake-model"
        self.provider = "fake"

    async def __call__(
        self, messages: Sequence[Message], tools: Sequence[Mapping[str, Any]]
    ) -> NormalizedResponse:
        self.sent.append([dict(m) for m in messages])
        self.tool_names.append(sorted(t["function"]["name"] for t in tools))
        self.origins.append(current_origin())
        reply = self.replies.pop(0) if self.replies else final("done")
        if isinstance(reply, Exception):
            raise reply
        return reply


def tool_results(step: ScriptedStep) -> list[str]:
    return [m["content"] for m in step.sent[-1] if m["role"] == "tool"]


def run(scope: Scope, step: ScriptedStep | None, **kwargs: Any) -> consolidate.ConsolidationResult:
    kwargs.setdefault("registry_factory", consolidate.curator_registry)
    return asyncio.run(run_consolidation(scope, step, **kwargs))


MERGED = skill_md(
    "git-history",
    "Rewrite and merge git history safely",
    body="## Rebase\nRules.\n\n## Merge (was git-merge-notes)\nMore rules.\n",
)


def test_merge_archives_and_maps(scope: Scope) -> None:
    add_skill(scope, "git-history", created_days=20)
    add_skill(scope, "git-merge-notes", created_days=20)
    source = FakeReferenceSource(["git-merge-notes", "other"])
    curator.register_reference_source(source)
    step = ScriptedStep(
        call("skills_list", 1),
        call("skill_view", 2, name="git-history"),
        call("skill_manage", 3, action="edit", name="git-history", content=MERGED),
        call(
            "skill_manage", 4, action="delete", name="git-merge-notes", absorbed_into="git-history"
        ),
        final("git-merge-notes -> git-history"),
    )
    result = run(scope, step)
    assert result.ran and result.error is None and result.writes_available
    assert result.consolidated == [
        {"name": "git-merge-notes", "into": "git-history", "source": "declared at delete"}
    ]
    assert result.renames == {"git-merge-notes": "git-history"}
    assert (skills_root(scope) / ".archive" / "git-merge-notes" / "SKILL.md").is_file()
    assert "  git-merge-notes → git-history" in result.rename_lines()
    assert source.names == ["git-history", "other"]
    assert source.rewrites == [({"git-merge-notes": "git-history"}, [])]
    assert result.tool_calls == {"skills_list": 1, "skill_view": 1, "skill_manage": 2}
    assert step.tool_names[0] == ["skill_manage", "skill_view", "skills_list"]
    assert set(step.origins) == {"background_review"}
    assert result.model == "fake-model" and result.final == "git-merge-notes -> git-history"
    # the real skill_manage wrote the ledger, as the curator
    assert [(r["action"], r["actor"], r["skill"]) for r in ledger_rows(scope)] == [
        ("edit", "curator", "git-history"),
        ("archive", "curator", "git-merge-notes"),
    ]


def test_other_tools_refused(scope: Scope) -> None:
    add_skill(scope, "alpha")
    step = ScriptedStep(call("terminal", 1, command="rm -rf ~"), final("nothing to do"))
    result = run(scope, step)
    [answer] = tool_results(step)
    assert "Unknown tool: terminal" in answer
    assert result.tool_calls == {"terminal": 1}
    assert (skills_root(scope) / "alpha").is_dir()


def test_only_managed_unpinned_skills_visible(scope: Scope) -> None:
    add_skill(scope, "mine", origin="learn")
    add_skill(scope, "pinned-one", pinned=True)
    add_skill(scope, "managed")
    step = ScriptedStep(
        call("skills_list", 1),
        call("skill_view", 2, name="mine"),
        call("skill_view", 3, name="pinned-one"),
        final("ok"),
    )
    run(scope, step)
    listing, mine, pinned = tool_results(step)
    assert [row["name"] for row in json.loads(listing)["skills"]] == ["managed"]
    assert "not one of the skills" in mine and "not one of the skills" in pinned
    prompt = step.sent[0][1]["content"]
    assert "- managed" in prompt and "mine" not in prompt and "pinned-one" not in prompt


def test_no_candidates_no_call(scope: Scope) -> None:
    add_skill(scope, "mine", origin="learn")
    step = ScriptedStep()
    result = run(scope, step)
    assert result.ran is False and step.sent == []
    assert result.summary == "skipped (no skills to consolidate)"


def test_model_error_recorded(scope: Scope) -> None:
    add_skill(scope, "alpha")
    result = run(scope, ScriptedStep(RuntimeError("model down")))
    assert result.ran and result.error == "RuntimeError: model down"
    assert result.summary == "error: RuntimeError: model down"


def test_dry_run_is_read_only(scope: Scope) -> None:
    add_skill(scope, "alpha")
    step = ScriptedStep(call("skill_manage", 1, action="delete", name="alpha"), final("would"))
    result = run(scope, step, dry_run=True)
    assert result.writes_available is False
    assert step.tool_names[0] == ["skill_view", "skills_list"]
    assert "Unknown tool: skill_manage" in tool_results(step)[0]
    assert step.sent[0][1]["content"].startswith("PREVIEW ONLY")
    assert (skills_root(scope) / "alpha").is_dir()


def test_iteration_cap(scope: Scope) -> None:
    add_skill(scope, "alpha")
    step = ScriptedStep(*[call("skills_list", n) for n in range(5)])
    result = run(scope, step, max_iterations=3)
    assert len(step.sent) == 3 and result.error is not None and "3 rounds" in result.error


def test_classification_audit_and_prune() -> None:
    calls = [
        RecordedCall(
            "skill_manage",
            {"action": "edit", "name": "umbrella", "content": "now covers old_helper too"},
        ),
        RecordedCall("skill_manage", {"action": "delete", "name": "dropped", "absorbed_into": ""}),
        RecordedCall(
            "skill_manage",
            {"action": "delete", "name": "ghosted", "absorbed_into": "does-not-exist"},
        ),
    ]
    merged, pruned = classify_removed(
        ["old-helper", "dropped", "ghosted", "latest"], {"umbrella"}, calls
    )
    assert merged == [{"name": "old-helper", "into": "umbrella", "source": "tool-call audit"}]
    assert [p["name"] for p in pruned] == ["dropped", "ghosted", "latest"]


def test_run_curator_snapshots_before_pass(scope: Scope) -> None:
    add_skill(scope, "git-history", created_days=20)
    add_skill(scope, "git-merge-notes", created_days=20)
    step = ScriptedStep(
        call(
            "skill_manage", 1, action="delete", name="git-merge-notes", absorbed_into="git-history"
        ),
        final("folded"),
    )
    lines: list[str] = []
    outcome = run_curator(
        scope, consolidate=True, step_factory=lambda _s: step, on_line=lines.append
    )
    assert outcome.backup is not None
    [snap] = list_backups(scope)
    assert snap.id == outcome.backup and snap.reason == "before a curator run"
    assert "  git-merge-notes → git-history" in lines
    report = (scope.home / "logs" / "curator").glob("*/REPORT.md").__next__().read_text()
    assert "`git-merge-notes` → `git-history`" in report and "fake-model via fake" in report


def test_aux_step_passes_tools_and_timeout() -> None:
    class Route:
        model = "m"
        provider = "p"

    class Result:
        route = Route()
        response = final("hi")

    class Aux:
        def __init__(self) -> None:
            self.seen: dict[str, Any] = {}

        def task_config(self, task: str) -> dict[str, Any]:
            return {}

        async def call(self, task: str, messages: Any, **options: Any) -> Result:
            self.seen = {"task": task, **options}
            return Result()

    aux = Aux()
    step = AuxStep(aux)
    reply = asyncio.run(step([{"role": "user", "content": "x"}], [{"type": "function"}]))
    assert reply.content == "hi" and (step.model, step.provider) == ("m", "p")
    assert aux.seen == {"task": "curator", "tools": [{"type": "function"}], "timeout_s": 600.0}
