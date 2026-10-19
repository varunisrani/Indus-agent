"""P17-01: the runtime ledger (admissions FIFO, owner epoch, unknown turns, retirement)."""

from __future__ import annotations

import threading
from typing import Any

import pytest

from indus.state import deletion, sessions
from indus.state import runtime_ledger as ledger
from indus.state.registry import StoreHandle
from indus.state.runtime_ledger import LedgerRefusal
from tests.unit.state._helpers_d import DAY, advance

ME = "cli:alice"


def _admit(
    store: StoreHandle,
    epoch: int,
    request_id: str,
    text: str = "hello",
    *,
    session_id: str = "s1",
    principal_id: str = ME,
    **kwargs: Any,
) -> ledger.Admission:
    return ledger.admit(
        store,
        epoch=epoch,
        principal_id=principal_id,
        session_id=session_id,
        request_id=request_id,
        payload={"text": text},
        **kwargs,
    )


def _epoch(store: StoreHandle) -> int:
    return ledger.begin_owner_epoch(store, "daemon-1").epoch


def _refusal(exc: pytest.ExceptionInfo[LedgerRefusal]) -> str:
    return exc.value.reason


def test_exact_retry_returns_the_same_row(store: StoreHandle) -> None:
    epoch = _epoch(store)
    first = _admit(store, epoch, "r1")
    assert first.created and first.row.state == "queued" and first.row.owner_epoch == epoch
    again = _admit(store, epoch, "r1")
    assert not again.created and again.row == first.row
    # the key holds in every state: a started row answers its retry too
    claim = ledger.claim_head(store, epoch=epoch, session_id="s1")
    assert claim.kind == "claimed"
    retry = _admit(store, epoch, "r1")
    assert retry.row.admission_id == first.row.admission_id and retry.row.state == "started"
    assert ledger.find(store, ME, "s1", "r1") == retry.row
    assert ledger.get(store, first.row.admission_id) == retry.row


def test_changed_payload_is_a_conflict(store: StoreHandle) -> None:
    epoch = _epoch(store)
    _admit(store, epoch, "r1", "hello")
    with pytest.raises(LedgerRefusal) as exc:
        _admit(store, epoch, "r1", "goodbye")
    assert _refusal(exc) == "admission_conflict" and exc.value.code == "runtime.admission_conflict"
    # another principal or another session is another key
    assert _admit(store, epoch, "r1", "goodbye", principal_id="tui:bob").created
    assert _admit(store, epoch, "r1", "goodbye", session_id="s2").created


def test_digest_follows_target_and_canonical_payload() -> None:
    one = ledger.payload_digest("s1", {"text": "a", "files": [1, 2]})
    assert one == ledger.payload_digest("s1", {"files": [1, 2], "text": "a"})
    assert one != ledger.payload_digest("s2", {"text": "a", "files": [1, 2]})
    assert one != ledger.payload_digest("s1", {"text": "a", "files": [2, 1]})


def test_invalid_input_is_refused(store: StoreHandle) -> None:
    epoch = _epoch(store)
    cases: list[dict[str, Any]] = [
        {"principal_id": "", "session_id": "s1", "request_id": "r", "payload": {}},
        {"principal_id": ME, "session_id": "x" * 1025, "request_id": "r", "payload": {}},
        {"principal_id": ME, "session_id": "s1", "request_id": "r", "payload": ["no"]},
        {"principal_id": ME, "session_id": "s1", "request_id": "r", "payload": {"n": float("nan")}},
        {"principal_id": ME, "session_id": "s1", "request_id": "r", "payload": {"t": "x" * 2**20}},
    ]
    for case in cases:
        with pytest.raises(LedgerRefusal) as exc:
            ledger.admit(store, epoch=epoch, **case)
        assert _refusal(exc) == "invalid_params"
    assert ledger.pending(store, "s1") == []


def test_the_33rd_queued_input_is_refused(store: StoreHandle) -> None:
    epoch = _epoch(store)
    for n in range(ledger.QUEUE_LIMIT):
        _admit(store, epoch, f"r{n}", f"m{n}")
    with pytest.raises(LedgerRefusal) as exc:
        _admit(store, epoch, "r32", "one too many")
    assert _refusal(exc) == "queue_full"
    assert len(ledger.pending(store, "s1")) == ledger.QUEUE_LIMIT
    assert ledger.find(store, ME, "s1", "r32") is None  # the refusal wrote nothing
    # an exact retry of an admitted input still answers while the queue is full
    assert _admit(store, epoch, "r0", "m0").row.request_id == "r0"
    # the started row does not count: claiming one frees a place
    assert ledger.claim_head(store, epoch=epoch, session_id="s1").kind == "claimed"
    assert _admit(store, epoch, "r32", "one too many").created


