"""The shared "an update owns the runtime" marker (P23-18/19 with the desktop's P20-37).

``<root>/.indus-update-in-progress`` tells the desktop app (and any other reader) that ``indus
update`` is replacing this install's code and environment right now, so nothing may start a
backend from it. The body is one JSON object, written complete before the file appears::

    {"format": 1, "owner": "indus-update", "pid": <pid>,
     "process_start": <unix seconds the pid started, or null>, "created_at": <unix seconds>}

``apply`` claims it (under the update lock) before the checkout moves; the same process — after
``exec`` of the new code's ``--finish``, which keeps the pid — removes it when the run ends. Only
its owner removes it. A reader judges a marker by pid and start time (within 2 s): a dead pid, or
a pid whose start time differs (the OS gave the number to another process), is stale. A marker
with no comparable start time counts as live for 20 minutes from ``created_at``. A stale marker is
replaced only by a new claim, which happens under the update lock.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = [
    "MARKER_FILE",
    "MARKER_FORMAT",
    "OWNER",
    "MarkerBusy",
    "claim",
    "is_live",
    "marker_path",
    "process_start",
    "read_marker",
    "release",
]

MARKER_FILE = ".indus-update-in-progress"
MARKER_FORMAT = 1
OWNER = "indus-update"
START_TOLERANCE_S = 2.0
UNVERIFIED_CEILING_S = 20 * 60.0


class MarkerBusy(Exception):
    """Another live process holds the marker."""


def marker_path(root: Path) -> Path:
    return root / MARKER_FILE


def _linux_start(pid: int) -> float | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
        boot = Path("/proc/stat").read_text(encoding="ascii", errors="replace")
        ticks = int(stat.rsplit(")", 1)[-1].split()[19])
        btime = next(
            int(line.split()[1]) for line in boot.splitlines() if line.startswith("btime ")
        )
        hertz = os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return None
    return btime + ticks / hertz if hertz > 0 else None


def _ps_start(pid: int) -> float | None:
    try:
        done = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
            env={"LC_ALL": "C", "PATH": "/bin:/usr/bin:/usr/sbin:/sbin"},
        )
        text = " ".join(done.stdout.split())
        return time.mktime(time.strptime(text, "%a %b %d %H:%M:%S %Y")) if text else None
    except (OSError, subprocess.SubprocessError, ValueError, OverflowError):
        return None


def process_start(pid: int) -> float | None:
    """Unix seconds when ``pid`` started (None when it cannot be read, e.g. on Windows)."""
    if pid <= 0 or sys.platform == "win32":
        return None
    if sys.platform.startswith("linux"):
        return _linux_start(pid)
    return _ps_start(pid)


def _alive(pid: int) -> bool:
    if pid <= 0 or sys.platform == "win32":
        return False  # never probed with os.kill there (it would end the process)
    try:
        os.kill(pid, 0)
    except OverflowError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_marker(path: Path) -> dict[str, Any] | None:
    """The marker's body when it is one (``pid`` and ``created_at`` present), else None."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    body: dict[str, Any] = data  # type: ignore[assignment]
    pid, created = body.get("pid"), body.get("created_at")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if not isinstance(created, int | float) or isinstance(created, bool):
        return None
    return body


def is_live(
    body: dict[str, Any],
    *,
    alive: Callable[[int], bool] = _alive,
    start_of: Callable[[int], float | None] = process_start,
    now: Callable[[], float] = time.time,
) -> bool:
    """The desktop's judgement of a marker: its pid runs and started when the marker says (±2 s);
    without a comparable start time, it is younger than 20 minutes."""
    pid = int(body["pid"])
    recorded = body.get("process_start")
    if isinstance(recorded, int | float) and not isinstance(recorded, bool):
        if not alive(pid):
            return False
        started = start_of(pid)
        if started is not None:
            return abs(started - float(recorded)) <= START_TOLERANCE_S
    return 0.0 <= now() - float(body["created_at"]) <= UNVERIFIED_CEILING_S


def _body(pid: int, start: float | None, created: float) -> str:
    return json.dumps(
        {
            "format": MARKER_FORMAT,
            "owner": OWNER,
            "pid": pid,
            "process_start": start,
            "created_at": created,
        },
        separators=(",", ":"),
    )


def claim(
    root: Path,
    *,
    pid: int | None = None,
    now: Callable[[], float] = time.time,
    start_of: Callable[[int], float | None] = process_start,
    live: Callable[[dict[str, Any]], bool] = is_live,
) -> Path:
    """Create the marker for ``pid`` (this process by default), complete before it appears.

    A live marker of another process raises ``MarkerBusy``; a stale one (or our own) is replaced.
    The caller holds the update lock, so no other claimant races this one."""
    owner = os.getpid() if pid is None else pid
    target = marker_path(root)
    root.mkdir(parents=True, exist_ok=True)
    temp = root / f"{MARKER_FILE}.{owner}.tmp"
    temp.write_text(_body(owner, start_of(owner), now()), encoding="utf-8")
    try:
        try:
            os.link(temp, target)
            return target
        except FileExistsError:
            pass
        except OSError:
            # No hard links here: create exclusively and write (readers allow a short empty file).
            with contextlib.suppress(FileExistsError):
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                with os.fdopen(fd, "w", encoding="utf-8") as out:
                    out.write(temp.read_text(encoding="utf-8"))
                return target
        existing = read_marker(target)
        if existing is not None and int(existing["pid"]) != owner and live(existing):
            raise MarkerBusy(
                f"another update (pid {existing['pid']}) is replacing this install right now"
            )
        os.replace(temp, target)
        return target
    finally:
        with contextlib.suppress(OSError):
            temp.unlink()


def release(root: Path, *, pid: int | None = None) -> bool:
    """Remove the marker when ``pid`` (this process by default) owns it; True when removed."""
    owner = os.getpid() if pid is None else pid
    target = marker_path(root)
    body = read_marker(target)
    if body is None or int(body["pid"]) != owner:
        return False
    try:
        target.unlink()
    except OSError:
        return False
    return True
