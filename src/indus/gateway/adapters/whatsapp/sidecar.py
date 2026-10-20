"""Who owns the WhatsApp bridge process (P16-03); the bridge itself is P16-02.

One profile, one bridge. Its state lives in ``<home>/platforms/whatsapp/<profile id>/`` (0700):
``auth/`` (the bridge's WhatsApp session, written 0600 by the bridge), ``bridge.pid`` (JSON: pid,
the kernel start time of that pid, port, profile), ``bridge.token`` (0600, the bearer token of the
running bridge, fresh for every start), ``bridge_port`` (a secondary profile's chosen port) and
``bridge.log`` (0600, the bridge's own output: lifecycle lines only).

``SidecarOwner.ensure`` adopts this profile's bridge when the pidfile names a live process with the
recorded start time, on the port wanted, answering ``/health`` with our token and our profile.
Anything else on the port is not ours: it is never adopted and never signalled
(``whatsapp_bridge_port_taken``, fatal). Otherwise a fresh bridge is spawned (``node <script>``,
environment from ``safety.child_env`` minimal plus the ``INDUS_WA_*`` values, nothing secret in
argv, its own process group) and waited for until ``/health`` answers. ``attach`` is the send-only
path: the running bridge of this profile or nothing. ``stop`` ends only a bridge this owner started
or adopted (TERM, then KILL after 3 s) and removes the pidfile and token.

Ports: ``platforms.whatsapp.bridge_port``; else 3000 for the default profile; else the port this
profile recorded; else the first port in 3001–3999 that no sibling profile recorded and that binds
on 127.0.0.1 (then recorded).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import signal
import socket
import subprocess
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from indus.foundation.clock import Clock
from indus.state.gateway_runtime import pid_alive, process_start_time

__all__ = [
    "DEFAULT_PORT",
    "SECONDARY_FIRST",
    "SECONDARY_LAST",
    "BridgeEndpoint",
    "BridgeError",
    "Child",
    "Health",
    "PidRecord",
    "SidecarOwner",
    "bridge_port",
    "port_free",
    "spawn_process",
    "state_dir",
]

log = logging.getLogger(__name__)

DEFAULT_PORT = 3000
SECONDARY_FIRST = 3001
SECONDARY_LAST = 3999
SPAWN_WAIT_S = 15.0
PROBE_EVERY_S = 0.5
STOP_GRACE_S = 3.0
_DEFAULT_PROFILE = "default"


def state_dir(home: Path, profile_id: str) -> Path:
    return home / "platforms" / "whatsapp" / profile_id


@dataclass(frozen=True)
class Health:
    """What ``/health`` said: ``ok`` (with the body), ``unauthorized`` (our token refused: not
    our bridge) or ``unreachable`` (nothing answered, or not a bridge)."""

    state: Literal["ok", "unauthorized", "unreachable"]
    status: str = ""
    profile: str = ""
    epoch: str = ""
    self_jid: str = ""
    self_lid: str = ""


type ProbeFn = Callable[[str, str], Awaitable[Health]]


class BridgeError(Exception):
    """The bridge could not be started or reached; ``retryable`` false is a configuration
    problem the operator must fix."""

    def __init__(self, message: str, *, code: str, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class BridgeEndpoint:
    url: str
    port: int
    token: str = field(repr=False)


@dataclass(frozen=True)
class PidRecord:
    pid: int
    start: int | None
    port: int
    profile: str

    def to_json(self) -> str:
        return json.dumps(
            {"pid": self.pid, "start": self.start, "port": self.port, "profile": self.profile}
        )

    @classmethod
    def read(cls, path: Path) -> PidRecord | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        pid, start, port, profile = (
            data.get("pid"),  # pyright: ignore[reportUnknownMemberType]
            data.get("start"),  # pyright: ignore[reportUnknownMemberType]
            data.get("port"),  # pyright: ignore[reportUnknownMemberType]
            data.get("profile"),  # pyright: ignore[reportUnknownMemberType]
        )
        if not isinstance(pid, int) or pid <= 1 or not isinstance(port, int):
            return None
        if start is not None and not isinstance(start, int):
            return None
        return cls(pid, start, port, str(profile or ""))


class Child(Protocol):
    """A started bridge process (``asyncio.subprocess.Process`` fits)."""

    @property
    def pid(self) -> int: ...

    @property
    def returncode(self) -> int | None: ...

    def send_signal(self, sig: int, /) -> None: ...


type Spawner = Callable[[Sequence[str], Mapping[str, str], Path], Awaitable[Child]]


async def spawn_process(argv: Sequence[str], env: Mapping[str, str], log_path: Path) -> Child:
    """Start the bridge in its own session (process group), output appended to ``log_path``."""
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        return await asyncio.create_subprocess_exec(
            *argv,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=fd,
            stderr=fd,
            start_new_session=True,
        )
    finally:
        os.close(fd)


def port_free(port: int) -> bool:
    """Whether a listener could bind ``port`` on 127.0.0.1 now (a live listener: no). Like the
    bridge's own bind, address reuse is on where it does not let a bind share a live port."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform != "win32":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))
    except OSError:
        free = False
    else:
        free = True
    finally:
        probe.close()
    return free