def test_concurrent_admits_of_one_key_make_one_row(store: StoreHandle) -> None:
    epoch = _epoch(store)
    barrier = threading.Barrier(4)
    results: list[ledger.Admission] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        got = _admit(store, epoch, "same", "hi")
        with lock:
            results.append(got)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(results) == 4
    assert len({r.row.admission_id for r in results}) == 1
    assert sum(r.created for r in results) == 1
    assert len(ledger.pending(store, "s1")) == 1


def test_claim_kinds(store: StoreHandle) -> None:
    epoch = _epoch(store)
    assert ledger.claim_head(store, epoch=epoch, session_id="s1").kind == "idle"
    a = _admit(store, epoch, "a", "first").row
    b = _admit(store, epoch, "b", "second").row
    moved = ledger.claim_head(store, epoch=epoch, session_id="s1", expect_head=b.admission_id)
    assert (
        moved.kind == "moved" and moved.row is not None and moved.row.admission_id == a.admission_id
    )
    claimed = ledger.claim_head(store, epoch=epoch, session_id="s1", expect_head=a.admission_id)
    assert claimed.kind == "claimed" and claimed.row is not None
    assert claimed.row.admission_id == a.admission_id and claimed.row.generation == 1
    running = ledger.claim_head(store, epoch=epoch, session_id="s1")
    assert running.kind == "running" and running.row is not None
    assert running.row.admission_id == a.admission_id
    ledger.settle(
        store, epoch=epoch, admission_id=a.admission_id, generation=1, outcome="completed"
    )
    second = ledger.claim_head(store, epoch=epoch, session_id="s1")
    assert second.kind == "claimed" and second.row is not None
    assert second.row.admission_id == b.admission_id and second.row.generation == 2
    assert ledger.generation_of(store, "s1") == 2 and ledger.generation_of(store, "s9") == 0


def test_fifo_positions_and_sessions_with_queued(store: StoreHandle) -> None:
    epoch = _epoch(store)
    rows = [_admit(store, epoch, f"r{n}", f"m{n}").row for n in range(3)]
    _admit(store, epoch, "x", session_id="s2")
    pending = ledger.pending(store, "s1")
    assert [ledger.queue_position(pending, r.admission_id) for r in rows] == [1, 2, 3]
    assert ledger.sessions_with_queued(store) == ["s1", "s2"]
    ledger.claim_head(store, epoch=epoch, session_id="s1")
    pending = ledger.pending(store, "s1")
    assert ledger.queue_position(pending, rows[0].admission_id) is None
    assert ledger.queue_position(pending, rows[2].admission_id) == 2
    assert ledger.payload_text(rows[1]) == "m1"


def test_settle_is_fenced_by_generation(store: StoreHandle) -> None:
    epoch = _epoch(store)
    a = _admit(store, epoch, "a").row
    claim = ledger.claim_head(store, epoch=epoch, session_id="s1")
    assert claim.row is not None and claim.row.generation == 1
    with pytest.raises(LedgerRefusal) as exc:
        ledger.settle(
            store, epoch=epoch, admission_id=a.admission_id, generation=0, outcome="completed"
        )
    assert _refusal(exc) == "stale_generation"
    with pytest.raises(LedgerRefusal) as exc:
        odd: Any = "weird"
        ledger.settle(store, epoch=epoch, admission_id=a.admission_id, generation=1, outcome=odd)
    assert _refusal(exc) == "invalid_params"
    done = ledger.settle(
        store,
        epoch=epoch,
        admission_id=a.admission_id,
        generation=1,
        outcome="completed",
        final_text="the answer",
        detail="ok\n\x1b[31mred\tline",
    )
    assert done.state == "terminal" and done.outcome == "completed"
    assert done.final_text == "the answer" and done.detail == "ok [31mred line"
    # the same settle again is answered with the settled row
    again = ledger.settle(
        store, epoch=epoch, admission_id=a.admission_id, generation=1, outcome="failed"
    )
    assert again == done
    with pytest.raises(LedgerRefusal) as exc:
        ledger.settle(store, epoch=epoch, admission_id="nope", generation=1, outcome="completed")
    assert _refusal(exc) == "not_found"


