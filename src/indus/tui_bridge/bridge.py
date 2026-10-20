"""What the bridge's handlers share for one client connection (P12-28..31).

``Bridge`` holds the process scope, the frame emitter, the session service (built on first use, so
a profile without a usable provider still answers listings, completion and settings), the store
handle, one ``SessionView`` per session the client touched, the server-request hub and the event
projector. Everything runs on the bridge's event loop.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from indus.core.persist import StateStore
from indus.foundation.clock import Clock
from indus.foundation.scope import Scope
from indus.protocol.errors import INTERNAL_ERROR, PROVIDER_UNAVAILABLE
from indus.protocol.methods import ProtocolError
from indus.service.rpc.dispatch import ContractViolation
from indus.service.session_service import SessionService, protocol_error_for
from indus.state.registry import StoreHandle

__all__ = [
    "TOOL_MODES",
    "Bridge",
    "ContractViolation",
    "Emitter",
    "ServiceFactory",
    "SessionView",
    "ToolMode",
]

log = logging.getLogger(__name__)

type ToolMode = Literal["off", "new", "all", "verbose"]
TOOL_MODES: tuple[ToolMode, ...] = ("off", "new", "all", "verbose")


class Emitter(Protocol):
    """Writes one notification frame; False when the client is gone."""

    def notify(self, method: str, params: Mapping[str, Any]) -> bool: ...


type ServiceFactory = Callable[[Bridge], SessionService | Awaitable[SessionService]]
"""Builds the session service for a bridge (it wires the bridge's request port into agents)."""


@dataclass
class SessionView:
    """What the bridge keeps about one session the client uses."""

    session_id: str
    tool_mode: ToolMode = "all"
    queue: deque[str] = field(default_factory=deque[str])
    images: list[Path] = field(default_factory=list[Path])
    unsubscribe: list[Callable[[], None]] = field(default_factory=list[Callable[[], None]])
    turn_id: str | None = None
    last_tool: str | None = None
    hidden_calls: set[str] = field(default_factory=set[str])
    reasons: dict[str, str] = field(default_factory=dict[str, str])
    status: str | None = None
    title: str | None = None
    pending_title: str | None = None
    pending_model: str | None = None

    @property
    def compressing(self) -> bool:
        return self.status == "compressing"


class Bridge:
    """Shared state of one connection (see the module notes)."""

    def __init__(
        self,
        scope: Scope,
        emitter: Emitter,
        *,
        make_service: ServiceFactory,
        store: Callable[[], StoreHandle | None] | None = None,
        strict: bool = False,
        clock: Clock | None = None,
        environments: Any = None,
        slash_timeout: float = 45.0,
        shell_timeout: float = 30.0,
    ) -> None:
        from indus.tui_bridge.events import Projector
        from indus.tui_bridge.server_requests import RequestHub

        self.scope = scope
        self.emitter = emitter
        self.strict = strict
        self.clock: Clock = clock if clock is not None else scope.clock
        self.environments = environments
        self.slash_timeout = slash_timeout
        self.shell_timeout = shell_timeout
        self._make_service = make_service
        self._store_source = store
        self._service: SessionService | None = None
        self._service_lock = asyncio.Lock()
        self.views: dict[str, SessionView] = {}
        self.requests = RequestHub(self)
        self.projector = Projector(self)
        self.paste_count = 0
        self.closed = False
        self.background: dict[str, str] = {}  # side session id -> session it reports to
        self.closers: list[Callable[[], Awaitable[object]]] = []

    # -- the service ------------------------------------------------------------------------------

    async def service(self) -> SessionService:
        """The session service, built on first use; a build failure is ``PROVIDER_UNAVAILABLE``
        (or the mapped product error) and is retried on the next call."""
        if self._service is not None:
            return self._service
        async with self._service_lock:
            if self._service is not None:
                return self._service
            try:
                made = self._make_service(self)
                service = await made if asyncio.iscoroutine(made) else made
            except ProtocolError:
                raise
            except Exception as exc:
                log.warning("the session service could not be built: %s", exc)
                mapped = protocol_error_for(exc)
                if mapped.error is INTERNAL_ERROR:
                    raise ProtocolError(
                        PROVIDER_UNAVAILABLE, f"no model provider could be set up: {exc}"
                    ) from exc
                raise mapped from exc
            self._service = cast(SessionService, service)
            self.requests.service_ready(self._service)
            self.projector.service_ready(self._service)
            return self._service

    @property
    def built(self) -> SessionService | None:
        """The service when it was built already (never builds it)."""
        return self._service

    def store_handle(self) -> StoreHandle | None:
        """The P02 store handle (None on an in-memory store)."""
        if self._store_source is not None:
            handle = self._store_source()
            if handle is not None:
                return handle
        service = self._service
        if service is not None and isinstance(service.store, StateStore):
            return service.store.handle
        return None

    async def any_store_handle(self) -> StoreHandle | None:
        """Like ``store_handle``, building the service when nothing else gives a handle."""
        handle = self.store_handle()
        if handle is not None:
            return handle
        try:
            await self.service()
        except ProtocolError:
            return None
        return self.store_handle()

    # -- sessions ---------------------------------------------------------------------------------

    def default_tool_mode(self) -> ToolMode:
        from indus.tui_bridge.methods_config import tui_setting

        value = str(tui_setting(self.scope, "tool_progress") or "all").strip().lower()
        return cast(ToolMode, value) if value in TOOL_MODES else "all"

    def view(self, session_id: str) -> SessionView | None:
        service = self._service
        if service is not None:
            session_id = service.current_id(session_id)
        return self.views.get(session_id)

    def watch(self, session_id: str) -> SessionView:
        """The view of an attached session, subscribing to its events the first time."""
        service = self._service
        if service is None:
            raise RuntimeError("watch before the service exists")
        session_id = service.current_id(session_id)
        view = self.views.get(session_id)
        if view is None:
            view = SessionView(session_id, tool_mode=self.default_tool_mode())
            self.views[session_id] = view
        if not view.unsubscribe:
            self.projector.watch(service, view)
        return view

    def forget(self, session_id: str) -> SessionView | None:
        """Stop forwarding a session's events (it was closed)."""
        view = self.views.pop(session_id, None)
        if view is not None:
            for undo in view.unsubscribe:
                try:
                    undo()
                except Exception:
                    log.debug("unsubscribe failed", exc_info=True)
            view.unsubscribe.clear()
        return view

    # -- output -----------------------------------------------------------------------------------

    def notify(self, method: str, params: Mapping[str, Any]) -> bool:
        if self.closed:
            return False
        return self.emitter.notify(method, params)

    # -- shutdown ---------------------------------------------------------------------------------

    async def aclose(self) -> None:
        """Close every session and the service; nothing more is written."""
        self.closed = True
        for session_id in list(self.views):
            self.forget(session_id)
        service = self._service
        if service is not None:
            try:
                await service.aclose()
            except Exception:
                log.warning("closing the session service failed", exc_info=True)
        for closer in reversed(self.closers):
            try:
                await closer()
            except Exception:
                log.warning("a bridge shutdown step failed", exc_info=True)
        self.closers.clear()
