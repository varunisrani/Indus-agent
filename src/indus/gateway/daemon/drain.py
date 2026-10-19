"""The external drain marker (P15-25, R06.20).

A dashboard (or an operator) asks a running gateway to stop taking new turns by writing
``<home>/gateway_drain.json``; removing it lets turns in again. While a marker is active the daemon
reports ``draining`` and answers new turns with a maintenance notice.

A marker can outlive what it was written for (a durable volume keeps it across the machine restart
the drain was for), so it is ignored as stale when either holds:
- its ``epoch`` differs from this machine's (both known): the boot it was written in is over;
- its ``requested_at`` is older than ``DRAIN_MAX_AGE`` (3,600 s); writing it again refreshes it.
Anything else counts: a file that is not valid JSON, or has no fields, is active (when unsure,
quiesce). A future ``requested_at`` (clock skew) is active.

The epoch is the Linux boot id plus PID 1's start time: stable for one boot of one container or
VM, different after either restarts. Where neither can be read it is empty, which turns the epoch
check off (never on by guesswork).
"""

from __future__ import annotations

import contextlib
import functools
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from indus.foundation import fsio
from indus.state.gateway_runtime import drain_marker_path

__all__ = [
    "DRAIN_MAX_AGE",
    "DrainVerdict",
    "clear_drain_marker",
    "drain_verdict",
    "machine_epoch",
    "write_drain_marker",
]

DRAIN_MAX_AGE = 3600.0


@functools.lru_cache(maxsize=1)
def machine_epoch() -> str:
    """``<boot id>:<pid 1 start>`` for this machine instance ("" where neither is readable)."""
    boot_id = ""
    first = ""
    with contextlib.suppress(OSError):
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    with contextlib.suppress(OSError, IndexError):
        raw = Path("/proc/1/stat").read_text(encoding="ascii", errors="replace")
        first = raw.rsplit(")", 1)[1].split()[19]
    return f"{boot_id}:{first}" if boot_id or first else ""


def write_drain_marker(
    home: Path,
    *,
    principal: str = "operator",
    now: datetime | None = None,
    epoch: str | None = None,
) -> dict[str, Any]:
    """Write (or refresh) the drain request; returns what was written."""
    body: dict[str, Any] = {
        "action": "drain",
        "principal": principal,
        "requested_at": (now or datetime.now(UTC)).astimezone(UTC).isoformat(),
        "epoch": machine_epoch() if epoch is None else epoch,
    }
    fsio.atomic_write_json(drain_marker_path(home), body, mode=0o600)
    return body


def clear_drain_marker(home: Path) -> bool:
    try:
        drain_marker_path(home).unlink()
    except FileNotFoundError:
        return False
    return True


@dataclass(frozen=True)
class DrainVerdict:
    active: bool
    why: Literal["absent", "requested", "malformed", "other_epoch", "expired"]
    principal: str = ""


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def drain_verdict(
    home: Path, *, now: datetime, epoch: str | None = None, max_age: float = DRAIN_MAX_AGE
) -> DrainVerdict:
    """Is a drain requested right now? (see the module notes)."""
    path = drain_marker_path(home)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return DrainVerdict(False, "absent")
    except OSError:
        return DrainVerdict(True, "malformed")
    try:
        data = json.loads(raw)
    except ValueError:
        return DrainVerdict(True, "malformed")
    if not isinstance(data, dict):
        return DrainVerdict(True, "malformed")
    body = {str(k): v for k, v in cast(dict[object, Any], data).items()}
    principal = str(body.get("principal") or "")
    ours = machine_epoch() if epoch is None else epoch
    theirs = body.get("epoch")
    if ours and isinstance(theirs, str) and theirs and theirs != ours:
        return DrainVerdict(False, "other_epoch", principal)
    requested = _parse(body.get("requested_at"))
    if requested is not None and (now - requested).total_seconds() > max_age:
        return DrainVerdict(False, "expired", principal)
    if not body:
        return DrainVerdict(True, "malformed")
    return DrainVerdict(True, "requested", principal)
