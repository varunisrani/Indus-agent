"""Google Gemini ``generateContent`` over raw HTTP (the ``gemini`` API mode).

Wire facts follow Google's Gemini API reference: ``POST {base}/models/{model}:generateContent``
and ``:streamGenerateContent?alt=sse`` (each event is one ``GenerateContentResponse``), the key in
the ``x-goog-api-key`` header, ``contents`` of ``user`` / ``model`` turns made of ``parts``
(``text``, ``inlineData``, ``functionCall``, ``functionResponse``, ``thought``,
``thoughtSignature``), ``systemInstruction``, ``tools[].functionDeclarations``,
``toolConfig.functionCallingConfig`` and ``generationConfig`` with ``thinkingConfig``.

The key is never put in the URL, so it cannot reach access logs or our error messages. Thought
signatures Google attaches to parts are kept as opaque ``reasoning_details`` records
(``{"type": "gemini.signature", ...}``) and returned on the same parts in the next request.
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
    normalize_effort,
)

log = logging.getLogger(__name__)

API_MODE = "gemini"
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
SIGNATURE_TYPE = "gemini.signature"
KEY_HEADER = "x-goog-api-key"
# A key read from the generic OpenAI variable never goes to Google (custom providers default to it).
FOREIGN_KEY_ENVS = frozenset({"OPENAI_API_KEY"})
# Google documents this value for function calls that came from another model and so carry no
# signature (Gemini 3 validates signatures on the current turn's calls).
FOREIGN_CALL_SIGNATURE = "context_engineering_is_the_way_to_go"
BLANK_TEXT = "[empty]"

# Gemini 2.5 thinking budgets per ladder level (tokens).
BUDGETS: dict[str, int] = {
    "minimal": 512,
    "low": 2_048,
    "medium": 8_192,
    "high": 16_384,
    "xhigh": 24_576,
    "max": 24_576,
    "ultra": 24_576,
}
FINISH_TABLE: dict[str, FinishReason] = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "BLOCKLIST": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "SPII": "content_filter",
    "IMAGE_SAFETY": "content_filter",
    "LANGUAGE": "content_filter",
    "MALFORMED_FUNCTION_CALL": "error",
    "UNEXPECTED_TOOL_CALL": "error",
    "OTHER": "stop",
}
# Google RPC status words → codes the P03 classifier knows.
STATUS_CODES: dict[str, str] = {
    "RESOURCE_EXHAUSTED": "rate_limit_exceeded",
    "UNAVAILABLE": "overloaded_error",
    "INTERNAL": "server_error",
    "DEADLINE_EXCEEDED": "timeout_error",
    "UNAUTHENTICATED": "invalid_api_key",
    "PERMISSION_DENIED": "authentication_error",
}
_VERSION = re.compile(r"gemini-(\d+)(?:\.(\d+))?")


# -- models ----------------------------------------------------------------------------------------


def bare_model(model: str) -> str:
    """``gemini-2.5-flash`` from ``models/gemini-2.5-flash`` or ``google/gemini-2.5-flash``."""
    name = model.strip()
    for prefix in ("models/", "google/"):
        if name.lower().startswith(prefix):
            name = name[len(prefix) :]
    return name


def generation(model: str) -> tuple[int, int]:
    """``(major, minor)`` of a Gemini model id; ``(0, 0)`` when it names no version."""
    match = _VERSION.search(bare_model(model).lower())
    return (int(match.group(1)), int(match.group(2) or 0)) if match else (0, 0)


def thinking_config(model: str, effort: str | None) -> dict[str, Any] | None:
    """``generationConfig.thinkingConfig`` for ``effort`` (a ladder level, or None for off)."""
    name = bare_model(model).lower()
    if not name.startswith("gemini"):
        return None  # Gemma and other models on the endpoint refuse the field
    level = normalize_effort(effort)
    major, minor = generation(name)
    modern = major >= 3 or name.endswith("-latest")
    if level is None:
        if modern:
            return {"includeThoughts": False, "thinkingLevel": "low"}
        if (major, minor) >= (2, 5) and "pro" not in name:
            return {"includeThoughts": False, "thinkingBudget": 0}
        return {"includeThoughts": False}
    if modern:
        if level in ("minimal", "low"):
            chosen = "low"
        elif level == "medium":
            chosen = "high" if "pro" in name else "medium"
        else:
            chosen = "high"
        return {"includeThoughts": True, "thinkingLevel": chosen}
    if (major, minor) >= (2, 5):
        return {"includeThoughts": True, "thinkingBudget": BUDGETS.get(level, BUDGETS["medium"])}
    return None


# -- request conversion ------------------------------------------------------------------------


def _blank(text: object) -> bool:
    return not isinstance(text, str) or not text.strip()


def _parts(content: object) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [] if _blank(content) else [{"text": content}]
    out: list[dict[str, Any]] = []
    if not isinstance(content, list):
        return out
    for part in content:
        if isinstance(part, str):
            if not _blank(part):
                out.append({"text": part})
            continue
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text" and not _blank(part.get("text")):
            out.append({"text": part["text"]})
        elif kind == "image_url":
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            if isinstance(url, str) and url.startswith("data:"):
                header, _, data = url.partition(",")
                mime = header[len("data:") :].split(";", 1)[0].strip() or "image/png"
                out.append({"inlineData": {"mimeType": mime, "data": data}})
            elif isinstance(url, str) and url:
                out.append({"text": f"[image: {url}]"})
    return out


def _text(content: object) -> str:
    return "".join(p["text"] for p in _parts(content) if "text" in p)


def _args(arguments: object) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return dict(arguments)
    text = arguments if isinstance(arguments, str) else ""
    if not text.strip():
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        return {"_raw": text}
    return value if isinstance(value, dict) else {"_value": value}


def _signatures(details: object) -> tuple[dict[str, str], str | None]:
    """``(call id → signature, text-part signature)`` stored on an assistant row."""
    by_call: dict[str, str] = {}
    text_sig: str | None = None
    if not isinstance(details, list):
        return by_call, text_sig
    for detail in details:
        if not isinstance(detail, dict) or detail.get("type") != SIGNATURE_TYPE:
            continue
        signature = detail.get("signature")
        if not isinstance(signature, str) or not signature:
            continue
        call_id = detail.get("call_id")
        if isinstance(call_id, str) and call_id:
            by_call[call_id] = signature
        else:
            text_sig = signature
    return by_call, text_sig


def _response_value(content: object) -> dict[str, Any]:
    text = content if isinstance(content, str) else _text(content) if content is not None else ""
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    return {"output": text}


def convert_contents(
    messages: Iterable[Message], *, model: str
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """OpenAI-shaped history → (``systemInstruction`` or None, ``contents``)."""
    with_ids = generation(model)[0] >= 3
    system: list[str] = []
    contents: list[dict[str, Any]] = []
    names: dict[str, str] = {}

    def add(role: str, parts: list[dict[str, Any]]) -> None:
        if not parts:
            return
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": role, "parts": parts})

    for message in messages:
        role = message.get("role")
        if role in ("system", "developer"):
            text = _text(message.get("content"))
            if text.strip():
                system.append(text)
        elif role == "assistant":
            by_call, text_sig = _signatures(message.get("reasoning_details"))
            parts = _parts(message.get("content"))
            if parts and text_sig and "text" in parts[-1]:
                parts[-1] = {**parts[-1], "thoughtSignature": text_sig}
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") or {}
                if not isinstance(function, dict) or not function.get("name"):
                    continue
                name = str(function["name"])
                call_id = str(call.get("id") or "")
                if call_id:
                    names[call_id] = name
                body: dict[str, Any] = {"name": name, "args": _args(function.get("arguments"))}
                if with_ids and call_id:
                    body["id"] = call_id
                part: dict[str, Any] = {"functionCall": body}
                signature = by_call.get(call_id) or (FOREIGN_CALL_SIGNATURE if with_ids else None)
                if signature:
                    part["thoughtSignature"] = signature
                parts.append(part)
            add("model", parts or [{"text": BLANK_TEXT}])
        elif role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            name = names.get(call_id) or str(message.get("name") or "") or "tool"
            response: dict[str, Any] = {
                "name": name,
                "response": _response_value(message.get("content")),
            }
            if with_ids and call_id:
                response["id"] = call_id
            add("user", [{"functionResponse": response}])
        else:
            add("user", _parts(message.get("content")) or [{"text": BLANK_TEXT}])
    if contents and contents[0]["role"] != "user":
        contents.insert(0, {"role": "user", "parts": [{"text": BLANK_TEXT}]})
    instruction = {"parts": [{"text": "\n\n".join(system)}]} if system else None
    return instruction, contents


def convert_tools(tools: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    declarations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for tool in tools:
        function = tool.get("function") if tool.get("type", "function") == "function" else None
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "")
        if not name or name in seen:
            continue
        seen.add(name)
        entry: dict[str, Any] = {"name": name}
        if function.get("description"):
            entry["description"] = str(function["description"])
        schema = function.get("parameters")
        if isinstance(schema, dict) and schema.get("properties"):
            entry["parameters"] = copy.deepcopy(schema)
        declarations.append(entry)
    return [{"functionDeclarations": declarations}] if declarations else []


def convert_tool_choice(choice: object) -> dict[str, Any] | None:
    modes = {"auto": "AUTO", "required": "ANY", "any": "ANY", "none": "NONE"}
    if isinstance(choice, str):
        if choice in modes:
            return {"functionCallingConfig": {"mode": modes[choice]}}
        return {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": [choice]}}
    if isinstance(choice, dict):
        function = choice.get("function")
        if isinstance(function, dict) and function.get("name"):
            return {
                "functionCallingConfig": {
                    "mode": "ANY",
                    "allowedFunctionNames": [str(function["name"])],
                }
            }
    return None


# -- reply parsing ---------------------------------------------------------------------------------


def _count(value: object) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def parse_usage(raw: object) -> Usage | None:
    """``usageMetadata`` → ``Usage``. Thought tokens are output the model produced, so they count
    as completion tokens (``candidatesTokenCount`` holds only the visible part)."""
    if not isinstance(raw, dict):
        return None
    thoughts = _count(raw.get("thoughtsTokenCount"))
    return Usage(
        prompt_tokens=_count(raw.get("promptTokenCount")),
        completion_tokens=_count(raw.get("candidatesTokenCount")) + thoughts,
        cache_read_tokens=_count(raw.get("cachedContentTokenCount")),
        reasoning_tokens=thoughts,
    )


def _error(err: Mapping[str, Any], raw_text: str) -> ProviderStreamError:
    status = str(err.get("status") or "")
    code = err.get("code")
    return stream_error(
        str(err.get("message") or status or "provider error"),
        error_type=status or None,
        code=STATUS_CODES.get(status, status or None),
        status=code if isinstance(code, int) else None,
        raw_text=raw_text,
    )


@dataclass
class _Call:
    index: int
    id: str
    name: str
    chunk: int
    sent: str = ""


@dataclass
class _Reply:
    calls: list[_Call] = field(default_factory=list[_Call])
    by_id: dict[str, _Call] = field(default_factory=dict[str, _Call])
    finish: str | None = None
    chunks: int = 0

    def call_events(self, part: Mapping[str, Any], fc: Mapping[str, Any]) -> list[StreamEvent]:
        name = str(fc.get("name") or "")
        provider_id = fc.get("id") if isinstance(fc.get("id"), str) and fc.get("id") else None
        args = json.dumps(fc.get("args") or {}, ensure_ascii=False, sort_keys=True)
        call: _Call | None = None
        if provider_id is not None:
            call = self.by_id.get(provider_id)
        elif self.calls:
            # Without ids, a call in a later chunk that repeats or extends the previous call's
            # arguments is that call sent again (Google resends whole arguments).
            last = self.calls[-1]
            if last.chunk < self.chunks and last.name == name and args.startswith(last.sent):
                call = last
        events: list[StreamEvent] = []
        if call is None:
            index = len(self.calls)
            call = _Call(index, provider_id or f"call_{index}_{name}", name, self.chunks)
            self.calls.append(call)
            self.by_id[call.id] = call
            events.append(ToolCallStart(index, call.id, name))
        suffix = args[len(call.sent) :] if args.startswith(call.sent) else args
        if suffix:
            events.append(ToolCallArgsDelta(call.index, suffix))
        call.sent = args
        signature = part.get("thoughtSignature")
        if isinstance(signature, str) and signature:
            events.append(
                ReasoningDetail(
                    {"type": SIGNATURE_TYPE, "call_id": call.id, "signature": signature}
                )
            )
        return events

    def chunk(self, payload: Mapping[str, Any]) -> list[StreamEvent]:
        self.chunks += 1
        events: list[StreamEvent] = []
        candidates = payload.get("candidates")
        candidate = candidates[0] if isinstance(candidates, list) and candidates else None
        if isinstance(candidate, dict):
            content = candidate.get("content")
            parts = content.get("parts") if isinstance(content, dict) else None
            for part in parts if isinstance(parts, list) else []:
                if not isinstance(part, dict):
                    continue
                fc = part.get("functionCall")
                if isinstance(fc, dict) and fc.get("name"):
                    events.extend(self.call_events(part, fc))
                    continue
                text = part.get("text")
                if isinstance(text, str) and text:
                    if part.get("thought") is True:
                        events.append(ReasoningDelta(text))
                    else:
                        events.append(TextDelta(text))
                signature = part.get("thoughtSignature")
                if isinstance(signature, str) and signature:
                    events.append(ReasoningDetail({"type": SIGNATURE_TYPE, "signature": signature}))
            if candidate.get("finishReason"):
                self.finish = str(candidate["finishReason"]).upper()
        feedback = payload.get("promptFeedback")
        if isinstance(feedback, dict) and feedback.get("blockReason") and candidate is None:
            self.finish = "SAFETY"
        usage = parse_usage(payload.get("usageMetadata"))
        if usage is not None:
            events.append(UsageReport(usage))
        return events or [KeepAlive("chunk")]

    def finish_reason(self) -> FinishReason:
        mapped = FINISH_TABLE.get(self.finish or "", "stop")
        if self.calls and mapped in ("stop", "length"):
            return "tool_calls" if mapped == "stop" else mapped
        return mapped


class GeminiTransport:
    api_mode = API_MODE

    def build_request(
        self, route: RouteLike, req: ChatRequest, *, profile: object = None
    ) -> httpx.Request:
        prof = profile_of(route, profile)
        model = bare_model(req.model or route.model)
        instruction, contents = convert_contents(req.messages, model=model)
        body: dict[str, Any] = {"contents": contents}
        if instruction is not None:
            body["systemInstruction"] = instruction
        tools = convert_tools(req.tools)
        if tools:
            body["tools"] = tools
            choice = convert_tool_choice(req.tool_choice)
            if choice is not None:
                body["toolConfig"] = choice
        config: dict[str, Any] = {}
        fixed = getattr(prof, "fixed_temperature", None)
        if fixed is not None:
            config["temperature"] = fixed
        elif req.temperature is not None:
            config["temperature"] = req.temperature
        max_tokens = req.max_tokens or getattr(prof, "default_max_tokens", None)
        if max_tokens:
            config["maxOutputTokens"] = int(max_tokens)
        thinking = thinking_config(model, req.reasoning_effort)
        if thinking is not None:
            config["thinkingConfig"] = thinking
        if config:
            body["generationConfig"] = config
        body.update(dict(req.extra_body))
        prepare = getattr(prof, "prepare_body", None)
        if callable(prepare):
            prepared = prepare(body, model)
            if isinstance(prepared, dict):
                body = prepared

        method = "streamGenerateContent?alt=sse" if req.stream else "generateContent"
        url = join_url(route.base_url or DEFAULT_BASE_URL, f"models/{model}:{method}")
        headers = {
            "content-type": "application/json",
            "accept": "text/event-stream" if req.stream else "application/json",
        }
        headers.update(route_headers(route))
        key = secret_text(route.api_key)
        key_env = str(getattr(route, "key_env", "") or "")
        if key and key_env not in FOREIGN_KEY_ENVS:
            headers[KEY_HEADER] = key
        elif key:
            log.warning("not sending %s to %s: set key_env for this provider", key_env, url)
        content = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return httpx.Request("POST", url, headers=headers, content=content)

    def on_request_rejected(self, route: RouteLike, error: ProviderHTTPError) -> bool:
        return False

    def error_from_stream(self, event: SSEEvent) -> ProviderStreamError | None:
        data = event.data.strip()
        try:
            payload = json.loads(data) if data else None
        except ValueError:
            return stream_error(data, code="non_json_data", raw_text=data)
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            return _error(payload["error"], data)
        return None

    async def parse_stream(self, events: AsyncIterator[SSEItem]) -> AsyncIterator[StreamEvent]:
        reply = _Reply()
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
            for event in reply.chunk(payload):
                yield event
        if reply.finish is None:
            raise stream_error(
                "the stream ended before the model finished its reply",
                error_type="incomplete_stream",
                code="incomplete_stream",
            )
        yield Finish(reply.finish_reason())

    def parse_response(self, payload: Mapping[str, Any]) -> NormalizedResponse:
        if isinstance(payload.get("error"), dict):
            raise _error(payload["error"], json.dumps(payload, default=str))
        reply = _Reply()
        text: list[str] = []
        reasoning: list[str] = []
        details: list[dict[str, Any]] = []
        calls: dict[int, list[str]] = {}
        usage: Usage | None = None
        for event in reply.chunk(payload):
            match event:
                case TextDelta(text=t):
                    text.append(t)
                case ReasoningDelta(text=t):
                    reasoning.append(t)
                case ReasoningDetail(detail=d):
                    details.append(d)
                case ToolCallArgsDelta(index=i, fragment=f):
                    calls.setdefault(i, []).append(f)
                case UsageReport(usage=u):
                    usage = u
                case _:
                    pass
        tool_calls = tuple(
            ToolCall(
                id=call.id, name=call.name, arguments="".join(calls.get(call.index, [])) or "{}"
            )
            for call in reply.calls
        )
        joined = "".join(text)
        provider_data: dict[str, Any] = {}
        for key in ("responseId", "modelVersion"):
            if isinstance(payload.get(key), str):
                provider_data[key] = payload[key]
        if reply.finish is None:
            reply.finish = "STOP"
        return NormalizedResponse(
            content=joined if joined else None,
            tool_calls=tool_calls,
            finish_reason=reply.finish_reason(),
            reasoning="".join(reasoning) or None,
            reasoning_details=tuple(details),
            usage=usage,
            provider_data=provider_data,
        )


register_transport(API_MODE, GeminiTransport)
