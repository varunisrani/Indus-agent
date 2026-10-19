"""Server requests that outlive viewer connections: offer, re-offer, settle once (P17-07).

In the daemon a session may be watched by several viewers (CLI, TUI, desktop …). When the agent
asks a person something, the session's ``RequestBook`` offers the request to every attached viewer
that can render its kind and keeps it open until it is settled. Ids are ``srq-<12 hex>`` (strings,
so they never collide with the client's integer request ids).

- Re-offer: a viewer that attaches again, or that catches up through ``events.since``, is offered
  every still-open request it can render once more, with ``replayed: True`` and the same id. An
  answer from any copy of a card therefore names the same request, and the first answer wins.
- Settlement: ``settle`` removes the request and announces it once with a ``request.settled``
  event (``answered``, ``withdrawn`` or ``undeliverable``) so every other card closes. A second
  settlement of the same id does nothing.
- Refusals (R05.3): a viewer that cannot render the kind it was sent answers ``-32601`` at once;
  a viewer that advertised the decline capability may answer ``4404`` ("not shown here"); any
  other error a viewer answers with counts like ``-32601``. Each leaves the request to the other
  viewers. When no attached viewer that could answer is left, the
  request is settled ``undeliverable`` right away, never by waiting out a timeout; the broker then
  fails the asker with ``RequestUnsupported`` (R02.5: distinct from a timeout or a denial). A
  ``4404`` from a viewer without the capability is ignored.
- A viewer that detaches does not refuse anything: its open requests wait for it (or another
  viewer) until the asker's own deadline.

The book lives in memory for the daemon's life; after a restart the turn that asked is
``unknown`` (R03.21) and its requests are gone with it. Every method runs on the session's event
loop, like the broker.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from indus.protocol.envelope import ErrorObject
from indus.protocol.errors import METHOD_NOT_FOUND, SERVER_REQUEST_UNSUPPORTED, make_error
from indus.protocol.server_requests import (
    SERVER_REQUEST_KINDS,
    ApprovalRequest,
    ClarifyRequest,
    SecretRequest,
    SudoRequest,
)

__all__ = [
    "NOT_SHOWN_CODE",
    "REQUEST_ID_PREFIX",
    "SETTLED_EVENT",
    "UNRENDERABLE_CODE",
    "Offer",
    "OpenRequest",
    "RequestBook",
    "RequestSettlement",
    "RequestViewer",
    "new_request_id",
]

log = logging.getLogger(__name__)

REQUEST_ID_PREFIX = "srq-"
NOT_SHOWN_CODE = 4404
"""A viewer's "this request is not shown here" answer (only with the decline capability)."""
UNRENDERABLE_CODE = METHOD_NOT_FOUND.code
"""A viewer's "I cannot render this kind" answer (JSON-RPC method not found, -32601)."""
SETTLED_EVENT = "request.settled"

type OfferedRequest = ApprovalRequest | ClarifyRequest | SudoRequest | SecretRequest
type SettledEmitter = Callable[[Mapping[str, Any]], object]
type UndeliverableHook = Callable[[str, ErrorObject], object]


def new_request_id() -> str:
    """A fresh server-request id: ``srq-`` and 12 lowercase hex digits."""
    return REQUEST_ID_PREFIX + secrets.token_hex(6)


class RequestSettlement(StrEnum):
    """How an open request ended."""

    ANSWERED = "answered"
    WITHDRAWN = "withdrawn"
    UNDELIVERABLE = "undeliverable"


@dataclass(frozen=True, slots=True)
class Offer:
    """One request as sent to one viewer."""

    request: OfferedRequest
    generation: int | None
    """The generation of the turn that asked (named again in the viewer's answer)."""
    replayed: bool
    """True when this is a repeat of an earlier offer (reattach or catch-up)."""

    def to_wire(self) -> dict[str, Any]:
        body: dict[str, Any] = {"request": self.request.to_wire(), "replayed": self.replayed}
        if self.generation is not None:
            body["generation"] = self.generation
        return body


@dataclass(frozen=True, slots=True)
class RequestViewer:
    """An attached viewer as the book sees it."""

    viewer_id: str
    kinds: frozenset[str]
    """Request kinds the viewer can render."""
    send: Callable[[Offer], object]
    can_decline: bool = False
    """Whether the viewer advertised the 4404 decline capability."""

    @classmethod
    def of(
        cls,
        viewer_id: str,
        kinds: Iterable[str],
        send: Callable[[Offer], object],
        *,
        can_decline: bool = False,
    ) -> RequestViewer:
        known = frozenset(k for k in kinds if k in SERVER_REQUEST_KINDS)
        return cls(viewer_id, known, send, can_decline)


@dataclass(slots=True)
class OpenRequest:
    """A request waiting for its first answer."""

    request: OfferedRequest
    generation: int | None
    offered: set[str] = field(default_factory=set[str])
    """Viewer ids it was sent to."""
    refused: dict[str, int] = field(default_factory=dict[str, int])
    """Viewer id → the code it refused with (-32601 or 4404)."""

    @property
    def request_id(self) -> str:
        return self.request.request_id

    @property
    def kind(self) -> str:
        return self.request.kind


