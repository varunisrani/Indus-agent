"""Google Gemini ``generateContent`` wire format for the fake model.

``stream_actions`` gives ``streamGenerateContent?alt=sse`` chunks (each a whole
``GenerateContentResponse``); ``json_body`` gives the ``generateContent`` answer. Field names follow
the public Gemini API reference: ``candidates[].content.parts`` (``text``, ``thought``,
``functionCall`` with ``thoughtSignature``), ``finishReason``, ``usageMetadata``.
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
from tests.fakes.model.wire_chat import Action, arguments_text, compact, implied_finish, sse_frame

FINISH_REASONS = {
    "stop": "STOP",
    "tool_calls": "STOP",
    "length": "MAX_TOKENS",
    "content_filter": "SAFETY",
}


def usage_object(usage: Usage | None) -> dict[str, Any]:
    prompt = usage.prompt if usage else 0
    completion = usage.completion if usage else 0
    return {
        "promptTokenCount": prompt,
        "candidatesTokenCount": completion,
        "totalTokenCount": prompt + completion,
        "cachedContentTokenCount": usage.cached if usage else 0,
    }


def _usage_of(turn: Turn) -> Usage | None:
    found = [s for s in turn.steps if isinstance(s, Usage)]
    return found[-1] if found else None


def _args(call: ToolCall) -> Any:
    text = arguments_text(call)
    try:
        return json.loads(text)
    except ValueError:
        return {"_raw": text}


def _chunk(model: str, parts: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    candidate: dict[str, Any] = {"content": {"role": "model", "parts": parts}, "index": 0}
    candidate.update(extra.pop("candidate", {}))
    return {"candidates": [candidate], "modelVersion": model, **extra}


def stream_actions(turn: Turn, *, turn_no: int, model: str) -> list[Action]:
    out: list[Action] = []
    usage = _usage_of(turn)
    finished = False
    calls = 0

    def finish(reason: str) -> None:
        out.append(
            sse_frame(
                compact(
                    _chunk(
                        model,
                        [{"text": ""}],
                        candidate={"finishReason": FINISH_REASONS.get(reason, reason.upper())},
                        usageMetadata=usage_object(usage),
                    )
                )
            )
        )

    for step in turn.steps:
        match step:
            case Text(chunks=parts):
                out.extend(sse_frame(compact(_chunk(model, [{"text": p}]))) for p in parts)
            case Reasoning(chunks=parts):
                out.extend(
                    sse_frame(compact(_chunk(model, [{"text": p, "thought": True}]))) for p in parts
                )
            case ToolCall():
                part = {
                    "functionCall": {"name": step.name, "args": _args(step)},
                    "thoughtSignature": f"fake-signature-{turn_no}-{calls}",
                }
                calls += 1
                out.append(sse_frame(compact(_chunk(model, [part]))))
            case Usage():
                pass  # reported on the last chunk
            case KeepAlive(count=count):
                out.extend(b": keep-alive\n\n" for _ in range(count))
            case Stall():
                out.append(step)
            case Disconnect():
                out.append(step)
                return out
            case Eof():
                return out
            case MidStreamError(body=body):
                if isinstance(body, str):
                    out.append(sse_frame(body))
                else:
                    inner = body.get("error", body)
                    data = dict(inner) if isinstance(inner, Mapping) else {"message": str(inner)}
                    out.append(sse_frame(compact({"error": data})))
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
    parts: list[dict[str, Any]] = []
    reason = implied_finish(turn)
    calls = 0
    for step in turn.steps:
        match step:
            case Text(chunks=chunks):
                parts.append({"text": "".join(chunks)})
            case Reasoning(chunks=chunks):
                parts.append({"text": "".join(chunks), "thought": True})
            case ToolCall():
                parts.append(
                    {
                        "functionCall": {"name": step.name, "args": _args(step)},
                        "thoughtSignature": f"fake-signature-{turn_no}-{calls}",
                    }
                )
                calls += 1
            case Finish(reason=r):
                reason = r
            case _:
                pass
    return _chunk(
        model,
        parts,
        candidate={"finishReason": FINISH_REASONS.get(reason, reason.upper())},
        usageMetadata=usage_object(_usage_of(turn)),
    )
