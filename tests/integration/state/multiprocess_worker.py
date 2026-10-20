"""One process of the P17-21 multi-process run; the parent is ``test_multiprocess_daemon.py``.

Run as ``python multiprocess_worker.py <role> <home> <out.json> <options-json>``. The worker builds
a scope for the temp ``home``, prints ``ready <journal mode or ->``, reads one JSON line
``{"start": t, "deadline": t}`` (wall-clock seconds) from stdin, works until the deadline and
writes its result to ``out.json``. Roles:

- ``daemon``: holds the profile ownership lock (``gateway.lock``) as the gateway daemon does and
  writes turns until the deadline: ``threads`` threads, each creating a session per turn and
  appending a user and an assistant message through the P02 writer. At the window's midpoint it
  runs a WAL checkpoint on its writer connection.
- ``cli``: what `indus sessions list` and a transcript read do, once per loop: open the store
  read-only (``read_view``), list sessions, read the newest session's messages, close.
- ``dashboard``: one long-lived read-only connection (a dashboard keeps one), searching and
  counting in a loop.
- ``maint``: at the midpoint, runs `indus sessions prune -y` through the app's dispatch; with the
  daemon owning the profile the gate must refuse it.

Every reader samples the store file's header (bytes 18-19: 1 = rollback journal, 2 = WAL) and the
journal mode its connection reports, so the parent can tell whether the mode ever changed live.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any

from indus.foundation.clock import SystemClock
from indus.foundation.scope import EnvView, Scope, make_scope
from indus.state import journal, listing, messages, registry, search, sessions
from indus.state.registry import StoreHandle

PINNED_SQLITE = (3, 51, 3)
MAX_KEPT_ERRORS = 50
WORDS = ("amber", "basalt", "cobalt", "delta", "ember", "fjord")


def _until(moment: float) -> None:
    gate = threading.Event()
    while (left := moment - time.time()) > 0:
        gate.wait(left)


class Errors:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.count = 0
        self.kept: list[dict[str, Any]] = []

    def add(self, where: str, exc: BaseException) -> None:
        with self.lock:
            self.count += 1
            if len(self.kept) < MAX_KEPT_ERRORS:
                self.kept.append({"where": where, "type": type(exc).__name__, "text": str(exc)})

    def as_dict(self) -> dict[str, Any]:
        return {"error_count": self.count, "errors": self.kept}


def _scope(home: Path) -> Scope:
    env = EnvView.from_process().with_overlay({"INDUS_HOME": str(home)})
    return make_scope(home, env=env, clock=SystemClock())


def header_mode(db: Path) -> int | None:
    """Bytes 18-19 of the file header: 1 rollback journal, 2 WAL (None when unreadable)."""
    try:
        with db.open("rb") as raw:
            head = raw.read(20)
    except OSError:
        return None
    if len(head) < 20 or head[18] != head[19]:
        return None
    return head[18]


def run_daemon(scope: Scope, opts: dict[str, Any], window: dict[str, float]) -> dict[str, Any]:
    from indus.service.maintenance_gate import OwnershipLock

    lock = OwnershipLock(scope.home)
    if not lock.try_acquire():
        raise SystemExit("the daemon could not take the ownership lock")
    store = registry.open_unshared(scope)
    threads = int(opts.get("threads", 4))
    turns: dict[str, list[list[int]]] = {f"t{j}": [] for j in range(threads)}
    errors = Errors()
    checkpoint: dict[str, Any] = {}
    deadline = window["deadline"]
    midpoint = window["start"] + (deadline - window["start"]) / 2

    def work(thread: int) -> None:
        name = f"t{thread}"
        n = 0
        while time.time() < deadline:
            sid = f"turn-{name}-{n:05d}"
            try:
                sessions.create(store, sid, "cli")
                ids: list[int] = []
                for role in ("user", "assistant"):
                    text = f"{sid} {role} {WORDS[n % len(WORDS)]} " + "words " * (n % 40)
                    ids.append(messages.append(store, sid, {"role": role, "content": text}))
                turns[name].append(ids)
            except Exception as exc:  # judged by the parent
                errors.add(sid, exc)
            n += 1

    def checkpointer() -> None:
        _until(midpoint)
        try:
            with store.writer.borrow() as conn:
                row = conn.execute("PRAGMA wal_checkpoint(RESTART)").fetchone()
            checkpoint.update(ran=True, at=time.time(), row=list(row) if row else None)
        except Exception as exc:
            errors.add("checkpoint", exc)
            checkpoint["ran"] = False

    workers = [threading.Thread(target=work, args=(j,)) for j in range(threads)]
    workers.append(threading.Thread(target=checkpointer))
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    mode = store.journal_mode
    store.close()
    lock.release()
    return {
        "role": "daemon",
        "turns": turns,
        "count": sum(len(v) for v in turns.values()),
        "checkpoint": checkpoint,
        "mode": mode,
        **errors.as_dict(),
    }


def _sample(db: Path, conn: sqlite3.Connection, seen: dict[str, set[Any]]) -> None:
    seen["header"].add(header_mode(db))
    seen["pragma"].add(str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower())


def run_cli(scope: Scope, opts: dict[str, Any], window: dict[str, float]) -> dict[str, Any]:
    from indus.cli.commands.sessions import read_view

    errors = Errors()
    seen: dict[str, set[Any]] = {"header": set(), "pragma": set()}
    loops = rows = 0
    db = Path(opts["db"])
    while time.time() < window["deadline"]:
        loops += 1
        try:
            with read_view(scope) as view:
                assert view is not None
                found = listing.list_sessions(view.as_handle(), limit=20)
                with view.read() as conn:
                    _sample(db, conn, seen)
                    if found:
                        rows += len(
                            conn.execute(
                                "SELECT id FROM messages WHERE session_id = ? ORDER BY id",
                                (found[0].id,),
                            ).fetchall()
                        )
        except Exception as exc:
            errors.add(f"loop {loops}", exc)
    return {
        "role": "cli",
        "loops": loops,
        "rows": rows,
        "header": sorted(x for x in seen["header"] if x is not None),
        "pragma": sorted(seen["pragma"]),
        **errors.as_dict(),
    }


def run_dashboard(scope: Scope, opts: dict[str, Any], window: dict[str, float]) -> dict[str, Any]:
    from indus.cli.commands.sessions import ReadView
    from indus.state import browse

    errors = Errors()
    seen: dict[str, set[Any]] = {"header": set(), "pragma": set()}
    loops = hits = 0
    db = Path(opts["db"])
    store = browse.open_readonly(scope)
    view = ReadView(scope, store)
    try:
        while time.time() < window["deadline"]:
            loops += 1
            word = WORDS[loops % len(WORDS)]
            try:
                hits += len(search.search(view.as_handle(), word, limit=20))
                with view.read() as conn:
                    conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
                    _sample(db, conn, seen)
            except Exception as exc:
                errors.add(f"loop {loops}", exc)
    finally:
        store.close()
    return {
        "role": "dashboard",
        "loops": loops,
        "hits": hits,
        "header": sorted(x for x in seen["header"] if x is not None),
        "pragma": sorted(seen["pragma"]),
        **errors.as_dict(),
    }


def run_maint(scope: Scope, opts: dict[str, Any], window: dict[str, float]) -> dict[str, Any]:
    from click.testing import CliRunner

    from indus.cli import app as app_mod
    from indus.cli.args import CliState

    _until(window["start"] + (window["deadline"] - window["start"]) / 2)
    state = CliState(scope=scope, stdin_isatty=lambda: False, stdout_isatty=lambda: False)
    with CliRunner().isolation() as (out, err, _mixed):
        code = app_mod.dispatch(["sessions", "prune", "-y"], state)
    return {
        "role": "maint",
        "code": code,
        "out": out.getvalue().decode(),
        "err": err.getvalue().decode(),
        "error_count": 0,
    }


def main(argv: list[str]) -> int:
    role, home, out = argv[1], Path(argv[2]), Path(argv[3])
    opts: dict[str, Any] = json.loads(argv[4]) if len(argv) > 4 else {}
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    if opts.get("pin_sqlite"):
        journal.linked_sqlite_version = lambda: PINNED_SQLITE  # same override as the unit tests
    scope = _scope(home)
    store: StoreHandle | None = None
    mode = "-"
    if role == "daemon":
        store = registry.open_unshared(scope)
        mode = store.journal_mode
        store.close()
    print(f"ready {mode}", flush=True)
    window = json.loads(sys.stdin.readline())
    runners = {"daemon": run_daemon, "cli": run_cli, "dashboard": run_dashboard, "maint": run_maint}
    result = runners[role](scope, opts, window)
    out.write_text(json.dumps(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
