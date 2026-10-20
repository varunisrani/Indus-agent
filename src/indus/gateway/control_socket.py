"""The gateway's control socket server (P15-27).

Local only, never TCP: a Unix socket in the profile home (paths, wire and client in
``indus.service.gateway_control``). The socket is created 0600 under a 0177 umask, and every
connection's peer uid is checked where the OS reports it (``SO_PEERCRED`` on Linux,
``LOCAL_PEERCRED`` on macOS): another user gets ``forbidden`` and nothing else.

One exchange per connection: read one JSON line (at most 64 KiB, 2 s to arrive), answer one line
(at most 512 KiB, else ``response_too_large``), close. Requests carry exactly
``protocol``/``verb``/``id`` and optionally ``params``; anything else is ``bad_request``.

Verbs: ``identify`` and ``status`` are built in and answered on the loop (they only copy what the
daemon already holds, so they answer while turns run). ``drain``, ``reload-plugins``,
``pause-for-update``, ``session-ticket`` and ``describe`` are known but answered ``unsupported``
until the daemon registers handlers for them (``register``; the attach verbs come from
``gateway.daemon.attach_verbs``, P17-11). A handler takes the request's ``params`` dict and
returns a dict; a synchronous one runs on the ``io`` blocking pool (D-04.1) within its time
budget. A handler refuses with ``VerbRefused(code, detail)``: the answer carries that code.

Owner-only verbs (``session-ticket``, and any verb registered with ``owner_only=True``) are
answered only when the OS confirmed the peer is this user; where it cannot say, they answer
``forbidden`` (the other verbs still answer: the socket file itself is 0600).

``start`` fails (``ControlSocketError``) when the socket cannot be bound — the daemon treats that
as a failed startup (P15-24). A leftover socket from a crash is replaced only when it is ours and
nobody answers on it.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import os
import shutil
import socket
import stat
import struct
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, cast

from indus.foundation.blocking import DeadlineMissed, run_blocking
from indus.foundation.errors import IndusError
from indus.foundation.fsio import atomic_write_text
from indus.foundation.profiles import profile_id_of
from indus.foundation.scope import EnvView
from indus.service import gateway_control as wire
from indus.state.gateway_runtime import process_start_time

__all__ = [
    "KNOWN_VERBS",
    "ControlServer",
    "ControlSocketError",
    "Handler",
    "OWNER_ONLY_VERBS",
    "VerbRefused",
    "code_version",
    "identity_payload",
    "peer_uid",
    "supervisor_kind",
]

log = logging.getLogger(__name__)

KNOWN_VERBS = (
    "identify",
    "status",
    "drain",
    "reload-plugins",
    "pause-for-update",
    "session-ticket",
    "describe",
    "maintenance-run",
)
"""Verbs P15 and P17 name; all but the first two answer ``unsupported`` until registered."""
OWNER_ONLY_VERBS = frozenset({"session-ticket", "maintenance-run"})
"""Verbs answered only to a peer the OS confirmed as this user (they hand out credentials, or
bulk-change the profile's data: ``maintenance-run``, P17-21)."""

LAUNCHD_LABEL_PREFIX = "ai.indus.gateway"
_SOCKET_NAME = "gw.sock"
_MAX_ID_CHARS = 128

type Handler = Callable[[dict[str, Any]], Mapping[str, Any] | Awaitable[Mapping[str, Any]]]


class ControlSocketError(IndusError):
    default_code = "gateway.control_socket"


class VerbRefused(Exception):
    """A handler's refusal: answered ``{"ok": false, "error": code, "detail": detail}``. The
    detail must never carry a secret."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


@dataclass
class _Verb:
    handler: Handler
    timeout: float
    inline: bool = False
    owner_only: bool = False


# -- identity -------------------------------------------------------------------------------------


def supervisor_kind(env: Mapping[str, str]) -> str:
    """Who restarts this process: ``systemd``, ``launchd``, ``external`` or ``manual``."""
    if env.get("INVOCATION_ID"):
        return "systemd"
    if env.get("XPC_SERVICE_NAME", "").startswith(LAUNCHD_LABEL_PREFIX):
        return "launchd"
    if env.get("INDUS_GATEWAY_SUPERVISED", "").strip() not in ("", "0"):
        return "external"
    return "manual"


def code_version() -> str:
    """The installed ``indus`` version (``unknown`` from a bare source tree)."""
    try:
        return metadata.version("indus")
    except metadata.PackageNotFoundError:
        return "unknown"


def identity_payload(home: Path, env: Mapping[str, str]) -> dict[str, Any]:
    """The ``identify`` answer for this process serving ``home``."""
    pid = os.getpid()
    return {
        "pid": pid,
        "start_time": process_start_time(pid),
        "home": str(home),
        "profile": profile_id_of(home),
        "supervisor": supervisor_kind(env),
        "code_version": code_version(),
    }


# -- peer credentials -----------------------------------------------------------------------------

_LOCAL_PEERCRED = 0x001  # macOS <sys/un.h>, at level SOL_LOCAL (0)
_XUCRED_SIZE = 76


def peer_uid(sock: socket.socket) -> int | None:
    """The connecting process's uid, or None when this OS does not say."""
    try:
        if sys.platform.startswith("linux"):
            raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", raw)
            return uid
        if sys.platform == "darwin":
            raw = sock.getsockopt(0, _LOCAL_PEERCRED, _XUCRED_SIZE)
            _version, uid = struct.unpack_from("=II", raw)
            return uid
    except (OSError, struct.error):
        return None
    return None


# -- the server -----------------------------------------------------------------------------------


def _short_base(env: Mapping[str, str]) -> Path:
    """A base directory for the long-home fallback: a private ``$XDG_RUNTIME_DIR``, else the
    temp dir, else ``/tmp`` (the shortest)."""
    runtime = env.get("XDG_RUNTIME_DIR", "")
    if runtime and os.path.isabs(runtime):
        with contextlib.suppress(OSError):
            info = os.lstat(runtime)
            private = (
                stat.S_ISDIR(info.st_mode)
                and info.st_uid == os.getuid()
                and not info.st_mode & 0o077
            )
            if private and wire.fits(Path(runtime) / "indus-gw-XXXXXXXX" / _SOCKET_NAME):
                return Path(runtime)
    temp = Path(tempfile.gettempdir())
    if wire.fits(temp / "indus-gw-XXXXXXXX" / _SOCKET_NAME):
        return temp
    return Path("/tmp")  # only for a short name; the directory itself is still mkdtemp


class ControlServer:
    """See the module notes. ``identity`` and ``status`` build the two built-in answers."""

    def __init__(
        self,
        home: Path,
        *,
        identity: Callable[[], Mapping[str, Any]] | None = None,
        status: Callable[[], Mapping[str, Any]] | None = None,
        env: Mapping[str, str] | None = None,
        handler_timeout: float = wire.DEFAULT_TIMEOUT_S,
        read_timeout: float = wire.DEFAULT_TIMEOUT_S,
        peer: Callable[[socket.socket], int | None] = peer_uid,
    ) -> None:
        self.home = home
        self.env: Mapping[str, str] = EnvView.from_process() if env is None else env
        self.handler_timeout = handler_timeout
        self.read_timeout = read_timeout
        self._peer = peer
        self._identity = identity or (lambda: identity_payload(self.home, self.env))
        self._status = status or (lambda: {})
        self._verbs: dict[str, _Verb] = {
            "identify": _Verb(lambda _p: self._identity(), handler_timeout, inline=True),
            "status": _Verb(self._status_answer, handler_timeout, inline=True),
        }
        self._server: asyncio.AbstractServer | None = None
        self._path: Path | None = None
        self._pointer: Path | None = None
        self._temp_dir: Path | None = None
        self._ids: dict[Path, tuple[int, int]] = {}

    @property
    def path(self) -> Path | None:
        """Where the socket is bound (None before ``start`` / after ``stop``)."""
        return self._path

    @property
    def verbs(self) -> list[str]:
        return sorted(self._verbs)

    def register(
        self,
        verb: str,
        handler: Handler,
        *,
        timeout: float | None = None,
        owner_only: bool = False,
    ) -> None:
        """Answer ``verb`` with ``handler`` (replaces an earlier one). ``owner_only`` (always on
        for ``OWNER_ONLY_VERBS``) answers only a peer the OS confirmed as this user."""
        if not verb or not isinstance(verb, str):
            raise ValueError("a verb needs a name")
        self._verbs[verb] = _Verb(
            handler,
            self.handler_timeout if timeout is None else timeout,
            owner_only=owner_only or verb in OWNER_ONLY_VERBS,
        )

    def _status_answer(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {**dict(self._status()), "answering_pid": os.getpid(), "answered_at": time.time()}

    # -- lifecycle ------------------------------------------------------------------------------

    async def start(self) -> Path:
        if sys.platform == "win32":
            raise ControlSocketError("the control socket needs a POSIX system (D-16.55)")
        try:
            return await self._bind()
        except ControlSocketError:
            await self.stop()
            raise
        except OSError as exc:
            await self.stop()
            raise ControlSocketError(
                f"the control socket could not be bound: {exc.strerror or exc}"
            ) from exc

    async def _bind(self) -> Path:
        direct = self.home / wire.SOCKET_FILE
        if wire.fits(direct):
            bind = direct
        else:
            self._temp_dir = Path(tempfile.mkdtemp(prefix="indus-gw-", dir=_short_base(self.env)))
            bind = self._temp_dir / _SOCKET_NAME
            if not wire.fits(bind):
                raise ControlSocketError(f"no short enough place for the control socket ({bind})")
        await self._clear_leftover(bind)
        old_mask = os.umask(0o177)
        try:
            self._server = await asyncio.start_unix_server(self._serve, path=str(bind))
        finally:
            os.umask(old_mask)
        os.chmod(bind, 0o600)
        self._path = bind
        self._remember(bind)
        if self._temp_dir is not None:
            pointer = wire.pointer_path(self.home)
            atomic_write_text(pointer, str(bind), mode=0o600)
            self._pointer = pointer
            self._remember(pointer)
        log.info("control socket listening at %s", bind)
        return bind

    def _remember(self, path: Path) -> None:
        info = path.lstat()
        self._ids[path] = (info.st_dev, info.st_ino)

    async def _clear_leftover(self, path: Path) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode):
            raise ControlSocketError(f"{path} is a symlink; refusing to bind there")
        if not stat.S_ISSOCK(info.st_mode):
            raise ControlSocketError(f"{path} exists and is not a socket")
        if info.st_uid != os.getuid():
            raise ControlSocketError(f"{path} belongs to another user")
        try:
            _reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(path)), 1.0)
        except (OSError, TimeoutError):
            path.unlink()  # a crash leftover: nobody listens
            return
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        raise ControlSocketError(f"another gateway already answers on {path}")

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        for path, ident in list(self._ids.items()):
            with contextlib.suppress(OSError):
                info = path.lstat()
                if (info.st_dev, info.st_ino) == ident:
                    path.unlink()
        self._ids.clear()
        temp, self._temp_dir = self._temp_dir, None
        if temp is not None:
            shutil.rmtree(temp, ignore_errors=True)
        self._path = self._pointer = None

    # -- one connection -------------------------------------------------------------------------

    async def _read_request(self, reader: asyncio.StreamReader) -> bytes | str:
        """The request line, or an error code."""
        buffer = bytearray()
        try:
            async with asyncio.timeout(self.read_timeout):
                while b"\n" not in buffer:
                    chunk = await reader.read(65536)
                    if not chunk:
                        break
                    buffer.extend(chunk)
                    if len(buffer.partition(b"\n")[0]) > wire.MAX_REQUEST_BYTES:
                        await self._discard_rest(reader, chunk)
                        return "request_too_large"
        except TimeoutError:
            return "timeout" if buffer else ""
        line, newline, _ = bytes(buffer).partition(b"\n")
        if len(line) > wire.MAX_REQUEST_BYTES:
            return "request_too_large"
        if not newline:
            return "bad_request" if line else ""
        return line

    async def _discard_rest(self, reader: asyncio.StreamReader, last: bytes) -> None:
        """Swallow what is left of an oversized line (bounded), so the refusal reaches a peer
        that is still writing instead of a reset connection."""
        budget = 4 * wire.MAX_REQUEST_BYTES
        with contextlib.suppress(TimeoutError, OSError):
            async with asyncio.timeout(self.read_timeout):
                while b"\n" not in last and budget > 0:
                    last = await reader.read(65536)
                    if not last:
                        return
                    budget -= len(last)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            sock = cast(socket.socket | None, writer.get_extra_info("socket"))
            uid = self._peer(sock) if sock is not None else None
            # The request is read (bounded) even from a refused peer: closing with unread input
            # would reset the connection before the peer sees why.
            got = await self._read_request(reader)
            if uid is not None and uid != os.getuid():
                answer = _encode({"ok": False, "protocol": wire.PROTOCOL, "error": "forbidden"})
            elif got == "":
                return
            elif isinstance(got, str):
                answer = _encode({"ok": False, "protocol": wire.PROTOCOL, "error": got})
            else:
                answer = await self.handle_line(got, peer_verified=uid == os.getuid())
            writer.write(answer)
            async with asyncio.timeout(self.read_timeout):
                await writer.drain()
        except (OSError, TimeoutError):
            pass
        except Exception:
            log.debug("control socket connection failed", exc_info=True)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def handle_line(self, raw: bytes, *, peer_verified: bool = False) -> bytes:
        """One request line → one answer line (with its newline). Never raises.

        ``peer_verified``: the OS confirmed the peer is this user (owner-only verbs need it)."""
        request_id: str | int | None = None
        try:
            request = _parse(raw)
            request_id = cast(str | int | None, request.get("id"))
            verb = cast(str, request["verb"])
            params = cast(dict[str, Any], request.get("params") or {})
        except _BadRequest as bad:
            return _encode(_failure("bad_request", str(bad), bad.request_id))
        entry = self._verbs.get(verb)
        if entry is None:
            code = "unsupported" if verb in KNOWN_VERBS else "unknown_verb"
            return _encode(_failure(code, f"no handler for {verb!r}", request_id, self.verbs))
        if entry.owner_only and not peer_verified:
            return _encode(_failure("forbidden", "the peer could not be verified", request_id))
        try:
            result = await self._call(entry, params)
        except VerbRefused as refused:
            return _encode(_failure(refused.code, refused.detail, request_id))
        except (TimeoutError, DeadlineMissed):
            return _encode(_failure("timeout", f"{verb} took too long", request_id))
        except Exception as exc:
            log.warning("control verb %s failed", verb, exc_info=True)
            return _encode(_failure("failed", type(exc).__name__, request_id))
        response: dict[str, Any] = {"ok": True, "protocol": wire.PROTOCOL, "result": result}
        if request_id is not None:
            response["id"] = request_id
        try:
            encoded = _encode(response)
        except (TypeError, ValueError):
            return _encode(_failure("failed", "the result is not JSON", request_id))
        if len(encoded) - 1 > wire.MAX_RESPONSE_BYTES:
            too_large = _failure("response_too_large", "", request_id)
            too_large["limit"] = wire.MAX_RESPONSE_BYTES
            return _encode(too_large)
        return encoded

    async def _call(self, entry: _Verb, params: dict[str, Any]) -> dict[str, Any]:
        handler = entry.handler
        if entry.inline:
            produced = handler(params)
        elif inspect.iscoroutinefunction(handler):
            async with asyncio.timeout(entry.timeout):
                produced = await cast(Awaitable[Mapping[str, Any]], handler(params))
        else:
            produced = await run_blocking(
                handler, params, pool="io", deadline=entry.timeout, label="control verb"
            )
        if inspect.isawaitable(produced):
            async with asyncio.timeout(entry.timeout):
                produced = await produced
        if not isinstance(produced, Mapping):
            raise TypeError("a control verb must answer with a mapping")
        return dict(cast(Mapping[str, Any], produced))


