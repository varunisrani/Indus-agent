"""P07-15: `indus sessions` maintenance — prune, archive, optimize, repair, recover, import."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from indus.foundation.scope import Scope, make_scope
from indus.state import browse, connection, fts, portability, registry, sessions
from indus.state.schema import meta_set
from tests.unit.cli.commands._helpers_d import (  # noqa: F401
    aged,
    fake_clock,
    run,
    seeded,
    store_globals,
)
from tests.unit.state._helpers_d import count, session_row


def _ids(scope: Scope) -> set[str]:
    with seeded(scope) as store, store.read() as conn:
        return {str(r[0]) for r in conn.execute("SELECT id FROM sessions")}


def _mixed(scope: Scope) -> None:
    with seeded(scope) as store:
        aged(store, "old", 100, title="old talk")
        aged(store, "old-pinned", 100, title="pinned talk", pinned=1)
        aged(store, "recent", 10, title="recent talk")
        aged(store, "old-open", 100, title="open talk", ended=False)


# -- prune ----------------------------------------------------------------------------------------


def test_prune_dry_run_lists_then_yes_deletes(scope: Scope) -> None:
    _mixed(scope)
    dry = run(scope, "sessions", "prune", "--dry-run")
    assert dry.code == 0, dry.err
    assert "Would delete 1 session(s)" in dry.out and "old talk" in dry.out
    assert _ids(scope) == {"old", "old-pinned", "recent", "old-open"}
    done = run(scope, "sessions", "prune", "-y")
    assert done.code == 0, done.err
    assert "Deleted 1 session(s)" in done.out
    assert _ids(scope) == {"old-pinned", "recent", "old-open"}


def test_prune_without_yes_and_without_a_terminal_deletes_nothing(scope: Scope) -> None:
    _mixed(scope)
    result = run(scope, "sessions", "prune")
    assert result.code == 1
    assert "-y" in result.err
    assert "old" in _ids(scope)


def test_prune_asks_on_a_terminal(scope: Scope) -> None:
    _mixed(scope)
    result = run(scope, "sessions", "prune", tty=True, input="n\n")
    assert result.code == 0 and "Nothing was deleted" in result.out
    assert "old" in _ids(scope)


def test_filtered_prune_and_include_pinned(scope: Scope) -> None:
    _mixed(scope)
    with seeded(scope) as store:
        aged(store, "old-other", 100, title="unrelated")
    titled = run(scope, "sessions", "prune", "--title", "talk", "-y")
    assert titled.code == 0, titled.err
    # Any filter on its own matches every age: "recent talk" goes too, the open one stays.
    assert _ids(scope) == {"old-pinned", "old-open", "old-other"}
    pinned = run(scope, "sessions", "prune", "--title", "pinned", "--include-pinned", "-y")
    assert pinned.code == 0, pinned.err
    assert "old-pinned" not in _ids(scope)


def test_prune_older_than_window(scope: Scope) -> None:
    _mixed(scope)
    result = run(scope, "sessions", "prune", "--older-than", "5d", "-y")
    assert result.code == 0, result.err
    assert _ids(scope) == {"old-pinned", "old-open"}


def test_prune_refuses_while_the_store_is_held(scope: Scope) -> None:
    _mixed(scope)
    held = registry.acquire(scope)
    try:
        refused = run(scope, "sessions", "prune", "-y")
        assert refused.code == 1
        assert "open elsewhere" in refused.err and "Nothing was deleted" in refused.err
        assert "old" in _ids(scope)
        forced = run(scope, "sessions", "prune", "-y", "--force")
        assert forced.code == 0, forced.err
    finally:
        held.close()
    assert "old" not in _ids(scope)


def test_never_active(scope: Scope) -> None:
    with seeded(scope) as store:
        aged(store, "ghost", 40, messages=0, ended=False)
        aged(store, "young-ghost", 5, messages=0, ended=False)
        aged(store, "used", 40, title="used")
    result = run(scope, "sessions", "prune", "--never-active", "-y")
    assert result.code == 0, result.err
    assert _ids(scope) == {"young-ghost", "used"}


def test_nothing_to_prune(scope: Scope) -> None:
    assert "no session store" in run(scope, "sessions", "prune").out
    with seeded(scope) as store:
        aged(store, "fresh", 1)
    result = run(scope, "sessions", "prune", "-y")
    assert result.code == 0 and "Nothing to prune" in result.out


# -- archive --------------------------------------------------------------------------------------


def test_archive_needs_a_filter(scope: Scope) -> None:
    _mixed(scope)
    result = run(scope, "sessions", "archive", "-y")
    assert result.code == 2 and "filter" in result.err


def test_archive_ended_matches(scope: Scope) -> None:
    _mixed(scope)
    dry = run(scope, "sessions", "archive", "--older-than", "30d", "--dry-run")
    assert "Would archive 1" in dry.out
    done = run(scope, "sessions", "archive", "--older-than", "30d", "-y")
    assert done.code == 0, done.err
    with seeded(scope) as store:
        assert session_row(store, "old")["archived"] == 1  # type: ignore[index]
        assert session_row(store, "old-open")["archived"] == 0  # type: ignore[index]
        assert session_row(store, "old-pinned")["archived"] == 0  # type: ignore[index]
    with_open = run(scope, "sessions", "archive", "--older-than", "30d", "--include-open", "-y")
    assert with_open.code == 0
    with seeded(scope) as store:
        assert session_row(store, "old-open")["archived"] == 1  # type: ignore[index]


# -- optimize (R03.25) ----------------------------------------------------------------------------


def test_optimize_runs_when_nobody_holds_the_store(scope: Scope) -> None:
    _mixed(scope)
    result = run(scope, "sessions", "optimize")
    assert result.code == 0, result.err
    assert "Optimised" in result.out


def test_optimize_under_a_held_handle_skips_with_a_message(scope: Scope) -> None:
    _mixed(scope)
    held = registry.acquire(scope)
    try:
        result = run(scope, "sessions", "optimize")
    finally:
        held.close()
    assert result.code == 0
    assert result.out.startswith("Skipped:")
    assert "in-process store handle" in result.out


def test_optimize_without_a_store(scope: Scope) -> None:
    result = run(scope, "sessions", "optimize")
    assert result.code == 0 and "no session store" in result.out
    assert not connection.default_store_path(scope).exists()


# -- repair ---------------------------------------------------------------------------------------


def _mark_stale(scope: Scope) -> Path:
    with seeded(scope) as store:
        aged(store, "s1", 1, content="searchable words")
        store.write(lambda c: meta_set(c, fts.STALE_KEY, "test"))
        return store.path


def test_repair_healthy(scope: Scope) -> None:
    _mixed(scope)
    result = run(scope, "sessions", "repair")
    assert result.code == 0 and "healthy" in result.out


def test_repair_check_only_reports_without_writing(scope: Scope) -> None:
    path = _mark_stale(scope)
    before = path.read_bytes()
    result = run(scope, "sessions", "repair", "--check-only")
    assert result.code == 1
    assert "out of date" in result.out and "Nothing was changed" in result.out
    assert path.read_bytes() == before
    assert not (scope.home / "backups").exists() or not list(
        (scope.home / "backups").glob("state-before-repair-*")
    )


def test_repair_rebuilds_the_index_after_a_backup(scope: Scope) -> None:
    _mark_stale(scope)
    result = run(scope, "sessions", "repair")
    assert result.code == 0, result.err
    assert "healthy" in result.out
    backups = list((scope.home / "backups").glob("state-before-repair-*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    report = browse.integrity(scope)
    assert not report.fts_stale


def test_repair_points_structural_damage_at_recover(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _mark_stale(scope)
    report = browse.IntegrityReport(path, False, ["row 7 missing from index"], None, False, False)
    monkeypatch.setattr(browse, "integrity", lambda _scope, *a, **k: report)
    before = path.read_bytes()
    result = run(scope, "sessions", "repair")
    assert result.code == 1
    assert "recover --output" in result.err
    assert path.read_bytes() == before


# -- recover --------------------------------------------------------------------------------------


def test_recover_copies_rows_into_a_new_file(scope: Scope, tmp_path: Path) -> None:
    with seeded(scope) as store:
        aged(store, "root", 3, title="root talk", messages=2)
        aged(store, "child", 2, parent="root", marker="_branched_from", messages=1)
        store.write(lambda c: meta_set(c, sessions.TOMBSTONE_PREFIX + "gone", "{}"))
    target = tmp_path / "salvaged.db"
    result = run(scope, "sessions", "recover", "--output", str(target))
    assert result.code == 0, result.err
    assert "sessions" in result.out and "2 of 2" in result.out
    other = make_scope(tmp_path / "elsewhere", clock=scope.clock)
    with registry.open_unshared(other, target) as out:
        assert count(out, "SELECT COUNT(*) FROM messages") == 3
        assert session_row(out, "child")["parent_session_id"] == "root"  # type: ignore[index]
        stone = sessions.TOMBSTONE_PREFIX + "gone"
        assert count(out, "SELECT COUNT(*) FROM meta WHERE key = ?", stone) == 1
    again = run(scope, "sessions", "recover", "--output", str(target))
    assert again.code == 1 and "already exists" in again.err


# -- import ---------------------------------------------------------------------------------------


def test_import_round_trip(scope: Scope, tmp_path: Path) -> None:
    donor = make_scope(tmp_path / "donor", clock=scope.clock)
    with registry.open_unshared(donor) as store:
        aged(store, "carried", 2, title="carried over", messages=2)
        lines = list(portability.export_session(store, "carried"))
    payload = tmp_path / "in.jsonl"
    payload.write_text("".join(portability.dump_line(o) + "\n" for o in lines))
    result = run(scope, "sessions", "import", str(payload))
    assert result.code == 0, result.err
    assert "Imported 1 session(s)" in result.out
    assert "carried" in _ids(scope)
    again = run(scope, "sessions", "import", str(payload))
    assert again.code == 0 and "skipped: 1" in again.out


def test_import_bad_payload_imports_nothing(scope: Scope, tmp_path: Path) -> None:
    payload = tmp_path / "bad.jsonl"
    payload.write_text(json.dumps({"kind": "message", "session_id": "x"}) + "\nnot json\n")
    result = run(scope, "sessions", "import", str(payload))
    assert result.code == 1
    assert "Nothing was imported" in result.err and "line" in result.err
    missing = run(scope, "sessions", "import", str(tmp_path / "nope.jsonl"))
    assert missing.code == 1
