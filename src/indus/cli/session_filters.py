"""The filters ``indus sessions`` commands share, their click options, and the row selection.

Times: ``--older-than`` / ``--newer-than`` compare against when a session was last active (the
freshest of its activity stamp, newest message and start, each only inside the sane window
[2020-01-01, now + 1 day]); ``--before`` / ``--after`` compare against its start. A time is a
duration back from now (``30s``, ``5m``, ``5h``, ``2d``, ``1w``; a bare number is days) or an ISO
date / date-time (a date without a zone is read in the zone of the scope's clock).

Text filters are case-insensitive substrings (``--title``, ``--model``); ``--cwd`` is a path
prefix; ``--provider`` matches the billing provider exactly; ``--source`` may repeat. Machine
sources (sub-agents, tool sessions, one-shot runs, board workers) are left out unless a
``--source`` names them.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from typing import Any

import click

from indus.state.listing import INTERNAL_SOURCES

__all__ = [
    "DAY",
    "FILTER_FLAGS",
    "FilterError",
    "SessionFilter",
    "SessionRow",
    "build_filter",
    "filter_options",
    "parse_duration",
    "parse_when",
    "select_rows",
]

DAY = 86_400.0
WINDOW_START = 1_577_836_800.0  # 2020-01-01T00:00:00Z
_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": DAY, "w": 7 * DAY}
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhdw]?)$")

FILTER_FLAGS = (
    "older_than",
    "newer_than",
    "before",
    "after",
    "source",
    "title",
    "cwd",
    "min_messages",
    "max_messages",
    "model",
    "provider",
)


class FilterError(ValueError):
    """A filter value that cannot be understood; the message names the flag."""


def parse_duration(text: str) -> float:
    """Seconds in ``30s`` / ``5m`` / ``5h`` / ``2d`` / ``1w``; a bare number is days."""
    found = _DURATION.match(text.strip().lower())
    if found is None:
        raise FilterError(f"{text!r} is not a duration like 30m, 5h, 2d or 1w")
    number, unit = float(found.group(1)), found.group(2) or "d"
    return number * _UNITS[unit]


def parse_when(text: str, now: datetime) -> float:
    """An epoch time: ``now`` minus a duration, or an ISO date / date-time."""
    raw = text.strip()
    if not raw:
        raise FilterError("an empty time was given")
    try:
        return now.timestamp() - parse_duration(raw)
    except FilterError:
        pass
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        raise FilterError(
            f"{text!r} is neither a duration (30m, 5h, 2d, 1w) nor a date like 2026-07-05 "
            "or 2026-07-05 14:30"
        ) from None
    if moment.tzinfo is None:
        zone: tzinfo | None = now.tzinfo
        moment = moment.replace(tzinfo=zone)
    return moment.timestamp()


@dataclass(frozen=True)
class SessionFilter:
    older_than: float | None = None
    newer_than: float | None = None
    before: float | None = None
    after: float | None = None
    sources: tuple[str, ...] = ()
    title: str | None = None
    cwd: str | None = None
    min_messages: int | None = None
    max_messages: int | None = None
    model: str | None = None
    provider: str | None = None

    @property
    def empty(self) -> bool:
        return self == SessionFilter()

    @property
    def only_age_and_source(self) -> bool:
        """Nothing beyond ``--older-than`` and at most one ``--source``."""
        rest = SessionFilter(older_than=self.older_than, sources=self.sources)
        return self == rest and len(self.sources) <= 1


def build_filter(values: Mapping[str, Any], now: datetime) -> SessionFilter:
    """A ``SessionFilter`` from click's values (names as in ``FILTER_FLAGS``)."""

    def when(name: str) -> float | None:
        raw = values.get(name)
        if raw is None:
            return None
        try:
            return parse_when(str(raw), now)
        except FilterError as exc:
            flag = "--" + name.replace("_", "-")
            raise FilterError(f"{flag}: {exc}") from None

    def count(name: str) -> int | None:
        raw = values.get(name)
        if raw is None:
            return None
        if int(raw) < 0:
            raise FilterError(f"--{name.replace('_', '-')} must be 0 or more")
        return int(raw)

    def text(name: str) -> str | None:
        raw = values.get(name)
        return str(raw).strip() or None if raw is not None else None

    sources = tuple(s.strip() for s in values.get("source") or () if s and s.strip())
    return SessionFilter(
        older_than=when("older_than"),
        newer_than=when("newer_than"),
        before=when("before"),
        after=when("after"),
        sources=sources,
        title=text("title"),
        cwd=text("cwd"),
        min_messages=count("min_messages"),
        max_messages=count("max_messages"),
        model=text("model"),
        provider=text("provider"),
    )


