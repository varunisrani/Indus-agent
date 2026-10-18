"""Anthropic Messages over raw HTTP (the official API and Anthropic-wire compatible hosts).

Wire facts follow Anthropic's published Messages and streaming references: ``POST /v1/messages``
with ``anthropic-version``, a top-level ``system``, content blocks (``text``, ``image``,
``tool_use``, ``tool_result``, ``thinking``), and a stream of ``message_start``, per-block
``content_block_start`` / ``content_block_delta`` / ``content_block_stop``, ``message_delta``
(cumulative usage) and ``message_stop``, with ``ping`` and ``error`` events possible anywhere.

Prompt-cache markers (``cache_control``, placed by ``core.cache_plan``) ride along: a marked
content part keeps its marker on the block it becomes, a marked system part turns ``system`` into
a list of text blocks, and a marker on a message itself (a tool result, or a turn without text)
lands on that message's last block.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from indus.foundation.errors import ProviderHTTPError, ProviderStreamError
from indus.providers.sse import SSEEvent, SSEItem
from indus.providers.transports import (
    RouteLike,
    join_url,
    profile_of,
    register_transport,
    route_headers,
    secret_text,
    stream_error,
)
from indus.providers.types import (
    EFFORT_LADDER,
    ChatRequest,
    Finish,
    FinishReason,
    KeepAlive,
    Message,
    NormalizedResponse,
    ReasoningDelta,
    ReasoningDetail,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallArgsDelta,
    ToolCallStart,
    Usage,
    UsageReport,
    map_finish_reason,
    normalize_effort,
)

log = logging.getLogger(__name__)

API_MODE = "anthropic_messages"
API_VERSION = "2023-06-01"
OFFICIAL_HOST = "api.anthropic.com"
DEFAULT_MAX_TOKENS = 8192
THINKING_HEADROOM = 4096
DEFAULT_BUDGET = 8000
BUDGETS: dict[str, int] = {
    "minimal": 4000,
    "low": 4000,
    "medium": 8000,
    "high": 16000,
    "xhigh": 32000,
    "max": 32000,
    "ultra": 32000,
}
ADAPTIVE_EFFORTS: dict[str, str] = {
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
    "ultra": "max",
}
STOP_TABLE: dict[str, str] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "refusal": "content_filter",
}
_THINKING_TYPES = ("thinking", "redacted_thinking")
_TOOL_ID_BAD = re.compile(r"[^A-Za-z0-9_-]")


# -- request conversion ------------------------------------------------------------------------


def wire_tool_id(raw: object) -> str:
    """Tool ids the API accepts (``[A-Za-z0-9_-]+``); the same input always maps the same way."""
    text = _TOOL_ID_BAD.sub("_", str(raw or ""))
    return text or "tool_0"


def _blank(text: object) -> bool:
    return not isinstance(text, str) or not text.strip()


def _image_block(url: str) -> dict[str, Any]:
    if url.startswith("data:"):
        header, _, data = url.partition(",")
        media_type = header[len("data:") :].split(";", 1)[0].strip() or "image/png"
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        }
    return {"type": "image", "source": {"type": "url", "url": url}}


CACHE_KEY = "cache_control"


def _with_marker(block: dict[str, Any], source: Mapping[str, Any]) -> dict[str, Any]:
    marker = source.get(CACHE_KEY)
    if isinstance(marker, dict):
        block[CACHE_KEY] = dict(marker)
    return block


def _content_blocks(content: object) -> list[dict[str, Any]]:
    """OpenAI-style content (string or parts) → Anthropic blocks, blank text dropped."""
    if content is None:
        return []
    if isinstance(content, str):
        return [] if _blank(content) else [{"type": "text", "text": content}]
    blocks: list[dict[str, Any]] = []
    if not isinstance(content, list):
        return blocks
    for part in content:
        if isinstance(part, str):
            if not _blank(part):
                blocks.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text":
            if not _blank(part.get("text")):
                blocks.append(_with_marker({"type": "text", "text": part["text"]}, part))
        elif kind == "image_url":
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            if isinstance(url, str) and url:
                blocks.append(_with_marker(_image_block(url), part))
        elif kind in ("image", "document"):
            blocks.append(copy.deepcopy(part))
    return blocks


def _mark_last(blocks: list[dict[str, Any]], message: Mapping[str, Any]) -> list[dict[str, Any]]:
    """A marker set on the message itself goes on its last block."""
    if blocks and isinstance(message.get(CACHE_KEY), dict):
        blocks[-1] = _with_marker(dict(blocks[-1]), message)
    return blocks


def _system_blocks(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    if isinstance(content, str):
        blocks = [] if _blank(content) else [{"type": "text", "text": content}]
    else:
        blocks = [b for b in _content_blocks(content) if b["type"] == "text"]
    return _mark_last(blocks, message)


def tool_input(arguments: object, *, tool: str = "?") -> dict[str, Any]:
    """Arguments JSON text → the ``input`` object; anything else becomes ``{"_raw": text}``."""
    if isinstance(arguments, dict):
        return dict(arguments)
    text = arguments if isinstance(arguments, str) else ""
    if not text.strip():
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    if isinstance(value, dict):
        return value
    log.warning("tool call %s has arguments that are not a JSON object; sent wrapped", tool)
    return {"_raw": text}


def _thinking_replay(details: object) -> list[dict[str, Any]]:
    """Thinking blocks stored in ``reasoning_details``, copied exactly for replay."""
    if not isinstance(details, list):
        return []
    out: list[dict[str, Any]] = []
    for detail in details:
        if not isinstance(detail, dict) or detail.get("type") not in _THINKING_TYPES:
            continue
        if detail["type"] == "thinking":
            block = {"type": "thinking", "thinking": detail.get("thinking", "")}
            if detail.get("signature"):
                block["signature"] = detail["signature"]
            out.append(block)
        elif detail.get("data"):
            out.append({"type": "redacted_thinking", "data": detail["data"]})
    return out


def _assistant_blocks(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    blocks = _thinking_replay(message.get("reasoning_details"))
    blocks.extend(_content_blocks(message.get("content")))
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        name = str(function.get("name") or "")
        blocks.append(
            {
                "type": "tool_use",
                "id": wire_tool_id(call.get("id")),
                "name": name,
                "input": tool_input(function.get("arguments"), tool=name),
            }
        )
    return blocks


def _tool_result(message: Mapping[str, Any]) -> dict[str, Any]:
    block: dict[str, Any] = _with_marker(
        {"type": "tool_result", "tool_use_id": wire_tool_id(message.get("tool_call_id"))}, message
    )
    content = message.get("content")
    if isinstance(content, str):
        if content.strip():
            block["content"] = content
    elif isinstance(content, list):
        inner = [b for b in _content_blocks(content) if b["type"] in ("text", "image")]
        if inner:
            block["content"] = inner
    elif content is not None:
        block["content"] = json.dumps(content, ensure_ascii=False)
    if message.get("is_error") is True:
        block["is_error"] = True
    return block


type System = str | list[dict[str, Any]] | None


def convert_messages(messages: Iterable[Message]) -> tuple[System, list[dict[str, Any]]]:
    """OpenAI-shaped history → (``system`` or None, Anthropic ``messages``).

    ``system`` is the joined text, or a list of text blocks when a block carries a cache marker.
    """
    system_parts: list[list[dict[str, Any]]] = []
    turns: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role in ("system", "developer"):
            blocks = _system_blocks(message)
            if blocks:
                system_parts.append(blocks)
            continue
        if role == "tool":
            result = _tool_result(message)
            last = turns[-1] if turns else None
            if (
                last is not None
                and last["role"] == "user"
                and isinstance(last["content"], list)
                and last["content"]
                and all(b.get("type") == "tool_result" for b in last["content"])
            ):
                last["content"].append(result)
            else:
                turns.append({"role": "user", "content": [result]})
            continue
        if role == "assistant":
            blocks = _mark_last(_assistant_blocks(message), message)
            if blocks:
                turns.append({"role": "assistant", "content": blocks})
            continue
        content = message.get("content")
        if isinstance(content, str) and CACHE_KEY not in message:
            if content.strip():
                turns.append({"role": "user", "content": content})
        else:
            blocks = _mark_last(_content_blocks(content), message)
            if blocks:
                turns.append({"role": "user", "content": blocks})
    merged: list[dict[str, Any]] = []
    for turn in turns:
        if merged and merged[-1]["role"] == turn["role"]:
            later = _as_blocks(turn["content"])
            if turn["role"] == "assistant":
                # thinking is signed against its own turn; inside a merged turn it is invalid
                later = [b for b in later if b.get("type") not in _THINKING_TYPES]
            merged[-1]["content"] = _as_blocks(merged[-1]["content"]) + later
        else:
            merged.append(turn)
    if not system_parts:
        return None, merged
    if any(CACHE_KEY in b for blocks in system_parts for b in blocks):
        return [b for blocks in system_parts for b in blocks], merged
    texts = ("\n".join(b["text"] for b in blocks) for blocks in system_parts)
    return "\n\n".join(texts), merged


def _as_blocks(content: object) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return list(content) if isinstance(content, list) else []


def convert_tools(tools: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for tool in tools:
        function = tool.get("function") if tool.get("type", "function") == "function" else None
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "")
        if not name or name in seen:
            continue
        seen.add(name)
        schema = function.get("parameters")
        entry: dict[str, Any] = {
            "name": name,
            "input_schema": dict(schema)
            if isinstance(schema, dict)
            else {"type": "object", "properties": {}},
        }
        if function.get("description"):
            entry["description"] = str(function["description"])
        out.append(entry)
    return out


def convert_tool_choice(choice: object) -> dict[str, Any] | None:
    if choice is None:
        return None
    if isinstance(choice, str):
        table = {
            "auto": {"type": "auto"},
            "required": {"type": "any"},
            "any": {"type": "any"},
            "none": {"type": "none"},
        }
        return table.get(choice, {"type": "tool", "name": choice})
    if isinstance(choice, dict):
        function = choice.get("function")
        if isinstance(function, dict) and function.get("name"):
            return {"type": "tool", "name": str(function["name"])}
    return None


def _is_adaptive(profile: object, model: str) -> bool:
    prefixes = getattr(profile, "adaptive_thinking_models", None) or ()
    lowered = model.lower()
    return any(lowered.startswith(str(p).lower()) for p in prefixes)


def apply_thinking(body: dict[str, Any], effort: str | None, *, adaptive: bool) -> None:
    """Add thinking parameters for ``effort`` (a ladder level) to ``body`` in place."""
    level = normalize_effort(effort)
    if level is None:
        return
    if adaptive:
        body["thinking"] = {"type": "adaptive"}
        body["output_config"] = {"effort": ADAPTIVE_EFFORTS.get(level, "medium")}
        return
    budget = BUDGETS.get(level, DEFAULT_BUDGET) if level in EFFORT_LADDER else DEFAULT_BUDGET
    body["thinking"] = {"type": "enabled", "budget_tokens": budget}
    body["temperature"] = 1
    body["max_tokens"] = max(int(body.get("max_tokens") or 0), budget + THINKING_HEADROOM)


def messages_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    return join_url(base, "messages" if base.endswith("/v1") else "v1/messages")


# -- stream state ------------------------------------------------------------------------------


@dataclass
class _Block:
    kind: str
    tool_index: int = -1
    thinking: list[str] = field(default_factory=list[str])
    signature: str = ""
    args_seen: bool = False


@dataclass
class _UsageCounts:
    input: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0

    def update(self, raw: object) -> bool:
        if not isinstance(raw, dict):
            return False
        changed = False
        for key, attr in (
            ("input_tokens", "input"),
            ("cache_read_input_tokens", "cache_read"),
            ("cache_creation_input_tokens", "cache_write"),
            ("output_tokens", "output"),
        ):
            value = raw.get(key)
            if isinstance(value, int) and value >= 0:
                setattr(self, attr, value)
                changed = True
        return changed

    def usage(self) -> Usage:
        return Usage(
            prompt_tokens=self.input + self.cache_read + self.cache_write,
            completion_tokens=self.output,
            cache_read_tokens=self.cache_read,
            cache_write_tokens=self.cache_write,
        )


def _error_body(payload: object, raw_text: str) -> ProviderStreamError:
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, dict):
        error_type = err.get("type")
        return stream_error(
            str(err.get("message") or error_type or "provider error"),
            error_type=str(error_type) if error_type else None,
            code=str(error_type) if error_type else None,
            raw_text=raw_text,
        )
    return stream_error(raw_text or "provider error", code="error_event", raw_text=raw_text)


class AnthropicTransport:
    api_mode = API_MODE

    def build_request(
        self, route: RouteLike, req: ChatRequest, *, profile: object = None
    ) -> httpx.Request:
        prof = profile_of(route, profile)
        model = req.model or route.model
        system, messages = convert_messages(req.messages)
        max_tokens = (
            req.max_tokens or getattr(prof, "default_max_tokens", None) or DEFAULT_MAX_TOKENS
        )
        body: dict[str, Any] = {"model": model, "max_tokens": int(max_tokens)}
        if system is not None:
            body["system"] = system
        body["messages"] = messages
        tools = convert_tools(req.tools)
        if tools:
            body["tools"] = tools
            choice = convert_tool_choice(req.tool_choice)
            if choice is not None:
                body["tool_choice"] = choice
        body["stream"] = bool(req.stream)
        fixed = getattr(prof, "fixed_temperature", None)
        if fixed is not None:
            body["temperature"] = fixed
        elif req.temperature is not None:
            body["temperature"] = req.temperature
        apply_thinking(body, req.reasoning_effort, adaptive=_is_adaptive(prof, model))
        body.update(dict(req.extra_body))
        prepare = getattr(prof, "prepare_body", None)
        if callable(prepare):
            prepared = prepare(body, model)
            if isinstance(prepared, dict):
                body = prepared

        headers = {
            "content-type": "application/json",
            "accept": "text/event-stream" if req.stream else "application/json",
            "anthropic-version": API_VERSION,
        }
        headers.update(route_headers(route))
        key = secret_text(route.api_key)
        if key:
            official = httpx.URL(route.base_url).host == OFFICIAL_HOST
            if not official and getattr(prof, "bearer_auth", False):
                headers["authorization"] = f"Bearer {key}"
            else:
                headers["x-api-key"] = key
        content = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return httpx.Request("POST", messages_url(route.base_url), headers=headers, content=content)

    def on_request_rejected(self, route: RouteLike, error: ProviderHTTPError) -> bool:
        return False

    def error_from_stream(self, event: SSEEvent) -> ProviderStreamError | None:
        data = event.data.strip()
        try:
            payload = json.loads(data) if data else None
        except ValueError:
            return stream_error(data, code="non_json_data", raw_text=data)
        if event.event == "error" or (isinstance(payload, dict) and payload.get("type") == "error"):
            return _error_body(payload, data)
        return None

    async def parse_stream(self, events: AsyncIterator[SSEItem]) -> AsyncIterator[StreamEvent]:
        blocks: dict[int, _Block] = {}
        tools = 0
        counts = _UsageCounts()
        stop_reason: str | None = None
        stopped = False
        async for item in events:
            if isinstance(item, KeepAlive):
                yield item
                continue
            error = self.error_from_stream(item)
            if error is not None:
                raise error
            data = item.data.strip()
            if not data:
                yield KeepAlive()
                continue
            payload = json.loads(data)
            if not isinstance(payload, dict):
                continue
            kind = payload.get("type") or item.event
            if kind == "ping":
                yield KeepAlive("ping")
            elif kind == "message_start":
                message = payload.get("message") or {}
                if isinstance(message, dict) and counts.update(message.get("usage")):
                    yield UsageReport(counts.usage())
            elif kind == "content_block_start":
                index = payload.get("index", 0)
                start = payload.get("content_block") or {}
                block_type = start.get("type", "")
                block = _Block(kind=block_type)
                blocks[index] = block
                if block_type == "text" and start.get("text"):
                    yield TextDelta(start["text"])
                elif block_type == "tool_use":
                    block.tool_index = tools
                    tools += 1
                    yield ToolCallStart(
                        block.tool_index, start.get("id"), str(start.get("name") or "")
                    )
                    if isinstance(start.get("input"), dict) and start["input"]:
                        block.args_seen = True
                        yield ToolCallArgsDelta(block.tool_index, json.dumps(start["input"]))
                elif block_type == "thinking":
                    if start.get("thinking"):
                        block.thinking.append(start["thinking"])
                        yield ReasoningDelta(start["thinking"])
                    block.signature = str(start.get("signature") or "")
                elif block_type == "redacted_thinking":
                    yield ReasoningDetail(
                        {"type": "redacted_thinking", "data": start.get("data", "")}
                    )
            elif kind == "content_block_delta":
                block = blocks.get(payload.get("index", 0))
                delta = payload.get("delta") or {}
                delta_type = delta.get("type")
                if delta_type == "text_delta":
                    if delta.get("text"):
                        yield TextDelta(delta["text"])
                elif delta_type == "input_json_delta" and block is not None:
                    fragment = delta.get("partial_json") or ""
                    if fragment:
                        block.args_seen = True
                        yield ToolCallArgsDelta(block.tool_index, fragment)
                elif delta_type == "thinking_delta":
                    if delta.get("thinking"):
                        if block is not None:
                            block.thinking.append(delta["thinking"])
                        yield ReasoningDelta(delta["thinking"])
                elif delta_type == "signature_delta" and block is not None:
                    block.signature += str(delta.get("signature") or "")
            elif kind == "content_block_stop":
                block = blocks.get(payload.get("index", 0))
                if block is None:
                    continue
                if block.kind == "thinking":
                    detail: dict[str, Any] = {
                        "type": "thinking",
                        "thinking": "".join(block.thinking),
                    }
                    if block.signature:
                        detail["signature"] = block.signature
                    yield ReasoningDetail(detail)
                elif block.kind == "tool_use" and not block.args_seen:
                    yield ToolCallArgsDelta(block.tool_index, "{}")
            elif kind == "message_delta":
                delta = payload.get("delta") or {}
                if isinstance(delta, dict) and delta.get("stop_reason"):
                    stop_reason = str(delta["stop_reason"])
                if counts.update(payload.get("usage")):
                    yield UsageReport(counts.usage())
            elif kind == "message_stop":
                stopped = True
                break
            # any other event type is tolerated and skipped
        if not stopped and stop_reason is None:
            raise stream_error(
                "the stream ended before the model finished its reply",
                error_type="incomplete_stream",
                code="incomplete_stream",
            )
        yield Finish(self._finish(stop_reason))

    @staticmethod
    def _finish(stop_reason: str | None) -> FinishReason:
        return map_finish_reason(stop_reason, STOP_TABLE)

    def parse_response(self, payload: Mapping[str, Any]) -> NormalizedResponse:
        if payload.get("type") == "error":
            raise _error_body(payload, json.dumps(payload, default=str))
        text: list[str] = []
        reasoning: list[str] = []
        details: list[dict[str, Any]] = []
        calls: list[ToolCall] = []
        for block in payload.get("content") or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                text.append(str(block.get("text") or ""))
            elif kind == "thinking":
                reasoning.append(str(block.get("thinking") or ""))
                details.append(_thinking_replay([block])[0])
            elif kind == "redacted_thinking":
                details.extend(_thinking_replay([block]))
            elif kind == "tool_use":
                calls.append(
                    ToolCall(
                        id=str(block.get("id") or f"call_{len(calls)}"),
                        name=str(block.get("name") or ""),
                        arguments=json.dumps(block.get("input") or {}, ensure_ascii=False),
                    )
                )
        counts = _UsageCounts()
        has_usage = counts.update(payload.get("usage"))
        provider_data: dict[str, Any] = {}
        for key in ("id", "model"):
            if isinstance(payload.get(key), str):
                provider_data[key] = payload[key]
        joined = "".join(text)
        return NormalizedResponse(
            content=joined if joined else None,
            tool_calls=tuple(calls),
            finish_reason=self._finish(payload.get("stop_reason")),
            reasoning="".join(reasoning) or None,
            reasoning_details=tuple(details),
            usage=counts.usage() if has_usage else None,
            provider_data=provider_data,
        )


register_transport(API_MODE, AnthropicTransport)
