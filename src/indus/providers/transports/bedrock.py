"""AWS Bedrock Converse / ConverseStream over our own HTTP (the ``bedrock`` API mode).

Wire facts follow AWS's Converse API reference: ``POST /model/{modelId}/converse`` (JSON reply)
and ``/converse-stream`` (an ``application/vnd.amazon.eventstream`` binary stream whose messages
carry ``messageStart``, ``contentBlockStart`` / ``contentBlockDelta`` / ``contentBlockStop``,
``messageStop`` and ``metadata`` events, or exception messages). Requests are signed with SigV4
(``providers.aws_sigv4``) for service ``bedrock``, or carry a Bedrock API key as a bearer token.

Converse refuses blank text blocks, so a blank turn becomes a short fixed placeholder; same-role
neighbours are merged and the conversation always opens with a user turn.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from urllib.parse import quote

import httpx

from indus.foundation.errors import NoUsableProvider, ProviderHTTPError, ProviderStreamError
from indus.foundation.scope import scope_or_none
from indus.providers.aws_sigv4 import (
    ACCESS_KEY_ENV,
    SECRET_KEY_ENV,
    SHARED_FILE_ENV,
    AwsCredentials,
    resolve_credentials,
    resolve_region,
    sign,
)
from indus.providers.eventstream import EventMessage, aiter_messages
from indus.providers.sse import SSEEvent
from indus.providers.transports import (
    RouteLike,
    join_url,
    profile_of,
    register_transport,
    route_headers,
    secret_text,
    stream_error,
)
from indus.providers.transports.anthropic import BUDGETS, DEFAULT_BUDGET, THINKING_HEADROOM
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

API_MODE = "bedrock"
SERVICE = "bedrock"
BEARER_ENV = "AWS_BEARER_TOKEN_BEDROCK"
# A key read from the generic OpenAI variable never goes to AWS (custom providers default to it).
FOREIGN_KEY_ENVS = frozenset({"OPENAI_API_KEY"})
BLANK_TEXT = "[empty]"
EVENT_STREAM = "application/vnd.amazon.eventstream"
MAX_TOOL_ID = 64
STOP_TABLE: dict[str, str] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "guardrail_intervened": "content_filter",
    "content_filtered": "content_filter",
}
# Exception message types → codes the P03 classifier knows.
EXCEPTION_CODES: dict[str, str] = {
    "throttlingException": "rate_limit_exceeded",
    "serviceUnavailableException": "overloaded_error",
    "internalServerException": "server_error",
    "modelStreamErrorException": "server_error",
    "modelTimeoutException": "timeout_error",
}

# Context windows of common Bedrock model ids, matched by the longest key contained in the id.
# Values from the AWS model cards (docs.aws.amazon.com/bedrock/latest/userguide/models-supported
# .html and the per-model cards, checked 2026-10-18).
CONTEXT_TABLE: dict[str, int] = {
    "anthropic.claude": 200_000,
    "amazon.nova-micro": 128_000,
    "amazon.nova-lite": 300_000,
    "amazon.nova-pro": 300_000,
    "amazon.nova-premier": 1_000_000,
    "meta.llama3-1": 128_000,
    "meta.llama3-2": 128_000,
    "meta.llama3-3": 128_000,
    "mistral.mistral-large": 128_000,
    "deepseek.r1": 128_000,
    "openai.gpt-oss": 128_000,
    "cohere.command-r": 128_000,
}

_TOOL_ID_BAD = re.compile(r"[^A-Za-z0-9_-]")


class BedrockCredentialsMissing(NoUsableProvider):
    """Neither a Bedrock API key nor AWS credentials could be found for a ``bedrock`` route."""


def context_length(model: str) -> int | None:
    """The window of a Bedrock model id from our table, or None when the id is not in it."""
    lowered = model.lower()
    best = ""
    for key in CONTEXT_TABLE:
        if key in lowered and len(key) > len(best):
            best = key
    return CONTEXT_TABLE[best] if best else None


def is_claude(model: str) -> bool:
    return "anthropic.claude" in model.lower()


def wire_tool_id(raw: object) -> str:
    """Tool-use ids Converse accepts (``[A-Za-z0-9_-]``, at most 64); stable for one input."""
    text = _TOOL_ID_BAD.sub("_", str(raw or "")) or "tool_0"
    if len(text) <= MAX_TOOL_ID:
        return text
    return "tool_" + sha256(text.encode("utf-8")).hexdigest()[:32]


# -- request conversion ------------------------------------------------------------------------


def _safe(text: object) -> str:
    value = text if isinstance(text, str) else ""
    return value if value.strip() else BLANK_TEXT


def _image_block(url: str) -> dict[str, Any] | None:
    if not url.startswith("data:"):
        return None
    header, _, data = url.partition(",")
    media = header[len("data:") :].split(";", 1)[0].strip() or "image/png"
    fmt = media.split("/", 1)[-1].lower()
    if fmt == "jpg":
        fmt = "jpeg"
    # JSON over REST carries blob members as base64 text, which a data URL already holds.
    return {"image": {"format": fmt, "source": {"bytes": data}}}


def _content_blocks(content: object) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"text": _safe(content)}]
    blocks: list[dict[str, Any]] = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                if part.strip():
                    blocks.append({"text": part})
            elif isinstance(part, dict):
                kind = part.get("type")
                if kind == "text" and isinstance(part.get("text"), str) and part["text"].strip():
                    blocks.append({"text": part["text"]})
                elif kind == "image_url":
                    image = part.get("image_url")
                    url = image.get("url") if isinstance(image, dict) else image
                    if isinstance(url, str) and url:
                        block = _image_block(url)
                        blocks.append(block if block is not None else {"text": f"[image: {url}]"})
    return blocks or [{"text": BLANK_TEXT}]


def _texts_only(content: object) -> list[dict[str, Any]]:
    """The non-blank text blocks of ``content`` (no placeholder, no images)."""
    if content is None or (isinstance(content, str) and not content.strip()):
        return []
    return [b for b in _content_blocks(content) if "text" in b and b["text"] != BLANK_TEXT]


def tool_input(arguments: object) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return dict(arguments)
    text = arguments if isinstance(arguments, str) else ""
    if not text.strip():
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    return value if isinstance(value, dict) else {"_raw": text}


def _reasoning_blocks(details: object) -> list[dict[str, Any]]:
    """Signed thinking kept from a Claude turn, put back as Converse reasoning blocks."""
    out: list[dict[str, Any]] = []
    if not isinstance(details, list):
        return out
    for detail in details:
        if not isinstance(detail, dict):
            continue
        if detail.get("type") == "thinking" and detail.get("signature"):
            out.append(
                {
                    "reasoningContent": {
                        "reasoningText": {
                            "text": str(detail.get("thinking") or ""),
                            "signature": str(detail["signature"]),
                        }
                    }
                }
            )
        elif detail.get("type") == "redacted_thinking" and detail.get("data"):
            out.append({"reasoningContent": {"redactedContent": str(detail["data"])}})
    return out


def _tool_result(message: Mapping[str, Any]) -> dict[str, Any]:
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "".join(
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and isinstance(p.get("text"), str)
        )
    elif content is None:
        text = ""
    else:
        text = json.dumps(content, ensure_ascii=False)
    result: dict[str, Any] = {
        "toolUseId": wire_tool_id(message.get("tool_call_id")),
        "content": [{"text": _safe(text)}],
    }
    if message.get("is_error") is True:
        result["status"] = "error"
    return {"toolResult": result}


def convert_messages(
    messages: Iterable[Message], *, model: str = ""
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """OpenAI-shaped history → (``system`` blocks, Converse ``messages``)."""
    system: list[dict[str, Any]] = []
    turns: list[dict[str, Any]] = []

    def add(role: str, blocks: list[dict[str, Any]]) -> None:
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"].extend(blocks)
        else:
            turns.append({"role": role, "content": blocks})

    replay_thinking = is_claude(model)
    for message in messages:
        role = message.get("role")
        if role in ("system", "developer"):
            system.extend(_texts_only(message.get("content")))
        elif role == "tool":
            add("user", [_tool_result(message)])
        elif role == "assistant":
            blocks = _reasoning_blocks(message.get("reasoning_details")) if replay_thinking else []
            blocks.extend(_texts_only(message.get("content")))
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") or {}
                if not isinstance(function, dict):
                    continue
                blocks.append(
                    {
                        "toolUse": {
                            "toolUseId": wire_tool_id(call.get("id")),
                            "name": str(function.get("name") or ""),
                            "input": tool_input(function.get("arguments")),
                        }
                    }
                )
            if turns and turns[-1]["role"] == "assistant":
                blocks = [b for b in blocks if "reasoningContent" not in b]
            add("assistant", blocks or [{"text": BLANK_TEXT}])
        else:
            add("user", _content_blocks(message.get("content")))
    if turns and turns[0]["role"] != "user":
        turns.insert(0, {"role": "user", "content": [{"text": BLANK_TEXT}]})
    return system, turns


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
        spec: dict[str, Any] = {"name": name}
        if function.get("description"):
            spec["description"] = str(function["description"])
        spec["inputSchema"] = {
            "json": dict(schema)
            if isinstance(schema, dict)
            else {"type": "object", "properties": {}}
        }
        out.append({"toolSpec": spec})
    return out


def convert_tool_choice(choice: object) -> dict[str, Any] | None:
    if isinstance(choice, str):
        table: dict[str, dict[str, Any]] = {
            "auto": {"auto": {}},
            "required": {"any": {}},
            "any": {"any": {}},
        }
        if choice in table:
            return table[choice]
        return None if choice == "none" else {"tool": {"name": choice}}
    if isinstance(choice, dict):
        function = choice.get("function")
        if isinstance(function, dict) and function.get("name"):
            return {"tool": {"name": str(function["name"])}}
    return None


def apply_reasoning(body: dict[str, Any], effort: str | None, *, model: str) -> None:
    """Claude models think through ``additionalModelRequestFields``; others are left alone."""
    level = normalize_effort(effort)
    if level is None or not is_claude(model):
        return
    budget = BUDGETS.get(level, DEFAULT_BUDGET) if level in EFFORT_LADDER else DEFAULT_BUDGET
    extra = dict(body.get("additionalModelRequestFields") or {})
    extra["thinking"] = {"type": "enabled", "budget_tokens": budget}
    body["additionalModelRequestFields"] = extra
    config = dict(body.get("inferenceConfig") or {})
    config["temperature"] = 1
    config["maxTokens"] = max(int(config.get("maxTokens") or 0), budget + THINKING_HEADROOM)
    body["inferenceConfig"] = config


def default_base_url(region: str) -> str:
    return f"https://bedrock-runtime.{region}.amazonaws.com"


# -- reply parsing ---------------------------------------------------------------------------------


def _count(value: object) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def parse_usage(raw: object) -> Usage | None:
    """Converse usage → ``Usage``; ``inputTokens`` excludes cache reads and writes."""
    if not isinstance(raw, dict):
        return None
    read = _count(raw.get("cacheReadInputTokens"))
    write = _count(raw.get("cacheWriteInputTokens"))
    return Usage(
        prompt_tokens=_count(raw.get("inputTokens")) + read + write,
        completion_tokens=_count(raw.get("outputTokens")),
        cache_read_tokens=read,
        cache_write_tokens=write,
    )


def _finish(raw: object) -> FinishReason:
    return map_finish_reason(str(raw) if raw is not None else None, STOP_TABLE)


def _exception(message: EventMessage) -> ProviderStreamError:
    kind = message.header(":exception-type") or message.header(":error-code") or "exception"
    text = message.payload.decode("utf-8", errors="replace")
    try:
        payload = json.loads(text) if text.strip() else {}
    except ValueError:
        payload = {}
    detail = ""
    if isinstance(payload, dict):
        detail = str(payload.get("message") or payload.get("Message") or "")
    detail = detail or message.header(":error-message") or kind
    return stream_error(
        f"{kind}: {detail}", error_type=kind, code=EXCEPTION_CODES.get(kind, kind), raw_text=text
    )


@dataclass
class _Block:
    kind: str = ""
    tool_index: int = -1
    args_seen: bool = False
    thinking: list[str] = field(default_factory=list[str])
    signature: str = ""
    redacted: list[str] = field(default_factory=list[str])


@dataclass
class _Stream:
    blocks: dict[int, _Block] = field(default_factory=dict[int, _Block])
    tools: int = 0
    stop: str | None = None

    def block(self, payload: Mapping[str, Any]) -> _Block:
        index = payload.get("contentBlockIndex")
        key = index if isinstance(index, int) else max(self.blocks, default=0)
        return self.blocks.setdefault(key, _Block())


type CredentialSource = Callable[[], AwsCredentials | None]


class BedrockTransport:
    api_mode = API_MODE

    def __init__(
        self,
        *,
        credentials: CredentialSource | None = None,
        now: Callable[[], datetime] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._credentials = credentials
        self._now = now
        self._env = env

    # -- request ---------------------------------------------------------------------------

    def _environment(self) -> Mapping[str, str]:
        if self._env is not None:
            return self._env
        scope = scope_or_none()
        return scope.env if scope is not None else {}

    def _time(self) -> datetime:
        if self._now is not None:
            return self._now()
        scope = scope_or_none()
        return scope.clock.now() if scope is not None else datetime.now(UTC)

    def _bearer(self, route: RouteLike, env: Mapping[str, str]) -> str | None:
        key_env = str(getattr(route, "key_env", "") or "")
        key = secret_text(route.api_key)
        if key and key_env not in FOREIGN_KEY_ENVS:
            return key
        token = env.get(BEARER_ENV, "").strip()
        return token or None

    def build_request(
        self, route: RouteLike, req: ChatRequest, *, profile: object = None
    ) -> httpx.Request:
        prof = profile_of(route, profile)
        model = req.model or route.model
        env = self._environment()
        base_url = route.base_url.rstrip("/")
        host = httpx.URL(base_url).host if base_url else ""
        region = resolve_region(host, env)
        if not base_url:
            base_url = default_base_url(region)
        system, messages = convert_messages(req.messages, model=model)
        body: dict[str, Any] = {"messages": messages}
        if system:
            body["system"] = system
        config: dict[str, Any] = {}
        max_tokens = req.max_tokens or getattr(prof, "default_max_tokens", None)
        if max_tokens:
            config["maxTokens"] = int(max_tokens)
        fixed = getattr(prof, "fixed_temperature", None)
        if fixed is not None:
            config["temperature"] = fixed
        elif req.temperature is not None:
            config["temperature"] = req.temperature
        if config:
            body["inferenceConfig"] = config
        tools = convert_tools(req.tools)
        if tools:
            tool_config: dict[str, Any] = {"tools": tools}
            choice = convert_tool_choice(req.tool_choice)
            if choice is not None:
                tool_config["toolChoice"] = choice
            body["toolConfig"] = tool_config
        apply_reasoning(body, req.reasoning_effort, model=model)
        body.update(dict(req.extra_body))
        prepare = getattr(prof, "prepare_body", None)
        if callable(prepare):
            prepared = prepare(body, model)
            if isinstance(prepared, dict):
                body = prepared

        action = "converse-stream" if req.stream else "converse"
        url = join_url(base_url, f"model/{quote(model, safe='')}/{action}")
        headers = {
            "content-type": "application/json",
            "accept": EVENT_STREAM if req.stream else "application/json",
        }
        headers.update(route_headers(route))
        content = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = httpx.Request("POST", url, headers=headers, content=content)
        bearer = self._bearer(route, env)
        if bearer:
            request.headers["authorization"] = f"Bearer {bearer}"
            return request
        credentials = (
            self._credentials() if self._credentials is not None else resolve_credentials(env)
        )
        if credentials is None:
            raise BedrockCredentialsMissing(
                "no AWS credentials for Bedrock: set a Bedrock API key or AWS access keys",
                env_names=(BEARER_ENV, ACCESS_KEY_ENV, SECRET_KEY_ENV, SHARED_FILE_ENV),
                hint=f"set {BEARER_ENV}, or {ACCESS_KEY_ENV} and {SECRET_KEY_ENV}, "
                "or a profile in ~/.aws/credentials (AWS_PROFILE picks it)",
            )
        return sign(request, credentials, region=region, service=SERVICE, now=self._time())

    def on_request_rejected(self, route: RouteLike, error: ProviderHTTPError) -> bool:
        return False

    # -- reply -----------------------------------------------------------------------------

    def read_frames(self, response: httpx.Response) -> AsyncIterator[EventMessage]:
        return aiter_messages(response)

    def error_from_stream(self, event: SSEEvent) -> ProviderStreamError | None:
        return None  # this wire has no server-sent events; exceptions arrive as frames

    async def parse_stream(self, events: AsyncIterator[Any]) -> AsyncIterator[StreamEvent]:
        state = _Stream()
        async for message in events:
            if isinstance(message, KeepAlive):
                yield message
                continue
            if not isinstance(message, EventMessage):
                continue
            kind = message.header(":message-type") or "event"
            if kind != "event":
                raise _exception(message)
            name = message.header(":event-type")
            text = message.payload.decode("utf-8", errors="replace")
            payload = json.loads(text) if text.strip() else {}
            if not isinstance(payload, dict):
                continue
            for event in self._event(state, name, payload):
                yield event
        if state.stop is None:
            raise stream_error(
                "the stream ended before the model finished its reply",
                error_type="incomplete_stream",
                code="incomplete_stream",
            )
        yield Finish(_finish(state.stop))

    @staticmethod
    def _event(state: _Stream, name: str, payload: Mapping[str, Any]) -> list[StreamEvent]:
        if name == "contentBlockStart":
            start = payload.get("start") or {}
            tool = start.get("toolUse") if isinstance(start, dict) else None
            block = state.block(payload)
            if isinstance(tool, dict):
                block.kind = "tool"
                block.tool_index = state.tools
                state.tools += 1
                call_id = tool.get("toolUseId")
                return [
                    ToolCallStart(
                        block.tool_index,
                        str(call_id) if call_id else None,
                        str(tool.get("name") or ""),
                    )
                ]
            return [KeepAlive(name)]
        if name == "contentBlockDelta":
            delta = payload.get("delta") or {}
            block = state.block(payload)
            if not isinstance(delta, dict):
                return [KeepAlive(name)]
            if isinstance(delta.get("text"), str):
                block.kind = block.kind or "text"
                return [TextDelta(delta["text"])] if delta["text"] else [KeepAlive(name)]
            tool = delta.get("toolUse")
            if isinstance(tool, dict) and block.kind == "tool":
                fragment = tool.get("input")
                if isinstance(fragment, str) and fragment:
                    block.args_seen = True
                    return [ToolCallArgsDelta(block.tool_index, fragment)]
                return [KeepAlive(name)]
            reasoning = delta.get("reasoningContent")
            if isinstance(reasoning, dict):
                block.kind = "reasoning"
                out: list[StreamEvent] = []
                if isinstance(reasoning.get("text"), str) and reasoning["text"]:
                    block.thinking.append(reasoning["text"])
                    out.append(ReasoningDelta(reasoning["text"]))
                if isinstance(reasoning.get("signature"), str):
                    block.signature += reasoning["signature"]
                if isinstance(reasoning.get("redactedContent"), str):
                    block.redacted.append(reasoning["redactedContent"])
                return out or [KeepAlive(name)]
            return [KeepAlive(name)]
        if name == "contentBlockStop":
            block = state.block(payload)
            if block.kind == "tool" and not block.args_seen:
                block.args_seen = True
                return [ToolCallArgsDelta(block.tool_index, "{}")]
            if block.kind == "reasoning":
                out = []
                if block.thinking or block.signature:
                    detail: dict[str, Any] = {
                        "type": "thinking",
                        "thinking": "".join(block.thinking),
                    }
                    if block.signature:
                        detail["signature"] = block.signature
                    out.append(ReasoningDetail(detail))
                if block.redacted:
                    out.append(
                        ReasoningDetail(
                            {"type": "redacted_thinking", "data": "".join(block.redacted)}
                        )
                    )
                return out or [KeepAlive(name)]
            return [KeepAlive(name)]
        if name == "messageStop":
            state.stop = str(payload.get("stopReason") or "end_turn")
            return [KeepAlive(name)]
        if name == "metadata":
            usage = parse_usage(payload.get("usage"))
            return [UsageReport(usage)] if usage is not None else [KeepAlive(name)]
        return [KeepAlive(name or "event")]

    def parse_response(self, payload: Mapping[str, Any]) -> NormalizedResponse:
        output = payload.get("output") or {}
        message = output.get("message") if isinstance(output, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        text: list[str] = []
        reasoning: list[str] = []
        details: list[dict[str, Any]] = []
        calls: list[ToolCall] = []
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if isinstance(block.get("text"), str):
                text.append(block["text"])
            elif isinstance(block.get("toolUse"), dict):
                tool = block["toolUse"]
                calls.append(
                    ToolCall(
                        id=str(tool.get("toolUseId") or f"call_{len(calls)}"),
                        name=str(tool.get("name") or ""),
                        arguments=json.dumps(tool.get("input") or {}, ensure_ascii=False),
                    )
                )
            elif isinstance(block.get("reasoningContent"), dict):
                inner = block["reasoningContent"]
                text_part = inner.get("reasoningText")
                if isinstance(text_part, dict):
                    thought = str(text_part.get("text") or "")
                    reasoning.append(thought)
                    detail: dict[str, Any] = {"type": "thinking", "thinking": thought}
                    if text_part.get("signature"):
                        detail["signature"] = str(text_part["signature"])
                    details.append(detail)
                if isinstance(inner.get("redactedContent"), str):
                    details.append({"type": "redacted_thinking", "data": inner["redactedContent"]})
        joined = "".join(text)
        provider_data: dict[str, Any] = {}
        metrics = payload.get("metrics")
        if isinstance(metrics, dict) and isinstance(metrics.get("latencyMs"), int):
            provider_data["latency_ms"] = metrics["latencyMs"]
        return NormalizedResponse(
            content=joined if joined else None,
            tool_calls=tuple(calls),
            finish_reason=_finish(payload.get("stopReason")),
            reasoning="".join(reasoning) or None,
            reasoning_details=tuple(details),
            usage=parse_usage(payload.get("usage")),
            provider_data=provider_data,
        )


register_transport(API_MODE, BedrockTransport)
