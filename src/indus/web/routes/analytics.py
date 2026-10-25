"""``GET /api/analytics/usage``: tokens, cost, API calls and sessions per day (P18-13).

``days`` (default 30, 1..365; the page offers 7, 30 and 90) counts calendar days back from today in
the clock's zone, today included. Each session's main-loop counters land on the day it started;
side calls (title, compression, vision… rows of ``model_usage`` with a task) land on their
session's day too, so nothing is counted twice. Cost is the billed figure where known, else the
estimate, row by row (``state.usage``'s rule). ``api_calls`` is the model requests the store
counted (``api_call_count``, main loop plus side calls). Only days with a session appear in
``daily``; ``totals`` sums them.

``by_model`` (W58-J1-1) splits the same window by model from ``model_usage`` (main loop and side
calls), most tokens first. A gateway that reports cumulative totals (``mode="absolute"``) writes no
``model_usage`` rows, so its sessions count in ``totals`` but under no model.

The store is opened read-only (``state.browse``): a missing store is an empty answer, a store that
cannot be read right now is 503 with a code; nothing is ever created or migrated.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, HTTPException, Query

from indus.foundation.errors import StoreUnavailable
from indus.foundation.scope import Scope
from indus.state import browse
from indus.state.errors import Bucket, classify
from indus.web.scope import read_scope

__all__ = ["ROUTE_LIST", "router", "usage_by_day", "usage_by_model"]

router = APIRouter()

ROUTE_LIST: Final[tuple[tuple[str, str, bool], ...]] = (("GET", "/api/analytics/usage", True),)
"""``(method, path, scoped)`` of every route here."""

ReadScope = Annotated[Scope, Depends(read_scope)]

_COST: Final = "COALESCE(actual_cost_usd, estimated_cost_usd, 0)"
_MAIN_SQL: Final = (
    "SELECT id, started_at, input_tokens, output_tokens, api_call_count, "
    f"{_COST} AS cost FROM sessions WHERE started_at >= ?"
)
_SIDE_SQL: Final = (
    "SELECT s.started_at AS started_at, "
    "COALESCE(SUM(u.input_tokens), 0) AS input_tokens, "
    "COALESCE(SUM(u.output_tokens), 0) AS output_tokens, "
    "COALESCE(SUM(u.api_call_count), 0) AS api_calls, "
    "COALESCE(SUM(COALESCE(u.actual_cost_usd, u.estimated_cost_usd, 0)), 0) AS cost "
    "FROM model_usage u JOIN sessions s ON s.id = u.session_id "
    "WHERE u.task <> '' AND s.started_at >= ? GROUP BY u.session_id"
)
_MODEL_SQL: Final = (
    "SELECT u.model AS model, "
    "COALESCE(SUM(u.input_tokens), 0) AS input_tokens, "
    "COALESCE(SUM(u.output_tokens), 0) AS output_tokens, "
    "COALESCE(SUM(u.api_call_count), 0) AS api_calls, "
    "COALESCE(SUM(COALESCE(u.actual_cost_usd, u.estimated_cost_usd, 0)), 0) AS cost "
    "FROM model_usage u JOIN sessions s ON s.id = u.session_id "
    "WHERE s.started_at >= ? GROUP BY u.model"
)
_FIELDS: Final = ("input_tokens", "output_tokens", "api_calls", "cost", "sessions")


def _store_error(exc: BaseException) -> HTTPException:
    if isinstance(exc, StoreUnavailable):
        return HTTPException(
            status_code=503,
            detail={"code": "store_unavailable", "message": "The session history cannot be read."},
        )
    if classify(exc) is Bucket.CORRUPT:
        code, message = "store_corrupt", "The session history is damaged; run `indus doctor`."
    else:
        code, message = "store_busy", "The session history is busy right now; try again."
    return HTTPException(status_code=503, detail={"code": code, "message": message})


def _day_of(started_at: float, zone: tzinfo | None) -> str:
    return datetime.fromtimestamp(started_at, tz=zone).date().isoformat()


def _first_day(today: date, days: int, zone: tzinfo | None) -> float:
    start = datetime.combine(today - timedelta(days=days - 1), time.min, tzinfo=zone)
    return start.timestamp()


def usage_by_day(conn: sqlite3.Connection, since: float, zone: tzinfo | None) -> dict[str, Any]:
    """``{day: {input_tokens, output_tokens, cost, sessions}}`` for sessions started at/after
    ``since`` (epoch seconds), days in ``zone``."""
    daily: dict[str, dict[str, Any]] = {}

    def bucket(day: str) -> dict[str, Any]:
        return daily.setdefault(
            day,
            {"input_tokens": 0, "output_tokens": 0, "api_calls": 0, "cost": 0.0, "sessions": 0},
        )

    for row in conn.execute(_MAIN_SQL, (since,)):
        into = bucket(_day_of(float(row["started_at"]), zone))
        into["input_tokens"] += int(row["input_tokens"] or 0)
        into["output_tokens"] += int(row["output_tokens"] or 0)
        into["api_calls"] += int(row["api_call_count"] or 0)
        into["cost"] += float(row["cost"] or 0.0)
        into["sessions"] += 1
    for row in conn.execute(_SIDE_SQL, (since,)):
        into = bucket(_day_of(float(row["started_at"]), zone))
        into["input_tokens"] += int(row["input_tokens"] or 0)
        into["output_tokens"] += int(row["output_tokens"] or 0)
        into["api_calls"] += int(row["api_calls"] or 0)
        into["cost"] += float(row["cost"] or 0.0)
    return daily


def usage_by_model(conn: sqlite3.Connection, since: float) -> list[dict[str, Any]]:
    """``[{model, input_tokens, output_tokens, api_calls, cost}]`` over ``model_usage`` rows of
    sessions started at/after ``since``, most tokens first (then calls, then name)."""
    rows = [
        {
            "model": str(row["model"]),
            "input_tokens": int(row["input_tokens"] or 0),
            "output_tokens": int(row["output_tokens"] or 0),
            "api_calls": int(row["api_calls"] or 0),
            "cost": round(float(row["cost"] or 0.0), 6),
        }
        for row in conn.execute(_MODEL_SQL, (since,))
    ]
    rows.sort(
        key=lambda r: (-(r["input_tokens"] + r["output_tokens"]), -r["api_calls"], r["model"])
    )
    return rows


@router.get("/api/analytics/usage")
def get_usage(scope: ReadScope, days: Annotated[int, Query(ge=1, le=365)] = 30) -> dict[str, Any]:
    now = scope.clock.now()
    zone = now.tzinfo
    since = _first_day(now.date(), days, zone)
    daily: dict[str, dict[str, Any]] = {}
    models: list[dict[str, Any]] = []
    try:
        store = browse.open_readonly(scope)
    except StoreUnavailable as exc:
        if exc.kind != "missing":
            raise _store_error(exc) from None
        store = None
    except (sqlite3.Error, OSError) as exc:
        raise _store_error(exc) from None
    if store is not None:
        try:
            with store.read() as conn:
                daily = usage_by_day(conn, since, zone)
                models = usage_by_model(conn, since)
        except sqlite3.Error as exc:
            raise _store_error(exc) from None
        finally:
            store.close()
    rows = [{"day": day, **values} for day, values in sorted(daily.items())]
    for row in rows:
        row["cost"] = round(row["cost"], 6)
    totals: dict[str, Any] = {name: sum(row[name] for row in rows) for name in _FIELDS}
    totals["cost"] = round(totals["cost"], 6)
    return {"days": days, "daily": rows, "totals": totals, "by_model": models}
