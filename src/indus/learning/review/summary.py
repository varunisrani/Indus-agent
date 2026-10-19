"""What a background review changed, in a few lines (P09-16).

``summary_lines(rows, mode)`` reads the review fork's own rows (never the replayed history): every
``memory`` and ``skill_manage`` call it made, matched with its result row. Only writes that really
happened count: a result whose ``success`` is not true, a staged write (``staged: true``, waiting
for approval) and a batch that was rolled back (``operations_applied`` false) are left out, so a
summary never claims a change that is not on disk.

``display.learning_notices`` picks the mode:

- ``off``: nothing, and no event;
- ``on`` (default): one generic line per store or skill — "Memory updated", "User profile
  updated", "Skill <name> created", "Skill <name> improved";
- ``verbose``: those lines with a preview of at most 200 characters (the saved text, the new span
  of a patch, a file path and the start of its content).

``emit_done`` sends ``learning.review.done {kinds, summary_lines}`` on the session's event stream;
``log_completion`` writes the one log line every review ends with.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from indus.core.transcript import TranscriptRow, call_id, call_name
from indus.protocol.events import LearningReviewDone

if TYPE_CHECKING:
    from indus.core.events import EventSink
    from indus.foundation.scope import Scope

__all__ = [
    "MODES",
    "PREVIEW_CHARS",
    "Mode",
    "emit_done",
    "log_completion",
    "mode_from",
    "summary_lines",
]

log = logging.getLogger(__name__)

type Mode = Literal["off", "on", "verbose"]

MODES: tuple[Mode, ...] = ("off", "on", "verbose")
PREVIEW_CHARS = 200
_NOTIFY = frozenset({"memory", "skill_manage"})
_STORE_LINES = {"memory": "Memory updated", "user": "User profile updated"}
_SKILL_VERBS = {
    "create": "created",
    "patch": "improved",
    "edit": "improved",
    "write_file": "improved",
    "remove_file": "improved",
    "delete": "archived",
    "archive": "archived",
}


def mode_from(scope: Scope | None) -> Mode:
    """``display.learning_notices``; anything unreadable or unknown is ``on``."""
    config = scope.config if scope is not None else None
    if config is None:
        return "on"
    try:
        value = config.get("display.learning_notices")
    except Exception:
        log.debug("display.learning_notices unreadable; using on", exc_info=True)
        return "on"
    if value is False:  # YAML reads a bare ``off`` as false
        return "off"
    text = str(value).strip().lower() if value is not None else "on"
    return cast(Mode, text) if text in MODES else "on"


# -- parsing --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Call:
    name: str
    args: Mapping[str, Any]


def _object(raw: object) -> dict[str, Any] | None:
    if isinstance(raw, Mapping):
        return dict(cast(Mapping[str, Any], raw))
    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def _calls(rows: Iterable[TranscriptRow]) -> dict[str, _Call]:
    found: dict[str, _Call] = {}
    for row in rows:
        if row.role != "assistant" or not row.tool_calls:
            continue
        for call in row.tool_calls:
            name, ident = call_name(call), call_id(call)
            if name not in _NOTIFY or not ident:
                continue
            fn = call.get("function") if isinstance(call, Mapping) else None
            raw = cast(Mapping[str, Any], fn).get("arguments") if isinstance(fn, Mapping) else None
            found[ident] = _Call(name, _object(raw) or {})
    return found


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _preview(text: str) -> str:
    flat = " ".join(text.split())
    if len(flat) <= PREVIEW_CHARS:
        return flat
    return flat[: PREVIEW_CHARS - 1] + "…"


def _with_preview(line: str, text: str, verbose: bool) -> str:
    shown = _preview(text) if verbose else ""
    return f"{line}: {shown}" if shown else line


# -- memory ---------------------------------------------------------------------------------------


def _memory_ops(args: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    ops = args.get("operations")
    if isinstance(ops, list):
        items = cast(list[object], ops)
        return [cast(Mapping[str, Any], o) for o in items if isinstance(o, Mapping)]
    return [args]


def _memory_preview(args: Mapping[str, Any]) -> str:
    bits: list[str] = []
    for op in _memory_ops(args):
        action = _text(op.get("action"))
        content = _text(op.get("content")) or _text(op.get("new_text"))
        if action == "remove":
            bits.append(f"removed {_text(op.get('old_text'))}".strip())
        elif content:
            bits.append(content)
    return "; ".join(b for b in bits if b)


def _memory_lines(call: _Call, body: Mapping[str, Any], verbose: bool) -> list[str]:
    if _text(body.get("message")).startswith("Already saved"):
        return []  # an exact duplicate: nothing changed
    target = _text(body.get("target")) or _text(call.args.get("target")) or "memory"
    line = _STORE_LINES.get(target, "Memory updated")
    return [_with_preview(line, _memory_preview(call.args), verbose)]


# -- skills ---------------------------------------------------------------------------------------


def _skill_preview(action: str, item: Mapping[str, Any]) -> str:
    if action == "patch":
        return _text(item.get("new_string")) or _text(item.get("content"))
    if action == "write_file":
        path = _text(item.get("file_path"))
        content = _text(item.get("file_content"))
        return f"{path}: {content}" if path and content else path or content
    if action == "remove_file":
        return f"removed {_text(item.get('file_path'))}".strip()
    return _text(item.get("content"))


def _skill_line(action: str, name: str, item: Mapping[str, Any], verbose: bool) -> str | None:
    verb = _SKILL_VERBS.get(action)
    if verb is None or not name:
        return None
    return _with_preview(f"Skill {name} {verb}", _skill_preview(action, item), verbose)


def _skill_lines(call: _Call, body: Mapping[str, Any], verbose: bool) -> list[str]:
    results = body.get("results")
    if isinstance(results, list):
        if body.get("operations_applied") is False:
            return []
        ops = call.args.get("operations")
        op_list = cast(list[object], ops) if isinstance(ops, list) else [call.args]
        lines: list[str] = []
        for position, raw in enumerate(cast(list[object], results)):
            result = _object(raw)
            # ``skill_manage`` lists one row per applied action under a successful body; a row
            # that says it failed or was only staged is left out.
            if result is None or result.get("success") is False or result.get("staged"):
                continue
            op = _object(op_list[position]) if position < len(op_list) else None
            item = {**(op or {}), **result}
            action = _text(item.get("action"))
            name = _text(item.get("name")) or _text(call.args.get("name"))
            line = _skill_line(action, name, item, verbose)
            if line:
                lines.append(line)
        return lines
    item = {**call.args, **body}
    action = _text(call.args.get("action")) or _text(body.get("action"))
    name = _text(call.args.get("name")) or _text(body.get("name"))
    line = _skill_line(action, name, item, verbose)
    return [line] if line else []


# -- the summary ----------------------------------------------------------------------------------


def summary_lines(rows: Sequence[TranscriptRow], mode: Mode = "on") -> list[str]:
    """The change lines for the review's own ``rows`` (see the module notes)."""
    if mode == "off":
        return []
    verbose = mode == "verbose"
    calls = _calls(rows)
    lines: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if row.role != "tool" or not row.tool_call_id:
            continue
        call = calls.get(row.tool_call_id)
        if call is None:
            continue
        body = _object(row.content)
        if body is None or body.get("success") is not True or body.get("staged"):
            continue
        try:
            found = (
                _memory_lines(call, body, verbose)
                if call.name == "memory"
                else _skill_lines(call, body, verbose)
            )
        except Exception:
            log.warning("could not summarise a %s result of the review", call.name, exc_info=True)
            continue
        for line in found:
            # ``on``: one line per store or skill ("Skill x created" then "... improved" → one)
            key = line if verbose else line.rsplit(" ", 1)[0]
            if key in seen:
                continue
            seen.add(key)
            lines.append(line)
    return lines