class RequestBook:
    """The open server requests of one session and the viewers they go to."""

    def __init__(
        self,
        session_id: str,
        *,
        emit: SettledEmitter,
        generation: Callable[[], int | None] = lambda: None,
    ) -> None:
        self.session_id = session_id
        self._emit = emit
        self._generation = generation
        self._viewers: dict[str, RequestViewer] = {}
        self._open: dict[str, OpenRequest] = {}
        self._undeliverable: UndeliverableHook | None = None

    def bind_undeliverable(self, hook: UndeliverableHook | None) -> None:
        """Called with ``(request_id, error)`` when a request is settled undeliverable."""
        self._undeliverable = hook

    # -- viewers ------------------------------------------------------------------------------

    def attach(self, viewer: RequestViewer) -> int:
        """Add (or replace) a viewer and offer it every open request it can render."""
        self._viewers[viewer.viewer_id] = viewer
        return self.reoffer(viewer.viewer_id)

    def detach(self, viewer_id: str) -> bool:
        """Remove a viewer; its open requests stay open."""
        return self._viewers.pop(viewer_id, None) is not None

    def viewers(self) -> list[str]:
        return list(self._viewers)

    def can_render(self, kind: str) -> bool:
        """Whether any attached viewer can render ``kind``."""
        return any(kind in v.kinds for v in self._viewers.values())

    def answerable_kinds(self) -> frozenset[str]:
        """Every kind at least one attached viewer can render."""
        kinds: set[str] = set()
        for viewer in self._viewers.values():
            kinds |= viewer.kinds
        return frozenset(kinds)

    # -- offering -----------------------------------------------------------------------------

    def offer(self, request: OfferedRequest) -> OpenRequest | None:
        """Open ``request`` and send it to every viewer that can render it.

        Returns the open entry, or None when nobody could take it (then it was settled
        undeliverable at once).
        """
        if request.request_id in self._open:
            raise ValueError(f"server request {request.request_id} is already open")
        entry = OpenRequest(request, self._generation())
        self._open[request.request_id] = entry
        targets = [v for v in self._viewers.values() if request.kind in v.kinds]
        if not targets:
            self._give_up(entry)
            return None
        for viewer in targets:
            self._send(viewer, entry, replayed=False)
        return entry if request.request_id in self._open else None

    def reoffer(self, viewer_id: str) -> int:
        """Offer ``viewer_id`` every open request it can render again (``replayed``)."""
        viewer = self._viewers.get(viewer_id)
        if viewer is None:
            return 0
        count = 0
        for entry in list(self._open.values()):
            if entry.kind not in viewer.kinds or viewer_id in entry.refused:
                continue
            if entry.request_id not in self._open:  # settled by an earlier send in this loop
                continue
            self._send(viewer, entry, replayed=True)
            count += 1
        return count

    def _send(self, viewer: RequestViewer, entry: OpenRequest, *, replayed: bool) -> None:
        entry.offered.add(viewer.viewer_id)
        try:
            viewer.send(Offer(entry.request, entry.generation, replayed))
        except Exception:
            log.warning(
                "could not offer server request %s to viewer %s",
                entry.request_id,
                viewer.viewer_id,
                exc_info=True,
            )

    # -- refusals -----------------------------------------------------------------------------

    def refuse(self, viewer_id: str, request_id: str, code: int) -> bool:
        """A viewer answered ``request_id`` with an error instead of an answer.

        ``-32601`` (cannot render) and any other error code count as that viewer refusing;
        ``4404`` counts only from a viewer with the decline capability. Returns True when the
        refusal was recorded. When it leaves no attached viewer that could answer, the request is
        settled undeliverable at once.
        """
        entry = self._open.get(request_id)
        if entry is None:
            return False
        viewer = self._viewers.get(viewer_id)
        if code == NOT_SHOWN_CODE and (viewer is None or not viewer.can_decline):
            log.warning(
                "viewer %s declined %s without the decline capability; ignored",
                viewer_id,
                request_id,
            )
            return False
        entry.refused[viewer_id] = code
        if not any(
            entry.kind in v.kinds and v.viewer_id not in entry.refused
            for v in self._viewers.values()
        ):
            self._give_up(entry)
        return True

    def _give_up(self, entry: OpenRequest) -> None:
        codes = set(entry.refused.values())
        reason = "not_shown" if codes == {NOT_SHOWN_CODE} else "unsupported"
        self.settle(entry.request_id, RequestSettlement.UNDELIVERABLE, reason)

    # -- settling -----------------------------------------------------------------------------

    def settle(
        self, request_id: str, outcome: RequestSettlement, reason: str | None = None
    ) -> bool:
        """Close ``request_id`` with ``outcome``; only the first settlement counts."""
        entry = self._open.pop(request_id, None)
        if entry is None:
            return False
        event: dict[str, Any] = {
            "type": SETTLED_EVENT,
            "session_id": self.session_id,
            "request_id": request_id,
            "kind": entry.kind,
            "outcome": outcome.value,
        }
        if reason:
            event["reason"] = reason
        try:
            self._emit(event)
        except Exception:
            log.warning("could not announce settlement of %s", request_id, exc_info=True)
        if outcome is RequestSettlement.UNDELIVERABLE and self._undeliverable is not None:
            why = (
                "every attached viewer declined it (not shown there)"
                if reason == "not_shown"
                else "no attached viewer can answer it"
            )
            error = make_error(
                SERVER_REQUEST_UNSUPPORTED,
                f"the {entry.kind} request could not be delivered: {why}",
                retryable=False,
            )
            self._undeliverable(request_id, error)
        return True

    # -- reading ------------------------------------------------------------------------------

    def lookup(self, request_id: str) -> OpenRequest | None:
        """The open request ``request_id``, or None once settled (or never opened)."""
        return self._open.get(request_id)

    def open_requests(self) -> list[OpenRequest]:
        """Open requests, oldest first."""
        return list(self._open.values())
