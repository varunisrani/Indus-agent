"""Turn a failed model call into one verdict with recovery hints.

``classify(err, route=..., profile=...)`` returns a ``ClassifiedError`` whose ``reason`` comes from
a closed set and whose flags tell the retry loop (P03-16), and later the credential pool and the
fallback chain (P08), what to do. Stages run in order and the first verdict wins:

1. the provider profile's ``classify_error`` hook;
2. a structured error code / type in the body;
3. HTTP status rules (each refined by the message text);
4. message patterns, for errors without a status (stream error frames);
5. transport exception types (timeouts, connection failures, TLS);
6. ``unknown`` (retryable).

The pattern tables are our own, written from the vendors' documented error bodies. Messages are
redacted before they are stored, so a key echoed back by a server never travels further.
"""

from __future__ import annotations

import contextlib
import json
import logging
import ssl
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, get_args
from urllib.parse import urlsplit

import httpx

from indus.foundation.errors import ProviderHTTPError, ProviderStreamError, StaleBreakerTripped
from indus.foundation.redact import redact_text
from indus.providers.retry import parse_retry_after

if TYPE_CHECKING:
    from indus.foundation.clock import Clock

log = logging.getLogger(__name__)

Reason = Literal[
    "auth",
    "auth_permanent",
    "billing",
    "rate_limit",
    "upstream_rate_limit",
    "upstream_blocked",
    "overloaded",
    "server_error",
    "timeout",
    "connection",
    "ssl_cert",
    "context_overflow",
    "payload_too_large",
    "model_not_found",
    "content_policy",
    "format_error",
    "role_alternation",
    "stream_unsupported",
    "incomplete_response",
    "unknown",
]
REASONS: frozenset[str] = frozenset(get_args(Reason))


@dataclass(frozen=True)
class ClassifiedError:
    reason: Reason
    status: int | None = None
    message: str = ""
    retryable: bool = True
    should_rotate_credential: bool = False
    should_fallback: bool = False
    should_compress: bool = False
    retry_after: float | None = None  # seconds, capped at 600 (P03-16)
    reset_at: datetime | None = None  # when a quota window reopens (only with a clock)
    host_hint: str | None = None  # the host an auth refusal came from, when not the default
    code: str = ""  # the structured code / type the body carried, lower-case

    @property
    def is_auth(self) -> bool:
        return self.reason in ("auth", "auth_permanent")


# Recovery hints per reason. Stages pick a reason; flags come from here unless a stage overrides.
_HINTS: dict[str, dict[str, bool]] = {
    "auth": {"retryable": False, "should_rotate_credential": True, "should_fallback": True},
    "auth_permanent": {"retryable": False, "should_fallback": True},
    "billing": {"retryable": False, "should_rotate_credential": True, "should_fallback": True},
    "rate_limit": {"should_rotate_credential": True, "should_fallback": True},
    "upstream_rate_limit": {"should_fallback": True},
    "upstream_blocked": {"retryable": False, "should_fallback": True},
    "overloaded": {},
    "server_error": {},
    "timeout": {},
    "connection": {},
    "ssl_cert": {"retryable": False, "should_fallback": True},
    "context_overflow": {"retryable": False, "should_compress": True},
    "payload_too_large": {"retryable": False, "should_compress": True},
    "model_not_found": {"retryable": False, "should_fallback": True},
    "content_policy": {"retryable": False, "should_fallback": True},
    "format_error": {"retryable": False, "should_fallback": True},
    "role_alternation": {"retryable": False, "should_fallback": True},
    "stream_unsupported": {},
    "incomplete_response": {},
    "unknown": {},
}


@dataclass(frozen=True)
class Verdict:
    """What a stage decided: a reason plus optional overrides of the default hints."""

    reason: Reason
    overrides: Mapping[str, Any] = field(default_factory=dict)


# -- pattern tables (lower-case substrings) -------------------------------------------------------

