"""OpenAI Responses API over raw HTTP (the ``responses`` API mode).

Wire facts follow OpenAI's published Responses reference: ``POST /responses`` with ``model``,
``instructions``, ``input`` items (``message``, ``function_call``, ``function_call_output``,
``reasoning``), flat function tools, ``max_output_tokens``, ``reasoning {effort, summary}``,
``include: ["reasoning.encrypted_content"]`` and ``store: false``. A streamed reply is a series of
typed events (``response.output_text.delta``, ``response.function_call_arguments.delta``,
``response.output_item.done``, ...) ending in ``response.completed``, ``response.incomplete`` or
``response.failed``; an ``error`` event may arrive instead.

Reasoning items come back sealed (``encrypted_content``). They are kept, exactly as received, in
the assistant row's ``reasoning_details`` as ``{"type": "responses.reasoning", "model": ...,
"item": {...}}`` and sent back in arrival order before that turn's text and calls, only to the
model that produced them.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
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
    clamp_effort,
)

log = logging.getLogger(__name__)

API_MODE = "responses"
DEFAULT_EFFORTS: tuple[str, ...] = ("minimal", "low", "medium", "high")
REPLAY_TYPE = "responses.reasoning"
MAX_CALL_ID = 64
# Reasoning-only incomplete replies in a row (per model) before the call counts as failed.
INCOMPLETE_LIMIT = 3
INCLUDE_ENCRYPTED = "reasoning.encrypted_content"

_TERMINAL = frozenset(
    {"response.completed", "response.incomplete", "response.failed", "response.cancelled"}
)
_TEXT_DELTAS = frozenset({"response.output_text.delta", "response.refusal.delta"})
_REASONING_DELTAS = frozenset(
    {"response.reasoning_summary_text.delta", "response.reasoning_text.delta"}
)
_ID_REJECTION_WORDS = ("not found", "not persisted", "invalid item", "item id", "store")


class ResponseCancelled(ProviderStreamError):
    """The provider reports the response as cancelled: the turn was interrupted, not answered."""

    interrupted = True


# -- ids -------------------------------------------------------------------------------------------


def wire_call_id(raw: object) -> str:
    """A ``call_id`` the API accepts (at most 64 characters); the same input maps the same way,
    so a call and its output always agree."""
    text = str(raw or "").strip() or "call_0"
    if len(text) <= MAX_CALL_ID:
        return text
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    return f"call_{digest[:32]}"


# -- request conversion ------------------------------------------------------------------------


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                out.append(part["text"])
        return "".join(out)
    return ""


def _user_content(content: object) -> str | list[dict[str, Any]] | None:
    """User content as a string or as typed input parts; None when nothing is left."""
    if isinstance(content, str):
        return content if content.strip() else None
    if not isinstance(content, list):
        return None
    parts: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            if part.strip():
                parts.append({"type": "input_text", "text": part})
            continue
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text" and isinstance(part.get("text"), str) and part["text"].strip():
            parts.append({"type": "input_text", "text": part["text"]})
        elif kind == "image_url":
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            if isinstance(url, str) and url:
                parts.append({"type": "input_image", "image_url": url})
        elif kind in ("input_text", "input_image", "input_file"):
            parts.append(copy.deepcopy(part))
    return parts or None


def _arguments(raw: object) -> str:
    if isinstance(raw, str):
        return raw if raw.strip() else "{}"
    if raw is None:
        return "{}"
    return json.dumps(raw, ensure_ascii=False)


def _replayable(detail: object, model: str) -> dict[str, Any] | None:
    """The stored reasoning item of ``detail`` when it may be sent to ``model``."""
    if not isinstance(detail, dict) or detail.get("type") != REPLAY_TYPE:
        return None
    item = detail.get("item")
    if not isinstance(item, dict) or not item.get("encrypted_content"):
        return None
    made_by = str(detail.get("model") or "")
    if made_by and not same_model(made_by, model):
        return None
    return item


def same_model(made_by: str, model: str) -> bool:
    """A sealed reasoning blob opens only for the model that wrote it. Servers report dated
    snapshots (``gpt-5-2025-08-07``) for an undated request (``gpt-5``), so both count."""
    a, b = made_by.strip().lower(), model.strip().lower()
    if not a or not b:
        return True
    return a == b or a.startswith(b + "-2") or b.startswith(a + "-2")


def convert_input(
    messages: Iterable[Message],
    *,
    model: str,
    replay_reasoning: bool = True,
    keep_item_ids: bool = True,
) -> tuple[str | None, list[dict[str, Any]]]:
    """OpenAI-shaped history → (``instructions`` or None, Responses ``input`` items)."""
    instructions: list[str] = []
    items: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role in ("system", "developer"):
            text = _text(message.get("content"))
            if text.strip():
                instructions.append(text)
            continue
        if role == "user":
            content = _user_content(message.get("content"))
            if content is not None:
                items.append({"type": "message", "role": "user", "content": content})
            continue
        if role == "assistant":
            details = message.get("reasoning_details")
            if replay_reasoning and isinstance(details, list):
                for detail in details:
                    item = _replayable(detail, model)
                    if item is None:
                        continue
                    replayed = dict(item)
                    if not keep_item_ids:
                        replayed.pop("id", None)
                    items.append(replayed)
            text = _text(message.get("content"))
            if text.strip():
                items.append({"type": "message", "role": "assistant", "content": text})
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") or {}
                name = str(function.get("name") or "") if isinstance(function, dict) else ""
                if not name:
                    continue
                items.append(
                    {
                        "type": "function_call",
                        "call_id": wire_call_id(call.get("id")),
                        "name": name,
                        "arguments": _arguments(function.get("arguments")),
                    }
                )
            continue
        if role == "tool":
            content = message.get("content")
            if isinstance(content, str):
                output = content
            elif isinstance(content, list):
                output = _text(content)
            elif content is None:
                output = ""
            else:
                output = json.dumps(content, ensure_ascii=False)
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": wire_call_id(message.get("tool_call_id")),
                    "output": output,
                }
            )
    return ("\n\n".join(instructions) if instructions else None), items


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
        entry: dict[str, Any] = {"type": "function", "name": name}
        if function.get("description"):
            entry["description"] = str(function["description"])
        entry["parameters"] = (
            dict(schema) if isinstance(schema, dict) else {"type": "object", "properties": {}}
        )
        entry["strict"] = False
        out.append(entry)
    return out


def convert_tool_choice(choice: object) -> str | dict[str, Any] | None:
    if choice is None:
        return None
    if isinstance(choice, str):
        return (
            choice
            if choice in ("auto", "required", "none")
            else {"type": "function", "name": choice}
        )
    if isinstance(choice, dict):
        function = choice.get("function")
        if isinstance(function, dict) and function.get("name"):
            return {"type": "function", "name": str(function["name"])}
    return None


# -- reply parsing ---------------------------------------------------------------------------------


def _count(value: object) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def parse_usage(raw: object) -> Usage | None:
    """Responses usage → ``Usage`` (``input_tokens`` already includes cached tokens)."""
    if not isinstance(raw, dict):
        return None
    input_details = raw.get("input_tokens_details") or {}
    output_details = raw.get("output_tokens_details") or {}
    return Usage(
        prompt_tokens=_count(raw.get("input_tokens")),
        completion_tokens=_count(raw.get("output_tokens")),
        cache_read_tokens=_count(input_details.get("cached_tokens"))
        if isinstance(input_details, dict)
        else 0,
        reasoning_tokens=_count(output_details.get("reasoning_tokens"))
        if isinstance(output_details, dict)
        else 0,
    )


def _embedded_error(response: Mapping[str, Any], raw_text: str) -> ProviderStreamError:
    err = response.get("error")
    if isinstance(err, dict):
        code = err.get("code")
        return stream_error(
            str(err.get("message") or code or "the response failed"),
            error_type=str(err.get("type") or code or "") or None,
            code=str(code) if code else None,
            raw_text=raw_text,
        )
    return stream_error("the response failed without details", code="failed", raw_text=raw_text)


def _cancelled(raw_text: str) -> ResponseCancelled:
    base = stream_error(
        "the provider cancelled this response", error_type="cancelled", code="response_cancelled"
    )
    return ResponseCancelled(str(base), body=base.body, raw_text=raw_text)


def _message_text(item: Mapping[str, Any]) -> str:
    out: list[str] = []
    for part in item.get("content") or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "refusal" and isinstance(part.get("refusal"), str):
            out.append(part["refusal"])
        elif isinstance(part.get("text"), str):
            out.append(part["text"])
    return "".join(out)


def _reasoning_text(item: Mapping[str, Any]) -> str:
    chunks = [
        str(part.get("text"))
        for part in item.get("summary") or []
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ]
    if not chunks:
        chunks = [
            str(part.get("text"))
            for part in item.get("content") or []
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
    return "\n".join(chunks)


def _stored_reasoning(item: Mapping[str, Any], model: str) -> dict[str, Any] | None:
    if not item.get("encrypted_content"):
        return None
    detail: dict[str, Any] = {"type": REPLAY_TYPE, "item": copy.deepcopy(dict(item))}
    if model:
        detail["model"] = model
    return detail


@dataclass
class _Output:
    kind: str
    tool_index: int = -1
    started: bool = False
    args_seen: bool = False
    text_seen: bool = False
    reasoning_seen: bool = False


@dataclass
class _ReplyState:
    model: str = ""
    outputs: dict[int, _Output] = field(default_factory=dict[int, _Output])
    by_item: dict[str, int] = field(default_factory=dict[str, int])
    tools: int = 0
    text: bool = False
    reasoning: bool = False

    def slot(self, payload: Mapping[str, Any], kind: str = "") -> _Output:
        index = payload.get("output_index")
        if not isinstance(index, int):
            item_id = payload.get("item_id")
            index = self.by_item.get(str(item_id), -1) if item_id else -1
        if index < 0:
            index = max(self.outputs, default=-1) + 1 if kind else max(self.outputs, default=0)
        found = self.outputs.get(index)
        if found is None:
            found = _Output(kind=kind)
            self.outputs[index] = found
        elif kind and not found.kind:
            found.kind = kind
        return found


class _Streaks:
    """Reasoning-only incomplete replies in a row, per model (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    def bump(self, model: str) -> int:
        with self._lock:
            self._counts[model] = self._counts.get(model, 0) + 1
            return self._counts[model]

    def reset(self, model: str) -> None:
        with self._lock:
            self._counts.pop(model, None)


def _finish_for(response: Mapping[str, Any], *, tools: bool, raw_text: str) -> FinishReason:
    status = str(response.get("status") or "completed").lower()
    if status == "failed":
        raise _embedded_error(response, raw_text)
    if status == "cancelled":
        raise _cancelled(raw_text)
    if status == "incomplete":
        details = response.get("incomplete_details")
        reason = details.get("reason") if isinstance(details, dict) else None
        if reason == "content_filter":
            return "content_filter"
        return "length"
    return "tool_calls" if tools else "stop"


class ResponsesTransport:
    api_mode = API_MODE

    def __init__(self) -> None:
        # Routes (base URL, model) that refused replayed reasoning or stored item ids.
        self._no_replay: set[tuple[str, str]] = set()
        self._no_item_ids: set[tuple[str, str]] = set()
        self._streaks = _Streaks()

    # -- request ---------------------------------------------------------------------------

    def build_request(
        self, route: RouteLike, req: ChatRequest, *, profile: object = None
    ) -> httpx.Request:
        prof = profile_of(route, profile)
        model = req.model or route.model
        key = (route.base_url, model)
        instructions, items = convert_input(
            req.messages,
            model=model,
            replay_reasoning=key not in self._no_replay,
            keep_item_ids=key not in self._no_item_ids,
        )
        body: dict[str, Any] = {"model": model}
        if instructions is not None:
            body["instructions"] = instructions
        body["input"] = items
        tools = convert_tools(req.tools)
        if tools:
            body["tools"] = tools
            choice = convert_tool_choice(req.tool_choice)
            if choice is not None:
                body["tool_choice"] = choice
        body["store"] = False
        body["stream"] = bool(req.stream)
        max_tokens = req.max_tokens or getattr(prof, "default_max_tokens", None)
        if max_tokens:
            body["max_output_tokens"] = int(max_tokens)
        fixed = getattr(prof, "fixed_temperature", None)
        if fixed is not None:
            body["temperature"] = fixed
        elif req.temperature is not None:
            body["temperature"] = req.temperature
        supported = getattr(prof, "reasoning_efforts", None) or DEFAULT_EFFORTS
        effort = clamp_effort(req.reasoning_effort, tuple(supported))
        if effort is not None:
            body["reasoning"] = {"effort": effort, "summary": "auto"}
            body["include"] = [INCLUDE_ENCRYPTED]
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
        secret = secret_text(route.api_key)
        if secret:
            headers["authorization"] = f"Bearer {secret}"
        content = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return httpx.Request(
            "POST", join_url(route.base_url, "responses"), headers=headers, content=content
        )

    def on_request_rejected(self, route: RouteLike, error: ProviderHTTPError) -> bool:
        """A route that cannot open our stored reasoning, or does not know our item ids, gets
        one more try without them; the change sticks for that route."""
        if error.status not in (400, 404):
            return False
        body = error.body if isinstance(error.body, str) else json.dumps(error.body, default=str)
        text = f"{error.message} {body}".lower()
        key = (route.base_url, route.model)
        sealed = "encrypted_content" in text or "encrypted content" in text
        if sealed and key not in self._no_replay:
            log.warning(
                "%s refused replayed reasoning; sending this route's history without it",
                httpx.URL(route.base_url).host,
            )
            self._no_replay.add(key)
            return True
        if (
            not sealed
            and key not in self._no_item_ids
            and ("rs_" in text or "item" in text)
            and any(word in text for word in _ID_REJECTION_WORDS)
        ):
            log.info("%s refused stored item ids; replaying items without them", route.base_url)
            self._no_item_ids.add(key)
            return True
        return False

    # -- reply -----------------------------------------------------------------------------

    def error_from_stream(self, event: SSEEvent) -> ProviderStreamError | None:
        data = event.data.strip()
        try:
            payload = json.loads(data) if data else None
        except ValueError:
            return stream_error(data, code="non_json_data", raw_text=data)
        kind = payload.get("type") if isinstance(payload, dict) else None
        if event.event == "error" or kind == "error":
            inner = payload.get("error") if isinstance(payload, dict) else None
            source = inner if isinstance(inner, dict) else payload
            if isinstance(source, dict):
                code = source.get("code")
                return stream_error(
                    str(source.get("message") or code or "provider error"),
                    error_type=str(source.get("type") or code or "") or None,
                    code=str(code) if code else None,
                    raw_text=data,
                )
            return stream_error(data or "provider error", code="error_event", raw_text=data)
        if kind == "response.failed" and isinstance(payload, dict):
            response = payload.get("response")
            return _embedded_error(response if isinstance(response, dict) else {}, data)
        return None

    async def parse_stream(self, events: AsyncIterator[SSEItem]) -> AsyncIterator[StreamEvent]:
        state = _ReplyState()
        finished = False
        async for item in events:
            if isinstance(item, KeepAlive):
                yield item
                continue
            error = self.error_from_stream(item)
            if error is not None:
                raise error
            data = item.data.strip()
            if not data or data == "[DONE]":
                yield KeepAlive()
                continue
            payload = json.loads(data)
            if not isinstance(payload, dict):
                continue
            kind = str(payload.get("type") or item.event)
            if kind in _TERMINAL:
                response = payload.get("response")
                response = response if isinstance(response, dict) else {}
                for event in self._terminal(state, response, data):
                    yield event
                finished = True
                break
            for event in self._event(state, kind, payload):
                yield event
        if not finished:
            raise stream_error(
                "the stream ended before the model finished its reply",
                error_type="incomplete_stream",
                code="incomplete_stream",
            )

    def _event(
        self, state: _ReplyState, kind: str, payload: Mapping[str, Any]
    ) -> list[StreamEvent]:
        if kind == "response.created":
            response = payload.get("response")
            if isinstance(response, dict) and isinstance(response.get("model"), str):
                state.model = response["model"]
            return [KeepAlive(kind)]
        if kind in _TEXT_DELTAS:
            delta = payload.get("delta")
            if isinstance(delta, str) and delta:
                state.slot(payload, "message").text_seen = True
                state.text = True
                return [TextDelta(delta)]
            return [KeepAlive(kind)]
        if kind in _REASONING_DELTAS:
            delta = payload.get("delta")
            if isinstance(delta, str) and delta:
                state.slot(payload, "reasoning").reasoning_seen = True
                state.reasoning = True
                return [ReasoningDelta(delta)]
            return [KeepAlive(kind)]
        if kind == "response.output_item.added":
            output = payload.get("item")
            if not isinstance(output, dict):
                return [KeepAlive(kind)]
            slot = state.slot(payload, str(output.get("type") or ""))
            if output.get("id"):
                index = payload.get("output_index")
                if isinstance(index, int):
                    state.by_item[str(output["id"])] = index
            if output.get("type") == "function_call":
                return self._start_call(state, slot, output)
            return [KeepAlive(kind)]
        if kind == "response.function_call_arguments.delta":
            slot = state.slot(payload, "function_call")
            delta = payload.get("delta")
            if not isinstance(delta, str) or not delta or not slot.started:
                return [KeepAlive(kind)]
            slot.args_seen = True
            return [ToolCallArgsDelta(slot.tool_index, delta)]
        if kind == "response.function_call_arguments.done":
            slot = state.slot(payload, "function_call")
            arguments = payload.get("arguments")
            if slot.started and not slot.args_seen and isinstance(arguments, str) and arguments:
                slot.args_seen = True
                return [ToolCallArgsDelta(slot.tool_index, arguments)]
            return [KeepAlive(kind)]
        if kind == "response.output_item.done":
            output = payload.get("item")
            if isinstance(output, dict):
                return self._item_done(
                    state, state.slot(payload, str(output.get("type") or "")), output
                )
        return [KeepAlive(kind)]

    @staticmethod
    def _start_call(
        state: _ReplyState, slot: _Output, output: Mapping[str, Any]
    ) -> list[StreamEvent]:
        if slot.started:
            return []
        slot.kind = "function_call"
        slot.started = True
        slot.tool_index = state.tools
        state.tools += 1
        call_id = output.get("call_id") or output.get("id")
        events: list[StreamEvent] = [
            ToolCallStart(
                slot.tool_index, str(call_id) if call_id else None, str(output.get("name") or "")
            )
        ]
        arguments = output.get("arguments")
        if isinstance(arguments, str) and arguments:
            slot.args_seen = True
            events.append(ToolCallArgsDelta(slot.tool_index, arguments))
        return events

    def _item_done(
        self, state: _ReplyState, slot: _Output, output: Mapping[str, Any]
    ) -> list[StreamEvent]:
        kind = output.get("type")
        if kind == "function_call":
            if not slot.started:
                return self._start_call(state, slot, output)
            arguments = output.get("arguments")
            if not slot.args_seen:
                slot.args_seen = True
                text = arguments if isinstance(arguments, str) and arguments else "{}"
                return [ToolCallArgsDelta(slot.tool_index, text)]
            return []
        if kind == "message":
            text = _message_text(output)
            if text and not slot.text_seen:
                slot.text_seen = True
                state.text = True
                return [TextDelta(text)]
            return []
        if kind == "reasoning":
            events: list[StreamEvent] = []
            state.reasoning = True
            summary = _reasoning_text(output)
            if summary and not slot.reasoning_seen:
                slot.reasoning_seen = True
                events.append(ReasoningDelta(summary))
            stored = _stored_reasoning(output, state.model)
            if stored is not None:
                events.append(ReasoningDetail(stored))
            return events
        return []

    def _terminal(
        self, state: _ReplyState, response: Mapping[str, Any], raw_text: str
    ) -> list[StreamEvent]:
        if isinstance(response.get("model"), str) and not state.model:
            state.model = response["model"]
        events: list[StreamEvent] = []
        # Output items the stream never announced (a server that only sends the final object).
        output = response.get("output")
        if isinstance(output, list) and not state.outputs:
            for index, item in enumerate(output):
                if isinstance(item, dict):
                    slot = state.slot({"output_index": index}, str(item.get("type") or ""))
                    events.extend(self._item_done(state, slot, item))
        finish = _finish_for(response, tools=state.tools > 0, raw_text=raw_text)
        self._check_streak(state, finish)
        usage = parse_usage(response.get("usage"))
        if usage is not None:
            events.append(UsageReport(usage))
        events.append(Finish(finish))
        return events

    def _check_streak(self, state: _ReplyState, finish: FinishReason) -> None:
        model = state.model or "?"
        if finish == "length" and state.reasoning and not state.text and state.tools == 0:
            count = self._streaks.bump(model)
            if count >= INCOMPLETE_LIMIT:
                self._streaks.reset(model)
                raise stream_error(
                    f"the model stopped {count} times in a row with only reasoning and no answer",
                    error_type="incomplete_response",
                    code="incomplete_stream",
                )
        else:
            self._streaks.reset(model)

    def parse_response(self, payload: Mapping[str, Any]) -> NormalizedResponse:
        raw_text = json.dumps(payload, default=str)
        if payload.get("type") == "error":
            inner = payload.get("error")
            source = inner if isinstance(inner, dict) else payload
            code = source.get("code")
            raise stream_error(
                str(source.get("message") or code or "provider error"),
                code=str(code) if code else None,
                raw_text=raw_text,
            )
        state = _ReplyState(model=str(payload.get("model") or ""))
        text: list[str] = []
        reasoning: list[str] = []
        details: list[dict[str, Any]] = []
        calls: list[ToolCall] = []
        status = str(payload.get("status") or "completed").lower()
        if status not in ("failed", "cancelled"):
            for item in payload.get("output") or []:
                if not isinstance(item, dict):
                    continue
                kind = item.get("type")
                if kind == "message":
                    text.append(_message_text(item))
                elif kind == "reasoning":
                    summary = _reasoning_text(item)
                    if summary:
                        reasoning.append(summary)
                    stored = _stored_reasoning(item, state.model)
                    if stored is not None:
                        details.append(stored)
                elif kind == "function_call":
                    call_id = item.get("call_id") or item.get("id") or f"call_{len(calls)}"
                    calls.append(
                        ToolCall(
                            id=str(call_id),
                            name=str(item.get("name") or ""),
                            arguments=_arguments(item.get("arguments")),
                        )
                    )
        state.text = any(t.strip() for t in text)
        state.reasoning = bool(reasoning or details)
        state.tools = len(calls)
        finish = _finish_for(payload, tools=bool(calls), raw_text=raw_text)
        self._check_streak(state, finish)
        joined = "".join(text)
        provider_data: dict[str, Any] = {}
        for key in ("id", "model", "status"):
            if isinstance(payload.get(key), str):
                provider_data[key] = payload[key]
        return NormalizedResponse(
            content=joined if joined else None,
            tool_calls=tuple(calls),
            finish_reason=finish,
            reasoning="\n".join(reasoning) or None,
            reasoning_details=tuple(details),
            usage=parse_usage(payload.get("usage")),
            provider_data=provider_data,
        )


register_transport(API_MODE, ResponsesTransport)
