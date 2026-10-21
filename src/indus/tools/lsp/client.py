# pyright: strict
"""One language server over stdio (P24-15), speaking LSP 3.17.

Framing is the base protocol: ``Content-Length: N`` headers, a blank line, N bytes of UTF-8
JSON-RPC 2.0. A header block over 8 KiB or a body over ``STREAM_LIMIT`` (16 MiB) is a protocol
error that ends the connection; stderr lines are read with the same limit and only the last
``STDERR_TAIL`` (30) are kept, for failure messages.

Lifecycle: ``start`` spawns the server in its own session (process group) with the environment the
manager built (``safety.child_env``, R02.22) and sends ``initialize``; no answer within
``INIT_TIMEOUT_S`` (45 s) is a failed start and the process group is killed. ``shutdown`` asks
politely (``shutdown`` request, ``exit`` notification), waits ``SHUTDOWN_GRACE_S`` (1 s) for the
process to leave, then sends SIGTERM to its group, waits another second, then SIGKILL. The steps
taken are recorded in ``stop_steps``.

Documents are synced whole. A document carries a version number (0 when opened, one more for
every change); each stored list of diagnostics remembers which version it was about, and only a
list about the current version counts as an answer, however late an older one arrives. At most
``MAX_OPEN_FILES`` (64) documents stay open; the least recently touched one is closed. Every sync
also sends ``workspace/didChangeWatchedFiles`` (some servers re-scan only then).

Diagnostics arrive pushed (``textDocument/publishDiagnostics``) or pulled
(``textDocument/diagnostic``, when the server advertised ``diagnosticProvider``; a
``MethodNotFound`` answer turns pulling off for this client). ``ContentModified`` answers are
retried ``CONTENT_MODIFIED_RETRIES`` (3) times after 0.5, 1 and 2 s. Every wait runs on the
injected clock.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import unquote, urlparse

from indus.foundation.clock import Clock, SystemClock

__all__ = [
    "CONTENT_MODIFIED",
    "CONTENT_MODIFIED_RETRIES",
    "INIT_TIMEOUT_S",
    "MAX_OPEN_FILES",
    "METHOD_NOT_FOUND",
    "RETRY_BASE_S",
    "SHUTDOWN_GRACE_S",
    "STDERR_TAIL",
    "STREAM_LIMIT",
    "ClientState",
    "LspClient",
    "LspError",
    "LspProtocolError",
    "LspStartError",
    "encode_frame",
    "file_uri",
    "read_frame",
    "uri_path",
    "within",
]

log = logging.getLogger(__name__)

INIT_TIMEOUT_S = 45.0
STREAM_LIMIT = 16 * 1024 * 1024
MAX_HEADER_BYTES = 8 * 1024
CONTENT_MODIFIED = -32801
METHOD_NOT_FOUND = -32601
CONTENT_MODIFIED_RETRIES = 3
RETRY_BASE_S = 0.5
SHUTDOWN_REQUEST_S = 2.0
SHUTDOWN_GRACE_S = 1.0
STDERR_TAIL = 30
STDERR_LINE_CHARS = 1000
MAX_OPEN_FILES = 64
PULL_TIMEOUT_S = 3.0
PUSH_SETTLE_S = 0.15

type ClientState = Literal["stopped", "starting", "running", "failed"]
type Diagnostics = list[dict[str, Any]]

CLIENT_CAPABILITIES: dict[str, Any] = {
    "general": {"positionEncodings": ["utf-16"]},
    "window": {"workDoneProgress": True},
    "workspace": {
        "configuration": True,
        "workspaceFolders": True,
        "didChangeWatchedFiles": {"dynamicRegistration": True},
    },
    "textDocument": {
        "synchronization": {"didSave": True, "dynamicRegistration": False},
        "publishDiagnostics": {"versionSupport": True, "relatedInformation": True},
        "diagnostic": {"dynamicRegistration": True},
    },
}


class LspError(Exception):
    """The server answered a request with an error object."""

    def __init__(self, code: int, message: str, data: object = None) -> None:
        super().__init__(f"{message} (code {code})")
        self.code = code
        self.message = message
        self.data = data


class LspProtocolError(Exception):
    """The connection broke or carried something that is not LSP."""


class LspStartError(LspProtocolError):
    """The server did not start or did not answer ``initialize``."""


# -- framing -------------------------------------------------------------------------------------


def encode_frame(message: Mapping[str, Any]) -> bytes:
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body


async def read_frame(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    """The next message, or None at a clean end of stream."""
    length: int | None = None
    seen = 0
    while True:
        try:
            line = await reader.readuntil(b"\r\n")
        except asyncio.IncompleteReadError as exc:
            if not exc.partial and seen == 0:
                return None
            raise LspProtocolError("the stream ended inside a header") from exc
        except asyncio.LimitOverrunError as exc:
            raise LspProtocolError("header line over the stream limit") from exc
        seen += len(line)
        if seen > MAX_HEADER_BYTES:
            raise LspProtocolError(f"header block over {MAX_HEADER_BYTES} bytes")
        text = line[:-2].decode("ascii", errors="replace")
        if not text:
            break
        name, _, value = text.partition(":")
        if name.strip().lower() == "content-length":
            try:
                length = int(value.strip())
            except ValueError as exc:
                raise LspProtocolError(f"bad Content-Length {value.strip()!r}") from exc
    if length is None:
        raise LspProtocolError("message without Content-Length")
    if length < 0 or length > STREAM_LIMIT:
        raise LspProtocolError(f"message of {length} bytes is over the 16 MiB limit")
    try:
        body = await reader.readexactly(length)
    except asyncio.IncompleteReadError as exc:
        raise LspProtocolError("the stream ended inside a message") from exc
    try:
        decoded: object = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise LspProtocolError("message body is not JSON") from exc
    if not isinstance(decoded, dict):
        raise LspProtocolError("message body is not a JSON object")
    return cast(dict[str, Any], decoded)


def file_uri(path: str) -> str:
    return Path(os.path.abspath(path)).as_uri()


def uri_path(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return uri
    return os.path.normpath(unquote(parsed.path))


async def within[T](clock: Clock, work: Awaitable[T], seconds: float) -> T:
    """``work``'s result, or ``TimeoutError`` once ``seconds`` passed on ``clock`` (``work`` is
    then cancelled)."""
    task: asyncio.Future[T] = asyncio.ensure_future(work)
    timer = asyncio.ensure_future(clock.sleep(max(0.0, seconds)))
    try:
        await asyncio.wait({task, timer}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        task.cancel()
        timer.cancel()
        raise
    finally:
        if not timer.done():
            timer.cancel()
    if task.done():
        return task.result()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
    raise TimeoutError(f"no answer within {seconds:g} s")


def _end_of(text: str) -> dict[str, int]:
    lines = text.split("\n")
    return {"line": len(lines) - 1, "character": len(lines[-1].encode("utf-16-le")) // 2}


def as_synced(text: str) -> str:
    """``text`` as a reader of the file would see it: no BOM, ``\\n`` line ends."""
    return text.removeprefix("﻿").replace("\r\n", "\n").replace("\r", "\n")


# -- documents -----------------------------------------------------------------------------------


@dataclass
class _Doc:
    version: int = 0
    text: str = ""
    pushed: Diagnostics = field(default_factory=lambda: list[dict[str, Any]]())
    push_version: int = -1
    pulled: Diagnostics = field(default_factory=lambda: list[dict[str, Any]]())
    pull_version: int = -1

    def fresh(self, version: int) -> bool:
        return self.push_version >= version or self.pull_version >= version


def diagnostic_key(item: Mapping[str, Any]) -> str:
    """Identity of a diagnostic: severity, code, source, message and range."""
    rng = cast(Mapping[str, Any], item.get("range") or {})
    start = cast(Mapping[str, Any], rng.get("start") or {})
    end = cast(Mapping[str, Any], rng.get("end") or {})
    code = item.get("code")
    return "|".join(
        (
            str(item.get("severity") or 1),
            "" if code is None else str(code),
            str(item.get("source") or ""),
            str(item.get("message") or "").strip(),
            f"{start.get('line', 0)}:{start.get('character', 0)}",
            f"{end.get('line', 0)}:{end.get('character', 0)}",
        )
    )


def _items(value: object) -> Diagnostics:
    if not isinstance(value, list):
        return []
    return [
        dict(cast(Mapping[str, Any], d))
        for d in cast(list[object], value)
        if isinstance(d, Mapping)
    ]


def merged(*lists: Sequence[Mapping[str, Any]]) -> Diagnostics:
    seen: set[str] = set()
    out: Diagnostics = []
    for items in lists:
        for item in items:
            key = diagnostic_key(item)
            if key not in seen:
                seen.add(key)
                out.append(dict(item))
    return out


# -- the client ----------------------------------------------------------------------------------


class LspClient:
    """One server process for one project root (see the module notes)."""

    def __init__(
        self,
        server_id: str,
        root: str,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        init_options: Mapping[str, Any] | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.server_id = server_id
        self.root = root
        self.argv = list(argv)
        self._env = dict(env) if env is not None else None
        self._cwd = cwd or root
        self._init_options = dict(init_options or {})
        self.clock: Clock = clock or SystemClock()
        self._state: ClientState = "stopped"
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._stderr: asyncio.Task[None] | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._docs: OrderedDict[str, _Doc] = OrderedDict()
        self._changed = asyncio.Event()
        self._tail: list[str] = []
        self._stopping = False
        self._exit_code: int | None = None
        self.sync_kind = 1
        self.pull_supported = False
        self.stop_steps: list[str] = []
        self._handlers: dict[str, Callable[[Any], object]] = {
            "workspace/configuration": self._configuration,
            "workspace/workspaceFolders": lambda _p: self._folders(),
            "window/workDoneProgress/create": lambda _p: None,
            "client/registerCapability": lambda _p: None,
            "client/unregisterCapability": lambda _p: None,
            "workspace/diagnostic/refresh": lambda _p: None,
        }

    # -- state ------------------------------------------------------------------------------

    @property
    def state(self) -> ClientState:
        return self._state

    @property
    def running(self) -> bool:
        return self._state == "running" and self._connected()

    def _connected(self) -> bool:
        proc = self._proc
        return (
            proc is not None
            and proc.returncode is None
            and self._reader is not None
            and not self._reader.done()
        )

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def stderr_tail(self) -> list[str]:
        return list(self._tail)

    def failure_details(self) -> str:
        parts: list[str] = []
        code = self._exit_code
        if code is None and self._proc is not None:
            code = self._proc.returncode
        if code is not None:
            parts.append(f"exited on signal {-code}" if code < 0 else f"exited with code {code}")
        if self._tail:
            parts.append("last stderr lines: " + " | ".join(self._tail))
        return "; ".join(parts)

    # -- lifecycle --------------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn and initialize; ``LspStartError`` (process killed) on any failure."""
        if self._state in ("starting", "running"):
            return
        self._state = "starting"
        try:
            await self._spawn()
            result = await within(
                self.clock, self.request("initialize", self._init_params()), INIT_TIMEOUT_S
            )
            self._read_capabilities(result)
            await self.notify("initialized", {})
            if self._init_options:
                await self.notify(
                    "workspace/didChangeConfiguration", {"settings": self._init_options}
                )
            if not self._connected():
                raise LspProtocolError("the server closed the connection while starting")
            self._state = "running"
        except BaseException as exc:
            self._state = "failed"
            await asyncio.shield(self._kill_now())
            if isinstance(exc, TimeoutError):
                reason = f"no answer to initialize within {INIT_TIMEOUT_S:g} s"
            else:
                reason = str(exc) or type(exc).__name__
            details = self.failure_details()
            if isinstance(exc, Exception):
                message = f"{self.server_id} did not start: {reason}"
                raise LspStartError(f"{message} ({details})" if details else message) from exc
            raise

    async def _spawn(self) -> None:
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env,
                cwd=self._cwd,
                start_new_session=True,
                limit=STREAM_LIMIT,
            )
        except (FileNotFoundError, PermissionError) as exc:
            raise LspStartError(f"cannot run {self.argv[0]}: {exc}") from exc
        self._stderr = asyncio.create_task(self._drain_stderr())
        self._reader = asyncio.create_task(self._read_loop())

    def _init_params(self) -> dict[str, Any]:
        return {
            "processId": os.getpid(),
            "clientInfo": {"name": "indus"},
            "rootUri": file_uri(self.root),
            "rootPath": self.root,
            "workspaceFolders": self._folders(),
            "initializationOptions": self._init_options,
            "capabilities": CLIENT_CAPABILITIES,
        }

    def _read_capabilities(self, result: object) -> None:
        caps: Mapping[str, Any] = {}
        if isinstance(result, Mapping):
            found = cast(Mapping[str, Any], result).get("capabilities")
            if isinstance(found, Mapping):
                caps = cast(Mapping[str, Any], found)
        sync = caps.get("textDocumentSync")
        if isinstance(sync, Mapping):
            sync = cast(Mapping[str, Any], sync).get("change")
        self.sync_kind = sync if isinstance(sync, int) and sync in (1, 2) else 1
        self.pull_supported = bool(caps.get("diagnosticProvider"))

    def _folders(self) -> list[dict[str, str]]:
        name = os.path.basename(self.root.rstrip(os.sep)) or self.root
        return [{"uri": file_uri(self.root), "name": name}]

    async def shutdown(self) -> None:
        """Polite stop, then SIGTERM, then SIGKILL (see the module notes). Idempotent."""
        if self._stopping:
            return
        self._stopping = True
        try:
            if self.running:
                with contextlib.suppress(Exception):
                    await within(self.clock, self.request("shutdown", None), SHUTDOWN_REQUEST_S)
                self.stop_steps.append("shutdown")
                with contextlib.suppress(Exception):
                    await self.notify("exit", None)
                self.stop_steps.append("exit")
                await self._wait_exit(SHUTDOWN_GRACE_S)
            if self._alive():
                self.stop_steps.append("term")
                self._signal(signal.SIGTERM)
                await self._wait_exit(SHUTDOWN_GRACE_S)
            if self._alive():
                self.stop_steps.append("kill")
                self._signal(signal.SIGKILL)
                await self._wait_exit(SHUTDOWN_GRACE_S)
        finally:
            await self._finish()
            self._state = "stopped"

    async def _kill_now(self) -> None:
        if self._alive():
            self._signal(signal.SIGKILL)
            await self._wait_exit(SHUTDOWN_GRACE_S)
        await self._finish()

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def _signal(self, sig: int) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return  # never signal a pid that may have been reused
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        except PermissionError:
            with contextlib.suppress(ProcessLookupError):
                proc.send_signal(sig)

    async def _wait_exit(self, seconds: float) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(TimeoutError):
            await within(self.clock, proc.wait(), seconds)

    async def _finish(self) -> None:
        tasks = [t for t in (self._reader, self._stderr) if t is not None]
        current = asyncio.current_task()
        for task in tasks:
            if task is not current and not task.done():
                task.cancel()
        await asyncio.gather(*(t for t in tasks if t is not current), return_exceptions=True)
        proc = self._proc
        if proc is not None:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
            if proc.returncode is not None:
                self._exit_code = proc.returncode
        self._fail_pending("the server connection is closed")

    # -- reading ----------------------------------------------------------------------------

    async def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        while True:
            try:
                line = await proc.stderr.readline()
            except ValueError:
                continue  # one line over the limit is dropped; keep draining
            except (OSError, asyncio.CancelledError):
                return
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                self._tail.append(text[:STDERR_LINE_CHARS])
                del self._tail[:-STDERR_TAIL]

    async def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while (message := await read_frame(proc.stdout)) is not None:
                self._dispatch(message)
        except LspProtocolError as exc:
            log.warning("%s: %s", self.server_id, exc)
        except (OSError, asyncio.CancelledError):
            pass
        finally:
            if not self._stopping and self._state in ("starting", "running"):
                self._state = "failed"
            self._fail_pending("the server connection closed")

    def _fail_pending(self, why: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(LspProtocolError(why))
        self._pending.clear()

    def _dispatch(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if isinstance(method, str):
            if "id" in message:
                asyncio.create_task(self._answer(message["id"], method, message.get("params")))
            elif method == "textDocument/publishDiagnostics":
                self._on_publish(message.get("params"))
            return
        ident = message.get("id")
        if not isinstance(ident, int):
            return
        future = self._pending.get(ident)
        if future is None or future.done():
            return
        error = message.get("error")
        if isinstance(error, Mapping):
            err = cast(Mapping[str, Any], error)
            code = err.get("code")
            future.set_exception(
                LspError(
                    code if isinstance(code, int) else -32000,
                    str(err.get("message") or "error"),
                    err.get("data"),
                )
            )
        else:
            future.set_result(message.get("result"))

    async def _answer(self, ident: object, method: str, params: object) -> None:
        handler = self._handlers.get(method)
        if handler is None:
            reply: dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": ident,
                "error": {"code": METHOD_NOT_FOUND, "message": f"method not found: {method}"},
            }
        else:
            try:
                reply = {"jsonrpc": "2.0", "id": ident, "result": handler(params)}
            except Exception as exc:
                reply = {
                    "jsonrpc": "2.0",
                    "id": ident,
                    "error": {"code": -32603, "message": type(exc).__name__},
                }
        with contextlib.suppress(Exception):
            await self._write(reply)

    def _configuration(self, params: object) -> list[object]:
        items: list[object] = []
        if isinstance(params, Mapping):
            found = cast(Mapping[str, Any], params).get("items")
            if isinstance(found, list):
                items = cast(list[object], found)
        return [self._section(item) for item in items]

    def _section(self, item: object) -> object:
        section = (
            cast(Mapping[str, Any], item).get("section") if isinstance(item, Mapping) else None
        )
        if not isinstance(section, str) or not section:
            return self._init_options or None
        node: object = self._init_options
        for part in section.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return None
            node = cast(Mapping[str, object], node)[part]
        return node

    def _on_publish(self, params: object) -> None:
        if not isinstance(params, Mapping):
            return
        body = cast(Mapping[str, Any], params)
        uri = body.get("uri")
        if not isinstance(uri, str):
            return
        doc = self._docs.get(uri_path(uri))
        if doc is None:
            return
        items = body.get("diagnostics")
        doc.pushed = _items(items)
        version = body.get("version")
        doc.push_version = version if isinstance(version, int) else doc.version
        self._wake()

    def _wake(self) -> None:
        old, self._changed = self._changed, asyncio.Event()
        old.set()

    # -- writing ----------------------------------------------------------------------------

    async def _write(self, message: Mapping[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdin.is_closing():
            raise LspProtocolError("the server connection is closed")
        try:
            proc.stdin.write(encode_frame(message))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            raise LspProtocolError(f"write failed: {exc}") from exc

    async def request(self, method: str, params: object) -> Any:
        """Send a request and wait for its answer (no time limit: callers bound it)."""
        if self._state not in ("starting", "running") or not self._alive():
            raise LspProtocolError(f"cannot send {method}: the server is not running")
        ident = self._next_id
        self._next_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[ident] = future
        try:
            await self._write({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
            return await future
        finally:
            self._pending.pop(ident, None)

    async def request_retrying(self, method: str, params: object, *, timeout: float) -> Any:
        """``request`` bounded by ``timeout`` per attempt, retrying ``ContentModified``."""
        attempt = 0
        while True:
            try:
                return await within(self.clock, self.request(method, params), timeout)
            except LspError as exc:
                if exc.code != CONTENT_MODIFIED or attempt >= CONTENT_MODIFIED_RETRIES:
                    raise
            await self.clock.sleep(RETRY_BASE_S * (2**attempt))
            attempt += 1

    async def notify(self, method: str, params: object) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    # -- documents --------------------------------------------------------------------------

    async def sync(self, path: str, text: str | None = None, *, language: str = "plaintext") -> int:
        """Send ``text`` (default: the file on disk) as the document's content; the version."""
        if not self.running:
            raise LspProtocolError("the server is not running")
        key = os.path.abspath(path)
        if text is None:
            try:
                text = Path(key).read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                raise LspProtocolError(f"cannot read {key}: {exc}") from exc
        text = as_synced(text)
        uri = file_uri(key)
        doc = self._docs.get(key)
        await self.notify(
            "workspace/didChangeWatchedFiles",
            {"changes": [{"uri": uri, "type": 1 if doc is None else 2}]},
        )
        if doc is None:
            self._docs[key] = _Doc(0, text)
            await self.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": uri, "languageId": language, "version": 0, "text": text}},
            )
            await self._evict()
            return 0
        self._docs.move_to_end(key)
        change: dict[str, Any] = {"text": text}
        if self.sync_kind == 2:
            change["range"] = {"start": {"line": 0, "character": 0}, "end": _end_of(doc.text)}
        doc.version += 1  # before the send: a list published meanwhile describes the old text
        doc.text = text
        await self.notify(
            "textDocument/didChange",
            {"textDocument": {"uri": uri, "version": doc.version}, "contentChanges": [change]},
        )
        return doc.version

    async def _evict(self) -> None:
        while len(self._docs) > MAX_OPEN_FILES:
            old, _ = self._docs.popitem(last=False)
            with contextlib.suppress(LspProtocolError):
                await self.notify("textDocument/didClose", {"textDocument": {"uri": file_uri(old)}})

    async def save(self, path: str) -> None:
        if self.running:
            await self.notify(
                "textDocument/didSave", {"textDocument": {"uri": file_uri(os.path.abspath(path))}}
            )

    def open_paths(self) -> list[str]:
        return list(self._docs)

    def synced_text(self, path: str) -> str | None:
        doc = self._docs.get(os.path.abspath(path))
        return doc.text if doc is not None else None

    def current(self, path: str) -> Diagnostics:
        """The stored diagnostics that describe the current text of ``path`` (fresh only)."""
        doc = self._docs.get(os.path.abspath(path))
        if doc is None:
            return []
        push = doc.pushed if doc.push_version >= doc.version else []
        pull = doc.pulled if doc.pull_version >= doc.version else []
        return merged(push, pull)

    async def _pull(self, key: str, budget: float) -> None:
        doc = self._docs.get(key)
        sent = doc.version if doc is not None else -1
        try:
            result = await self.request_retrying(
                "textDocument/diagnostic",
                {"textDocument": {"uri": file_uri(key)}},
                timeout=min(PULL_TIMEOUT_S, max(budget, 0.0)),
            )
        except LspError as exc:
            if exc.code == METHOD_NOT_FOUND:
                self.pull_supported = False
            return
        except (TimeoutError, LspProtocolError):
            return
        doc = self._docs.get(key)
        if doc is None or not isinstance(result, Mapping):
            return
        report = cast(Mapping[str, Any], result)
        if report.get("kind") == "full":
            items: object = report.get("items")
            if not isinstance(items, list):
                return
            doc.pulled = _items(cast(list[object], items))
        elif report.get("kind") != "unchanged":
            return
        doc.pull_version = max(doc.pull_version, sent)
        self._wake()

    async def diagnostics(
        self, path: str, version: int, *, timeout: float, settle: float = PUSH_SETTLE_S
    ) -> Diagnostics | None:
        """Wait up to ``timeout`` for diagnostics describing ``version`` of ``path``; None when
        none came (no verdict, which is not the same as clean). A pushed list is given
        ``settle`` seconds for a follow-up push (some servers publish twice)."""
        key = os.path.abspath(path)
        clock = self.clock
        deadline = clock.monotonic() + timeout
        pull = asyncio.create_task(self._pull(key, timeout)) if self.pull_supported else None
        try:
            while True:
                if not self.running:
                    raise LspProtocolError("the server stopped while diagnostics were awaited")
                doc = self._docs.get(key)
                if doc is not None and doc.pull_version >= version:
                    return self.current(key)
                pushed = doc is not None and doc.push_version >= version
                if pushed and (pull is None or pull.done()):
                    await self._settle(settle, deadline)
                    return self.current(key)
                remaining = deadline - clock.monotonic()
                if remaining <= 0:
                    return None
                changed = self._changed
                waits: set[asyncio.Future[Any]] = {asyncio.ensure_future(changed.wait())}
                if pull is not None and not pull.done():
                    waits.add(pull)
                with contextlib.suppress(TimeoutError):
                    await within(
                        clock,
                        asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED),
                        remaining,
                    )
                for waiter in waits:
                    if waiter is not pull and not waiter.done():
                        waiter.cancel()
        finally:
            if pull is not None and not pull.done():
                pull.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await pull

    async def _settle(self, settle: float, deadline: float) -> None:
        budget = min(settle, deadline - self.clock.monotonic())
        if budget <= 0:
            return
        changed = self._changed
        with contextlib.suppress(TimeoutError):
            await within(self.clock, changed.wait(), budget)
