# pyright: strict
"""Requests an MCP server sends to us: sampling and elicitation (P13-10).

Sampling (``sampling/createMessage``): a server asks our model for a completion. Each server gets
one ``SamplingHandler`` that admits a request only when sampling is on for it, it is within
``max_rpm`` requests in the last 60 s (sliding window on the injected clock), its model (config
``sampling.model``, else the server's first hint) is in ``allowed_models`` when that list is not
empty, and its history holds at most ``max_tool_rounds`` assistant tool-use rounds.
``max_tokens`` is capped at ``max_tokens_cap``. The completion is one auxiliary call with task
``mcp_sampling`` (usage is recorded under that tag), bounded by ``sampling.timeout``. Every
failure is answered with ``ErrorData``; nothing raises into the SDK.

Elicitation (``elicitation/create``): URL mode is declined. A form becomes one ``clarify``
request (a question per field; enums and booleans as choices) on the port of the session whose
tool call to that server is in flight (``Requesters``). With no such session, an unattended one
(cron, API, background) or a client that cannot answer, the request is declined. The person's
answers are converted to the field types; a skipped required field declines; a timeout or a
withdrawn question cancels.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, cast

import mcp_types as types
from mcp.client.session import ClientRequestContext

from indus.foundation.clock import Clock
from indus.mcp.config import ElicitationSettings, McpServer, SamplingSettings
from indus.mcp.connect import clock_timeout
from indus.protocol.requests_port import (
    RequestUnsupported,
    RequestWithdrawn,
    ServerRequestPort,
)
from indus.protocol.server_requests import ClarifyAnswer
from indus.providers.types import FinishReason, NormalizedResponse

__all__ = [
    "AUX_TASK",
    "ElicitationHandler",
    "ElicitationMetrics",
    "Requester",
    "Requesters",
    "SamplingBackend",
    "SamplingHandler",
    "SamplingMetrics",
    "ServerHandlers",
    "SlidingWindow",
    "aux_backend",
    "session_kwargs",
]

log = logging.getLogger(__name__)

AUX_TASK = "mcp_sampling"
WINDOW_SECONDS = 60.0
ELICITATION_GRACE_S = 5.0
_STOP_REASONS: Mapping[FinishReason, str] = {
    "stop": "endTurn",
    "length": "maxTokens",
    "tool_calls": "toolUse",
}
_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING}


# -- the model behind sampling ------------------------------------------------------------------


class SamplingBackend(Protocol):
    """Runs one completion; returns the answer and the model that gave it."""

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str | None,
        max_tokens: int,
        temperature: float | None,
        tools: Sequence[Mapping[str, Any]],
        timeout_s: float,
    ) -> tuple[NormalizedResponse, str]: ...


class _AuxLike(Protocol):
    async def call(
        self,
        task: str,
        messages: Sequence[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout_s: float | None = None,
        tools: Sequence[Mapping[str, Any]] = (),
    ) -> Any: ...

    async def call_route(
        self,
        task: str,
        route: Any,
        messages: Sequence[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        timeout_s: float | None = None,
        temperature: float | None = None,
        tools: Sequence[Mapping[str, Any]] = (),
    ) -> Any: ...

    def route_for(self, candidate: Any) -> Any: ...


class _AuxBackend:
    def __init__(self, aux: _AuxLike) -> None:
        self._aux = aux

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str | None,
        max_tokens: int,
        temperature: float | None,
        tools: Sequence[Mapping[str, Any]],
        timeout_s: float,
    ) -> tuple[NormalizedResponse, str]:
        chat = [dict(m) for m in messages]
        if model:
            from indus.providers.auxiliary import Candidate

            route = self._aux.route_for(Candidate("pin", model=model))
            result = await self._aux.call_route(
                AUX_TASK,
                route,
                chat,
                max_tokens=max_tokens,
                timeout_s=timeout_s,
                temperature=temperature,
                tools=tools,
            )
        else:
            result = await self._aux.call(
                AUX_TASK,
                chat,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout_s=timeout_s,
                tools=tools,
            )
        return cast(NormalizedResponse, result.response), str(result.route.model)


def aux_backend(aux: object) -> SamplingBackend:
    """Sampling through the auxiliary client (``providers.auxiliary.AuxClient``)."""
    return _AuxBackend(cast(_AuxLike, aux))


# -- limits ---------------------------------------------------------------------------------------


class SlidingWindow:
    """At most ``limit`` admissions in any ``window`` seconds of ``clock`` time."""

    def __init__(self, limit: int, clock: Clock, window: float = WINDOW_SECONDS) -> None:
        self.limit = limit
        self.window = window
        self._clock = clock
        self._admitted: deque[float] = deque()

    def admit(self) -> bool:
        now = self._clock.monotonic()
        while self._admitted and self._admitted[0] <= now - self.window:
            self._admitted.popleft()
        if len(self._admitted) >= self.limit:
            return False
        self._admitted.append(now)
        return True


@dataclass
class SamplingMetrics:
    requests: int = 0
    refused: int = 0
    errors: int = 0
    tokens: int = 0
    tool_use: int = 0


# -- message conversion ---------------------------------------------------------------------------

_Block = (
    types.TextContent
    | types.ImageContent
    | types.AudioContent
    | types.ToolUseContent
    | types.ToolResultContent
)


def _blocks(message: types.SamplingMessage) -> list[_Block]:
    content = message.content
    return list(content) if isinstance(content, list) else [content]


def _result_text(block: types.ToolResultContent) -> str:
    parts: list[str] = []
    for item in block.content:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            parts.append(text)
    if not parts and block.structured_content is not None:
        parts.append(json.dumps(block.structured_content, ensure_ascii=False))
    return "\n".join(parts)


def _chat_messages(params: types.CreateMessageRequestParams) -> list[dict[str, Any]]:
    """MCP sampling messages as chat messages: tool results become ``tool`` messages, tool use
    becomes an assistant ``tool_calls`` message, images become data URLs."""
    out: list[dict[str, Any]] = []
    if params.system_prompt:
        out.append({"role": "system", "content": params.system_prompt})
    for message in params.messages:
        parts: list[dict[str, Any]] = []
        calls: list[dict[str, Any]] = []
        for block in _blocks(message):
            if isinstance(block, types.ToolResultContent):
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": block.tool_use_id,
                        "content": _result_text(block),
                    }
                )
            elif isinstance(block, types.ToolUseContent):
                calls.append(
                    {
                        "id": block.id,
                        "type": "function",
                        "function": {
                            "name": block.name,
                            "arguments": json.dumps(block.input, ensure_ascii=False),
                        },
                    }
                )
            elif isinstance(block, types.TextContent):
                parts.append({"type": "text", "text": block.text})
            elif isinstance(block, types.ImageContent):
                url = f"data:{block.mime_type};base64,{block.data}"
                parts.append({"type": "image_url", "image_url": {"url": url}})
            else:
                parts.append({"type": "text", "text": "[audio omitted]"})
        if not parts and not calls:
            continue
        entry: dict[str, Any] = {"role": message.role}
        if calls:
            entry["tool_calls"] = calls
            texts = [p["text"] for p in parts if p["type"] == "text"]
            entry["content"] = "\n".join(texts) if texts else None
        elif len(parts) == 1 and parts[0]["type"] == "text":
            entry["content"] = parts[0]["text"]
        else:
            entry["content"] = parts
        out.append(entry)
    return out


def _tool_rounds(params: types.CreateMessageRequestParams) -> int:
    return sum(
        1
        for message in params.messages
        if message.role == "assistant"
        and any(isinstance(b, types.ToolUseContent) for b in _blocks(message))
    )


def _function_tools(params: types.CreateMessageRequestParams) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for tool in params.tools or []:
        schema: dict[str, Any] = dict(tool.input_schema) if tool.input_schema else {}
        schema.setdefault("type", "object")
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": schema,
                },
            }
        )
    return tools


def _arguments(text: str) -> dict[str, Any]:
    try:
        value: object = json.loads(text) if text.strip() else {}
    except ValueError:
        return {"_raw": text}
    return cast(dict[str, Any], value) if isinstance(value, dict) else {"_raw": text}


# -- sampling -------------------------------------------------------------------------------------

SamplingAnswer = types.CreateMessageResult | types.CreateMessageResultWithTools | types.ErrorData


class SamplingHandler:
    """``sampling_callback`` for one server (see the module notes)."""

    def __init__(
        self,
        server: str,
        settings: SamplingSettings,
        backend: SamplingBackend,
        *,
        clock: Clock,
    ) -> None:
        self.server = server
        self.settings = settings
        self.backend = backend
        self.clock = clock
        self.window = SlidingWindow(settings.max_rpm, clock)
        self.metrics = SamplingMetrics()
        self._level = _LEVELS.get(settings.log_level.strip().lower(), logging.INFO)

    def _refuse(self, text: str) -> types.ErrorData:
        self.metrics.refused += 1
        log.warning("MCP server %r: sampling refused: %s", self.server, text)
        return types.ErrorData(code=types.INVALID_REQUEST, message=text)

    def _fail(self, text: str) -> types.ErrorData:
        self.metrics.errors += 1
        log.warning("MCP server %r: sampling failed: %s", self.server, text)
        return types.ErrorData(code=types.INTERNAL_ERROR, message=text)

    def _model(self, params: types.CreateMessageRequestParams) -> tuple[str | None, str | None]:
        """(model, refusal)."""
        allowed = [m for m in self.settings.allowed_models if m]
        wanted = self.settings.model
        if not wanted and params.model_preferences is not None:
            hints = params.model_preferences.hints or []
            wanted = next((h.name for h in hints if h.name), None)
        if allowed and wanted and wanted not in allowed:
            return None, f"model {wanted!r} is not allowed for this server"
        if allowed and not wanted:
            wanted = allowed[0]
        return wanted, None

    async def __call__(
        self, context: ClientRequestContext, params: types.CreateMessageRequestParams
    ) -> SamplingAnswer:
        self.metrics.requests += 1
        name = self.server
        if not self.settings.enabled:
            return self._refuse("sampling is turned off for this server")
        if not self.window.admit():
            return self._refuse(
                f"more than {self.settings.max_rpm} sampling requests in {WINDOW_SECONDS:g} s"
            )
        model, refusal = self._model(params)
        if refusal is not None:
            return self._refuse(refusal)
        rounds = _tool_rounds(params)
        if rounds > self.settings.max_tool_rounds:
            return self._refuse(f"tool loop deeper than {self.settings.max_tool_rounds} rounds")
        max_tokens = min(params.max_tokens, self.settings.max_tokens_cap)
        messages = _chat_messages(params)
        tools = _function_tools(params) if self.settings.max_tool_rounds > 0 else []
        log.log(
            self._level,
            "MCP server %r sampling: model=%s max_tokens=%d messages=%d tools=%d",
            name,
            model or "(default)",
            max_tokens,
            len(messages),
            len(tools),
        )
        try:
            async with clock_timeout(self.clock, self.settings.timeout):
                response, used = await self.backend.complete(
                    messages,
                    model=model,
                    max_tokens=max_tokens,
                    temperature=params.temperature,
                    tools=tools,
                    timeout_s=self.settings.timeout,
                )
        except TimeoutError:
            return self._fail(f"no answer within {self.settings.timeout:g} s")
        except Exception as exc:
            text = getattr(exc, "message", None) or str(exc) or type(exc).__name__
            return self._fail(f"the model call failed: {text}")
        if response.usage is not None:
            self.metrics.tokens += response.usage.prompt_tokens + response.usage.completion_tokens
        if response.tool_calls:
            if rounds >= self.settings.max_tool_rounds:
                return self._refuse(f"tool loop deeper than {self.settings.max_tool_rounds} rounds")
            self.metrics.tool_use += 1
            uses: list[_Block] = [
                types.ToolUseContent(
                    type="tool_use", id=call.id, name=call.name, input=_arguments(call.arguments)
                )
                for call in response.tool_calls
            ]
            log.log(self._level, "MCP server %r sampling: %d tool call(s)", name, len(uses))
            return types.CreateMessageResultWithTools(
                role="assistant", content=uses, model=used, stop_reason="toolUse"
            )
        if response.finish_reason == "error" or (
            response.content is None and response.finish_reason != "stop"
        ):
            return self._fail("the model gave no answer")
        stop = _STOP_REASONS.get(response.finish_reason, "endTurn")
        log.log(self._level, "MCP server %r sampling: answered (%s)", name, stop)
        return types.CreateMessageResult(
            role="assistant",
            content=types.TextContent(type="text", text=response.content or ""),
            model=used,
            stop_reason=stop,
        )


# -- elicitation ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Requester:
    """A session that can put a question to its person."""

    port: ServerRequestPort | None
    unattended: bool = False
    turn_id: str | None = None


class Requesters:
    """The sessions whose tool calls to one server are in flight; the latest answers questions.

    The call path wraps each ``tools/call`` in ``serving(...)``.
    """

    def __init__(self) -> None:
        self._active: list[Requester] = []

    @contextmanager
    def serving(
        self,
        port: ServerRequestPort | None,
        *,
        unattended: bool = False,
        turn_id: str | None = None,
    ) -> Generator[Requester]:
        entry = Requester(port, unattended, turn_id)
        self._active.append(entry)
        try:
            yield entry
        finally:
            self._active.remove(entry)

    def current(self) -> Requester | None:
        return self._active[-1] if self._active else None


@dataclass
class ElicitationMetrics:
    requests: int = 0
    accepted: int = 0
    declined: int = 0
    cancelled: int = 0
    errors: int = 0


ElicitAction = Literal["accept", "decline", "cancel"]
_YES = frozenset({"yes", "y", "true", "1", "accept"})
_NO = frozenset({"no", "n", "false", "0"})
CONFIRM_ID = "_confirm"
ACCEPT, DECLINE = "Accept", "Decline"


@dataclass(frozen=True)
class _Field:
    name: str
    kind: str
    required: bool
    choices: tuple[str, ...] | None
    multi: bool
    label: str


def _fields(schema: Mapping[str, Any]) -> list[_Field]:
    props = schema.get("properties")
    if not isinstance(props, Mapping):
        return []
    required_raw = schema.get("required")
    required = (
        {str(r) for r in cast(Sequence[object], required_raw)}
        if isinstance(required_raw, list)
        else set[str]()
    )
    found: list[_Field] = []
    for key, raw in cast(Mapping[object, object], props).items():
        name = str(key)
        spec: Mapping[str, Any] = cast(Mapping[str, Any], raw) if isinstance(raw, Mapping) else {}
        kind = str(spec.get("type") or "string")
        choices: tuple[str, ...] | None = None
        multi = False
        enum = spec.get("enum")
        if kind == "array":
            items = spec.get("items")
            inner = (
                cast(Mapping[str, Any], items).get("enum") if isinstance(items, Mapping) else None
            )
            if isinstance(inner, list):
                choices = tuple(str(v) for v in cast(list[object], inner))
                multi = True
        elif isinstance(enum, list):
            choices = tuple(str(v) for v in cast(list[object], enum))
        elif kind == "boolean":
            choices = ("yes", "no")
        title = spec.get("title")
        label = str(title) if title else name
        description = spec.get("description")
        if description:
            label = f"{label}: {description}"
        found.append(_Field(name, kind, name in required, choices, multi, label))
    return found


def _coerce(item: _Field, picked: list[str]) -> tuple[bool, Any]:
    """(ok, value) for one answered field."""
    if item.multi:
        return True, list(picked)
    text = picked[0].strip()
    if item.kind == "boolean":
        lowered = text.lower()
        if lowered in _YES:
            return True, True
        if lowered in _NO:
            return True, False
        return False, None
    if item.kind == "integer":
        try:
            return True, int(text)
        except ValueError:
            return False, None
    if item.kind == "number":
        try:
            return True, float(text)
        except ValueError:
            return False, None
    return True, text


class ElicitationHandler:
    """``elicitation_callback`` for one server (see the module notes)."""

    def __init__(
        self,
        server: str,
        settings: ElicitationSettings,
        requesters: Requesters,
        *,
        clock: Clock,
    ) -> None:
        self.server = server
        self.settings = settings
        self.requesters = requesters
        self.clock = clock
        self.metrics = ElicitationMetrics()

    def _result(
        self, action: ElicitAction, content: dict[str, Any] | None = None
    ) -> types.ElicitResult:
        if action == "accept":
            self.metrics.accepted += 1
        elif action == "decline":
            self.metrics.declined += 1
        else:
            self.metrics.cancelled += 1
        return types.ElicitResult(action=action, content=content if action == "accept" else None)

    def _questions(self, message: str, fields: list[_Field]) -> list[dict[str, Any]]:
        lead = f"The MCP server {self.server!r} asks: {message}".strip()
        if not fields:
            return [{"id": CONFIRM_ID, "text": lead, "choices": [ACCEPT, DECLINE]}]
        questions: list[dict[str, Any]] = []
        for index, item in enumerate(fields):
            text = item.label if index else f"{lead}\n\n{item.label}"
            if not item.required:
                text += " (optional)"
            question: dict[str, Any] = {"id": item.name, "text": text}
            if item.choices:
                question["choices"] = list(item.choices)
                question["multi_select"] = item.multi
            questions.append(question)
        return questions

    async def __call__(
        self, context: ClientRequestContext, params: types.ElicitRequestParams
    ) -> types.ElicitResult | types.ErrorData:
        self.metrics.requests += 1
        name = self.server
        if not self.settings.enabled:
            return self._result("decline")
        if isinstance(params, types.ElicitRequestURLParams):
            log.info("MCP server %r asked to open a URL (elicitation); declined", name)
            return self._result("decline")
        requester = self.requesters.current()
        if requester is None or requester.port is None or requester.unattended:
            log.info("MCP server %r asked a question nobody can answer here; declined", name)
            return self._result("decline")
        fields = _fields(params.requested_schema)
        payload = {"questions": self._questions(params.message, fields)}
        try:
            async with clock_timeout(self.clock, self.settings.timeout + ELICITATION_GRACE_S):
                answer = await requester.port.ask(
                    "clarify", payload, timeout=self.settings.timeout, turn_id=requester.turn_id
                )
        except RequestUnsupported:
            return self._result("decline")
        except (RequestWithdrawn, TimeoutError):
            return self._result("cancel")
        except Exception as exc:
            self.metrics.errors += 1
            log.warning("MCP server %r: the question could not be asked: %s", name, exc)
            return self._result("decline")
        if not isinstance(answer, ClarifyAnswer):
            return self._result("decline")
        picked = answer.answers
        if not fields:
            chosen = picked.get(CONFIRM_ID) or []
            accepted = bool(chosen) and chosen[0].strip().lower() in (ACCEPT.lower(), *_YES)
            return self._result("accept", {}) if accepted else self._result("decline")
        content: dict[str, Any] = {}
        for item in fields:
            values = [v for v in picked.get(item.name, []) if v.strip()]
            if not values:
                if item.required:
                    return self._result("decline")
                continue
            ok, value = _coerce(item, values)
            if not ok:
                return self._result("decline")
            content[item.name] = value
        return self._result("accept", content)


# -- wiring -------------------------------------------------------------------------------------


@dataclass
class ServerHandlers:
    """The handlers built for one server, kept for metrics and status."""

    sampling: SamplingHandler | None = None
    elicitation: ElicitationHandler | None = None
    kwargs: dict[str, Any] = field(default_factory=dict[str, Any])


def session_kwargs(
    name: str,
    server: McpServer,
    *,
    clock: Clock,
    backend: SamplingBackend | None,
    requesters: Requesters | None,
) -> ServerHandlers:
    """``ClientSession`` keyword arguments for one server: a callback only for a feature that is
    on (and possible here), so a server is never told we sample or ask when we would not."""
    handlers = ServerHandlers()
    if server.sampling.enabled and backend is not None:
        handlers.sampling = SamplingHandler(name, server.sampling, backend, clock=clock)
        handlers.kwargs["sampling_callback"] = handlers.sampling
        # Tool use in sampling is advertised only when tool rounds are allowed at all.
        tools = types.SamplingToolsCapability() if server.sampling.max_tool_rounds > 0 else None
        handlers.kwargs["sampling_capabilities"] = types.SamplingCapability(tools=tools)
    if server.elicitation.enabled and requesters is not None:
        handlers.elicitation = ElicitationHandler(name, server.elicitation, requesters, clock=clock)
        handlers.kwargs["elicitation_callback"] = handlers.elicitation
    return handlers
