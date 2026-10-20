"""``GET /api/status``: the public status probe of the dashboard (P18-13).

The path is on the shared public list (``auth.public_paths``), so whoever can reach the port reads
it. It therefore carries only counts, enums and coarse numbers: never a path, pid, host name,
session id, title or error text.

What it reports:

- ``version`` and ``daemon {state, heartbeat_age_s}``: the daemon's own word from its runtime record
  (``state.gateway_runtime``); ``stopped`` when there is no record or the process it names is gone;
  ``unknown`` when the heartbeat is older than ``HEARTBEAT_LIMIT_S``, dated in the future, or the
  record cannot be read;
- ``platforms``: ``[{name, state}]`` as the daemon reports them (only while it runs);
- ``active_sessions`` / ``recent_sessions``: of the ``RECENT_WINDOW`` most recently active sessions,
  how many are open and were active in the last ``ACTIVE_WINDOW_S``, and how many the window held
  (store opened read-only; a missing store is never created);
- ``auth_required`` / ``auth_providers`` (``auth.gate.describe_mode``);
- ``components {store, daemon, platforms}`` and ``overall``;
- ``memory`` / ``disk``: pressure (``ok`` / ``elevated`` / ``critical``), one coarse number each.

Every collector is fail-safe: an error makes its block ``unknown`` and the rest is still answered.
Tests replace collectors through ``DashboardState.extras`` (``status.memory``, ``status.disk``,
``status.now``).
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

import indus
from indus.foundation.errors import StoreUnavailable
from indus.foundation.scope import Scope
from indus.state import browse
from indus.state import gateway_runtime as runtime
from indus.state.errors import Bucket, classify, is_lock_error
from indus.web.auth.gate import describe_mode
from indus.web.scope import read_scope
from indus.web.state import dashboard_state

__all__ = [
    "ACTIVE_WINDOW_S",
    "HEARTBEAT_LIMIT_S",
    "RECENT_WINDOW",
    "ROUTE_LIST",
    "Pressure",
    "daemon_view",
    "disk_pressure",
    "memory_pressure",
    "router",
]

log = logging.getLogger(__name__)

router = APIRouter()

ROUTE_LIST: Final[tuple[tuple[str, str, bool], ...]] = (("GET", "/api/status", True),)
"""``(method, path, scoped)`` of every route here (the SPA's generated route list)."""

ReadScope = Annotated[Scope, Depends(read_scope)]

type Pressure = Literal["ok", "elevated", "critical", "unknown"]

MIB: Final = 1024 * 1024
GIB: Final = 1024 * MIB
HEARTBEAT_LIMIT_S: Final = 150.0
ACTIVE_WINDOW_S: Final = 300.0
RECENT_WINDOW: Final = 20

MEMORY_CRITICAL: Final = (64 * MIB, 5.0)
"""(bytes available, percent available) under either of which memory is critical."""
MEMORY_ELEVATED: Final = (128 * MIB, 15.0)
DISK_CRITICAL: Final = (256 * MIB, 95.0, 1 * GIB)
"""(free bytes floor, used percent, headroom): critical below the floor, or at/over the percent
with less than the headroom left."""
DISK_ELEVATED: Final = (512 * MIB, 85.0, 4 * GIB)

_LIVE_DAEMON: Final = frozenset({"starting", "running", "draining", "stopping", "degraded"})
_DOWN_DAEMON: Final = frozenset({"stopped", "startup_failed"})
_HEALTHY_PLATFORM: Final = "connected"
_PAUSED_PLATFORM: Final = "paused"
"""Paused on purpose: shown, but never a degraded verdict."""
_SERVING_DAEMON: Final = frozenset({"running", "draining"})


# -- pressure --------------------------------------------------------------------------------------


def memory_pressure(available: int, total: int) -> Pressure:
    """Pressure for ``available`` of ``total`` bytes of memory."""
    if total <= 0 or available < 0:
        return "unknown"
    percent = available * 100.0 / total
    if available < MEMORY_CRITICAL[0] or percent < MEMORY_CRITICAL[1]:
        return "critical"
    if available < MEMORY_ELEVATED[0] or percent < MEMORY_ELEVATED[1]:
        return "elevated"
    return "ok"


def disk_pressure(free: int, total: int) -> Pressure:
    """Pressure for ``free`` of ``total`` bytes on the home's file system."""
    if total <= 0 or free < 0:
        return "unknown"
    used = (total - free) * 100.0 / total

    def under(rule: tuple[int, float, int]) -> bool:
        floor, percent, headroom = rule
        return free < floor or (used >= percent and free < headroom)

    if under(DISK_CRITICAL):
        return "critical"
    if under(DISK_ELEVATED):
        return "elevated"
    return "ok"


@dataclass(frozen=True)
class Amount:
    """``available``/``free`` and ``total`` bytes from a collector."""

    left: int
    total: int


def _meminfo() -> Amount:
    fields: dict[str, int] = {}
    with open("/proc/meminfo", encoding="ascii") as handle:
        for line in handle:
            name, _, rest = line.partition(":")
            parts = rest.split()
            if parts and parts[0].isdigit():
                fields[name.strip()] = int(parts[0]) * 1024
    return Amount(fields["MemAvailable"], fields["MemTotal"])


def read_memory() -> Amount:
    """Available and total memory: ``/proc/meminfo``, else psutil when installed."""
    try:
        return _meminfo()
    except (OSError, KeyError, ValueError):
        pass
    import psutil  # pyright: ignore[reportMissingImports, reportMissingModuleSource]

    found: Any = psutil.virtual_memory()  # pyright: ignore[reportUnknownMemberType]
    return Amount(int(found.available), int(found.total))


def disk_reader(home: Path) -> Callable[[], Amount]:
    def read() -> Amount:
        usage = shutil.disk_usage(home)
        return Amount(int(usage.free), int(usage.total))

    return read


def _memory_block(read: Callable[[], Amount]) -> dict[str, Any]:
    try:
        amount = read()
        pressure = memory_pressure(amount.left, amount.total)
        if pressure == "unknown":
            return {"pressure": "unknown"}
        return {"pressure": pressure, "available_percent": int(amount.left * 100 / amount.total)}
    except Exception as exc:
        log.debug("memory pressure unavailable: %s", type(exc).__name__)
        return {"pressure": "unknown"}


def _disk_block(read: Callable[[], Amount]) -> dict[str, Any]:
    try:
        amount = read()
        pressure = disk_pressure(amount.left, amount.total)
        if pressure == "unknown":
            return {"pressure": "unknown"}
        used = (amount.total - amount.left) * 100 / amount.total
        return {
            "pressure": pressure,
            "used_percent": int(used),
            "free_gb": int(amount.left // GIB),
        }
    except Exception as exc:
        log.debug("disk pressure unavailable: %s", type(exc).__name__)
        return {"pressure": "unknown"}


# -- daemon ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DaemonView:
    state: str
    heartbeat_age_s: int | None = None
    platforms: tuple[tuple[str, str], ...] = ()


def _when(value: object) -> datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return datetime.fromtimestamp(float(value)).astimezone()
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.astimezone()


def _platform_rows(data: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    raw: object = data.get("platforms")
    if not isinstance(raw, dict):
        return ()
    rows: list[tuple[str, str]] = []
    for name, row in raw.items():  # pyright: ignore[reportUnknownVariableType]
        parsed = runtime.PlatformStatus.from_json(row)
        if parsed is not None and isinstance(name, str):
            rows.append((name, parsed.state))
    return tuple(sorted(rows))


def daemon_view(
    home: Path,
    now: datetime,
    *,
    alive: Callable[[runtime.ProcessIdentity], bool] | None = None,
) -> DaemonView:
    """The daemon's state for the public probe (see the module notes)."""
    path = runtime.status_path(home)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return DaemonView("stopped")
    except OSError:
        return DaemonView("unknown")
    try:
        data: object = json.loads(text)
    except ValueError:
        return DaemonView("unknown")
    if not isinstance(data, dict):
        return DaemonView("unknown")
    record: dict[str, Any] = {str(k): v for k, v in data.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    status = runtime.RuntimeStatus.from_json(record)
    if status is None:
        return DaemonView("unknown")
    if status.state in _DOWN_DAEMON:
        return DaemonView(status.state)
    stamped = _when(record.get("updated_at"))
    if stamped is None:
        return DaemonView("unknown")
    age = (now - stamped).total_seconds()
    if age < 0 or age > HEARTBEAT_LIMIT_S:
        return DaemonView("unknown")
    check = alive if alive is not None else _process_alive
    if not check(status.process):
        return DaemonView("stopped")
    state = status.state if status.state in _LIVE_DAEMON else "unknown"
    return DaemonView(state, int(age), _platform_rows(record))


def _process_alive(identity: runtime.ProcessIdentity) -> bool:
    return identity.is_alive()


def _daemon_component(view: DaemonView) -> str:
    if view.state in _SERVING_DAEMON:
        return "ok"
    if view.state == "unknown":
        return "unknown"
    if view.state in _DOWN_DAEMON:
        return "down"
    return "degraded"


# -- sessions --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionCounts:
    store: str
    active: int = 0
    recent: int = 0


_RECENT_SQL: Final = (
    "SELECT ended_at, COALESCE(last_activity_at, started_at) AS seen FROM sessions "
    "WHERE hidden = 0 ORDER BY seen DESC LIMIT ?"
)


def _store_problem(exc: BaseException) -> str:
    if is_lock_error(exc):
        return "busy"
    return "corrupt" if classify(exc) is Bucket.CORRUPT else "unavailable"


def session_counts(scope: Scope, now: float) -> SessionCounts:
    """Active and recent counts from the read-only store (never created)."""
    try:
        store = browse.open_readonly(scope)
    except StoreUnavailable as exc:
        return SessionCounts("missing" if exc.kind == "missing" else "unavailable")
    except (sqlite3.Error, OSError) as exc:
        return SessionCounts(_store_problem(exc))
    try:
        with store.read() as conn:
            rows = conn.execute(_RECENT_SQL, (RECENT_WINDOW,)).fetchall()
    except sqlite3.Error as exc:
        return SessionCounts(_store_problem(exc))
    finally:
        store.close()
    active = sum(
        1
        for row in rows
        if row["ended_at"] is None
        and row["seen"] is not None
        and now - row["seen"] < ACTIVE_WINDOW_S
    )
    return SessionCounts("ok", active, len(rows))


# -- the route -------------------------------------------------------------------------------------


def _now(request: Request, scope: Scope) -> datetime:
    override: object = dashboard_state(request.app).extras.get("status.now")
    if callable(override):
        found: object = override()
        if isinstance(found, datetime):
            return found
    return scope.clock.now()


def _collector(request: Request, key: str, default: Callable[[], Amount]) -> Callable[[], Amount]:
    override: object = dashboard_state(request.app).extras.get(key)
    if callable(override):
        return override  # pyright: ignore[reportReturnType]
    return default


def build_status(request: Request, scope: Scope) -> dict[str, Any]:
    state = dashboard_state(request.app)
    now = _now(request, scope)
    try:
        daemon = daemon_view(scope.home, now)
    except Exception as exc:
        log.debug("daemon state unavailable: %s", type(exc).__name__)
        daemon = DaemonView("unknown")
    try:
        sessions = session_counts(scope, now.timestamp())
    except Exception as exc:
        log.debug("session counts unavailable: %s", type(exc).__name__)
        sessions = SessionCounts("unknown")
    counted = [row for _name, row in daemon.platforms if row != _PAUSED_PLATFORM]
    connected = sum(1 for row in counted if row == _HEALTHY_PLATFORM)
    platform_status = "ok" if connected == len(counted) else "degraded"
    components: dict[str, dict[str, Any]] = {
        "store": {"status": "ok" if sessions.store in ("ok", "missing") else sessions.store},
        "daemon": {"status": _daemon_component(daemon), "state": daemon.state},
        "platforms": {
            "status": platform_status,
            "configured": len(counted),
            "connected": connected,
        },
    }
    overall = "ok" if all(c["status"] == "ok" for c in components.values()) else "degraded"
    memory = _memory_block(_collector(request, "status.memory", read_memory))
    disk = _disk_block(_collector(request, "status.disk", disk_reader(scope.home)))
    return {
        "version": indus.__version__,
        "daemon": {"state": daemon.state, "heartbeat_age_s": daemon.heartbeat_age_s},
        "platforms": [{"name": name, "state": row} for name, row in daemon.platforms],
        "active_sessions": sessions.active,
        "recent_sessions": sessions.recent,
        **describe_mode(state.auth),
        "components": components,
        "overall": overall,
        "memory": memory,
        "disk": disk,
    }


@router.get("/api/status")
def get_status(request: Request, scope: ReadScope) -> JSONResponse:
    try:
        body = build_status(request, scope)
    except Exception as exc:  # the probe answers even when something above is broken
        log.warning("the status probe failed: %s", type(exc).__name__)
        body = {"version": indus.__version__, "daemon": {"state": "unknown"}, "overall": "unknown"}
    return JSONResponse(body, headers={"cache-control": "no-store"})