def test_owner_epoch_bumps_and_stale_epochs_are_refused(store: StoreHandle) -> None:
    assert ledger.current_epoch(store) is None
    with pytest.raises(LedgerRefusal) as exc:
        _admit(store, 1, "r")
    assert _refusal(exc) == "stale_epoch"
    first = ledger.begin_owner_epoch(store, "daemon-a")
    second = ledger.begin_owner_epoch(store, "daemon-b")
    assert (first.epoch, second.epoch) == (1, 2) and ledger.current_epoch(store) == 2
    for call in (
        lambda: _admit(store, first.epoch, "r"),
        lambda: ledger.claim_head(store, epoch=first.epoch, session_id="s1"),
        lambda: ledger.cancel(store, epoch=first.epoch, admission_id="x"),
    ):
        with pytest.raises(LedgerRefusal) as exc:
            call()
        assert _refusal(exc) == "stale_epoch"
    with pytest.raises(LedgerRefusal):
        ledger.begin_owner_epoch(store, "")


def test_restart_flips_started_to_unknown_and_blocks_the_next_row(store: StoreHandle) -> None:
    epoch = _epoch(store)
    lost = _admit(store, epoch, "lost", "do the risky thing").row
    later = _admit(store, epoch, "later", "and then this").row
    _admit(store, epoch, "other", session_id="s2")
    claim = ledger.claim_head(store, epoch=epoch, session_id="s1")
    assert claim.kind == "claimed"
    # the daemon dies here; the next one starts
    start = ledger.begin_owner_epoch(store, "daemon-2")
    assert start.epoch == epoch + 1 and start.unknown == (lost.admission_id,)
    row = ledger.get(store, lost.admission_id)
    assert row is not None and row.state == "unknown" and row.generation == 1
    blocked = ledger.claim_head(store, epoch=start.epoch, session_id="s1")
    assert blocked.kind == "blocked" and blocked.row is not None
    assert blocked.row.admission_id == lost.admission_id
    queued = ledger.get(store, later.admission_id)
    assert queued is not None and queued.state == "queued"
    # other sessions are not held up
    assert ledger.claim_head(store, epoch=start.epoch, session_id="s2").kind == "claimed"
    # the unknown turn is never replayed: a third start leaves it unknown, not started
    third = ledger.begin_owner_epoch(store, "daemon-3")
    assert lost.admission_id not in third.unknown
    still = ledger.get(store, lost.admission_id)
    assert still is not None and still.state == "unknown"
    # an exact retry of the lost input answers with the unknown row, no new admission
    retry = _admit(store, third.epoch, "lost", "do the risky thing")
    assert not retry.created and retry.row.state == "unknown"


def test_queue_cap_holds_while_the_fifo_is_paused(store: StoreHandle) -> None:
    epoch = _epoch(store)
    _admit(store, epoch, "lost")
    ledger.claim_head(store, epoch=epoch, session_id="s1")
    epoch = ledger.begin_owner_epoch(store, "d2").epoch
    for n in range(ledger.QUEUE_LIMIT):
        _admit(store, epoch, f"q{n}", f"m{n}")
    with pytest.raises(LedgerRefusal) as exc:
        _admit(store, epoch, "q32", "x")
    assert _refusal(exc) == "queue_full"


