"""OpenAI-style Chat Completions over raw HTTP (aggregators, OpenAI-compatible vendors, local
servers).

Wire facts follow OpenAI's published API description: ``POST /chat/completions`` with ``messages``,
``tools``, ``stream`` and ``stream_options.include_usage``; streamed ``chat.completion.chunk``
objects carry ``choices[].delta`` and a final usage chunk whose ``choices`` is empty; the stream
ends with the literal ``data: [DONE]``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from indus.foundation.digits import is_digits
from indus.foundation.errors import ProviderHTTPError, ProviderStreamError
from indus.providers.sse import SSEEvent, SSEItem
from indus.providers.transports import (
    RouteLike,
    clip,
    join_url,
    profile_of,
    register_transport,
    route_headers,
    secret_text,
    stream_error,
)
from indus.providers.types import (
    ChatRequest,
    Finish,
    FinishReason,
    KeepAlive,
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
    clamp_effort,
    map_finish_reason,
)

log = logging.getLogger(__name__)

API_MODE = "chat_completions"
DEFAULT_EFFORTS: tuple[str, ...] = ("low", "medium", "high")
FINISH_TABLE: dict[str, str] = {
    "stop": "stop",
    "eos": "stop",
    "end_turn": "stop",
    "length": "length",
    "max_tokens": "length",
    "tool_calls": "tool_calls",
    "function_call": "tool_calls",
    "content_filter": "content_filter",
}
_DECODER = json.JSONDecoder()
_REASONING_KEYS = ("reasoning", "reasoning_content", "reasoning_details")
_REJECTION_WORDS = (
    "unknown",
    "unrecognized",
    "unexpected",
    "not supported",
    "unsupported",
    "extra",
    "not allowed",
    "not permitted",
)


def _text_of(content: object) -> str:
    """Text of a message/delta ``content`` that may be a string or a list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "".join(parts)
    return ""


def _count(value: object) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def parse_usage(raw: Mapping[str, Any]) -> Usage:
    prompt_details = raw.get("prompt_tokens_details") or {}
    completion_details = raw.get("completion_tokens_details") or {}
    if not isinstance(prompt_details, dict):
        prompt_details = {}
    if not isinstance(completion_details, dict):
        completion_details = {}
    cached = _count(prompt_details.get("cached_tokens")) or _count(
        raw.get("prompt_cache_hit_tokens")
    )
    return Usage(
        prompt_tokens=_count(raw.get("prompt_tokens")),
        completion_tokens=_count(raw.get("completion_tokens")),
        cache_read_tokens=cached,
        cache_write_tokens=_count(prompt_details.get("cache_write_tokens")),
        reasoning_tokens=_count(completion_details.get("reasoning_tokens")),
    )


def _finish(raw: object) -> FinishReason:
    if raw is None:
        return "stop"
    return map_finish_reason(str(raw).strip().lower(), FINISH_TABLE)


def _status_from(value: object) -> int | None:
    if isinstance(value, int) and 100 <= value <= 599:
        return value
    if is_digits(value) and 100 <= int(value) <= 599:
        return int(value)
    return None


def _error_from_object(err: object, *, raw_text: str) -> ProviderStreamError:
    if isinstance(err, dict):
        message = str(err.get("message") or err.get("type") or "provider error")
        code = err.get("code")
        status = _status_from(code) or _status_from(err.get("status"))
        error_type = err.get("type")
        return stream_error(
            message,
            error_type=str(error_type) if error_type else None,
            code=str(code) if code is not None else None,
            status=status,
            raw_text=raw_text,
        )
    return stream_error(str(err) or "provider error", raw_text=raw_text)


def _arguments_closed(text: str) -> bool:
    """The call's arguments already form one complete JSON value."""
    stripped = text.strip()
    if not stripped:
        return False
    try:
        _, end = _DECODER.raw_decode(stripped)
    except ValueError:
        return False
    return end == len(stripped)


@dataclass
class _Slot:
    index: int
    id: str | None = None
    name: str = ""
    started: bool = False
    args: list[str] = field(default_factory=list[str])
    pending: list[str] = field(default_factory=list[str])


