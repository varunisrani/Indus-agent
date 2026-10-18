"""AWS Bedrock Converse wire format for the fake model.

``stream_actions`` gives binary event-stream frames (``/converse-stream``); ``json_body`` gives
the ``/converse`` JSON answer. Event names follow the public ConverseStream reference:
``messageStart``, ``contentBlockStart`` (tool use only), ``contentBlockDelta`` (``text``,
``toolUse.input`` fragments, ``reasoningContent``), ``contentBlockStop``, ``messageStop`` with
``stopReason``, ``metadata`` with usage; failures are ``exception`` messages.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from indus.providers.eventstream import encode_message
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
    tool_call_id,
)

STOP_REASONS = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "content_filter": "content_filtered",
}


def event(name: str, payload: Mapping[str, Any]) -> bytes:
    headers = {
        ":event-type": name,
        ":content-type": "application/json",
        ":message-type": "event",
    }
    return encode_message(headers, compact(dict(payload)).encode("utf-8"))


def exception(kind: str, message: str) -> bytes:
    headers = {
        ":exception-type": kind,
        ":content-type": "application/json",
        ":message-type": "exception",
    }
    return encode_message(headers, compact({"message": message}).encode("utf-8"))


def usage_object(usage: Usage | None) -> dict[str, Any]:
    prompt = usage.prompt if usage else 0
    cached = usage.cached if usage else 0
    completion = usage.completion if usage else 0
    return {
        "inputTokens": prompt - cached,
        "outputTokens": completion,
        "totalTokens": prompt + completion,
        "cacheReadInputTokens": cached,
        "cacheWriteInputTokens": 0,
    }


def _usage_of(turn: Turn) -> Usage | None:
    found = [s for s in turn.steps if isinstance(s, Usage)]
    return found[-1] if found else None


def _input(call: ToolCall) -> Any:
    text = arguments_text(call)
    try:
        return json.loads(text)
    except ValueError:
        return {"_raw": text}


def stream_actions(turn: Turn, *, turn_no: int, model: str) -> list[Action]:
    out: list[Action] = [event("messageStart", {"role": "assistant"})]
    usage = _usage_of(turn)
    block = 0
    tool_index = 0
    finished = False

    def finish(reason: str) -> None:
        out.append(event("messageStop", {"stopReason": STOP_REASONS.get(reason, reason)}))
        out.append(event("metadata", {"usage": usage_object(usage), "metrics": {"latencyMs": 12}}))

    for step in turn.steps:
        match step:
            case Text(chunks=parts):
                for part in parts:
                    out.append(
                        event(
                            "contentBlockDelta",
                            {"contentBlockIndex": block, "delta": {"text": part}},
                        )
                    )
                out.append(event("contentBlockStop", {"contentBlockIndex": block}))
                block += 1
            case Reasoning(chunks=parts):
                for part in parts:
                    out.append(
                        event(
                            "contentBlockDelta",
                            {
                                "contentBlockIndex": block,
                                "delta": {"reasoningContent": {"text": part}},
                            },
                        )
                    )
                signature = {"reasoningContent": {"signature": f"fake-signature-{turn_no}-{block}"}}
                out.append(
                    event("contentBlockDelta", {"contentBlockIndex": block, "delta": signature})
                )
                out.append(event("contentBlockStop", {"contentBlockIndex": block}))
                block += 1
            case ToolCall():
                call_id = tool_call_id(step, turn_no, tool_index, prefix="tooluse_fake")
                tool_index += 1
                start = {"toolUse": {"toolUseId": call_id, "name": step.name}}
                out.append(event("contentBlockStart", {"contentBlockIndex": block, "start": start}))
                for piece in split_fragments(arguments_text(step), step.fragments):
                    out.append(
                        event(
                            "contentBlockDelta",
                            {"contentBlockIndex": block, "delta": {"toolUse": {"input": piece}}},
                        )
                    )
                out.append(event("contentBlockStop", {"contentBlockIndex": block}))
                block += 1
            case Usage():
                pass  # reported in metadata
            case KeepAlive():
                pass  # this wire has no keep-alive frame
            case Stall():
                out.append(step)
            case Disconnect():
                out.append(step)
                return out
            case Eof():
                return out
            case MidStreamError(body=body):
                if isinstance(body, str):
                    out.append(exception("internalServerException", body))
                else:
                    inner = body.get("error", body)
                    data = dict(inner) if isinstance(inner, Mapping) else {}
                    kind = str(data.get("type") or "internalServerException")
                    out.append(exception(kind, str(data.get("message") or "fake failure")))
                return out
            case Finish(reason=reason):
                finish(reason)
                finished = True
            case Fail():
                raise ValueError("a fail step is only allowed as the first step of a turn")
    if not finished:
        finish(implied_finish(turn))
    return out


def json_body(turn: Turn, *, turn_no: int, model: str) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    reason = implied_finish(turn)
    tool_index = 0
    for step in turn.steps:
        match step:
            case Text(chunks=parts):
                content.append({"text": "".join(parts)})
            case Reasoning(chunks=parts):
                content.append(
                    {
                        "reasoningContent": {
                            "reasoningText": {
                                "text": "".join(parts),
                                "signature": f"fake-signature-{turn_no}-{len(content)}",
                            }
                        }
                    }
                )
            case ToolCall():
                content.append(
                    {
                        "toolUse": {
                            "toolUseId": tool_call_id(
                                step, turn_no, tool_index, prefix="tooluse_fake"
                            ),
                            "name": step.name,
                            "input": _input(step),
                        }
                    }
                )
                tool_index += 1
            case Finish(reason=r):
                reason = r
            case _:
                pass
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": STOP_REASONS.get(reason, reason),
        "usage": usage_object(_usage_of(turn)),
        "metrics": {"latencyMs": 12},
    }