OVERFLOW = (
    "context_length_exceeded",
    "maximum context length",
    "context length",
    "context window",
    "prompt is too long",
    "input is too long",
    "too many tokens",
    "too many input tokens",
    "exceeds the context",
    "exceeds the maximum context",
    "max_model_len",
    "maximum model length",
    "reduce the length of the messages",
    "please reduce your prompt",
    "input length exceeds",
    "maximum allowed input length",
    "token limit",
)
RATE_LIMIT = (
    "rate limit",
    "rate-limit",
    "rate_limit",
    "ratelimit",
    "too many requests",
    "requests per minute",
    "tokens per minute",
    "requests per day",
    "throttl",
    "resource exhausted",
    "resource_exhausted",
    "slow down",
)
OVERLOADED = (
    "overloaded",
    "over capacity",
    "at capacity",
    "capacity exceeded",
    "temporarily unavailable",
    "service unavailable",
    "server is busy",
    "engine is currently busy",
)
BILLING = (
    "insufficient_quota",
    "insufficient quota",
    "insufficient credit",
    "insufficient balance",
    "credit balance is too low",
    "out of credits",
    "credits exhausted",
    "payment required",
    "billing",
    "exceeded your current quota",
    "spending limit",
    "spend limit",
    "budget exceeded",
    "budget limit",
    "key limit exceeded",
    "account balance",
    "top up",
)
USAGE_LIMIT = ("usage limit", "quota", "limit exceeded", "limit reached")
RESET_SIGNALS = (
    "try again in",
    "retry in",
    "retry after",
    "resets in",
    "reset in",
    "resets at",
    "available again",
    "per minute",
    "per hour",
    "per second",
)
AUTH = (
    "invalid api key",
    "invalid_api_key",
    "incorrect api key",
    "invalid x-api-key",
    "authentication",
    "unauthorized",
    "unauthorised",
    "invalid token",
    "token expired",
    "token has expired",
    "api key not valid",
    "no api key",
    "missing api key",
    "permission denied",
    "access denied",
    "forbidden",
)
MODEL_MISSING = (
    "model_not_found",
    "model not found",
    "no such model",
    "unknown model",
    "invalid model",
    "is not a valid model",
    "model does not exist",
    "does not exist",
    "not a supported model",
    "unsupported model",
    "no endpoints found",
    "no allowed providers",
)
CONTENT_POLICY = (
    "content_policy_violation",
    "content policy",
    "content_filter",
    "content management policy",
    "flagged by our moderation",
    "flagged as potentially",
    "violates our usage policies",
    "safety system",
    "output blocked",
    "responsible ai policy",
)
ROLE_ALTERNATION = (
    "roles must alternate",
    "must alternate between",
    "alternating user/assistant",
    "consecutive messages with the same role",
    "consecutive user messages",
    "same role in a row",
    "first message must use the user role",
)
VALIDATION = (
    "unknown parameter",
    "unsupported parameter",
    "unrecognized request argument",
    "unrecognized parameter",
    "unknown_parameter",
    "unsupported_parameter",
    "extra inputs are not permitted",
    "invalid value for",
    "is not a valid enum value",
    "missing required parameter",
    "request validation failed",
)
PAYLOAD_TOO_LARGE = (
    "request entity too large",
    "payload too large",
    "request_too_large",
    "request too large",
    "body too large",
)
WAF_MARKERS = (
    "cloudflare",
    "cf-ray",
    "attention required",
    "just a moment...",
    "checking your browser",
    "enable javascript and cookies",
    "request blocked",
    "you have been blocked",
    "access denied | ",
    "akamai",
    "captcha",
)
UPSTREAM_WRAPPERS = ("provider returned error", "upstream error", "upstream provider")
STREAM_UNSUPPORTED = (
    "streaming is not supported",
    "stream is not supported",
    "streaming not supported",
    "does not support streaming",
    "stream mode is not supported",
    "'stream' is not supported",
)
TIMEOUT_WORDS = ("timed out", "timeout", "deadline exceeded")
CONNECTION_WORDS = (
    "connection refused",
    "connection reset",
    "connection aborted",
    "connection closed",
    "server disconnected",
    "peer closed connection",
    "name or service not known",
    "temporary failure in name resolution",
    "nodename nor servname",
    "network is unreachable",
    "no route to host",
    "unexpected eof",
)
CERT_WORDS = (
    "certificate verify failed",
    "certificate_verify_failed",
    "self signed certificate",
    "self-signed certificate",
    "certificate has expired",
    "unable to get local issuer certificate",
    "hostname mismatch",
)

