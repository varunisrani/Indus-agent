"""Compression records: lock, watermark, atomic publish with tail clone, counters, chains."""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any

import pytest

from indus.foundation.errors import StoreError, StoreLocked
from indus.state import compression, holders, lineage
from indus.state.compression import (
    CompressionBusy,
    CompressionClosed,
    CompressionParentEnded,
)
from indus.state.handoff import HANDOFF_FINISH
from indus.state.registry import StoreHandle
from tests.unit.state._helpers_b import (
    add_message,
    add_session,
    live_contents,
    message_rows,
    now,
    session,
    update_session,
)
from tests.unit.state.conftest import AutoClock

SUMMARY = [
    {"role": "user", "content": "summary of the earlier talk"},
    {"role": "assistant", "content": "noted, carrying on"},
]
PAYLOAD = (
    "role",
    "content",
    "tool_call_id",
    "tool_calls",
    "tool_name",
    "timestamp",
    "token_count",
    "finish_reason",
    "reasoning",
    "reasoning_details",
    "api_content",
    "message_uid",
    "tool_call_uids",
)


@pytest.fixture
def parent(store: StoreHandle) -> str:
    add_session(
        store,
        "p1",
        model="model-a",
        model_config={"_branched_from": "elsewhere", "temperature": 0.2},
        billing_provider="prov",
        billing_base_url="https://api.example.test",
        cwd="/work/repo",
        git_root="/work/repo",
        profile_id="work",
        user_id="u1",
        pinned=1,
    )
    for i in range(6):
        add_message(store, "p1", "user" if i % 2 == 0 else "assistant", f"turn {i}")
    return "p1"


@pytest.fixture
def same_ns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(holders, "local_pid_namespace", lambda: "4026531836")
    monkeypatch.setattr(holders, "_host", lambda: "box-a")


def lock_row(store: StoreHandle, sid: str) -> dict[str, Any] | None:
    with store.read() as conn:
        row = conn.execute(
            "SELECT * FROM compression_locks WHERE session_id = ?", (sid,)
        ).fetchone()
    return None if row is None else dict(row)


# -- lock ---------------------------------------------------------------------------------------


def test_second_holder_is_refused(store: StoreHandle, parent: str) -> None:
    assert compression.acquire(store, parent, "worker-a")
    assert not compression.acquire(store, parent, "worker-b")
    assert compression.acquire(store, parent, "worker-a")  # the owner asking again
    assert compression.holder_of(store, parent) == "worker-a"


def test_locks_are_per_session(store: StoreHandle, parent: str) -> None:
    add_session(store, "p2")
    assert compression.acquire(store, parent, "worker-a")
    assert compression.acquire(store, "p2", "worker-b")


def test_missing_session_is_not_locked(store: StoreHandle) -> None:
    assert not compression.acquire(store, "nope", "worker-a")
    assert not compression.acquire(store, "", "worker-a")


def test_expired_lock_is_reaped(store: StoreHandle, parent: str, fake_clock: AutoClock) -> None:
    assert compression.acquire(store, parent, "worker-a", ttl=10)
    fake_clock.advance(9)
    assert not compression.acquire(store, parent, "worker-b")
    fake_clock.advance(1)
    assert compression.acquire(store, parent, "worker-b")
    row = lock_row(store, parent)
    assert row is not None and row["holder"] == "worker-b"
    assert row["expires_at"] == pytest.approx(now(store) + compression.LOCK_TTL)


