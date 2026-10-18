"""P02-14: FTS5 indexes, the row-only projection, fail-open and the guarded rebuild."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from indus.foundation.errors import StoreCorrupt
from indus.foundation.scope import Scope
from indus.state import fts, guards, journal, registry
from indus.state.readpool import open_read_only
from indus.state.registry import StoreHandle
from indus.state.schema import meta_get
from tests.unit.state._helpers_c import (
    add_message,
    add_session,
    names,
    parts,
    strict_check,
)
from tests.unit.state.conftest import AutoClock

LONG_TAIL_WORD = "zephyrtail"


@pytest.fixture(autouse=True)
def _fresh_capabilities() -> Iterator[None]:
    fts.reset_capabilities()
    yield
    fts.reset_capabilities()


def _matches(conn: sqlite3.Connection, table: str, query: str) -> list[int]:
    rows = conn.execute(
        f"SELECT rowid FROM {table} WHERE {table} MATCH ? ORDER BY rowid", (query,)
    ).fetchall()
    return [int(row[0]) for row in rows]


def _fts_triggers(conn: sqlite3.Connection) -> set[str]:
    return names(conn, "trigger") & set(fts.ALL_TRIGGERS)


def _long_tool_text(size: int = 20_000) -> str:
    head, tail = "opening words ", " " + LONG_TAIL_WORD
    filler = ("filler " * size)[: size - len(head) - len(tail)]
    return head + filler + tail


# -- layout and projection ----------------------------------------------------------------------


def test_fresh_store_has_both_indexes_views_and_triggers(store: StoreHandle) -> None:
    with store.read() as conn:
        assert {fts.BASE, fts.TRIGRAM} <= names(conn, "table")
        assert {fts.BASE_VIEW, fts.TRIGRAM_VIEW} <= names(conn, "view")
        assert _fts_triggers(conn) == set(fts.ALL_TRIGGERS)
        assert fts.usable_tables(conn) == [fts.BASE, fts.TRIGRAM]
        assert fts.integrity_ok(conn) is True
        assert not fts.is_stale(conn)


def test_long_tool_row_is_indexed_as_its_prefix_only(store: StoreHandle) -> None:
    add_session(store, "s1")
    text = _long_tool_text()
    assert len(text) == 20_000
    row = add_message(store, "s1", "tool", text, tool_name="terminal")
    with store.read() as conn:
        assert _matches(conn, fts.BASE, "opening") == [row]
        assert _matches(conn, fts.BASE, LONG_TAIL_WORD) == []
        assert _matches(conn, fts.BASE, "terminal") == [row]  # tool name column
        projected = conn.execute(
            f"SELECT length(content) FROM {fts.BASE_VIEW} WHERE id = ?", (row,)
        ).fetchone()[0]
        assert projected == fts.TOOL_PREFIX_CHARS
        assert _matches(conn, fts.TRIGRAM, '"opening"') == []  # tool rows stay out
    store.write(lambda conn: strict_check(conn, fts.BASE))


def test_strict_integrity_survives_updates_deletes_and_cascades(store: StoreHandle) -> None:
    add_session(store, "s1")
    add_session(store, "s2")

    def check() -> None:
        def run(conn: sqlite3.Connection) -> None:
            strict_check(conn, fts.BASE)
            strict_check(conn, fts.TRIGRAM)

        store.write(run)

    check()
    tool = add_message(store, "s1", "tool", _long_tool_text(), tool_name="terminal")
    check()
    user = add_message(store, "s1", "user", "plain words here")
    rich = add_message(store, "s2", "user", parts([{"type": "text", "text": "picture caption"}]))
    check()
    store.write(lambda c: c.execute("UPDATE messages SET content = 'short' WHERE id = ?", (tool,)))
    store.write(lambda c: c.execute("UPDATE messages SET role = 'tool' WHERE id = ?", (user,)))
    store.write(lambda c: c.execute("UPDATE messages SET active = 0 WHERE id = ?", (rich,)))
    check()
    store.write(lambda c: c.execute("DELETE FROM messages WHERE id = ?", (tool,)))
    check()
    store.write(lambda c: c.execute("DELETE FROM sessions WHERE id = 's2'"))  # cascade
    check()
    with store.read() as conn:
        assert _matches(conn, fts.TRIGRAM, '"caption"') == []
        assert fts.integrity_ok(conn) is True


def test_parts_content_contributes_only_its_text(store: StoreHandle) -> None:
    add_session(store, "s1")
    listed = add_message(
        store,
        "s1",
        "user",
        parts(
            [
                {"type": "text", "text": "zebra crossing"},
                {"type": "image_url", "image_url": {"url": "https://example.test/giraffe.png"}},
                "loose string part",
            ]
        ),
    )
    single = add_message(store, "s1", "user", parts({"type": "text", "text": "walrus"}))
    wrapped = add_message(store, "s1", "user", parts(fts._parts_prefix() + "okapi"))  # pyright: ignore[reportPrivateUsage]
    broken = add_message(store, "s1", "user", fts._parts_prefix() + "[not json")  # pyright: ignore[reportPrivateUsage]
    with store.read() as conn:
        assert _matches(conn, fts.BASE, "zebra") == [listed]
        assert _matches(conn, fts.BASE, "loose") == [listed]
        assert _matches(conn, fts.BASE, "giraffe") == []
        assert _matches(conn, fts.BASE, "image_url") == []
        assert _matches(conn, fts.BASE, "walrus") == [single]
        assert _matches(conn, fts.BASE, "okapi") == [wrapped]
        assert _matches(conn, fts.BASE, "json") == [broken]  # stored as is, write not refused
        assert _matches(conn, fts.TRIGRAM, '"ebra cro"') == [listed]
    store.write(lambda conn: strict_check(conn, fts.BASE))


def test_trigram_leaves_out_tool_rows_tool_calls_and_internal_sources(store: StoreHandle) -> None:
    add_session(store, "chat", "cli")
    add_session(store, "job", "cron")
    add_session(store, "helper", "subagent")
    kept = add_message(
        store, "chat", "assistant", "visible sentence", tool_calls='[{"a":"qqxyzzy"}]'
    )
    add_message(store, "job", "user", "visible sentence")
    add_message(store, "helper", "user", "visible sentence")
    add_message(store, "chat", "tool", "visible sentence")
    with store.read() as conn:
        assert _matches(conn, fts.TRIGRAM, '"sible sent"') == [kept]
        assert _matches(conn, fts.TRIGRAM, '"qqxyzzy"') == []
        assert _matches(conn, fts.BASE, "qqxyzzy") == [kept]
        assert len(_matches(conn, fts.BASE, "visible")) == 4


# -- fail-open ----------------------------------------------------------------------------------


def _damage_index(store: StoreHandle) -> None:
    store.write(lambda conn: conn.execute(f"DROP TABLE {fts.BASE}_data"))


def test_index_damage_during_append_fails_open(store: StoreHandle) -> None:
    add_session(store, "s1")
    for n in range(5):
        add_message(store, "s1", "user", f"seed row {n}")
    _damage_index(store)
    row = add_message(store, "s1", "user", "lands after the damage")
    with store.read() as conn:
        landed = conn.execute("SELECT content FROM messages WHERE id = ?", (row,)).fetchone()
        assert landed[0] == "lands after the damage"
        info = fts.stale_info(conn)
        assert info is not None and info["at"] == pytest.approx(1_767_258_000.0)
        assert "malformed" in str(info["reason"]) or "corrupt" in str(info["reason"]).lower()
        assert _fts_triggers(conn) == set()
    assert not store.quarantined and guards.health_state(store.path) == "ok"
    add_message(store, "s1", "user", "and the next one too")


def test_quarantine_while_detaching_commits_nothing(
    store: StoreHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_session(store, "s1")
    add_message(store, "s1", "user", "seed")
    _damage_index(store)
    real = fts.is_fts_scoped_corruption

    def latch_then_check(exc: object) -> bool:
        guards.mark_corrupt(store.path, "database disk image is malformed (another handle)")
        return real(exc)

    monkeypatch.setattr(fts, "is_fts_scoped_corruption", latch_then_check)
    with pytest.raises(StoreCorrupt):
        add_message(store, "s1", "user", "must not land")
    raw = sqlite3.connect(store.path)
    try:
        assert raw.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
        assert (
            raw.execute("SELECT value FROM meta WHERE key = ?", (fts.STALE_KEY,)).fetchone() is None
        )
        assert _fts_triggers(raw) == set(fts.ALL_TRIGGERS)
    finally:
        raw.close()


def test_detach_waits_out_a_sibling_holding_the_write_lock(
    store: StoreHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_session(store, "s1")
    add_message(store, "s1", "user", "seed")
    _damage_index(store)
    real = fts.is_fts_scoped_corruption
    holding = threading.Event()
    siblings: list[threading.Thread] = []

    def sibling() -> None:
        raw = sqlite3.connect(store.path, isolation_level=None, timeout=10)
        try:
            raw.execute("BEGIN IMMEDIATE")
            holding.set()
            threading.Event().wait(0.3)  # real time, inside the writer's 1 s busy wait
            raw.execute("COMMIT")
        finally:
            raw.close()

    def contend_then_check(exc: object) -> bool:
        if not siblings:  # the failed write has rolled back; a sibling takes the lock now
            siblings.append(threading.Thread(target=sibling))
            siblings[0].start()
            assert holding.wait(10)
        return real(exc)

    monkeypatch.setattr(fts, "is_fts_scoped_corruption", contend_then_check)
    row = add_message(store, "s1", "user", "lands after the sibling lets go")
    siblings[0].join(10)
    with store.read() as conn:
        assert conn.execute("SELECT id FROM messages ORDER BY id DESC").fetchone()[0] == row
        assert fts.is_stale(conn)


def test_an_error_the_triggers_do_not_cause_is_not_retried_forever(store: StoreHandle) -> None:
    attempts: list[int] = []

    def always_broken(conn: sqlite3.Connection) -> None:
        attempts.append(1)
        raise sqlite3.DatabaseError("database disk image is malformed (messages_fts_idx)")

    with pytest.raises(sqlite3.DatabaseError):
        store.write(always_broken)
    assert len(attempts) == 3  # first try, retry after the drop, one blank retry
    assert not store.quarantined


# -- recovery -----------------------------------------------------------------------------------


def _make_stale(store: StoreHandle, reason: str = "test damage") -> None:
    store.write(lambda conn: fts.detach_in(conn, reason, now=1.0))


def _deferral(store: StoreHandle) -> fts.Deferral | None:
    with store.writer.borrow() as conn:
        return fts.read_deferral(conn)


def test_rebuild_waits_for_other_generations_then_runs(scope: Scope, fake_clock: AutoClock) -> None:
    first = registry.acquire(scope)
    add_session(first, "s1")
    old = add_message(first, "s1", "user", "written before the damage")
    _make_stale(first)
    during = add_message(first, "s1", "user", "written while detached")

    second = registry.open_unshared(scope)
    try:
        record = _deferral(second)
        assert record is not None and record.attempts == 1 and not record.gave_up
        assert "another handle" in record.reason
        with second.writer.borrow() as conn:
            assert fts.is_stale(conn)
    finally:
        second.close()
    first.close()

    third = registry.open_unshared(scope)
    try:
        assert fts.recover(third) == "waiting"  # backoff not over yet
    finally:
        third.close()
    fake_clock.advance(61)
    fourth = registry.open_unshared(scope)
    try:
        with fourth.writer.borrow() as conn:
            assert not fts.is_stale(conn)
            assert fts.read_deferral(conn) is None
            assert _fts_triggers(conn) == set(fts.ALL_TRIGGERS)
            assert _matches(conn, fts.BASE, "written") == [old, during]
            assert _matches(conn, fts.TRIGRAM, '"detached"') == [during]
            assert fts.integrity_ok(conn) is True
        assert (fourth.path.parent / fts.REBUILD_LOCK_NAME).exists()
    finally:
        fourth.close()


@pytest.mark.parametrize(
    ("times", "delays", "gives_up_at"),
    [
        # retried as soon as allowed: the 1,800 s rule ends it at the sixth deferral
        ("at_next", [60, 120, 240, 480, 960], 6),
        # retried without waiting: the ten-attempt rule ends it, delays capped at an hour
        ("same", [60, 120, 240, 480, 960, 1920, 3600, 3600, 3600], 10),
    ],
)
def test_backoff_doubles_to_an_hour_then_gives_up(
    times: str, delays: list[int], gives_up_at: int
) -> None:
    record = fts.Deferral()
    now = 1000.0
    seen: list[float] = []
    for attempt in range(1, 20):
        record = record.after(now, "busy")
        if record.gave_up:
            assert attempt == gives_up_at
            break
        seen.append(record.next_at - now)
        if times == "at_next":
            now = record.next_at
    else:
        pytest.fail("never gave up")
    assert seen == delays
    assert record.first_at == 1000.0


def test_recover_reports_gave_up_and_leaves_the_index_alone(store: StoreHandle) -> None:
    _make_stale(store)
    gave_up = fts.Deferral(attempts=10, first_at=1.0, next_at=2.0, reason="x", gave_up=True)
    store.write(
        lambda conn: conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            (fts.DEFERRAL_KEY, json.dumps(gave_up.__dict__)),
        )
    )
    assert fts.recover(store) == "gave_up"
    with store.read() as conn:
        assert fts.is_stale(conn)
    assert fts.rebuild(store) is True  # the explicit repair path still works
    with store.read() as conn:
        assert not fts.is_stale(conn) and fts.read_deferral(conn) is None


def test_recover_on_a_trusted_index_does_nothing(store: StoreHandle) -> None:
    assert fts.recover(store) == "not_stale"


def test_rebuild_lock_held_by_a_live_process_defers(store: StoreHandle) -> None:
    _make_stale(store)
    clock = AutoClock(store.scope.clock.now())
    holder = fts.RebuildLock(store.path.parent / fts.REBUILD_LOCK_NAME, clock=clock)
    assert holder.acquire()
    try:
        assert fts.recover(store) == "deferred"
        record = _deferral(store)
        assert record is not None and "rebuild lock" in record.reason
    finally:
        holder.release()


def _dead_pid() -> int:
    done = subprocess.run([sys.executable, "-c", "pass"], check=True)
    assert done.returncode == 0
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def test_dead_holder_is_broken_after_five_seconds(tmp_path: Path, fake_clock: AutoClock) -> None:
    path = tmp_path / fts.REBUILD_LOCK_NAME
    orphan = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        record = fts._holder_record(fake_clock)  # pyright: ignore[reportPrivateUsage]
        record.update(pid=_dead_pid(), start=None)
        os.write(orphan, json.dumps(record).encode())
        fts._try_lock(orphan)  # pyright: ignore[reportPrivateUsage]  # like an fd a child inherited
        lock = fts.RebuildLock(path, clock=fake_clock)
        started = fake_clock.monotonic()
        assert lock.acquire()
        waited = fake_clock.monotonic() - started
        assert 5.0 <= waited < 6.0 and lock.broke_stale_holder
        written = json.loads(path.read_text())
        assert written["pid"] == os.getpid()
        lock.release()
        assert path.read_text() == ""
    finally:
        os.close(orphan)


def test_live_holder_is_waited_for_until_the_timeout(tmp_path: Path, fake_clock: AutoClock) -> None:
    path = tmp_path / fts.REBUILD_LOCK_NAME
    first = fts.RebuildLock(path, clock=fake_clock)
    assert first.acquire()
    try:
        second = fts.RebuildLock(path, clock=fake_clock, timeout=12.0)
        started = fake_clock.monotonic()
        assert not second.acquire()
        assert 12.0 <= fake_clock.monotonic() - started < 12.2
        assert not second.broke_stale_holder
    finally:
        first.release()
    assert fts.RebuildLock(path, clock=fake_clock, timeout=0.0).acquire()


@pytest.mark.parametrize(
    ("record", "dead"),
    [
        (None, False),
        ("text", False),
        ({"pid": "12"}, False),
        ({"pid": 0}, False),
        ({"pid": True}, False),
    ],
)
def test_holder_records_that_prove_nothing_are_never_broken(record: object, dead: bool) -> None:
    assert fts.holder_is_dead(record) is dead


def test_holder_in_another_namespace_or_host_is_never_probed(fake_clock: AutoClock) -> None:
    record = fts._holder_record(fake_clock)  # pyright: ignore[reportPrivateUsage]
    record["pid"] = _dead_pid()
    assert fts.holder_is_dead(record)
    assert not fts.holder_is_dead({**record, "host": "elsewhere"})
    assert not fts.holder_is_dead({**record, "pidns": -1})
    assert not fts.holder_is_dead({**record, "pid": os.getpid()})


# -- capabilities -------------------------------------------------------------------------------


def test_old_sqlite_builds_only_the_word_index(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(journal, "linked_sqlite_version", lambda: (3, 33, 0))
    with registry.open_unshared(scope) as handle, handle.read() as conn:
        assert fts.BASE in names(conn, "table")
        assert fts.TRIGRAM not in names(conn, "table")
        assert _fts_triggers(conn) == set(fts.BASE_TRIGGERS)
        assert fts.usable_tables(conn) == [fts.BASE]


def test_trigram_detached_on_an_old_build_and_restored_on_a_new_one(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    with registry.open_unshared(scope) as handle:
        add_session(handle, "s1")
        add_message(handle, "s1", "user", "before downgrade")
    monkeypatch.setattr(journal, "linked_sqlite_version", lambda: (3, 30, 0))
    with registry.open_unshared(scope) as handle:
        with handle.read() as conn:
            assert _fts_triggers(conn) == set(fts.BASE_TRIGGERS)
            assert meta_get(conn, fts.TRIGRAM_STALE_KEY) is not None
        later = add_message(handle, "s1", "user", "during downgrade")
    monkeypatch.undo()
    monkeypatch.setattr(journal, "linked_sqlite_version", lambda: (3, 51, 3))
    with registry.open_unshared(scope) as handle:
        with handle.read() as conn:
            assert meta_get(conn, fts.TRIGRAM_STALE_KEY) is None
            assert _fts_triggers(conn) == set(fts.ALL_TRIGGERS)
            assert _matches(conn, fts.TRIGRAM, '"downgrade"')[-1] == later
        handle.write(lambda conn: strict_check(conn, fts.TRIGRAM))


def test_without_fts5_no_index_is_made_and_moved_triggers_are_dropped(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    with registry.open_unshared(scope) as handle:
        add_session(handle, "s1")
    monkeypatch.setattr(fts, "_probe", lambda spec: False)
    fts.reset_capabilities()
    with registry.open_unshared(scope) as handle:
        with handle.read() as conn:
            assert _fts_triggers(conn) == set()
            assert fts.is_stale(conn)
        add_message(handle, "s1", "user", "still saved")
        assert fts.recover(handle) == "unsupported"


def test_without_fts5_a_new_store_has_no_index(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fts, "_probe", lambda spec: False)
    with registry.open_unshared(scope) as handle, handle.read() as conn:
        assert names(conn, "table") & {fts.BASE, fts.TRIGRAM} == set()
        assert not fts.is_stale(conn)
        assert fts.integrity_ok(conn) is None


def test_orphan_shadow_tables_are_dropped_and_the_index_rebuilt(scope: Scope) -> None:
    with registry.open_unshared(scope) as handle:
        add_session(handle, "s1")
        row = add_message(handle, "s1", "user", "survives the orphan cleanup")
        path = handle.path
    raw = sqlite3.connect(path, isolation_level=None)
    try:
        for trigger in fts.TRIGRAM_TRIGGERS:
            raw.execute(f"DROP TRIGGER {trigger}")
        raw.execute(f"DROP TABLE {fts.TRIGRAM}")
        raw.execute(f"CREATE TABLE {fts.TRIGRAM}_data(id INTEGER PRIMARY KEY, block BLOB)")
        raw.execute(f"CREATE TABLE {fts.TRIGRAM}_config(k PRIMARY KEY, v)")
        raw.execute(f"CREATE TABLE {fts.TRIGRAM}_extra(x)")  # not a shadow name: kept
    finally:
        raw.close()
    with registry.open_unshared(scope) as handle:
        with handle.read() as conn:
            assert f"{fts.TRIGRAM}_extra" in names(conn, "table")
            assert _matches(conn, fts.TRIGRAM, '"orphan"') == [row]
        handle.write(lambda conn: strict_check(conn, fts.TRIGRAM))


# -- merge and integrity ------------------------------------------------------------------------


def test_merge_is_bounded_and_stops_when_idle(store: StoreHandle) -> None:
    add_session(store, "s1")
    for n in range(40):
        add_message(store, "s1", "user", f"segment number {n}")
    worked = fts.merge(store)
    assert 0 < worked <= 2 * fts.MERGE_COMMANDS
    assert fts.merge(store) == 0
    with store.read() as conn:
        assert len(_matches(conn, fts.BASE, "segment")) == 40


def test_merge_runs_every_thousand_writes(scope: Scope, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(fts, "_housekeeping_merge", lambda conn: calls.append(1))
    with registry.open_unshared(scope) as handle:
        handle.writer.write_count = fts.MERGE_EVERY - 2
        add_session(handle, "s1")
        assert calls == []
        add_message(handle, "s1", "user", "the thousandth write")
        assert calls == [1]


def test_merge_skips_a_stale_index(store: StoreHandle) -> None:
    _make_stale(store)
    assert fts.merge(store) == 0


def test_integrity_check_reports_damage_and_works_read_only(store: StoreHandle) -> None:
    add_session(store, "s1")
    add_message(store, "s1", "user", "checked words")
    reader = open_read_only(store.path)
    try:
        assert fts.integrity_ok(reader) is True  # structure check on a read-only connection
    finally:
        reader.close()
    store.write(
        lambda conn: conn.execute(
            f"UPDATE {fts.BASE}_data SET block = X'00112233445566778899AABBCCDDEEFF'"
        )
    )
    with store.writer.borrow() as conn:
        assert fts.integrity_ok(conn) is False
    plain = sqlite3.connect(":memory:")
    try:
        assert fts.integrity_ok(plain) is None
    finally:
        plain.close()


def test_a_setup_this_sqlite_rejects_leaves_the_store_usable(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fts, "_trigram_triggers", lambda: ["CREATE TRIGGER broken syntax here"])
    with registry.open_unshared(scope) as handle:
        add_session(handle, "s1")
        add_message(handle, "s1", "user", "saved without an index")
        with handle.read() as conn:
            assert names(conn, "table") & {fts.BASE, fts.TRIGRAM} == set()
            assert _fts_triggers(conn) == set()
    with registry.open_unshared(scope) as handle:  # not retried on every open
        assert handle.init_report.changed is False
