"""Wall-clock helpers for schedules: the effective zone, and wall times that a DST change skips or
repeats.

``resolve_zone(scope)`` (P01) gives the configured zone or None for "the system zone". Schedules
need a zone that knows its DST rules, so ``effective_zone(None)`` loads the system zone from
``/etc/localtime``; only when that file is missing, or disagrees with what the C library reports
right now (a ``TZ`` override), does it fall back to the current fixed offset.

``instants_of(wall, zone)`` lists the real instants a naive wall time names: none in a
spring-forward gap, two in a fall-back repeat (earlier first), one otherwise.
``first_instant_at_or_after(wall, zone)`` picks the earliest; inside a gap it returns the instant
the gap ends (the first valid wall time after it).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import AfterValidator

__all__ = [
    "LOCALTIME",
    "UtcDatetime",
    "effective_zone",
    "first_instant_at_or_after",
    "instants_of",
    "to_utc",
]

log = logging.getLogger(__name__)

LOCALTIME = Path("/etc/localtime")


def to_utc(value: datetime) -> datetime:
    """Aware UTC; a naive value is taken as UTC (hand-edited files)."""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


UtcDatetime = Annotated[datetime, AfterValidator(to_utc)]


def _system_zone(localtime: Path) -> tzinfo:
    now = datetime.now().astimezone()
    fixed = now.tzinfo
    assert fixed is not None
    try:
        with localtime.open("rb") as handle:
            zone: tzinfo = ZoneInfo.from_file(handle, key="localtime")
    except (OSError, ValueError):
        return fixed
    if now.astimezone(zone).utcoffset() != now.utcoffset():
        return fixed  # TZ points somewhere else; the C library's answer wins
    return zone


def effective_zone(tz: tzinfo | None, *, localtime: Path = LOCALTIME) -> tzinfo:
    """``tz`` itself, or the DST-aware system zone when ``tz`` is None."""
    return tz if tz is not None else _system_zone(localtime)


def _round_trips(candidate: datetime, wall: datetime, zone: tzinfo) -> bool:
    return candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == wall


def instants_of(wall: datetime, zone: tzinfo) -> list[datetime]:
    """The UTC instants at which ``zone``'s clock shows ``wall`` (naive), earliest first."""
    found: list[datetime] = []
    for fold in (0, 1):
        candidate = wall.replace(tzinfo=zone, fold=fold)
        if _round_trips(candidate, wall, zone):
            instant = candidate.astimezone(UTC)
            if instant not in found:
                found.append(instant)
    return sorted(found)


def _gap_end(wall: datetime, zone: tzinfo) -> datetime:
    """The instant a spring-forward gap containing ``wall`` ends (binary search, 1 s)."""
    # fold=1 reads the wall with the offset after the change (earlier instant), fold=0 with the
    # offset before it (later instant); the change happens between them.
    low = wall.replace(tzinfo=zone, fold=1).astimezone(UTC)
    high = wall.replace(tzinfo=zone, fold=0).astimezone(UTC)
    if high < low:
        low, high = high, low
    after = high.astimezone(zone).utcoffset()
    while high - low > timedelta(seconds=1):
        middle = low + (high - low) / 2
        if middle.astimezone(zone).utcoffset() == after:
            high = middle
        else:
            low = middle
    return high.replace(microsecond=0) if high.microsecond else high


def first_instant_at_or_after(wall: datetime, zone: tzinfo) -> datetime:
    """The earliest instant showing ``wall``; for a skipped wall time, the end of the gap."""
    found = instants_of(wall, zone)
    if found:
        return found[0]
    return _gap_end(wall, zone)
