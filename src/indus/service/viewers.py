"""Viewers of daemon-hosted sessions: who watches which session, and from what surface (P17-08).

A viewer is one client connection watching one session: its connection id, the kind of surface
(``cli``, ``tui``, ``gui``, ``acp``, ``web``, ``messaging``) and its capabilities. Capabilities that
name a server-request kind (``approval``, ``clarify``, ``sudo``, ``secret``) say which requests the
viewer can render; ``request.decline`` says it may answer 4404 ("not shown here"). One connection
can watch several sessions, and a session any number of viewers, including none.

``ViewerRegistry.deliver`` hands one numbered event to every viewer of its session in attach
order; a viewer whose ``send`` raises is logged and skipped, the others still get the event. The
registry is called from the daemon loop; a lock keeps it consistent if a listener runs elsewhere.

When a surface sends input while a turn runs without saying what it wants, the surface decides
(R05.18): the TUI queues it behind the turn, every other surface interrupts the turn.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

from indus.protocol.server_requests import SERVER_REQUEST_KINDS
from indus.service.request_redelivery import Offer, RequestViewer

__all__ = [
    "BUSY_MODES",
    "DECLINE_CAPABILITY",
    "SURFACES",
    "BusyMode",
    "SurfaceKind",
    "Viewer",
    "ViewerRegistry",
    "busy_default",
]

log = logging.getLogger(__name__)

type SurfaceKind = Literal["cli", "tui", "gui", "acp", "web", "messaging"]
type BusyMode = Literal["queue", "interrupt"]

SURFACES: Final[frozenset[str]] = frozenset({"cli", "tui", "gui", "acp", "web", "messaging"})
BUSY_MODES: Final[frozenset[str]] = frozenset({"queue", "interrupt"})
DECLINE_CAPABILITY: Final = "request.decline"
"""The capability a viewer advertises to answer a server request with 4404 (not shown here)."""


def busy_default(surface: str) -> BusyMode:
    """What input sent during a running turn does when the surface did not say (R05.18)."""
    return "queue" if surface == "tui" else "interrupt"


def _drop(offer: Offer) -> None:
    del offer


@dataclass(frozen=True, slots=True)
class Viewer:
    """One connection watching one session."""

    connection_id: str
    surface: SurfaceKind
    capabilities: frozenset[str]
    send: Callable[[dict[str, Any]], object]
    """Receives every numbered event of the session (its wire form, a fresh dict per call)."""
    offer: Callable[[Offer], object] | None = None
    """Receives the server requests it can render (first offers and re-offers)."""

    @classmethod
    def of(
        cls,
        connection_id: str,
        surface: str,
        capabilities: Iterable[str] = (),
        *,
        send: Callable[[dict[str, Any]], object],
        offer: Callable[[Offer], object] | None = None,
    ) -> Viewer:
        if not connection_id:
            raise ValueError("a viewer needs a connection id")
        if surface not in SURFACES:
            raise ValueError(f"unknown surface kind {surface!r}")
        return cls(connection_id, cast(SurfaceKind, surface), frozenset(capabilities), send, offer)

    @property
    def request_kinds(self) -> frozenset[str]:
        """The server-request kinds this viewer can render (none without an ``offer`` hook)."""
        if self.offer is None:
            return frozenset()
        return frozenset(k for k in self.capabilities if k in SERVER_REQUEST_KINDS)

    @property
    def can_decline(self) -> bool:
        return DECLINE_CAPABILITY in self.capabilities

    @property
    def busy_default(self) -> BusyMode:
        return busy_default(self.surface)

    def request_viewer(self) -> RequestViewer:
        """This viewer as the session's ``RequestBook`` sees it."""
        return RequestViewer.of(
            self.connection_id,
            self.request_kinds,
            self.offer or _drop,
            can_decline=self.can_decline,
        )


class ViewerRegistry:
    """Viewers per session (keyed by the host's session key)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, dict[str, Viewer]] = {}

    def attach(self, session_key: str, viewer: Viewer) -> bool:
        """Add ``viewer`` (a viewer of the same connection is replaced); True when it is new."""
        with self._lock:
            members = self._sessions.setdefault(session_key, {})
            new = viewer.connection_id not in members
            members[viewer.connection_id] = viewer
            return new

    def detach(self, session_key: str, connection_id: str) -> Viewer | None:
        """Remove one viewer; the removed viewer, or None when it was not attached."""
        with self._lock:
            members = self._sessions.get(session_key)
            if members is None:
                return None
            viewer = members.pop(connection_id, None)
            if not members:
                del self._sessions[session_key]
            return viewer

    def detach_connection(self, connection_id: str) -> list[str]:
        """Remove the connection from every session it watched; those session keys."""
        with self._lock:
            keys = [k for k, members in self._sessions.items() if connection_id in members]
            for key in keys:
                members = self._sessions[key]
                del members[connection_id]
                if not members:
                    del self._sessions[key]
            return keys

    def forget(self, session_key: str) -> list[Viewer]:
        """Drop every viewer of a closed session."""
        with self._lock:
            return list(self._sessions.pop(session_key, {}).values())

    def viewers(self, session_key: str) -> list[Viewer]:
        with self._lock:
            return list(self._sessions.get(session_key, {}).values())

    def find(self, session_key: str, connection_id: str) -> Viewer | None:
        with self._lock:
            return self._sessions.get(session_key, {}).get(connection_id)

    def count(self, session_key: str) -> int:
        with self._lock:
            return len(self._sessions.get(session_key, {}))

    def sessions(self) -> list[str]:
        """Session keys with at least one viewer."""
        with self._lock:
            return list(self._sessions)

    def sessions_of(self, connection_id: str) -> list[str]:
        with self._lock:
            return [k for k, members in self._sessions.items() if connection_id in members]

    def deliver(self, session_key: str, event: Mapping[str, Any]) -> int:
        """Send ``event`` to every viewer of the session in attach order; how many took it."""
        delivered = 0
        for viewer in self.viewers(session_key):
            try:
                viewer.send(dict(event))
            except Exception:
                log.warning(
                    "viewer %s (%s) could not take an event of session %s",
                    viewer.connection_id,
                    viewer.surface,
                    session_key,
                    exc_info=True,
                )
                continue
            delivered += 1
        return delivered
