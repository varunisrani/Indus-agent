"""The gateway daemon: assembly and the whole life of one gateway process (P15-24..26).

``GatewayDaemon(scope).run()`` returns the process exit code. The order (also in
``docs/notes/P15/daemon-process.md``, which other features build against):

1. the profile's instance lock and pid record (``liveness``); a live gateway already serving the
   profile → exit 0 at a shell, 75 under a supervisor (``--replace`` takes over instead);
2. status ``starting``; the previous process's death checked (``status.previous_death``); the
   startup watchdog armed;
3. assembly: settings, store, the session service (with its MCP and plugin upkeep; plugin
   platforms join the registry), authorization, pairing, the bot-loop guard, the concurrency cap,
   the outbound senders' shared pieces, the reply port and the admission pipeline;
4. every part's ``prepare``; then the ``CONTROL`` parts start (the control socket);
5. platforms connect, each with its token lock and a timeout, while the intake holds inbound
   events (startup restore);
6. the ``RECOVERY`` parts (ledger sweep), boot recovery (``recovery``), the held events replayed;
7. the ``BACKGROUND`` parts, housekeeping (status heartbeat, drain marker), the loop watchdog,
   systemd ``READY=1``, status ``running``.

Then it waits for a stop or restart (``lifecycle``) and shuts down in reverse.
Exit codes: 0 clean, 75 restart me, 78 fatal configuration (``state.gateway_runtime``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from indus import __version__
from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
from indus.foundation.config import loader as config_loader
from indus.foundation.scope import Scope
from indus.gateway.adapters.base import Adapter, AdapterConnectError
from indus.gateway.config import (
    AdapterConfig,
    GatewaySettings,
    PlatformSettings,
    load_gateway_settings,
    read_secret,
)
from indus.gateway.core.admission import AdmissionPipeline, ModelChoice
from indus.gateway.core.authz import Authorizer, UnauthorizedHandler
from indus.gateway.core.bot_loop import BotLoopGuard, ConcurrencyCap, settings_reader
from indus.gateway.core.sent_index import SentIndex
from indus.gateway.daemon import liveness
from indus.gateway.daemon.command import run_command  # the CLI's "module:attr" target
from indus.gateway.daemon.drain import drain_verdict, machine_epoch
from indus.gateway.daemon.lifecycle import Lifecycle, drain_turns, wait_for_turns
from indus.gateway.daemon.parts import DaemonPart, Stage, load_parts
from indus.gateway.daemon.recovery import BootRecovery, RecoveryReport
from indus.gateway.daemon.status import StatusWriter, append_exit_line, previous_death
from indus.gateway.daemon.watchdogs import (
    Deadline,
    LoopWatchdog,
    SystemdNotifier,
    dump_stacks,
    loop_probe,
)
from indus.gateway.outbound.dead_targets import DeadTargets
from indus.gateway.outbound.media_policy import MediaPolicy
from indus.gateway.outbound.replies import PlatformReplies
from indus.gateway.outbound.sender import OutboundSender
from indus.gateway.registry import PlatformRegistry, proxy_for
from indus.gateway.token_locks import TokenLocks, default_lock_dir
from indus.safety.child_env import FORCE_PREFIX
from indus.state import registry as store_registry
from indus.state.gateway_runtime import (
    EXIT_CONFIG,
    EXIT_OK,
    EXIT_RESTART,
    GATEWAY_GUARD_ENV,
    GatewayState,
    PidRecord,
    PlatformState,
    PlatformStatus,
    ProcessIdentity,
    clear_planned_stop,
    faulthandler_log_path,
    read_planned_stop,
    read_status,
    supervised,
)
from indus.state.pairing import PairingStore
from indus.state.registry import StoreHandle

if TYPE_CHECKING:
    from indus.gateway.http import SurfaceSpec
    from indus.plugins.hooks import HookBus
    from indus.service.automation import SessionAutomation
    from indus.service.mcp_host import McpUpkeep
    from indus.service.plugin_host import PluginUpkeep
    from indus.service.session_service import SessionService

__all__ = [
    "AdapterConfig",
    "BootInfo",
    "DaemonOptions",
    "GatewayDaemon",
    "PlatformRuntime",
    "StartupFailed",
    "build_service",
    "build_surface_service",
    "run_command",
    "run_daemon",
]

log = logging.getLogger(__name__)

type ServiceFactory = Callable[[GatewayDaemon], Awaitable[SessionService]]
type SurfaceFactory = Callable[[GatewayDaemon, SurfaceSpec], Awaitable[SessionService]]
type UpListener = Callable[[str, Adapter], object]
type DownListener = Callable[[str, bool, str], object]
type ReadyListener = Callable[[], object]


_NOT_CONFLICTS = frozenset({"missing_dependency", "adapter_build_failed", "unknown_platform"})
"""Fatal platform states that do not make a start with nothing connected exit 78: a node may
lack one platform's library or plugin and still run the others' cron and API work."""


def _hard_exit(code: int) -> NoReturn:
    os._exit(code)


@dataclass(frozen=True)
class DaemonOptions:
    """Knobs of one daemon run; ``None`` means "the profile's config value"."""

    replace: bool = False
    supervised: bool | None = None
    connect_timeout: float | None = None
    platform_connect_timeouts: Mapping[str, float] = field(
        default_factory=lambda: {"telegram": 45.0}
    )
    restart_drain_timeout: float | None = None
    restart_after_turn_timeout: float | None = None
    systemd_watchdog_seconds: float | None = None
    disconnect_timeout: float = 5.0
    post_interrupt_grace: float = 5.0
    part_stop_timeout: float = 35.0
    heartbeat_interval: float = 60.0
    drain_poll: float = 1.0
    resume_fresh: float = 3600.0
    startup_watchdog: float = 300.0
    shutdown_grace: float = 60.0
    loop_watchdog_interval: float = 30.0
    loop_watchdog_timeout: float = 10.0
    loop_watchdog_strikes: int = 3
    takeover_wait: float = 10.0
    install_signals: bool = True
    watchdogs: bool = True
    export_guard_env: bool = True
    hard_exit: Callable[[int], object] = _hard_exit


@dataclass
class PlatformRuntime:
    """One platform's state as the status file and ``/platform`` show it."""

    name: str
    state: PlatformState = "retrying"
    error_code: str | None = None
    error: str | None = None
    retrying_since: str | None = None
    needs_attention: bool = False

    def as_status(self) -> PlatformStatus:
        return PlatformStatus(
            self.state,
            needs_attention=self.needs_attention,
            error_code=self.error_code,
            error=self.error,
            retrying_since=self.retrying_since,
        )


