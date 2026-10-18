"""OpenAI Responses wire format for the fake model.

Same shape as ``wire_chat``. Event and field names follow the public Responses streaming
reference: ``response.created``, ``response.output_item.added`` / ``.done``,
``response.output_text.delta``, ``response.reasoning_summary_text.delta``,
``response.function_call_arguments.delta`` / ``.done``, and a terminal ``response.completed``,
``response.incomplete`` or ``response.failed``. Reasoning items carry a fake ``encrypted_content``.
"""

from __future__ import annotations

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
    CREATED,
    Action,
    arguments_text,
    compact,
    implied_finish,
    split_fragments,
    sse_frame,
    tool_call_id,
)


def usage_object(usage: Usage | None) -> dict[str, Any]:
    prompt = usage.prompt if usage else 0
    completion = usage.completion if usage else 0
    return {
        "input_tokens": prompt,
        "input_tokens_details": {"cached_tokens": usage.cached if usage else 0},
        "output_tokens": completion,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": prompt + completion,
    }


def _usage_of(turn: Turn) -> Usage | None:
    found = [s for s in turn.steps if isinstance(s, Usage)]
    return found[-1] if found else None


def _status(reason: str) -> tuple[str, dict[str, Any]]:
    """Terminal event type and response fields for one of our finish words."""
    if reason == "length":
        return "response.incomplete", {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
        }
    if reason == "content_filter":
        return "response.incomplete", {
            "status": "incomplete",
            "incomplete_details": {"reason": "content_filter"},
        }
    if reason == "error":
        return "response.failed", {
            "status": "failed",
            "error": {"code": "server_error", "message": "fake failure"},
        }
    return "response.completed", {"status": "completed"}


class _Events:
    def __init__(self, turn_no: int, model: str) -> None:
        self.turn_no = turn_no
        self.model = model
        self.seq = 0
        self.out: list[Action] = []
        self.output: list[dict[str, Any]] = []

    def emit(self, kind: str, payload: Mapping[str, Any]) -> None:
        self.out.append(
            sse_frame(compact({"type": kind, "sequence_number": self.seq, **payload}), event=kind)
        )
        self.seq += 1

    def response(self, **fields: Any) -> dict[str, Any]:
        return {
            "id": f"resp_fake_{self.turn_no}",
            "object": "response",
            "created_at": CREATED,
            "model": self.model,
            "output": list(self.output),
            **fields,
        }


def _reasoning_item(turn_no: int, index: int, text: str) -> dict[str, Any]:
    return {
        "type": "reasoning",
        "id": f"rs_fake_{turn_no}_{index}",
        "summary": [{"type": "summary_text", "text": text}],
        "encrypted_content": f"fake-sealed-{turn_no}-{index}",
    }


