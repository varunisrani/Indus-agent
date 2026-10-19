"""P15-19: the delivery ledger table (states, claims, retention)."""

from __future__ import annotations

from indus.state import delivery_ledger as ledger
from indus.state.registry import StoreHandle
from tests.unit.state._helpers_d import DAY, advance, count, now_of

KEY = "@default:telegram:dm:100"
US = (4242, 777)
DEAD = (9999, 111)


def _record(
    store: StoreHandle, ref: str = "s1:t1", content: str = "hello", owner: ledger.Owner = US
) -> ledger.LedgerRow:
    return ledger.record_pending(
        store,
        row_id=ledger.obligation_id(KEY, ref, content),
        session_key=KEY,
        platform="telegram",
        chat_id="100",
        thread_id="7",
        content=content,
        owner=owner,
    )


def test_obligation_id_is_stable_and_separates_turns_and_chats() -> None:
    first = ledger.obligation_id(KEY, "s1:t1", "hi")
    assert first == ledger.obligation_id(KEY, "s1:t1", "hi") and len(first) == 32
    assert first != ledger.obligation_id(KEY, "s1:t2", "hi")
    assert first != ledger.obligation_id("@default:telegram:dm:101", "s1:t1", "hi")
    assert first != ledger.obligation_id(KEY, "s1:t1", "hi!")


def test_record_and_walk_the_states(store: StoreHandle) -> None:
    row = _record(store)
    assert row.state == "pending" and row.attempts == 0 and row.owner == US
    assert row.thread_id == "7" and row.pieces == () and row.created_at == now_of(store)
    assert ledger.mark_attempting(store, row.id)
    assert ledger.get(store, row.id).state == "attempting"  # type: ignore[union-attr]
    assert ledger.mark_failed(
        store, row.id, error="x" * 900, kind="refused", retry_at=5.0, pieces=["b", "c"]
    )
    failed = ledger.get(store, row.id)
    assert failed is not None and failed.state == "failed" and failed.error_kind == "refused"
    assert failed.pieces == ("b", "c") and failed.retry_at == 5.0
    assert failed.last_error is not None and len(failed.last_error) == 500
    # a later failure without pieces keeps the ones still owed
    ledger.mark_failed(store, row.id, error="again", kind="refused")
    assert ledger.get(store, row.id).pieces == ("b", "c")  # type: ignore[union-attr]
    assert ledger.mark_delivered(store, row.id)
    done = ledger.get(store, row.id)
    assert done is not None and done.state == "delivered" and done.pieces == ()
    assert ledger.list_open(store) == []
    assert [r.id for r in ledger.rows_for_session_key(store, KEY)] == [row.id]


def test_recording_the_same_final_again_resets_one_row(store: StoreHandle) -> None:
    row = _record(store)
    ledger.mark_failed(store, row.id, error="boom", kind="refused")
    again = _record(store)
    assert again.id == row.id and again.state == "pending" and again.last_error is None
    assert count(store, "SELECT COUNT(*) FROM delivery_ledger") == 1


def test_claim_is_compare_and_set_on_the_owner(store: StoreHandle) -> None:
    row = _record(store, owner=DEAD)
    assert ledger.claim(store, row.id, expect_owner=US, owner=US) is None  # wrong owner
    before = ledger.claim(store, row.id, expect_owner=DEAD, owner=US)
    assert before is not None and before.state == "pending" and before.owner == DEAD
    after = ledger.get(store, row.id)
    assert after is not None and after.state == "attempting" and after.attempts == 1
    assert after.owner == US
    # a second sweeper with the old view loses
    assert ledger.claim(store, row.id, expect_owner=DEAD, owner=(1, 2)) is None
    # only listed states are claimed
    assert ledger.claim(store, row.id, expect_owner=US, owner=US, states=("failed",)) is None


def test_adopt_restamps_without_spending_an_attempt(store: StoreHandle) -> None:
    row = _record(store, owner=DEAD)
    ledger.mark_failed(store, row.id, error="flood", kind="flood", retry_at=now_of(store) + 60)
    assert ledger.adopt(store, row.id, expect_owner=DEAD, owner=US)
    adopted = ledger.get(store, row.id)
    assert adopted is not None and adopted.owner == US and adopted.attempts == 0
    assert adopted.state == "failed" and adopted.error_kind == "flood"
    assert not ledger.adopt(store, row.id, expect_owner=DEAD, owner=US)


def test_stale_rows_are_abandoned(store: StoreHandle) -> None:
    old = _record(store, ref="old")
    advance(store, DAY + 1)
    fresh = _record(store, ref="fresh")
    assert ledger.abandon_older_than(store, now_of(store) - DAY) == 1
    assert ledger.get(store, old.id).state == "abandoned"  # type: ignore[union-attr]
    assert ledger.get(store, fresh.id).state == "pending"  # type: ignore[union-attr]


def test_retention_prunes_old_terminal_rows(store: StoreHandle) -> None:
    done = _record(store, ref="done")
    ledger.mark_delivered(store, done.id)
    gone = _record(store, ref="gone")
    ledger.abandon(store, gone.id, "chat gone")
    open_row = _record(store, ref="open")
    advance(store, 7 * DAY + 1)
    _record(store, ref="new")  # recording runs retention
    ids = {r.id for r in ledger.rows_for_session_key(store, KEY)}
    assert done.id not in ids and gone.id not in ids and open_row.id in ids


def test_the_501st_row_prunes_the_oldest_delivered_first(store: StoreHandle) -> None:
    first_delivered = _record(store, ref="r0")
    ledger.mark_delivered(store, first_delivered.id)
    advance(store, 1)
    first_open = _record(store, ref="r1")
    for i in range(2, ledger.MAX_ROWS):
        advance(store, 1)
        row = _record(store, ref=f"r{i}")
        if i % 2 == 0:
            ledger.mark_delivered(store, row.id)
    assert count(store, "SELECT COUNT(*) FROM delivery_ledger") == ledger.MAX_ROWS
    advance(store, 1)
    _record(store, ref="the-501st")
    assert count(store, "SELECT COUNT(*) FROM delivery_ledger") == ledger.MAX_ROWS
    assert ledger.get(store, first_delivered.id) is None
    assert ledger.get(store, first_open.id) is not None  # open rows outlive delivered ones


def test_list_open_filters_by_platform(store: StoreHandle) -> None:
    _record(store)
    assert len(ledger.list_open(store, platform="telegram")) == 1
    assert ledger.list_open(store, platform="slack") == []
