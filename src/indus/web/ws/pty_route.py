"""``/api/pty``: the Chat tab's terminal socket (P18-09 over P18-07/08).

Query: ``channel``, ``attach``, ``resume``, ``profile``, ``cwd``, ``fresh`` (the credential is the
gate's business). In order:

1. the shared WebSocket helper (``guarded_ws``, purpose ``web-pty``, a person's credential only)
   accepts the socket or has already closed it;
2. no PTY support on this machine → its reason in red in the terminal, close 1011 (on Windows the
   WSL2 notice, D-16.38);
3. the profile scope (P18-10) of the grant; unknown → red text + 1011;
4. the channel (an invalid id counts as none) and its active-session file; ``fresh`` drops the
   file first; with no ``resume`` the channel's last session is reopened and announced with a JSON
   text frame ``{"type": "resume", "id": …}`` before any terminal byte;
5. a picked ``cwd`` for a fresh chat (absolute, existing directory, else red text + 1011);
6. the child: ``python -m indus --tui [-r <id>]`` for that profile — what ``indus --tui`` runs —
   with the env from the single child-env builder (R02.22) plus the publish URL and the internal
   credential (separate variables, the URL carries no credential), the channel file for the
   renderer's live session id, ``COLORTERM=truecolor``, mouse reporting off (the browser's wheel
   scrolls the terminal) and ``INDUS_PTY_HOST=dashboard``; no Node
   on the PATH → red text + 1011;
7. without ``attach``: one PTY for this socket only, ended when the socket goes, the channel file
   this socket made dropped in a ``finally`` a cancelled handler still runs. With ``attach``: the
   keep-alive registry (P18-08) — other PTYs of the token and orphans holding the resumed
   conversation (by profile + resume id, or by its turn-lease holder) are ended, then attach or
   spawn; a reused PTY replays its ring and gets a redraw byte.

Input: a frame that is exactly the resize escape is applied and never written (R09.7); anything
else is written; input the child does not take closes the socket 1013. When the child exits the
socket is closed 4410.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

from starlette.websockets import WebSocket, WebSocketDisconnect

from indus.foundation.blocking import run_blocking
from indus.foundation.scope import Scope
from indus.platform import locate
from indus.safety.child_env import child_env
from indus.web.pty import bridge as pty_bridge
from indus.web.pty.bridge import PtyBridge, PtyUnavailable
from indus.web.pty.channels import ChannelFiles, CwdRefused, resolve_cwd, valid_channel
from indus.web.pty.registry import (
    CLOSE_EXITED,
    CLOSE_STALLED,
    PtyKey,
    PtyRegistry,
    RegistryFull,
    close_quietly,
)
from indus.web.pty.resize import TermSize, message_bytes, parse_resize
from indus.web.scope import ProfileScopes, ProfileUnknown
from indus.web.ws import WsGrant, guarded_ws
from indus.web.ws.chat_gate import PURPOSE_PTY, SidecarLink, scope_of

__all__ = [
    "CLOSE_START_FAILED",
    "NODE_MISSING",
    "PTY_HOST_ENV",
    "SESSION_FILE_ENV",
    "SIDECAR_CREDENTIAL_ENV",
    "SIDECAR_URL_ENV",
    "ChatStartError",
    "ChildRequest",
    "ChildSpec",
    "PtyRoute",
    "TuiChild",
    "lease_holder_pid",
]

log = logging.getLogger(__name__)

CLOSE_START_FAILED: Final = 1011
SIDECAR_URL_ENV: Final = "INDUS_TUI_SIDECAR_URL"
SIDECAR_CREDENTIAL_ENV: Final = "INDUS_TUI_SIDECAR_CREDENTIAL"
SESSION_FILE_ENV: Final = "INDUS_TUI_DASHBOARD_SESSION_FILE"
"""Read by ``cli.tui_launch``: the renderer writes its live session id into this file (which the
launcher then keeps) instead of a temp file of its own."""
PTY_HOST_ENV: Final = "INDUS_PTY_HOST"
MOUSE_ENV: Final = "INDUS_TUI_MOUSE"
PTY_HOST: Final = "dashboard"
_TRUE: Final = frozenset({"1", "true", "yes", "on"})

NODE_MISSING: Final = (
    "Chat could not start: Node.js was not found on this machine's PATH. Install Node.js 24 LTS "
    "and reload this page."
)
PROFILE_MISSING: Final = "Chat could not start: there is no such profile."
SPAWN_FAILED: Final = "Chat could not start: the terminal process could not be started."


class ChatStartError(Exception):
    """The chat cannot start; the message is shown to the person as is."""


@dataclass(frozen=True)
class ChildRequest:
    """Everything the child's argv and env depend on."""

    scope: Scope
    resume: str | None
    cwd: Path | None
    session_file: Path | None
    sidecar_url: str | None
    sidecar_credential: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ChildSpec:
    """The PTY child to start."""

    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str] = field(repr=False)


