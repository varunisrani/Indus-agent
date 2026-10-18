"""VACUUM refuses while another process holds the store file (P02-19, R03.25).

A child process opens the store with plain ``sqlite3`` and keeps it open; the Linux holder scan
of ``/proc/*/fd`` must name that pid, manual VACUUM must refuse (unless forced), automatic
maintenance must only skip, and once the child exits VACUUM runs.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from indus.foundation.scope import Scope
from indus.state import guards, maintenance, readpool, registry
from indus.state.maintenance import VacuumRefused
from indus.state.registry import StoreHandle

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="holder scan is Linux-only"),
]

_KEEP_OPEN = (
    "import sqlite3, sys\n"
    "conn = sqlite3.connect(sys.argv[1])\n"
    "conn.execute('SELECT COUNT(*) FROM sessions').fetchone()\n"
    "print('open', flush=True)\n"
    "sys.stdin.readline()\n"
    "conn.close()\n"
)


@pytest.fixture
def store(scope: Scope) -> Iterator[StoreHandle]:
    guards.reset_health()
    readpool.reset_fd_cache()
    handle = registry.open_unshared(scope)
    yield handle
    handle.close()
    registry.close_all()


def _hold(path: Path) -> subprocess.Popen[str]:
    child = subprocess.Popen(
        [sys.executable, "-c", _KEEP_OPEN, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "open"
    return child


def _release(child: subprocess.Popen[str]) -> None:
    if child.poll() is None:
        assert child.stdin is not None
        child.stdin.close()
        child.wait(timeout=30)


def test_second_process_blocks_vacuum(store: StoreHandle) -> None:
    child = _hold(store.path)
    try:
        holders = maintenance.scan_holders(store.path)
        assert child.pid in [h.pid for h in holders]
        with pytest.raises(VacuumRefused) as caught:
            maintenance.vacuum(store)
        assert child.pid in [h.pid for h in caught.value.holders]
        assert str(child.pid) in str(caught.value)
        forced = maintenance.vacuum(store, force=True)
        assert forced.forced and child.pid in [h.pid for h in forced.holders]
    finally:
        _release(child)
    assert maintenance.scan_holders(store.path) == []
    report = maintenance.vacuum(store)
    assert report.holders == [] and not report.forced


def test_auto_maintenance_only_skips_with_a_holder(store: StoreHandle, scope: Scope) -> None:
    now = store.scope.clock.now().timestamp()
    old = now - 100 * 86_400.0

    def seed(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO sessions (id, source, started_at, ended_at, end_reason) "
            "VALUES ('old', 'cli', ?, ?, 'cli_close')",
            (old, old),
        )
        conn.executemany(
            "INSERT INTO messages (session_id, role, content, timestamp) "
            "VALUES ('old', 'user', ?, ?)",
            [("z" * 4000, old)] * 400,
        )

    store.write(seed)

    class _On:
        def get(self, path: str, /) -> object:
            return {"sessions.auto_prune": True, "sessions.retention_days": 30}.get(path)

    child = _hold(store.path)
    try:
        report = maintenance.auto_maintain(scope.with_config(_On()), store=store)
    finally:
        _release(child)
    assert report.error is None and report.pruned == 1
    assert report.holders >= 1 and not report.vacuumed