# -- wire helpers ---------------------------------------------------------------------------------


class _BadRequest(ValueError):
    def __init__(self, message: str, request_id: str | int | None = None) -> None:
        super().__init__(message)
        self.request_id = request_id


def _usable_id(value: object) -> str | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) or (isinstance(value, str) and len(value) <= _MAX_ID_CHARS):
        return value
    return None


def _parse(raw: bytes) -> dict[str, Any]:
    try:
        loaded: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _BadRequest("the request is not JSON") from None
    if not isinstance(loaded, dict):
        raise _BadRequest("the request must be a JSON object")
    request = cast(dict[str, Any], loaded)
    raw_id: object = request.get("id")
    request_id = _usable_id(raw_id)
    extra = set(request) - wire.REQUEST_KEYS
    if extra:
        raise _BadRequest(f"unknown keys: {', '.join(sorted(extra))}", request_id)
    if raw_id is not None and request_id is None:
        raise _BadRequest("id must be a number or a short string")
    if request.get("protocol") != wire.PROTOCOL or isinstance(request.get("protocol"), bool):
        raise _BadRequest(f"protocol must be {wire.PROTOCOL}", request_id)
    verb = request.get("verb")
    if not isinstance(verb, str) or not verb:
        raise _BadRequest("verb must be a non-empty string", request_id)
    if "params" in request and not isinstance(request["params"], dict):
        raise _BadRequest("params must be a JSON object", request_id)
    return request


def _failure(
    code: str, detail: str, request_id: str | int | None, verbs: list[str] | None = None
) -> dict[str, Any]:
    answer: dict[str, Any] = {"ok": False, "protocol": wire.PROTOCOL, "error": code}
    if detail:
        answer["detail"] = detail
    if request_id is not None:
        answer["id"] = request_id
    if verbs is not None:
        answer["verbs"] = verbs
    return answer


def _encode(answer: Mapping[str, Any]) -> bytes:
    return json.dumps(answer, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
