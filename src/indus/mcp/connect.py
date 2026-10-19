# pyright: strict
"""Opening one MCP session: transports, the handshake and the task that owns them (P13-03).

The SDK transports enter anyio cancel scopes, which must be left by the task that entered them.
So a session is opened *inside* the task that will also close it: ``open_session`` is an async
context manager meant to be entered by that owner task (``Connection`` here, ``ManagedServer`` in
``lifecycle``); callers in other tasks only send requests through the ``LiveSession``.

- stdio: the SDK stdio client spawns the P13-01 launch (filtered env, cwd) in its own process
  group. The server's stderr is pumped line by line, redacted, into ``<home>/logs/mcp-stderr.log``
  (rotated). After the SDK's own shutdown, processes still carrying this run's tag get SIGTERM and,
  after a 3 s grace, SIGKILL, so grandchildren never outlive a closed server (Linux sweep).
- Streamable HTTP and legacy SSE: the SDK clients over an ``httpx2`` client built here: headers
  (identity header included), the proxy named by the scope's env (``NO_PROXY`` honoured, loopback
  always direct, ``socks://`` read as ``socks5://``), the client certificate; the process env is
  not consulted (``trust_env`` off).
- URL gate for remote servers: http(s) with a host, the website blocklist, no cloud metadata name
  or address (checked again on the resolved addresses). Private and loopback addresses and
  credentials in the URL are allowed: the URL is the operator's own configuration.
- Handshake: ``initialize``; a server that refuses it as unknown or unsupported gets one
  ``server/discover``. Spawn plus handshake are bounded by ``connect_timeout`` on the clock.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import re
import secrets
import signal
import sys
import threading
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import IO, Any, Literal, Protocol, Self, cast
from urllib.parse import urlsplit

import httpx2
import mcp_types as types
from mcp import ClientSession, StdioServerParameters
from mcp.client.session import IncomingMessage, MessageHandlerFnT
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.shared.message import SessionMessage

from indus import __version__
from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock, SystemClock
from indus.foundation.log import logs_dir
from indus.foundation.redact import redact_text
from indus.foundation.scope import Scope
from indus.mcp.config import (
    McpClientCertMissing,
    McpConfigInvalid,
    McpError,
    McpServer,
    McpUnresolvedRef,
    RemoteEndpoint,
    StdioLaunch,
    remote_endpoint,
    stdio_launch,
)
from indus.safety.url_policy import (
    METADATA_HOSTS,
    Resolver,
    blocklist_hit,
    classify_address,
    system_resolver,
)

__all__ = [
    "LIST_MAX_PAGES",
    "SSE_READ_TIMEOUT_S",
    "STOP_GRACE_S",
    "Connection",
    "ConnectOptions",
    "HttpClientFactory",
    "LiveSession",
    "McpConnectFailed",
    "McpConnectTimeout",
    "McpUrlRefused",
    "NotificationHandler",
    "StderrLog",
    "check_endpoint",
    "clock_timeout",
    "first_cause",
    "is_permanent",
    "open_session",
    "proxy_for_url",
]

log = logging.getLogger(__name__)

LIST_MAX_PAGES = 50
SSE_READ_TIMEOUT_S = 300.0
STOP_GRACE_S = 3.0
RUN_TAG_ENV = "INDUS_MCP_RUN_ID"
STDERR_LOG_NAME = "mcp-stderr.log"
STDERR_MAX_BYTES = 5 * 1024 * 1024
STDERR_BACKUPS = 3
STDERR_TAIL_CHARS = 4000

# Process waits are about real processes, whatever clock the caller injected.
_REAL_CLOCK = SystemClock()


# -- errors -------------------------------------------------------------------------------------


class McpUrlRefused(McpError):
    default_code = "mcp.url_refused"


class McpConnectTimeout(McpError, TimeoutError):
    default_code = "mcp.connect_timeout"


class McpConnectFailed(McpError):
    """Starting or reaching the server failed; ``cause`` is the underlying error and
    ``permanent`` says whether retrying the same config can help."""

    default_code = "mcp.connect_failed"

    def __init__(
        self,
        message: str,
        *,
        server: str = "",
        cause: BaseException | None = None,
        permanent: bool = False,
        stderr_tail: str = "",
    ) -> None:
        super().__init__(message, server=server)
        self.cause = cause
        self.permanent = permanent
        self.stderr_tail = stderr_tail


def first_cause(exc: BaseException) -> BaseException:
    """The first real error inside (nested) exception groups; ``exc`` itself otherwise."""
    current: BaseException = exc
    while isinstance(current, BaseExceptionGroup):
        members = cast(BaseExceptionGroup[BaseException], current).exceptions
        real = [m for m in members if not isinstance(m, asyncio.CancelledError)]
        current = (real or list(members))[0]
    return current


_PERMANENT_TYPES: tuple[type[BaseException], ...] = (
    McpConfigInvalid,
    McpUnresolvedRef,
    McpClientCertMissing,
    McpUrlRefused,
    FileNotFoundError,
    PermissionError,
    NotADirectoryError,
)


def is_permanent(exc: BaseException) -> bool:
    """True when retrying with the same config cannot help (bad command, missing file, refused
    URL, rejected credentials)."""
    root = first_cause(exc)
    if isinstance(root, McpConnectFailed):
        return root.permanent
    if isinstance(root, _PERMANENT_TYPES):
        return True
    if isinstance(root, httpx2.HTTPStatusError):
        return root.response.status_code in (401, 403)
    return False


# -- timing -------------------------------------------------------------------------------------


@asynccontextmanager
async def clock_timeout(clock: Clock, seconds: float) -> AsyncGenerator[None]:
    """``asyncio.timeout`` driven by ``clock``: the body is cancelled when ``clock`` has moved
    ``seconds`` on, and ``TimeoutError`` comes out. Cancels the task that entered it."""
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(None) as budget:

        async def expire() -> None:
            await clock.sleep(seconds)
            budget.reschedule(loop.time())

        timer = asyncio.ensure_future(expire())
        try:
            yield
        finally:
            timer.cancel()


# -- stderr of stdio servers --------------------------------------------------------------------


class StderrLog:
    """``<home>/logs/mcp-stderr.log`` shared by the stdio servers of one home.

    Each server's stderr reaches it through a pipe and a pump thread: lines are stamped with the
    time and the server name, redacted, and the file rotates at 5 MiB keeping 3 old files. The
    last few thousand characters of a run stay readable so a server that dies at startup can say
    why.
    """

    _shared: dict[str, StderrLog] = {}
    _shared_lock = threading.Lock()

    def __init__(
        self, path: Path, *, max_bytes: int = STDERR_MAX_BYTES, backups: int = STDERR_BACKUPS
    ) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.backups = backups
        self._lock = threading.Lock()

    @classmethod
    def for_home(cls, home: Path) -> StderrLog:
        path = logs_dir(home) / STDERR_LOG_NAME
        with cls._shared_lock:
            found = cls._shared.get(str(path))
            if found is None:
                found = cls._shared[str(path)] = cls(path)
            return found

    def _rotate(self) -> None:
        for index in range(self.backups - 1, 0, -1):
            older = self.path.with_name(f"{self.path.name}.{index}")
            if older.exists():
                older.replace(self.path.with_name(f"{self.path.name}.{index + 1}"))
        if self.path.exists():
            self.path.replace(self.path.with_name(f"{self.path.name}.1"))

    def write(self, server: str, lines: Sequence[str]) -> None:
        if not lines:
            return
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        text = "".join(f"{stamp} [{server}] {redact_text(line)}\n" for line in lines)
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size + len(text) > self.max_bytes:
                    self._rotate()
                with self.path.open("a", encoding="utf-8", errors="replace") as out:
                    out.write(text)
            except OSError as exc:  # a full disk must not break the server
                log.debug("could not write the MCP stderr log: %s", exc)

    def tap(self, server: str) -> StderrTap:
        self.write(server, [f"--- starting MCP server {server} ---"])
        return StderrTap(self, server)


class StderrTap:
    """One server run's stderr: ``sink`` goes to the child; a daemon thread copies it to the log."""

    def __init__(self, target: StderrLog, server: str) -> None:
        read_fd, write_fd = os.pipe()
        self.sink: IO[bytes] = os.fdopen(write_fd, "wb", buffering=0)
        self._target = target
        self._server = server
        self._tail = ""
        self._thread = threading.Thread(
            target=self._pump, args=(read_fd,), name=f"mcp-stderr-{server}", daemon=True
        )
        self._thread.start()

    def _pump(self, read_fd: int) -> None:
        """Copy whole lines to the log until every writer has closed the pipe."""
        held = b""
        while True:
            try:
                block = os.read(read_fd, 65536)
            except OSError:
                block = b""
            if not block:
                break
            self._tail = (self._tail + block.decode("utf-8", "replace"))[-STDERR_TAIL_CHARS:]
            held += block
            complete, sep, held = held.rpartition(b"\n")
            if sep:
                text = complete.decode("utf-8", "replace")
                self._target.write(self._server, text.split("\n"))
        with contextlib.suppress(OSError):
            os.close(read_fd)
        if held:
            self._target.write(self._server, [held.decode("utf-8", "replace")])

    def release(self) -> None:
        """Close our copy of the write end (the child holds its own)."""
        with contextlib.suppress(OSError):
            self.sink.close()

    def tail(self) -> str:
        return redact_text(self._tail.strip())

    def close(self, wait: float = 0.5) -> str:
        self.release()
        self._thread.join(wait)
        return self.tail()


