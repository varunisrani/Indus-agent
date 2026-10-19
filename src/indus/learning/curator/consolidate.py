"""Curator phase 2: a model folds overlapping managed skills into class-level umbrellas (P09-19).

Off unless ``curator.consolidate`` is on or a run asks for it. The pass is a small tool loop on the
``curator`` task of the auxiliary client (P08-17): each round sends the conversation and the tool
array, runs the tools the model asked for, and appends their results, up to ``ITERATION_CAP``
rounds. The registry it dispatches through holds only ``skills_list``, ``skill_view`` and (on a
live run) ``skill_manage``, so any other tool name is answered "Unknown tool" and nothing runs.
The list and view tools here are narrowed to the skills the curator manages and that are not
pinned: the model never reads, let alone writes, anything else. Writes go through slice D's
``skill_manage`` with the write origin bound to ``background_review``, so its review-fork guards
(read before write, archive instead of delete) apply, and the ledger actor bound to ``curator``.

Afterwards the managed set before and after the pass is compared. Each skill that left is
classified: a ``skill_manage delete`` that declared ``absorbed_into`` an existing skill is a
consolidation; ``absorbed_into: ""`` is a prune; without a declaration, a write to a surviving
skill that mentions the removed name counts as the consolidation evidence; anything else was
pruned. The ``old → new`` map is printed and every registered reference source is asked to
follow it.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from indus.foundation.scope import Scope
from indus.learning.curator import ReferenceRewrite, referenced_skills, rewrite_references
from indus.learning.curator.archive import Ownership, managed_skills
from indus.learning.curator.settings import CONSOLIDATE_TIMEOUT_S, ITERATION_CAP
from indus.learning.pending import bind_origin
from indus.learning.skills import view_tools
from indus.learning.skills.ledger import bind_actor
from indus.providers.types import Message, NormalizedResponse
from indus.tools.calls import ToolCall, ToolContext, ToolResult
from indus.tools.definitions import definitions
from indus.tools.dispatch import dispatch
from indus.tools.registry import ToolRegistry, ToolTraits

__all__ = [
    "CURATOR_TOOLS",
    "DRY_RUN_PREFACE",
    "AuxStep",
    "ConsolidationResult",
    "ModelStep",
    "candidate_list",
    "classify_removed",
    "curator_registry",
    "rename_lines",
    "run_consolidation",
]

log = logging.getLogger(__name__)

TASK = "curator"
CURATOR_TOOLS = ("skills_list", "skill_view", "skill_manage")
MANAGE_MODULE = "indus.learning.skills.manage_tool"
_ARG_PREVIEW = 400
_SUMMARY_CHARS = 240

SYSTEM_TEXT = """\
You look after a library of skills that an assistant wrote for itself while working. You are not \
talking to a person; nobody will answer questions. Work only with the three skill tools you have \
and finish with a short plain-text report."""

CURATOR_PROMPT = """\
Tidy the skill library listed below so it is easier for the assistant to find the right skill.

What a good library looks like:
- Few broad skills, each covering one kind of task, rather than many narrow skills that each \
record one session. A skill is found by its description, so a broad description that names the \
kind of task wins over several near-identical ones.
- SKILL.md holds the rules that always apply; deeper material for one topic goes in a small number \
of files under references/, templates/ or scripts/.
- Rules are written as instructions with a short reason. Drop stories about what happened, issue \
or PR numbers, dates and quoted conversation. Say each lesson once.

How to work:
1. Read the list. Group skills that serve the same kind of task (shared words in the name are a \
good first hint, but judge by what the skills say, not by their usage counts — the counts are new \
and mostly zero).
2. For each group of two or more, decide whether a careful maintainer would write it as one skill \
with sections. If so, pick the best existing skill as the umbrella (or create one), fold the \
others' lasting rules into it with skill_manage, then remove each folded skill with \
skill_manage(action="delete", name=<old>, absorbed_into=<umbrella>). Removal moves the folder to \
the archive; nothing is lost.
3. A skill whose content is obsolete or fully covered elsewhere may be removed with \
absorbed_into="" — but never one marked in_use=yes, and never one that was never used and is \
younger than 30 days.
4. Always open a skill with skill_view right before you change it.

Never:
- touch a skill that is not in the list below (the tools will refuse anyway);
- remove a skill marked in_use=yes, except by folding it into an umbrella (its users are updated \
to the new name automatically);
- delete files outside skill_manage, or try any other tool.