def emit_done(
    sink: EventSink | None, kinds: Iterable[str], lines: Sequence[str], mode: Mode
) -> LearningReviewDone | None:
    """Send ``learning.review.done`` on ``sink`` (nothing in ``off`` mode or without a sink)."""
    if sink is None or mode == "off":
        return None
    try:
        event = LearningReviewDone(
            session_id=sink.session_id,
            seq=sink.next_seq(),
            kinds=sorted(set(kinds)),
            summary_lines=list(lines),
        )
        sink.emit(event)
    except Exception:
        log.warning("could not send the review summary event", exc_info=True)
        return None
    return event


def log_completion(
    session_id: str,
    *,
    kinds: Iterable[str],
    lines: Sequence[str],
    requests: int,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    stop: str,
    routed: bool,
) -> None:
    """The one line every review ends with: cost, where it ran, what it changed."""
    changed = sorted({"memory" if not line.startswith("Skill ") else "skills" for line in lines})
    log.info(
        "review finished: session=%s kinds=%s requests=%d in=%d out=%d cache_read=%d "
        "changed=%s stop=%s routed=%s",
        session_id,
        ",".join(sorted(set(kinds))),
        requests,
        input_tokens,
        output_tokens,
        cache_read_tokens,
        ",".join(changed) or "none",
        stop,
        "yes" if routed else "no",
    )