# -- process sweep (stdio) ----------------------------------------------------------------------


def _tagged_pids(token: str) -> list[int]:
    """Processes of this user whose environment carries ``RUN_TAG_ENV=token`` (Linux /proc)."""
    proc = Path("/proc")
    if not proc.is_dir():
        return []
    needle = f"{RUN_TAG_ENV}={token}".encode()
    found: list[int] = []
    own = os.getpid()
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == own:
            continue
        try:
            data = (entry / "environ").read_bytes()
        except OSError:
            continue
        if needle in data.split(b"\0"):
            found.append(int(entry.name))
    return found


def _signal_all(pids: Sequence[int], sig: int) -> None:
    for pid in pids:
        with contextlib.suppress(OSError):
            os.kill(pid, sig)


async def _sweep(server: str, token: str) -> None:
    """SIGTERM what is left of a closed stdio server, then SIGKILL after the grace."""
    if sys.platform == "win32":
        return
    try:
        left = await run_blocking(_tagged_pids, token, pool="io", label="mcp sweep")
    except Exception as exc:
        log.debug("MCP server %r: process sweep skipped: %s", server, exc)
        return
    if not left:
        return
    log.info("MCP server %r: %d process(es) outlived the server; stopping them", server, len(left))
    _signal_all(left, signal.SIGTERM)
    waited = 0.0
    while waited < STOP_GRACE_S:
        await _REAL_CLOCK.sleep(0.1)
        waited += 0.1
        left = await run_blocking(_tagged_pids, token, pool="io", label="mcp sweep")
        if not left:
            return
    log.warning("MCP server %r: killing %d process(es) after %.0f s", server, len(left), waited)
    _signal_all(left, signal.SIGKILL)


