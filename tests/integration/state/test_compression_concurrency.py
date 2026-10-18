"""A writer process appends while this process publishes continuations: no append is lost (R03.10).

For every rotation the rows the writer committed to the parent after the watermark and before the
publish transaction (their ids lie between the watermark and the child's first id, because SQLite
serialises write transactions and ids only grow) must all reappear in the child, in order, with
their original uids, right after the handoff rows.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from indus.foundation.clock import SystemClock
from indus.foundation.scope import EnvView, make_scope
from indus.state import compression, holders, registry

ITERATIONS = 200
SUMMARY = [
    {"role": "user", "content": "summary so far"},
    {"role": "assistant", "content": "carrying on"},
]
WRITER = Path(__file__).with_name("compression_writer.py")


def _point_writer_at(current: Path, session_id: str) -> None:
    staging = current.with_suffix(".tmp")
    staging.write_text(session_id, encoding="utf-8")
    os.replace(staging, current)


@pytest.mark.slow
def test_no_append_is_lost_while_publishing(isolated_home: Path, tmp_path: Path) -> None:
    home = isolated_home / "profiles" / "work"
    home.mkdir(parents=True, exist_ok=True)
    (home / "profile.yaml").touch()
    env = EnvView.from_process().with_overlay({"INDUS_HOME": str(home)})
    store = registry.open_unshared(make_scope(home, env=env, clock=SystemClock()))
    parents = [f"p{i:03d}" for i in range(ITERATIONS)]
    started = store.scope.clock.now().timestamp()

    def seed(conn: sqlite3.Connection) -> None:
        for sid in parents:
            conn.execute(
                "INSERT INTO sessions (id, source, started_at) VALUES (?, 'cli', ?)",
                (sid, started),
            )
            for n in range(3):
                conn.execute(
                    "INSERT INTO messages (session_id, role, content, timestamp) "
                    "VALUES (?, 'user', ?, ?)",
                    (sid, f"seed {n}", started),
                )

    try:
        store.write(seed)
        current, stop = tmp_path / "current", tmp_path / "stop"
        writer = subprocess.Popen(
            [sys.executable, "-I", str(WRITER), str(store.path), str(current), str(stop)]
        )
        marks: dict[str, int] = {}
        holder = holders.holder_stamp() + ":test"
        summary_time = threading.Event()
        try:
            for i, sid in enumerate(parents):
                _point_writer_at(current, sid)
                marks[sid] = compression.watermark(store, sid)
                assert compression.acquire(store, sid, holder)
                summary_time.wait(0.003)  # the slow summary: the writer keeps appending
                compression.publish_continuation(
                    store, sid, {"id": f"c{i:03d}"}, SUMMARY, marks[sid], holder
                )
                assert compression.release(store, sid, holder)
        finally:
            stop.touch()
            writer.wait(timeout=60)
        assert writer.returncode == 0

        carried = 0
        with store.read() as conn:
            for i, sid in enumerate(parents):
                child = conn.execute(
                    "SELECT id, message_uid, content FROM messages WHERE session_id = ? "
                    "ORDER BY id",
                    (f"c{i:03d}",),
                ).fetchall()
                assert [r["content"] for r in child[:2]] == ["summary so far", "carrying on"]
                expected = conn.execute(
                    "SELECT message_uid FROM messages WHERE session_id = ? AND id > ? AND id < ? "
                    "ORDER BY id",
                    (sid, marks[sid], child[0]["id"]),
                ).fetchall()
                assert [r["message_uid"] for r in child[2:]] == [r["message_uid"] for r in expected]
                carried += len(expected)
                end = conn.execute(
                    "SELECT end_reason FROM sessions WHERE id = ?", (sid,)
                ).fetchone()
                assert end["end_reason"] == "compression"
        assert carried > 0, "the writer never raced a publish; the test proved nothing"
    finally:
        store.close()
