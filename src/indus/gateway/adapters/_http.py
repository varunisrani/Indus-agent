"""The listener kit for adapters that receive vendor callbacks over HTTP (P16-01).

Serving reuses the P15 helpers in ``gateway.http.serving`` (``bind_listener``, ``HttpServer``,
``LengthGate``, ``SecurityHeaders``, ``read_body``); this module adds what callback adapters share.

Listener (``CallbackListener``): one per adapter port. ``ListenerSettings`` come from the
platform's ``webhook_*`` keys (``from_extra``); the host is 127.0.0.1 unless the operator sets a
non-loopback ``webhook_host`` **and** ``webhook_public: true``. Requests, in order:

1. ``/health`` (GET/HEAD) answers ``{"status": "ok"|"stopping", "platform": name}``;
2. an unknown path is 404, a method the route does not take is 405;
3. before ``start`` and from ``stop`` on, routes answer 503 with ``Retry-After`` (nothing read);
4. a declared ``Content-Length`` over the cap is 413 (a malformed one 400) before any byte is
   read; a body without one is counted as it arrives and cut with 413 once over the cap, the rest
   never read;
5. the route's handler gets a `CallbackRequest` and returns a `Reply`; an exception is 500 with no
   detail (logged through the adapter's ``scrub``).

Handlers check the vendor's signature themselves with the helpers below (vendors sign different
things), answer fast, and hand slow work to ``background(...)``. ``stop(budget)`` closes the door,
lets running handlers finish for a short drain, cancels the rest and background work, and stops
the server inside the adapter's 5 s disconnect budget; the socket is always closed.
``serve=False`` keeps the whole app but binds nothing: wire fakes call ``app`` in-process.

Signatures: every comparison goes through ``constant_time_equal`` (``hmac.compare_digest``).
HMAC-SHA256 as hex (optionally prefixed, e.g. ``sha256=``) or base64; the SHA1 signature over the
sorted parameter strings (WeCom/WeChat callbacks); HMAC-SHA1 over the URL plus the sorted
parameters, base64 (Twilio).

Replay: ``ReplayWindow`` refuses timestamps outside ±window and keys seen inside it; ``SeenIds``
is the bounded, expiring id memory behind it (also used for message-id dedupe).

Outbound (R06.4): ``failure_from_exception`` and ``failure_from_status`` turn an httpx failure or
a vendor error status into a classified `SendResult` by class and status, not by text: a connect
failure is retryable, a read or write timeout (or a dropped response) is "maybe delivered" and
never retried, 429 carries the server's ``Retry-After``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import urllib.parse
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable, Mapping, MutableMapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal, cast

import httpx
from starlette.requests import Request

from indus.foundation.clock import Clock
from indus.gateway.adapters.base import AdapterConnectError
from indus.gateway.core.send import ErrorKind, SendResult
from indus.gateway.http.serving import (
    Asgi,
    BodyTooLarge,
    HttpServer,
    LengthGate,
    SecurityHeaders,
    bind_listener,
    is_loopback,
    read_body,
)

__all__ = [
    "DEFAULT_HOST",
    "DRAIN_S",
    "HEALTH_PATH",
    "MAX_BODY_CEILING",
    "STOP_BUDGET_S",
    "BadPayload",
    "CallbackListener",
    "CallbackRequest",
    "Handler",
    "ListenerError",
    "ListenerSettings",
    "ReplayWindow",
    "Reply",
    "SeenIds",
    "constant_time_equal",
    "failure_from_exception",
    "failure_from_status",
    "hmac_sha1_url_signature",
    "hmac_sha256_base64",
    "hmac_sha256_hex",
    "retry_after_seconds",
    "sha1_sorted_signature",
    "verify_hmac_sha256_base64",
    "verify_hmac_sha256_hex",
    "verify_sha1_sorted",
    "verify_url_signature",
]

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
HEALTH_PATH = "/health"
# The adapter disconnect budget; ``stop`` finishes inside it.
STOP_BUDGET_S = 5.0
# How long running handlers may finish after the door closes (bounded by the budget too).
DRAIN_S = 2.0
# No config may raise a listener's body cap above this.
MAX_BODY_CEILING = 16 * 1024 * 1024
RETRY_LATER_S = 5


# -- settings ---------------------------------------------------------------------------------


class ListenerError(ValueError):
    """A listener setting that cannot be served safely (a configuration problem)."""


def _truthy(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return value is True


@dataclass(frozen=True)
class ListenerSettings:
    port: int
    max_body_bytes: int
    host: str = DEFAULT_HOST
    path: str = "/webhook"
    # Binding a non-loopback host needs this as well (see the module notes).
    public: bool = False
    # The URL the vendor calls (behind a proxy or tunnel), for signatures over the URL.
    public_url: str | None = None

    @classmethod
    def from_extra(
        cls,
        extra: Mapping[str, Any],
        *,
        port: int,
        max_body_bytes: int,
        path: str = "/webhook",
    ) -> ListenerSettings:
        """Settings from ``platforms.<name>`` keys (``webhook_host``, ``webhook_port``,
        ``webhook_path``, ``webhook_public``, ``webhook_public_url``,
        ``webhook_max_body_bytes``), the adapter's defaults otherwise. ``ListenerError`` when a
        value cannot be read."""

        def number(key: str, default: int) -> int:
            raw = extra.get(key)
            if raw is None or raw == "":
                return default
            if isinstance(raw, bool):
                raise ListenerError(f"{key} must be a whole number")
            try:
                return int(str(raw).strip())
            except ValueError:
                raise ListenerError(f"{key} must be a whole number") from None

        host = str(extra.get("webhook_host") or DEFAULT_HOST).strip()
        url = str(extra.get("webhook_public_url") or "").strip() or None
        settings = cls(
            port=number("webhook_port", port),
            max_body_bytes=number("webhook_max_body_bytes", max_body_bytes),
            host=host,
            path=str(extra.get("webhook_path") or path).strip(),
            public=_truthy(extra.get("webhook_public")),
            public_url=url,
        )
        settings.check()
        return settings

    def check(self) -> None:
        """``ListenerError`` for settings the kit refuses to serve."""
        if not self.host:
            raise ListenerError("webhook_host is empty")
        if not is_loopback(self.host) and not self.public:
            raise ListenerError(
                f"webhook_host {self.host} is not a loopback address; set webhook_public: true "
                "to accept callbacks from other machines"
            )
        if not 0 <= self.port <= 65535:
            raise ListenerError(f"webhook_port {self.port} is not a port number")
        if not 0 < self.max_body_bytes <= MAX_BODY_CEILING:
            raise ListenerError(f"webhook_max_body_bytes must be between 1 and {MAX_BODY_CEILING}")
        if not self.path.startswith("/") or self.path == HEALTH_PATH:
            raise ListenerError(f"webhook_path {self.path!r} must start with / and not be /health")
        if self.public_url is not None and not self.public_url.startswith(("https://", "http://")):
            raise ListenerError("webhook_public_url must be an http(s) URL")


# -- requests and replies ---------------------------------------------------------------------


class BadPayload(ValueError):
    """A callback body that is not the JSON or form data the handler expected."""


@dataclass(frozen=True)
class CallbackRequest:
    method: str
    path: str
    # The raw query string as sent (signatures over the URL need it unchanged).
    query_string: str
    # Lower-case names; repeated headers joined with ", ".
    headers: Mapping[str, str]
    body: bytes
    client: str | None
    received_at: datetime

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    def query_params(self) -> dict[str, str]:
        """Query parameters (the first value of a repeated name)."""
        out: dict[str, str] = {}
        for key, value in urllib.parse.parse_qsl(self.query_string, keep_blank_values=True):
            out.setdefault(key, value)
        return out

    def form_pairs(self) -> list[tuple[str, str]]:
        """``application/x-www-form-urlencoded`` fields in order (``BadPayload`` otherwise)."""
        try:
            text = self.body.decode("utf-8")
            return urllib.parse.parse_qsl(text, keep_blank_values=True, strict_parsing=bool(text))
        except (UnicodeDecodeError, ValueError):
            raise BadPayload("the body is not form data") from None

    def form(self) -> dict[str, str]:
        return dict(self.form_pairs())

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except (ValueError, UnicodeDecodeError):
            raise BadPayload("the body is not JSON") from None

    def signed_url(self, public_url: str | None = None) -> str:
        """The URL the vendor called, query included: ``public_url`` (as configured) when given,
        else rebuilt from the ``Host`` header."""
        if public_url:
            base = public_url
        else:
            host = self.headers.get("host", "localhost")
            proto = self.headers.get("x-forwarded-proto", "http").split(",")[0].strip()
            base = f"{proto or 'http'}://{host}{self.path}"
        return f"{base}?{self.query_string}" if self.query_string else base


@dataclass(frozen=True)
class Reply:
    status: int = 200
    body: bytes = b""
    content_type: str = "text/plain; charset=utf-8"
    headers: tuple[tuple[str, str], ...] = ()

    @classmethod
    def json(cls, data: object, status: int = 200, **kw: Any) -> Reply:
        return cls(status, json.dumps(data).encode(), "application/json", **kw)

    @classmethod
    def text(cls, text: str, status: int = 200, **kw: Any) -> Reply:
        return cls(status, text.encode("utf-8"), **kw)

    @classmethod
    def empty(cls, status: int = 200) -> Reply:
        return cls(status)


Handler = Callable[[CallbackRequest], Awaitable[Reply]]

type _Scope = MutableMapping[str, Any]
type _Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
type _Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class _Route:
    handler: Handler
    methods: frozenset[str]


async def _answer(send: _Send, reply: Reply) -> None:
    headers = [
        (b"content-type", reply.content_type.encode("latin-1")),
        (b"content-length", str(len(reply.body)).encode()),
        *((k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in reply.headers),
    ]
    await send({"type": "http.response.start", "status": reply.status, "headers": headers})
    await send({"type": "http.response.body", "body": reply.body})


def _error(status: int, message: str, *headers: tuple[str, str]) -> Reply:
    return Reply.json({"error": message}, status, headers=tuple(headers))


def _header_map(scope: _Scope) -> dict[str, str]:
    found: dict[str, str] = {}
    for raw_key, raw_value in scope.get("headers") or ():
        key = bytes(raw_key).decode("latin-1").lower()
        value = bytes(raw_value).decode("latin-1")
        found[key] = f"{found[key]}, {value}" if key in found else value
    return found


class CallbackListener:
    """One callback port of one adapter (see the module notes)."""

    def __init__(
        self,
        settings: ListenerSettings,
        *,
        name: str,
        clock: Clock,
        scrub: Callable[[str], str] | None = None,
        serve: bool = True,
    ) -> None:
        self.settings = settings
        self.name = name
        self.clock = clock
        self.scrub = scrub or (lambda text: text)
        self.serve = serve
        self._routes: dict[str, _Route] = {}
        self._accepting = False
        self._server: HttpServer | None = None
        self._handlers: set[asyncio.Task[Reply]] = set()
        self._background: set[asyncio.Task[Any]] = set()
        self.app: Asgi = SecurityHeaders(
            LengthGate(self._dispatch, settings.max_body_bytes, self._too_large)
        )

    # -- routes ---------------------------------------------------------------------------------

    def route(self, path: str, handler: Handler, *, methods: Iterable[str] = ("POST",)) -> None:
        if not path.startswith("/") or path == HEALTH_PATH:
            raise ValueError(f"route path {path!r} must start with / and not be {HEALTH_PATH}")
        if path in self._routes:
            raise ValueError(f"route {path} is already registered")
        wanted = frozenset(m.upper() for m in methods)
        if not wanted:
            raise ValueError("a route needs at least one method")
        self._routes[path] = _Route(handler, wanted)

    # -- state ----------------------------------------------------------------------------------

    @property
    def accepting(self) -> bool:
        return self._accepting

    @property
    def address(self) -> tuple[str, int] | None:
        return self._server.address if self._server is not None else None

    async def start(self) -> None:
        """Bind and serve (or, with ``serve=False``, only open the door). Raises
        `AdapterConnectError`: not retryable for bad settings, retryable when binding fails."""
        if self._accepting:
            return
        try:
            self.settings.check()
        except ListenerError as exc:
            raise AdapterConnectError(str(exc), retryable=False, code="listener_config") from None
        if self.serve:
            host, port = self.settings.host, self.settings.port
            try:
                sock = await bind_listener(host, port, self.clock)
            except OSError as exc:
                raise AdapterConnectError(
                    f"cannot listen on {host}:{port} for {self.name} callbacks: "
                    f"{exc.strerror or type(exc).__name__}",
                    retryable=True,
                    code="listener_bind",
                ) from None
            server = HttpServer(self.app, sock, name=f"{self.name} callbacks")
            try:
                await server.start()
            except OSError as exc:
                await server.stop()
                raise AdapterConnectError(
                    f"the {self.name} callback server did not start: {type(exc).__name__}",
                    retryable=True,
                    code="listener_bind",
                ) from None
            self._server = server
        self._accepting = True

    async def stop(self, budget: float = STOP_BUDGET_S) -> None:
        """Close the door, drain, cancel, stop the server — all inside ``budget`` seconds."""
        if not (self._accepting or self._server or self._handlers or self._background):
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(budget, 0.1)
        self._accepting = False
        running = set(self._handlers)
        if running:
            drain = min(DRAIN_S, max(budget / 2, 0.0))
            await asyncio.wait(running, timeout=drain)
        for task in [*self._handlers, *self._background]:
            task.cancel()
        leftovers = [*self._handlers, *self._background]
        if leftovers:
            await asyncio.wait(leftovers, timeout=max(deadline - loop.time(), 0.05))
        server, self._server = self._server, None
        if server is not None:
            try:
                await asyncio.wait_for(server.stop(), max(deadline - loop.time(), 0.05))
            except TimeoutError:
                log.warning("%s callbacks: the server did not stop in time", self.name)
            finally:
                server.sock.close()

    def background(self, work: Awaitable[Any]) -> asyncio.Task[Any]:
        """Run ``work`` after the reply went out; cancelled by ``stop``. Failures are logged."""
        task: asyncio.Task[Any] = asyncio.ensure_future(work)
        self._background.add(task)
        task.add_done_callback(self._background_done)
        return task

    def _background_done(self, task: asyncio.Task[Any]) -> None:
        self._background.discard(task)
        if not task.cancelled() and task.exception() is not None:
            exc = task.exception()
            log.warning(
                "%s callbacks: background work failed: %s",
                self.name,
                self.scrub(f"{type(exc).__name__}: {exc}"),
            )

    async def idle(self) -> None:
        """Wait until no handler or background work is running (tests, wire fakes)."""
        while self._handlers or self._background:
            await asyncio.wait({*self._handlers, *self._background})

    # -- the ASGI app ---------------------------------------------------------------------------

    @staticmethod
    def _too_large(limit: int) -> dict[str, str]:
        return {"error": "invalid Content-Length" if limit < 0 else "payload too large"}

    async def _dispatch(self, scope: _Scope, receive: _Receive, send: _Send) -> None:
        if scope["type"] != "http":
            return
        path = str(scope.get("path") or "/")
        method = str(scope.get("method") or "GET").upper()
        if path == HEALTH_PATH:
            if method not in ("GET", "HEAD"):
                await _answer(send, _error(405, "method not allowed", ("allow", "GET, HEAD")))
                return
            status = "ok" if self._accepting else "stopping"
            await _answer(send, Reply.json({"status": status, "platform": self.name}))
            return
        route = self._routes.get(path)
        if route is None:
            await _answer(send, _error(404, "not found"))
            return
        if method not in route.methods:
            allow = ", ".join(sorted(route.methods))
            await _answer(send, _error(405, "method not allowed", ("allow", allow)))
            return
        if not self._accepting:
            await _answer(
                send, _error(503, "not accepting callbacks", ("retry-after", str(RETRY_LATER_S)))
            )
            return
        try:
            body = await read_body(Request(scope, receive), self.settings.max_body_bytes)
        except BodyTooLarge:
            await _answer(send, _error(413, "payload too large"))
            return
        client = scope.get("client")
        request = CallbackRequest(
            method=method,
            path=path,
            query_string=bytes(scope.get("query_string") or b"").decode("latin-1"),
            headers=_header_map(scope),
            body=body,
            client=str(client[0]) if client else None,
            received_at=self.clock.now().astimezone(UTC),
        )
        await _answer(send, await self._run(route, request))

    async def _run(self, route: _Route, request: CallbackRequest) -> Reply:
        task: asyncio.Task[Reply] = asyncio.ensure_future(route.handler(request))
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)
        try:
            return await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise  # the connection itself is going away
            # ``stop`` cancelled the handler after the drain.
            return _error(503, "shutting down", ("retry-after", str(RETRY_LATER_S)))
        except Exception as exc:
            log.warning(
                "%s callbacks: handler for %s failed: %s",
                self.name,
                request.path,
                self.scrub(f"{type(exc).__name__}: {exc}"),
            )
            return _error(500, "internal error")


# -- signatures -------------------------------------------------------------------------------

_compare: Callable[[bytes, bytes], bool] = hmac.compare_digest


def _bytes(value: str | bytes) -> bytes:
    return value if isinstance(value, bytes) else value.encode("utf-8", "surrogatepass")


def constant_time_equal(offered: str | bytes, expected: str | bytes) -> bool:
    """Compare two secrets or signatures without leaking where they differ."""
    return _compare(_bytes(offered), _bytes(expected))


def hmac_sha256_hex(secret: str | bytes, data: bytes) -> str:
    return hmac.new(_bytes(secret), data, hashlib.sha256).hexdigest()


def hmac_sha256_base64(secret: str | bytes, data: bytes) -> str:
    return base64.b64encode(hmac.new(_bytes(secret), data, hashlib.sha256).digest()).decode()


def verify_hmac_sha256_hex(
    secret: str | bytes, data: bytes, offered: str | None, *, prefix: str = ""
) -> bool:
    """``offered`` equals ``prefix`` + the hex HMAC-SHA256 of ``data`` (hex in either case)."""
    expected = prefix + hmac_sha256_hex(secret, data)
    given = (offered or "").strip()
    if given[: len(prefix)].lower() == prefix.lower():
        given = prefix + given[len(prefix) :].lower()
    return constant_time_equal(given, expected) and offered is not None


def verify_hmac_sha256_base64(secret: str | bytes, data: bytes, offered: str | None) -> bool:
    """``offered`` equals the base64 HMAC-SHA256 of ``data``."""
    expected = hmac_sha256_base64(secret, data)
    return constant_time_equal((offered or "").strip(), expected) and offered is not None


def sha1_sorted_signature(*parts: str) -> str:
    """Hex SHA1 of the parts sorted as strings and joined (WeCom/WeChat callback signature)."""
    return hashlib.sha1("".join(sorted(parts)).encode("utf-8")).hexdigest()


def verify_sha1_sorted(offered: str | None, *parts: str) -> bool:
    expected = sha1_sorted_signature(*parts)
    return constant_time_equal((offered or "").strip().lower(), expected) and offered is not None


def hmac_sha1_url_signature(
    secret: str | bytes, url: str, params: Mapping[str, str] | Iterable[tuple[str, str]]
) -> str:
    """Base64 HMAC-SHA1 of ``url`` followed by every parameter name and value, sorted (Twilio)."""
    if isinstance(params, Mapping):
        table = cast(Mapping[str, str], params)
        pairs = [(str(name), str(table[name])) for name in table]
    else:
        pairs = [(str(name), str(value)) for name, value in params]
    signed = url + "".join(name + value for name, value in sorted(pairs))
    digest = hmac.new(_bytes(secret), signed.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def verify_url_signature(
    secret: str | bytes,
    url: str,
    params: Mapping[str, str] | Iterable[tuple[str, str]],
    offered: str | None,
) -> bool:
    expected = hmac_sha1_url_signature(secret, url, params)
    return constant_time_equal((offered or "").strip(), expected) and offered is not None


# -- replay -----------------------------------------------------------------------------------


class SeenIds:
    """Ids remembered for ``ttl_s`` (monotonic clock), at most ``max_entries`` (oldest dropped)."""

    def __init__(self, clock: Clock, ttl_s: float, max_entries: int = 10_000) -> None:
        if ttl_s <= 0 or max_entries <= 0:
            raise ValueError("ttl_s and max_entries must be positive")
        self.clock = clock
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self._seen: OrderedDict[str, float] = OrderedDict()

    def _prune(self, now: float) -> None:
        while self._seen:
            key, at = next(iter(self._seen.items()))
            if now - at < self.ttl_s and len(self._seen) <= self.max_entries:
                break
            del self._seen[key]

    def first_time(self, key: str) -> bool:
        """True (and remembered) the first time ``key`` is offered inside the window."""
        now = self.clock.monotonic()
        self._prune(now)
        if key in self._seen:
            return False
        self._seen[key] = now
        self._prune(now)
        return True

    def forget(self, key: str) -> None:
        self._seen.pop(key, None)

    def __contains__(self, key: object) -> bool:
        self._prune(self.clock.monotonic())
        return key in self._seen

    def __len__(self) -> int:
        self._prune(self.clock.monotonic())
        return len(self._seen)


class ReplayWindow:
    """Signed-callback freshness: a timestamp within ±``window_s`` of now and a key (nonce,
    delivery id, signature) not seen inside the window."""

    def __init__(self, clock: Clock, window_s: float = 300.0, *, max_entries: int = 10_000):
        self.clock = clock
        self.window_s = window_s
        self.seen = SeenIds(clock, window_s * 2, max_entries)

    def fresh(self, timestamp: float | str | None, *, unit: Literal["s", "ms"] = "s") -> bool:
        if timestamp is None:
            return False
        try:
            value = float(timestamp)
        except (TypeError, ValueError):
            return False
        if value != value or value in (float("inf"), float("-inf")):
            return False
        seconds = value / 1000.0 if unit == "ms" else value
        return abs(self.clock.now().timestamp() - seconds) <= self.window_s

    def first_time(self, key: str) -> bool:
        return self.seen.first_time(key)

    def accept(
        self, timestamp: float | str | None, key: str, *, unit: Literal["s", "ms"] = "s"
    ) -> bool:
        """Fresh and not a repeat; a stale request never records its key."""
        return self.fresh(timestamp, unit=unit) and self.first_time(key)


# -- outbound results (R06.4) -----------------------------------------------------------------

_CONNECT_FAILURES = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
_MAYBE_DELIVERED = (
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
)
_SNIPPET = 300


def failure_from_exception(exc: BaseException) -> SendResult:
    """A classified failure for an exception raised by an httpx call (see the module notes)."""
    name = type(exc).__name__
    if isinstance(exc, _CONNECT_FAILURES):
        return SendResult.failure(f"connect error: {name}; the request never reached the platform")
    if isinstance(exc, _MAYBE_DELIVERED):
        return SendResult.failure(f"read timeout: {name}; the platform may have the message")
    return SendResult.failure(exc=exc)


def retry_after_seconds(value: str | None, now: datetime) -> float | None:
    """``Retry-After`` as seconds from ``now`` (delta seconds or an HTTP date); None if absent."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - now).total_seconds()
    if seconds != seconds:
        return None
    return max(seconds, 0.0)


