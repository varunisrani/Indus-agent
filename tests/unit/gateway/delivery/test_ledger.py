"""P15-19: the delivery ledger's rules over the real sender and store (fake clock)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.gateway.core.send import SendResult
from indus.gateway.delivery import ledger as ledger_mod
from indus.gateway.delivery.ledger import (
    DUPLICATE_PREFIX,
    FLOOD_PREFIX,
    RECONNECTED_PREFIX,
    DeliveryLedger,
)
from indus.gateway.outbound.sender import FinalReply
from indus.state import delivery_ledger as table
from indus.state import registry as store_registry
from indus.state.registry import StoreHandle
from tests.fakes.clock import FakeClock
from tests.unit.cli.commands.cmdrun import write_settings
from tests.unit.gateway.delivery.kit import US, ledgered, settle, until
from tests.unit.gateway.outbound.recording import fail

KEY = "@default:telegram:dm:100"
THREE_PIECES = " ".join(f"word{i:02d}" for i in range(24))  # three pieces at 80


@pytest.fixture
def store(scope: Scope) -> Iterator[StoreHandle]:
    handle = store_registry.open_unshared(scope)
    yield handle
    handle.close()


def _final(text: str = "Here is the answer.", ref: str = "s1:t1") -> FinalReply:
    return FinalReply(text, reply_to="in1", session_key=KEY, message_ref=ref)


def _only_row(store: StoreHandle) -> table.LedgerRow:
    rows = table.rows_for_session_key(store, KEY)
    assert len(rows) == 1
    return rows[0]


def test_a_final_is_recorded_then_delivered(scope: Scope, store: StoreHandle) -> None:
    adapter, sender, _ = ledgered(scope, store)
    points: list[str] = []
    report = asyncio.run(_with_hook(points, sender.deliver("100", _final())))
    assert report.outcome == "sent" and [op.text for op in adapter.ops] == ["Here is the answer."]
    row = _only_row(store)
    assert row.state == "delivered" and row.chat_id == "100" and row.platform == "telegram"
    assert points == ["before_pending", "pending", "attempting", "delivered"]


async def _with_hook(points: list[str], coro: Any) -> Any:
    ledger_mod.FAULT_HOOK = points.append
    try:
        return await coro
    finally:
        ledger_mod.FAULT_HOOK = None


def test_only_turn_finals_are_recorded(scope: Scope, store: StoreHandle) -> None:
    _, sender, _ = ledgered(scope, store)
    asyncio.run(sender.deliver("100", FinalReply("a command answer", reply_to="in1")))
    asyncio.run(
        sender.deliver(
            "100",
            FinalReply("[SILENT]", reply_expected=False, session_key=KEY, message_ref="s1:t9"),
        )
    )
    assert table.list_open(store) == [] and table.rows_for_session_key(store, KEY) == []


def test_secrets_never_reach_the_ledger(scope: Scope, store: StoreHandle) -> None:
    _, sender, _ = ledgered(scope, store)
    secret = "ghp_" + "Q7w" * 12
    asyncio.run(sender.deliver("100", _final(f"token {secret} done")))
    assert secret not in _only_row(store).content


def test_a_ledger_failure_never_blocks_the_send(
    scope: Scope, store: StoreHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_a: Any, **_k: Any) -> None:
        raise OSError("database is locked")

    monkeypatch.setattr(table, "record_pending", broken)
    adapter, sender, _ = ledgered(scope, store)
    report = asyncio.run(sender.deliver("100", _final()))
    assert report.outcome == "sent" and len(adapter.ops) == 1


def test_whole_chat_death_is_abandoned_and_never_retried(scope: Scope, store: StoreHandle) -> None:
    adapter, sender, book = ledgered(scope, store)
    adapter.send_results.append(fail("forbidden", "Forbidden: bot was blocked by the user"))
    report = asyncio.run(sender.deliver("100", _final()))
    assert report.outcome == "failed"
    assert _only_row(store).state == "abandoned" and book.next_retry_at is None


def test_refused_send_retries_after_30_then_120_then_waits_for_boot(
    scope: Scope, store: StoreHandle, fake_clock: FakeClock
) -> None:
    async def main() -> None:
        adapter, sender, book = ledgered(scope, store)
        too_long = fail("too_long", "Bad Request: message is too long")
        adapter.send_results.extend([too_long, too_long, too_long])
        start = fake_clock.now().timestamp()
        assert (await sender.deliver("100", _final())).outcome == "failed"
        row = _only_row(store)
        assert row.state == "failed" and row.error_kind == "refused"
        assert row.retry_at == start + 30 and book.next_retry_at == start + 30
        fake_clock.advance(29)
        await settle()
        assert len(adapter.ops) == 1
        fake_clock.advance(1)
        await until(lambda: len(adapter.ops) == 2)
        await until(lambda: _only_row(store).state == "failed")
        row = _only_row(store)
        assert row.attempts == 1 and row.retry_at == fake_clock.now().timestamp() + 120
        assert adapter.ops[1].text == "Here is the answer."  # refused: never a duplicate
        await until(lambda: book.next_retry_at is not None)
        fake_clock.advance(120)
        await until(lambda: len(adapter.ops) == 3)
        await until(lambda: _only_row(store).attempts == 2 and _only_row(store).state == "failed")
        await settle()
        assert _only_row(store).retry_at is None and book.next_retry_at is None
        fake_clock.advance(3600)
        await settle()
        assert len(adapter.ops) == 3  # the last attempt is kept for the next boot
        # the next boot (another process) uses it
        _, _, boot = ledgered(scope, store, owner=(5, 6), adapter=adapter)
        report = await boot.recover()
        assert report.plain == 1 and report.delivered == 1
        assert _only_row(store).state == "delivered" and _only_row(store).attempts == 3
        await book.aclose()

    asyncio.run(main())


def test_flood_waits_the_stated_time_capped_and_labels_the_retry(
    scope: Scope, store: StoreHandle, fake_clock: FakeClock
) -> None:
    async def main() -> None:
        adapter, sender, book = ledgered(scope, store)
        adapter.send_results.append(fail("rate_limited", "Too Many Requests", retry_after=97 * 60))
        start = fake_clock.now().timestamp()
        assert (await sender.deliver("100", _final())).outcome == "failed"
        row = _only_row(store)
        assert row.error_kind == "flood" and row.retry_at == start + 15 * 60 + 2
        assert adapter.ops[-1].text == "Here is the answer."  # no failure notice into a flood
        fake_clock.advance(15 * 60 + 2)
        await until(lambda: _only_row(store).state == "delivered")
        assert adapter.ops[-1].text.startswith(FLOOD_PREFIX.format(platform="Telegram"))
        assert adapter.ops[-1].text.endswith("Here is the answer.")
        await book.aclose()

    asyncio.run(main())


def test_flood_default_wait_without_a_stated_time(scope: Scope, store: StoreHandle) -> None:
    async def main() -> None:
        _, sender, book = ledgered(scope, store)
        row_id = await book.open(
            session_key=KEY, message_ref="r", platform="telegram", chat_id="1", content="x"
        )
        assert row_id is not None
        flood = SendResult(ok=False, error="flood", kind=fail("rate_limited").kind)
        now = scope.clock.now().timestamp()
        await book.failed(row_id, flood)
        assert table.get(store, row_id).retry_at == now + 62  # type: ignore[union-attr]
        await book.aclose()
        del sender

    asyncio.run(main())


def test_partial_split_resumes_only_the_undelivered_pieces(
    scope: Scope, store: StoreHandle, fake_clock: FakeClock
) -> None:
    async def main() -> None:
        adapter, sender, book = ledgered(scope, store, max_len=80)
        flood = fail("rate_limited", "Too Many Requests", retry_after=600)
        adapter.send_results.extend([SendResult.sent("x"), flood])
        report = await sender.deliver("100", _final(THREE_PIECES))
        assert report.outcome == "failed" and len(report.undelivered) == 2
        first = adapter.ops[0].text
        row = _only_row(store)
        assert row.pieces == tuple(report.undelivered)
        fake_clock.advance(602)
        await until(lambda: _only_row(store).state == "delivered")
        resent = [op.text for op in adapter.ops[2:]]
        # the label (split at this tiny limit), then only the two pieces still owed
        assert resent[0].startswith("(Resent after Telegram asked us to slow down")
        assert resent[-2:] == list(report.undelivered) and len(resent) <= 4
        assert first not in resent  # the piece that landed is never sent again
        await book.aclose()

    asyncio.run(main())


def test_reconnect_only_rows_wait_for_the_platform(scope: Scope, store: StoreHandle) -> None:
    async def main() -> None:
        adapter, _, book = ledgered(scope, store)
        row_id = await book.open(
            session_key=KEY, message_ref="r", platform="telegram", chat_id="100", content="hi"
        )
        assert row_id is not None
        await book.failed(row_id, fail("transient", "connect error"), reconnect_only=True)
        assert table.get(store, row_id).error_kind == "reconnect"  # type: ignore[union-attr]
        assert book.next_retry_at is None
        assert await book.platform_reconnected("slack") == 0
        assert await book.platform_reconnected("Telegram") == 1
        assert adapter.ops[-1].text == RECONNECTED_PREFIX.format(platform="Telegram") + "\n\nhi"
        assert table.get(store, row_id).state == "delivered"  # type: ignore[union-attr]

    asyncio.run(main())


def test_a_transient_failure_while_disconnected_is_reconnect_only(
    scope: Scope, store: StoreHandle
) -> None:
    async def main() -> None:
        adapter, sender, book = ledgered(scope, store)
        # the adapter is not connected; connect-level failures exhaust the inline retries
        adapter.send_results.extend([fail("transient", "connect error")] * 3)
        task = asyncio.ensure_future(sender.deliver("100", _final()))
        clock = scope.clock
        assert isinstance(clock, FakeClock)
        await until(task.done, clock)
        assert task.result().outcome == "failed"
        assert _only_row(store).error_kind == "reconnect"
        await book.aclose()

    asyncio.run(main())


# -- boot recovery -----------------------------------------------------------------------------


def _row(store: StoreHandle, ref: str, owner: table.Owner, platform: str = "telegram") -> str:
    row_id = table.obligation_id(KEY, ref, f"reply {ref}")
    table.record_pending(
        store,
        row_id=row_id,
        session_key=KEY,
        platform=platform,
        chat_id="100",
        content=f"reply {ref}",
        owner=owner,
    )
    return row_id


def test_boot_sweep_rules(scope: Scope, store: StoreHandle, fake_clock: FakeClock) -> None:
    dead, live = (11, 1), (12, 2)
    pending = _row(store, "pending", dead)
    attempting = _row(store, "attempting", dead)
    table.mark_attempting(store, attempting)
    uncertain = _row(store, "uncertain", dead)
    table.mark_failed(store, uncertain, error="read timeout", kind="uncertain")
    refused = _row(store, "refused", dead)
    table.mark_failed(store, refused, error="too long", kind="refused")
    owned = _row(store, "live", live)
    elsewhere = _row(store, "slack", dead, platform="slack")
    spent = _row(store, "spent", dead)
    for _ in range(3):
        table.claim(store, spent, expect_owner=dead, owner=dead)
    flood = _row(store, "flood", dead)
    table.mark_failed(
        store, flood, error="flood", kind="flood", retry_at=fake_clock.now().timestamp() + 300
    )

    async def main() -> None:
        adapter, _, book = ledgered(scope, store)
        book.is_alive = lambda pid, start: (pid, start) == live
        report = await book.recover()
        texts = [op.text for op in adapter.ops]
        assert sorted(texts) == sorted(
            [
                "reply pending",
                "reply refused",
                DUPLICATE_PREFIX + "\n\nreply attempting",
                DUPLICATE_PREFIX + "\n\nreply uncertain",
            ]
        )
        assert (report.plain, report.labelled, report.adopted) == (2, 2, 1)
        assert report.abandoned == 1 and report.delivered == 4
        for row_id in (pending, attempting, uncertain, refused):
            row = table.get(store, row_id)
            assert row is not None and row.state == "delivered" and row.owner == US
        assert table.get(store, owned).state == "pending"  # type: ignore[union-attr]
        untouched = table.get(store, elsewhere)
        assert untouched is not None and untouched.attempts == 0 and untouched.owner == dead
        assert table.get(store, spent).state == "abandoned"  # type: ignore[union-attr]
        adopted = table.get(store, flood)
        assert adopted is not None and adopted.attempts == 0 and adopted.owner == US
        assert book.next_retry_at == fake_clock.now().timestamp() + 300
        # a second boot finds nothing more to send
        second = ledgered(scope, store, owner=(77, 7), adapter=adapter)[2]
        second.is_alive = lambda pid, start: (pid, start) in (live, US)
        again = await second.recover()
        assert again.redelivered == 0 and again.adopted == 0
        fake_clock.advance(300)
        await until(lambda: table.get(store, flood).state == "delivered")  # type: ignore[union-attr]
        assert adapter.ops[-1].text.startswith(FLOOD_PREFIX.format(platform="Telegram"))
        await book.aclose()

    asyncio.run(main())


def test_rows_older_than_a_day_are_abandoned_at_boot(
    scope: Scope, store: StoreHandle, fake_clock: FakeClock
) -> None:
    old = _row(store, "old", (11, 1))
    fake_clock.advance(86400 + 1)

    async def main() -> None:
        adapter, _, book = ledgered(scope, store)
        report = await book.recover()
        assert report.abandoned == 1 and adapter.ops == []

    asyncio.run(main())
    assert table.get(store, old).state == "abandoned"  # type: ignore[union-attr]


def test_ledger_can_be_switched_off(scope: Scope, store: StoreHandle) -> None:
    assert isinstance(DeliveryLedger.for_scope(scope, store), DeliveryLedger)
    write_settings(scope, "gateway:\n  delivery_ledger: false\n")
    assert DeliveryLedger.for_scope(scope, store) is None


def test_owner_alive_uses_the_start_fingerprint() -> None:
    import os

    me = ledger_mod.current_owner()
    assert me[0] == os.getpid()
    assert ledger_mod.owner_alive(*me)
    if me[1] is not None:
        assert not ledger_mod.owner_alive(me[0], me[1] + 12345)  # a reused pid
    assert not ledger_mod.owner_alive(None, None)
    assert me != US
