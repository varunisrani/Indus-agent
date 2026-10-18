"""Vocabulary for scripting a fake model, and a loader for script files.

A ``Script`` is a list of ``Turn``s, one per model request; each turn is an ordered list of steps
the fake streams back. ``FakeModelServer`` (``server.py``, P03-18) speaks the Chat Completions and
Anthropic Messages wire formats from these scripts; the format is documented in ``README.md``.

File format (YAML or JSON)::

    turns:
      - steps:
          - text: ["hel", "l", "o"]
          - usage: {prompt: 10, completion: 3}
          - finish: stop

Each step is a mapping with exactly one key naming its kind.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from tests.fakes.model.mini_yaml import LineList, LineMap, YamlSubsetError, loads

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "model_scripts"


@dataclass(frozen=True)
class Text:
    chunks: tuple[str, ...]


@dataclass(frozen=True)
class Reasoning:
    chunks: tuple[str, ...]


@dataclass(frozen=True)
class ToolCall:
    """One tool call; ``arguments`` may be a raw string to script malformed JSON.

    ``late_id`` sends the call id with the second fragment instead of the first (Chat Completions
    servers that do this exist; the Anthropic wire always names the id up front).
    """

    name: str
    arguments: Mapping[str, Any] | str = field(default_factory=dict)
    id: str | None = None
    fragments: int = 3
    late_id: bool = False


@dataclass(frozen=True)
class Usage:
    prompt: int
    completion: int
    cached: int = 0


@dataclass(frozen=True)
class Fail:
    """An HTTP error answer (any status, any body, e.g. an HTML page)."""

    status: int
    body: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Stall:
    """Go silent for ``seconds`` of clock time without closing the stream."""

    seconds: float


@dataclass(frozen=True)
class Disconnect:
    """Drop the connection here, without a finish."""


@dataclass(frozen=True)
class Eof:
    """End the body cleanly here without a finish (a stream that stops early).

    A turn that simply runs out of steps gets an implied finish instead.
    """


@dataclass(frozen=True)
class KeepAlive:
    """``count`` keep-alive frames with no content (an SSE comment / an Anthropic ping)."""

    count: int = 1


@dataclass(frozen=True)
class MidStreamError:
    """An error reported inside an open stream: a mapping becomes a JSON error frame, a string
    is sent as plain-text data (some gateways do that)."""

    body: Mapping[str, Any] | str


@dataclass(frozen=True)
class Finish:
    reason: str = "stop"


Step = (
    Text
    | Reasoning
    | ToolCall
    | Usage
    | Fail
    | Stall
    | Disconnect
    | Eof
    | KeepAlive
    | MidStreamError
    | Finish
)


@dataclass(frozen=True)
class Turn:
    steps: tuple[Step, ...]


@dataclass(frozen=True)
class Script:
    turns: tuple[Turn, ...]
    # Where the script came from (file stem); failures name it. Not part of equality.
    name: str = field(default="", compare=False)


class ScriptError(ValueError):
    """A script file that does not follow the format; the message names where."""


def _chunks(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(c, str) for c in value):
        return tuple(value)
    raise TypeError("expected a string or a list of strings")


def _fields(value: Any, allowed: set[str], required: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("expected a mapping")
    unknown = set(value) - allowed
    if unknown:
        raise TypeError(f"unknown field(s) {sorted(unknown)}; allowed {sorted(allowed)}")
    missing = required - set(value)
    if missing:
        raise TypeError(f"missing field(s) {sorted(missing)}")
    return dict(value)


def _tool_call(value: Any) -> ToolCall:
    data = _fields(value, {"name", "arguments", "id", "fragments", "late_id"}, {"name"})
    arguments = data.get("arguments", {})
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, str | dict):
        raise TypeError("arguments must be a mapping or a raw string")
    return ToolCall(
        name=str(data["name"]),
        arguments=dict(arguments) if isinstance(arguments, dict) else arguments,
        id=None if data.get("id") is None else str(data["id"]),
        fragments=int(data.get("fragments", 3)),
        late_id=bool(data.get("late_id", False)),
    )


def _usage(value: Any) -> Usage:
    data = _fields(value, {"prompt", "completion", "cached"}, {"prompt", "completion"})
    return Usage(int(data["prompt"]), int(data["completion"]), int(data.get("cached", 0)))


def _fail(value: Any) -> Fail:
    data = _fields(value, {"status", "body", "headers"}, {"status"})
    headers = data.get("headers") or {}
    if not isinstance(headers, dict):
        raise TypeError("headers must be a mapping")
    return Fail(
        int(data["status"]),
        str(data.get("body", "")),
        {str(k): str(v) for k, v in headers.items()},
    )


def _stall(value: Any) -> Stall:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        raise TypeError("expected a number of seconds >= 0")
    return Stall(float(value))


def _disconnect(value: Any) -> Disconnect:
    if value not in (None, {}, True):
        raise TypeError("takes no value")
    return Disconnect()


def _eof(value: Any) -> Eof:
    if value not in (None, {}, True):
        raise TypeError("takes no value")
    return Eof()


def _keep_alive(value: Any) -> KeepAlive:
    if value is None:
        return KeepAlive()
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise TypeError("expected a frame count >= 1 (or nothing for one frame)")
    return KeepAlive(value)


def _mid_stream_error(value: Any) -> MidStreamError:
    if isinstance(value, str):
        return MidStreamError(value)
    if isinstance(value, dict):
        return MidStreamError(dict(value))
    raise TypeError("expected an error body mapping or a plain-text string")


def _finish(value: Any) -> Finish:
    if value is None:
        return Finish()
    if not isinstance(value, str):
        raise TypeError("expected a finish reason string")
    return Finish(value)


_STEP_KINDS: dict[str, Callable[[Any], Step]] = {
    "text": lambda v: Text(_chunks(v)),
    "reasoning": lambda v: Reasoning(_chunks(v)),
    "tool_call": _tool_call,
    "usage": _usage,
    "fail": _fail,
    "stall": _stall,
    "disconnect": _disconnect,
    "eof": _eof,
    "keep_alive": _keep_alive,
    "mid_stream_error": _mid_stream_error,
    "finish": _finish,
}


def _where(source: str, node: Any, fallback: str) -> str:
    line = getattr(node, "line", 0)
    return f"{source}:{line}" if line else f"{source} ({fallback})"


def parse_script(data: Any, source: str = "<script>") -> Script:
    """Build a ``Script`` from already-parsed data (dicts and lists)."""
    if not isinstance(data, dict) or "turns" not in data:
        raise ScriptError(f"{_where(source, data, 'top level')}: expected a mapping with 'turns'")
    extra = set(data) - {"turns"}
    if extra:
        raise ScriptError(f"{_where(source, data, 'top level')}: unknown key(s) {sorted(extra)}")
    raw_turns = data["turns"]
    if not isinstance(raw_turns, list) or not raw_turns:
        raise ScriptError(f"{_where(source, data, 'top level')}: 'turns' must be a non-empty list")
    turns: list[Turn] = []
    for t_index, raw_turn in enumerate(raw_turns, start=1):
        turn_at = _where(source, raw_turn, f"turn {t_index}")
        if not isinstance(raw_turn, dict) or set(raw_turn) != {"steps"}:
            raise ScriptError(f"{turn_at}: a turn is a mapping with only 'steps'")
        raw_steps = raw_turn["steps"]
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ScriptError(f"{turn_at}: 'steps' must be a non-empty list")
        steps: list[Step] = []
        for s_index, raw_step in enumerate(raw_steps, start=1):
            at = _where(source, raw_step, f"turn {t_index}, step {s_index}")
            if not isinstance(raw_step, dict) or len(raw_step) != 1:
                raise ScriptError(f"{at}: a step is a mapping with exactly one key (its kind)")
            ((kind, value),) = raw_step.items()
            build = _STEP_KINDS.get(kind)
            if build is None:
                known = ", ".join(sorted(_STEP_KINDS))
                raise ScriptError(f"{at}: unknown step kind {kind!r} (known: {known})")
            try:
                steps.append(build(value))
            except (TypeError, ValueError) as exc:
                raise ScriptError(f"{at}: bad {kind!r} step: {exc}") from None
        turns.append(Turn(tuple(steps)))
    return Script(tuple(turns))


def load_script(path: str | Path) -> Script:
    """Load a script from a ``.yaml``/``.yml``/``.json`` file.

    A bare name (no suffix, no directory) is looked up in ``tests/fixtures/model_scripts/``.
    """
    p = Path(path)
    if not p.suffix and p.parent == Path("."):
        p = SCRIPTS_DIR / f"{p.name}.yaml"
    text = p.read_text(encoding="utf-8")
    if p.suffix == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ScriptError(f"{p}:{exc.lineno}: invalid JSON: {exc.msg}") from None
    elif p.suffix in (".yaml", ".yml"):
        try:
            data = loads(text)
        except YamlSubsetError as exc:
            raise ScriptError(f"{p}:{exc.line}: {exc.message}") from None
    else:
        raise ScriptError(f"{p}: unsupported script file type {p.suffix!r}")
    return replace(parse_script(data, str(p)), name=p.stem)


# -- script helpers (P04-13) ----------------------------------------------------------------------


def text_turn(*chunks: str, finish: str = "stop") -> Turn:
    """A turn that streams ``chunks`` as visible text."""
    return Turn((Text(tuple(chunks) or ("",)), Finish(finish)))


def tool_batch(*calls: ToolCall | tuple[str, Mapping[str, Any] | str]) -> Turn:
    """A turn made of tool calls (``ToolCall`` values or ``(name, arguments)`` pairs)."""
    steps: list[Step] = []
    for index, call in enumerate(calls):
        if isinstance(call, ToolCall):
            steps.append(call)
        else:
            name, arguments = call
            steps.append(ToolCall(name=name, arguments=arguments, id=f"call_{index}"))
    return Turn((*steps, Finish("tool_calls")))


def malformed_args(name: str, raw: str = '{"path": "a.txt"', call_id: str = "call_0") -> Turn:
    """A turn whose one tool call carries arguments that are not valid JSON."""
    return Turn((ToolCall(name=name, arguments=raw, id=call_id), Finish("tool_calls")))


def mid_stream_cut(text: str) -> Turn:
    """A turn that shows ``text`` and then loses the connection."""
    return Turn((Text((text,)), Disconnect()))


def thinking_only(reasoning: str) -> Turn:
    """A turn with reasoning and no visible text."""
    return Turn((Reasoning((reasoning,)), Finish("stop")))


def script_of(*turns: Turn, name: str = "inline") -> Script:
    return Script(tuple(turns), name=name)


# The wire servers import the vocabulary above, so they come last.
from tests.fakes.model.server import (  # noqa: E402
    FakeModelServer,
    FakeScriptExhausted,
    RecordedRequest,
    fake_route,
)

__all__ = [
    "SCRIPTS_DIR",
    "Disconnect",
    "Eof",
    "Fail",
    "FakeModelServer",
    "FakeScriptExhausted",
    "Finish",
    "KeepAlive",
    "LineList",
    "LineMap",
    "MidStreamError",
    "Reasoning",
    "RecordedRequest",
    "Script",
    "ScriptError",
    "Stall",
    "Step",
    "Text",
    "ToolCall",
    "Turn",
    "Usage",
    "fake_route",
    "load_script",
    "malformed_args",
    "mid_stream_cut",
    "parse_script",
    "script_of",
    "text_turn",
    "thinking_only",
    "tool_batch",
]
