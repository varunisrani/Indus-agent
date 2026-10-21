"""P24-07 / R08.11: a profile's canonical bot chat is (profile, fixed title) and nothing else."""

from __future__ import annotations

import sqlite3

import pytest

from indus.state import bot_chat, compression, listing, messages, sessions, titles
from indus.state.bot_chat import (
    BOT_SOURCE,
    CANONICAL_TITLE,
    FAILURE_REASONS,
    RETRYABLE_REASONS,
    classify_failure,
    ensure_canonical,
    find_canonical,
    inject_incoming,
)
from indus.state.lineage import AmbiguousChain
from indus.state.registry import StoreHandle
from tests.fakes.clock import FakeClock

SUMMARY = [{"role": "user", "content": "summary so far"}]


def test_created_once_hidden_and_found_again(store: StoreHandle) -> None:
    assert find_canonical(store) is None
    first = ensure_canonical(store)
    assert first.created
    row = sessions.get(store, first.session_id)
    assert row is not None
    assert (row["title"], row["hidden"], row["source"]) == (CANONICAL_TITLE, 1, BOT_SOURCE)
    again = ensure_canonical(store)
    assert (again.session_id, again.created) == (first.session_id, False)
    assert find_canonical(store) == first.session_id  # hidden rows are part of the lookup
    visible = listing.list_sessions(store, listing.ListFilters(), 50, 0)
    assert first.session_id not in [r.id for r in visible]


def test_canonical_chat_survives_compression(store: StoreHandle) -> None:
    chat = ensure_canonical(store).session_id
    messages.append(store, chat, {"role": "user", "content": "hello"})
    assert compression.acquire(store, chat, "rotator")
    mark = compression.watermark(store, chat)
    child = compression.publish_continuation(store, chat, {"id": "c1"}, SUMMARY, mark, "rotator")
    assert find_canonical(store) == child
    assert ensure_canonical(store).session_id == child  # same route, the live tip
    session_id, _ = inject_incoming(store, "next message")
    assert session_id == child


def test_a_racing_creator_is_adopted_not_forked(
    store: StoreHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = "20260101_000000_racer"
    real_find = bot_chat.find_canonical

    def first_look_misses(handle: StoreHandle) -> str | None:
        # The other writer mints the chat right after our first look found nothing.
        monkeypatch.setattr(bot_chat, "find_canonical", real_find)

        def mint(conn: sqlite3.Connection) -> None:
            sessions.create_in(conn, other, BOT_SOURCE, now=0.0, hidden=1)
            titles.set_title_in(conn, other, CANONICAL_TITLE, source="user")

        handle.write(mint)
        return None

    monkeypatch.setattr(bot_chat, "find_canonical", first_look_misses)
    got = bot_chat.ensure_canonical(store)
    assert (got.session_id, got.created) == (other, False)
    with store.read() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE source = ?", (BOT_SOURCE,)
        ).fetchone()[0]
    assert count == 1


def test_recency_is_never_consulted(store: StoreHandle, fake_clock: FakeClock) -> None:
    chat = ensure_canonical(store).session_id
    fake_clock.advance(60)
    sessions.create(store, "20991231_235959_newer", BOT_SOURCE)
    messages.append(store, "20991231_235959_newer", {"role": "user", "content": "newest"})
    assert find_canonical(store) == chat


def test_the_title_is_the_identity_even_on_a_user_session(store: StoreHandle) -> None:
    sessions.create(store, "20260101_000000_mine", "cli")
    titles.set_title(store, "20260101_000000_mine", CANONICAL_TITLE)
    found = ensure_canonical(store)
    assert (found.session_id, found.created) == ("20260101_000000_mine", False)


def test_an_ambiguous_lineage_fails_closed(store: StoreHandle) -> None:
    chat = ensure_canonical(store).session_id

    def fork(conn: sqlite3.Connection) -> None:
        now = 1.0
        sessions.end_in(conn, chat, sessions.END_COMPRESSION, now=now)
        for child in ("k1", "k2"):
            sessions.create_in(conn, child, BOT_SOURCE, now=now, parent_session_id=chat)

    store.write(fork)
    with pytest.raises(AmbiguousChain):
        find_canonical(store)


def test_inject_incoming_creates_the_chat_and_adds_a_user_row(store: StoreHandle) -> None:
    session_id, row_id = inject_incoming(store, "[cron] nightly report: all green")
    assert row_id > 0 and find_canonical(store) == session_id
    rows = messages.get(store, session_id)
    assert [(r["role"], r["content"]) for r in rows] == [
        ("user", "[cron] nightly report: all green")
    ]
    with pytest.raises(ValueError):
        inject_incoming(store, "   ")


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("Error 401: invalid api key", "provider_auth"),
        ("insufficient_quota: check your billing", "provider_quota"),
        ("429 Too Many Requests", "provider_rate_limit"),
        ("upstream 503 service unavailable", "provider_server"),
        ("maximum context length is 128000 tokens", "context_overflow"),
        ("no provider is configured", "missing_config"),
        ("the model foo-1 does not exist", "model_unavailable"),
        ("connection refused", "runtime_offline"),
        ("the message expired in the queue", "queued_expired"),
        ("the request timed out", "delivery_timeout"),
        ("another turn is running", "target_busy"),
        ("something odd", "unknown"),
        (None, "unknown"),
    ],
)
def test_classify_failure(text: str | None, reason: str) -> None:
    assert classify_failure(text) == reason
    assert reason in FAILURE_REASONS


def test_retryable_reasons() -> None:
    assert {
        "runtime_offline",
        "delivery_timeout",
        "provider_rate_limit",
        "provider_server",
        "context_overflow",
    } == RETRYABLE_REASONS