def _read_port(path: Path) -> int | None:
    try:
        value = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return value if 1 <= value <= 65535 else None


def _sibling_ports(root: Path, mine: Path) -> set[int]:
    """Ports other profiles recorded: a sibling whose bridge is down keeps its claim."""
    found: set[int] = set()
    patterns = ("platforms/whatsapp/*/bridge_port", "profiles/*/platforms/whatsapp/*/bridge_port")
    for pattern in patterns:
        for record in root.glob(pattern):
            if record == mine:
                continue
            port = _read_port(record)
            if port is not None:
                found.add(port)
    return found


def bridge_port(
    home: Path,
    root: Path,
    profile_id: str,
    explicit: int | None,
    *,
    is_free: Callable[[int], bool] = port_free,
) -> int:
    """The port this profile's bridge listens on (see the module notes)."""
    if explicit is not None:
        if not 1 <= explicit <= 65535:
            raise BridgeError(
                f"platforms.whatsapp.bridge_port {explicit} is not a port",
                code="config",
                retryable=False,
            )
        return explicit
    if profile_id == _DEFAULT_PROFILE:
        return DEFAULT_PORT
    record = state_dir(home, profile_id) / "bridge_port"
    recorded = _read_port(record)
    if recorded is not None:
        return recorded
    claimed = _sibling_ports(root, record)
    for port in range(SECONDARY_FIRST, SECONDARY_LAST + 1):
        if port in claimed or not is_free(port):
            continue
        record.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        record.write_text(f"{port}\n", encoding="utf-8")
        return port
    raise BridgeError(
        f"no free WhatsApp bridge port in {SECONDARY_FIRST}-{SECONDARY_LAST}; set "
        "platforms.whatsapp.bridge_port",
        code="whatsapp_no_port",
        retryable=False,
    )


def _signal_pid(pid: int, sig: int) -> None:
    """Signal the bridge's process group when it leads one (we start it in its own session),
    else the pid alone."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        if os.getpgid(pid) == pid:
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)


def _write_private(path: Path, text: str) -> None:
    """Replace ``path`` with ``text``, owner-only, never through a symlink."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)


