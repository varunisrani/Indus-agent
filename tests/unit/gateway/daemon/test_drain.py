"""P15-25: the external drain marker's staleness rules (R06.20) and the lifecycle requests."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from indus.gateway.daemon.drain import (
    clear_drain_marker,
    drain_verdict,
    machine_epoch,
    write_drain_marker,
)
from indus.gateway.daemon.lifecycle import (
    MAINTENANCE_NOTICE,
    RESTARTING_NOTICE,
    Lifecycle,
)
from indus.state.gateway_runtime import EXIT_OK, EXIT_RESTART, drain_marker_path
from tests.fakes.clock import checkpoint

NOW = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("body", "active", "why"),
    [
        (None, False, "absent"),
        ({"requested_at": NOW.isoformat(), "epoch": "e1"}, True, "requested"),
        ("{not json", True, "malformed"),
        ("[1, 2]", True, "malformed"),
        ({}, True, "malformed"),
        ({"requested_at": NOW.isoformat(), "epoch": "e0"}, False, "other_epoch"),
        ({"requested_at": (NOW - timedelta(seconds=3601)).isoformat()}, False, "expired"),
        ({"requested_at": (NOW - timedelta(seconds=3599)).isoformat()}, True, "requested"),
        ({"requested_at": (NOW + timedelta(hours=5)).isoformat()}, True, "requested"),
        ({"requested_at": "sometime", "epoch": "e1"}, True, "requested"),
        ({"requested_at": NOW.isoformat(), "epoch": ""}, True, "requested"),
    ],
)
def test_marker_verdicts(tmp_path: Path, body: object, active: bool, why: str) -> None:
    path = drain_marker_path(tmp_path)
    if isinstance(body, str):
        path.write_text(body, encoding="utf-8")
    elif isinstance(body, dict):
        path.write_text(json.dumps(body), encoding="utf-8")
    verdict = drain_verdict(tmp_path, now=NOW, epoch="e1")
    assert (verdict.active, verdict.why) == (active, why)


def test_unknown_machine_epoch_turns_the_epoch_check_off(tmp_path: Path) -> None:
    write_drain_marker(tmp_path, now=NOW, epoch="old-boot")
    assert drain_verdict(tmp_path, now=NOW, epoch="").active
    assert not drain_verdict(tmp_path, now=NOW, epoch="new-boot").active


def test_write_and_clear(tmp_path: Path) -> None:
    body = write_drain_marker(tmp_path, principal="dashboard", now=NOW)
    assert body["epoch"] == machine_epoch()
    assert drain_marker_path(tmp_path).stat().st_mode & 0o777 == 0o600
    verdict = drain_verdict(tmp_path, now=NOW + timedelta(seconds=10))
    assert verdict.active and verdict.principal == "dashboard"
    assert clear_drain_marker(tmp_path) and not clear_drain_marker(tmp_path)
    assert not drain_verdict(tmp_path, now=NOW).active


def test_stop_beats_restart_and_a_second_stop_hurries() -> None:
    async def main() -> None:
        life = Lifecycle()
        assert life.notice() is None and not life.stopping
        life.set_draining()
        assert life.notice() == MAINTENANCE_NOTICE
        life.clear_draining()
        life.request_restart("signal:SIGUSR1")
        assert life.mode == "restart" and life.exit_code == EXIT_RESTART
        assert life.notice() == RESTARTING_NOTICE
        life.set_draining()  # ignored once a stop or restart is under way
        assert life.drain_reason == "restarting"
        assert not life.hurried()
        life.request_stop("signal:SIGTERM")
        assert life.mode == "stop" and life.exit_code == EXIT_OK and life.hurried()
        life.request_restart("again")
        assert life.mode == "stop"
        await asyncio.wait_for(life.wait_requested(), 1)

    asyncio.run(main())


def test_a_second_stop_cuts_the_drain_wait_short() -> None:
    async def main() -> None:
        life = Lifecycle()
        life.request_stop("first")
        assert not await life.wait_hurry(0)
        waiter = asyncio.ensure_future(life.wait_hurry(30))
        await checkpoint()
        life.request_stop("second")
        assert await asyncio.wait_for(waiter, 2)

    asyncio.run(main())
