"""P17-10: single-use tickets and the shared WebSocket upgrade check."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import pytest

from indus.service import ws_auth
from indus.service.ws_auth import (
    CLOSE_BAD_TICKET,
    CLOSE_FORBIDDEN,
    NATIVE_HTTP,
    SUBPROTOCOL,
    TICKET_PROTOCOL_PREFIX,
    WS_INTERACTIVE,
    TicketBook,
    TicketRefused,
    UpgradeAccepted,
    UpgradeRefused,
    authenticate_upgrade,
    is_loopback_host,
    stable_profile_id,
)
from tests.fakes.clock import FakeClock


@pytest.fixture
def book(fake_clock: FakeClock) -> TicketBook:
    return TicketBook(profile_id="p" * 16, monotonic=fake_clock.monotonic)


def refused(book: TicketBook, ticket: object, purpose: str = WS_INTERACTIVE) -> str:
    with pytest.raises(TicketRefused) as caught:
        book.redeem(ticket, purpose=purpose)
    return caught.value.reason


def ws_scope(
    *protocols: str,
    client: tuple[str, int] | None = ("127.0.0.1", 50123),
    origin: str | None = None,
    query: bytes = b"",
) -> dict[str, Any]:
    headers: list[tuple[bytes, bytes]] = [(b"host", b"127.0.0.1:8765")]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    if protocols:
        headers.append((b"sec-websocket-protocol", ", ".join(protocols).encode()))
    return {
        "type": "websocket",
        "path": "/ws",
        "client": client,
        "headers": headers,
        "query_string": query,
        "subprotocols": list(protocols),
    }


def offer(ticket: str) -> tuple[str, str]:
    return SUBPROTOCOL, TICKET_PROTOCOL_PREFIX + ticket


# -- the book -------------------------------------------------------------------------------------


def test_minted_ticket_shape_and_hidden_repr(book: TicketBook) -> None:
    minted = book.mint(WS_INTERACTIVE)
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", minted.ticket)
    assert minted.expires_in == 30.0 and minted.expires_at == 30.0
    assert minted.profile_id == book.profile_id and minted.instance_id == book.instance_id
    assert minted.ticket not in repr(minted)
    # Only a digest is kept, never the ticket.
    assert minted.ticket not in repr(vars(book))
    stored = list(book._entries)  # pyright: ignore[reportPrivateUsage]
    assert stored and all(isinstance(key, bytes) and len(key) == 32 for key in stored)
    assert book.mint(WS_INTERACTIVE).ticket != minted.ticket


def test_reuse_within_ttl_fails(book: TicketBook, fake_clock: FakeClock) -> None:
    minted = book.mint(WS_INTERACTIVE)
    grant = book.redeem(minted.ticket, purpose=WS_INTERACTIVE)
    assert grant.purpose == WS_INTERACTIVE and grant.profile_id == book.profile_id
    fake_clock.advance(1)
    assert refused(book, minted.ticket) == "used"


def test_expiry_at_thirty_seconds(book: TicketBook, fake_clock: FakeClock) -> None:
    early = book.mint(WS_INTERACTIVE)
    late = book.mint(WS_INTERACTIVE)
    fake_clock.advance(29.9)
    book.redeem(early.ticket, purpose=WS_INTERACTIVE)
    fake_clock.advance(0.1)
    assert refused(book, late.ticket) == "expired"
    assert refused(book, late.ticket) == "unknown"  # pruned once expired


def test_wrong_purpose_fails_and_spends_the_ticket(book: TicketBook) -> None:
    minted = book.mint(NATIVE_HTTP)
    assert refused(book, minted.ticket, WS_INTERACTIVE) == "wrong_purpose"
    assert refused(book, minted.ticket, NATIVE_HTTP) == "used"
    with pytest.raises(TicketRefused) as caught:
        book.mint("dashboard-root")
    assert caught.value.reason == "malformed"


def test_profile_binding(book: TicketBook) -> None:
    minted = book.mint(WS_INTERACTIVE)
    with pytest.raises(TicketRefused) as caught:
        book.redeem(minted.ticket, purpose=WS_INTERACTIVE, profile_id="q" * 16)
    assert caught.value.reason == "wrong_profile"
    other = book.mint(WS_INTERACTIVE)
    assert book.redeem(other.ticket, purpose=WS_INTERACTIVE, profile_id=book.profile_id)


def test_a_ticket_from_another_daemon_start_is_unknown(fake_clock: FakeClock) -> None:
    first = TicketBook(profile_id="p" * 16, monotonic=fake_clock.monotonic)
    second = TicketBook(profile_id="p" * 16, monotonic=fake_clock.monotonic)
    assert first.instance_id != second.instance_id
    minted = first.mint(WS_INTERACTIVE)
    assert refused(second, minted.ticket) == "unknown"


@pytest.mark.parametrize(
    "bad", [None, 7, b"x" * 43, "", "short", "a" * 42, "a" * 44, "a" * 42 + "=", "a" * 4096]
)
def test_malformed_tickets_are_refused_before_lookup(book: TicketBook, bad: object) -> None:
    assert refused(book, bad) == "malformed"
    assert book.spend(bad) is False


def test_capacity_and_revoke(fake_clock: FakeClock) -> None:
    book = TicketBook(profile_id="p" * 16, monotonic=fake_clock.monotonic, limit=3)
    minted = [book.mint(WS_INTERACTIVE) for _ in range(3)]
    assert book.outstanding() == 3
    with pytest.raises(TicketRefused) as caught:
        book.mint(WS_INTERACTIVE)
    assert caught.value.reason == "capacity"
    book.redeem(minted[0].ticket, purpose=WS_INTERACTIVE)
    assert book.outstanding() == 2
    book.mint(WS_INTERACTIVE)  # a used ticket frees its slot
    fake_clock.advance(30)
    assert book.outstanding() == 0
    book.mint(WS_INTERACTIVE)  # expired ones are pruned
    fresh = book.mint(WS_INTERACTIVE)
    book.revoke_all()
    assert book.outstanding() == 0
    assert refused(book, fresh.ticket) == "unknown"


def test_mint_and_burn_cannot_grow_the_book_without_bound(fake_clock: FakeClock) -> None:
    book = TicketBook(profile_id="p" * 16, monotonic=fake_clock.monotonic, limit=2)
    for _ in range(2 * ws_auth.SPENT_MARKERS_PER_SLOT):
        assert book.spend(book.mint(WS_INTERACTIVE).ticket)
    with pytest.raises(TicketRefused) as caught:
        book.mint(WS_INTERACTIVE)
    assert caught.value.reason == "capacity"
    fake_clock.advance(30)  # the markers expire with their tickets
    book.mint(WS_INTERACTIVE)


def test_spend_burns_a_live_ticket(book: TicketBook) -> None:
    minted = book.mint(WS_INTERACTIVE)
    assert book.spend(minted.ticket) is True
    assert book.spend(minted.ticket) is False
    assert refused(book, minted.ticket) == "used"


def test_stable_profile_id(tmp_path: Path) -> None:
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    assert stable_profile_id(one) == stable_profile_id(tmp_path / "x" / ".." / "one")
    assert stable_profile_id(one) != stable_profile_id(two)
    assert re.fullmatch(r"[0-9a-f]{16}", stable_profile_id(one))
    assert str(tmp_path) not in stable_profile_id(one)
    assert TicketBook.for_home(one).profile_id == stable_profile_id(one)


@pytest.mark.parametrize(
    ("host", "ok"),
    [
        ("127.0.0.1", True),
        ("127.8.0.3", True),
        ("::1", True),
        ("[::1]", True),
        ("::ffff:127.0.0.1", True),
        ("10.0.0.2", False),
        ("192.168.1.4", False),
        ("::ffff:10.0.0.1", False),
        ("localhost", False),
        ("testclient", False),
        ("", False),
        (None, False),
    ],
)
def test_loopback_hosts(host: object, ok: bool) -> None:
    assert is_loopback_host(host) is ok


# -- the upgrade ----------------------------------------------------------------------------------


def test_upgrade_accepts_once_and_echoes_only_the_base_protocol(book: TicketBook) -> None:
    minted = book.mint(WS_INTERACTIVE)
    verdict = authenticate_upgrade(ws_scope(*offer(minted.ticket)), WS_INTERACTIVE, book=book)
    assert isinstance(verdict, UpgradeAccepted)
    assert verdict.subprotocol == SUBPROTOCOL
    assert verdict.grant.instance_id == book.instance_id
    again = authenticate_upgrade(ws_scope(*offer(minted.ticket)), WS_INTERACTIVE, book=book)
    assert again == UpgradeRefused(CLOSE_BAD_TICKET, "ticket used")


def test_upgrade_reads_the_header_when_the_scope_has_no_list(book: TicketBook) -> None:
    minted = book.mint(WS_INTERACTIVE)
    scope = ws_scope(*offer(minted.ticket))
    del scope["subprotocols"]
    assert isinstance(authenticate_upgrade(scope, WS_INTERACTIVE, book=book), UpgradeAccepted)


@pytest.mark.parametrize("case", ["none", "two", "no_base", "wrong_purpose", "expired", "unknown"])
def test_upgrade_refuses_bad_tickets_with_4401(
    book: TicketBook, fake_clock: FakeClock, case: str
) -> None:
    a = book.mint(WS_INTERACTIVE)
    b = book.mint(WS_INTERACTIVE)
    protocols: tuple[str, ...] = offer(a.ticket)
    purpose = WS_INTERACTIVE
    if case == "none":
        protocols = (SUBPROTOCOL,)
    elif case == "two":
        protocols = (*offer(a.ticket), TICKET_PROTOCOL_PREFIX + b.ticket)
    elif case == "no_base":
        protocols = (TICKET_PROTOCOL_PREFIX + a.ticket,)
    elif case == "wrong_purpose":
        purpose = NATIVE_HTTP
    elif case == "expired":
        fake_clock.advance(30)
    elif case == "unknown":
        protocols = offer("Z" * 43)
    verdict = authenticate_upgrade(ws_scope(*protocols), purpose, book=book)
    assert isinstance(verdict, UpgradeRefused) and verdict.close_code == CLOSE_BAD_TICKET
    if case not in {"none", "unknown"}:
        # A ticket offered in a refused upgrade never works afterwards.
        assert refused(book, a.ticket) in {"used", "unknown"}


def test_upgrade_refuses_bad_origin(book: TicketBook) -> None:
    minted = book.mint(WS_INTERACTIVE)
    verdict = authenticate_upgrade(
        ws_scope(*offer(minted.ticket), origin="http://evil"), WS_INTERACTIVE, book=book
    )
    assert verdict == UpgradeRefused(CLOSE_FORBIDDEN, "origin not allowed")
    assert isinstance(verdict, UpgradeRefused) and "evil" not in verdict.reason
    assert refused(book, minted.ticket) == "used"
    null = book.mint(WS_INTERACTIVE)
    assert isinstance(
        authenticate_upgrade(
            ws_scope(*offer(null.ticket), origin="null"), WS_INTERACTIVE, book=book
        ),
        UpgradeRefused,
    )


def test_allow_listed_origin_is_accepted(book: TicketBook) -> None:
    minted = book.mint(WS_INTERACTIVE)
    verdict = authenticate_upgrade(
        ws_scope(*offer(minted.ticket), origin="http://127.0.0.1:5173/"),
        WS_INTERACTIVE,
        book=book,
        origins=["HTTP://127.0.0.1:5173"],
    )
    assert isinstance(verdict, UpgradeAccepted)


@pytest.mark.parametrize("client", [("10.1.2.3", 4000), ("testclient", 50000), None])
def test_upgrade_refuses_a_remote_peer(book: TicketBook, client: tuple[str, int] | None) -> None:
    minted = book.mint(WS_INTERACTIVE)
    verdict = authenticate_upgrade(
        ws_scope(*offer(minted.ticket), client=client), WS_INTERACTIVE, book=book
    )
    assert verdict == UpgradeRefused(CLOSE_FORBIDDEN, "peer is not loopback")
    assert refused(book, minted.ticket) == "used"


def test_query_string_ticket_is_ignored_and_not_logged(
    book: TicketBook, caplog: pytest.LogCaptureFixture
) -> None:
    minted = book.mint(WS_INTERACTIVE)
    with caplog.at_level(logging.DEBUG):
        verdict = authenticate_upgrade(
            ws_scope(SUBPROTOCOL, query=b"ticket=" + minted.ticket.encode()),
            WS_INTERACTIVE,
            book=book,
        )
    assert verdict == UpgradeRefused(CLOSE_BAD_TICKET, "exactly one ticket must be offered")
    assert "ignored" in caplog.text and minted.ticket not in caplog.text
    # Seen in a URL: spent, even when offered properly afterwards.
    again = authenticate_upgrade(ws_scope(*offer(minted.ticket)), WS_INTERACTIVE, book=book)
    assert again == UpgradeRefused(CLOSE_BAD_TICKET, "ticket used")


def test_ticket_absent_from_logs(
    book: TicketBook, fake_clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    tickets: list[str] = []
    with caplog.at_level(logging.DEBUG):
        cases: list[dict[str, Any]] = [
            {},
            {},
            {"origin": "http://evil"},
            {"client": ("8.8.8.8", 1)},
        ]
        for scope_kw in cases:
            minted = book.mint(WS_INTERACTIVE)
            tickets.append(minted.ticket)
            authenticate_upgrade(
                ws_scope(*offer(minted.ticket), **scope_kw), WS_INTERACTIVE, book=book
            )
        authenticate_upgrade(ws_scope(*offer(tickets[0])), WS_INTERACTIVE, book=book)
        late = book.mint(NATIVE_HTTP)
        tickets.append(late.ticket)
        fake_clock.advance(31)
        authenticate_upgrade(ws_scope(*offer(late.ticket)), NATIVE_HTTP, book=book)
        for value in tickets:
            with pytest.raises(TicketRefused) as caught:
                book.redeem(value, purpose=WS_INTERACTIVE)
            assert value not in str(caught.value)
    assert caplog.records  # the refusals were logged ...
    for value in tickets:  # ... without any ticket
        assert value not in caplog.text


def test_module_exports_are_stable() -> None:
    assert ws_auth.SUBPROTOCOL == "indus-gateway-v1"
    assert ws_auth.TICKET_PROTOCOL_PREFIX == "indus-gateway-ticket."
    assert (ws_auth.CLOSE_BAD_TICKET, ws_auth.CLOSE_FORBIDDEN) == (4401, 4403)
    assert ws_auth.TICKET_TTL_S == 30.0