def stream_actions(turn: Turn, *, turn_no: int, model: str) -> list[Action]:
    ev = _Events(turn_no, model)
    usage = _usage_of(turn)
    ev.emit("response.created", {"response": ev.response(status="in_progress")})
    tool_index = 0
    finished = False

    def finish(reason: str) -> None:
        kind, fields = _status(reason)
        ev.emit(kind, {"response": ev.response(usage=usage_object(usage), **fields)})

    for step in turn.steps:
        index = len(ev.output)
        match step:
            case Text(chunks=parts):
                item_id = f"msg_fake_{turn_no}_{index}"
                ev.emit(
                    "response.output_item.added",
                    {
                        "output_index": index,
                        "item": {
                            "type": "message",
                            "id": item_id,
                            "role": "assistant",
                            "status": "in_progress",
                            "content": [],
                        },
                    },
                )
                for part in parts:
                    ev.emit(
                        "response.output_text.delta",
                        {
                            "item_id": item_id,
                            "output_index": index,
                            "content_index": 0,
                            "delta": part,
                        },
                    )
                item = {
                    "type": "message",
                    "id": item_id,
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "".join(parts), "annotations": []}],
                }
                ev.output.append(item)
                ev.emit("response.output_item.done", {"output_index": index, "item": item})
            case Reasoning(chunks=parts):
                item = _reasoning_item(turn_no, index, "".join(parts))
                ev.emit(
                    "response.output_item.added",
                    {
                        "output_index": index,
                        "item": {"type": "reasoning", "id": item["id"], "summary": []},
                    },
                )
                for part in parts:
                    ev.emit(
                        "response.reasoning_summary_text.delta",
                        {
                            "item_id": item["id"],
                            "output_index": index,
                            "summary_index": 0,
                            "delta": part,
                        },
                    )
                ev.output.append(item)
                ev.emit("response.output_item.done", {"output_index": index, "item": item})
            case ToolCall():
                call_id = tool_call_id(step, turn_no, tool_index)
                tool_index += 1
                item_id = f"fc_fake_{turn_no}_{index}"
                args = arguments_text(step)
                ev.emit(
                    "response.output_item.added",
                    {
                        "output_index": index,
                        "item": {
                            "type": "function_call",
                            "id": item_id,
                            "call_id": call_id,
                            "name": step.name,
                            "arguments": "",
                            "status": "in_progress",
                        },
                    },
                )
                for piece in split_fragments(args, step.fragments):
                    ev.emit(
                        "response.function_call_arguments.delta",
                        {"item_id": item_id, "output_index": index, "delta": piece},
                    )
                ev.emit(
                    "response.function_call_arguments.done",
                    {"item_id": item_id, "output_index": index, "arguments": args},
                )
                item = {
                    "type": "function_call",
                    "id": item_id,
                    "call_id": call_id,
                    "name": step.name,
                    "arguments": args,
                    "status": "completed",
                }
                ev.output.append(item)
                ev.emit("response.output_item.done", {"output_index": index, "item": item})
            case Usage():
                pass  # reported on the terminal event
            case KeepAlive(count=count):
                ev.out.extend(b": keep-alive\n\n" for _ in range(count))
            case Stall():
                ev.out.append(step)
            case Disconnect():
                ev.out.append(step)
                return ev.out
            case Eof():
                return ev.out
            case MidStreamError(body=body):
                if isinstance(body, str):
                    ev.out.append(sse_frame(body, event="error"))
                else:
                    inner = body.get("error", body)
                    payload = dict(inner) if isinstance(inner, Mapping) else {"message": str(inner)}
                    ev.out.append(sse_frame(compact({"type": "error", **payload}), event="error"))
                return ev.out
            case Finish(reason=reason):
                finish(reason)
                finished = True
            case Fail():
                raise ValueError("a fail step is only allowed as the first step of a turn")
    if not finished:
        finish(implied_finish(turn))
    return ev.out


def json_body(turn: Turn, *, turn_no: int, model: str) -> dict[str, Any]:
    """The turn as one response object (``stream: false``)."""
    output: list[dict[str, Any]] = []
    reason = implied_finish(turn)
    tool_index = 0
    for step in turn.steps:
        index = len(output)
        match step:
            case Text(chunks=parts):
                output.append(
                    {
                        "type": "message",
                        "id": f"msg_fake_{turn_no}_{index}",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": "".join(parts), "annotations": []}
                        ],
                    }
                )
            case Reasoning(chunks=parts):
                output.append(_reasoning_item(turn_no, index, "".join(parts)))
            case ToolCall():
                output.append(
                    {
                        "type": "function_call",
                        "id": f"fc_fake_{turn_no}_{index}",
                        "call_id": tool_call_id(step, turn_no, tool_index),
                        "name": step.name,
                        "arguments": arguments_text(step),
                        "status": "completed",
                    }
                )
                tool_index += 1
            case Finish(reason=r):
                reason = r
            case _:
                pass
    _, fields = _status(reason)
    return {
        "id": f"resp_fake_{turn_no}",
        "object": "response",
        "created_at": CREATED,
        "model": model,
        "output": output,
        "usage": usage_object(_usage_of(turn)),
        **fields,
    }
