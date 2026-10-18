"""One process of the D-04.6 stress test (P02-21); the parent is ``test_stress.py``.

Run as ``python stress_worker.py <role> <home> <db> <out.json> <options-json>``. The worker opens
the store in ``home`` (a temp ``INDUS_HOME``; the isolation guard refuses a real root), prints
``ready``, reads one JSON line ``{"start": t, "deadline": t}`` (wall-clock seconds) from stdin,
works until the deadline and writes its result to ``out.json``. Roles:

- ``writer``: ``threads`` threads each append to their own session through ``messages.append``,
  recording every returned id in call order, each call's latency and every exception.
- ``vacuum``: waits for the window's midpoint, runs ``maintenance.vacuum(force=True)`` once.
- ``reader``: lists, fetches recent conversations and searches in a loop.
- ``hold``: (negative control) holds ``BEGIN IMMEDIATE`` on a plain connection from the midpoint
  for ``hold_seconds``.

Options: ``index``, ``threads``, ``pin_sqlite`` (patch the linked SQLite version like the unit
tests do, so a new store may use WAL), ``budget`` (negative control: the append budget in
seconds), ``hold_seconds``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from indus.foundation.clock import SystemClock
from indus.foundation.scope import EnvView, make_scope
from indus.state import journal, listing, maintenance, messages, registry, search, sessions, writer
from indus.state.registry import StoreHandle

PINNED_SQLITE = (3, 51, 3)
WORDS = ("amber", "basalt", "cobalt", "delta", "ember", "fjord", "garnet", "harbor", "indigo")
FILLER = "the quick brown fox jumps over the lazy dog while the kettle hums " * 3
MAX_KEPT_ERRORS = 50


def _until(moment: float) -> None:
    """Block until the wall-clock ``moment`` (a deadline wait, not a timed sleep)."""
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


def _open(home: Path) -> StoreHandle:
    env = EnvView.from_process().with_overlay({"INDUS_HOME": str(home)})
    return registry.open_unshared(make_scope(home, env=env, clock=SystemClock()))


def _content(index: int, thread: int, seq: int) -> str:
    word = WORDS[seq % len(WORDS)]
    return f"stress w{index} t{thread} seq {seq} {word} {FILLER[: 40 + (seq * 7) % 120]}"


def run_writer(
    store: StoreHandle, opts: dict[str, Any], window: dict[str, float]
) -> dict[str, Any]:
    index, threads = int(opts["index"]), int(opts.get("threads", 8))
    names = [f"stress-w{index}-t{j}" for j in range(threads)]
    ids: dict[str, list[int]] = {name: [] for name in names}
    waits: dict[str, list[float]] = {name: [] for name in names}
    attempts = dict.fromkeys(names, 0)
    errors = Errors()
    deadline = window["deadline"]

    def work(thread: int) -> None:
        name = names[thread]
        mine, lat = ids[name], waits[name]
        seq = 0
        while time.time() < deadline:
            msg = {
                "role": "user" if seq % 2 == 0 else "assistant",
                "content": _content(index, thread, seq),
            }
            began = time.monotonic()
            try:
                row_id = messages.append(store, name, msg)
            except Exception as exc:  # recorded and judged by the parent
                errors.add(f"{name} seq {seq}", exc)
                lat.append(time.monotonic() - began)
                seq += 1
                continue
            lat.append(time.monotonic() - began)
            mine.append(row_id)
            seq += 1
        attempts[name] = seq

    workers = [
        threading.Thread(target=work, args=(j,), name=f"w{index}-t{j}") for j in range(threads)
    ]
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    every = sorted(x for lat in waits.values() for x in lat)
    return {
        "role": "writer",
        "ids": ids,
        "attempts": attempts,
        "appends": sum(len(v) for v in ids.values()),
        "max_wait": every[-1] if every else 0.0,
        "p99_wait": every[int(len(every) * 0.99) - 1] if len(every) >= 100 else None,
        **errors.as_dict(),
    }


def run_vacuum(
    store: StoreHandle, opts: dict[str, Any], window: dict[str, float]
) -> dict[str, Any]:
    _until(window["start"] + (window["deadline"] - window["start"]) / 2)
    errors = Errors()
    began = time.time()
    result: dict[str, Any] = {"role": "vacuum", "began": began}
    try:
        report = maintenance.vacuum(store, force=True)
        result.update(size_before=report.size_before, size_after=report.size_after, ran=True)
    except Exception as exc:
        errors.add("vacuum", exc)
        result["ran"] = False
    result["ended"] = time.time()
    result.update(errors.as_dict())
    return result


def run_reader(
    store: StoreHandle, opts: dict[str, Any], window: dict[str, float]
) -> dict[str, Any]:
    errors = Errors()
    ops: list[tuple[str, Callable[[], int]]] = [
        ("list", lambda: len(listing.list_sessions(store, limit=20))),
        ("recent", lambda: len(listing.recent(store, limit=10))),
        ("search-word", lambda: len(search.search(store, "cobalt", limit=20))),
        ("search-phrase", lambda: len(search.search(store, '"seq 7"', limit=20))),
        ("search-prefix", lambda: len(search.search(store, "harb*", sort="newest", limit=20))),
        ("search-substring", lambda: len(search.search(store, "arbo", limit=20))),
    ]
    counts = {name: 0 for name, _ in ops}
    found = {name: 0 for name, _ in ops}
    worst = 0.0
    n = 0
    while time.time() < window["deadline"]:
        name, op = ops[n % len(ops)]
        n += 1
        began = time.monotonic()
        try:
            found[name] += op()
        except Exception as exc:
            errors.add(name, exc)
        worst = max(worst, time.monotonic() - began)
        counts[name] += 1
    return {"role": "reader", "ops": counts, "found": found, "max_wait": worst, **errors.as_dict()}


def run_hold(db: Path, opts: dict[str, Any], window: dict[str, float]) -> dict[str, Any]:
    _until(window["start"] + (window["deadline"] - window["start"]) / 2)
    conn = sqlite3.connect(str(db), timeout=30.0, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        began = time.time()
        _until(began + float(opts.get("hold_seconds", 1.5)))
        conn.execute("COMMIT")
        return {"role": "hold", "began": began, "ended": time.time(), "error_count": 0}
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    role, home, db, out = argv[1], Path(argv[2]), Path(argv[3]), Path(argv[4])
    opts: dict[str, Any] = json.loads(argv[5]) if len(argv) > 5 else {}
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    if opts.get("pin_sqlite"):
        journal.linked_sqlite_version = lambda: PINNED_SQLITE  # same override as the unit tests
    if opts.get("budget") is not None:
        # The negative control: appends run under TRANSCRIPT, so bind it to the patched ROUTINE.
        writer.ROUTINE = writer.Budget("routine", float(opts["budget"]))
        messages.TRANSCRIPT = writer.ROUTINE
    store = None
    if role != "hold":
        store = _open(home)
        if role == "writer":
            for j in range(int(opts.get("threads", 8))):
                sessions.create(store, f"stress-w{opts['index']}-t{j}", "cli")
    print(f"ready {store.journal_mode if store else '-'}", flush=True)
    window = json.loads(sys.stdin.readline())
    try:
        if role == "writer":
            assert store is not None
            result = run_writer(store, opts, window)
        elif role == "vacuum":
            assert store is not None
            result = run_vacuum(store, opts, window)
        elif role == "reader":
            assert store is not None
            result = run_reader(store, opts, window)
        elif role == "hold":
            result = run_hold(db, opts, window)
        else:
            raise SystemExit(f"unknown role {role!r}")
        result["mode"] = store.journal_mode if store else None
    finally:
        if store is not None:
            store.close()
    out.write_text(json.dumps(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