When you are done, reply with a short report: one line per change, written as \
"old-name -> umbrella-name" for folds and "old-name -> (archived)" for removals, then one line on \
anything you left alone on purpose."""

DRY_RUN_PREFACE = """\
PREVIEW ONLY. You can read skills but not change them in this pass. Do everything below up to the \
point of changing a skill, and write the report as the list of changes you would make."""


# -- the model -----------------------------------------------------------------------------------


class ModelStep(Protocol):
    """One model round: the conversation and tool array in, the normalised reply out."""

    model: str
    provider: str

    def __call__(
        self, messages: Sequence[Message], tools: Sequence[Mapping[str, Any]]
    ) -> Awaitable[NormalizedResponse]: ...


class AuxStep:
    """A ``ModelStep`` over the auxiliary client's ``curator`` task (600 s unless configured)."""

    def __init__(self, aux: Any, *, task: str = TASK) -> None:
        self.aux = aux
        self.task = task
        self.model = ""
        self.provider = ""

    def _timeout(self) -> float:
        try:
            configured = self.aux.task_config(self.task).get("timeout_s")
        except Exception:
            configured = None
        if isinstance(configured, bool) or not isinstance(configured, int | float):
            return CONSOLIDATE_TIMEOUT_S
        if configured > 0:
            return float(configured)
        return CONSOLIDATE_TIMEOUT_S

    async def __call__(
        self, messages: Sequence[Message], tools: Sequence[Mapping[str, Any]]
    ) -> NormalizedResponse:
        result = await self.aux.call(
            self.task, list(messages), tools=list(tools), timeout_s=self._timeout()
        )
        self.model = result.route.model
        self.provider = result.route.provider
        return result.response


# -- the tools ------------------------------------------------------------------------------------


def _allowed(scope: Scope) -> dict[str, Ownership]:
    return {o.name: o for o in managed_skills(scope) if o.record.get("pinned") is not True}


def _list_handler(args: Mapping[str, Any], *, scope: Scope | None = None) -> str:
    if scope is None:
        return ToolResult.error("skills_list needs a profile scope").text
    from indus.learning.skills.resolver import load_catalog

    allowed = _allowed(scope)
    rows: list[dict[str, Any]] = []
    for rec in load_catalog(scope).loadable():
        if rec.name in allowed:
            rows.append(
                {"name": rec.name, "description": rec.description, "category": rec.category}
            )
    category = args.get("category")
    if isinstance(category, str) and category.strip():
        rows = [r for r in rows if r["category"] == category.strip()]
    return json.dumps({"skills": rows, "count": len(rows)}, ensure_ascii=False)


async def _view_handler(
    args: Mapping[str, Any], *, scope: Scope | None = None, session_id: str | None = None
) -> str:
    if scope is None:
        return ToolResult.error("skill_view needs a profile scope").text
    name = args.get("name")
    if not isinstance(name, str) or name.strip() not in _allowed(scope):
        return ToolResult.error(
            f"{name!r} is not one of the skills this pass may work on; see skills_list"
        ).text
    return await view_tools.skill_view(args, scope=scope, session_id=session_id)


def _register_manage(registry: ToolRegistry) -> bool:
    import importlib

    try:
        module = importlib.import_module(MANAGE_MODULE)
    except ImportError:
        log.info("skill_manage is not installed; the curator pass can only read")
        return False
    register = getattr(module, "register_tools", None)
    if register is None:
        return False
    scratch = ToolRegistry()
    register(scratch)
    entry = scratch.get("skill_manage")
    if entry is None:
        return False
    registry.register(
        entry.name,
        entry.toolset,
        entry.schema,
        entry.handler,
        is_async=entry.is_async,
        timeout_s=entry.timeout_s,
        traits=entry.traits,
    )
    return True


def curator_registry(*, writes: bool = True) -> tuple[ToolRegistry, bool]:
    """A registry holding only the curator's tools; the flag says whether writes are possible."""
    registry = ToolRegistry()
    read_only = ToolTraits(read_only=True)
    registry.register(
        "skills_list", "skills", view_tools.SKILLS_LIST_SCHEMA, _list_handler, traits=read_only
    )
    registry.register(
        "skill_view",
        "skills",
        view_tools.SKILL_VIEW_SCHEMA,
        _view_handler,
        is_async=True,
        traits=read_only,
    )
    can_write = _register_manage(registry) if writes else False
    return registry, can_write


# -- the candidate list ---------------------------------------------------------------------------


