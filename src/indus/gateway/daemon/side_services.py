"""The daemon's side services from P15-27 and P15-29: the control socket part.

``ControlPart`` is a required ``Stage.CONTROL`` daemon part (``PART_FACTORIES`` names
``create``): its ``start`` calls ``attach`` before any platform connects (a socket that cannot be
bound fails the start, exit 75), ``platform_failed`` is fed from the daemon's
``on_platform_down``, ``started`` runs once the daemon is ready, and its ``stop`` (CONTROL parts
stop last) closes everything. ``DaemonBridge`` shows the ``GatewayDaemon`` through the
``DaemonPorts`` this module needs: ``connect_platform`` answers the daemon's ``PlatformRuntime`` as
a ``ConnectOutcome``, the watcher's status rows become ``PlatformStatus`` rows, and the control
verbs are built from the daemon's ``identity()``, ``status_snapshot()``, ``request_stop`` /
``request_restart``, ``lifecycle.set_draining`` / ``clear_draining`` and the gateway hook folders.

``attach(daemon)`` wires, in order:

1. the control socket (``identify``, ``status`` from ``daemon.runtime_status()``, plus the verbs
   the daemon answers itself: ``drain``, ``reload-plugins``, ``pause-for-update``, the owner-only
   ``stop`` (``{reason, restart}``: the planned-stop record ``indus gateway stop`` writes, then the
   normal shutdown; P18-15's dashboard buttons), the owner-only ``lsp-restart`` / ``lsp-release``
   (``service.lsp_control``, wave 45: the profile's language servers), and the attach
   verbs ``describe`` / ``session-ticket`` of P17-11 over a ticket book made for this daemon start);
   a socket that cannot be bound raises ``ControlSocketError`` and the startup fails;
2. the reconnect watcher, connecting through ``daemon.connect_platform(name)`` and reporting each
   change through ``daemon.set_platform_status(name, status | None)``; ``/platform`` sees it;
3. the gateway hook folders, fed from the daemon's hooks bus and the service's turn ends, and
   answering ``command:<name>`` for the chat commands when nothing else does.

Then the daemon calls ``started(platforms)`` once its adapters are up (``gateway:startup``),
``platform_failed(name, ...)`` for each platform that failed to connect, and ``stop()`` on the
way out.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from indus.foundation import config
from indus.foundation.blocking import run_blocking
from indus.foundation.scope import Scope, scope_or_none
from indus.gateway.control_socket import ControlServer, Handler, identity_payload
from indus.gateway.daemon.attach_verbs import AttachVerbs
from indus.gateway.daemon.bridge_verbs import BridgeVerbs
from indus.gateway.daemon.parts import DaemonPart, Stage
from indus.gateway.daemon.reconnect import ConnectOutcome, PlatformHealth, ReconnectWatcher
from indus.gateway.hooks import GatewayHooks, bridge
from indus.service.lsp_control import LspVerbs
from indus.service.maintenance_gate import MaintenanceVerb
from indus.service.ws_auth import TicketBook
from indus.state.gateway_runtime import PlatformStatus, record_planned_stop

if TYPE_CHECKING:
    from indus.gateway.adapters.base import Adapter
    from indus.gateway.core.admission import AdmissionPipeline
    from indus.gateway.daemon.process import GatewayDaemon, PlatformRuntime
    from indus.plugins.hooks import HookBus

__all__ = [
    "DEFAULT_ATTENTION_AFTER_S",
    "ControlPart",
    "DaemonBridge",
    "DaemonPorts",
    "SideServices",
    "attach",
    "create",
    "outcome_of",
]

log = logging.getLogger(__name__)

DEFAULT_ATTENTION_AFTER_S = 1800.0


class DaemonPorts(Protocol):
    """What the side services need from the daemon (P15-24 provides it)."""

    @property
    def scope(self) -> Scope: ...

    @property
    def pipeline(self) -> AdmissionPipeline | None: ...

    @property
    def hook_bus(self) -> HookBus | None: ...

    def runtime_status(self) -> Mapping[str, Any]: ...

    def identity(self) -> Mapping[str, Any]:
        """The ``identify`` answer (pid, start time, home, profile, ...)."""
        ...

    def control_handlers(self) -> Mapping[str, tuple[Handler, float]]:
        """Extra control verbs: ``{verb: (handler, seconds allowed)}``."""
        ...

    async def connect_platform(self, name: str) -> ConnectOutcome: ...

    def set_platform_status(self, name: str, status: Mapping[str, Any] | None) -> None: ...


def attention_after(scope: Scope) -> float:
    """``agent.reconnect_attention_after`` read now (a config edit applies without a restart)."""
    try:
        value = config.load(scope).get("agent.reconnect_attention_after", None)
    except Exception:
        return DEFAULT_ATTENTION_AFTER_S
    if isinstance(value, bool) or not isinstance(value, int | float):
        return DEFAULT_ATTENTION_AFTER_S
    return float(value)


def _model_of(pipeline: AdmissionPipeline) -> Callable[[str], tuple[str, str]]:
    def model(session_id: str) -> tuple[str, str]:
        state = pipeline.service.agent(session_id).binding.state
        return str(state.model), str(state.provider)

    return model


@dataclass
class SideServices:
    control: ControlServer
    watcher: ReconnectWatcher
    hooks: GatewayHooks
    scope: Scope
    _undo: list[Callable[[], None]] = field(default_factory=list[Callable[[], None]])

    def platform_failed(
        self,
        name: str,
        error: str = "",
        *,
        error_code: str = "",
        retryable: bool = True,
        lost: bool = False,
    ) -> None:
        """A platform did not connect at startup, or was ``lost`` while running (then it is
        tried again at once): queue it for the watcher."""
        self.watcher.enqueue(
            name, error=error, error_code=error_code, retryable=retryable, immediate=lost
        )

    async def started(self, platforms: Sequence[str]) -> None:
        """The adapters are up: fire ``gateway:startup``."""
        await self.hooks.emit(
            "gateway:startup",
            {
                "platforms": list(platforms),
                "profile": self.scope.profile_id,
                "pid": os.getpid(),
            },
        )

    async def stop(self) -> None:
        for undo in reversed(self._undo):
            try:
                undo()
            except Exception:
                log.debug("side service undo failed", exc_info=True)
        self._undo.clear()
        await self.watcher.stop()
        await self.control.stop()


async def attach(daemon: DaemonPorts) -> SideServices:
    """See the module notes."""
    scope = daemon.scope
    control = ControlServer(
        scope.home, identity=daemon.identity, status=daemon.runtime_status, env=scope.env
    )
    for verb, (handler, seconds) in daemon.control_handlers().items():
        control.register(verb, handler, timeout=seconds)
    await control.start()

    def changed(name: str, health: PlatformHealth | None) -> None:
        daemon.set_platform_status(name, health.as_status() if health is not None else None)

    watcher = ReconnectWatcher(
        daemon.connect_platform,
        clock=scope.clock,
        attention_after=lambda: attention_after(scope),
        on_change=changed,
    )
    watcher.start()
    hooks = GatewayHooks(lambda: (scope_or_none() or scope).home)
    services = SideServices(control, watcher, hooks, scope)

    pipeline = daemon.pipeline
    if pipeline is not None:
        pipeline.reconnect = watcher
        if pipeline.commands.command_hooks is None:
            pipeline.commands.command_hooks = hooks.command_hooks
    bus = daemon.hook_bus
    if bus is not None:
        services._undo.append(
            bridge(
                hooks,
                bus,
                session_of=pipeline.cached_session if pipeline is not None else lambda _k: None,
                on_turn_end=pipeline.service.on_turn_end if pipeline is not None else None,
                model_of=_model_of(pipeline) if pipeline is not None else None,
            )
        )
    return services


# -- the daemon part ------------------------------------------------------------------------------

_DRAIN_S = 5.0


def outcome_of(row: PlatformRuntime) -> ConnectOutcome:
    """The daemon's platform row after a connect, as the watcher reads it."""
    error, code = row.error or "", row.error_code or ""
    match row.state:
        case "connected":
            return ConnectOutcome.connected()
        case "fatal":
            return ConnectOutcome.fatal(error, code)
        case _:
            return ConnectOutcome.retryable(error, code)