@dataclass
class BootInfo:
    """What this boot found: the machine epoch, an earlier run, an unclean death."""

    epoch: str = ""
    previous_run: bool = False
    unclean_previous: bool = False
    restart_loop_tripped: bool = False
    recovery: RecoveryReport | None = None


class StartupFailed(Exception):
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


class _DaemonReplies(PlatformReplies):
    """The reply port over the daemon's live sender table (platforms come and go)."""

    def __init__(self, senders: dict[str, OutboundSender]) -> None:
        super().__init__({})
        self._live = senders

    def sender_for(self, platform: str) -> OutboundSender | None:
        return self._live.get(platform.lower())

    async def aclose(self) -> None:
        for sender in list(self._live.values()):
            with contextlib.suppress(Exception):
                await sender.aclose()


def _config_value(scope: Scope, key: str, default: float) -> float:
    try:
        view: Any = scope.config if scope.config is not None else config_loader.load(scope)
        value = view.get(key)
    except Exception:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return float(value) if value >= 0 else default


class GatewayDaemon:
    """One profile's gateway process (see the module notes and the task note's API section)."""

    # Set by assembly (before any part's ``prepare``); reading them earlier raises.
    store: StoreHandle
    service: SessionService
    pipeline: AdmissionPipeline
    replies: PlatformReplies
    authorizer: Authorizer
    dead_targets: DeadTargets
    media: MediaPolicy

    def __init__(
        self,
        scope: Scope,
        *,
        options: DaemonOptions | None = None,
        registry: PlatformRegistry | None = None,
        service_factory: ServiceFactory | None = None,
        parts: Sequence[DaemonPart] | None = None,
        surface_factory: SurfaceFactory | None = None,
    ) -> None:
        self.scope = scope
        self.clock: Clock = scope.clock
        self.options = options or DaemonOptions()
        self.registry = registry if registry is not None else PlatformRegistry()
        self._service_factory: ServiceFactory = service_factory or build_service
        self._surface_factory: SurfaceFactory = surface_factory or build_surface_service
        self._explicit_parts = list(parts) if parts is not None else None
        self.parts: list[DaemonPart] = []
        self.lifecycle = Lifecycle()
        self.process = ProcessIdentity.current()
        self.boot = BootInfo(epoch=machine_epoch())
        self.recovery = BootRecovery(self)
        self.adapters: dict[str, Adapter] = {}
        self.senders: dict[str, OutboundSender] = {}
        self.platforms: dict[str, PlatformRuntime] = {}
        self.sender_options: dict[str, Any] = {}
        self.closers: list[Callable[[], Awaitable[None]]] = []
        self.holder: PidRecord | None = None
        self._up: list[UpListener] = []
        self._down: list[DownListener] = []
        self._ready: list[ReadyListener] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._claim: liveness.InstanceClaim | None = None
        self._started: list[DaemonPart] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_watchdog: LoopWatchdog | None = None
        self.status = StatusWriter(
            scope.home,
            clock=self.clock,
            process=self.process,
            profile=scope.profile_id,
            code_version=__version__,
            epoch=self.boot.epoch,
        )
        self.systemd = SystemdNotifier(scope.env, clock=self.clock)
        self.token_locks = TokenLocks(
            default_lock_dir(scope.env), profile=scope.profile_id, home=str(scope.home)
        )
        # Filled in by assembly.
        self.settings: GatewaySettings = GatewaySettings({}, _empty_section())
        self.plugins: PluginUpkeep | None = None
        self.mcp: McpUpkeep | None = None
        self.hooks: HookBus | None = None
        self.automation: SessionAutomation | None = None
        self.sent_index = SentIndex()
        self._restart_drain = 0.0
        self._after_turn = 1800.0

    # -- small helpers for parts -------------------------------------------------------------------

    @property
    def section(self) -> Any:
        return self.settings.gateway

    @property
    def is_supervised(self) -> bool:
        chosen = self.options.supervised
        return supervised(self.scope.env) if chosen is None else chosen

    async def surface_service(self, spec: SurfaceSpec) -> SessionService:
        """A session service for one HTTP surface (P15-30..32; the HTTP part closes it)."""
        return await self._surface_factory(self, spec)

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
        """A tracked background task (cancelled at shutdown; a failure is logged)."""
        task = asyncio.ensure_future(coro)
        task.set_name(name)
        self._tasks.add(task)

        def done(finished: asyncio.Task[Any]) -> None:
            self._tasks.discard(finished)
            if not finished.cancelled() and finished.exception() is not None:
                log.error("%s failed", name, exc_info=finished.exception())

        task.add_done_callback(done)
        return task

    def on_platform_up(self, listener: UpListener) -> Callable[[], None]:
        self._up.append(listener)
        return lambda: self._up.remove(listener) if listener in self._up else None

    def on_platform_down(self, listener: DownListener) -> Callable[[], None]:
        self._down.append(listener)
        return lambda: self._down.remove(listener) if listener in self._down else None

    def on_ready(self, listener: ReadyListener) -> Callable[[], None]:
        """Called once the startup is complete (platforms connected, recovery done, every part
        started), just before the daemon reports ``running``."""
        self._ready.append(listener)
        return lambda: self._ready.remove(listener) if listener in self._ready else None

    async def _tell(self, listeners: Sequence[Callable[..., object]], *args: Any) -> None:
        for listener in list(listeners):
            try:
                result = listener(*args)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                log.exception("a platform listener failed")

    def identity(self) -> dict[str, Any]:
        """What the control socket's ``identify`` answers."""
        return {
            "pid": self.process.pid,
            "start_time": self.process.start_time,
            "pidns": self.process.pidns,
            "home": str(self.scope.home),
            "profile": self.scope.profile_id,
            "supervised": self.is_supervised,
            "code_version": __version__,
            "epoch": self.boot.epoch,
        }

    def status_snapshot(self) -> dict[str, Any]:
        return self.status.snapshot().to_json()

    # -- names other slices' ports use ----------------------------------------------------------

    @property
    def hook_bus(self) -> HookBus | None:
        return self.hooks

    def runtime_status(self) -> dict[str, Any]:
        return self.status_snapshot()

    def set_platform_status(self, name: str, status: PlatformStatus | None) -> None:
        """Replace (or with None drop) one platform's status row."""
        if status is None:
            self.platforms.pop(name, None)
            self.status.set_platform(name, None)
            return
        row = self.set_platform_state(
            name,
            status.state,
            error_code=status.error_code,
            error=status.error,
            needs_attention=status.needs_attention,
        )
        if status.retrying_since:
            row.retrying_since = status.retrying_since
            self.status.set_platform(name, row.as_status())

    def request_stop(self, reason: str, *, exit_code: int = EXIT_OK) -> None:
        self.lifecycle.request_stop(reason, exit_code=exit_code)

    def request_restart(self, reason: str) -> None:
        self.lifecycle.request_restart(reason)

    def set_platform_state(
        self,
        name: str,
        state: PlatformState,
        *,
        error_code: str | None = None,
        error: str | None = None,
        needs_attention: bool | None = None,
    ) -> PlatformRuntime:
        row = self.platforms.setdefault(name, PlatformRuntime(name))
        if state == "retrying" and row.retrying_since is None:
            row.retrying_since = self.clock.now().astimezone(UTC).isoformat(timespec="seconds")
        elif state != "retrying":
            row.retrying_since = None
        row.state = state
        row.error_code = error_code
        row.error = error
        if needs_attention is not None:
            row.needs_attention = needs_attention
        elif state == "connected":
            row.needs_attention = False
        self.status.set_platform(name, row.as_status())
        if self.status.state in ("running", "degraded") and not self.lifecycle.draining:
            serving = self.serving_state()
            if serving != self.status.state:
                self.status.set_state(serving)
        return row

    # -- settings readers for the intake -----------------------------------------------------------

    def _extra_for(self, platform: str) -> Mapping[str, Any] | None:
        found = self.settings.get(platform)
        return None if found is None else found.extra

    def _home_for(self, platform: str) -> str | None:
        found = self.settings.get(platform)
        if found is None or found.home_channel is None:
            return None
        return found.home_channel.chat_id

    def _channel_model(self, platform: str, chat_id: str) -> ModelChoice | None:
        found = self.settings.get(platform)
        override = None if found is None else found.channel_override(chat_id)
        if override is None or not override.model:
            return None
        return ModelChoice(override.model, override.provider or None, None, "channel")

    # -- the run -----------------------------------------------------------------------------------

    async def run(self) -> int:
        """The whole life of the daemon; returns the exit code."""
        home = self.scope.home
        home.mkdir(parents=True, exist_ok=True)
        record = PidRecord(
            self.process,
            home=str(home),
            profile=self.scope.profile_id,
            started_at=self.clock.now().astimezone(UTC).isoformat(timespec="seconds"),
            code_version=__version__,
            supervised=self.is_supervised,
        )
        claim = await run_blocking(
            lambda: liveness.claim_instance(
                home, record, replace=self.options.replace, wait=self.options.takeover_wait
            ),
            label="gateway instance lock",
        )
        if claim.outcome != "acquired":
            self.holder = claim.holder
            who = f"pid {claim.holder.pid}" if claim.holder is not None else "another process"
            log.warning("a gateway already serves this profile (%s)", who)
            return EXIT_RESTART if self.is_supervised else EXIT_OK
        self._claim = claim
        try:
            return await self._run_owned()
        finally:
            liveness.release_instance(home, claim, self.process)

    async def _run_owned(self) -> int:
        home = self.scope.home
        loop = asyncio.get_running_loop()
        self._loop = loop
        if self.options.export_guard_env:
            # Children (the agent's own terminal) carry the marker through child_env's force
            # entry, so `indus gateway stop/restart` run from inside this gateway can refuse.
            marker = {GATEWAY_GUARD_ENV: "1", FORCE_PREFIX + GATEWAY_GUARD_ENV: "1"}
            self.scope = self.scope.with_env(self.scope.env.with_overlay(marker))
        earlier = read_status(home)
        self.boot.previous_run = earlier is not None
        dead = previous_death(home, self.process)
        self.status.set_state("starting")
        if dead is not None:
            self.boot.unclean_previous = True
            log.warning(
                "the previous gateway (pid %d) died without shutting down (last state %s)",
                dead.pid,
                dead.state,
            )
            append_exit_line(
                home,
                {
                    "event": "previous_unclean_exit",
                    "pid": dead.pid,
                    "state": dead.state,
                    "updated_at": dead.updated_at,
                    "seen_by": self.process.pid,
                },
            )
            self.status.set_previous_exit("unclean")
        if self.options.install_signals:
            self.lifecycle.install_signals(loop, on_dump=self._dump_now)
        startup = (
            Deadline(self.options.startup_watchdog, self._startup_timeout, name="startup watchdog")
            if self.options.watchdogs
            else None
        )
        if startup is not None:
            startup.start()
        try:
            try:
                await self._start()
            except Exception as exc:
                failed = (
                    exc
                    if isinstance(exc, StartupFailed)
                    else StartupFailed(EXIT_RESTART, f"{type(exc).__name__}: {exc}")
                )
                if startup is not None:
                    startup.cancel()
                log.error("the gateway could not start: %s", failed.reason, exc_info=exc)
                await self._stop_everything()
                self.status.set_state(
                    "startup_failed", exit_reason=failed.reason, exit_code=failed.code
                )
                return failed.code
            if startup is not None:
                startup.cancel()
            if not self.lifecycle.stopping:
                self._running()
                await self.lifecycle.wait_requested()
            return await self._shutdown()
        finally:
            if self.options.install_signals:
                self.lifecycle.remove_signals(loop)

    def _running(self) -> None:
        self.status.set_state(self.serving_state())
        self.spawn(self._housekeeping(), name="gateway housekeeping")
        self.spawn(self._drain_watcher(), name="gateway drain watcher")
        if self.options.watchdogs and self._loop is not None:
            loop = self._loop
            timeout = self.options.loop_watchdog_timeout
            self._loop_watchdog = LoopWatchdog(
                lambda: loop_probe(loop, timeout),
                self._loop_wedged,
                interval=self.options.loop_watchdog_interval,
                strikes=self.options.loop_watchdog_strikes,
            )
            self._loop_watchdog.start()
        self.systemd.ready("running")
        log.info(
            "gateway running: %s",
            ", ".join(sorted(self.adapters)) or "no messaging platform connected",
        )

    # -- startup -----------------------------------------------------------------------------------

    async def _start(self) -> None:
        await self._assemble()
        self.parts = (
            list(self._explicit_parts) if self._explicit_parts is not None else load_parts(self)
        )
        for part in self.parts:
            try:
                await part.prepare(self)
            except Exception as exc:
                if part.required:
                    raise StartupFailed(EXIT_RESTART, f"{part.name}: {exc}") from exc
                log.exception("gateway part %s failed to prepare", part.name)
        await self._start_stage(Stage.CONTROL)
        if self.lifecycle.stopping:
            return
        await self._connect_all()
        if self.lifecycle.stopping:
            return
        await self._start_stage(Stage.RECOVERY)
        try:
            self.boot.recovery = await self.recovery.run()
            self.boot.restart_loop_tripped = self.boot.recovery.guard_tripped
        except Exception:
            log.exception("boot recovery failed; continuing")
        await self.pipeline.finish_restore()
        await self._start_stage(Stage.BACKGROUND)
        await self._tell(self._ready)

    async def _start_stage(self, stage: Stage) -> None:
        for part in self.parts:
            if part.stage is not stage:
                continue
            try:
                await part.start(self)
            except Exception as exc:
                if part.required:
                    raise StartupFailed(EXIT_RESTART, f"{part.name}: {exc}") from exc
                log.exception("gateway part %s failed to start", part.name)
                continue
            self._started.append(part)

    async def _assemble(self) -> None:
        scope = self.scope
        self.settings = await run_blocking(load_gateway_settings, scope, label="gateway settings")
        for problem in self.settings.problems:
            log.warning("gateway config: %s", problem)
        restart_drain = self.options.restart_drain_timeout
        if restart_drain is None:
            restart_drain = _config_value(scope, "agent.restart_drain_timeout", 0.0)
        after_turn = self.options.restart_after_turn_timeout
        if after_turn is None:
            after_turn = _config_value(scope, "agent.restart_after_turn_timeout", 1800.0)
        self._restart_drain = restart_drain
        self._after_turn = after_turn
        watchdog = self.options.systemd_watchdog_seconds
        if watchdog is None:
            watchdog = _config_value(scope, "gateway.systemd_watchdog_seconds", 0.0)
        self.systemd = SystemdNotifier(scope.env, clock=self.clock, watchdog_seconds=watchdog)
        self.store = await run_blocking(store_registry.acquire, scope, label="session store")
        service = await self._service_factory(self)
        self.service = service
        self.plugins = service.plugins
        self.mcp = service.mcp
        if self.plugins is not None:
            await self.plugins.start()
            host = self.plugins.host
            if host is not None:
                added = self.registry.load_plugin_entries(host.providers)
                if added:
                    log.info("%d platform(s) offered by plugins", added)
            self.hooks = self.plugins.bus
        from indus.service.automation import SessionAutomation

        self.automation = SessionAutomation(service)
        self.automation.start()
        view: Any = scope.config if scope.config is not None else config_loader.load(scope)
        section = self.settings.gateway
        bot_guard = BotLoopGuard(settings_reader(view.get))
        pairing = PairingStore(scope)
        self.authorizer = Authorizer.from_registry(
            self.registry,
            env=lambda name: read_secret(scope, name),
            extra_for=self._extra_for,
            adapter_for=self.adapters.get,
            is_paired=pairing.is_approved,
            global_unknown_dm=lambda: section.unauthorized_dm_behavior,
            bot_guard=bot_guard,
            profile=scope.profile_id,
        )
        self.dead_targets = DeadTargets.for_scope(scope)
        self.media = MediaPolicy.for_scope(scope, section)
        self.replies = _DaemonReplies(self.senders)
        self.pipeline = AdmissionPipeline(
            scope=scope,
            service=service,
            sender=self.replies,
            store=self.store,
            authorizer=self.authorizer,
            unauthorized=UnauthorizedHandler(self.authorizer, pairing),
            bot_guard=bot_guard,
            cap=ConcurrencyCap(lambda: section.max_concurrent_sessions),
            hooks=self.hooks,
            paused=self.lifecycle.notice,
            interceptors=[self.recovery.intercept],
            sent_index=self.sent_index,
            extra_for=self._extra_for,
            channel_model=self._channel_model,
            home_for=self._home_for,
            automation=self.automation,
        )

    def make_sender(self, name: str, adapter: Adapter) -> OutboundSender:
        """The platform's outbound sender (``sender_options`` from parts are passed through)."""
        found = self.settings.get(name)
        mode: Any = found.reply_to_mode if found is not None else "first"
        return OutboundSender(
            adapter,
            scope=self.scope,
            platform=name,
            dead_targets=self.dead_targets,
            sent_index=self.sent_index,
            media=self.media,
            reply_to_mode=mode,
            ephemeral_ttl=float(self.settings.display.ephemeral_system_ttl),
            **self.sender_options,
        )

    # -- platforms ---------------------------------------------------------------------------------

    async def _connect_all(self) -> None:
        self.pipeline.begin_restore()
        self.pipeline.start()
        enabled = self.settings.enabled()
        for name in enabled:
            self.set_platform_state(name, "retrying", error_code="starting")
        await asyncio.gather(*(self._connect(name, startup=True) for name in enabled))
        connected = [n for n in enabled if self.platforms[n].state == "connected"]
        if connected:
            return
        if not enabled:
            if self.settings.problems:
                raise StartupFailed(
                    EXIT_CONFIG, "no messaging platform is enabled and the config has errors"
                )
            log.warning("no messaging platform is enabled; only the background parts run")
            return
        retrying = [n for n in enabled if self.platforms[n].state == "retrying"]
        conflicts = [
            n
            for n in enabled
            if self.platforms[n].state == "fatal"
            and self.platforms[n].error_code not in _NOT_CONFLICTS
        ]
        if conflicts and not retrying:
            codes = sorted({self.platforms[n].error_code or "fatal" for n in conflicts})
            raise StartupFailed(EXIT_CONFIG, "no platform could start: " + ", ".join(codes))
        log.warning(
            "no platform connected (%s); the gateway keeps running for the background parts",
            ", ".join(f"{n}: {self.platforms[n].error_code}" for n in enabled),
        )

    def serving_state(self) -> GatewayState:
        """``degraded`` while a platform is parked as fatal (it never heals on its own)."""
        if any(row.state == "fatal" for row in self.platforms.values()):
            return "degraded"
        return "running"

    def _credential(self, settings: PlatformSettings) -> str:
        return "\n".join(settings.credentials[k] for k in sorted(settings.credentials))

    async def connect_platform(self, name: str, *, is_reconnect: bool = True) -> PlatformRuntime:
        """(Re)connect one platform: the reconnect watcher's entry point (W4, P15-29)."""
        await self._connect(name, startup=not is_reconnect)
        return self.platforms[name]

    async def _connect(self, name: str, *, startup: bool) -> None:
        if name in self.adapters:
            return
        settings = self.settings.get(name)
        entry = self.registry.get(name)
        if settings is None or entry is None:
            await self._failed(name, "fatal", "unknown_platform", f"no platform named {name}")
            return
        missing = self.registry.missing_deps(name)
        if missing is not None:
            await self._failed(name, "fatal", "missing_dependency", missing)
            return
        credential = self._credential(settings)
        if credential:
            outcome = await run_blocking(
                lambda: self.token_locks.acquire(name, credential, startup=startup),
                label="token lock",
            )
            if not outcome.acquired:
                state: PlatformState = "fatal" if outcome.fatal else "retrying"
                await self._failed(name, state, "token_conflict", outcome.message)
                return
        proxy = proxy_for(
            env=self.scope.env,
            trust_env=bool(self.settings.gateway.trust_env),
            platform_proxy=str(settings.extra.get("proxy") or "") or None,
        )
        config = AdapterConfig(name, settings, self.scope, self.clock, proxy)
        adapter = self.registry.create_adapter(name, config)
        if not isinstance(adapter, Adapter):
            if credential:
                self.token_locks.release(name, credential)
            await self._failed(
                name, "fatal", "adapter_build_failed", "the adapter could not be built"
            )
            return
        timeout = self.options.platform_connect_timeouts.get(name)
        if timeout is None:
            timeout = self.options.connect_timeout
        if timeout is None:
            timeout = _config_value(self.scope, "gateway.connect_timeout_seconds", 30.0)
        try:
            await asyncio.wait_for(adapter.connect(is_reconnect=not startup), timeout)
        except TimeoutError:
            with contextlib.suppress(Exception):
                await adapter.disconnect()
            await self._failed(
                name, "retrying", "connect_timeout", f"no connection within {timeout:g} s"
            )
            return
        except AdapterConnectError as exc:
            if not exc.retryable and credential:
                self.token_locks.release(name, credential)
            state = "retrying" if exc.retryable else "fatal"
            await self._failed(name, state, exc.code, str(exc))
            return
        except Exception as exc:
            await self._failed(name, "retrying", "connect_failed", adapter.scrub(str(exc)))
            return
        self.adapters[name] = adapter
        self.pipeline.adapters[name] = adapter
        adapter.set_sink(self.pipeline.accept)
        self.senders[name] = self.make_sender(name, adapter)
        self.set_platform_state(name, "connected")
        log.info("platform %s connected", name)
        await self._tell(self._up, name, adapter)

    async def _failed(self, name: str, state: PlatformState, code: str, message: str) -> None:
        log.warning("platform %s %s: %s", name, state, message)
        self.set_platform_state(name, state, error_code=code, error=message)
        await self._tell(self._down, name, state == "fatal", code)

    async def disconnect_platform(self, name: str, *, fatal: bool, reason: str) -> None:
        """Take one platform down (fatal: it is not retried and its token lock is released)."""
        adapter = self.adapters.pop(name, None)
        pipeline: AdmissionPipeline | None = getattr(self, "pipeline", None)
        if pipeline is not None:
            pipeline.adapters.pop(name, None)
        sender = self.senders.pop(name, None)
        if sender is not None:
            with contextlib.suppress(Exception):
                await sender.aclose()
        if adapter is not None:
            adapter.set_sink(None)
            try:
                await asyncio.wait_for(adapter.disconnect(), self.options.disconnect_timeout)
            except Exception:
                log.warning("platform %s did not disconnect cleanly", name, exc_info=True)
        settings = self.settings.get(name)
        if fatal and settings is not None and (credential := self._credential(settings)):
            self.token_locks.release(name, credential)
        if reason != "stopping":
            self.set_platform_state(name, "fatal" if fatal else "retrying", error_code=reason)
        await self._tell(self._down, name, fatal, reason)

    # -- housekeeping ------------------------------------------------------------------------------

    async def _housekeeping(self) -> None:
        while True:
            await self.clock.sleep(self.options.heartbeat_interval)
            await run_blocking(self.status.heartbeat, label="status heartbeat")

    async def _drain_watcher(self) -> None:
        home = self.scope.home
        while True:
            verdict = await run_blocking(
                lambda: drain_verdict(home, now=self.clock.now(), epoch=self.boot.epoch),
                label="drain marker",
            )
            lifecycle = self.lifecycle
            if not lifecycle.stopping:
                if verdict.active and lifecycle.drain_reason is None:
                    log.info("external drain requested (%s): not starting new turns", verdict.why)
                    lifecycle.set_draining("maintenance")
                    self.status.set_state("draining")
                elif not verdict.active and lifecycle.drain_reason == "maintenance":
                    log.info("external drain cleared: taking new turns again")
                    lifecycle.clear_draining()
                    self.status.set_state(self.serving_state())
            await self.clock.sleep(self.options.drain_poll)

    # -- watchdog exits (called from threads) ------------------------------------------------------

    def _dump_now(self) -> None:
        dump_stacks(faulthandler_log_path(self.scope.home), "SIGUSR2", loop=self._loop)

    def _startup_timeout(self) -> None:
        dump_stacks(faulthandler_log_path(self.scope.home), "startup watchdog", loop=self._loop)
        with contextlib.suppress(Exception):
            self.status.set_state(
                "startup_failed", exit_reason="startup_watchdog", exit_code=EXIT_RESTART
            )
        self.options.hard_exit(EXIT_RESTART)

    def _loop_wedged(self) -> None:
        dump_stacks(faulthandler_log_path(self.scope.home), "loop watchdog", loop=self._loop)
        with contextlib.suppress(Exception):
            self.status.set_state("degraded", exit_reason="loop_watchdog", exit_code=EXIT_RESTART)
        self.options.hard_exit(EXIT_RESTART)

    def _shutdown_timeout(self) -> None:
        dump_stacks(faulthandler_log_path(self.scope.home), "shutdown watchdog", loop=self._loop)
        code = self.lifecycle.exit_code
        with contextlib.suppress(Exception):
            self.status.set_state("stopped", exit_reason="shutdown_watchdog", exit_code=code)
        self.options.hard_exit(code)

    # -- shutdown ----------------------------------------------------------------------------------

    async def _shutdown(self) -> int:
        lifecycle = self.lifecycle
        if lifecycle.mode == "run":
            lifecycle.request_stop("stop")
        reason = lifecycle.reason
        planned = await run_blocking(read_planned_stop, self.scope.home, label="planned stop")
        if planned is not None and planned.matches(self.process.pid, self.process.start_time):
            reason = f"planned_stop:{planned.reason}"
            await run_blocking(clear_planned_stop, self.scope.home, label="planned stop")
        self.status.set_state("draining")
        budget = self._restart_drain + self.options.post_interrupt_grace
        if lifecycle.mode == "restart":
            budget += self._after_turn
        deadline = None
        if self.options.watchdogs:
            deadline = Deadline(
                budget + self.options.shutdown_grace,
                self._shutdown_timeout,
                name="shutdown watchdog",
            ).start()
        try:
            built = getattr(self, "pipeline", None) is not None
            if lifecycle.mode == "restart" and built:
                log.info("restart requested: waiting for running turns to finish")
                await wait_for_turns(self, self._after_turn)
            if built:
                await drain_turns(
                    self,
                    drain_timeout=self._restart_drain,
                    grace=self.options.post_interrupt_grace,
                )
            self.status.set_state("stopping")
            await self._stop_everything()
            code = lifecycle.exit_code
            self.status.set_state("stopped", exit_reason=reason, exit_code=code)
            log.info("gateway stopped (%s, exit %d)", reason, code)
            return code
        finally:
            if deadline is not None:
                deadline.cancel()

    async def _stop_part(self, part: DaemonPart) -> None:
        try:
            await asyncio.wait_for(part.stop(self), self.options.part_stop_timeout)
        except Exception:
            log.exception("gateway part %s did not stop cleanly", part.name)

    async def _stop_everything(self) -> None:
        """Reverse of the start, for whatever was built (also after a failed start)."""
        await self.systemd.stopping()
        if self._loop_watchdog is not None:
            self._loop_watchdog.stop()
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        started = list(self._started)
        self._started.clear()
        for part in reversed([p for p in started if p.stage is Stage.BACKGROUND]):
            await self._stop_part(part)
        for name in list(self.adapters):
            await self.disconnect_platform(name, fatal=False, reason="stopping")
        for part in reversed([p for p in started if p.stage is Stage.RECOVERY]):
            await self._stop_part(part)
        pipeline: AdmissionPipeline | None = getattr(self, "pipeline", None)
        if pipeline is not None:
            with contextlib.suppress(Exception):
                await pipeline.aclose()
        replies: PlatformReplies | None = getattr(self, "replies", None)
        if replies is not None:
            with contextlib.suppress(Exception):
                await replies.aclose()
        if self.automation is not None:
            with contextlib.suppress(Exception):
                await self.automation.stop()
        service: SessionService | None = getattr(self, "service", None)
        if service is not None:
            try:
                await service.aclose()
            except Exception:
                log.warning("the session service did not close cleanly", exc_info=True)
        for closer in reversed(self.closers):
            with contextlib.suppress(Exception):
                await closer()
        for part in reversed([p for p in started if p.stage is Stage.CONTROL]):
            await self._stop_part(part)
        with contextlib.suppress(Exception):
            await run_blocking(self.token_locks.release_all, label="token locks")
        store: StoreHandle | None = getattr(self, "store", None)
        if store is not None:
            with contextlib.suppress(Exception):
                store_registry.release(store)


