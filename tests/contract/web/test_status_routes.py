"""P18-13: the public status probe (pressure thresholds, daemon state, fail-safe collectors)."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.state import gateway_runtime as runtime
from indus.state import registry, sessions
from indus.web.app import AppOptions
from indus.web.auth.basic import hash_password
from indus.web.routes import status
from indus.web.routes.status import GIB, MIB, Amount, daemon_view, disk_pressure, memory_pressure
from tests.contract.web.rest_app import RestApp, build_rest_app, route_table
from tests.fakes.clock import FakeClock


@pytest.fixture
def rest(scope: Scope) -> Iterator[RestApp]:
    built = build_rest_app(scope)
    yield built
    built.close()


# -- pressure tables -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("available", "total", "expected"),
    [
        (64 * MIB, 1 * GIB, "elevated"),  # at the critical floor (not under it), under 128 MiB
        (64 * MIB - 1, 1 * GIB, "critical"),
        (128 * MIB, 512 * MIB, "ok"),  # at the elevated floor, 25 % available
        (128 * MIB - 1, 512 * MIB, "elevated"),
        (5 * GIB, 100 * GIB, "elevated"),  # exactly 5 %
        (5 * GIB - 1, 100 * GIB, "critical"),
        (15 * GIB, 100 * GIB, "ok"),  # exactly 15 %
        (15 * GIB - 1, 100 * GIB, "elevated"),
        (10 * GIB, 16 * GIB, "ok"),
        (1, 0, "unknown"),
    ],
)
def test_memory_pressure_table(available: int, total: int, expected: str) -> None:
    assert memory_pressure(available, total) == expected


@pytest.mark.parametrize(
    ("free", "total", "expected"),
    [
        (256 * MIB, 4 * GIB, "elevated"),  # at the critical floor, 93.75 % used
        (256 * MIB - 1, 4 * GIB, "critical"),
        (512 * MIB, 2 * GIB, "ok"),  # at the elevated floor, 75 % used
        (512 * MIB - 1, 2 * GIB, "elevated"),
        (512 * MIB, 10 * GIB, "critical"),  # exactly 95 % used with < 1 GiB free
        (512 * MIB + 1, 10 * GIB, "elevated"),  # just under 95 %
        (1 * GIB, 20 * GIB, "elevated"),  # 95 % used with exactly 1 GiB: headroom is enough
        (1 * GIB - 1, 20 * GIB, "critical"),
        (3 * GIB, 20 * GIB, "elevated"),  # exactly 85 % used with < 4 GiB free
        (3 * GIB + 1, 20 * GIB, "ok"),  # just under 85 %
        (4 * GIB, 100 * GIB, "ok"),  # 96 % used with exactly 4 GiB: headroom is enough
        (4 * GIB - 1, 100 * GIB, "elevated"),
        (50 * GIB, 100 * GIB, "ok"),
        (0, 0, "unknown"),
    ],
)
def test_disk_pressure_table(free: int, total: int, expected: str) -> None:
    assert disk_pressure(free, total) == expected


# -- the probe -------------------------------------------------------------------------------------


def test_status_is_public_and_carries_no_paths_or_pids(rest: RestApp, scope: Scope) -> None:
    _write_status(scope, "running", age_s=10)
    answer = rest.anonymous().get("/api/status")
    assert answer.status_code == 200
    assert answer.headers["cache-control"] == "no-store"
    body = answer.json()
    assert body["daemon"] == {"state": "running", "heartbeat_age_s": 10}
    assert body["auth_required"] is False and isinstance(body["auth_providers"], list)
    assert body["platforms"] == [{"name": "telegram", "state": "connected"}]
    text = answer.text
    assert str(scope.home) not in text and str(os.getpid()) not in text
    assert "pid" not in text and "path" not in text and "host" not in text
    assert set(body) == {
        "version",
        "daemon",
        "platforms",
        "active_sessions",
        "recent_sessions",
        "auth_required",
        "auth_providers",
        "components",
        "overall",
        "memory",
        "disk",
    }


def test_status_is_public_in_gated_mode_too(scope: Scope) -> None:
    password_hash = hash_password("correct horse", memory_kib=1024, iterations=1, lanes=1)
    config = {"dashboard": {"basic_auth": {"username": "admin", "password_hash": password_hash}}}
    built = build_rest_app(scope, options=AppOptions(host="0.0.0.0", config=config))
    try:
        anon = built.anonymous()
        answer = anon.get("/api/status")
        assert answer.status_code == 200
        assert answer.json()["auth_required"] is True
        assert answer.json()["auth_providers"] == ["basic"]
        assert anon.get("/api/logs").status_code == 401  # the neighbours stay gated
    finally:
        built.close()


def test_a_raising_collector_reads_unknown(scope: Scope) -> None:
    def broken() -> Amount:
        raise OSError("no meminfo here")

    built = build_rest_app(scope, extras={"status.memory": broken, "status.disk": broken})
    try:
        body = built.client.get("/api/status").json()
        assert body["memory"] == {"pressure": "unknown"}
        assert body["disk"] == {"pressure": "unknown"}
        assert "daemon" in body and body["version"]
    finally:
        built.close()


def test_collectors_report_pressure_and_coarse_numbers(scope: Scope) -> None:
    extras = {
        "status.memory": lambda: Amount(100 * MIB, 1 * GIB),
        "status.disk": lambda: Amount(3 * GIB, 20 * GIB),
    }
    built = build_rest_app(scope, extras=extras)
    try:
        body = built.client.get("/api/status").json()
        assert body["memory"] == {"pressure": "elevated", "available_percent": 9}
        assert body["disk"] == {"pressure": "elevated", "used_percent": 85, "free_gb": 3}
    finally:
        built.close()


def test_status_on_a_fresh_home_creates_no_store(rest: RestApp, scope: Scope) -> None:
    before = sorted(p.name for p in scope.home.iterdir())
    body = rest.client.get("/api/status").json()
    assert body["active_sessions"] == 0 and body["recent_sessions"] == 0
    assert body["components"]["store"] == {"status": "ok"}
    assert body["daemon"]["state"] == "stopped"
    assert sorted(p.name for p in scope.home.iterdir()) == before


def test_active_sessions_counts_open_recent_sessions(
    rest: RestApp, scope: Scope, fake_clock: FakeClock
) -> None:
    now = fake_clock.now().timestamp()
    store = registry.open_unshared(scope)
    try:
        for index in range(25):  # 25 old ended sessions, outside the recent window
            sessions.create(store, f"old-{index}", "cli", started_at=now - 90_000 - index)
            sessions.end(store, f"old-{index}", "user_exit")
        sessions.create(store, "open-fresh", "cli", started_at=now - 60)
        sessions.create(store, "open-quiet", "cli", started_at=now - 600)
        sessions.create(store, "ended-fresh", "cli", started_at=now - 30)
        sessions.end(store, "ended-fresh", "user_exit")

        def stamp(conn: sqlite3.Connection) -> None:
            conn.execute("UPDATE sessions SET last_activity_at = started_at")

        store.write(stamp)
    finally:
        store.close()
    body = rest.client.get("/api/status").json()
    assert body["active_sessions"] == 1
    assert body["recent_sessions"] == 20


def test_components_roll_up(rest: RestApp, scope: Scope) -> None:
    rows = {
        "telegram": runtime.PlatformStatus("connected"),
        "slack": runtime.PlatformStatus("paused"),
    }
    _write_status(scope, "draining", age_s=2, platforms=rows)
    body = rest.client.get("/api/status").json()
    assert body["components"]["daemon"] == {"status": "ok", "state": "draining"}
    assert body["components"]["platforms"] == {"status": "ok", "configured": 1, "connected": 1}
    rows["discord"] = runtime.PlatformStatus("fatal", error="bad token")
    _write_status(scope, "running", age_s=2, platforms=rows)
    body = rest.client.get("/api/status").json()
    assert body["components"]["platforms"]["status"] == "degraded"
    assert body["overall"] == "degraded" and "bad token" not in str(body)


def test_route_list_matches_the_app(rest: RestApp) -> None:
    assert route_table(rest.app, ("/api/status",)) == set(status.ROUTE_LIST)


# -- daemon state ----------------------------------------------------------------------------------


def _write_status(
    scope: Scope,
    state: str,
    *,
    age_s: float,
    identity: runtime.ProcessIdentity | None = None,
    platforms: dict[str, runtime.PlatformStatus] | None = None,
) -> None:
    stamped = scope.clock.now() - timedelta(seconds=age_s)
    record = runtime.RuntimeStatus(
        state,  # pyright: ignore[reportArgumentType]
        stamped.isoformat(),
        identity or runtime.ProcessIdentity.current(),
        platforms=platforms
        if platforms is not None
        else {"telegram": runtime.PlatformStatus("connected", error="secret detail")},
    )
    runtime.write_status(scope.home, record)


@pytest.mark.parametrize(
    ("state", "age_s", "expected"),
    [
        ("running", 0, "running"),
        ("running", 150, "running"),
        ("running", 151, "unknown"),  # heartbeat too old
        ("running", -1, "unknown"),  # dated in the future
        ("draining", 5, "draining"),
        ("stopped", 9999, "stopped"),
        ("startup_failed", 3, "startup_failed"),
    ],
)
def test_daemon_state_table(scope: Scope, state: str, age_s: float, expected: str) -> None:
    _write_status(scope, state, age_s=age_s)
    assert daemon_view(scope.home, scope.clock.now()).state == expected


def test_daemon_with_a_dead_process_reads_stopped(scope: Scope) -> None:
    gone = runtime.ProcessIdentity(2**22 + 12345, 1, runtime.pid_namespace(), "")
    _write_status(scope, "running", age_s=1, identity=gone)
    assert daemon_view(scope.home, scope.clock.now()).state == "stopped"


@pytest.mark.parametrize("content", ["{not json", '["a list"]', '{"state": "flying", "pid": 3}'])
def test_unreadable_daemon_record_reads_unknown(scope: Scope, content: str) -> None:
    runtime.status_path(scope.home).write_text(content, encoding="utf-8")
    assert daemon_view(scope.home, scope.clock.now()).state == "unknown"


def test_missing_daemon_record_reads_stopped(scope: Scope) -> None:
    path: Path = runtime.status_path(scope.home)
    assert not path.exists()
    view: Any = daemon_view(scope.home, scope.clock.now())
    assert view.state == "stopped" and view.heartbeat_age_s is None