def _label(value: object) -> str:
    """A short plain label from a caller (letters, digits, ``-``/``_``; at most 32)."""
    if not isinstance(value, str):
        return ""
    return "".join(c for c in value if c.isascii() and (c.isalnum() or c in "-_"))[:32]


def _flag(params: Mapping[str, Any], key: str, default: bool) -> bool:
    value = params.get(key, default)
    return value if isinstance(value, bool) else default


class _HostView:
    """The daemon's session host as ``AttachHost`` reads it: empty until the host is open."""

    def __init__(self, daemon: GatewayDaemon) -> None:
        self._daemon = daemon

    def creatable_sources(self) -> Sequence[str]:
        host = getattr(self._daemon, "host", None)
        return host.creatable_sources() if host is not None else ()

    def capabilities(self) -> Sequence[str]:
        host = getattr(self._daemon, "host", None)
        return host.capabilities() if host is not None else ()


class DaemonBridge:
    """``GatewayDaemon`` through ``DaemonPorts`` (see the module notes).

    ``attach_verbs`` holds the P17-11 attach verbs over the daemon's ticket book for this start
    (``daemon.tickets``: its ``instance_id`` is the start's, so tickets of an earlier start are
    useless), the daemon's session host and its WebSocket URL (P17-22). Until the host is open and
    the endpoint listens, ``describe`` answers empty source and capability lists with a null
    ``ws_url``, and ``session-ticket`` answers ``ws_unavailable`` without minting."""

    def __init__(self, daemon: GatewayDaemon, part: ControlPart) -> None:
        self.daemon = daemon
        self.part = part
        book = getattr(daemon, "tickets", None) or TicketBook.for_home(daemon.scope.home)
        ws_url = getattr(daemon, "ws_url", None) or (lambda: None)
        self.attach_verbs = AttachVerbs(
            daemon.scope.home, book, host=_HostView(daemon), ws_url=ws_url
        )

    @property
    def scope(self) -> Scope:
        return self.daemon.scope

    @property
    def pipeline(self) -> AdmissionPipeline | None:
        return getattr(self.daemon, "pipeline", None)

    @property
    def hook_bus(self) -> HookBus | None:
        return self.daemon.hook_bus

    def runtime_status(self) -> Mapping[str, Any]:
        return self.daemon.status_snapshot()

    def identity(self) -> Mapping[str, Any]:
        scope = self.daemon.scope
        return {**identity_payload(scope.home, scope.env), **self.daemon.identity()}

    async def connect_platform(self, name: str) -> ConnectOutcome:
        return outcome_of(await self.daemon.connect_platform(name, is_reconnect=True))

    def set_platform_status(self, name: str, status: Mapping[str, Any] | None) -> None:
        if status is None:
            return  # connected again (the daemon set the row) or forgotten: the row stays
        row = PlatformStatus.from_json(dict(status))
        if row is not None:
            self.daemon.set_platform_status(name, row)

    def control_handlers(self) -> Mapping[str, tuple[Handler, float]]:
        # ``session-ticket`` is owner-only by name (``control_socket.OWNER_ONLY_VERBS``).
        return {
            "drain": (self._drain, _DRAIN_S),
            "pause-for-update": (self._pause_for_update, _DRAIN_S),
            "reload-plugins": (self._reload_plugins, _DRAIN_S),
            "stop": (self._stop, _DRAIN_S),  # owner-only by name
            **self.attach_verbs.handlers(),
            # P17-21: bulk session maintenance runs here, beside the live turns (R09.21).
            **MaintenanceVerb(self.daemon.scope).handlers(),
            # P19-16/17: `indus mcp serve` sends and answers approvals through the daemon.
            **BridgeVerbs(self.daemon).handlers(),
            # Wave 45: `indus lsp restart` and the CLI's worktree removals reach the profile's
            # language servers here (owner-only by name).
            **LspVerbs(self.daemon.scope).handlers(),
            # P24-01: the multiplex host's serve-profile / unserve-profile / rescan-profiles.
            **getattr(self.daemon, "control_verbs", {}),
        }

    # -- verbs (async: they run on the daemon's loop) -------------------------------------------

    def _drain_answer(self) -> dict[str, Any]:
        daemon = self.daemon
        pipeline = self.pipeline
        turns = len(list(pipeline.claims.keys())) if pipeline is not None else 0
        return {
            "draining": daemon.lifecycle.draining,
            "reason": daemon.lifecycle.drain_reason,
            "stopping": daemon.lifecycle.stopping,
            "active_turns": turns,
        }

    def _set_drain(self, on: bool, reason: str) -> None:
        daemon = self.daemon
        lifecycle = daemon.lifecycle
        if lifecycle.stopping:
            return
        if on and lifecycle.drain_reason is None:
            lifecycle.set_draining(reason)
            daemon.status.set_state("draining")
        elif not on and lifecycle.drain_reason == reason:
            lifecycle.clear_draining()
            daemon.status.set_state(daemon.serving_state())

    async def _drain(self, params: dict[str, Any]) -> Mapping[str, Any]:
        """``{"on": true}`` (default) stops new turns with the maintenance notice; ``false`` ends
        a drain this verb started."""
        self._set_drain(_flag(params, "on", True), "operator")
        return self._drain_answer()

    async def _pause_for_update(self, params: dict[str, Any]) -> Mapping[str, Any]:
        """Stop new turns before an update (``{"resume": true}`` takes them again)."""
        self._set_drain(not _flag(params, "resume", False), "update")
        return self._drain_answer()

    async def _stop(self, params: dict[str, Any]) -> Mapping[str, Any]:
        """Stop the way ``indus gateway stop`` does: write the planned-stop record for this very
        process (``restart: true`` labels it ``restart``, so the next attach starts the daemon
        again; any other stop stays stopped), then the normal drain and shutdown. Answered before
        the shutdown begins; a second call while stopping changes nothing."""
        restart = _flag(params, "restart", False)
        given = params.get("reason")
        reason = "restart" if restart else _label(given) or "control"
        daemon = self.daemon
        if not daemon.lifecycle.stopping:
            process = daemon.process
            await run_blocking(
                lambda: record_planned_stop(
                    daemon.scope.home,
                    process.pid,
                    reason,
                    start_time=process.start_time,
                    by=_label(given) or "control",
                ),
                label="planned stop",
            )
            daemon.request_stop(f"control:{reason}")
        return {"stopping": True, "restart": restart, "reason": reason}

    async def _reload_plugins(self, _params: dict[str, Any]) -> Mapping[str, Any]:
        """Reread the gateway hook folders now; plugin code itself is loaded once per process
        (a restart picks up new plugins)."""
        services = self.part.services
        hooks = services.hooks.reload() if services is not None else []
        plugins = self.daemon.plugins
        host = plugins.host if plugins is not None else None
        return {
            "hooks": sorted(h.name for h in hooks),
            "plugins": sorted(host.provided) if host is not None else [],
            "plugins_reloaded": False,
        }


