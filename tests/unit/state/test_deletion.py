"""Delete and tombstones (P02-18)."""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from indus.foundation.errors import SessionTombstoned
from indus.state import deletion, sessions
from indus.state.deletion import SessionDeleted, SessionGuarded
from indus.state.registry import StoreHandle
from tests.unit.state._helpers_d import (
    DAY,
    add_guard,
    add_message,
    add_session,
    advance,
    count,
    meta_value,
    now_of,
    session_row,
)


def _tombstone(store: StoreHandle, session_id: str) -> dict[str, object] | None:
    raw = meta_value(store, sessions.TOMBSTONE_PREFIX + session_id)
    return None if raw is None else json.loads(raw)


def test_delegates_go_and_branches_are_orphaned(store: StoreHandle) -> None:
    add_session(store, "parent")
    add_message(store, "parent")
    add_session(store, "worker", parent="parent", marker=sessions.MARKER_DELEGATE)
    add_message(store, "worker", "sub-task output", role="assistant")
    add_session(store, "worker-2", parent="worker", marker=sessions.MARKER_DELEGATE)
    add_session(store, "fork", parent="parent", marker=sessions.MARKER_BRANCHED)
    add_message(store, "fork", "kept")

    assert deletion.delete_session(store, "parent") is True

    for gone in ("parent", "worker", "worker-2"):
        assert session_row(store, gone) is None, gone
        assert _tombstone(store, gone) == {"deleted_at": now_of(store), "by": "user"}
    fork = session_row(store, "fork")
    assert fork is not None and fork["parent_session_id"] is None
    assert count(store, "SELECT COUNT(*) FROM messages WHERE session_id = 'fork'") == 1
    assert count(store, "SELECT COUNT(*) FROM messages WHERE session_id <> 'fork'") == 0


def test_inherited_delegate_marker_naming_another_id_is_not_cascaded(store: StoreHandle) -> None:
    add_session(store, "a")
    add_session(store, "b", parent="a", model_config={sessions.MARKER_DELEGATE: "elsewhere"})
    deletion.delete_session(store, "a")
    kept = session_row(store, "b")
    assert kept is not None and kept["parent_session_id"] is None


def _chain(store: StoreHandle) -> None:
    add_session(store, "root", end_reason="compression")
    add_session(store, "mid", parent="root", end_reason="compression")
    add_session(store, "tip", parent="mid")
    add_session(store, "stale", parent="root", end_reason="idle")
    add_session(store, "fork", parent="root", marker=sessions.MARKER_BRANCHED)
    add_session(store, "helper", parent="tip", marker=sessions.MARKER_DELEGATE)
    add_session(store, "solo")


def test_whole_chain_from_the_tip(store: StoreHandle) -> None:
    _chain(store)
    report = deletion.delete_many(store, ["tip", "solo"], whole_chain=True)
    assert report.deleted == ["tip", "solo"]
    assert set(report.removed) == {"root", "mid", "tip", "stale", "helper", "solo"}
    for gone in report.removed:
        assert session_row(store, gone) is None
    fork = session_row(store, "fork")
    assert fork is not None and fork["parent_session_id"] is None


def test_default_delete_is_chain_blind(store: StoreHandle) -> None:
    _chain(store)
    assert deletion.delete_session(store, "tip") is True
    assert session_row(store, "mid") is not None and session_row(store, "root") is not None
    assert session_row(store, "helper") is None  # the delegate still goes


def test_whole_chain_guard_keeps_the_chain(store: StoreHandle) -> None:
    _chain(store)
    add_guard(store, "mid", table="turn_leases", holder="w1", expires_at=now_of(store) + 300)
    report = deletion.delete_many(store, ["tip"], whole_chain=True)
    assert (report.deleted, report.guarded, report.removed) == ([], ["tip"], [])
    with pytest.raises(SessionGuarded):
        deletion.delete_session(store, "tip", whole_chain=True)
    for kept in ("root", "mid", "tip"):
        assert session_row(store, kept) is not None


@pytest.mark.parametrize("table", ["turn_leases", "compression_locks"])
def test_live_guard_refuses_then_expiry_allows(store: StoreHandle, table: str) -> None:
    add_session(store, "busy")
    add_guard(store, "busy", table=table, holder="worker-1", expires_at=now_of(store) + 60)
    with pytest.raises(SessionGuarded) as caught:
        deletion.delete_session(store, "busy")
    assert caught.value.code == "store.session_guarded"
    assert session_row(store, "busy") is not None
    assert _tombstone(store, "busy") is None

    advance(store, 61)
    assert deletion.delete_session(store, "busy") is True
    column = "conversation_id" if table == "turn_leases" else "session_id"
    assert count(store, f"SELECT COUNT(*) FROM {table} WHERE {column} = 'busy'") == 0


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