def test_discard_unknown_needs_the_generation(store: StoreHandle) -> None:
    epoch = _epoch(store)
    lost = _admit(store, epoch, "lost").row
    nxt = _admit(store, epoch, "next").row
    ledger.claim_head(store, epoch=epoch, session_id="s1")
    epoch = ledger.begin_owner_epoch(store, "d2").epoch
    with pytest.raises(LedgerRefusal) as exc:
        ledger.resolve_unknown(store, epoch=epoch, admission_id=lost.admission_id, generation=0)
    assert _refusal(exc) == "stale_generation"
    with pytest.raises(LedgerRefusal) as exc:
        ledger.resolve_unknown(store, epoch=epoch, admission_id=nxt.admission_id, generation=1)
    assert _refusal(exc) == "stale_generation"  # not an unknown row
    with pytest.raises(LedgerRefusal) as exc:
        ledger.cancel(store, epoch=epoch, admission_id=lost.admission_id)
    assert _refusal(exc) == "unknown_execution"
    done = ledger.resolve_unknown(store, epoch=epoch, admission_id=lost.admission_id, generation=1)
    assert done.state == "terminal" and done.outcome == "discarded"
    again = ledger.resolve_unknown(store, epoch=epoch, admission_id=lost.admission_id, generation=1)
    assert again == done
    claim = ledger.claim_head(store, epoch=epoch, session_id="s1")
    assert claim.kind == "claimed" and claim.row is not None
    assert claim.row.admission_id == nxt.admission_id and claim.row.generation == 2


def test_cancel_rules(store: StoreHandle) -> None:
    epoch = _epoch(store)
    a = _admit(store, epoch, "a").row
    b = _admit(store, epoch, "b").row
    ledger.claim_head(store, epoch=epoch, session_id="s1")
    with pytest.raises(LedgerRefusal) as exc:
        ledger.cancel(store, epoch=epoch, admission_id=a.admission_id)
    assert _refusal(exc) == "admission_started"
    cancelled = ledger.cancel(store, epoch=epoch, admission_id=b.admission_id)
    assert cancelled.state == "terminal" and cancelled.outcome == "cancelled"
    assert ledger.cancel(store, epoch=epoch, admission_id=b.admission_id) == cancelled
    with pytest.raises(LedgerRefusal) as exc:
        ledger.cancel(store, epoch=epoch, admission_id="missing")
    assert _refusal(exc) == "not_found"


def test_steer_transfer_settles_with_the_claim(store: StoreHandle) -> None:
    epoch = _epoch(store)
    a = _admit(store, epoch, "a").row
    b = _admit(store, epoch, "b", "also this").row
    c = _admit(store, epoch, "c", "and this").row
    with pytest.raises(LedgerRefusal) as exc:
        ledger.steer_into(store, epoch=epoch, admission_id=b.admission_id, generation=1)
    assert _refusal(exc) == "session_idle"
    ledger.claim_head(store, epoch=epoch, session_id="s1")
    with pytest.raises(LedgerRefusal) as exc:
        ledger.steer_into(store, epoch=epoch, admission_id=a.admission_id, generation=1)
    assert _refusal(exc) == "admission_started"
    moved, committed = ledger.steer_into(
        store, epoch=epoch, admission_id=b.admission_id, generation=1
    )
    assert committed and moved.state == "terminal" and moved.outcome == "steering"
    replay, again = ledger.steer_into(store, epoch=epoch, admission_id=b.admission_id, generation=1)
    assert not again and replay.admission_id == b.admission_id
    with pytest.raises(LedgerRefusal) as exc:
        ledger.cancel(store, epoch=epoch, admission_id=b.admission_id)
    assert _refusal(exc) == "admission_started"
    ledger.steer_into(store, epoch=epoch, admission_id=c.admission_id, generation=1)
    # b reached the turn, c did not: b settles steered, c goes back to its FIFO place
    ledger.settle(
        store,
        epoch=epoch,
        admission_id=a.admission_id,
        generation=1,
        outcome="completed",
        transfers={b.admission_id: True, c.admission_id: False},
    )
    steered = ledger.get(store, b.admission_id)
    assert steered is not None and steered.outcome == "steered"
    back = ledger.get(store, c.admission_id)
    assert back is not None and back.state == "queued" and back.seq == c.seq
    nxt = ledger.claim_head(store, epoch=epoch, session_id="s1")
    assert nxt.row is not None and nxt.row.admission_id == c.admission_id


def test_steer_transfer_into_a_turn_that_did_not_complete_is_interrupted(
    store: StoreHandle,
) -> None:
    epoch = _epoch(store)
    a = _admit(store, epoch, "a").row
    b = _admit(store, epoch, "b").row
    ledger.claim_head(store, epoch=epoch, session_id="s1")
    ledger.steer_into(store, epoch=epoch, admission_id=b.admission_id, generation=1)
    ledger.settle(
        store,
        epoch=epoch,
        admission_id=a.admission_id,
        generation=1,
        outcome="interrupted",
        transfers={b.admission_id: True},
    )
    row = ledger.get(store, b.admission_id)
    assert row is not None and row.outcome == "interrupted"


