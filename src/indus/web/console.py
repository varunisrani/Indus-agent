"""``/api/console``: the dashboard's admin console socket (P24-36, over ``service.console``).

Accepted only through the shared WebSocket helper (``guarded_ws``, purpose ``web-console``): the
feature must be on (``DashboardState.console_enabled``, else 4404), then the credential (4401),
Host/Origin (4403) and peer (4408) checks of every dashboard socket. The handler never sees a
credential; the grant names the profile, whose scope the console runs commands for.

Frames are JSON objects, both ways (research 09 §3.6):

- client: ``{"type": "ping"}``, ``{"type": "cancel"}``, ``{"type": "confirm", "command": …}``,
  ``{"type": "input", "line": …}`` (``"command"`` is an alias of ``"input"``);
- server: ``ready {profile, prompt}``, ``output {id, stream, data, command}``, ``error {id?,
  message, command?}``, ``confirm_required {id, command, message, prompt}``, ``complete {id, status,
  command, prompt}``, ``clear``, ``pong``.

Rules: one command at a time (``already running``); a pending confirmation is confirmed only with
the same command, or cancelled, before anything else runs; ``cancel`` stops the running command
(its ``CancelToken`` kills the command's process tree, an agent run it started included) and
waits ``UNWIND_S`` for the worker;
a command runs at most ``COMMAND_TIMEOUT_S``; the engine caps output (50,000 characters);
commands run on one pool of ``WORKERS`` threads shared by every console socket; ``exit`` closes
1000; a profile that does not exist closes 4400 and any other start failure 1011.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Final, cast

from starlette.websockets import WebSocket, WebSocketDisconnect

from indus.foundation.scope import Scope
from indus.service.console import PROMPT, CancelToken, ConsoleEngine, ConsoleResult
from indus.service.ws_auth import WEB_CONSOLE
from indus.web.scope import ProfileScopes, ProfileUnknown
from indus.web.ws import WsGrant, guarded_ws
from indus.web.ws.chat_gate import scope_of

__all__ = [
    "CLOSE_BAD_PROFILE",
    "CLOSE_EXIT",
    "CLOSE_START_FAILED",
    "COMMAND_TIMEOUT_S",
    "CONSOLE_PATH",
    "MAX_FRAME",
    "UNWIND_S",
    "WORKERS",
    "ConsoleRoute",
]

log = logging.getLogger(__name__)

CONSOLE_PATH: Final = "/api/console"
COMMAND_TIMEOUT_S: Final = 60.0
UNWIND_S: Final = 10.0
WORKERS: Final = 4
MAX_FRAME: Final = 64 * 1024
CLOSE_EXIT: Final = 1000
CLOSE_BAD_PROFILE: Final = 4400
CLOSE_START_FAILED: Final = 1011

type Sleep = Callable[[float], Awaitable[None]]
type EngineFactory = Callable[[Scope], ConsoleEngine]

_POOL: ThreadPoolExecutor | None = None


def _shared_pool() -> ThreadPoolExecutor:
    global _POOL
    if _POOL is None:
        _POOL = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="indus-console")
    return _POOL


@dataclass
class _Running:
    id: int
    command: str
    token: CancelToken
    future: asyncio.Future[ConsoleResult]
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None


@dataclass
class _Session:
    websocket: WebSocket
    engine: ConsoleEngine
    profile: str
    ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1))
    running: _Running | None = None
    pending: tuple[int, str] | None = None
    closed: bool = False

    async def send(self, frame: dict[str, Any]) -> None:
        if self.closed:
            return
        try:
            await self.websocket.send_text(json.dumps(frame))
        except (WebSocketDisconnect, RuntimeError):
            self.closed = True


async def _say_and_close(websocket: WebSocket, message: str, code: int) -> None:
    with contextlib.suppress(Exception):
        await websocket.send_text(json.dumps({"type": "error", "message": message, "prompt": ""}))
    with contextlib.suppress(Exception):
        await websocket.close(code=code, reason=message)


@dataclass
class ConsoleRoute:
    """The console socket of one backend (see the module notes)."""

    scopes: ProfileScopes
    pool: ThreadPoolExecutor | None = None
    engine_factory: EngineFactory | None = None
    sleep: Sleep | None = None
    """How the command limit waits; default: the backend scope's clock."""
    timeout_s: float = COMMAND_TIMEOUT_S
    unwind_s: float = UNWIND_S

    def __post_init__(self) -> None:
        self.endpoint = guarded_ws(WEB_CONSOLE)(self._handle)

    def _engine(self, scope: Scope) -> ConsoleEngine:
        if self.engine_factory is not None:
            return self.engine_factory(scope)
        return ConsoleEngine(scope)

    async def _handle(self, websocket: WebSocket, grant: WsGrant) -> None:
        """The admin console (see the module notes)."""
        try:
            scope = scope_of(self.scopes, grant)
            engine = self._engine(scope)
        except ProfileUnknown:
            await _say_and_close(websocket, "There is no such profile.", CLOSE_BAD_PROFILE)
            return
        except Exception as exc:
            log.warning("the console could not start: %s", type(exc).__name__)
            await _say_and_close(websocket, "The console could not start.", CLOSE_START_FAILED)
            return
        profile = grant.profile.name if grant.profile is not None else ""
        session = _Session(websocket, engine, profile or "default")
        await session.send({"type": "ready", "profile": session.profile, "prompt": PROMPT})
        try:
            while not session.closed:
                try:
                    message = await websocket.receive()
                except (WebSocketDisconnect, RuntimeError):
                    break
                if message.get("type") == "websocket.disconnect":
                    break
                await self._on_message(session, message)
        finally:
            session.closed = True
            running = session.running
            if running is not None:
                running.token.cancel()
                running.stop.set()

    # -- frames --

    async def _on_message(self, session: _Session, message: Mapping[str, Any]) -> None:
        """Handle one frame."""
        text = message.get("text")
        if text is None:
            await session.send({"type": "error", "message": "Frames are JSON text."})
            return
        if len(text) > MAX_FRAME:
            await session.send({"type": "error", "message": "That frame is too large."})
            return
        try:
            frame: object = json.loads(text)
        except ValueError:
            await session.send({"type": "error", "message": "That frame is not JSON."})
            return
        if not isinstance(frame, dict):
            await session.send({"type": "error", "message": "A frame is a JSON object."})
            return
        data = cast(dict[str, Any], frame)
        kind = data.get("type")
        if kind == "ping":
            await session.send({"type": "pong"})
            return
        if kind == "cancel":
            await self._cancel(session)
            return
        if kind == "confirm":
            command = data.get("command")
            await self._confirm(session, command if isinstance(command, str) else "")
            return
        if kind in ("input", "command"):
            line = data.get("line", data.get("command"))
            if not isinstance(line, str):
                await session.send({"type": "error", "message": "`line` must be text."})
                return
            await self._input(session, line)
            return
        await session.send({"type": "error", "message": f"Unknown frame type {kind!r}."})

    async def _input(self, session: _Session, line: str) -> None:
        if session.running is not None:
            await session.send(
                {
                    "type": "error",
                    "id": session.running.id,
                    "message": "A command is already running; wait for it or cancel it.",
                    "command": session.running.command,
                }
            )
            return
        if session.pending is not None:
            pending_id, pending = session.pending
            await session.send(
                {
                    "type": "error",
                    "id": pending_id,
                    "message": "Confirm or cancel the pending command first.",
                    "command": pending,
                }
            )
            return
        self._start(session, next(session.ids), line, confirmed=False)

    async def _confirm(self, session: _Session, command: str) -> None:
        if session.running is not None:
            await session.send(
                {"type": "error", "id": session.running.id, "message": "A command is running."}
            )
            return
        if session.pending is None:
            await session.send({"type": "error", "message": "Nothing is waiting to be confirmed."})
            return
        pending_id, pending = session.pending
        if command.strip() != pending:
            await session.send(
                {
                    "type": "error",
                    "id": pending_id,
                    "message": "Confirm the same command that asked, or cancel it.",
                    "command": pending,
                }
            )
            return
        session.pending = None
        self._start(session, pending_id, pending, confirmed=True)

    async def _cancel(self, session: _Session) -> None:
        running = session.running
        if running is not None:
            running.token.cancel()
            running.stop.set()
            if running.task is not None:
                await asyncio.shield(running.task)
            return
        if session.pending is not None:
            pending_id, pending = session.pending
            session.pending = None
            await session.send(
                {
                    "type": "complete",
                    "id": pending_id,
                    "status": "cancelled",
                    "command": pending,
                    "prompt": PROMPT,
                }
            )
            return
        await session.send({"type": "complete", "status": "idle", "prompt": PROMPT})

    # -- running --

    def _start(self, session: _Session, cid: int, line: str, *, confirmed: bool) -> None:
        command = line.strip()
        loop = asyncio.get_running_loop()
        token = CancelToken()
        pool = self.pool or _shared_pool()
        future = loop.run_in_executor(
            pool, lambda: session.engine.execute(command, confirmed=confirmed, token=token)
        )
        running = _Running(cid, command, token, future)
        session.running = running
        running.task = loop.create_task(
            self._supervise(session, running), name="indus-console-command"
        )

    async def _supervise(self, session: _Session, running: _Running) -> None:
        cid, command, future = running.id, running.command, running.future
        sleep = self.sleep if self.sleep is not None else self.scopes.base.clock.sleep
        timer = asyncio.ensure_future(sleep(self.timeout_s))
        stopped = asyncio.ensure_future(running.stop.wait())
        try:
            done, _ = await asyncio.wait(
                {future, timer, stopped}, return_when=asyncio.FIRST_COMPLETED
            )
            stop_asked = running.stop.is_set()
            if future in done and not stop_asked and timer not in done:
                result = future.result()
                session.running = None
                await self._report(session, cid, result)
                return
            status = "cancelled" if stop_asked or stopped in done else "timeout"
            running.token.cancel()
            finished, _ = await asyncio.wait({future}, timeout=self.unwind_s)
            session.running = None
            if not finished:
                log.warning("a console command did not stop within %.0f s", self.unwind_s)
            message = (
                f"The command ran longer than {self.timeout_s:.0f} s and was stopped."
                if status == "timeout"
                else "The command was cancelled."
            )
            await session.send({"type": "error", "id": cid, "message": message, "command": command})
            await session.send(
                {
                    "type": "complete",
                    "id": cid,
                    "status": status,
                    "command": command,
                    "prompt": PROMPT,
                }
            )
        finally:
            timer.cancel()
            stopped.cancel()

    async def _report(self, session: _Session, cid: int, result: ConsoleResult) -> None:
        command = result.command
        if result.status == "confirm_required":
            session.pending = (cid, command)
            await session.send(
                {
                    "type": "confirm_required",
                    "id": cid,
                    "command": command,
                    "message": result.message,
                    "prompt": PROMPT,
                }
            )
            await session.send(
                {
                    "type": "complete",
                    "id": cid,
                    "status": "confirm_required",
                    "command": command,
                    "prompt": PROMPT,
                }
            )
            return
        if result.status == "clear":
            await session.send({"type": "clear"})
        for stream, data in (("stdout", result.output), ("stderr", result.errors)):
            if data:
                await session.send(
                    {
                        "type": "output",
                        "id": cid,
                        "stream": stream,
                        "data": data,
                        "command": command,
                    }
                )
        if result.status == "error":
            await session.send(
                {"type": "error", "id": cid, "message": result.message, "command": command}
            )
        await session.send(
            {
                "type": "complete",
                "id": cid,
                "status": result.status,
                "command": command,
                "prompt": PROMPT,
            }
        )
        if result.status == "exit" and not session.closed:
            session.closed = True
            with contextlib.suppress(Exception):
                await session.websocket.close(code=CLOSE_EXIT)