@pytest.mark.skipif(not Path("/proc/self/ns/pid").exists(), reason="needs PID namespaces")
def test_dead_holder_is_reaped(store: StoreHandle) -> None:
    pidns = os.stat("/proc/self/ns/pid").st_ino
    holder = f"pid={_dead_pid()}:pidns={pidns}:{socket.gethostname()}"
    add_session(store, "crashed")
    add_guard(
        store, "crashed", table="compression_locks", holder=holder, expires_at=now_of(store) + 3600
    )
    assert deletion.delete_session(store, "crashed") is True


def test_guarded_delegate_protects_its_parent(store: StoreHandle) -> None:
    add_session(store, "parent")
    add_session(store, "worker", parent="parent", marker=sessions.MARKER_DELEGATE)
    add_guard(store, "worker", table="turn_leases", holder="w", expires_at=now_of(store) + 60)
    with pytest.raises(SessionGuarded):
        deletion.delete_session(store, "parent")
    assert session_row(store, "parent") is not None


def test_deleted_id_cannot_be_recreated(store: StoreHandle) -> None:
    add_session(store, "gone")
    deletion.delete_session(store, "gone")
    assert deletion.is_tombstoned(store, "gone") is True
    assert deletion.is_tombstoned(store, "never-existed") is False
    with pytest.raises(SessionTombstoned):
        sessions.create(store, "gone", "cli")


def test_unknown_id_deletes_nothing(store: StoreHandle) -> None:
    assert deletion.delete_session(store, "ghost") is False
    assert _tombstone(store, "ghost") is None


def test_tombstone_shape_and_expiry(store: StoreHandle) -> None:
    add_session(store, "mine")
    add_session(store, "empty", end_reason="cli_close")
    deletion.delete_session(store, "mine")
    assert deletion.delete_if_empty(store, "empty") is True
    assert _tombstone(store, "empty") == {"deleted_at": now_of(store), "by": "sweep"}

    advance(store, 6 * DAY)
    assert store.write(lambda c: deletion.reap_tombstones_in(c, now=now_of(store))) == 0
    advance(store, 1 * DAY + 1)
    assert store.write(lambda c: deletion.reap_tombstones_in(c, now=now_of(store))) == 1
    assert _tombstone(store, "empty") is None
    assert _tombstone(store, "mine") is not None  # user tombstones never age out


def test_sweep_never_weakens_a_user_tombstone(store: StoreHandle) -> None:
    now = now_of(store)
    store.write(lambda c: deletion.write_tombstone_in(c, "x", by="user", now=now))
    store.write(lambda c: deletion.write_tombstone_in(c, "x", by="sweep", now=now + 5))
    assert _tombstone(store, "x") == {"deleted_at": now, "by": "user"}
    store.write(lambda c: deletion.write_tombstone_in(c, "y", by="sweep", now=now))
    store.write(lambda c: deletion.write_tombstone_in(c, "y", by="user", now=now + 5))
    assert _tombstone(store, "y") == {"deleted_at": now + 5, "by": "user"}


def test_unreadable_tombstone_still_fences(store: StoreHandle) -> None:
    store.write(lambda c: c.execute("INSERT INTO meta VALUES ('tombstone:odd', 'not json')"))
    with store.read() as conn:
        stone = deletion.tombstone_in(conn, "odd")
    assert stone is not None and stone.by == "user"
    assert store.write(lambda c: deletion.reap_tombstones_in(c, now=now_of(store) + 30 * DAY)) == 0


def test_delete_many_five_thousand_ids(store: StoreHandle) -> None:
    now = now_of(store)
    ids = [f"bulk-{i:05d}" for i in range(5000)]
    store.write(
        lambda c: c.executemany(
            "INSERT INTO sessions (id, source, started_at) VALUES (?, 'cli', ?)",
            [(i, now) for i in ids],
        )
    )
    store.write(
        lambda c: c.executemany(
            "INSERT INTO messages (session_id, role, content, timestamp) "
            "VALUES (?, 'user', 'x', ?)",
            [(i, now) for i in ids[:1000]],
        )
    )
    statements: list[str] = []
    conn = store.writer.conn
    assert conn is not None
    conn.set_trace_callback(statements.append)
    try:
        report = deletion.delete_many(store, [*ids, "unknown-1", "unknown-2"])
    finally:
        conn.set_trace_callback(None)
    assert len(report.deleted) == 5000 and report.unknown == ["unknown-1", "unknown-2"]
    assert count(store, "SELECT COUNT(*) FROM sessions") == 0
    assert count(store, "SELECT COUNT(*) FROM messages") == 0
    # Traced statements show bound values inlined; count list members instead of '?'.
    widest = max(
        (
            len(m.group(1).split(","))
            for s in statements
            for m in re.finditer(r"IN \(([^()]*)\)", s)
        ),
        default=0,
    )
    assert 0 < widest <= deletion.SQL_CHUNK