def test_dead_pid_in_our_namespace_is_reaped(
    store: StoreHandle, parent: str, same_ns: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    dead = "pid=424242:pidns=4026531836:box-a:job=1"
    assert compression.acquire(store, parent, dead)
    probed: list[int] = []
    monkeypatch.setattr(holders, "_pid_gone", lambda pid: probed.append(pid) or True)
    assert compression.acquire(store, parent, "pid=525252:pidns=4026531836:box-a")
    assert probed == [424242]


def test_foreign_namespace_holder_is_kept_until_expiry(
    store: StoreHandle,
    parent: str,
    same_ns: None,
    fake_clock: AutoClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    foreign = "pid=424242:pidns=4026532999:box-a"
    assert compression.acquire(store, parent, foreign, ttl=60)
    monkeypatch.setattr(holders, "_pid_gone", lambda pid: pytest.fail("foreign pid probed"))
    assert not compression.acquire(store, parent, "worker-b")
    fake_clock.advance(60)
    assert compression.acquire(store, parent, "worker-b")


def test_live_pid_holder_is_kept(store: StoreHandle, parent: str) -> None:
    assert compression.acquire(store, parent, holders.holder_stamp())
    assert not compression.acquire(store, parent, "worker-b")


def test_refresh_and_release_only_for_the_owner(
    store: StoreHandle, parent: str, fake_clock: AutoClock
) -> None:
    assert compression.acquire(store, parent, "worker-a", ttl=10)
    assert not compression.refresh(store, parent, "worker-b")
    assert not compression.release(store, parent, "worker-b")
    fake_clock.advance(20)  # expired, but nobody took it: the owner may revive it
    assert compression.refresh(store, parent, "worker-a", ttl=30)
    assert compression.holder_of(store, parent) == "worker-a"
    assert compression.release(store, parent, "worker-a")
    assert lock_row(store, parent) is None
    assert not compression.release(store, parent, "worker-a")
    assert not compression.refresh(store, parent, "worker-a")


def test_contention_past_the_lock_budget_refuses(
    store: StoreHandle, parent: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    budgets: list[float] = []

    def busy(fn: Any, *, budget: Any) -> Any:
        budgets.append(budget.seconds)
        raise StoreLocked("busy")

    monkeypatch.setattr(store, "write", busy)
    assert not compression.acquire(store, parent, "worker-a")
    assert not compression.refresh(store, parent, "worker-a")
    assert budgets == [5.0, 5.0]


def test_racing_threads_have_one_winner(store: StoreHandle, parent: str) -> None:
    barrier = threading.Barrier(8)
    results: list[bool] = []
    guard = threading.Lock()

    def race(i: int) -> None:
        barrier.wait()
        won = compression.acquire(store, parent, f"thread-{i}")
        with guard:
            results.append(won)

    threads = [threading.Thread(target=race, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [False] * 7 + [True]


def test_is_locked_in_reaps_a_stale_row(
    store: StoreHandle, parent: str, fake_clock: AutoClock
) -> None:
    assert compression.acquire(store, parent, "worker-a", ttl=5)
    assert store.write(lambda c: compression.is_locked_in(c, parent, now=now(store)))
    fake_clock.advance(5)
    assert not store.write(lambda c: compression.is_locked_in(c, parent, now=now(store)))
    assert lock_row(store, parent) is None


# -- watermark and publish ---------------------------------------------------------------------


def test_watermark_is_the_newest_live_id(store: StoreHandle, parent: str) -> None:
    add_session(store, "empty")
    assert compression.watermark(store, "empty") == 0
    newest = message_rows(store, parent)[-1]["id"]
    assert compression.watermark(store, parent) == newest
    add_message(store, parent, "user", "rewound", active=0)
    assert compression.watermark(store, parent) == newest


def slow_summary(store: StoreHandle, sid: str, late: list[str]) -> list[dict[str, Any]]:
    """Stands in for the provider call: messages keep arriving from another thread meanwhile."""

    def chat() -> None:
        for text in late:
            add_message(store, sid, "user", text, reasoning="r-" + text)

    worker = threading.Thread(target=chat)
    worker.start()
    worker.join()
    return [dict(row) for row in SUMMARY]


def test_rows_appended_during_the_summary_follow_the_handoff(
    store: StoreHandle, parent: str
) -> None:
    assert compression.acquire(store, parent, "rotator")
    mark = compression.watermark(store, parent)
    handoff = slow_summary(store, parent, ["late 1", "late 2", "late 3"])
    originals = [r for r in message_rows(store, parent) if r["id"] > mark]

    child = compression.publish_continuation(store, parent, {"id": "c1"}, handoff, mark, "rotator")

    assert child == "c1"
    rows = message_rows(store, child)
    assert [r["content"] for r in rows] == [
        "summary of the earlier talk",
        "noted, carrying on",
        "late 1",
        "late 2",
        "late 3",
    ]
    assert [r["finish_reason"] for r in rows[:2]] == [HANDOFF_FINISH] * 2
    for clone, original in zip(rows[2:], originals, strict=True):
        assert clone["id"] > original["id"]
        assert {k: clone[k] for k in PAYLOAD} == {k: original[k] for k in PAYLOAD}
    meta = session(store, child)
    assert meta is not None and meta["message_count"] == 5
    assert meta["parent_session_id"] == parent
    old = session(store, parent)
    assert old is not None and old["end_reason"] == "compression"
    assert len(message_rows(store, parent)) == 9  # the parent keeps its rows


def test_tail_clone_is_byte_exact_for_tool_and_multimodal_rows(
    store: StoreHandle, parent: str
) -> None:
    assert compression.acquire(store, parent, "rotator")
    mark = compression.watermark(store, parent)
    calls = [{"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]
    add_message(store, parent, "assistant", None, tool_calls=calls, reasoning_details='[{"k":1}]')
    add_message(store, parent, "tool", "out ퟿", tool_call_id="c1", tool_name="terminal")
    add_message(store, parent, "user", [{"type": "text", "text": "look"}, {"type": "image_url"}])
    originals = [r for r in message_rows(store, parent) if r["id"] > mark]
    child = compression.publish_continuation(store, parent, {}, SUMMARY, mark, "rotator")
    clones = message_rows(store, child)[2:]
    assert [{k: r[k] for k in PAYLOAD} for r in clones] == [
        {k: r[k] for k in PAYLOAD} for r in originals
    ]
    meta = session(store, child)
    assert meta is not None and meta["tool_call_count"] == 1


def test_ceiling_keeps_the_rotators_own_flush_out(store: StoreHandle, parent: str) -> None:
    assert compression.acquire(store, parent, "rotator")
    mark = compression.watermark(store, parent)
    add_message(store, parent, "user", "foreign steer")
    ceiling = compression.watermark(store, parent)
    add_message(store, parent, "user", "own flush")
    child = compression.publish_continuation(
        store, parent, {}, SUMMARY, mark, "rotator", ceiling=ceiling
    )
    assert live_contents(store, child)[2:] == ["foreign steer"]


def test_lost_lock_publishes_nothing(store: StoreHandle, parent: str) -> None:
    assert compression.acquire(store, parent, "worker-a")
    mark = compression.watermark(store, parent)
    assert compression.release(store, parent, "worker-a")
    assert compression.acquire(store, parent, "worker-b")
    before = message_rows(store, parent)
    with pytest.raises(CompressionBusy):
        compression.publish_continuation(store, parent, {"id": "c1"}, SUMMARY, mark, "worker-a")
    assert session(store, "c1") is None
    meta = session(store, parent)
    assert meta is not None and meta["ended_at"] is None and meta["end_reason"] is None
    assert message_rows(store, parent) == before
    assert compression.holder_of(store, parent) == "worker-b"


def test_expired_or_never_held_lock_publishes_nothing(
    store: StoreHandle, parent: str, fake_clock: AutoClock
) -> None:
    with pytest.raises(CompressionBusy):
        compression.publish_continuation(store, parent, {}, SUMMARY, 0, "never-held")
    assert compression.acquire(store, parent, "worker-a", ttl=1)
    fake_clock.advance(1)
    with pytest.raises(CompressionBusy):
        compression.publish_continuation(store, parent, {}, SUMMARY, 0, "worker-a")
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1


def test_empty_handoff_is_refused(store: StoreHandle, parent: str) -> None:
    assert compression.acquire(store, parent, "rotator")
    with pytest.raises(ValueError):
        compression.publish_continuation(store, parent, {}, [], 0, "rotator")


def test_child_carries_route_workspace_and_lineage_flags(store: StoreHandle, parent: str) -> None:
    update_session(store, parent, archived=1, hidden=1, system_prompt_hash=None)
    assert compression.acquire(store, parent, "rotator")
    child = compression.publish_continuation(
        store,
        parent,
        {"model": "model-b"},
        SUMMARY,
        compression.watermark(store, parent),
        "rotator",
    )
    meta = session(store, child)
    assert meta is not None
    assert meta["model"] == "model-b"
    assert (meta["billing_provider"], meta["billing_base_url"]) == (
        "prov",
        "https://api.example.test",
    )
    assert (meta["cwd"], meta["git_root"], meta["profile_id"], meta["user_id"]) == (
        "/work/repo",
        "/work/repo",
        "work",
        "u1",
    )
    assert (meta["pinned"], meta["archived"], meta["hidden"]) == (1, 1, 1)
    # Copied verbatim: the inherited marker names another id, so this is still a continuation.
    assert json.loads(meta["model_config"]) == {"_branched_from": "elsewhere", "temperature": 0.2}
    assert meta["source"] == "cli"
    with store.read() as conn:
        assert lineage.tip_in(conn, parent) == child


def test_idle_sweep_archive_is_lifted_by_a_live_continuation(
    store: StoreHandle, parent: str
) -> None:
    update_session(store, parent, archived=1, auto_archived=1)
    assert compression.acquire(store, parent, "rotator")
    child = compression.publish_continuation(store, parent, {}, SUMMARY, 0, "rotator")
    for sid in (parent, child):
        meta = session(store, sid)
        assert meta is not None and (meta["archived"], meta["auto_archived"]) == (0, 0)


def test_child_title_from_fields(store: StoreHandle, parent: str) -> None:
    assert compression.acquire(store, parent, "rotator")
    child = compression.publish_continuation(
        store, parent, {"title": "named", "title_source": "llm"}, SUMMARY, 0, "rotator"
    )
    meta = session(store, child)
    assert meta is not None and (meta["title"], meta["title_source"]) == ("named", "llm")


def test_automatic_end_stamp_is_cleared_by_the_lock_holder(store: StoreHandle, parent: str) -> None:
    update_session(store, parent, ended_at=now(store), end_reason="tui_shutdown")
    assert compression.acquire(store, parent, "rotator")
    compression.publish_continuation(store, parent, {"id": "c1"}, SUMMARY, 0, "rotator")
    meta = session(store, parent)
    assert meta is not None and meta["end_reason"] == "compression"


@pytest.mark.parametrize(
    ("reason", "error"),
    [("user_exit", CompressionParentEnded), ("compression", CompressionClosed)],
)
def test_deliberate_or_compression_end_refuses(
    store: StoreHandle, parent: str, reason: str, error: type[StoreError]
) -> None:
    update_session(store, parent, ended_at=now(store), end_reason=reason)
    assert compression.acquire(store, parent, "rotator")
    with pytest.raises(error):
        compression.publish_continuation(store, parent, {"id": "c1"}, SUMMARY, 0, "rotator")
    assert session(store, "c1") is None
    meta = session(store, parent)
    assert meta is not None and meta["end_reason"] == reason


def test_publish_retry_after_a_busy_begin_creates_one_child(
    store: StoreHandle, parent: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert compression.acquire(store, parent, "rotator")
    real = store.write
    calls = {"n": 0}

    def flaky(fn: Any, *, budget: Any) -> Any:
        def once(conn: sqlite3.Connection) -> Any:
            calls["n"] += 1
            result = fn(conn)
            if calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return result

        return real(once, budget=budget)

    monkeypatch.setattr(store, "write", flaky)
    child = compression.publish_continuation(store, parent, {}, SUMMARY, 0, "rotator")
    assert calls["n"] == 2
    with store.read() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE parent_session_id = ?", (parent,)
            ).fetchone()[0]
            == 1
        )
    assert session(store, child) is not None


# -- counters ----------------------------------------------------------------------------------


def test_cooldown_merge_max_and_latest_error(
    store: StoreHandle, parent: str, fake_clock: AutoClock
) -> None:
    t = now(store)
    assert compression.record_cooldown(store, parent, t + 100, "first")
    assert compression.record_cooldown(store, parent, t + 50, "second")
    found = compression.active_cooldown(store, parent)
    assert found is not None
    assert (found.cooldown_until, found.cooldown_error) == (t + 100, "second")
    compression.record_cooldown(store, parent, t + 200, None)
    assert compression.counters(store, parent).cooldown_until == t + 200  # type: ignore[union-attr]
    fake_clock.advance(200)
    assert compression.active_cooldown(store, parent) is None
    assert compression.clear_cooldown(store, parent)
    cleared = compression.counters(store, parent)
    assert cleared is not None and cleared.cooldown_until is None and cleared.cooldown_error is None


def test_streak_counters(store: StoreHandle, parent: str) -> None:
    assert compression.set_counter(store, parent, "fallback_streak", 3)
    assert compression.set_counter(store, parent, "ineffective_count", -4)
    assert compression.bump_overload_streak(store, parent) == 1
    assert compression.bump_overload_streak(store, parent) == 2
    found = compression.counters(store, parent)
    assert found is not None
    assert (found.fallback_streak, found.ineffective_count, found.overload_streak) == (3, 0, 2)
    assert compression.bump_overload_streak(store, "nope") is None
    assert compression.counters(store, "nope") is None
    assert not compression.set_counter(store, "nope", "fallback_streak", 1)


def test_snapshot_restore_is_exact(store: StoreHandle, parent: str) -> None:
    snap = compression.snapshot(store, parent)
    assert snap.exists and snap.values == (None, None, 0, 0, 0)
    compression.record_cooldown(store, parent, now(store) + 10, "boom")
    compression.bump_overload_streak(store, parent)
    compression.set_counter(store, parent, "fallback_streak", 4)
    compression.restore(store, parent, snap)
    assert compression.snapshot(store, parent) == snap


def test_restore_tolerates_a_vanished_session_and_refuses_a_new_one(
    store: StoreHandle, parent: str
) -> None:
    snap = compression.snapshot(store, parent)
    store.write(lambda c: c.execute("DELETE FROM sessions WHERE id = ?", (parent,)))
    compression.restore(store, parent, snap)  # gone: nothing to put back
    absent = compression.snapshot(store, "later")
    assert not absent.exists
    add_session(store, "later")
    with pytest.raises(StoreError):
        compression.restore(store, "later", absent)


def test_restore_that_does_not_stick_raises(
    store: StoreHandle, parent: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    snap = compression.snapshot(store, parent)
    other = compression.CounterSnapshot(True, (None, None, 9, 0, 0))
    monkeypatch.setattr(compression, "snapshot", lambda s, sid: other)
    with pytest.raises(StoreError):
        compression.restore(store, parent, snap)


# -- chains ------------------------------------------------------------------------------------


def chain_of(store: StoreHandle, count: int) -> list[str]:
    ids = [f"seg{i:04d}" for i in range(count)]
    t = now(store)

    def build(conn: sqlite3.Connection) -> None:
        for i, sid in enumerate(ids):
            last = i == count - 1
            conn.execute(
                "INSERT INTO sessions (id, source, started_at, parent_session_id, ended_at, "
                "end_reason) VALUES (?, 'cli', ?, ?, ?, ?)",
                (
                    sid,
                    t + i,
                    ids[i - 1] if i else None,
                    None if last else t + i + 0.5,
                    None if last else "compression",
                ),
            )

    store.write(build)
    return ids


def test_long_chain_tip_and_root(store: StoreHandle) -> None:
    ids = chain_of(store, 1200)
    assert compression.tip(store, ids[0]) == ids[-1]
    assert compression.tip(store, ids[600]) == ids[-1]
    assert compression.root(store, ids[-1]) == ids[0]
    assert compression.lineage_ids(store, ids[1100]) == ids


def test_two_live_continuations_fail_closed(store: StoreHandle) -> None:
    ids = chain_of(store, 2)
    add_session(store, "rival", parent_session_id=ids[0])
    with pytest.raises(lineage.AmbiguousChain):
        compression.tip(store, ids[0])


def test_mark_orphans(store: StoreHandle, fake_clock: AutoClock) -> None:
    old = now(store) - 8 * 86400
    add_session(store, "root", ended_at=old + 1, end_reason="compression", started_at=old)
    add_session(store, "orphan", parent_session_id="root", started_at=old + 2)
    add_message(store, "orphan", "user", "summary")
    add_session(store, "worked", parent_session_id="root", started_at=old + 3, api_call_count=2)
    add_message(store, "worked", "user", "x")
    add_session(store, "silent", parent_session_id="root", started_at=old + 4)
    add_session(
        store,
        "branch",
        parent_session_id="root",
        started_at=old + 5,
        model_config={"_branched_from": "root"},
    )
    add_message(store, "branch", "user", "y")
    add_session(store, "young", parent_session_id="root")
    add_message(store, "young", "user", "z")
    assert compression.mark_orphans(store) == 1
    reasons = {
        sid: (session(store, sid) or {}).get("end_reason")
        for sid in ("orphan", "worked", "silent", "branch", "young")
    }
    assert reasons == {
        "orphan": "orphaned_compression",
        "worked": None,
        "silent": None,
        "branch": None,
        "young": None,
    }
    fake_clock.advance(8 * 86400)
    assert compression.mark_orphans(store) == 1  # "young" has aged now
    assert compression.mark_orphans(store) == 0