class ControlPart(DaemonPart):
    """The control socket, the reconnect watcher and the gateway hooks (required)."""

    name = "control"
    stage = Stage.CONTROL
    required = True

    def __init__(self) -> None:
        self.services: SideServices | None = None
        self.bridge: DaemonBridge | None = None
        self._daemon: GatewayDaemon | None = None
        self._seen: set[str] = set()
        self._undo: list[Callable[[], None]] = []

    async def prepare(self, daemon: GatewayDaemon) -> None:
        self._daemon = daemon
        self._undo += [
            daemon.on_platform_up(self._up),
            daemon.on_platform_down(self._down),
            daemon.on_ready(self._ready),
        ]

    async def start(self, daemon: GatewayDaemon) -> None:
        self.bridge = DaemonBridge(daemon, self)
        self.services = await attach(self.bridge)

    def _up(self, name: str, _adapter: Adapter) -> None:
        self._seen.add(name)
        if self.services is not None:
            self.services.watcher.forget(name)

    def _down(self, name: str, fatal: bool, reason: str) -> None:
        daemon, services = self._daemon, self.services
        if daemon is None or services is None or reason == "stopping":
            return
        if daemon.lifecycle.stopping:
            return
        row = daemon.platforms.get(name)
        services.platform_failed(
            name,
            (row.error if row is not None else None) or reason,
            error_code=reason,
            retryable=not fatal,
            lost=name in self._seen,
        )

    async def _ready(self) -> None:
        daemon, services = self._daemon, self.services
        if daemon is not None and services is not None:
            await services.started(sorted(daemon.adapters))

    async def stop(self, daemon: GatewayDaemon) -> None:
        for undo in reversed(self._undo):
            undo()
        self._undo.clear()
        services, self.services = self.services, None
        bridge, self.bridge = self.bridge, None
        try:
            if services is not None:
                try:
                    await services.stop()
                except Exception:
                    log.warning("the control socket did not stop cleanly", exc_info=True)
        finally:
            if bridge is not None:
                bridge.attach_verbs.close()  # every outstanding ticket stops working


def create(_daemon: GatewayDaemon) -> ControlPart:
    """``PART_FACTORIES`` entry."""
    return ControlPart()