# -- remote endpoints ---------------------------------------------------------------------------


def _loopback(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _bypass_entry_matches(entry: str, host: str, port: int | None) -> bool:
    text = entry.strip().lower()
    if not text:
        return False
    if text == "*":
        return True
    try:
        network = ipaddress.ip_network(text.strip("[]"), strict=False)
    except ValueError:
        network = None
    if network is not None:
        try:
            return ipaddress.ip_address(host.strip("[]")) in network
        except ValueError:
            return False
    want_port: int | None = None
    if re.fullmatch(r"[^:\[\]]+:\d+", text):
        text, _, digits = text.rpartition(":")
        want_port = int(digits)
    if want_port is not None and port != want_port:
        return False
    text = text.removeprefix("*").removeprefix(".")
    return host == text or host.endswith("." + text)


def proxy_for_url(url: str, env: Mapping[str, str]) -> str | None:
    """The proxy the env names for ``url`` after ``NO_PROXY``; ``None`` means connect directly."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if not host or _loopback(host):
        return None
    bypass = env.get("NO_PROXY") or env.get("no_proxy") or ""
    if any(_bypass_entry_matches(e, host, port) for e in re.split(r"[,\s]+", bypass)):
        return None
    scheme = parts.scheme.lower()
    names = ("HTTPS_PROXY", "https_proxy") if scheme == "https" else ("HTTP_PROXY", "http_proxy")
    raw = next(
        (env[n].strip() for n in (*names, "ALL_PROXY", "all_proxy") if env.get(n, "").strip()), ""
    )
    if not raw:
        return None
    if raw.lower().startswith("socks://"):
        raw = "socks5://" + raw[len("socks://") :]
    if "://" not in raw:
        raw = "http://" + raw
    return raw


async def check_endpoint(
    name: str, url: str, scope: Scope, *, resolver: Resolver | None = None
) -> None:
    """Refuse a remote server URL that is not http(s), is on the website blocklist or reaches a
    cloud metadata service (by name, literal address or any resolved address)."""
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - a bad port raises here
    except ValueError as exc:
        raise McpUrlRefused(
            f"MCP server {name!r}: the url does not parse ({exc})", server=name
        ) from exc
    if parts.scheme.lower() not in ("http", "https"):
        raise McpUrlRefused(
            f"MCP server {name!r}: the url must be http or https (got {parts.scheme or 'none'})",
            server=name,
        )
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise McpUrlRefused(f"MCP server {name!r}: the url has no host", server=name)
    hit = blocklist_hit(url, scope)
    if hit is not None:
        raise McpUrlRefused(f"MCP server {name!r}: {hit.message}", server=name)
    if host in METADATA_HOSTS:
        raise McpUrlRefused(f"MCP server {name!r}: {host} is a cloud metadata host", server=name)
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        answers = [str(literal)]
    else:
        lookup = resolver or system_resolver
        try:
            answers = list(await run_blocking(lookup, host, parts.port, pool="io", label="mcp dns"))
        except OSError:
            return  # the connect reports it; a proxy may still resolve the name
    for answer in answers:
        try:
            address = ipaddress.ip_address(answer.split("%", 1)[0])
        except ValueError:
            continue
        if classify_address(address, allow_private=True) == "metadata":
            raise McpUrlRefused(
                f"MCP server {name!r}: {host} reaches a cloud metadata address ({answer})",
                server=name,
            )


class HttpClientFactory(Protocol):
    """Builds the ``httpx2`` client a remote transport uses (tests pass an in-process one)."""

    def __call__(
        self,
        endpoint: RemoteEndpoint,
        *,
        proxy: str | None,
        timeout: httpx2.Timeout,
        auth: httpx2.Auth | None,
    ) -> httpx2.AsyncClient: ...


def default_http_client(
    endpoint: RemoteEndpoint,
    *,
    proxy: str | None,
    timeout: httpx2.Timeout,
    auth: httpx2.Auth | None,
) -> httpx2.AsyncClient:
    kwargs: dict[str, Any] = {
        "headers": dict(endpoint.headers),
        "timeout": timeout,
        "trust_env": False,
    }
    if proxy is not None:
        kwargs["proxy"] = proxy
    if endpoint.cert is not None:
        kwargs["cert"] = endpoint.cert
    if auth is not None:
        kwargs["auth"] = auth
    return httpx2.AsyncClient(**kwargs)


# -- the live session ---------------------------------------------------------------------------

NotificationHandler = Callable[[str, types.ServerNotification], Awaitable[None]]


def _note_raw_hints(item: object, hints: dict[str, object]) -> None:
    """Keep each listed tool's ``annotations.readOnlyHint`` exactly as the server sent it.

    The SDK's models coerce ``"yes"`` or ``1`` to ``True``; the trust gate (P13-07) must see the
    wire value, so every response whose result carries a ``tools`` list is read here first.
    """
    message = getattr(item, "message", None)
    if not isinstance(message, types.JSONRPCResponse):
        return
    listed = message.result.get("tools")
    if not isinstance(listed, list):
        return
    for raw in cast(list[object], listed):
        if not isinstance(raw, dict):
            continue
        entry = cast(dict[str, object], raw)
        name = entry.get("name")
        if not isinstance(name, str):
            continue
        notes = entry.get("annotations")
        value = (
            cast(dict[str, object], notes).get("readOnlyHint") if isinstance(notes, dict) else None
        )
        hints[name] = value


class _WatchedReader:
    """The transport's read stream, with ``ended`` set when the server side goes away and the raw
    ``readOnlyHint`` of every listed tool noted in ``hints``."""

    def __init__(
        self, inner: Any, ended: asyncio.Event, hints: dict[str, object] | None = None
    ) -> None:
        self._inner = inner
        self._ended = ended
        self._hints = hints

    @property
    def last_context(self) -> Any:
        return getattr(self._inner, "last_context", None)

    def _seen(self, item: SessionMessage | Exception) -> SessionMessage | Exception:
        if self._hints is not None:
            try:
                _note_raw_hints(item, self._hints)
            except Exception:  # never let bookkeeping break the stream
                log.debug("could not read raw tool hints", exc_info=True)
        return item

    async def receive(self) -> SessionMessage | Exception:
        try:
            return self._seen(await self._inner.receive())
        except BaseException as exc:
            if not isinstance(exc, asyncio.CancelledError):
                self._ended.set()
            raise

    async def aclose(self) -> None:
        await self._inner.aclose()

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> SessionMessage | Exception:
        try:
            return self._seen(await self._inner.__anext__())
        except BaseException as exc:
            if not isinstance(exc, asyncio.CancelledError):
                self._ended.set()
            raise

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        await self._inner.aclose()
        return None


class LiveSession:
    """An open session. Requests may come from any task; the transport stays with its owner."""

    def __init__(
        self,
        name: str,
        kind: Literal["stdio", "http", "sse"],
        session: ClientSession,
        dropped: asyncio.Event | None = None,
        raw_hints: dict[str, object] | None = None,
    ) -> None:
        self.name = name
        self.kind = kind
        self.raw = session
        self.dropped = dropped if dropped is not None else asyncio.Event()
        # tool name -> ``readOnlyHint`` as sent on the wire (absent key: not seen).
        self.raw_hints: dict[str, object] = raw_hints if raw_hints is not None else {}

    @property
    def capabilities(self) -> types.ServerCapabilities | None:
        return self.raw.server_capabilities

    @property
    def server_info(self) -> types.Implementation | None:
        return self.raw.server_info

    @property
    def protocol_version(self) -> str | None:
        return self.raw.protocol_version

    @property
    def instructions(self) -> str | None:
        return self.raw.instructions

    @property
    def offers_tools(self) -> bool:
        caps = self.capabilities
        return caps is None or caps.tools is not None

    async def list_tools(self, *, max_pages: int = LIST_MAX_PAGES) -> list[types.Tool]:
        """Every tool, following cursors for at most ``max_pages`` pages."""
        tools: list[types.Tool] = []
        cursor: str | None = None
        for _ in range(max_pages):
            params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
            page = await self.raw.list_tools(params=params)
            tools.extend(page.tools)
            cursor = page.next_cursor
            if not cursor:
                return tools
        log.warning(
            "MCP server %r: tools/list went past %d pages; kept the first %d tools",
            self.name,
            max_pages,
            len(tools),
        )
        return tools

    async def call_tool(
        self, tool: str, arguments: Mapping[str, Any] | None = None
    ) -> types.CallToolResult:
        """One ``tools/call`` with the server's own tool name (the caller bounds the time)."""
        return await self.raw.call_tool(tool, dict(arguments) if arguments is not None else None)

    async def ping(self) -> None:
        await self.raw.send_ping()


@dataclass(frozen=True)
class ConnectOptions:
    """Everything about a connect that is not the server's config."""

    workspace: Path | None = None
    http_client: HttpClientFactory = default_http_client
    resolver: Resolver | None = None
    on_notification: NotificationHandler | None = None
    stderr_log: StderrLog | None = None
    auth: httpx2.Auth | None = None
    session_kwargs: Mapping[str, Any] = field(default_factory=dict[str, Any])


async def _handshake(session: ClientSession) -> None:
    try:
        await session.initialize()
    except MCPError as exc:
        if exc.code not in (types.METHOD_NOT_FOUND, types.UNSUPPORTED_PROTOCOL_VERSION):
            raise
        log.debug("initialize refused (%s); trying server/discover", exc.code)
        await session.discover()


def _message_handler(name: str, handler: NotificationHandler | None) -> MessageHandlerFnT:
    async def deliver(message: IncomingMessage) -> None:
        if isinstance(message, Exception):
            log.debug("MCP server %r: transport reported %s", name, type(message).__name__)
            return
        if handler is not None:
            try:
                await handler(name, message)
            except Exception:
                log.exception("MCP server %r: notification handler failed", name)

    return deliver


async def _enter_stdio(
    stack: AsyncExitStack, launch: StdioLaunch, tap: StderrTap, token: str
) -> tuple[Any, Any]:
    params = StdioServerParameters(
        command=launch.command,
        args=list(launch.args),
        env={**launch.env, RUN_TAG_ENV: token},
        cwd=launch.cwd,
        encoding_error_handler="replace",
    )
    streams = await stack.enter_async_context(stdio_client(params, errlog=cast(Any, tap.sink)))
    tap.release()
    return streams


async def _enter_remote(
    stack: AsyncExitStack,
    endpoint: RemoteEndpoint,
    scope: Scope,
    server: McpServer,
    options: ConnectOptions,
) -> tuple[Any, Any]:
    proxy = proxy_for_url(endpoint.url, scope.env)
    timeout = httpx2.Timeout(server.connect_timeout, read=SSE_READ_TIMEOUT_S)
    if endpoint.transport == "sse":

        def factory(
            headers: dict[str, str] | None = None,
            timeout: httpx2.Timeout | None = None,
            auth: httpx2.Auth | None = None,
        ) -> httpx2.AsyncClient:
            merged = RemoteEndpoint(
                endpoint.url, {**endpoint.headers, **(headers or {})}, "sse", endpoint.cert
            )
            return options.http_client(
                merged,
                proxy=proxy,
                timeout=timeout or httpx2.Timeout(server.connect_timeout, read=SSE_READ_TIMEOUT_S),
                auth=auth or options.auth,
            )

        return await stack.enter_async_context(
            sse_client(
                endpoint.url,
                timeout=server.connect_timeout,
                sse_read_timeout=SSE_READ_TIMEOUT_S,
                httpx_client_factory=factory,
            )
        )
    client = options.http_client(endpoint, proxy=proxy, timeout=timeout, auth=options.auth)
    await stack.enter_async_context(client)
    return await stack.enter_async_context(streamable_http_client(endpoint.url, http_client=client))


@asynccontextmanager
async def open_session(
    name: str,
    server: McpServer,
    scope: Scope,
    *,
    clock: Clock,
    options: ConnectOptions | None = None,
) -> AsyncGenerator[LiveSession]:
    """Open, handshake and yield a session; leave the transport on exit. Enter and exit in the
    same task. Spawn plus handshake are bounded by ``server.connect_timeout`` on ``clock``."""
    opts = options or ConnectOptions()
    kind = server.kind
    launch: StdioLaunch | None = None
    endpoint: RemoteEndpoint | None = None
    if kind == "stdio":
        launch = stdio_launch(name, server, scope, workspace=opts.workspace)
    else:
        endpoint = remote_endpoint(name, server, scope, workspace=opts.workspace)
        await check_endpoint(name, endpoint.url, scope, resolver=opts.resolver)

    tap: StderrTap | None = None
    token = secrets.token_hex(12)
    dropped = asyncio.Event()
    raw_hints: dict[str, object] = {}
    opened = False
    try:
        async with AsyncExitStack() as stack:
            if launch is not None:
                sink = opts.stderr_log or StderrLog.for_home(scope.home)
                tap = sink.tap(name)
                # Unwound last to first: the transport closes, leftovers are swept, then the pump.
                stack.callback(tap.close)
                stack.push_async_callback(_sweep, name, token)
            async with clock_timeout(clock, server.connect_timeout):
                if launch is not None and tap is not None:
                    read, write = await _enter_stdio(stack, launch, tap, token)
                else:
                    assert endpoint is not None
                    read, write = await _enter_remote(stack, endpoint, scope, server, opts)
                session = ClientSession(
                    _WatchedReader(read, dropped, raw_hints),
                    write,
                    message_handler=_message_handler(name, opts.on_notification),
                    client_info=types.Implementation(name="indus", version=__version__),
                    **dict(opts.session_kwargs),
                )
                await stack.enter_async_context(session)
                await _handshake(session)
            opened = True
            try:
                yield LiveSession(name, kind, session, dropped, raw_hints)
            finally:
                dropped.set()
    except BaseException as exc:
        root = first_cause(exc)
        if opened or isinstance(root, asyncio.CancelledError | GeneratorExit):
            if root is exc:
                raise
            raise root from None
        raise _opening_error(name, server, root, tap.tail() if tap is not None else "") from None


def _opening_error(name: str, server: McpServer, root: BaseException, tail: str) -> BaseException:
    if isinstance(root, McpError) or not isinstance(root, Exception):
        return root
    if isinstance(root, TimeoutError):
        error = McpConnectTimeout(
            f"MCP server {name!r} did not finish connecting within "
            f"{server.connect_timeout:g} s" + _tail_note(tail),
            server=name,
        )
    else:
        text = f"{type(root).__name__}: {root}" if str(root) else type(root).__name__
        error = McpConnectFailed(
            f"MCP server {name!r} could not connect ({text})" + _tail_note(tail),
            server=name,
            cause=root,
            permanent=is_permanent(root),
            stderr_tail=tail,
        )
    error.__cause__ = root
    return error


def _tail_note(tail: str) -> str:
    return f"; its stderr ends: {tail[-500:]}" if tail else ""


# -- one connection, one task -------------------------------------------------------------------


class Connection:
    """A single session owned by its own task: ``start`` opens it, ``close`` leaves it.

    No reconnects (``lifecycle.ManagedServer`` adds those); used for probes such as
    ``indus mcp test`` and by the contract tests.
    """

    def __init__(
        self,
        name: str,
        server: McpServer,
        scope: Scope,
        *,
        clock: Clock,
        options: ConnectOptions | None = None,
    ) -> None:
        self.name = name
        self.server = server
        self.scope = scope
        self.clock = clock
        self.options = options or ConnectOptions()
        self._task: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[LiveSession] | None = None
        self._stop = asyncio.Event()
        self.session: LiveSession | None = None

    async def _own(self) -> None:
        ready = self._ready
        assert ready is not None
        try:
            async with open_session(
                self.name, self.server, self.scope, clock=self.clock, options=self.options
            ) as live:
                self.session = live
                ready.set_result(live)
                stop = asyncio.ensure_future(self._stop.wait())
                gone = asyncio.ensure_future(live.dropped.wait())
                try:
                    await asyncio.wait({stop, gone}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    stop.cancel()
                    gone.cancel()
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            if not isinstance(exc, Exception):
                raise
        finally:
            self.session = None

    async def start(self) -> LiveSession:
        if self._task is not None:
            raise RuntimeError("a Connection starts once")
        self._ready = asyncio.get_running_loop().create_future()
        self._task = asyncio.ensure_future(self._own())
        try:
            return await asyncio.shield(self._ready)
        except asyncio.CancelledError:
            self._task.cancel()
            raise

    async def close(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def __aenter__(self) -> LiveSession:
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
