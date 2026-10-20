"""Single-use tickets and the one WebSocket upgrade check (P17-10).

A client reaches the daemon's loopback WebSocket in two steps: it asks the owner-only control
socket for a ticket (``session-ticket``, P17-11), then opens the WebSocket offering two
subprotocols, ``indus-gateway-v1`` and ``indus-gateway-ticket.<ticket>``. Every WS endpoint (the
gateway's, P17-13, and the web dashboard's, P18) calls ``authenticate_upgrade`` and nothing else
to decide; this module sits in ``service`` so both import it without importing each other.

Tickets: 32 bytes from ``secrets``, URL-safe base64 without padding (43 characters). The book
keeps only the SHA-256 digest with what the ticket is bound to: purpose, profile id, the daemon
instance id and an expiry 30 s after minting. Redeeming is atomic and happens once: a second use,
an expired ticket or a ticket for another purpose is refused, and any failed redemption of a known
ticket spends it. The digests are compared with ``hmac.compare_digest``.

``profile_id`` is a stable hash of the profile home. ``instance_id`` is random per daemon start
(one book per start), so a ticket minted by an earlier daemon is unknown to the next one.

The upgrade check (``authenticate_upgrade``) refuses a peer that is not a loopback address and an
``Origin`` header that is present but not allow-listed (close 4403), then redeems exactly one
offered ticket (close 4401 when it is missing, doubled, unknown, used, expired or bound to another
purpose). Every ticket offered in a refused upgrade is spent. A ticket in the query string is
never used: it is spent and the attempt is logged without the value. On success the server echoes
only ``indus-gateway-v1``.

Nothing here logs, formats or raises with a ticket value; ``MintedTicket`` hides it from ``repr``.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import os
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import parse_qsl

from indus.protocol.errors import TICKET_INVALID

__all__ = [
    "CLOSE_BAD_TICKET",
    "CLOSE_FORBIDDEN",
    "DEFAULT_PURPOSES",
    "MAX_LIVE_TICKETS",
    "NATIVE_HTTP",
    "SUBPROTOCOL",
    "TICKET_PROTOCOL_PREFIX",
    "TICKET_TTL_S",
    "WS_INTERACTIVE",
    "MintedTicket",
    "TicketBook",
    "TicketGrant",
    "TicketRefused",
    "UpgradeAccepted",
    "UpgradeRefused",
    "authenticate_upgrade",
    "is_loopback_host",
    "looks_like_ticket",
    "new_instance_id",
    "offered_tickets",
    "stable_profile_id",
]

log = logging.getLogger(__name__)

SUBPROTOCOL = "indus-gateway-v1"
"""The protocol a client offers and the server echoes."""
TICKET_PROTOCOL_PREFIX = "indus-gateway-ticket."
"""The subprotocol that carries the ticket: this prefix and the ticket."""

WS_INTERACTIVE = "ws-interactive"
NATIVE_HTTP = "native-http"
DEFAULT_PURPOSES: frozenset[str] = frozenset({WS_INTERACTIVE, NATIVE_HTTP})
"""What a ticket may be minted for; P18 passes its dashboard purposes to ``TicketBook``."""

TICKET_TTL_S = 30.0
TICKET_BYTES = 32
MAX_LIVE_TICKETS = 1024
SPENT_MARKERS_PER_SLOT = 4
"""Spent tickets leave a digest marker until they expire; at most this many markers per live
slot are kept, so minting and burning tickets in a loop cannot grow the book without bound."""
CLOSE_BAD_TICKET = TICKET_INVALID.code
"""4401: the protocol table's ``TICKET_INVALID``."""
CLOSE_FORBIDDEN = 4403

_TICKET_SHAPE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_QUERY_TICKET_KEYS = frozenset({"ticket", "token"})

type RefusalReason = Literal[
    "malformed", "unknown", "used", "expired", "wrong_purpose", "wrong_profile", "capacity"
]