# Structured codes / types → reasons. Only specific codes: OpenAI stamps the generic
# ``invalid_request_error`` type on overflow and validation errors alike, so it decides nothing.
CODES: dict[str, Reason] = {
    # OpenAI-style ``error.code``
    "context_length_exceeded": "context_overflow",
    "string_above_max_length": "context_overflow",
    "insufficient_quota": "billing",
    "billing_hard_limit_reached": "billing",
    "billing_not_active": "billing",
    "rate_limit_exceeded": "rate_limit",
    "model_not_found": "model_not_found",
    "invalid_api_key": "auth",
    "account_deactivated": "auth_permanent",
    "unsupported_parameter": "format_error",
    "unknown_parameter": "format_error",
    "content_policy_violation": "content_policy",
    "content_filter": "content_policy",
    "server_error": "server_error",
    # Anthropic-style ``error.type``
    "authentication_error": "auth",
    "billing_error": "billing",
    "rate_limit_error": "rate_limit",
    "overloaded_error": "overloaded",
    "api_error": "server_error",
    "request_too_large": "payload_too_large",
    "timeout_error": "timeout",
    # Our own stream codes (transports and the stream driver)
    "incomplete_stream": "incomplete_response",
    "stale_stream": "timeout",
}


# -- the context a stage sees ---------------------------------------------------------------------


@dataclass(frozen=True)
class ErrorContext:
    """Everything known about one failure; profile hooks receive this."""

    error: BaseException
    status: int | None
    body: Any
    headers: Mapping[str, str]
    code: str
    text: str  # lower-case: exception text plus every message in the body
    message: str  # the most specific human message, unredacted
    route: Any = None

    def has(self, patterns: Sequence[str]) -> bool:
        return any(p in self.text for p in patterns)


def _body_of(err: BaseException) -> Any:
    if isinstance(err, ProviderHTTPError | ProviderStreamError):
        body: Any = err.body
    elif isinstance(err, httpx.HTTPStatusError):
        try:
            body = err.response.json()
        except (ValueError, httpx.ResponseNotRead):
            body = _safe_text(err.response)
    else:
        body = getattr(err, "body", None)
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    if isinstance(body, str) and body.strip()[:1] in ("{", "["):
        with contextlib.suppress(ValueError):
            body = json.loads(body)
    return body


def _safe_text(response: httpx.Response) -> str:
    try:
        return response.text
    except httpx.ResponseNotRead:
        return ""