def candidate_list(scope: Scope) -> str:
    """One line per skill the pass may work on; empty text when there is none."""
    allowed = _allowed(scope)
    if not allowed:
        return ""
    in_use = referenced_skills() or set()
    lines = [f"Skills you may work on ({len(allowed)}):"]
    for name, owner in sorted(allowed.items()):
        rec = owner.record
        lines.append(
            f"- {name}  state={rec.get('state', 'active')}  "
            f"in_use={'yes' if name in in_use else 'no'}  "
            f"used={rec.get('use_count', 0)}  viewed={rec.get('view_count', 0)}  "
            f"patched={rec.get('patch_count', 0)}  "
            f"created={rec.get('created_at') or 'unknown'}  "
            f"last_activity={rec.get('last_activity_at') or 'never'}"
        )
    return "\n".join(lines)


# -- classification -------------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordedCall:
    name: str
    args: Mapping[str, Any]


def _manage_ops(call: RecordedCall) -> list[Mapping[str, Any]]:
    """The skill_manage operations one call carried (a batch counts as several)."""
    if call.name != "skill_manage":
        return []
    ops = call.args.get("operations")
    if isinstance(ops, list):
        out: list[Mapping[str, Any]] = []
        for op in cast(list[object], ops):
            if isinstance(op, Mapping):
                merged = dict(cast(Mapping[str, Any], op))
                merged.setdefault("name", call.args.get("name"))
                out.append(merged)
        return out
    return [call.args]


_TEXT_KEYS = ("content", "new_string", "file_content", "file_path")


def _mentions(text: str, name: str) -> bool:
    variants = {name, name.replace("-", "_"), name.replace("_", "-")}
    return any(re.search(rf"(?<![\w-]){re.escape(v)}(?![\w-])", text) for v in variants)


def classify_removed(
    removed: Sequence[str], survivors: set[str], calls: Sequence[RecordedCall]
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """``(consolidated, pruned)`` for every removed skill (see the module notes)."""
    ops = [op for call in calls for op in _manage_ops(call)]
    declared: dict[str, str] = {}
    for op in ops:
        target = op.get("absorbed_into")
        name = op.get("name")
        if op.get("action") == "delete" and isinstance(name, str) and isinstance(target, str):
            declared[name.strip()] = target.strip()
    consolidated: list[dict[str, str]] = []
    pruned: list[dict[str, str]] = []
    for name in removed:
        into = declared.get(name)
        if into:
            if into in survivors:
                consolidated.append({"name": name, "into": into, "source": "declared at delete"})
                continue
        elif into == "":
            pruned.append({"name": name, "source": "declared at delete"})
            continue
        found = None
        for op in ops:
            target = op.get("name")
            if not isinstance(target, str) or target == name or target not in survivors:
                continue
            if any(isinstance(op.get(k), str) and _mentions(op[k], name) for k in _TEXT_KEYS):
                found = target
                break
        if found is not None:
            consolidated.append({"name": name, "into": found, "source": "tool-call audit"})
        else:
            pruned.append({"name": name, "source": "no successor found"})
    return consolidated, pruned


def rename_lines(
    consolidated: Sequence[Mapping[str, str]], pruned: Sequence[Mapping[str, str]]
) -> list[str]:
    if not consolidated and not pruned:
        return []
    lines = [f"curator: {len(consolidated) + len(pruned)} skill(s) archived"]
    lines += [f"  {e['name']} → {e['into']}" for e in consolidated]
    lines += [f"  {e['name']} → (archived, no successor)" for e in pruned]
    lines.append("  undo one with `indus curator restore <name>`")
    return lines


# -- the pass -------------------------------------------------------------------------------------


@dataclass
class ConsolidationResult:
    ran: bool = False
    summary: str = ""
    model: str = ""
    provider: str = ""
    final: str = ""
    error: str | None = None
    writes_available: bool = False
    tool_calls: dict[str, int] = field(default_factory=dict[str, int])
    calls: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    consolidated: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    pruned: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    added: list[str] = field(default_factory=list[str])
    renames: dict[str, str] = field(default_factory=dict[str, str])
    reference_rewrites: list[ReferenceRewrite] = field(default_factory=list[ReferenceRewrite])

    def rename_lines(self) -> list[str]:
        return rename_lines(self.consolidated, self.pruned)

    def as_payload(self) -> dict[str, Any]:
        return {
            "ran": self.ran,
            "summary": self.summary,
            "model": self.model,
            "provider": self.provider,
            "final": self.final,
            "error": self.error,
            "writes_available": self.writes_available,
            "tool_calls": dict(self.tool_calls),
            "calls": list(self.calls),
            "consolidated": list(self.consolidated),
            "pruned": list(self.pruned),
            "added": list(self.added),
            "renames": dict(self.renames),
            "reference_rewrites": [
                {"source": r.source, "changes": list(r.changes), "error": r.error}
                for r in self.reference_rewrites
            ],
        }


def _assistant_row(response: NormalizedResponse) -> Message:
    row: Message = {"role": "assistant", "content": response.content or ""}
    if response.tool_calls:
        row["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": tc.arguments},
            }
            for tc in response.tool_calls
        ]
    return row


