"""Single-use file download tickets (P18-16; no credential in any URL).

A download opened in a new tab cannot set a header, and in loopback mode the SPA has no cookie, so
the Files page first asks ``POST /api/files/download-ticket {path}`` (behind the gate, so only with
the session token or a verified session cookie) for a ticket, then opens
``GET /api/files/download?path=…&ticket=…``. A ticket is not a credential to anything else:

- it is good **once** — any presentation of a known ticket spends it, refused or not;
- for **30 seconds** (``DOWNLOAD_TICKET_TTL_S``, never more than 60);
- for **one resolved path** on **one backend**: the book belongs to the dashboard process that
  minted the ticket, so it cannot fetch another file or be replayed against another profile's
  dashboard on the same machine;
- at most ``MAX_LIVE_DOWNLOADS`` wait unused at once.

Only the SHA-256 digest is kept; the value lives in the client's URL for a moment and is never
logged (uvicorn's access log is off, refusals name a reason code only).
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final, Literal

__all__ = [
    "DOWNLOAD_TICKET_TTL_S",
    "MAX_LIVE_DOWNLOADS",
    "DownloadRefused",
    "DownloadTicket",
    "DownloadTickets",
]

DOWNLOAD_TICKET_TTL_S: Final = 30.0
MAX_LIVE_DOWNLOADS: Final = 256
_TICKET_BYTES: Final = 32
_MAX_LEN: Final = 64

type DownloadRefusal = Literal["malformed", "unknown", "used", "expired", "capacity"]


class DownloadRefused(Exception):
    """A ticket could not be minted or used; ``reason`` is a code, the message never holds it."""

    def __init__(self, reason: DownloadRefusal) -> None:
        super().__init__(reason)
        self.reason: DownloadRefusal = reason


@dataclass(frozen=True, slots=True)
class DownloadTicket:
    ticket: str = field(repr=False)
    expires_in: float


@dataclass(slots=True)
class _Entry:
    path: str
    expires_at: float
    spent: bool = False


def _digest(ticket: str) -> bytes:
    return hashlib.sha256(ticket.encode("ascii")).digest()


class DownloadTickets:
    """The live download tickets of one dashboard process (thread-safe)."""

    def __init__(
        self,
        *,
        ttl: float = DOWNLOAD_TICKET_TTL_S,
        limit: int = MAX_LIVE_DOWNLOADS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 0 < ttl <= 60:
            raise ValueError("a download ticket lives more than 0 and at most 60 seconds")
        self.ttl = ttl
        self.limit = limit
        self._now = monotonic
        self._lock = threading.Lock()
        self._entries: dict[bytes, _Entry] = {}

    def _prune(self, now: float) -> None:
        for key in [k for k, e in self._entries.items() if e.expires_at <= now]:
            del self._entries[key]

    def mint(self, path: str) -> DownloadTicket:
        """A ticket for the resolved ``path``; ``capacity`` when too many wait (spent ones count
        four to a slot until they expire, so the book stays small)."""
        with self._lock:
            now = self._now()
            self._prune(now)
            live = sum(1 for e in self._entries.values() if not e.spent)
            if live >= self.limit or len(self._entries) >= self.limit * 4:
                raise DownloadRefused("capacity")
            ticket = secrets.token_urlsafe(_TICKET_BYTES)
            self._entries[_digest(ticket)] = _Entry(path, now + self.ttl)
        return DownloadTicket(ticket, self.ttl)

    def redeem(self, ticket: str) -> str:
        """Spend ``ticket`` and return the path it was minted for; ``DownloadRefused`` when it is
        malformed, unknown (another process's, or made up), used or expired. The caller checks
        the path the request names against the returned one."""
        if not ticket or len(ticket) > _MAX_LEN or not ticket.isascii():
            raise DownloadRefused("malformed")
        digest = _digest(ticket)
        with self._lock:
            now = self._now()
            entry = self._entries.get(digest)
            if entry is None:
                self._prune(now)
                raise DownloadRefused("unknown")
            if entry.spent:
                raise DownloadRefused("used")
            entry.spent = True
            if entry.expires_at <= now:
                self._prune(now)
                raise DownloadRefused("expired")
            return entry.path

    def outstanding(self) -> int:
        """Tickets minted and not used yet."""
        with self._lock:
            now = self._now()
            return sum(1 for e in self._entries.values() if not e.spent and e.expires_at > now)