class TicketRefused(Exception):
    """A ticket could not be minted or redeemed. ``reason`` is a short code; the message never
    carries the ticket."""

    def __init__(self, reason: RefusalReason, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason: RefusalReason = reason


@dataclass(frozen=True, slots=True)
class TicketGrant:
    """What a redeemed ticket was bound to (no secret in here)."""

    purpose: str
    profile_id: str
    instance_id: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class MintedTicket:
    """A fresh ticket. ``ticket`` is the secret: hand it to the client, never log it."""

    ticket: str = field(repr=False)
    purpose: str
    profile_id: str
    instance_id: str
    expires_at: float
    expires_in: float


@dataclass(slots=True)
class _Entry:
    digest: bytes
    grant: TicketGrant
    spent: bool = False


# -- ids ------------------------------------------------------------------------------------------


def stable_profile_id(home: Path) -> str:
    """16 hex characters derived from the resolved profile home; the same home always gives the
    same id, and nothing about the path can be read back from it."""
    resolved = os.path.normcase(str(Path(home).expanduser().resolve(strict=False)))
    return hashlib.sha256(b"indus-profile\0" + resolved.encode("utf-8")).hexdigest()[:16]


def new_instance_id() -> str:
    """A fresh id for one daemon start."""
    return secrets.token_hex(8)


def looks_like_ticket(value: object) -> bool:
    """The shape a minted ticket has (checked before anything is hashed)."""
    return isinstance(value, str) and _TICKET_SHAPE.match(value) is not None


def _digest(ticket: str) -> bytes:
    return hashlib.sha256(ticket.encode("ascii")).digest()


# -- the book -------------------------------------------------------------------------------------


class TicketBook:
    """The live tickets of one daemon start (see the module notes). Thread-safe: the control
    socket may mint on a worker thread while the event loop redeems."""

    def __init__(
        self,
        *,
        profile_id: str,
        instance_id: str | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        ttl: float = TICKET_TTL_S,
        limit: int = MAX_LIVE_TICKETS,
        purposes: Iterable[str] = DEFAULT_PURPOSES,
    ) -> None:
        if not profile_id:
            raise ValueError("a ticket book needs a profile id")
        if ttl <= 0 or limit < 1:
            raise ValueError("ticket ttl and limit must be positive")
        self.profile_id = profile_id
        self.instance_id = instance_id or new_instance_id()
        self.ttl = ttl
        self.limit = limit
        self.purposes: frozenset[str] = frozenset(purposes)
        self._now = monotonic
        self._lock = threading.Lock()
        self._entries: dict[bytes, _Entry] = {}

    @classmethod
    def for_home(cls, home: Path, **kwargs: Any) -> TicketBook:
        """A book for the daemon serving ``home``, with a fresh instance id."""
        return cls(profile_id=stable_profile_id(home), **kwargs)

    def _prune(self, now: float) -> None:
        stale = [key for key, entry in self._entries.items() if entry.grant.expires_at <= now]
        for key in stale:
            del self._entries[key]

    def mint(self, purpose: str) -> MintedTicket:
        """A new ticket for ``purpose``; ``TicketRefused`` for an unknown purpose (``malformed``)
        or when ``limit`` tickets are already live (``capacity``)."""
        if purpose not in self.purposes:
            raise TicketRefused("malformed", "unknown ticket purpose")
        with self._lock:
            now = self._now()
            self._prune(now)
            live = sum(1 for entry in self._entries.values() if not entry.spent)
            if live >= self.limit or len(self._entries) >= self.limit * SPENT_MARKERS_PER_SLOT:
                raise TicketRefused("capacity", "too many tickets are waiting to be used")
            while True:
                ticket = secrets.token_urlsafe(TICKET_BYTES)
                digest = _digest(ticket)
                if digest not in self._entries:
                    break
            grant = TicketGrant(
                purpose=purpose,
                profile_id=self.profile_id,
                instance_id=self.instance_id,
                expires_at=now + self.ttl,
            )
            self._entries[digest] = _Entry(digest, grant)
        return MintedTicket(
            ticket=ticket,
            purpose=purpose,
            profile_id=grant.profile_id,
            instance_id=grant.instance_id,
            expires_at=grant.expires_at,
            expires_in=self.ttl,
        )

    def redeem(self, ticket: object, *, purpose: str, profile_id: str | None = None) -> TicketGrant:
        """Use ``ticket`` once for ``purpose``. Any refusal of a known ticket spends it."""
        if not looks_like_ticket(ticket):
            raise TicketRefused("malformed", "not a ticket")
        digest = _digest(cast(str, ticket))
        with self._lock:
            now = self._now()
            entry = self._entries.get(digest)
            if entry is None or not hmac.compare_digest(entry.digest, digest):
                self._prune(now)
                raise TicketRefused("unknown", "no such ticket")
            if entry.spent:
                raise TicketRefused("used", "the ticket was used already")
            entry.spent = True
            grant = entry.grant
            if grant.expires_at <= now:
                self._prune(now)
                raise TicketRefused("expired", "the ticket expired")
            if grant.purpose != purpose:
                raise TicketRefused("wrong_purpose", "the ticket was minted for another purpose")
            if profile_id is not None and profile_id != grant.profile_id:
                raise TicketRefused("wrong_profile", "the ticket belongs to another profile")
            return grant

    def spend(self, ticket: object) -> bool:
        """Burn ``ticket`` without using it (it was seen where it must not be). True when it was
        live."""
        if not looks_like_ticket(ticket):
            return False
        digest = _digest(cast(str, ticket))
        with self._lock:
            entry = self._entries.get(digest)
            if entry is None or entry.spent:
                return False
            entry.spent = True
            return True

    def outstanding(self) -> int:
        """Live tickets not used yet (a client is attaching right now)."""
        with self._lock:
            now = self._now()
            return sum(
                1
                for entry in self._entries.values()
                if not entry.spent and entry.grant.expires_at > now
            )

    def revoke_all(self) -> None:
        """Forget every ticket (daemon stop)."""
        with self._lock:
            self._entries.clear()


# -- the upgrade check ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UpgradeAccepted:
    """Accept the upgrade and echo ``subprotocol``."""

    subprotocol: str
    grant: TicketGrant


@dataclass(frozen=True, slots=True)
class UpgradeRefused:
    """Close the socket with ``close_code``; ``reason`` is a short fixed text (no origin echo)."""

    close_code: int
    reason: str


def is_loopback_host(host: object) -> bool:
    """A literal loopback address (``127.0.0.0/8``, ``::1``, IPv4-mapped loopback). Names, Unix
    socket peers and anything unparsable are not."""
    if not isinstance(host, str) or not host:
        return False
    try:
        address = ipaddress.ip_address(host.strip("[]").split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped.is_loopback
    return address.is_loopback


def offered_tickets(subprotocols: Sequence[object]) -> list[str]:
    """The ticket parts of every ``indus-gateway-ticket.<t>`` subprotocol offered."""
    return [
        item[len(TICKET_PROTOCOL_PREFIX) :]
        for item in subprotocols
        if isinstance(item, str) and item.startswith(TICKET_PROTOCOL_PREFIX)
    ]


def _headers(scope: Mapping[str, Any]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    raw: object = scope.get("headers") or ()
    if not isinstance(raw, list | tuple):
        return found
    for pair in cast(Sequence[object], raw):
        if not isinstance(pair, list | tuple) or len(cast(Sequence[object], pair)) != 2:
            continue
        name, value = cast(Sequence[object], pair)
        if isinstance(name, bytes) and isinstance(value, bytes):
            key = name.decode("latin-1").lower()
            found.setdefault(key, []).append(value.decode("latin-1"))
    return found


def _subprotocols(scope: Mapping[str, Any], headers: Mapping[str, list[str]]) -> list[str]:
    raw: object = scope.get("subprotocols")
    if isinstance(raw, list | tuple):
        return [item for item in cast(Sequence[object], raw) if isinstance(item, str)]
    offered: list[str] = []
    for value in headers.get("sec-websocket-protocol", []):
        offered.extend(part.strip() for part in value.split(",") if part.strip())
    return offered


def _query_tickets(scope: Mapping[str, Any]) -> list[str]:
    raw: object = scope.get("query_string") or b""
    if not isinstance(raw, bytes) or not raw:
        return []
    found: list[str] = []
    for key, value in parse_qsl(raw.decode("latin-1"), keep_blank_values=True):
        if key.lower() in _QUERY_TICKET_KEYS or value.startswith(TICKET_PROTOCOL_PREFIX):
            found.append(value.removeprefix(TICKET_PROTOCOL_PREFIX))
    return found


def _normal_origin(value: str) -> str:
    return value.strip().rstrip("/").lower()


def _peer_host(scope: Mapping[str, Any]) -> object:
    client: object = scope.get("client")
    if isinstance(client, list | tuple) and cast(Sequence[object], client):
        return cast(Sequence[object], client)[0]
    return None


def authenticate_upgrade(
    scope: Mapping[str, Any],
    purpose: str,
    *,
    book: TicketBook,
    origins: Iterable[str] = (),
) -> UpgradeAccepted | UpgradeRefused:
    """Decide one WebSocket upgrade from its ASGI ``scope`` (see the module notes)."""
    headers = _headers(scope)
    offered = _subprotocols(scope, headers)
    tickets = offered_tickets(offered)
    in_query = _query_tickets(scope)
    for value in in_query:
        book.spend(value)
    if in_query:
        log.warning("a ticket in the WebSocket URL was ignored and spent (value not logged)")

    def refuse(code: int, reason: str) -> UpgradeRefused:
        for value in tickets:
            book.spend(value)
        log.info("WebSocket upgrade refused: %s", reason)
        return UpgradeRefused(code, reason)

    if not is_loopback_host(_peer_host(scope)):
        return refuse(CLOSE_FORBIDDEN, "peer is not loopback")
    origin_values = headers.get("origin", [])
    if origin_values:
        allowed = {_normal_origin(item) for item in origins}
        if len(origin_values) != 1 or _normal_origin(origin_values[0]) not in allowed:
            return refuse(CLOSE_FORBIDDEN, "origin not allowed")
    if len(tickets) != 1:
        return refuse(CLOSE_BAD_TICKET, "exactly one ticket must be offered")
    if SUBPROTOCOL not in offered:
        return refuse(CLOSE_BAD_TICKET, f"{SUBPROTOCOL} not offered")
    try:
        grant = book.redeem(tickets[0], purpose=purpose)
    except TicketRefused as refused:
        log.info("WebSocket upgrade refused: ticket %s", refused.reason)
        return UpgradeRefused(CLOSE_BAD_TICKET, f"ticket {refused.reason}")
    return UpgradeAccepted(SUBPROTOCOL, grant)
