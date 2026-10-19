# pyright: strict
"""Tool calls from a running script: the request pipeline and the socket server (P10-08).

``RpcPipeline.handle`` answers one request in a fixed order: token (constant-time; an empty
server token refuses everything) → allow-list → call budget (only dispatched calls count, a
refusal is free) → argument shape and the foreground-only rule for ``terminal`` → dispatch
through the normal tool dispatcher (``ToolInvoker``; approvals and hooks apply as for any call)
→ log. Answers are ``{"result": <tool text>}`` or ``{"refused": <why>}``.

``SocketRpcServer`` listens on a Unix socket (owner-only) inside the run's private directory, or
on loopback TCP with a random port on Windows. It runs on its own threads and hands each request
to the event loop of the call that is running (``attach``), so a session kernel that outlives one
turn keeps working when the next call comes from another loop. Between calls nothing is attached
and every request is refused.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import select
import shutil
import socket
import tempfile
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from indus.foundation.clock import Clock

log = logging.getLogger(__name__)

type ToolInvoker = Callable[[str, dict[str, Any]], Awaitable[str]]
type Answer = dict[str, str]

# ``terminal`` arguments a script may not use: a script's commands run in the foreground only.
FOREGROUND_ONLY: frozenset[str] = frozenset(
    {
        "background",
        "pty",
        "notify",
        "notify_on_complete",
        "watch_patterns",
        "heartbeat",
        "persist_on_release",
    }
)
MAX_LINE_BYTES = 64 * 1024 * 1024
ERROR_PREVIEW_CHARS = 300
SOCKET_NAME = "rpc.sock"
MAX_SOCKET_PATH = 100  # AF_UNIX allows 104 (macOS) to 108 (Linux) bytes
ACCEPT_POLL_S = 0.1
ANSWER_POLL_S = 0.2

NOT_RUNNING = (
    "No execute_code call is running for this script, so the tool call was refused (the call "
    "that started it has ended)."
)


def refused(why: str) -> Answer:
    return {"refused": why}


def token_ok(sent: object, expected: str) -> bool:
    if not expected:
        return False
    given = sent if isinstance(sent, str) else ""
    return hmac.compare_digest(given.encode("utf-8", "replace"), expected.encode("utf-8"))


def error_of(text: str) -> str | None:
    """The ``error`` text of a JSON-object tool result, else None."""
    if '"error"' not in text:
        return None
    try:
        body = json.loads(text)
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    error = cast(dict[str, Any], body).get("error")
    return str(error)[:ERROR_PREVIEW_CHARS] if error else None


@dataclass(frozen=True)
class CallRecord:
    tool: str
    seconds: float
    error: str | None = None


class RpcPipeline:
    """The checks and the dispatch for one run (or one kernel cell)."""

    def __init__(
        self,
        *,
        token: str,
        allowed: frozenset[str],
        max_calls: int,
        invoke: ToolInvoker,
        clock: Clock,
    ) -> None:
        self.token = token
        self.allowed = allowed
        self.max_calls = max_calls
        self.invoke = invoke
        self.clock = clock
        self.calls_made = 0
        self.records: list[CallRecord] = []

    async def handle(self, request: object) -> Answer:
        if not isinstance(request, Mapping):
            return refused("The request is not a JSON object.")
        body = cast(Mapping[str, Any], request)
        if not token_ok(body.get("token"), self.token):
            log.warning("execute_code: a tool request with a wrong token was refused")
            return refused("Unauthorised tool request.")
        tool = body.get("tool")
        if not isinstance(tool, str) or tool not in self.allowed:
            offered = ", ".join(sorted(self.allowed)) or "none"
            return refused(f"'{tool}' cannot be called from execute_code here. Offered: {offered}.")
        if self.calls_made >= self.max_calls:
            return refused(
                f"This execute_code call already made its {self.max_calls} tool calls, the most "
                "one call may make. Split the work over several execute_code calls."
            )
        raw_args = body.get("args", {})
        if raw_args is None:
            raw_args = {}
        if not isinstance(raw_args, Mapping):
            return refused(f"The arguments for {tool} must be a JSON object.")
        args = dict(cast(Mapping[str, Any], raw_args))
        if tool == "terminal":
            used = sorted(k for k in FOREGROUND_ONLY if args.get(k))
            if used:
                return refused(
                    "terminal inside execute_code runs commands in the foreground only; "
                    f"{', '.join(used)} cannot be used here. Start background work with a direct "
                    "terminal call instead."
                )
            for name in FOREGROUND_ONLY:
                args.pop(name, None)
        started = self.clock.monotonic()
        self.calls_made += 1
        try:
            text = await self.invoke(tool, args)
        except Exception as exc:
            log.error("execute_code: in-script %s call failed: %s", tool, exc, exc_info=exc)
            text = json.dumps({"error": f"{tool} failed: {type(exc).__name__}: {exc}"})
        seconds = round(max(0.0, self.clock.monotonic() - started), 3)
        self.records.append(CallRecord(tool, seconds, error_of(text)))
        return {"result": text}

    def tool_errors(self, limit: int) -> list[dict[str, str]]:
        return [{"tool": r.tool, "error": r.error} for r in self.records if r.error][:limit]


def decode_request(line: bytes) -> object:
    try:
        return json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


# -- the socket server -----------------------------------------------------------------------------


class SocketRpcServer:
    """See the module notes. ``endpoint`` is what ``INDUS_RPC_SOCKET`` carries."""

    def __init__(self, folder: Path, *, use_tcp: bool | None = None) -> None:
        self._tcp = os.name == "nt" if use_tcp is None else use_tcp
        self._lock = threading.Lock()
        self._pipeline: RpcPipeline | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopped = threading.Event()
        self._conns: set[socket.socket] = set()
        self._short_dir: str | None = None
        self.socket_path: str | None = None
        if self._tcp:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.bind(("127.0.0.1", 0))
            host, port = cast(tuple[str, int], server.getsockname()[:2])
            self.endpoint = f"tcp://{host}:{port}"
        else:
            path = str(folder / SOCKET_NAME)
            if len(path.encode()) > MAX_SOCKET_PATH:
                self._short_dir = tempfile.mkdtemp(prefix="irpc")
                path = os.path.join(self._short_dir, SOCKET_NAME)
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            old = os.umask(0o177)
            try:
                server.bind(path)
            finally:
                os.umask(old)
            os.chmod(path, 0o600)
            self.socket_path = path
            self.endpoint = path
        server.listen(8)
        server.setblocking(False)
        self._server = server
        self._thread = threading.Thread(target=self._accept_loop, name="code-rpc", daemon=True)
        self._thread.start()

    # -- attachment -------------------------------------------------------------------------------

    def attach(self, pipeline: RpcPipeline, loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            self._pipeline, self._loop = pipeline, loop

    def detach(self) -> None:
        with self._lock:
            self._pipeline, self._loop = None, None

    # -- serving ----------------------------------------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._stopped.is_set():
            try:
                ready, _, _ = select.select([self._server], [], [], ACCEPT_POLL_S)
            except (OSError, ValueError):
                return
            if not ready:
                continue
            try:
                conn, _ = self._server.accept()
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                return
            conn.setblocking(True)
            with self._lock:
                if self._stopped.is_set():
                    conn.close()
                    return
                self._conns.add(conn)
            threading.Thread(
                target=self._serve, args=(conn,), name="code-rpc-conn", daemon=True
            ).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            reader = conn.makefile("rb")
            while not self._stopped.is_set():
                line = reader.readline(MAX_LINE_BYTES + 1)
                if not line:
                    return
                if not line.endswith(b"\n"):
                    answer = refused("The request is too large.")
                    conn.sendall((json.dumps(answer) + "\n").encode())
                    return
                if not line.strip():
                    continue
                answer = self._answer(decode_request(line))
                conn.sendall((json.dumps(answer, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError:
            return
        finally:
            with self._lock:
                self._conns.discard(conn)
            with contextlib.suppress(OSError):
                conn.close()

    def _answer(self, request: object) -> Answer:
        with self._lock:
            pipeline, loop = self._pipeline, self._loop
        if pipeline is None or loop is None or loop.is_closed():
            return refused(NOT_RUNNING)
        try:
            future = asyncio.run_coroutine_threadsafe(pipeline.handle(request), loop)
        except RuntimeError:
            return refused(NOT_RUNNING)
        while True:
            try:
                return future.result(timeout=ANSWER_POLL_S)
            except TimeoutError:
                if self._stopped.is_set() or loop.is_closed():
                    future.cancel()
                    return refused(NOT_RUNNING)
            except Exception as exc:  # cancelled with its call, or the dispatch itself broke
                log.debug("execute_code rpc answer failed: %s", type(exc).__name__)
                return refused(NOT_RUNNING)

    # -- closing ----------------------------------------------------------------------------------

    def close(self) -> None:
        """Stop serving and remove the socket; safe to call twice."""
        if self._stopped.is_set():
            return
        self._stopped.set()
        self.detach()
        with contextlib.suppress(OSError):
            self._server.close()
        with self._lock:
            conns = list(self._conns)
            self._conns.clear()
        for conn in conns:
            with contextlib.suppress(OSError):
                conn.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                conn.close()
        self._thread.join(timeout=2)
        if self.socket_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(self.socket_path)
        if self._short_dir is not None:
            shutil.rmtree(self._short_dir, ignore_errors=True)


__all__ = [
    "FOREGROUND_ONLY",
    "NOT_RUNNING",
    "Answer",
    "CallRecord",
    "RpcPipeline",
    "SocketRpcServer",
    "ToolInvoker",
    "decode_request",
    "error_of",
    "refused",
    "token_ok",
]