@dataclass
class _StreamState:
    slots: list[_Slot] = field(default_factory=list[_Slot])
    active: dict[int, _Slot] = field(default_factory=dict[int, _Slot])
    finish: FinishReason | None = None
    text_seen: bool = False
    refusal: list[str] = field(default_factory=list[str])

    def tool_delta(self, delta: Mapping[str, Any]) -> list[StreamEvent]:
        raw_index = delta.get("index")
        index = raw_index if isinstance(raw_index, int) else 0
        raw_id = delta.get("id")
        call_id = str(raw_id) if isinstance(raw_id, int | str) and str(raw_id) else None
        function = delta.get("function") or {}
        if not isinstance(function, dict):
            function = {}
        name = function.get("name") if isinstance(function.get("name"), str) else None
        arguments = function.get("arguments")
        if isinstance(arguments, dict):
            arguments = json.dumps(arguments, ensure_ascii=False)
        fragment = arguments if isinstance(arguments, str) else ""

        slot = self.active.get(index)
        opens_new = (
            slot is not None
            and call_id is not None
            and slot.id is not None
            and call_id != slot.id
            and bool(name)
            and (name != slot.name or _arguments_closed("".join(slot.args)))
        )
        if slot is None or opens_new:
            slot = _Slot(index=len(self.slots))
            self.slots.append(slot)
            self.active[index] = slot

        out: list[StreamEvent] = []
        id_filled = False
        if call_id and slot.id != call_id and slot.id is None:
            slot.id = call_id
            id_filled = True
        if name:
            slot.name = name
        if not slot.started and slot.name:
            slot.started = True
            out.append(ToolCallStart(slot.index, slot.id, slot.name))
            for held in slot.pending:
                out.append(ToolCallArgsDelta(slot.index, held))
            slot.pending.clear()
        elif slot.started and id_filled:
            out.append(ToolCallStart(slot.index, slot.id, slot.name))
        if fragment:
            slot.args.append(fragment)
            if slot.started:
                out.append(ToolCallArgsDelta(slot.index, fragment))
            else:
                slot.pending.append(fragment)
        return out

    def unstarted(self) -> list[StreamEvent]:
        """Calls that never got a name are still reported, so nothing silently disappears."""
        out: list[StreamEvent] = []
        for slot in self.slots:
            if not slot.started:
                slot.started = True
                out.append(ToolCallStart(slot.index, slot.id, slot.name))
                out.extend(ToolCallArgsDelta(slot.index, held) for held in slot.pending)
                slot.pending.clear()
        return out


