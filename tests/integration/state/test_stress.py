"""D-04.6: the store holds under real multi-process contention (P02-21, ACC-B1-4).

Four writer processes with eight threads each append through ``messages.append`` for
``INDUS_STRESS_SECONDS`` (default 60) while a fifth forces one VACUUM in the middle and a sixth
lists and searches in a loop. Afterwards: every append that returned an id is stored exactly
once, in call order per session; no caller ever saw ``StoreLocked`` or "database is locked"; the
file and the FTS indexes pass their integrity checks. A negative control with the append budget
cut to 0.01 s and a 1.5 s lock holder shows the same checker failing.

Each run prints one ``stress:`` line with its measurements (run with ``-s`` to see it).
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.clock import SystemClock
from indus.foundation.scope import EnvView, make_scope
from indus.state import fts, guards, journal, readpool, registry, schema
from indus.state.registry import StoreHandle
from tests.conftest import STRESS_SECONDS

pytestmark = pytest.mark.slow

WORKER = Path(__file__).with_name("stress_worker.py")
WRITERS = 4
THREADS = 8
PINNED_SQLITE = (3, 51, 3)
CONTROL_SECONDS = 4.0
HOLD_SECONDS = 1.5
_SEQ = re.compile(r"^stress w(\d+) t(\d+) seq (\d+) ")


@dataclass
class StressRun:
    home: Path
    db: Path
    seconds: float
    mode: str
    window: dict[str, float] = field(default_factory=lambda: {})
    results: dict[str, dict[str, Any]] = field(default_factory=lambda: {})

    def of(self, role: str) -> list[dict[str, Any]]:
        return [r for r in self.results.values() if r.get("role") == role]


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


def _spawn(
    run: StressRun, name: str, role: str, opts: dict[str, Any], logs: Path
) -> subprocess.Popen[str]:
    out = logs / f"{name}.json"
    err = (logs / f"{name}.stderr").open("w", encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, str(WORKER), role, str(run.home), str(run.db), str(out), json.dumps(opts)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=err,
        text=True,
        cwd=str(Path(__file__).resolve().parents[3]),
    )


def _stderr(logs: Path, name: str) -> str:
    return (logs / f"{name}.stderr").read_text(encoding="utf-8")[-4000:]


def run_stress(
    run: StressRun,
    logs: Path,
    *,
    pin: bool,
    budget: float | None = None,
    hold: float | None = None,
) -> StressRun:
    """Start every worker, release them together on one deadline, collect their results."""
    logs.mkdir(parents=True, exist_ok=True)
    plan: list[tuple[str, str, dict[str, Any]]] = [
        (f"writer{k}", "writer", {"index": k, "threads": THREADS}) for k in range(WRITERS)
    ]
    plan += [("vacuum", "vacuum", {}), ("reader", "reader", {})]
    if hold is not None:
        plan.append(("hold", "hold", {"hold_seconds": hold}))
    procs: dict[str, subprocess.Popen[str]] = {}
    try:
        for name, role, opts in plan:
            procs[name] = _spawn(
                run, name, role, {**opts, "pin_sqlite": pin, "budget": budget}, logs
            )
        for name, proc in procs.items():
            assert proc.stdout is not None
            line = proc.stdout.readline().split()
            assert line[:1] == ["ready"], f"{name} did not start:\n{_stderr(logs, name)}"
            if line[1] != "-":
                assert line[1] == run.mode, f"{name} opened the store in {line[1]}, not {run.mode}"
        start = time.time()
        run.window = {"start": start, "deadline": start + run.seconds}
        for proc in procs.values():
            assert proc.stdin is not None
            proc.stdin.write(json.dumps(run.window) + "\n")
            proc.stdin.close()
        for name, proc in procs.items():
            code = proc.wait(timeout=run.seconds + 120)
            assert code == 0, f"{name} exited {code}:\n{_stderr(logs, name)}"
            run.results[name] = json.loads((logs / f"{name}.json").read_text(encoding="utf-8"))
    finally:
        for proc in procs.values():
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=30)
    return run


# -- the checker ----------------------------------------------------------------------------------


def _caller_errors(run: StressRun) -> list[str]:
    found: list[str] = []
    for name, result in sorted(run.results.items()):
        if result.get("error_count"):
            kinds = sorted({e["type"] for e in result.get("errors", [])})
            found.append(f"{name}: {result['error_count']} exceptions reached callers {kinds}")
        for err in result.get("errors", []):
            if err["type"] == "StoreLocked":
                found.append(f"{name}: StoreLocked reached a caller: {err['text']}")
                break
        for err in result.get("errors", []):
            if "database is locked" in err["text"].lower():
                found.append(f"{name}: 'database is locked' reached a caller: {err['where']}")
                break
    return found


def _transcript_problems(run: StressRun, conn: sqlite3.Connection) -> list[str]:
    found: list[str] = []
    stored: dict[str, list[tuple[int, int]]] = {}
    for row in conn.execute(
        "SELECT id, session_id, content FROM messages WHERE session_id LIKE 'stress-w%' ORDER BY id"
    ):
        match = _SEQ.match(str(row["content"]))
        prefix = f"stress-w{match.group(1)}-t{match.group(2)}" if match else None
        if match is None or prefix != row["session_id"]:
            found.append(f"row {row['id']} in {row['session_id']} carries another message")
            continue
        stored.setdefault(str(row["session_id"]), []).append((int(row["id"]), int(match.group(3))))
    counts = {
        str(r["id"]): int(r["message_count"])
        for r in conn.execute("SELECT id, message_count FROM sessions WHERE id LIKE 'stress-w%'")
    }
    for writer in run.of("writer"):
        clean = not writer.get("error_count")
        for session, returned in writer["ids"].items():
            rows = stored.get(session, [])
            ids = [i for i, _ in rows]
            if len(set(returned)) != len(returned):
                found.append(f"{session}: an id was returned twice")
            missing = set(returned) - set(ids)
            if missing:
                found.append(f"{session}: {len(missing)} returned ids are not stored")
            seqs = [s for _, s in rows]
            if seqs != sorted(seqs) or len(set(seqs)) != len(seqs):
                found.append(f"{session}: stored order differs from append order")
            if [i for i in ids if i in set(returned)] != returned:
                found.append(f"{session}: returned ids are not in stored id order")
            if clean and ids != returned:
                found.append(
                    f"{session}: {len(ids) - len(returned)} stored rows nobody was told of"
                )
            if counts.get(session) != len(ids):
                found.append(f"{session}: message_count {counts.get(session)} != {len(ids)} rows")
    return found


def _maintenance_problems(run: StressRun, conn: sqlite3.Connection) -> list[str]:
    found: list[str] = []
    vacuums = run.of("vacuum")
    if len(vacuums) != 1 or not vacuums[0].get("ran"):
        found.append("the VACUUM did not run")
    else:
        vac = vacuums[0]
        before = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id LIKE 'stress-w%' AND timestamp < ?",
            (vac["began"],),
        ).fetchone()[0]
        after = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id LIKE 'stress-w%' AND timestamp > ?",
            (vac["ended"],),
        ).fetchone()[0]
        if not before or not after:
            found.append(f"the VACUUM did not overlap the appends ({before} before, {after} after)")
    readers = run.of("reader")
    if not readers or not sum(readers[0]["ops"].values()) or not sum(readers[0]["found"].values()):
        found.append("the reader made no progress or found nothing")
    return found


def _integrity_problems(store: StoreHandle) -> list[str]:
    found: list[str] = []
    with store.writer.borrow() as conn:
        check = [str(r[0]) for r in conn.execute("PRAGMA integrity_check").fetchall()]
        if check != ["ok"]:
            found.append(f"integrity_check: {check[:5]}")
        if fts.integrity_ok(conn) is not True:
            found.append("the FTS integrity-check failed")
        for key in (fts.STALE_KEY, fts.TRIGRAM_STALE_KEY):
            if schema.meta_get(conn, key) is not None:
                found.append(f"the FTS index was marked stale ({key})")
    return found


def problems(run: StressRun, store: StoreHandle) -> list[str]:
    """Everything wrong with a finished run (empty when the store held)."""
    found = _caller_errors(run)
    with store.read() as conn:
        found += _transcript_problems(run, conn)
        found += _maintenance_problems(run, conn)
    return found + _integrity_problems(store)


def metrics(run: StressRun) -> dict[str, Any]:
    writers, vac, reader = run.of("writer"), run.of("vacuum"), run.of("reader")
    appends = sum(w["appends"] for w in writers)
    p99 = [w["p99_wait"] for w in writers if w.get("p99_wait") is not None]
    out: dict[str, Any] = {
        "mode": run.mode,
        "sqlite": sqlite3.sqlite_version,
        "seconds": run.seconds,
        "appends": appends,
        "appends_per_s": round(appends / run.seconds, 1),
        "max_write_wait_s": round(max((w["max_wait"] for w in writers), default=0.0), 3),
        "p99_write_wait_s": round(max(p99), 4) if p99 else None,
    }
    if vac and vac[0].get("ran"):
        out["vacuum_s"] = round(vac[0]["ended"] - vac[0]["began"], 3)
        out["vacuum_bytes"] = [vac[0]["size_before"], vac[0]["size_after"]]
    if reader:
        out["reader_ops"] = sum(reader[0]["ops"].values())
        out["reader_max_wait_s"] = round(reader[0]["max_wait"], 3)
    return out


# -- the tests ------------------------------------------------------------------------------------


def _create_store(home: Path, monkeypatch: pytest.MonkeyPatch, *, pin: bool) -> StressRun:
    if pin:
        monkeypatch.setattr(journal, "linked_sqlite_version", lambda: PINNED_SQLITE)
    store = _open(home)
    try:
        return StressRun(home, store.path, STRESS_SECONDS, store.journal_mode)
    finally:
        store.close()


@pytest.mark.parametrize("journal_kind", ["host", "wal-pinned"])
def test_store_survives_the_stress(
    journal_kind: str, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pin = journal_kind == "wal-pinned"
    if pin and not journal.wal_reset_vulnerable(journal.linked_sqlite_version()):
        pytest.skip("the host SQLite already opens WAL; the host run covers it")
    run = _create_store(home, monkeypatch, pin=pin)
    if pin:
        assert run.mode == "wal", "the version pin did not give a WAL store"
    run_stress(run, tmp_path / "logs", pin=pin)
    print("stress:", json.dumps(metrics(run)))
    store = _open(home)
    try:
        assert problems(run, store) == []
    finally:
        store.close()


def test_negative_control_surfaces_store_locked(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _create_store(home, monkeypatch, pin=False)
    run.seconds = CONTROL_SECONDS
    run_stress(run, tmp_path / "logs", pin=False, budget=0.01, hold=HOLD_SECONDS)
    print("stress-control:", json.dumps(metrics(run)))
    locked = [
        e for w in run.of("writer") for e in w.get("errors", []) if e["type"] == "StoreLocked"
    ]
    assert locked, "a 0.01 s budget behind a 1.5 s lock hold never surfaced StoreLocked"
    assert "contention" in locked[0]["text"]
    store = _open(home)
    try:
        found = problems(run, store)
    finally:
        store.close()
    assert any("StoreLocked reached a caller" in p for p in found), found