def test_steer_transfer_revert_keeps_the_fifo_place(store: StoreHandle) -> None:
    epoch = _epoch(store)
    _admit(store, epoch, "a")
    b = _admit(store, epoch, "b").row
    c = _admit(store, epoch, "c").row
    ledger.claim_head(store, epoch=epoch, session_id="s1")
    ledger.steer_into(store, epoch=epoch, admission_id=b.admission_id, generation=1)
    assert ledger.revert_steer(store, epoch=epoch, admission_id=b.admission_id, generation=1)
    assert not ledger.revert_steer(store, epoch=epoch, admission_id=b.admission_id, generation=1)
    pending = ledger.pending(store, "s1")
    assert ledger.queue_position(pending, b.admission_id) == 1
    assert ledger.queue_position(pending, c.admission_id) == 2


def test_steer_transfer_of_a_lost_claim_is_interrupted_on_restart(store: StoreHandle) -> None:
    epoch = _epoch(store)
    _admit(store, epoch, "a")
    b = _admit(store, epoch, "b").row
    ledger.claim_head(store, epoch=epoch, session_id="s1")
    ledger.steer_into(store, epoch=epoch, admission_id=b.admission_id, generation=1)
    start = ledger.begin_owner_epoch(store, "d2")
    assert start.interrupted_steers == (b.admission_id,)
    row = ledger.get(store, b.admission_id)
    assert row is not None and row.state == "terminal" and row.outcome == "interrupted"


def test_replace_queued_is_atomic(store: StoreHandle) -> None:
    epoch = _epoch(store)
    old = _admit(store, epoch, "r1", "first try").row
    other = _admit(store, epoch, "r2", "unrelated").row
    new = _admit(store, epoch, "r3", "better wording", replaces=old.admission_id)
    assert new.created and new.row.replaces == old.admission_id
    gone = ledger.get(store, old.admission_id)
    assert gone is not None and gone.state == "terminal" and gone.outcome == "replaced"
    order = [r.admission_id for r in ledger.pending(store, "s1")]
    assert order == [other.admission_id, new.row.admission_id]
    # the exact retry answers only for the same replaced row
    again = _admit(store, epoch, "r3", "better wording", replaces=old.admission_id)
    assert not again.created and again.row.admission_id == new.row.admission_id
    with pytest.raises(LedgerRefusal) as exc:
        _admit(store, epoch, "r3", "better wording", replaces=other.admission_id)
    assert _refusal(exc) == "admission_conflict"
    # a started row cannot be replaced, and the refusal admits nothing
    ledger.claim_head(store, epoch=epoch, session_id="s1")
    with pytest.raises(LedgerRefusal) as exc:
        _admit(store, epoch, "r4", "too late", replaces=other.admission_id)
    assert _refusal(exc) == "admission_started"
    assert ledger.find(store, ME, "s1", "r4") is None


def test_retired_id_stays_retired_after_an_exact_retry(store: StoreHandle) -> None:
    epoch = _epoch(store)
    a = _admit(store, epoch, "r1", "question").row
    ledger.claim_head(store, epoch=epoch, session_id="s1")
    ledger.settle(
        store,
        epoch=epoch,
        admission_id=a.admission_id,
        generation=1,
        outcome="completed",
        final_text="answer",
    )
    queued = _admit(store, epoch, "r2", "still waiting").row
    advance(store, ledger.RETENTION_SECONDS + DAY)
    assert ledger.prune(store) == 1  # terminal rows only
    assert ledger.get(store, queued.admission_id) is not None
    retired = ledger.get(store, a.admission_id)
    assert retired is not None and retired.retired and retired.state == "terminal"
    assert retired.outcome == "completed" and retired.final_text == "answer"
    for _ in range(2):
        retry = _admit(store, epoch, "r1", "question")
        assert not retry.created and retry.row.retired
        assert retry.row.admission_id == a.admission_id and retry.row.final_text == "answer"
    assert ledger.pending(store, "s1") == [ledger.get(store, queued.admission_id)]
    with pytest.raises(LedgerRefusal) as exc:
        _admit(store, epoch, "r1", "a different question")
    assert _refusal(exc) == "admission_conflict"
    assert ledger.retire(store, [queued.admission_id]) == 0  # not terminal: kept


