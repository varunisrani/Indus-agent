"""Anthropic Messages wire format for the fake model.

Same shape as ``wire_chat``: ``stream_actions`` gives SSE frames plus control actions, and
``json_body`` gives the non-streaming ``message`` object. Event and field names follow the public
Messages API streaming reference: ``message_start``, ``content_block_start`` / ``_delta`` /
``_stop`` (``text_delta``, ``thinking_delta``, ``signature_delta``, ``input_json_delta``),
``message_delta`` with ``stop_reason`` and output usage, ``message_stop``, ``ping``, ``error``.
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
from tests.fakes.model.wire_chat import (
    Action,
    arguments_text,
    compact,
    implied_finish,
    split_fragments,
    sse_frame,
    tool_call_id,
)

# Our finish words → the stop reasons this wire uses; anything else is sent as written.
STOP_REASONS = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "content_filter": "refusal",
}


def _event(name: str, payload: Mapping[str, Any]) -> bytes:
    return sse_frame(compact({"type": name, **payload}), event=name)


def _usage_of(turn: Turn) -> Usage | None:
    found = [s for s in turn.steps if isinstance(s, Usage)]
    return found[-1] if found else None


def error_event_body(body: Mapping[str, Any]) -> dict[str, Any]:
    if body.get("type") == "error" and "error" in body:
        return dict(body)
    inner = body.get("error", body)
    return {"type": "error", "error": dict(inner) if isinstance(inner, Mapping) else inner}


class _Blocks:
    """Opens and closes content blocks so consecutive deltas share one block."""

    def __init__(self, turn_no: int) -> None:
        self.turn_no = turn_no
        self.index = -1
        self.open_kind: str | None = None
        self.out: list[Action] = []

    def close(self) -> None:
        if self.open_kind is None:
            return
        if self.open_kind == "thinking":
            signature = f"fake-signature-{self.turn_no}-{self.index}"
            self.out.append(
                _event(
                    "content_block_delta",
                    {
                        "index": self.index,
                        "delta": {"type": "signature_delta", "signature": signature},
                    },
                )
            )
        self.out.append(_event("content_block_stop", {"index": self.index}))
        self.open_kind = None

    def open(self, kind: str, block: Mapping[str, Any]) -> None:
        self.close()
        self.index += 1
        self.open_kind = kind
        self.out.append(
            _event("content_block_start", {"index": self.index, "content_block": block})
        )

    def delta(self, delta: Mapping[str, Any]) -> None:
        self.out.append(_event("content_block_delta", {"index": self.index, "delta": dict(delta)}))


def stream_actions(turn: Turn, *, turn_no: int, model: str) -> list[Action]:
    usage = _usage_of(turn)
    blocks = _Blocks(turn_no)
    start_usage: dict[str, Any] = {
        "input_tokens": usage.prompt if usage else 0,
        "output_tokens": 1,
        "cache_read_input_tokens": usage.cached if usage else 0,
        "cache_creation_input_tokens": 0,
    }
    message = {
        "id": f"msg_fake_{turn_no}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": start_usage,
    }
    blocks.out.append(_event("message_start", {"message": message}))
    tool_index = 0
    finished = False

    def finish(reason: str) -> None:
        blocks.close()
        blocks.out.append(
            _event(
                "message_delta",
                {
                    "delta": {
                        "stop_reason": STOP_REASONS.get(reason, reason),
                        "stop_sequence": None,
                    },
                    "usage": {"output_tokens": usage.completion if usage else 0},
                },
            )
        )
        blocks.out.append(_event("message_stop", {}))

    for step in turn.steps:
        match step:
            case Text(chunks=parts):
                if blocks.open_kind != "text":
                    blocks.open("text", {"type": "text", "text": ""})
                for part in parts:
                    blocks.delta({"type": "text_delta", "text": part})
            case Reasoning(chunks=parts):
                if blocks.open_kind != "thinking":
                    blocks.open("thinking", {"type": "thinking", "thinking": ""})
                for part in parts:
                    blocks.delta({"type": "thinking_delta", "thinking": part})
            case ToolCall():
                call_id = tool_call_id(step, turn_no, tool_index, prefix="toolu_fake")
                tool_index += 1
                blocks.open(
                    "tool_use", {"type": "tool_use", "id": call_id, "name": step.name, "input": {}}
                )
                for piece in split_fragments(arguments_text(step), step.fragments):
                    blocks.delta({"type": "input_json_delta", "partial_json": piece})
                blocks.close()
            case Usage():
                pass  # reported in message_start / message_delta
            case KeepAlive(count=count):
                blocks.out.extend(_event("ping", {}) for _ in range(count))
            case Stall():
                blocks.out.append(step)
            case Disconnect():
                blocks.out.append(step)
                return blocks.out
            case Eof():
                return blocks.out
            case MidStreamError(body=body):
                if isinstance(body, str):
                    blocks.out.append(sse_frame(body, event="error"))
                else:
                    blocks.out.append(sse_frame(compact(error_event_body(body)), event="error"))
                return blocks.out
            case Finish(reason=reason):
                finish(reason)
                finished = True
            case Fail():
                raise ValueError("a fail step is only allowed as the first step of a turn")
    if not finished:
        finish(implied_finish(turn))
    return blocks.out


def json_body(turn: Turn, *, turn_no: int, model: str) -> dict[str, Any]:
    """The turn as one ``message`` object (``stream: false``)."""
    content: list[dict[str, Any]] = []
    usage = _usage_of(turn)
    stop = STOP_REASONS[implied_finish(turn)]
    tool_index = 0
    for step in turn.steps:
        match step:
            case Text(chunks=parts):
                if content and content[-1]["type"] == "text":
                    content[-1]["text"] += "".join(parts)
                else:
                    content.append({"type": "text", "text": "".join(parts)})
            case Reasoning(chunks=parts):
                content.append(
                    {
                        "type": "thinking",
                        "thinking": "".join(parts),
                        "signature": f"fake-signature-{turn_no}-{len(content)}",
                    }
                )
            case ToolCall():
                raw = arguments_text(step)
                try:
                    parsed: Any = json.loads(raw)
                except ValueError:
                    parsed = {"_raw": raw}
                content.append(
                    {
                        "type": "tool_use",
                        "id": tool_call_id(step, turn_no, tool_index, prefix="toolu_fake"),
                        "name": step.name,
                        "input": parsed,
                    }
                )
                tool_index += 1
            case Finish(reason=reason):
                stop = STOP_REASONS.get(reason, reason)
            case _:
                pass
    return {
        "id": f"msg_fake_{turn_no}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.prompt if usage else 0,
            "output_tokens": usage.completion if usage else 0,
            "cache_read_input_tokens": usage.cached if usage else 0,
            "cache_creation_input_tokens": 0,
        },
    }
