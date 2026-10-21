"""``wake.*``: the wake-word methods the desktop and the TUI call (P24-20).

``WAKE_METHODS`` (with the wire models, in ``indus.protocol.wake``, re-exported here) lists the
catalogue rows; ``WakeRpc`` holds the process's ``WakeService`` and answers them, the caller's
connection context being the lease owner. ``register(table)`` adds the handlers to a dispatcher
table (the daemon's WebSocket and the TUI bridge);
``WAKE_DEADLINES`` gives ``wake.start`` its 180 s (a first start may load a model). A detection
reaches the owner as a ``wake.detected`` event (``WakeDetected``) through ``emit``; the surface then
starts its own voice capture (P10), and calls ``wake.resume`` when it is done.

Consent: ``wake.start`` with ``consent: true`` — sent only from an explicit gesture (the Settings
toggle, ``/wake on``) — writes ``wake_word.consent`` and ``wake_word.enabled``; every other start
(an auto-arm at launch) uses what is stored and never opens the microphone without both.
"""

from __future__ import annotations

import base64
import binascii
import threading
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol, cast

from indus.foundation import config
from indus.foundation.blocking import run_blocking
from indus.foundation.scope import Scope
from indus.protocol.base import ProtocolModel
from indus.protocol.errors import INVALID_PARAMS
from indus.protocol.methods import ProtocolError
from indus.protocol.wake import (
    SAMPLE_RATE,
    WAKE_DEADLINES,
    WAKE_DETECTED_EVENT,
    WAKE_METHODS,
    WakeDetected,
    WakeFeedParams,
    WakeFeedResult,
    WakeNoParams,
    WakeOwnerResult,
    WakeStartParams,
    WakeStartResult,
    WakeStatusResult,
    WakeStopParams,
)
from indus.tools.builtin.media.wake_word import (
    WakeError,
    WakeService,
    WakeSettings,
    check_requirements,
)

__all__ = [
    "WAKE_DEADLINES",
    "WAKE_DETECTED_EVENT",
    "WAKE_METHODS",
    "WakeContext",
    "WakeDetected",
    "WakeFeedParams",
    "WakeRpc",
    "WakeStartParams",
    "WakeStartResult",
    "WakeStatusResult",
    "WakeStopParams",
    "settings_for",
]


class WakeContext(Protocol):
    @property
    def scope(self) -> Scope: ...


def settings_for(scope: Scope) -> WakeSettings:
    raw: object = config.load(scope).get("wake_word", {})
    return WakeSettings.from_mapping(
        cast(Mapping[str, Any], raw) if isinstance(raw, Mapping) else {}
    )


class WakeRpc:
    """The ``wake.*`` handlers over one ``WakeService`` per process."""

    def __init__(
        self,
        scope: Scope,
        *,
        emit: Callable[[object, str, ProtocolModel], None],
        service: WakeService | None = None,
    ) -> None:
        self.scope = scope
        self._emit = emit
        self._surfaces: dict[int, str] = {}
        self._lock = threading.Lock()
        self.service = service or WakeService(lambda: settings_for(scope), on_detect=self._detected)

    def _detected(self, owner: object, phrase: str) -> None:
        with self._lock:
            surface = self._surfaces.get(id(owner), "")
        self._emit(owner, WAKE_DETECTED_EVENT, WakeDetected(phrase=phrase, surface=surface))

    async def start(self, ctx: WakeContext, params: WakeStartParams) -> WakeStartResult:
        settings = settings_for(ctx.scope)
        saved = False
        if params.consent:
            available, hint = check_requirements(settings)
            if not available:
                # an "on" that cannot arm stores nothing: settings never say on while nothing runs
                return WakeStartResult(
                    started=False, reason="unavailable", hint=hint, phrase=settings.phrase
                )
            if not (settings.consent and settings.enabled):
                await run_blocking(
                    config.save, ctx.scope, {"wake_word": {"consent": True, "enabled": True}}
                )
                saved = True
        with self._lock:
            self._surfaces[id(ctx)] = params.surface
        outcome = await run_blocking(
            lambda: self.service.start(ctx, params.surface, client_capture=params.client_capture),
            label="wake start",
        )
        return WakeStartResult(
            started=outcome.started,
            reason=outcome.reason,
            hint=outcome.hint or None,
            phrase=outcome.phrase,
            engine=outcome.engine or None,
            capture=outcome.capture if outcome.started else None,
            consent_saved=True if saved else None,
        )

    async def stop(self, ctx: WakeContext, params: WakeStopParams) -> WakeOwnerResult:
        stopped = await run_blocking(self.service.stop, ctx)
        if params.persist:
            await run_blocking(config.save, ctx.scope, {"wake_word": {"enabled": False}})
        return WakeOwnerResult(ok=stopped, reason=None if stopped else "not_owner")

    async def pause(self, ctx: WakeContext, params: WakeNoParams) -> WakeOwnerResult:
        done = await run_blocking(self.service.pause, ctx)
        return WakeOwnerResult(ok=done, reason=None if done else "not_owner")

    async def resume(self, ctx: WakeContext, params: WakeNoParams) -> WakeOwnerResult:
        done = await run_blocking(self.service.resume, ctx)
        return WakeOwnerResult(ok=done, reason=None if done else "not_owner")

    async def status(self, ctx: WakeContext, params: WakeNoParams) -> WakeStatusResult:
        settings = settings_for(ctx.scope)
        available, hint = check_requirements(settings)
        return WakeStatusResult(
            enabled=settings.enabled,
            consent=settings.consent,
            listening=self.service.listening(ctx),
            owner_surface=self.service.owner_surface(),
            phrase=settings.phrase,
            engine=settings.engine,
            available=available,
            hint=hint or None,
        )

    async def feed(self, ctx: WakeContext, params: WakeFeedParams) -> WakeFeedResult:
        if params.sample_rate != SAMPLE_RATE:
            raise ProtocolError(INVALID_PARAMS, "wake.feed takes 16 kHz audio only")
        try:
            pcm = base64.b64decode(params.pcm, validate=True)
        except (binascii.Error, ValueError):
            raise ProtocolError(INVALID_PARAMS, "pcm is not base64") from None
        try:
            fed = await run_blocking(self.service.feed, ctx, pcm)
        except WakeError as exc:
            raise ProtocolError(INVALID_PARAMS, str(exc)) from None
        return WakeFeedResult(fed=fed)

    def connection_closed(self, ctx: object) -> None:
        """Release the lease of a connection that went away."""
        self.service.release_owner(ctx)
        with self._lock:
            self._surfaces.pop(id(ctx), None)

    def handlers(self) -> Mapping[str, Callable[[Any, Any], Awaitable[ProtocolModel]]]:
        return {
            "wake.start": self.start,
            "wake.stop": self.stop,
            "wake.pause": self.pause,
            "wake.resume": self.resume,
            "wake.status": self.status,
            "wake.feed": self.feed,
        }

    def register(self, table: Any) -> None:
        """Add every handler to a ``MethodTable`` (the names must be in the method catalogue)."""
        for name, handler in self.handlers().items():
            table.add(name, handler)