class ChatCompletionsTransport:
    api_mode = API_MODE

    def __init__(self) -> None:
        # Base URLs that rejected ``stream_options``; requests to them go without it.
        self._no_stream_options: set[str] = set()

    # -- request ---------------------------------------------------------------------------

    def build_request(
        self, route: RouteLike, req: ChatRequest, *, profile: object = None
    ) -> httpx.Request:
        prof = profile_of(route, profile)
        model = req.model or route.model
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {k: v for k, v in m.items() if k not in _REASONING_KEYS} for m in req.messages
            ],
        }
        if req.tools:
            body["tools"] = [dict(t) for t in req.tools]
            if req.tool_choice is not None:
                choice = req.tool_choice
                body["tool_choice"] = choice if isinstance(choice, str) else dict(choice)
        body["stream"] = bool(req.stream)
        if req.stream and route.base_url not in self._no_stream_options:
            body["stream_options"] = {"include_usage": True}
        fixed = getattr(prof, "fixed_temperature", None)
        if fixed is not None:
            body["temperature"] = fixed
        elif req.temperature is not None:
            body["temperature"] = req.temperature
        supported = getattr(prof, "reasoning_efforts", None) or DEFAULT_EFFORTS
        effort = clamp_effort(req.reasoning_effort, tuple(supported))
        if effort is not None:
            body["reasoning_effort"] = effort
        max_tokens = req.max_tokens or getattr(prof, "default_max_tokens", None)
        if max_tokens:
            param = getattr(prof, "max_tokens_param", None) or "max_tokens"
            body[str(param)] = int(max_tokens)
        body.update(dict(req.extra_body))
        prepare = getattr(prof, "prepare_body", None)
        if callable(prepare):
            prepared = prepare(body, model)
            if isinstance(prepared, dict):
                body = prepared

        headers = {
            "content-type": "application/json",
            "accept": "text/event-stream" if req.stream else "application/json",
        }
        headers.update(route_headers(route))
        key = secret_text(route.api_key)
        if key:
            headers["authorization"] = f"Bearer {key}"
        content = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return httpx.Request(
            "POST", join_url(route.base_url, "chat/completions"), headers=headers, content=content
        )

    def on_request_rejected(self, route: RouteLike, error: ProviderHTTPError) -> bool:
        if error.status not in (400, 422) or route.base_url in self._no_stream_options:
            return False
        text = f"{error.message} {json.dumps(error.body, default=str)}".lower()
        if "stream_options" not in text or not any(w in text for w in _REJECTION_WORDS):
            return False
        self._no_stream_options.add(route.base_url)
        log.info("endpoint rejected stream_options; sending without it from now on")
        return True

    # -- stream ----------------------------------------------------------------------------

    def error_from_stream(self, event: SSEEvent) -> ProviderStreamError | None:
        data = event.data.strip()
        if data == "[DONE]":
            return None
        if not data:
            if event.event == "error":
                return stream_error("the provider sent an empty error event", code="error_event")
            return None
        try:
            payload = json.loads(data)
        except ValueError:
            return stream_error(clip(data), code="non_json_data", raw_text=data)
        if event.event == "error":
            err = payload.get("error", payload) if isinstance(payload, dict) else payload
            return _error_from_object(err, raw_text=data)
        if not isinstance(payload, dict):
            return stream_error(clip(data), code="non_json_data", raw_text=data)
        if payload.get("error") is not None:
            return _error_from_object(payload["error"], raw_text=data)
        if not payload.get("choices") and (
            payload.get("error_type") or payload.get("error_message")
        ):
            # some servers put a validation error into a choiceless chunk
            return _error_from_object(
                {
                    "message": payload.get("error_message") or payload.get("error_type"),
                    "type": payload.get("error_type"),
                    "code": payload.get("error_type"),
                },
                raw_text=data,
            )
        return None

    async def parse_stream(self, events: AsyncIterator[SSEItem]) -> AsyncIterator[StreamEvent]:
        state = _StreamState()
        done = False
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
            if data == "[DONE]":
                done = True
                break
            for event in self._chunk_events(json.loads(data), state):
                yield event
        for event in state.unstarted():
            yield event
        if state.refusal and not state.text_seen and not state.slots:
            # a reply that is only a refusal reads as content with a content_filter finish
            yield TextDelta("".join(state.refusal))
            if state.finish in (None, "stop"):
                state.finish = "content_filter"
        if state.finish is None:
            if not done:
                raise stream_error(
                    "the stream ended before the model finished its reply",
                    error_type="incomplete_stream",
                    code="incomplete_stream",
                )
            state.finish = "tool_calls" if state.slots else "stop"
        yield Finish(state.finish)

    def _chunk_events(self, payload: Mapping[str, Any], state: _StreamState) -> list[StreamEvent]:
        out: list[StreamEvent] = []
        choices = payload.get("choices") or []
        choice: Mapping[str, Any] | None = None
        if isinstance(choices, list):
            for candidate in choices:
                if isinstance(candidate, dict) and candidate.get("index", 0) == 0:
                    choice = candidate
                    break
        if choice is not None:
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                delta = {}
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                out.append(ReasoningDelta(reasoning))
            details = delta.get("reasoning_details")
            if isinstance(details, list):
                out.extend(ReasoningDetail(dict(d)) for d in details if isinstance(d, dict))
            text = _text_of(delta.get("content"))
            if text:
                state.text_seen = True
                out.append(TextDelta(text))
            refusal = delta.get("refusal")
            if isinstance(refusal, str) and refusal:
                state.refusal.append(refusal)
            calls = delta.get("tool_calls")
            if isinstance(calls, list):
                for call in calls:
                    if isinstance(call, dict):
                        out.extend(state.tool_delta(call))
            raw_finish = choice.get("finish_reason")
            if raw_finish is not None:
                if str(raw_finish).strip().lower() == "error":
                    message = text or "the provider ended the reply with finish_reason=error"
                    raise _error_from_object(
                        choice.get("error") or {"message": message, "code": "finish_reason_error"},
                        raw_text=json.dumps(payload, default=str),
                    )
                state.finish = _finish(raw_finish)
        usage = payload.get("usage")
        if isinstance(usage, dict):
            out.append(UsageReport(parse_usage(usage)))
        return out

    # -- non-streaming ---------------------------------------------------------------------

    def parse_response(self, payload: Mapping[str, Any]) -> NormalizedResponse:
        if payload.get("error") is not None and not payload.get("choices"):
            raise _error_from_object(payload["error"], raw_text=json.dumps(payload, default=str))
        choices = payload.get("choices") or []
        choice = choices[0] if isinstance(choices, list) and choices else {}
        message = choice.get("message") or {}
        content = message.get("content")
        text = _text_of(content) if content is not None else None
        reasoning = message.get("reasoning_content") or message.get("reasoning")
        details = message.get("reasoning_details")
        calls: list[ToolCall] = []
        for position, call in enumerate(message.get("tool_calls") or []):
            if not isinstance(call, dict):
                continue
            function = call.get("function") or {}
            name = function.get("name")
            if not isinstance(name, str) or not name:
                continue
            arguments = function.get("arguments")
            if isinstance(arguments, dict):
                arguments = json.dumps(arguments, ensure_ascii=False)
            calls.append(
                ToolCall(
                    id=str(call.get("id") or f"call_{position}"),
                    name=name,
                    arguments=arguments if isinstance(arguments, str) and arguments else "{}",
                )
            )
        finish = _finish(choice.get("finish_reason"))
        refusal = message.get("refusal")
        if isinstance(refusal, str) and refusal.strip() and not (text or "").strip() and not calls:
            text = refusal
            if finish == "stop":
                finish = "content_filter"
        usage = payload.get("usage")
        provider_data: dict[str, Any] = {}
        for key in ("id", "model"):
            if isinstance(payload.get(key), str):
                provider_data[key] = payload[key]
        return NormalizedResponse(
            content=text if text else None,
            tool_calls=tuple(calls),
            finish_reason=finish,
            reasoning=reasoning if isinstance(reasoning, str) and reasoning else None,
            reasoning_details=tuple(dict(d) for d in details if isinstance(d, dict))
            if isinstance(details, list)
            else (),
            usage=parse_usage(usage) if isinstance(usage, dict) else None,
            provider_data=provider_data,
        )


register_transport(API_MODE, ChatCompletionsTransport)
