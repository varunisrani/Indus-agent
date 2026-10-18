"""Writer process for the compression concurrency test: keeps appending to whichever session the
test names in a small file, using its own SQLite connection, until a stop file appears.

Run as ``python -I compression_writer.py <db> <current-file> <stop-file>``; standard library only.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import threading


def main(db: str, current: str, stop: str) -> int:
    conn = sqlite3.connect(db, timeout=30.0, isolation_level=None)
    pause = threading.Event()
    written = 0
    while not os.path.exists(stop):
        try:
            with open(current, encoding="utf-8") as fh:
                target = fh.read().strip()
        except OSError:
            target = ""
        if not target:
            pause.wait(0.001)
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp) "
                "VALUES (?, 'user', ?, 1767258000.0)",
                (target, f"from-writer {written}"),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        written += 1
        pause.wait(0.0005)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3]))
