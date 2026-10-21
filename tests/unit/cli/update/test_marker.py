"""The update marker ``<root>/.indus-update-in-progress`` (P23-18/19): the body the desktop app
reads (P20-37 contract), exclusive creation, owner-only removal and the live/stale judgement."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from indus.cli.update import marker

NOW = 1_800_000_000.0


def _body(pid: int, start: float | None, created: float = NOW) -> dict[str, Any]:
    return {
        "format": 1,
        "owner": "indus-update",
        "pid": pid,
        "process_start": start,
        "created_at": created,
    }


def test_claim_writes_the_contract_body(tmp_path: Path) -> None:
    root = tmp_path / "root"
    path = marker.claim(root, pid=4242, now=lambda: NOW, start_of=lambda _pid: 123.5)
    assert path == root / ".indus-update-in-progress"
    assert json.loads(path.read_text(encoding="utf-8")) == _body(4242, 123.5)
    assert sorted(p.name for p in root.iterdir()) == [".indus-update-in-progress"]


def test_a_live_foreign_marker_refuses_and_stays(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    path = marker.marker_path(root)
    path.write_text(json.dumps(_body(77, 1.0)), encoding="utf-8")
    with pytest.raises(marker.MarkerBusy, match="pid 77"):
        marker.claim(root, pid=4242, live=lambda _body: True)
    assert json.loads(path.read_text(encoding="utf-8"))["pid"] == 77
    assert sorted(p.name for p in root.iterdir()) == [".indus-update-in-progress"]


@pytest.mark.parametrize("existing", ["stale", "ours", "garbage"])
def test_stale_own_or_garbage_markers_are_replaced(tmp_path: Path, existing: str) -> None:
    path = marker.marker_path(tmp_path)
    if existing == "garbage":
        path.write_text("{not json", encoding="utf-8")
    else:
        path.write_text(json.dumps(_body(4242 if existing == "ours" else 77, 1.0)), "utf-8")
    marker.claim(
        tmp_path, pid=4242, now=lambda: NOW, start_of=lambda _p: 9.0, live=lambda _b: False
    )
    assert json.loads(path.read_text(encoding="utf-8")) == _body(4242, 9.0)


def test_only_the_owner_releases(tmp_path: Path) -> None:
    marker.claim(tmp_path, pid=4242, start_of=lambda _p: None)
    assert marker.release(tmp_path, pid=99) is False
    assert marker.marker_path(tmp_path).exists()
    assert marker.release(tmp_path, pid=4242) is True
    assert not marker.marker_path(tmp_path).exists()
    assert marker.release(tmp_path, pid=4242) is False


@pytest.mark.parametrize(
    ("body", "alive", "started", "live"),
    [
        (_body(5, 100.0), True, 101.5, True),  # same process (within 2 s)
        (_body(5, 100.0), True, 140.0, False),  # the pid now belongs to another process
        (_body(5, 100.0), False, 100.0, False),  # gone
        (_body(5, None, NOW - 60), True, None, True),  # no start time: young enough
        (_body(5, None, NOW - 21 * 60), True, None, False),  # no start time: too old
        (_body(5, 100.0, NOW - 60), True, None, True),  # start unreadable now: by age
    ],
)
def test_live_or_stale_like_the_desktop(
    body: dict[str, Any], alive: bool, started: float | None, live: bool
) -> None:
    judged = marker.is_live(
        body, alive=lambda _pid: alive, start_of=lambda _pid: started, now=lambda: NOW
    )
    assert judged is live


def test_read_marker_rejects_what_is_not_one(tmp_path: Path) -> None:
    path = tmp_path / "m"
    for text in ("[]", "{}", '{"pid": 0, "created_at": 1}', '{"pid": 3, "created_at": "x"}'):
        path.write_text(text, encoding="utf-8")
        assert marker.read_marker(path) is None
    path.write_text('{"pid": 3, "created_at": 1}', encoding="utf-8")
    assert marker.read_marker(path) == {"pid": 3, "created_at": 1}


@pytest.mark.platforms("posix")
def test_process_start_is_unix_seconds_of_that_process() -> None:
    before = time.time()
    child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                             stdin=subprocess.PIPE)  # fmt: skip
    try:
        started = marker.process_start(child.pid)
        assert started is not None
        assert before - 2.0 <= started <= time.time() + 2.0
        assert marker.is_live(_body(child.pid, started, time.time()))
    finally:
        assert child.stdin is not None
        child.stdin.close()
        child.wait(timeout=10)
    assert marker.process_start(os.getpid()) is not None