def _preview(args: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in args.items():
        out[key] = (
            value[:_ARG_PREVIEW] + "…"
            if isinstance(value, str) and len(value) > _ARG_PREVIEW
            else value
        )
    return out


async def _tool_loop(
    scope: Scope,
    step: ModelStep,
    registry: ToolRegistry,
    prompt: str,
    *,
    session_id: str,
    max_iterations: int,
    result: ConsolidationResult,
) -> list[RecordedCall]:
    messages: list[Message] = [
        {"role": "system", "content": SYSTEM_TEXT},
        {"role": "user", "content": prompt},
    ]
    tools = definitions(registry, registry.names(), scope=scope)
    recorded: list[RecordedCall] = []
    counts: Counter[str] = Counter()
    ctx = ToolContext(scope=scope, session_id=session_id, task_id=session_id)
    for _round in range(max_iterations):
        response = await step(messages, tools)
        messages.append(_assistant_row(response))
        if not response.tool_calls:
            result.final = (response.content or "").strip()
            break
        for tc in response.tool_calls:
            counts[tc.name] += 1
            try:
                parsed = json.loads(tc.arguments) if tc.arguments.strip() else {}
            except ValueError:
                parsed = None
            if not isinstance(parsed, dict):
                outcome = ToolResult.error("arguments must be a JSON object")
                args: dict[str, Any] = {}
            else:
                args = cast(dict[str, Any], parsed)
                outcome = await dispatch(ToolCall(tc.name, args, tc.id), ctx, registry=registry)
            recorded.append(RecordedCall(tc.name, args))
            result.calls.append(
                {"name": tc.name, "args": _preview(args), "error": outcome.is_error}
            )
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": outcome.text})
    else:
        result.error = f"stopped after {max_iterations} rounds without a final report"
    result.tool_calls = dict(counts)
    return recorded


async def run_consolidation(
    scope: Scope,
    step: ModelStep | None,
    *,
    dry_run: bool = False,
    session_id: str = "curator",
    max_iterations: int = ITERATION_CAP,
    registry_factory: Callable[..., tuple[ToolRegistry, bool]] | None = None,
) -> ConsolidationResult:
    """One consolidation pass; never raises (an error ends up in ``result.error``)."""
    result = ConsolidationResult()
    candidates = candidate_list(scope)
    if not candidates:
        result.summary = "skipped (no skills to consolidate)"
        return result
    if step is None:
        result.summary = "skipped (no model available for the curator task)"
        result.error = "no model step"
        return result
    before = {o.name for o in managed_skills(scope)}
    factory = registry_factory if registry_factory is not None else curator_registry
    registry, can_write = factory(writes=not dry_run)
    result.writes_available = can_write
    prompt = f"{CURATOR_PROMPT}\n\n{candidates}"
    if dry_run:
        prompt = f"{DRY_RUN_PREFACE}\n\n{prompt}"
    elif not can_write:
        prompt = (
            "Note: skill_manage is not available in this pass, so you can only read and "
            f"report.\n\n{prompt}"
        )
    result.ran = True
    recorded: list[RecordedCall] = []
    try:
        with bind_origin("background_review", attended=False), bind_actor("curator"):
            recorded = await _tool_loop(
                scope,
                step,
                registry,
                prompt,
                session_id=session_id,
                max_iterations=max_iterations,
                result=result,
            )
    except Exception as exc:
        log.warning("curator consolidation stopped: %s: %s", type(exc).__name__, exc)
        result.error = f"{type(exc).__name__}: {exc}"
    result.model = getattr(step, "model", "") or ""
    result.provider = getattr(step, "provider", "") or ""
    after = {o.name for o in managed_skills(scope)}
    removed = sorted(before - after)
    result.added = sorted(after - before)
    result.consolidated, result.pruned = classify_removed(removed, after, recorded)
    result.renames = {e["name"]: e["into"] for e in result.consolidated}
    if not dry_run:
        result.reference_rewrites = rewrite_references(
            result.renames, [e["name"] for e in result.pruned]
        )
    final = result.final or ("error: " + result.error if result.error else "no change")
    result.summary = final if len(final) <= _SUMMARY_CHARS else final[:_SUMMARY_CHARS] + "…"
    return result
