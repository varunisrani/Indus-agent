# pyright: strict
"""``delegate_task``: the schema, the limits and the input normalisation (P10-01).

The tool hands work to subagents. One call either spawns (``tasks``: one entry per subagent) or
controls children already running in the background (``action``: ``list``, ``steer``, ``stop``).
The agent loop runs it (an inline tool, ``core.subagents``); the registry entry only carries the
schema, and its handler answers a direct dispatch with the inline-only error.

``normalise(args, max_children=...)`` turns whatever the model sent into one ``DelegateRequest``
or raises ``DelegateInputError`` with a message for the model:

- an old single ``goal`` / ``context`` / ``output_schema`` / ``images`` becomes a one-entry task
  list (``tasks`` wins when both are sent; an empty ``tasks`` list counts as absent);
- ``tasks`` sent as a JSON string is parsed;
- a per-task or top-level ``role`` and a top-level ``background`` are accepted and ignored (what a
  child may do comes from its depth; where it runs comes from the surface);
- fields only trusted config may set (``TRUSTED_FIELDS``) are dropped from every task and from the
  top level;
- ``images`` (a string becomes a one-entry list) hold at most ``MAX_TASK_IMAGES`` non-empty
  entries; ``output_schema`` must be an object (a JSON string is parsed) — its meta-check is the
  runtime's, before any child starts.

``DelegationLimits.from_scope`` reads ``delegation.*`` (defaults per D-16.24). The schema's
description is rebuilt per session (``schema_overrides``) so it names the live concurrency cap.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from indus.foundation.config.schema import default_for
from indus.foundation.scope import Scope
from indus.tools.calls import ToolResult
from indus.tools.registry import ToolRegistry, ToolTraits

__all__ = [
    "ACTIONS",
    "DELEGATE_SCHEMA",
    "INLINE_ONLY_TEXT",
    "MAX_TASK_IMAGES",
    "TOOLSET",
    "TOOL_NAME",
    "TRUSTED_FIELDS",
    "DelegateInputError",
    "DelegateRequest",
    "DelegateTask",
    "DelegationLimits",
    "normalise",
    "register_tools",
    "schema_overrides",
]

TOOL_NAME = "delegate_task"
TOOLSET = "delegation"
ACTIONS = ("spawn", "list", "steer", "stop")
MAX_TASK_IMAGES = 8
SUMMARY_FLOOR_CHARS = 2_000

# Set only by trusted config or internal callers; never taken from the model's arguments.
TRUSTED_FIELDS = frozenset(
    {
        "toolsets",
        "model",
        "provider",
        "base_url",
        "api_key",
        "api_mode",
        "worktree",
        "max_iterations",
        "acp_command",
        "acp_args",
    }
)
# Accepted for old transcripts and ignored.
IGNORED_FIELDS = frozenset({"role", "background"})

INLINE_ONLY_TEXT = (
    f"{TOOL_NAME} is run by the agent loop and cannot be dispatched as a registry tool"
)


class DelegateInputError(ValueError):
    """The arguments cannot be used; the message is for the model."""


# -- limits ---------------------------------------------------------------------------------------


def _int(value: object, fallback: int, floor: int) -> int:
    if isinstance(value, bool):
        return fallback
    try:
        number = int(cast(Any, value))
    except (TypeError, ValueError):
        return fallback
    return max(floor, number)


def _setting(scope: Scope | None, key: str) -> object:
    full = f"delegation.{key}"
    value: object = None
    if scope is not None and scope.config is not None:
        try:
            value = scope.config.get(full)
        except Exception:
            value = None
    return default_for(full) if value is None else value


@dataclass(frozen=True)
class DelegationLimits:
    """The numbers delegation runs by (``delegation.*``; defaults per D-16.24)."""

    max_concurrent_children: int = 10
    max_depth: int = 1
    max_iterations: int = 250
    max_async_children: int = 3
    summary_max_chars: int = 24_000
    oneshot_max_children: int = 2
    child_toolsets: tuple[str, ...] = ("terminal", "file", "web")
    model: str = ""
    inherit_mcp_toolsets: bool = True
    worktree: bool = False

    @classmethod
    def from_scope(cls, scope: Scope | None) -> DelegationLimits:
        base = cls()
        toolsets = _setting(scope, "child_toolsets")
        names: tuple[str, ...] = base.child_toolsets
        if isinstance(toolsets, (list, tuple)):
            names = tuple(str(t) for t in cast(Sequence[object], toolsets) if str(t).strip())
        model = _setting(scope, "model")
        return cls(
            max_concurrent_children=_int(
                _setting(scope, "max_concurrent_children"), base.max_concurrent_children, 1
            ),
            max_depth=_int(_setting(scope, "max_depth"), base.max_depth, 1),
            max_iterations=_int(_setting(scope, "max_iterations"), base.max_iterations, 1),
            max_async_children=_int(
                _setting(scope, "max_async_children"), base.max_async_children, 1
            ),
            summary_max_chars=_int(
                _setting(scope, "summary_max_chars"), base.summary_max_chars, SUMMARY_FLOOR_CHARS
            ),
            oneshot_max_children=_int(
                _setting(scope, "oneshot_max_children"), base.oneshot_max_children, 0
            ),
            child_toolsets=names,
            model=model.strip() if isinstance(model, str) else "",
            inherit_mcp_toolsets=_setting(scope, "inherit_mcp_toolsets") is not False,
            worktree=_setting(scope, "worktree") is True,
        )


# -- the normalised request -----------------------------------------------------------------------


@dataclass(frozen=True)
class DelegateTask:
    goal: str
    context: str = ""
    output_schema: Mapping[str, Any] | None = None
    images: tuple[str, ...] = ()
    group: str | None = None


@dataclass(frozen=True)
class DelegateRequest:
    action: str
    tasks: tuple[DelegateTask, ...] = ()
    subagent_id: str = ""
    message: str = ""
    # Argument names that were accepted but ignored or dropped (for logs and tests).
    ignored: tuple[str, ...] = field(default=())


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _tasks_value(raw: object) -> object:
    if not isinstance(raw, str):
        return raw
    text = raw.strip()
    if not text:
        return None
    try:
        parsed: object = json.loads(text)
    except ValueError as exc:
        raise DelegateInputError(
            f"tasks must be an array of task objects; the text sent is not valid JSON ({exc})"
        ) from None
    if not isinstance(parsed, list):
        raise DelegateInputError(
            f"tasks must be an array of task objects, not {type(parsed).__name__}"
        )
    return cast(list[object], parsed)


def _schema_value(raw: object, index: int) -> Mapping[str, Any] | None:
    if raw is None:
        return None
    value: object = raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except ValueError:
            raise DelegateInputError(
                f"task {index}: output_schema must be a JSON Schema object; the text sent is "
                "not JSON"
            ) from None
    if not isinstance(value, Mapping):
        raise DelegateInputError(f"task {index}: output_schema must be a JSON Schema object")
    return dict(cast(Mapping[str, Any], value))


def _images_value(raw: object, index: int) -> tuple[str, ...]:
    if raw is None:
        return ()
    items: list[object] = [raw] if isinstance(raw, str) else []
    if not isinstance(raw, str):
        if not isinstance(raw, (list, tuple)):
            raise DelegateInputError(
                f"task {index}: images must be a list of file paths or http(s) URLs"
            )
        items = list(cast(Sequence[object], raw))
    cleaned: list[str] = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise DelegateInputError(
                f"task {index}: every images entry must be a non-empty path or URL"
            )
        cleaned.append(item.strip())
    if len(cleaned) > MAX_TASK_IMAGES:
        raise DelegateInputError(
            f"task {index} has {len(cleaned)} images; at most {MAX_TASK_IMAGES} are allowed. Keep "
            "the ones the subagent has to look at."
        )
    return tuple(cleaned)


def _task(raw: object, index: int, ignored: list[str]) -> DelegateTask:
    if not isinstance(raw, Mapping):
        raise DelegateInputError(f"task {index} must be an object with a goal")
    entry = cast(Mapping[str, Any], raw)
    for name in entry:
        if name in TRUSTED_FIELDS or name in IGNORED_FIELDS:
            ignored.append(f"tasks[{index}].{name}")
    goal = _text(entry.get("goal"))
    if not goal:
        raise DelegateInputError(f"task {index} has no goal; say what the subagent should do")
    group = _text(entry.get("group")) or None
    return DelegateTask(
        goal=goal,
        context=_text(entry.get("context")),
        output_schema=_schema_value(entry.get("output_schema"), index),
        images=_images_value(entry.get("images"), index),
        group=group,
    )


def normalise(args: Mapping[str, Any], *, max_children: int) -> DelegateRequest:
    """The one request ``args`` stand for (see the module notes)."""
    ignored = [name for name in args if name in TRUSTED_FIELDS or name in IGNORED_FIELDS]
    action = _text(args.get("action")).lower() or "spawn"
    if action not in ACTIONS:
        raise DelegateInputError(
            f"unknown action {args.get('action')!r}; use one of: {', '.join(ACTIONS)}"
        )
    subagent_id = _text(args.get("subagent_id"))
    message = _text(args.get("message"))
    if action == "list":
        return DelegateRequest(action, ignored=tuple(ignored))
    if action in ("steer", "stop"):
        if not subagent_id:
            raise DelegateInputError(
                f"action {action} needs subagent_id (from the spawn result or action=list)"
            )
        if action == "steer" and not message:
            raise DelegateInputError("action steer needs message: the correction to send")
        return DelegateRequest(
            action, subagent_id=subagent_id, message=message, ignored=tuple(ignored)
        )
    raw_tasks = _tasks_value(args.get("tasks"))
    if isinstance(raw_tasks, (list, tuple)) and not raw_tasks:
        raw_tasks = None
    if raw_tasks is None:
        goal = _text(args.get("goal"))
        if not goal:
            raise DelegateInputError(
                "no tasks given; pass tasks=[{goal: ..., context: ...}], one entry per subagent"
            )
        single: dict[str, Any] = {"goal": goal, "context": args.get("context")}
        for name in ("output_schema", "images"):
            if args.get(name) is not None:
                single[name] = args.get(name)
        raw_tasks = [single]
    if not isinstance(raw_tasks, (list, tuple)):
        raise DelegateInputError("tasks must be an array of task objects")
    items = list(cast(Sequence[object], raw_tasks))
    if len(items) > max_children:
        raise DelegateInputError(
            f"{len(items)} tasks were given but at most {max_children} subagents run per call "
            "(delegation.max_concurrent_children); send fewer, or split them over several calls"
        )
    tasks = tuple(_task(item, index, ignored) for index, item in enumerate(items))
    return DelegateRequest("spawn", tasks=tasks, ignored=tuple(ignored))


# -- schema ---------------------------------------------------------------------------------------


def _prop(kind: str, description: str, **extra: Any) -> dict[str, Any]:
    return {"type": kind, "description": description, **extra}


_TASK_ITEM: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal": _prop(
            "string",
            "What this subagent has to achieve. Make it complete on its own: the subagent has "
            "not seen this conversation.",
        ),
        "context": _prop(
            "string",
            "Everything this subagent needs to know: paths, error text, constraints, the "
            "language or style of the answer. Repeat shared background in every task.",
        ),
        "output_schema": _prop(
            "object",
            "Optional JSON Schema the subagent's final answer must match. It is told so up "
            "front; a wrong answer gets one correction round, and its text comes back either way "
            "with schema_valid. Require only the fields you will read.",
        ),
        "images": _prop(
            "array",
            f"Optional images the subagent has to look at (at most {MAX_TASK_IMAGES}): local "
            "paths or http(s) URLs. Put text files in context instead.",
            items={"type": "string"},
        ),
        "group": _prop("string", "Optional label for related tasks; it does not order them."),
    },
    "required": ["goal"],
}

_PROPERTIES: dict[str, Any] = {
    "action": _prop(
        "string",
        "spawn (the default) starts subagents. For subagents running in the background: "
        "list shows them, steer sends one a correction (subagent_id and message) without "
        "stopping it, stop ends one early (subagent_id) and returns what it has so far.",
        enum=list(ACTIONS),
    ),
    "tasks": _prop(
        "array",
        "The tasks, one subagent each (the limit is filled in per session).",
        items=_TASK_ITEM,
        minItems=1,
    ),
    "subagent_id": _prop(
        "string", "The subagent to steer or stop (from the spawn result or list)."
    ),
    "message": _prop(
        "string",
        "For steer: the correction, shown to the subagent after its next tool call. Be specific.",
    ),
}

DELEGATE_SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": "Hand tasks to subagents (the full text is rebuilt per session).",
    "parameters": {"type": "object", "required": [], "properties": _PROPERTIES},
}

_DESCRIPTION = (
    "Hand tasks to subagents. Each subagent starts with an empty conversation, its own terminal "
    "session and a reduced tool set, works on its task alone and gives back only a final "
    "summary. Put every task in tasks; several tasks run side by side.\n\n"
    "On an interactive surface the subagents run in the background: the call returns at once "
    "and their results arrive as a new message between your turns. Do what does not depend on "
    "them, say briefly what is running, and end your turn; do not poll or wait for them. In "
    "one-shot runs, scheduled jobs and inside another subagent the call waits and returns the "
    "results itself. Subagents live only as long as this process: work that has to survive a "
    "restart or the end of the session belongs in a scheduled job or a background process.\n\n"
    "Good uses: work that needs its own reasoning, work that would flood this conversation with "
    "output, and independent pieces that can run in parallel. Not for: a single tool call (make "
    "it yourself), mechanical scripted steps, or anything that needs the user's answers "
    "(subagents cannot ask questions).\n\n"
    "Rules: subagents know nothing of this conversation, so the goal and context must carry "
    "everything. Their summaries are their own account, not checked facts: before telling the "
    "user that something was published, uploaded or written, ask the subagent for a handle "
    "(URL, id, absolute path) and check it yourself. Subagents cannot ask the user, keep "
    "memory or schedule jobs{nested}. They use your model unless delegation.model in the "
    "config names another."
)


def schema_overrides(scope: Scope | None) -> Mapping[str, Any]:
    """The description and ``tasks`` text with this profile's live limits."""
    limits = DelegationLimits.from_scope(scope)
    nested = (
        "; they may hand work on to subagents of their own while depth remains "
        f"(delegation.max_depth = {limits.max_depth})"
        if limits.max_depth >= 2
        else ", and they cannot delegate further"
    )
    params = dict(cast(Mapping[str, Any], DELEGATE_SCHEMA["parameters"]))
    properties = {k: dict(v) for k, v in cast(Mapping[str, Any], params["properties"]).items()}
    properties["tasks"]["description"] = (
        f"The tasks, one subagent each: at most {limits.max_concurrent_children} per call "
        "(delegation.max_concurrent_children). A single task is a one-entry array. Required to "
        "spawn."
    )
    params["properties"] = properties
    return {"description": _DESCRIPTION.format(nested=nested), "parameters": params}


def _outside_the_loop(args: Mapping[str, Any]) -> str:
    """The registry handler: ``delegate_task`` only runs through the agent loop's inline table."""
    return ToolResult.error(INLINE_ONLY_TEXT, error_type="inline_tool").text


def register_tools(registry: ToolRegistry) -> None:
    registry.register(
        TOOL_NAME,
        TOOLSET,
        DELEGATE_SCHEMA,
        _outside_the_loop,
        schema_overrides=schema_overrides,
        traits=ToolTraits(mutating=True),
    )
