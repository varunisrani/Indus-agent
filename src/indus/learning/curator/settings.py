"""The ``curator`` settings of one profile, read once per run.

Day and hour counts below 1 would make the automatic pass archive or re-run everything on its next
tick, unconfirmed; such values fall back to the built-in default with one warning per key and
value. The built-in values follow the reference's code, not its docs (D-16.24, R07.30).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from indus.foundation.scope import Scope
from indus.learning.skills.resolver import skills_setting

__all__ = [
    "ARCHIVE_AFTER_DAYS",
    "BACKUP_KEEP",
    "CLAIM_STALE_S",
    "CONSOLIDATE_TIMEOUT_S",
    "INTERVAL_HOURS",
    "ITERATION_CAP",
    "MIN_IDLE_HOURS",
    "STALE_AFTER_DAYS",
    "CuratorSettings",
]

log = logging.getLogger(__name__)

INTERVAL_HOURS = 168
MIN_IDLE_HOURS = 2.0
STALE_AFTER_DAYS = 14
ARCHIVE_AFTER_DAYS = 30
BACKUP_KEEP = 2
CLAIM_STALE_S = 3600.0
CONSOLIDATE_TIMEOUT_S = 600.0
ITERATION_CAP = 9999

_warned: set[tuple[str, object]] = set()
_warned_lock = threading.Lock()


def _bool(value: object, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        try:
            return float(str(value)) if value is not None else None
        except ValueError:
            return None
    return float(value)


def _at_least_one(key: str, value: object, default: float) -> float:
    number = _number(value)
    if number is not None and number >= 1:
        return number
    with _warned_lock:
        first = (key, value) not in _warned
        _warned.add((key, value))
    if first:
        log.warning("curator.%s must be 1 or more (got %r); using %s", key, value, default)
    return default


@dataclass(frozen=True)
class CuratorSettings:
    enabled: bool = True
    interval_hours: float = INTERVAL_HOURS
    min_idle_hours: float = MIN_IDLE_HOURS
    stale_after_days: float = STALE_AFTER_DAYS
    archive_after_days: float = ARCHIVE_AFTER_DAYS
    consolidate: bool = False
    prune_builtins: bool = False
    archive_ttl_days: float = 0
    backup_enabled: bool = True
    backup_keep: int = BACKUP_KEEP

    @classmethod
    def from_scope(cls, scope: Scope) -> CuratorSettings:
        def get(key: str) -> object:
            return skills_setting(scope, f"curator.{key}")

        idle = _number(get("min_idle_hours"))
        ttl = _number(get("archive_ttl_days"))
        keep = _number(get("backup.keep"))
        return cls(
            enabled=_bool(get("enabled"), True),
            interval_hours=_at_least_one("interval_hours", get("interval_hours"), INTERVAL_HOURS),
            min_idle_hours=idle if idle is not None and idle >= 0 else MIN_IDLE_HOURS,
            stale_after_days=_at_least_one(
                "stale_after_days", get("stale_after_days"), STALE_AFTER_DAYS
            ),
            archive_after_days=_at_least_one(
                "archive_after_days", get("archive_after_days"), ARCHIVE_AFTER_DAYS
            ),
            consolidate=_bool(get("consolidate"), False),
            prune_builtins=_bool(get("prune_builtins"), False),
            archive_ttl_days=ttl if ttl is not None and ttl > 0 else 0,
            backup_enabled=_bool(get("backup.enabled"), True),
            backup_keep=max(1, int(keep)) if keep is not None else BACKUP_KEEP,
        )
