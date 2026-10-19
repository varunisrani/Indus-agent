"""Cron, chat prompts and media intake plugged into a gateway daemon (P15-21, P15-22, P15-23).

``install_extras(daemon)`` is the one call the daemon (P15-24) makes after it has built its
admission pipeline and before it starts accepting events:

- media intake: the pipeline's ``enrich`` hook (``intake.media.MediaIntake``, with the vision probe
  over the pipeline's effective model and the service's side model for descriptions), and the
  album collector as the first front door;
- chat prompts: the pipeline's ``on_request`` hook and a front door (``core.prompts.ChatPrompts``);
- the cron ticker (``cron_host.GatewayCronHost``): started by the daemon's start hooks, stopped
  (30 s drain, lease dropped) by its stop hooks. ``cron=False`` leaves cron to another process.

The daemon runs it as ``ExtrasPart`` (a ``Stage.BACKGROUND`` part named in ``PART_FACTORIES``):
``install_extras`` in the part's ``prepare`` (the pipeline is built, nothing accepts events yet)
through an ``ExtrasSeat``, the start hooks (the cron ticker) when BACKGROUND parts start, the stop
hooks (cron drain, prompts and albums closed) when they stop, before adapters disconnect.
Media downloads: when a platform comes up the part calls the adapter's
``register_fetchers(intake)`` (Telegram ``getFile``, Discord CDN, Slack private files) and the
returned undo when it goes down.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from indus.cron.ticker import DRAIN_S
from indus.foundation.scope import Scope
from indus.gateway.adapters.base import Adapter
from indus.gateway.core.admission import AdmissionPipeline
from indus.gateway.core.prompts import ChatPrompts
from indus.gateway.cron_host import GatewayCronHost
from indus.gateway.daemon.parts import DaemonPart, Stage
from indus.gateway.intake.media import (
    AlbumCollector,
    MediaIntake,
    pipeline_vision_probe,
    vision_describer,
)

if TYPE_CHECKING:
    from indus.gateway.daemon.process import GatewayDaemon

__all__ = [
    "DaemonSeat",
    "Extras",
    "ExtrasPart",
    "ExtrasSeat",
    "attach_fetcher",
    "create",
    "install_extras",
]

log = logging.getLogger(__name__)

type Hook = Callable[[], Awaitable[None]]


class DaemonSeat(Protocol):
    """What ``install_extras`` needs from the daemon."""

    @property
    def scope(self) -> Scope: ...

    @property
    def pipeline(self) -> AdmissionPipeline: ...

    def on_start(self, hook: Hook) -> None: ...

    def on_stop(self, hook: Hook) -> None: ...


@dataclass
class Extras:
    prompts: ChatPrompts
    intake: MediaIntake
    albums: AlbumCollector
    cron: GatewayCronHost | None


def cron_drain_seconds(scope: Scope) -> float:
    """``agent.cron_drain_timeout`` (default 30 s): what a stop gives running cron jobs."""
    from indus.foundation import config

    try:
        view: Any = scope.config if scope.config is not None else config.load(scope)
        value = view.get("agent.cron_drain_timeout")
    except Exception:
        return DRAIN_S
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return DRAIN_S
    return float(value)


def install_extras(daemon: DaemonSeat, *, cron: bool = True) -> Extras:
    """Wire cron, prompts and intake into ``daemon`` (see the module notes)."""
    scope = daemon.scope
    pipeline = daemon.pipeline
    options: Any = getattr(pipeline.service, "options", None)
    aux = getattr(options, "aux", None)
    intake = MediaIntake(
        scope,
        describe=vision_describer(scope, aux),
        sees_images=pipeline_vision_probe(pipeline),
        sent_index=pipeline.sent_index,
    )
    pipeline.enrich = intake.enrich
    albums = AlbumCollector(pipeline.clock, pipeline.accept)
    pipeline.front_doors.insert(0, albums.front_door)
    prompts = ChatPrompts(pipeline)
    undo_prompts = prompts.install()
    host = (
        GatewayCronHost(scope, clock=pipeline.clock, drain_s=cron_drain_seconds(scope))
        if cron
        else None
    )
    if host is not None:
        daemon.on_start(host.start)

    async def stop() -> None:
        try:
            if host is not None:
                await host.stop()
        finally:
            undo_prompts()
            if albums.front_door in pipeline.front_doors:
                pipeline.front_doors.remove(albums.front_door)
            await prompts.aclose()
            await albums.aclose()

    daemon.on_stop(stop)
    return Extras(prompts, intake, albums, host)


def attach_fetcher(intake: MediaIntake, adapter: Adapter) -> Callable[[], None] | None:
    """Let the adapter register its media downloads with the intake (``register_fetchers``);
    the undo, or None when the adapter downloads nothing. A failing adapter is logged, not fatal."""
    try:
        return adapter.register_fetchers(intake)
    except Exception:
        log.warning("%s: registering media downloads failed", adapter.platform, exc_info=True)
        return None


@dataclass
class ExtrasSeat:
    """A ``DaemonSeat``: a scope, a pipeline and the start / stop hooks ``install_extras`` adds."""

    scope: Scope
    pipeline: AdmissionPipeline
    starts: list[Hook] = field(default_factory=list[Hook])
    stops: list[Hook] = field(default_factory=list[Hook])

    def on_start(self, hook: Hook) -> None:
        self.starts.append(hook)

    def on_stop(self, hook: Hook) -> None:
        self.stops.append(hook)

    async def start(self) -> None:
        for hook in self.starts:
            await hook()

    async def stop(self) -> None:
        """Every stop hook runs (reverse order); a failure is logged."""
        for hook in reversed(self.stops):
            try:
                await hook()
            except Exception:
                log.warning("a gateway extras stop hook failed", exc_info=True)


class ExtrasPart(DaemonPart):
    """Cron, chat prompts and media intake in the daemon (see the module notes)."""

    name = "extras"
    stage = Stage.BACKGROUND

    def __init__(self, *, cron: bool = True) -> None:
        self.cron = cron
        self.seat: ExtrasSeat | None = None
        self.extras: Extras | None = None
        self._fetchers: dict[str, Callable[[], None]] = {}
        self._listeners: list[Callable[[], None]] = []

    async def prepare(self, daemon: GatewayDaemon) -> None:
        self.seat = ExtrasSeat(daemon.scope, daemon.pipeline)
        self.extras = install_extras(self.seat, cron=self.cron)
        self._listeners = [
            daemon.on_platform_up(self._platform_up),
            daemon.on_platform_down(self._platform_down),
        ]

    def _platform_up(self, name: str, adapter: Adapter) -> None:
        """The adapter's media downloads feed the intake (P15-23) while the platform is up."""
        if self.extras is None:
            return
        self._platform_down(name, False, "replaced")
        undo = attach_fetcher(self.extras.intake, adapter)
        if undo is not None:
            self._fetchers[name] = undo

    def _platform_down(self, name: str, fatal: bool, reason: str) -> None:
        undo = self._fetchers.pop(name, None)
        if undo is not None:
            undo()

    async def start(self, daemon: GatewayDaemon) -> None:
        if self.seat is not None:
            await self.seat.start()

    async def stop(self, daemon: GatewayDaemon) -> None:
        for undo in self._listeners:
            undo()
        self._listeners = []
        for name in list(self._fetchers):
            self._platform_down(name, False, "stopping")
        seat, self.seat = self.seat, None
        if seat is not None:
            await seat.stop()


def create(_daemon: GatewayDaemon) -> ExtrasPart:
    """``PART_FACTORIES`` entry: the cron ticker runs in the gateway (P15-21)."""
    return ExtrasPart(cron=True)