def _status_of(err: BaseException) -> int | None:
    if isinstance(err, ProviderHTTPError):
        return err.status
    if isinstance(err, ProviderStreamError):
        return err.status
    if isinstance(err, httpx.HTTPStatusError):
        return err.response.status_code
    for attr in ("status", "status_code"):
        value = getattr(err, attr, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _headers_of(err: BaseException) -> Mapping[str, str]:
    if isinstance(err, ProviderHTTPError):
        return {k.lower(): v for k, v in err.headers.items()}
    if isinstance(err, httpx.HTTPStatusError):
        return {k.lower(): v for k, v in err.response.headers.items()}
    return {}


def _error_object(body: Any) -> Mapping[str, Any]:
    if isinstance(body, Mapping):
        inner = body.get("error")  # pyright: ignore[reportUnknownMemberType]
        if isinstance(inner, Mapping):
            return inner  # pyright: ignore[reportUnknownVariableType]
        return body  # pyright: ignore[reportUnknownVariableType]
    return {}


def _code_of(err: BaseException, body: Any) -> str:
    obj = _error_object(body)
    for key in ("code", "type", "status"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip() and value.strip().lower() != "error":
            return value.strip().lower()
    return ""


def _messages(body: Any) -> list[str]:
    found: list[str] = []
    if isinstance(body, str):
        found.append(body)
    elif isinstance(body, Mapping):
        obj = _error_object(body)
        for key in ("message", "detail", "error_description"):
            value = obj.get(key)
            if isinstance(value, str) and value:
                found.append(value)
        if isinstance(body.get("error"), str):  # pyright: ignore[reportUnknownMemberType]
            found.append(str(body["error"]))  # pyright: ignore[reportUnknownArgumentType]
        metadata = obj.get("metadata")
        if isinstance(metadata, Mapping):
            raw = metadata.get("raw")  # pyright: ignore[reportUnknownMemberType]
            if isinstance(raw, str) and raw:
                found.append(raw)
        if not found:
            found.append(json.dumps(body, default=str)[:2000])
    return found


def _context(err: BaseException, route: Any) -> ErrorContext:
    body = _body_of(err)
    messages = _messages(body)
    exc_text = str(err)
    message = messages[0] if messages else exc_text
    text = " ".join([exc_text, *messages, type(err).__name__]).lower()
    return ErrorContext(
        error=err,
        status=_status_of(err),
        body=body,
        headers=_headers_of(err),
        code=_code_of(err, body),
        text=text,
        message=message.strip() or exc_text or type(err).__name__,
        route=route,
    )


# -- stages ---------------------------------------------------------------------------------------


def _profile_stage(ctx: ErrorContext, profile: Any) -> ClassifiedError | Verdict | None:
    hook = getattr(profile, "classify_error", None) if profile is not None else None
    if not callable(hook):
        return None
    try:
        result = hook(ctx)
    except Exception:
        log.debug("profile classify_error hook failed; using the built-in rules", exc_info=True)
        return None
    if result is None or isinstance(result, ClassifiedError):
        return result
    if isinstance(result, Verdict):
        return result if result.reason in REASONS else None
    if isinstance(result, str):
        return Verdict(result) if result in REASONS else None  # type: ignore[arg-type]
    if isinstance(result, Mapping):
        reason = result.get("reason")  # pyright: ignore[reportUnknownMemberType]
        if reason not in REASONS:
            return None
        overrides = {
            k: bool(v)  # pyright: ignore[reportUnknownArgumentType]
            for k, v in result.items()  # pyright: ignore[reportUnknownVariableType]
            if k in ("retryable", "should_rotate_credential", "should_fallback", "should_compress")
        }
        return Verdict(reason, overrides)  # type: ignore[arg-type]
    return None


def _code_stage(ctx: ErrorContext) -> Verdict | None:
    if isinstance(ctx.error, StaleBreakerTripped):
        return Verdict("timeout", {"retryable": False, "should_fallback": True})
    reason = CODES.get(ctx.code)
    if reason is None:
        return None
    if reason == "rate_limit" and ctx.has(BILLING) and not ctx.has(RESET_SIGNALS):
        return Verdict("billing")
    return Verdict(reason)


def _status_stage(ctx: ErrorContext) -> Verdict | None:
    status = ctx.status
    if status is None or status < 400:
        return None
    handler = _STATUS_HANDLERS.get(status)
    if handler is not None:
        return handler(ctx)
    if 500 <= status < 600:
        return _server_error(ctx, "server_error")
    return _client_error(ctx)


def _status_401(ctx: ErrorContext) -> Verdict:
    return Verdict("auth")


def _status_402(ctx: ErrorContext) -> Verdict:
    if ctx.has(USAGE_LIMIT) and ctx.has(RESET_SIGNALS):
        return Verdict("rate_limit")
    return Verdict("billing")


def _status_403(ctx: ErrorContext) -> Verdict:
    if ctx.has(CONTENT_POLICY):
        return Verdict("content_policy")
    if ctx.has(BILLING):
        return Verdict("billing")
    if ctx.has(WAF_MARKERS) or _looks_like_html(ctx):
        return Verdict("upstream_blocked")
    return Verdict("auth")


def _status_404(ctx: ErrorContext) -> Verdict:
    if ctx.has(MODEL_MISSING):
        return Verdict("model_not_found")
    return Verdict("unknown")


def _status_429(ctx: ErrorContext) -> Verdict:
    if ctx.has(OVERLOADED):
        return Verdict("overloaded")
    if _aggregator_upstream(ctx):
        return Verdict("upstream_rate_limit")
    quota = ctx.has(BILLING) or ctx.has(USAGE_LIMIT)
    reset = ctx.has(RESET_SIGNALS) or "retry-after" in ctx.headers
    if quota and not ctx.has(RATE_LIMIT) and not reset:
        return Verdict("billing")
    return Verdict("rate_limit")


def _server_error(ctx: ErrorContext, default: Reason) -> Verdict:
    if ctx.has(VALIDATION):
        return Verdict("format_error")
    if ctx.has(OVERFLOW):
        return Verdict("context_overflow")
    return Verdict(default)


def _client_error(ctx: ErrorContext) -> Verdict:
    if ctx.has(CONTENT_POLICY):
        return Verdict("content_policy")
    if ctx.has(ROLE_ALTERNATION):
        return Verdict("role_alternation")
    if ctx.has(STREAM_UNSUPPORTED):
        return Verdict("stream_unsupported")
    if ctx.has(VALIDATION):
        return Verdict("format_error")
    if ctx.has(PAYLOAD_TOO_LARGE):
        return Verdict("payload_too_large")
    if ctx.has(OVERFLOW):
        return Verdict("context_overflow")
    if ctx.has(MODEL_MISSING):
        return Verdict("model_not_found")
    if ctx.has(RATE_LIMIT):
        return Verdict("rate_limit")
    if ctx.has(BILLING):
        return Verdict("billing")
    return Verdict("format_error")


_STATUS_HANDLERS: dict[int, Callable[[ErrorContext], Verdict]] = {
    401: _status_401,
    402: _status_402,
    403: _status_403,
    404: _status_404,
    408: lambda ctx: Verdict("timeout"),
    413: lambda ctx: Verdict("payload_too_large"),
    429: _status_429,
    500: lambda ctx: _server_error(ctx, "server_error"),
    502: lambda ctx: _server_error(ctx, "server_error"),
    503: lambda ctx: _server_error(ctx, "overloaded"),
    504: lambda ctx: Verdict("timeout"),
    529: lambda ctx: _server_error(ctx, "overloaded"),
}


def _message_stage(ctx: ErrorContext) -> Verdict | None:
    if ctx.status is not None and ctx.status >= 400:
        return None
    rules: tuple[tuple[Sequence[str], Reason], ...] = (
        (CONTENT_POLICY, "content_policy"),
        (STREAM_UNSUPPORTED, "stream_unsupported"),
        (PAYLOAD_TOO_LARGE, "payload_too_large"),
        (ROLE_ALTERNATION, "role_alternation"),
        (OVERLOADED, "overloaded"),
    )
    for patterns, reason in rules:
        if ctx.has(patterns):
            return Verdict(reason)
    if ctx.has(USAGE_LIMIT) and not ctx.has(RATE_LIMIT):
        return Verdict("rate_limit" if ctx.has(RESET_SIGNALS) else "billing")
    rules = (
        (BILLING, "billing"),
        (RATE_LIMIT, "rate_limit"),
        (OVERFLOW, "context_overflow"),
        (MODEL_MISSING, "model_not_found"),
        (VALIDATION, "format_error"),
        (CERT_WORDS, "ssl_cert"),
    )
    for patterns, reason in rules:
        if ctx.has(patterns):
            return Verdict(reason)
    # Auth words only for errors that came from a provider, not for e.g. a local TLS failure.
    if isinstance(ctx.error, ProviderStreamError | ProviderHTTPError) and ctx.has(AUTH):
        return Verdict("auth")
    return None


def _transport_stage(ctx: ErrorContext) -> Verdict | None:
    err = ctx.error
    if isinstance(err, ssl.SSLCertVerificationError) or (
        isinstance(err, httpx.TransportError | ssl.SSLError | OSError) and ctx.has(CERT_WORDS)
    ):
        return Verdict("ssl_cert")
    if isinstance(err, httpx.TimeoutException | TimeoutError):
        return Verdict("timeout")
    if isinstance(
        err,
        httpx.ConnectError
        | httpx.RemoteProtocolError
        | httpx.ReadError
        | httpx.WriteError
        | httpx.NetworkError
        | ConnectionError,
    ):
        return Verdict("connection")
    if isinstance(err, httpx.TransportError | OSError):
        if ctx.has(TIMEOUT_WORDS):
            return Verdict("timeout")
        return Verdict("connection")
    if ctx.has(CONNECTION_WORDS):
        return Verdict("connection")
    if ctx.has(TIMEOUT_WORDS):
        return Verdict("timeout")
    return None


def _looks_like_html(ctx: ErrorContext) -> bool:
    return isinstance(ctx.body, str) and "<html" in ctx.body.lower()


def _aggregator_upstream(ctx: ErrorContext) -> bool:
    obj = _error_object(ctx.body)
    message = str(obj.get("message") or "").strip().lower()
    metadata = obj.get("metadata")
    wrapped = isinstance(metadata, Mapping) and (
        "provider_name" in metadata or "raw" in metadata  # pyright: ignore[reportUnknownMemberType]
    )
    return wrapped or any(message.startswith(w) for w in UPSTREAM_WRAPPERS)


# -- assembling the verdict -----------------------------------------------------------------------


def classify(
    err: BaseException,
    *,
    route: Any = None,
    profile: Any = None,
    clock: Clock | None = None,
) -> ClassifiedError:
    """Classify ``err``; never raises."""
    ctx = _context(err, route)
    now = clock.now() if clock is not None else None
    decided = _profile_stage(ctx, profile)
    if isinstance(decided, ClassifiedError):
        return replace(decided, message=_redacted(decided.message, route))
    verdict = (
        decided
        or _code_stage(ctx)
        or _status_stage(ctx)
        or _message_stage(ctx)
        or _transport_stage(ctx)
        or Verdict("unknown")
    )
    hints: dict[str, Any] = {
        "retryable": True,
        "should_rotate_credential": False,
        "should_fallback": False,
        "should_compress": False,
        **_HINTS[verdict.reason],
        **verdict.overrides,
    }
    retry_after = _retry_after(ctx, now)
    reset_at = None
    if verdict.reason in ("rate_limit", "upstream_rate_limit", "billing") and now is not None:
        reset_seconds = _reset_seconds(ctx, now)
        if reset_seconds is not None:
            reset_at = now + timedelta(seconds=reset_seconds)
    if verdict.reason == "rate_limit" and retry_after is None and ctx.status == 402:
        retry_after = _reset_seconds(ctx, now, cap=True)
    message = ctx.message
    host_hint = None
    if verdict.reason in ("auth", "auth_permanent"):
        host_hint = _off_default_host(route, profile)
        if host_hint:
            message = f"{message} (sent to {host_hint})"
    return ClassifiedError(
        reason=verdict.reason,
        status=ctx.status,
        message=_redacted(message, route),
        retry_after=retry_after,
        reset_at=reset_at,
        host_hint=host_hint,
        code=ctx.code,
        **hints,
    )


def _retry_after(ctx: ErrorContext, now: datetime | None) -> float | None:
    found = parse_retry_after(ctx.headers, now=now) if ctx.headers else None
    if found is None and isinstance(ctx.body, Mapping):
        found = parse_retry_after(ctx.body, now=now)
    return found


def _reset_seconds(ctx: ErrorContext, now: datetime | None, *, cap: bool = False) -> float | None:
    limit = 600.0 if cap else None
    for source in (ctx.headers or None, ctx.body if isinstance(ctx.body, Mapping) else None):
        if source is not None:
            found = parse_retry_after(source, now=now, cap=limit)
            if found is not None:
                return found
    return parse_retry_after(ctx.text, now=now, cap=limit)


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").rstrip(".").lower()
    except ValueError:
        return ""


def _off_default_host(route: Any, profile: Any) -> str | None:
    base_url = getattr(route, "base_url", None)
    if not isinstance(base_url, str) or not base_url:
        return None
    host = _host(base_url)
    if not host:
        return None
    if profile is None:
        return host
    default = getattr(profile, "hostname", None) or _host(str(getattr(profile, "base_url", "")))
    if isinstance(default, str) and default.rstrip(".").lower() == host:
        return None
    return host


def route_key(route: Any) -> str:
    """The route's key as text (a secret wrapper is unwrapped), or ""."""
    key = getattr(route, "api_key", None)
    if key is None:
        return ""
    for name in ("reveal", "get_secret_value", "value"):
        attr = getattr(key, name, None)
        if callable(attr):
            try:
                return str(attr())
            except Exception:
                return ""
        if isinstance(attr, str):
            return attr
    return key if isinstance(key, str) else ""


def _redacted(message: str, route: Any) -> str:
    key = route_key(route)
    return redact_text(message, [key] if key else [])


__all__ = [
    "CODES",
    "REASONS",
    "ClassifiedError",
    "ErrorContext",
    "Reason",
    "Verdict",
    "classify",
    "route_key",
]