def filter_options(*, older_help: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """The shared filter flags plus ``--dry-run`` and ``-y`` as one decorator."""
    options = [
        click.option("--older-than", metavar="AGE", default=None, help=older_help),
        click.option(
            "--newer-than",
            metavar="AGE",
            default=None,
            help="Only sessions active within AGE (5h, 2d, 1w) or since a date.",
        ),
        click.option(
            "--before", metavar="TIME", default=None, help="Only sessions started before TIME."
        ),
        click.option(
            "--after", metavar="TIME", default=None, help="Only sessions started at or after TIME."
        ),
        click.option(
            "--source",
            multiple=True,
            help="Only sessions from this source (cli, telegram, ...); may repeat.",
        ),
        click.option("--title", default=None, help="Only titles containing this text."),
        click.option("--cwd", default=None, help="Only sessions whose folder is under this path."),
        click.option("--min-messages", type=int, default=None, help="At least N messages."),
        click.option("--max-messages", type=int, default=None, help="At most N messages."),
        click.option("--model", default=None, help="Only models whose name contains this text."),
        click.option("--provider", default=None, help="Only sessions billed by this provider."),
        click.option("--dry-run", is_flag=True, help="Show what matches and change nothing."),
        click.option("-y", "--yes", "assume_yes", is_flag=True, help="Do not ask first."),
    ]

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        for option in reversed(options):
            fn = option(fn)
        return fn

    return decorate


# -- selection ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionRow:
    id: str
    source: str
    title: str | None
    model: str | None
    started_at: float
    ended_at: float | None
    last_active: float | None
    message_count: int
    pinned: bool
    archived: bool
    cwd: str | None
    extra: dict[str, Any] = field(default_factory=lambda: {}, compare=False)


def _sane(value: object, now: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if WINDOW_START <= number <= now + DAY else None


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _where(flt: SessionFilter) -> tuple[list[str], list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    if flt.sources:
        clauses.append(f"s.source IN ({', '.join('?' for _ in flt.sources)})")
        params.extend(flt.sources)
    else:
        clauses.append(f"s.source NOT IN ({', '.join('?' for _ in INTERNAL_SOURCES)})")
        params.extend(INTERNAL_SOURCES)
    if flt.before is not None:
        clauses.append("s.started_at < ?")
        params.append(flt.before)
    if flt.after is not None:
        clauses.append("s.started_at >= ?")
        params.append(flt.after)
    if flt.title:
        clauses.append("s.title LIKE ? ESCAPE '\\'")
        params.append(_like(flt.title))
    if flt.model:
        clauses.append("s.model LIKE ? ESCAPE '\\'")
        params.append(_like(flt.model))
    if flt.provider:
        clauses.append("LOWER(COALESCE(s.billing_provider, '')) = ?")
        params.append(flt.provider.lower())
    if flt.cwd:
        prefix = flt.cwd.rstrip("/\\") or flt.cwd
        clauses.append("(s.cwd = ? OR s.cwd LIKE ? ESCAPE '\\')")
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.extend([prefix, escaped + "/%"])
    if flt.min_messages is not None:
        clauses.append("s.message_count >= ?")
        params.append(flt.min_messages)
    if flt.max_messages is not None:
        clauses.append("s.message_count <= ?")
        params.append(flt.max_messages)
    return clauses, params


def select_rows(
    conn: sqlite3.Connection,
    flt: SessionFilter,
    *,
    now: float,
    ended_only: bool = False,
    extra_where: Sequence[str] = (),
) -> list[SessionRow]:
    """Rows matching ``flt``, newest start first (read-only SELECTs)."""
    clauses, params = _where(flt)
    if ended_only:
        clauses.append("s.ended_at IS NOT NULL")
    clauses.extend(extra_where)
    sql = (
        "SELECT s.id, s.source, s.title, s.model, s.started_at, s.ended_at, s.last_activity_at, "
        "s.message_count, s.pinned, s.archived, s.cwd, s.tool_call_count, "
        "(SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = s.id) AS last_message_at "
        f"FROM sessions s WHERE {' AND '.join(clauses)} ORDER BY s.started_at DESC, s.id DESC"
    )
    out: list[SessionRow] = []
    for row in conn.execute(sql, params):
        stamps = [_sane(row[k], now) for k in ("last_activity_at", "last_message_at", "started_at")]
        usable = [s for s in stamps if s is not None]
        last = max(usable) if usable else None
        if flt.older_than is not None and (last is None or last >= flt.older_than):
            continue
        if flt.newer_than is not None and (last is None or last < flt.newer_than):
            continue
        out.append(
            SessionRow(
                id=str(row["id"]),
                source=str(row["source"]),
                title=row["title"],
                model=row["model"],
                started_at=float(row["started_at"]),
                ended_at=row["ended_at"],
                last_active=last,
                message_count=int(row["message_count"] or 0),
                pinned=bool(row["pinned"]),
                archived=bool(row["archived"]),
                cwd=row["cwd"],
                extra={"tool_call_count": int(row["tool_call_count"] or 0)},
            )
        )
    return out


def days_between(cutoff: float, now: float) -> float:
    """``--older-than`` cutoff as a day count for P02's ``prune`` (never negative)."""
    return max((now - cutoff) / DAY, 0.0)


def after_days(now: datetime, days: float) -> float:
    return (now - timedelta(days=days)).timestamp()