def test_deleted_session_refuses_new_input(store: StoreHandle) -> None:
    epoch = _epoch(store)
    sessions.create(store, "s1", "cli")
    done = _admit(store, epoch, "r1", "hi").row
    ledger.claim_head(store, epoch=epoch, session_id="s1")
    ledger.settle(
        store, epoch=epoch, admission_id=done.admission_id, generation=1, outcome="completed"
    )
    waiting = _admit(store, epoch, "r2", "later").row
    assert deletion.delete_session(store, "s1")
    assert ledger.pending(store, "s1") == []
    marker = ledger.get(store, waiting.admission_id)
    assert marker is not None and marker.retired and marker.outcome == "cancelled"
    assert _admit(store, epoch, "r1", "hi").row.retired  # exact retry: the receipt
    with pytest.raises(LedgerRefusal) as exc:
        _admit(store, epoch, "r3", "fresh")
    assert _refusal(exc) == "retired"
    assert ledger.generation_of(store, "s1") == 0


def test_refusal_detail_is_cleaned() -> None:
    err = LedgerRefusal(
        "invalid_params", field="pay\nload", detail="line one\n\nline\x00 two‮" + "x" * 300
    )
    assert err.field == "pay load"
    assert err.detail_text is not None and "\n" not in err.detail_text
    assert "\x00" not in err.detail_text and "‮" not in err.detail_text
    assert len(err.detail_text) == 160 and err.detail_text.startswith("line one line two")
    assert str(err).startswith("invalid_params (pay load): line one")
    assert ledger.clean_text(None, 10) is None and ledger.clean_text(" \n ", 10) is None


def test_refusal_detail_hides_credentials_and_paths() -> None:
    token = "sk-" + "q" * 28  # built at runtime: no credential literal in the tree
    err = LedgerRefusal(
        "invalid_params",
        detail=f"cannot read /home/alice/.indus/auth.json with {token} (C:\\Users\\bob\\key.pem)",
    )
    text = err.detail_text
    assert text is not None and token not in text and "alice" not in text and "bob" not in text
    assert "\u2026/auth.json" in text and "\u2026/key.pem" in text
    kept = ledger.clean_text("see https://example.com/a/b and 3/4/5 or a/b", 160)
    assert kept == "see https://example.com/a/b and 3/4/5 or a/b"


class Draining(Exception):
    pass


def test_owner_gates_run_inside_the_transaction(store: StoreHandle) -> None:
    epoch = _epoch(store)
    first = _admit(store, epoch, "r1").row
    calls: list[str] = []

    def refuse(conn: object) -> None:
        calls.append("gate")
        raise Draining("the daemon is draining")

    with pytest.raises(Draining):
        _admit(store, epoch, "r2", authorize=refuse)
    assert ledger.find(store, ME, "s1", "r2") is None
    # an exact retry cannot create work, so it never meets the gate
    assert _admit(store, epoch, "r1", authorize=refuse).row == first
    assert calls == ["gate"]
    with pytest.raises(Draining):
        ledger.claim_head(store, epoch=epoch, session_id="s1", guard=refuse)
    still = ledger.get(store, first.admission_id)
    assert still is not None and still.state == "queued" and ledger.generation_of(store, "s1") == 0


def test_discard_runs_the_owner_write_in_its_transaction(store: StoreHandle) -> None:
    epoch = _epoch(store)
    lost = _admit(store, epoch, "lost").row
    ledger.claim_head(store, epoch=epoch, session_id="s1")
    epoch = ledger.begin_owner_epoch(store, "d2").epoch
    seen: list[str] = []

    def broken(conn: object, row: ledger.AdmissionRow) -> None:
        seen.append(row.admission_id)
        raise OSError("transcript unwritable")

    with pytest.raises(OSError):
        ledger.resolve_unknown(
            store, epoch=epoch, admission_id=lost.admission_id, generation=1, terminal_write=broken
        )
    row = ledger.get(store, lost.admission_id)
    assert row is not None and row.state == "unknown" and seen == [lost.admission_id]
    done = ledger.resolve_unknown(
        store,
        epoch=epoch,
        admission_id=lost.admission_id,
        generation=1,
        terminal_write=lambda conn, row: seen.append("closed"),
    )
    assert done.outcome == "discarded" and seen[-1] == "closed"
