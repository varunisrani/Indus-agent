"""Chat Completions wire format for the fake model.

``stream_actions`` turns one scripted ``Turn`` into what the server sends: SSE frames (bytes) and
control actions (``Stall`` waits on the clock, ``Disconnect`` cuts the body). ``json_body`` is the
same turn as one non-streaming JSON answer. Field names follow the public Chat Completions API
reference (streaming chunk object, ``choices[].delta``, ``tool_calls[].index``, final usage chunk
with ``stream_options.include_usage``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from tests.fakes.model import (
    Disconnect,
    Eof,
    Fail,
    Finish,
    KeepAlive,
    MidStreamError,
    Reasoning,
    Stall,
    Text,
    ToolCall,
    Turn,
    Usage,
)

Action = bytes | Stall | Disconnect

# A fixed creation time keeps every frame deterministic.
CREATED = 1_767_225_600


def sse_frame(data: str, event: str | None = None) -> bytes:
    lines = [] if event is None else [f"event: {event}"]
    lines.extend(f"data: {line}" for line in data.split("\n"))
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def compact(obj: object) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def arguments_text(call: ToolCall) -> str:
    """The arguments as the wire carries them: raw strings untouched, mappings as JSON."""
    if isinstance(call.arguments, str):
        return call.arguments
    return compact(dict(call.arguments))


def split_fragments(text: str, count: int) -> list[str]:
    """Split ``text`` into ``count`` contiguous pieces (fewer when it is shorter)."""
    count = max(1, min(count, len(text))) if text else 1
    size, extra = divmod(len(text), count)
    pieces: list[str] = []
    start = 0
    for i in range(count):
        end = start + size + (1 if i < extra else 0)
        pieces.append(text[start:end])
        start = end
    return pieces


def tool_call_id(call: ToolCall, turn_no: int, position: int, prefix: str = "call_fake") -> str:
    return call.id if call.id else f"{prefix}_{turn_no}_{position}"


def usage_object(usage: Usage) -> dict[str, Any]:
    return {
        "prompt_tokens": usage.prompt,
        "completion_tokens": usage.completion,
        "total_tokens": usage.prompt + usage.completion,
        "prompt_tokens_details": {"cached_tokens": usage.cached},
    }


def error_body(body: Mapping[str, Any]) -> dict[str, Any]:
    """An OpenAI-style error envelope (``{"error": {...}}``) around ``body`` if it is bare."""
    if "error" in body:
        return dict(body)
    return {"error": dict(body)}


class _Chunks:
    def __init__(self, turn_no: int, model: str) -> None:
        self.id = f"chatcmpl-fake-{turn_no}"
        self.model = model
        self.first = True

    def chunk(self, delta: dict[str, Any], finish: str | None = None) -> bytes:
        if self.first:
            delta = {"role": "assistant", **delta}
            self.first = False
        payload = {
            "id": self.id,
            "object": "chat.completion.chunk",
            "created": CREATED,
            "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return sse_frame(compact(payload))

    def usage(self, usage: Usage) -> bytes:
        payload = {
            "id": self.id,
            "object": "chat.completion.chunk",
            "created": CREATED,
            "model": self.model,
            "choices": [],
            "usage": usage_object(usage),
        }
        return sse_frame(compact(payload))


def implied_finish(turn: Turn) -> str:
    """The finish a turn without a ``finish`` step gets: tool_calls when it called a tool."""
    return "tool_calls" if any(isinstance(s, ToolCall) for s in turn.steps) else "stop"


def stream_actions(turn: Turn, *, turn_no: int, model: str) -> list[Action]:
    out: list[Action] = []
    chunks = _Chunks(turn_no, model)
    tool_index = 0
    finished = False
    broken = False
    for step in turn.steps:
        match step:
            case Text(chunks=parts):
                out.extend(chunks.chunk({"content": part}) for part in parts)
            case Reasoning(chunks=parts):
                out.extend(chunks.chunk({"reasoning": part}) for part in parts)
            case ToolCall():
                out.extend(_tool_call_frames(chunks, step, tool_index, turn_no))
                tool_index += 1
            case Usage():
                out.append(chunks.usage(step))
            case KeepAlive(count=count):
                out.extend(b": keep-alive\n\n" for _ in range(count))
            case Stall():
                out.append(step)
            case Disconnect():
                out.append(step)
                broken = True
                break
            case Eof():
                broken = True
                break
            case MidStreamError(body=body):
                if isinstance(body, str):
                    out.append(sse_frame(body))
                else:
                    out.append(sse_frame(compact(error_body(body))))
                broken = True
                break
            case Finish(reason=reason):
                out.append(chunks.chunk({}, finish=reason))
                finished = True
            case Fail():
                raise ValueError("a fail step is only allowed as the first step of a turn")
    if not broken:
        if not finished:
            out.append(chunks.chunk({}, finish=implied_finish(turn)))
        out.append(sse_frame("[DONE]"))
    return out


def _tool_call_frames(chunks: _Chunks, call: ToolCall, index: int, turn_no: int) -> list[bytes]:
    call_id = tool_call_id(call, turn_no, index)
    pieces = split_fragments(arguments_text(call), call.fragments)
    if call.late_id and len(pieces) < 2:
        pieces.append("")
    frames: list[bytes] = []
    for n, piece in enumerate(pieces):
        entry: dict[str, Any] = {"index": index}
        if n == 0:
            entry["type"] = "function"
            entry["function"] = {"name": call.name, "arguments": piece}
            if not call.late_id:
                entry["id"] = call_id
        else:
            entry["function"] = {"arguments": piece}
            if call.late_id and n == 1:
                entry["id"] = call_id
        frames.append(chunks.chunk({"tool_calls": [entry]}))
    return frames


def json_body(turn: Turn, *, turn_no: int, model: str) -> dict[str, Any]:
    """The turn as one ``chat.completion`` object (``stream: false``)."""
    text: list[str] = []
    reasoning: list[str] = []
    calls: list[dict[str, Any]] = []
    usage: Usage | None = None
    finish = implied_finish(turn)
    for step in turn.steps:
        match step:
            case Text(chunks=parts):
                text.extend(parts)
            case Reasoning(chunks=parts):
                reasoning.extend(parts)
            case ToolCall():
                calls.append(
                    {
                        "id": tool_call_id(step, turn_no, len(calls)),
                        "type": "function",
                        "function": {"name": step.name, "arguments": arguments_text(step)},
                    }
                )
            case Usage():
                usage = step
            case Finish(reason=reason):
                finish = reason
            case _:
                pass
    message: dict[str, Any] = {"role": "assistant", "content": "".join(text) if text else None}
    if reasoning:
        message["reasoning"] = "".join(reasoning)
    if calls:
        message["tool_calls"] = calls
    body: dict[str, Any] = {
        "id": f"chatcmpl-fake-{turn_no}",
        "object": "chat.completion",
        "created": CREATED,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
    }
    if usage is not None:
        body["usage"] = usage_object(usage)
    return body