def failure_from_status(
    status: int,
    text: str = "",
    headers: Mapping[str, str] | None = None,
    *,
    now: datetime | None = None,
    reading: str | None = None,
) -> SendResult:
    """A classified failure for a vendor's error status. ``text`` is the response body (its
    words feed the classifier); ``reading`` is the adapter's own reading of this status for this
    call (``"chat not found"`` for a 404 on a chat path)."""
    snippet = " ".join(text.split())[:_SNIPPET]
    message = f"HTTP {status}: {snippet}" if snippet else f"HTTP {status}"
    if reading:
        message = f"{message} ({reading})"
    lowered = {k.lower(): v for k, v in (headers or {}).items()}
    wait = retry_after_seconds(lowered.get("retry-after"), now or datetime.now(UTC))
    if status == 429:
        return SendResult.failure(f"{message} (too many requests)", retry_after=wait)
    if status == 403:
        return SendResult.failure(f"{message} (forbidden)")
    if status == 413:
        return SendResult.failure(f"{message} (too long)")
    result = SendResult.failure(message)
    if status == 503:
        if wait is not None:
            return SendResult.failure(f"{message} (retry after)", retry_after=wait)
        return replace(result, kind=ErrorKind.TRANSIENT, retryable=True)
    if status in (502, 504) and result.kind is ErrorKind.UNKNOWN:
        return replace(result, maybe_delivered=True, retryable=False)
    if status == 401 and result.kind is ErrorKind.FORBIDDEN:
        # A wrong token is not a closed chat: never mark the chat unreachable for it.
        return replace(result, kind=ErrorKind.UNKNOWN, chat_level=False, retryable=False)
    return result
