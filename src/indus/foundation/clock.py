"""The ``Clock`` protocol, the real system clock and the configured time zone.

Code that reads the time or waits takes a ``Clock``; tests pass ``tests.fakes.clock.FakeClock``.
This is the only product module allowed to call ``time.sleep`` / ``asyncio.sleep`` (ruff TID251
exemption).

Zone resolution for a scope: ``INDUS_TIMEZONE`` from the scope's env, but only when the scope is
the process's own profile (a routed profile must not inherit the launching profile's value), then
the config ``timezone`` key, else the system local zone. An invalid name logs one warning and
falls back to local time.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:
    from indus.foundation.scope import Scope

log = logging.getLogger(__name__)

TIMEZONE_ENV = "INDUS_TIMEZONE"
CONFIG_KEY = "timezone"


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """Current wall time, timezone-aware."""
        ...

    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """Real time. ``zone`` None means the system local zone, looked up on every call."""

    def __init__(self, zone: ZoneInfo | None = None) -> None:
        self.zone = zone

    def now(self) -> datetime:
        if self.zone is None:
            return datetime.now().astimezone()
        return datetime.now(self.zone)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("sleep seconds must be >= 0")
        await asyncio.sleep(seconds)

    def sleep_sync(self, seconds: float) -> None:
        """Blocking wait for worker threads (file locks); not part of the ``Clock`` protocol."""
        if seconds < 0:
            raise ValueError("sleep seconds must be >= 0")
        time.sleep(seconds)

    def __repr__(self) -> str:
        return f"SystemClock({self.zone.key if self.zone else 'local'})"


# -- zone resolution ----------------------------------------------------------------------------

_cache_lock = threading.Lock()
_zone_cache: dict[tuple[str, str, str], ZoneInfo | None] = {}


def _zone_name(scope: Scope) -> tuple[str, str]:
    if not scope.routed:
        from_env = scope.env.get(TIMEZONE_ENV, "").strip()
        if from_env:
            return "env", from_env
    if scope.config is not None:
        value = scope.config.get(CONFIG_KEY)
        if isinstance(value, str) and value.strip():
            return "config", value.strip()
    return "local", ""


def resolve_zone(scope: Scope) -> ZoneInfo | None:
    """The scope's configured zone; None means the system local zone."""
    source, name = _zone_name(scope)
    if not name:
        return None
    key = (str(scope.home), source, name)
    with _cache_lock:
        if key in _zone_cache:
            return _zone_cache[key]
    try:
        zone: ZoneInfo | None = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        log.warning(
            "time zone %r from %s is not valid (%s); using the system local zone",
            name,
            TIMEZONE_ENV if source == "env" else f"config '{CONFIG_KEY}'",
            exc,
        )
        zone = None
    with _cache_lock:
        return _zone_cache.setdefault(key, zone)


def reset_zone_cache() -> None:
    with _cache_lock:
        _zone_cache.clear()


def system_clock_for(scope: Scope) -> SystemClock:
    return SystemClock(resolve_zone(scope))


# -- formatting ---------------------------------------------------------------------------------

_SURROGATE = re.compile(r"[\ud800-\udfff]")


def _clean(text: str) -> str:
    return _SURROGATE.sub("�", text)


def safe_strftime(value: datetime, fmt: str) -> str:
    """``strftime`` that never raises on, or returns, lone surrogates from a locale zone name."""
    try:
        return _clean(value.strftime(fmt))
    except UnicodeEncodeError:
        zone = _clean(value.tzname() or "").replace("%", "%%")
        out: list[str] = []
        i = 0
        while i < len(fmt):
            if fmt[i] == "%" and i + 1 < len(fmt):
                directive = fmt[i + 1]
                out.append(zone if directive == "Z" else fmt[i : i + 2])
                i += 2
                continue
            out.append(fmt[i])
            i += 1
        return _clean(value.strftime("".join(out)))