def _empty_section() -> Any:
    from indus.foundation.config.schema import GatewaySection

    return GatewaySection()


# -- the real session service ----------------------------------------------------------------------


async def build_service(daemon: GatewayDaemon) -> SessionService:
    """The profile's session service for chat turns (lean host wiring, like the TUI bridge)."""
    return await _session_service(daemon, platform="gateway", owner="gateway", chat=True)


async def build_surface_service(daemon: GatewayDaemon, spec: SurfaceSpec) -> SessionService:
    """The session service of one HTTP surface (P15-30..32): the spec's toolsets, platform and
    answerable request kinds; MCP servers for the API server only, never plugins (one plugin host
    per profile, the chat service's); an ``untrusted`` surface (webhooks) gets neither."""
    return await _session_service(
        daemon,
        platform=spec.platform,
        owner=f"gateway-{spec.name}",
        chat=False,
        toolsets=list(spec.toolsets),
        answerable=spec.answerable_kinds,
        mcp=not spec.untrusted,
    )


async def _session_service(
    daemon: GatewayDaemon,
    *,
    platform: str,
    owner: str,
    chat: bool,
    toolsets: list[str] | None = None,
    answerable: tuple[str, ...] = (),
    mcp: bool = True,
) -> SessionService:
    from dataclasses import replace

    from indus.core.agent import Agent, AgentOptions
    from indus.core.persist import StateStore
    from indus.core.turn_state import LoopOptions
    from indus.providers.auxiliary import AuxClient
    from indus.providers.route import resolve
    from indus.service import terminal_host
    from indus.service.code_host import KernelUpkeep
    from indus.service.mcp_host import McpUpkeep
    from indus.service.plugin_host import PluginUpkeep
    from indus.service.session_service import SessionService
    from indus.tools.builtin import load_builtins, terminal
    from indus.tools.registry import ToolRegistry

    scope = daemon.scope
    handle = daemon.store
    route = await run_blocking(resolve, scope, label="model route")
    store = StateStore(handle)
    tools = ToolRegistry(clock=scope.clock)
    load_builtins(tools)
    aux = AuxClient(scope, route)
    cwd = Path.cwd()
    options = AgentOptions(
        platform=platform,
        source=platform,
        cwd=cwd,
        loop=LoopOptions.from_scope(scope, platform=platform),
        session_fields={"cwd": str(cwd), "model": route.model},
        store=store,
        registry=tools,
    )

    async def factory(
        session_id: str, sink: Any, broker: Any, opts: AgentOptions, selection: Any
    ) -> Agent:
        chosen = route
        model = opts.session_fields.get("model")
        if isinstance(model, str) and model and model != route.model:
            chosen = replace(route, model=model)
        full = replace(opts, session_id=session_id, store=store, broker=broker)
        agent = await Agent.start(scope, chosen, selection, full, sink)
        agent.runtime.environments = terminal.runtime().environments
        agent.runtime.run_context = "interactive"  # a person may be watching the chat
        return agent

    if chat:
        terminal_host.install(handle, terminal.runtime().processes)

        async def shut_terminal() -> None:
            await terminal.runtime().shutdown()

        daemon.closers.append(shut_terminal)
    return SessionService(
        scope,
        route,
        options=options,
        store=store,
        owner=owner,
        agent_factory=factory,
        aux=aux,
        toolsets=toolsets,
        answerable_kinds=answerable,
        kernels=KernelUpkeep(scope, reaper=chat),
        mcp=McpUpkeep(scope, tools, aux=aux, watch=False) if mcp else None,
        plugins=PluginUpkeep(scope, tools, surface="gateway", interactive=False) if chat else None,
    )


async def run_daemon(
    scope: Scope,
    *,
    options: DaemonOptions | None = None,
    registry: PlatformRegistry | None = None,
    service_factory: ServiceFactory | None = None,
    parts: Sequence[DaemonPart] | None = None,
) -> int:
    """Run one gateway for ``scope`` until it stops; the exit code."""
    daemon = GatewayDaemon(
        scope,
        options=options,
        registry=registry,
        service_factory=service_factory,
        parts=parts,
    )
    return await daemon.run()