def test_delete_many_skips_guarded_roots(store: StoreHandle) -> None:
    add_session(store, "a")
    add_session(store, "b")
    add_guard(store, "b", table="compression_locks", holder="w", expires_at=now_of(store) + 60)
    report = deletion.delete_many(store, ["a", "b", "nope", "a"])
    assert (report.deleted, report.guarded, report.unknown) == (["a"], ["b"], ["nope"])
    assert session_row(store, "b") is not None


def test_delete_if_empty_rules(store: StoreHandle) -> None:
    now = now_of(store)
    add_session(store, "open")
    add_session(store, "titled", end_reason="x", title="Named")
    add_session(store, "talked", end_reason="x")
    add_message(store, "talked")
    add_session(store, "rewound", end_reason="x")
    add_message(store, "rewound", active=0)
    add_session(store, "parent", end_reason="x")
    add_session(store, "child", parent="parent")
    add_session(store, "leased", end_reason="x")
    add_guard(store, "leased", table="turn_leases", holder="w", expires_at=now + 60)
    add_session(store, "empty", end_reason="x")
    for kept in ("open", "titled", "talked", "rewound", "parent", "leased", "missing"):
        assert deletion.delete_if_empty(store, kept) is False, kept
    assert deletion.delete_if_empty(store, "empty") is True
    assert session_row(store, "empty") is None


def test_prompt_row_collected_with_its_last_user(store: StoreHandle) -> None:
    add_session(store, "a", prompt="be brief")
    add_session(store, "b", prompt="be brief")
    deletion.delete_session(store, "a")
    assert count(store, "SELECT COUNT(*) FROM system_prompts") == 1
    deletion.delete_session(store, "b")
    assert count(store, "SELECT COUNT(*) FROM system_prompts") == 0


def test_child_rows_go_with_the_session(store: StoreHandle) -> None:
    now = now_of(store)
    add_session(store, "s")
    add_message(store, "s")
    store.write(
        lambda c: c.execute(
            "INSERT INTO session_meta VALUES ('s', 'goals', 'main', '{}', ?, NULL)", (now,)
        )
    )
    store.write(
        lambda c: c.execute("INSERT INTO model_usage (session_id, model) VALUES ('s', 'm')")
    )
    deletion.delete_session(store, "s")
    for table in ("messages", "session_meta", "model_usage"):
        assert count(store, f"SELECT COUNT(*) FROM {table}") == 0, table


def test_side_files_removed(store: StoreHandle) -> None:
    folder = store.scope.home / "sessions"
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("a.json", "a.jsonl", "b.json"):
        (folder / name).write_text("{}")
    outside = store.scope.home / "evil.json"
    outside.write_text("{}")
    add_session(store, "a")
    add_session(store, "../evil")
    deletion.delete_many(store, ["a", "../evil"])
    assert not (folder / "a.json").exists() and not (folder / "a.jsonl").exists()
    assert (folder / "b.json").exists() and outside.exists()


def test_hook_runs_after_commit_and_failures_are_swallowed(
    store: StoreHandle, caplog: pytest.LogCaptureFixture
) -> None:
    seen: list[SessionDeleted] = []
    rows_at_call: list[dict[str, object] | None] = []

    def broken(_event: SessionDeleted) -> None:
        raise RuntimeError("plugin bug")

    def record(event: SessionDeleted) -> None:
        rows_at_call.append(session_row(store, "s"))
        seen.append(event)

    deletion.register_on_session_delete(broken)
    deletion.register_on_session_delete(record)
    try:
        add_session(store, "s")
        with caplog.at_level(logging.WARNING, logger="indus.state.deletion"):
            assert deletion.delete_session(store, "s") is True
    finally:
        deletion.unregister_on_session_delete(broken)
        deletion.unregister_on_session_delete(record)
    assert rows_at_call == [None]  # the row was already gone: the hook ran after the commit
    assert seen[0].session_ids == ("s",) and seen[0].reason == "delete"
    assert seen[0].profile_id == store.scope.profile_id
    assert "on_session_delete hook failed" in caplog.text


def test_expect_message_ids_fences_a_moved_transcript(store: StoreHandle) -> None:
    add_session(store, "s")
    first = add_message(store, "s")
    second = add_message(store, "s", "more")
    add_message(store, "s", "rewound", active=0)
    assert deletion.delete_session(store, "s", expect_message_ids=[first]) is False
    assert session_row(store, "s") is not None and _tombstone(store, "s") is None
    assert deletion.delete_session(store, "s", expect_message_ids=[second, first]) is True
