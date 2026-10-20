"""P17-21: a daemon writing turns beside CLI and dashboard readers, for ``INDUS_STRESS_SECONDS``.

Five processes share one store (``multiprocess_worker.py``): the "daemon" holds the profile
ownership lock and writes turns (a session and a user and an assistant message each) with four
threads, running a WAL checkpoint at the midpoint; two CLI-style readers open the store read-only
per loop (`indus sessions list` plus a transcript read); a dashboard-style reader keeps one
read-only connection and searches; a maintenance process runs `indus sessions prune -y` at the
midpoint. Afterwards:

- every message the daemon was told was stored is stored exactly once, in its turn's order, and
  each turn's session counts its two messages (no lost rows);
- no process saw "database is locked" or any other error;
- the journal mode never changed live: every process saw the mode the store was created with,
  in the PRAGMA and in the file header (R03.1);
- the prune was refused by the maintenance gate (the daemon owns the profile) and the old
  sessions it would have deleted are still there (R09.21);
- the file passes its integrity check.

The run is in WAL mode: when the host SQLite is one whose WAL could be reset, the linked version
is pinned as the unit tests do so the store still opens in WAL.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.clock import SystemClock
from indus.foundation.scope import EnvView, make_scope
from indus.state import guards, journal, readpool, registry
from indus.state.registry import StoreHandle
from tests.conftest import STRESS_SECONDS

pytestmark = pytest.mark.slow

WORKER = Path(__file__).with_name("multiprocess_worker.py")
PINNED_SQLITE = (3, 51, 3)
HEADER_WAL = 2
OLD = ("old-a", "old-b", "old-c")


@pytest.fixture
def home(isolated_home: Path) -> Iterator[Path]:
    guards.reset_health()
    readpool.reset_fd_cache()
    journal.reset_once_logs()
    path = isolated_home / "profiles" / "work"
    path.mkdir(parents=True, exist_ok=True)
    (path / "profile.yaml").touch()
    yield path
    registry.close_all()
    guards.reset_health()
    readpool.reset_fd_cache()


def _open(home: Path) -> StoreHandle:
    env = EnvView.from_process().with_overlay({"INDUS_HOME": str(home)})
    return registry.open_unshared(make_scope(home, env=env, clock=SystemClock()))


def _seed_old(store: StoreHandle) -> None:
    """Sessions a bare prune would delete (ended 100 days ago)."""
    from tests.unit.state._helpers_d import DAY, add_message, add_session

    at = store.scope.clock.now().timestamp() - 100 * DAY
    for sid in OLD:
        add_session(store, sid, started_at=at, end_reason="cli_close", ended_at=at, message_count=1)
        add_message(store, sid, "an old conversation", timestamp=at)


def _run(home: Path, db: Path, logs: Path, pin: bool) -> dict[str, dict[str, Any]]:
    plan = [
        ("daemon", "daemon"),
        ("cli1", "cli"),
        ("cli2", "cli"),
        ("dashboard", "dashboard"),
        ("maint", "maint"),
    ]
    logs.mkdir(parents=True, exist_ok=True)
    procs: dict[str, subprocess.Popen[str]] = {}
    results: dict[str, dict[str, Any]] = {}
    root = str(Path(__file__).resolve().parents[3])
    opts = json.dumps({"pin_sqlite": pin, "db": str(db), "threads": 4})
    try:
        for name, role in plan:
            err = (logs / f"{name}.stderr").open("w", encoding="utf-8")
            procs[name] = subprocess.Popen(
                [sys.executable, str(WORKER), role, str(home), str(logs / f"{name}.json"), opts],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=err,
                text=True,
                cwd=root,
            )
            assert procs[name].stdout is not None
            line = procs[name].stdout.readline().split()  # type: ignore[union-attr]
            stderr = (logs / f"{name}.stderr").read_text(encoding="utf-8")[-3000:]
            assert line[:1] == ["ready"], f"{name} did not start:\n{stderr}"
        start = time.time()
        window = {"start": start, "deadline": start + STRESS_SECONDS}
        for proc in procs.values():
            assert proc.stdin is not None
            proc.stdin.write(json.dumps(window) + "\n")
            proc.stdin.close()
        for name, proc in procs.items():
            code = proc.wait(timeout=STRESS_SECONDS + 120)
            stderr = (logs / f"{name}.stderr").read_text(encoding="utf-8")[-3000:]
            assert code == 0, f"{name} exited {code}:\n{stderr}"
            results[name] = json.loads((logs / f"{name}.json").read_text(encoding="utf-8"))
    finally:
        for proc in procs.values():
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=30)
    return results


def _problems(results: dict[str, dict[str, Any]], store: StoreHandle, mode: str) -> list[str]:
    found: list[str] = []
    for name, result in sorted(results.items()):
        if result.get("error_count"):
            kinds = sorted({e["type"] for e in result.get("errors", [])})
            found.append(f"{name}: {result['error_count']} errors reached callers {kinds}")
        for err in result.get("errors", []):
            if "database is locked" in err["text"].lower():
                found.append(f"{name}: 'database is locked' surfaced at {err['where']}")
                break
    daemon = results["daemon"]
    if daemon["count"] < 10:
        found.append(f"the daemon wrote only {daemon['count']} turns")
    if not daemon["checkpoint"].get("ran"):
        found.append("the midpoint checkpoint did not run")
    if daemon["mode"] != mode:
        found.append(f"the daemon ended in {daemon['mode']}, created in {mode}")
    for name in ("cli1", "cli2", "dashboard"):
        result = results[name]
        if not result["loops"]:
            found.append(f"{name} made no progress")
        if result["pragma"] != [mode]:
            found.append(f"{name} saw journal modes {result['pragma']}, not only {mode}")
        if mode == "wal" and result["header"] != [HEADER_WAL]:
            found.append(f"{name} saw file header modes {result['header']}")
    if not results["dashboard"]["hits"]:
        found.append("the dashboard search found nothing")
    maint = results["maint"]
    if maint["code"] != 1 or "Try again in a moment" not in maint["err"]:
        found.append(f"the prune was not refused by the gate: {maint}")
    with store.read() as conn:
        stored: dict[str, list[int]] = {}
        for row in conn.execute(
            "SELECT id, session_id FROM messages WHERE session_id LIKE 'turn-%' ORDER BY id"
        ):
            stored.setdefault(str(row[1]), []).append(int(row[0]))
        counts = {
            str(r[0]): int(r[1])
            for r in conn.execute(
                "SELECT id, message_count FROM sessions WHERE id LIKE 'turn-%'"
            ).fetchall()
        }
        old = {
            str(r[0])
            for r in conn.execute(
                f"SELECT id FROM sessions WHERE id IN ({', '.join('?' for _ in OLD)})", OLD
            )
        }
        check = [str(r[0]) for r in conn.execute("PRAGMA integrity_check").fetchall()]
    told = 0
    for thread, turns in daemon["turns"].items():
        for n, ids in enumerate(turns):
            told += len(ids)
            sid_rows = [sid for sid, rows in stored.items() if rows == ids]
            if len(sid_rows) != 1:
                found.append(f"{thread} turn {n}: its messages {ids} are not stored as one turn")
    if told != sum(len(v) for v in stored.values()):
        found.append(f"{told} messages reported, {sum(len(v) for v in stored.values())} stored")
    wrong = [sid for sid, rows in stored.items() if counts.get(sid) != len(rows)]
    if wrong:
        found.append(f"{len(wrong)} sessions' message_count differs from their rows")
    if old != set(OLD):
        found.append(f"the refused prune deleted {set(OLD) - old}")
    if check != ["ok"]:
        found.append(f"integrity_check: {check[:5]}")
    return found


def test_daemon_writer_and_readers_share_the_store(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pin = journal.wal_reset_vulnerable(journal.linked_sqlite_version())
    if pin:
        monkeypatch.setattr(journal, "linked_sqlite_version", lambda: PINNED_SQLITE)
    store = _open(home)
    try:
        mode = store.journal_mode
        db = store.path
        _seed_old(store)
    finally:
        store.close()
    assert mode == "wal", "the store did not open in WAL"
    results = _run(home, db, tmp_path / "logs", pin)
    summary = {
        "seconds": STRESS_SECONDS,
        "turns": results["daemon"]["count"],
        "cli_loops": [results["cli1"]["loops"], results["cli2"]["loops"]],
        "dashboard_loops": results["dashboard"]["loops"],
        "checkpoint": results["daemon"]["checkpoint"].get("row"),
    }
    print("multiprocess:", json.dumps(summary))
    store = _open(home)
    try:
        assert _problems(results, store, mode) == []
    finally:
        store.close()
