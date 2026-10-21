"""A profile's canonical bot chat, and the vocabulary of bot-to-bot delivery (P24-07, R08.11).

Each profile ("bot") has one forever-chat. Its identity is **only** the fixed title
``CANONICAL_TITLE`` in that profile's store: titles are unique among titled sessions (a unique
index), so at most one row carries it, and compression hands the title to the newest segment.
``find_canonical`` reads hidden rows too and follows the compression lineage to its live tip; an
ambiguous lineage fails closed (``AmbiguousChain``) rather than guess. ``ensure_canonical`` runs
the lookup again inside its write transaction (adopt before mint) and only then creates the chat,
hidden from the session list. No session-id pointer is ever stored and recency is never consulted:
pointers dangled, and "the newest chat" heuristics stole the chat and forked it in loops.

It lives in the state layer so the gateway (receiving side, cron's ``self-chat`` target), the CLI
and the tools share it without importing each other (D-16.246).

Delivery failures carry one of ``FAILURE_REASONS``; ``RETRYABLE_REASONS`` are the ones a second
attempt can help (at most one retry). ``classify_failure`` turns a failure text into a reason.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Final, Literal

from indus.foundation.ids import new_session_id
from indus.state import lineage, messages, sessions, titles
from indus.state.registry import StoreHandle
from indus.state.titles import TitleTaken
from indus.state.writer import TRANSCRIPT

__all__ = [
    "BOT_SOURCE",
    "CANONICAL_TITLE",
    "FAILURE_REASONS",
    "RETRYABLE_REASONS",
    "CanonicalChat",
    "FailureReason",
    "classify_failure",
    "ensure_canonical",
    "find_canonical",
    "find_canonical_in",
    "inject_incoming",
]

CANONICAL_TITLE: Final = "Bot chat"
BOT_SOURCE: Final = "bots"

type FailureReason = Literal[
    "provider_auth",
    "provider_quota",
    "provider_rate_limit",
    "provider_server",
    "context_overflow",
    "missing_config",
    "model_unavailable",
    "runtime_offline",
    "queued_expired",
    "delivery_timeout",
    "target_busy",
    "unknown",
]
FAILURE_REASONS: Final[tuple[FailureReason, ...]] = (
    "provider_auth",
    "provider_quota",
    "provider_rate_limit",
    "provider_server",
    "context_overflow",
    "missing_config",
    "model_unavailable",
    "runtime_offline",
    "queued_expired",
    "delivery_timeout",
    "target_busy",
    "unknown",
)
RETRYABLE_REASONS: Final[frozenset[FailureReason]] = frozenset(
    {
        "runtime_offline",
        "delivery_timeout",
        "provider_rate_limit",
        "provider_server",
        "context_overflow",  # the retried turn compacts first, so it can fit where this did not
    }
)


@dataclass(frozen=True)
class CanonicalChat:
    session_id: str
    created: bool


def find_canonical_in(conn: sqlite3.Connection) -> str | None:
    """The canonical chat's live segment, or None (hidden rows included; no recency)."""
    row = conn.execute("SELECT id FROM sessions WHERE title = ?", (CANONICAL_TITLE,)).fetchone()
    if row is None:
        return None
    return lineage.tip_in(conn, str(row["id"]))


def find_canonical(store: StoreHandle) -> str | None:
    with store.read() as conn:
        return find_canonical_in(conn)


def ensure_canonical(store: StoreHandle) -> CanonicalChat:
    """The canonical chat, created hidden when (and only when) none exists (adopt before mint)."""
    found = find_canonical(store)
    if found is not None:
        return CanonicalChat(found, False)
    clock = store.scope.clock
    profile_id = store.scope.profile_id

    def mint(conn: sqlite3.Connection) -> CanonicalChat:
        again = find_canonical_in(conn)
        if again is not None:
            return CanonicalChat(again, False)
        session_id = new_session_id(clock)
        now = clock.now().timestamp()
        sessions.create_in(conn, session_id, BOT_SOURCE, now=now, profile_id=profile_id, hidden=1)
        titles.set_title_in(conn, session_id, CANONICAL_TITLE, source="user")
        return CanonicalChat(session_id, True)

    try:
        return store.write(mint)
    except (TitleTaken, sqlite3.IntegrityError):
        winner = find_canonical(store)  # another writer minted it between our two looks
        if winner is None:
            raise
        return CanonicalChat(winner, False)


def inject_incoming(store: StoreHandle, text: str) -> tuple[str, int]:
    """Append ``text`` to the canonical chat as an incoming (user-role) message, creating the
    chat when missing; no turn runs. Answers ``(session_id, message row id)``."""
    if not text.strip():
        raise ValueError("an incoming message needs text")
    chat = ensure_canonical(store)
    now = store.scope.clock.now().timestamp()
    message: dict[str, Any] = {"role": "user", "content": text}

    def run(conn: sqlite3.Connection) -> tuple[str, int]:
        tip = find_canonical_in(conn) or chat.session_id
        return tip, messages.append_in(conn, tip, message, now=now)

    return store.write(run, budget=TRANSCRIPT)


# -- failure reasons ----------------------------------------------------------------------------

_RULES: Final[tuple[tuple[FailureReason, re.Pattern[str]], ...]] = (
    (
        "context_overflow",
        re.compile(
            r"context[ _-]?(length|window|overflow|limit)|too many tokens|maximum context|"
            r"prompt is too long",
            re.I,
        ),
    ),
    (
        "provider_quota",
        re.compile(r"quota|billing|insufficient[ _]credit|payment required|\b402\b", re.I),
    ),
    ("provider_rate_limit", re.compile(r"rate[ _-]?limit|too many requests|\b429\b", re.I)),
    (
        "provider_auth",
        re.compile(
            r"\b40[13]\b|unauthori[sz]ed|forbidden|invalid (api )?key|authenticat|"
            r"permission denied|access denied",
            re.I,
        ),
    ),
    (
        "model_unavailable",
        re.compile(r"model[^.]{0,40}(not found|unavailable|does not exist|unknown)", re.I),
    ),
    (
        "missing_config",
        re.compile(
            r"no (api )?key|not configured|missing config|no provider|no model is set", re.I
        ),
    ),
    ("queued_expired", re.compile(r"queue[d]?[^.]{0,30}expired|expired in the queue", re.I)),
    ("target_busy", re.compile(r"\bbusy\b|another turn is running|already running", re.I)),
    ("delivery_timeout", re.compile(r"timed? ?out|deadline", re.I)),
    (
        "runtime_offline",
        re.compile(r"offline|not running|connection refused|unreachable|could not connect", re.I),
    ),
    (
        "provider_server",
        re.compile(
            r"\b5\d\d\b|server error|overloaded|bad gateway|service unavailable|internal error",
            re.I,
        ),
    ),
)


def classify_failure(text: str | None) -> FailureReason:
    """A failure text (a turn's failure, a peer's error) → one of ``FAILURE_REASONS``."""
    if text:
        for reason, pattern in _RULES:
            if pattern.search(text):
                return reason
    return "unknown"