class SidecarOwner:
    """Starts, adopts, watches and stops this profile's bridge (see the module notes)."""

    def __init__(
        self,
        *,
        home: Path,
        root: Path,
        profile_id: str,
        clock: Clock,
        probe: ProbeFn,
        node: str | None,
        script: Path | None,
        env_for: Callable[[dict[str, str]], dict[str, str]],
        explicit_port: int | None = None,
        provider: str = "",
        self_chat: bool = False,
        spawner: Spawner = spawn_process,
        start_time: Callable[[int], int | None] = process_start_time,
        alive_check: Callable[[int], bool] | None = None,
        kill: Callable[[int, int], None] | None = None,
        is_free: Callable[[int], bool] = port_free,
        token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
        parent_pid: Callable[[], int] = os.getpid,
    ) -> None:
        self.home = home
        self.root = root
        self.profile_id = profile_id
        self.clock = clock
        self.probe = probe
        self.node = node
        self.script = script
        self.env_for = env_for
        self.explicit_port = explicit_port
        self.provider = provider
        self.self_chat = self_chat
        self._spawner = spawner
        self._start_time = start_time
        self._pid_alive = alive_check or pid_alive
        self._kill = kill or _signal_pid
        self._is_free = is_free
        self._token_factory = token_factory
        self._parent_pid = parent_pid
        self.state = state_dir(home, profile_id)
        self.auth_dir = self.state / "auth"
        self.pidfile = self.state / "bridge.pid"
        self.token_file = self.state / "bridge.token"
        self.log_file = self.state / "bridge.log"
        self._child: Child | None = None
        self._adopted: PidRecord | None = None
        self.spawns = 0

    def __repr__(self) -> str:
        return f"<SidecarOwner {self.profile_id} pid={self.pid}>"

    @property
    def pid(self) -> int | None:
        if self._child is not None:
            return self._child.pid
        return self._adopted.pid if self._adopted is not None else None

    @staticmethod
    def url(port: int) -> str:
        return f"http://127.0.0.1:{port}"

    # -- identity ----------------------------------------------------------------------------------

    def _ours(self, record: PidRecord) -> bool:
        """The recorded pid is alive and still the process we started (same kernel start time);
        without a recorded start time nothing proves it, so it is not ours."""
        if record.profile != self.profile_id or record.start is None:
            return False
        return self._pid_alive(record.pid) and self._start_time(record.pid) == record.start

    def _token(self) -> str | None:
        try:
            text = self.token_file.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return text or None

    def _forget(self) -> None:
        for path in (self.pidfile, self.token_file):
            with contextlib.suppress(OSError):
                path.unlink()

    async def _healthy(self, port: int, token: str) -> Health:
        try:
            return await self.probe(self.url(port), token)
        except Exception as exc:
            log.debug("whatsapp: bridge probe failed: %s", type(exc).__name__)
            return Health("unreachable")

    # -- lifecycle ---------------------------------------------------------------------------------

    def _check_tools(self) -> tuple[str, Path]:
        if not self.node:
            raise BridgeError(
                "Node.js was not found on PATH; WhatsApp needs it to run its bridge",
                code="whatsapp_node_missing",
                retryable=False,
            )
        if self.script is None or not self.script.is_file():
            raise BridgeError(
                "the WhatsApp bridge script was not found; set platforms.whatsapp.bridge_script",
                code="whatsapp_bridge_missing",
                retryable=False,
            )
        return self.node, self.script

    def _taken(self, port: int) -> BridgeError:
        return BridgeError(
            f"port {port} is held by a process this profile does not own; nothing there was "
            "stopped. Give this profile its own platforms.whatsapp.bridge_port or stop that "
            "process",
            code="whatsapp_bridge_port_taken",
            retryable=False,
        )

    async def ensure(self) -> BridgeEndpoint:
        """This profile's bridge, adopted or freshly started (see the module notes)."""
        node, script = self._check_tools()
        port = bridge_port(
            self.home, self.root, self.profile_id, self.explicit_port, is_free=self._is_free
        )
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.state, 0o700)
        record = PidRecord.read(self.pidfile)
        if record is not None and self._ours(record):
            token = self._token()
            health = await self._healthy(record.port, token) if token else Health("unreachable")
            if record.port == port and health.state == "ok" and health.profile == self.profile_id:
                assert token is not None
                self._adopted = record
                log.info("whatsapp: adopted the running bridge (pid %d)", record.pid)
                return BridgeEndpoint(self.url(port), port, token)
            log.info("whatsapp: replacing this profile's stale bridge (pid %d)", record.pid)
            self._adopted = record
            await self.stop()
        elif record is not None:
            self._forget()
        if not self._is_free(port):
            raise self._taken(port)
        return await self._spawn(node, script, port)

    async def _spawn(self, node: str, script: Path, port: int) -> BridgeEndpoint:
        token = self._token_factory()
        _write_private(self.token_file, token + "\n")
        extra = {
            "INDUS_WA_TOKEN": token,
            "INDUS_WA_HOST": "127.0.0.1",
            "INDUS_WA_PORT": str(port),
            "INDUS_WA_AUTH_DIR": str(self.auth_dir),
            "INDUS_WA_PROFILE": self.profile_id,
            "INDUS_WA_PARENT_PID": str(self._parent_pid()),
            "INDUS_WA_SELF_CHAT": "1" if self.self_chat else "0",
            "INDUS_WA_PROVIDER": self.provider,
        }
        child = await self._spawner([node, str(script)], self.env_for(extra), self.log_file)
        self._child = child
        self._adopted = None
        self.spawns += 1
        record = PidRecord(child.pid, self._start_time(child.pid), port, self.profile_id)
        _write_private(self.pidfile, record.to_json())
        waited = 0.0
        while True:
            if child.returncode is not None:
                code = child.returncode
                self._child = None
                self._forget()
                if code == 2 and not self.provider:
                    # No provider ships with the bridge; it refuses to run without one (exit 2).
                    raise BridgeError(
                        "no WhatsApp provider is configured: set platforms.whatsapp.provider (or "
                        "INDUS_WHATSAPP_PROVIDER) to a module that exports createWaSocket",
                        code="whatsapp_provider_missing",
                        retryable=False,
                    )
                raise BridgeError(
                    f"the WhatsApp bridge exited at start (code {code}); see {self.log_file}",
                    code="whatsapp_bridge_exited",
                    retryable=True,
                )
            health = await self._healthy(port, token)
            if health.state == "ok" and health.profile == self.profile_id:
                log.info("whatsapp: bridge started on port %d (pid %d)", port, child.pid)
                return BridgeEndpoint(self.url(port), port, token)
            if health.state == "unauthorized" or (health.state == "ok" and health.profile):
                await self.stop()
                raise self._taken(port)
            if waited >= SPAWN_WAIT_S:
                await self.stop()
                raise BridgeError(
                    f"the WhatsApp bridge did not answer within {SPAWN_WAIT_S:.0f} s",
                    code="whatsapp_bridge_timeout",
                    retryable=True,
                )
            await self.clock.sleep(PROBE_EVERY_S)
            waited += PROBE_EVERY_S

    async def attach(self) -> BridgeEndpoint:
        """The running bridge of this profile, for a send-only adapter; never starts one (a
        temporary bridge would take this profile's inbound messages)."""
        record = PidRecord.read(self.pidfile)
        token = self._token()
        if record is not None and token and self._ours(record):
            health = await self._healthy(record.port, token)
            if health.state == "ok" and health.profile == self.profile_id:
                return BridgeEndpoint(self.url(record.port), record.port, token)
        raise BridgeError(
            "the WhatsApp bridge of this profile is not running; start the gateway first",
            code="whatsapp_bridge_not_running",
            retryable=False,
        )

    def exited(self) -> bool:
        """The bridge this owner started or adopted is gone."""
        if self._child is not None:
            return self._child.returncode is not None
        if self._adopted is not None:
            return not self._ours(self._adopted)
        return False

    async def restart(self) -> BridgeEndpoint:
        await self.stop()
        return await self.ensure()

    async def stop(self) -> None:
        """End the bridge this owner started or adopted; forget its pidfile and token."""
        child, adopted = self._child, self._adopted
        self._child = self._adopted = None
        if child is None and adopted is None:
            return
        if child is not None:
            started = child

            def gone() -> bool:
                return started.returncode is not None

            def send(sig: int) -> None:
                if isinstance(started, asyncio.subprocess.Process):
                    self._kill(started.pid, sig)
                else:
                    started.send_signal(sig)
        else:
            assert adopted is not None
            record = adopted

            def gone() -> bool:
                return not self._ours(record)

            def send(sig: int) -> None:
                self._kill(record.pid, sig)

        if not gone():
            send(signal.SIGTERM)
            waited = 0.0
            while not gone() and waited < STOP_GRACE_S:
                await self.clock.sleep(0.1)
                waited += 0.1
            if not gone():
                log.warning("whatsapp: the bridge ignored SIGTERM; killing it")
                send(signal.SIGKILL)
        if child is not None and isinstance(child, asyncio.subprocess.Process):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(child.wait(), 2.0)
        self._forget()