class ChildResolver(Protocol):
    def __call__(self, request: ChildRequest) -> ChildSpec: ...


def _default_cwd(scope: Scope) -> Path:
    home = (scope.env.get("HOME") or "").strip()
    if home:
        candidate = Path(home)
        if candidate.is_absolute() and candidate.is_dir():
            return candidate
    return scope.home


@dataclass(frozen=True)
class TuiChild:
    """``python -m indus --tui`` for the request's profile (see the module notes)."""

    python: str = field(default_factory=lambda: sys.executable)
    which: Callable[[str, str | None], str | None] = locate.which

    def __call__(self, request: ChildRequest) -> ChildSpec:
        env = child_env(request.scope)
        if self.which("node", env.get("PATH")) is None:
            raise ChatStartError(NODE_MISSING)
        argv = [self.python, "-m", "indus", "--tui"]
        if request.resume:
            argv += ["-r", request.resume]
        env.setdefault("COLORTERM", "truecolor")
        env[MOUSE_ENV] = "off"  # the browser's wheel scrolls xterm; no mouse reports
        env[PTY_HOST_ENV] = PTY_HOST
        if request.session_file is not None:
            env[SESSION_FILE_ENV] = str(request.session_file)
        if request.sidecar_url:
            env[SIDECAR_URL_ENV] = request.sidecar_url
            if request.sidecar_credential:
                env[SIDECAR_CREDENTIAL_ENV] = request.sidecar_credential
        cwd = request.cwd if request.cwd is not None else _default_cwd(request.scope)
        return ChildSpec(tuple(argv), cwd, env)


def lease_holder_pid(scope: Scope, session_id: str) -> int | None:
    """The pid holding ``session_id``'s turn lease in ``scope``'s store, read browse-only (an
    absent or unreadable store has no holder)."""
    from indus.state import browse, turn_leases

    try:
        store = browse.open_readonly(scope)
    except Exception:
        return None
    try:
        with store.read() as conn:
            row = turn_leases.get_in(conn, session_id)
    except Exception:
        return None
    finally:
        store.close()
    return None if row is None else row.holder_pid


type SpawnFn = Callable[..., PtyBridge]
type LeaseHolder = Callable[[Scope, str], int | None]


async def _fail(websocket: WebSocket, text: str) -> None:
    """Tell the person why the chat cannot start, then close 1011."""
    with contextlib.suppress(Exception):
        await websocket.send_text(f"\r\n\x1b[31m{text}\x1b[0m\r\n")
    await close_quietly(websocket, CLOSE_START_FAILED, "chat could not start")


async def _input_loop(
    websocket: WebSocket,
    write: Callable[[bytes], Awaitable[bool]],
    resize: Callable[[TermSize], None],
) -> None:
    """Forward the socket's input until it goes; resize frames are applied, never written."""
    while True:
        try:
            message = await websocket.receive()
        except (RuntimeError, WebSocketDisconnect):
            return
        if message.get("type") == "websocket.disconnect":
            return
        data = message_bytes(message)
        if not data:
            continue
        size = parse_resize(data)
        if size is not None:
            resize(size)
            continue
        if not await write(data):
            await close_quietly(websocket, CLOSE_STALLED, "the chat stopped taking input")
            return


class PtyRoute:
    """The ``/api/pty`` endpoint (see the module notes)."""

    purpose: Final = PURPOSE_PTY

    def __init__(
        self,
        *,
        scopes: ProfileScopes,
        registry: PtyRegistry,
        channels: ChannelFiles,
        link: SidecarLink | None = None,
        child: ChildResolver | None = None,
        spawn: SpawnFn = pty_bridge.spawn,
        unavailable: Callable[[], str | None] = pty_bridge.unavailable,
        lease_holder: LeaseHolder = lease_holder_pid,
    ) -> None:
        self.scopes = scopes
        self.registry = registry
        self.channels = channels
        self.link = link
        self.child: ChildResolver = child if child is not None else TuiChild()
        self._spawn = spawn
        self._unavailable = unavailable
        self._lease_holder = lease_holder

    @property
    def endpoint(self) -> Callable[[WebSocket], Awaitable[None]]:
        """The route endpoint: ``handle`` behind the shared WebSocket helper."""

        async def pty_socket(websocket: WebSocket, grant: WsGrant) -> None:
            await self.handle(websocket, grant)

        return guarded_ws(self.purpose)(pty_socket)

    async def _start_bridge(self, spec: ChildSpec) -> PtyBridge:
        def start() -> PtyBridge:
            return self._spawn(spec.argv, cwd=spec.cwd, env=spec.env)

        return await run_blocking(start, label="pty spawn")

    async def handle(self, websocket: WebSocket, grant: WsGrant) -> None:
        """Serve an accepted socket (``grant`` is what the helper proved: any person's
        credential may open a terminal, the helper refused the rest)."""
        reason = self._unavailable()
        if reason is not None:
            await _fail(websocket, reason)
            return
        params = websocket.query_params
        try:
            scope = scope_of(self.scopes, grant)
        except ProfileUnknown:
            await _fail(websocket, PROFILE_MISSING)
            return

        raw_resume = (params.get("resume") or "").strip() or None
        fresh = (params.get("fresh") or "").strip().lower() in _TRUE
        resume = None if fresh else raw_resume
        channel = valid_channel(params.get("channel"))
        marker: Path | None = None
        existed = False
        if channel is not None:
            if fresh:
                self.channels.clear(channel)
            marker, existed = self.channels.claim(channel)
            if resume is None and not fresh:
                implicit = self.channels.read(channel)
                if implicit is not None:
                    resume = implicit
                    with contextlib.suppress(Exception):
                        await websocket.send_json({"type": "resume", "id": implicit})

        def discard() -> None:
            if channel is not None and marker is not None and not existed:
                self.channels.release(channel, marker)

        try:
            cwd = None if resume else resolve_cwd(params.get("cwd"))
            request = ChildRequest(
                scope=scope,
                resume=resume,
                cwd=cwd,
                session_file=marker,
                sidecar_url=self.link.publish_url(channel) if self.link and channel else None,
                sidecar_credential=self.link.credential() if self.link and channel else None,
            )
            spec = await run_blocking(self.child, request, label="chat child")
        except (CwdRefused, ChatStartError) as exc:
            discard()
            await _fail(websocket, str(exc))
            return

        token = (params.get("attach") or "").strip()
        if not token:
            await self._direct(websocket, spec, discard)
            return
        key = PtyKey(token, scope.profile_id, raw_resume if not fresh and raw_resume else "")
        await self._keep_alive(websocket, scope, key, spec, channel, marker, discard)

    async def _direct(
        self, websocket: WebSocket, spec: ChildSpec, discard: Callable[[], None]
    ) -> None:
        """One PTY for one socket (no attach token)."""
        try:
            try:
                bridge = await self._start_bridge(spec)
            except PtyUnavailable as exc:
                await _fail(websocket, str(exc))
                return
            except OSError as exc:
                log.warning("pty spawn failed: %s", type(exc).__name__)
                await _fail(websocket, SPAWN_FAILED)
                return
            await self._pump(websocket, bridge)
        finally:
            discard()  # nothing outlives this socket, so its marker goes with it

    async def _pump(self, websocket: WebSocket, bridge: PtyBridge) -> None:
        async def output() -> None:
            try:
                while True:
                    chunk = await bridge.read()
                    if chunk is None:
                        break
                    await websocket.send_bytes(chunk)
            except Exception:
                pass
            finally:
                # The child ended (or the send side broke): closing the socket unparks the input
                # loop's receive even on a half-open connection.
                await close_quietly(websocket, CLOSE_EXITED, "the chat process exited")

        reader = asyncio.get_running_loop().create_task(output(), name="indus-pty-output")
        try:
            await _input_loop(websocket, bridge.write, bridge.resize)
        finally:
            reader.cancel()
            with contextlib.suppress(BaseException):
                await reader
            await bridge.close()

    async def _keep_alive(
        self,
        websocket: WebSocket,
        scope: Scope,
        key: PtyKey,
        spec: ChildSpec,
        channel: str | None,
        marker: Path | None,
        discard: Callable[[], None],
    ) -> None:
        on_close: Callable[[], object] | None = None
        if channel is not None and marker is not None:
            path, name = marker, channel

            def release() -> None:
                self.channels.release(name, path)

            on_close = release
        try:
            await self.registry.close_others(key.token, keep=key)
            holder = None
            if key.resume:
                holder = await run_blocking(
                    self._lease_holder, scope, key.resume, label="lease holder"
                )
            await self.registry.close_orphans(key, holder_pid=holder)
            session, created = await self.registry.attach_or_spawn(
                key, lambda: self._start_bridge(spec), on_close=on_close
            )
        except PtyUnavailable as exc:
            discard()
            await _fail(websocket, str(exc))
            return
        except RegistryFull as exc:
            discard()
            await _fail(websocket, str(exc))
            return
        except OSError as exc:
            log.warning("pty spawn failed: %s", type(exc).__name__)
            discard()
            await _fail(websocket, SPAWN_FAILED)
            return
        if not created:
            discard()  # the PTY that answered owns its own channel file
        if not await session.attach(websocket, redraw=not created):
            if session.viewer is websocket:
                await close_quietly(websocket, CLOSE_STALLED, "the chat stopped taking input")
            self.registry.detach(key, websocket)
            return
        try:
            await _input_loop(
                websocket,
                lambda data: session.write(websocket, data),
                lambda size: session.resize(websocket, size),
            )
        finally:
            self.registry.detach(key, websocket)
