"""Token and cost accounting per session and per model route.

Each model call reports a ``UsageDelta``. The session row keeps the conversation's running totals
and its route (model, billing provider, base URL); ``model_usage`` keeps the same numbers split by
(model, provider, base URL, task) so a mid-session model switch is billed to the right model.

- ``mode="incremental"`` (per-call deltas): counters are added. The route columns are only filled
  where they are still empty — except on the first call that accounts any usage, whose route wins
  over the one recorded at creation (that route may have failed over). The delta also lands in
  ``model_usage`` under task ``''`` (main loop).
- ``mode="absolute"`` (a gateway's cumulative totals): counters are set; nothing goes to
  ``model_usage``, because a cumulative figure cannot be split back into routes.
- ``task`` set (``title``, ``compression``, ``vision``, …): a side call. Only ``model_usage`` is
  written, under that task, with the route the delta names — never the session's main route.

Reads sum over the compression lineage (root … tip), so a rotated conversation keeps its totals.
Costs prefer the billed (actual) figure over the estimate. Writes are synchronous on the
``ROUTINE`` budget; a coalescing background writer arrives with P08.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from indus.state import lineage
from indus.state.registry import StoreHandle

__all__ = [
    "ModelUsage",
    "Route",
    "UsageDelta",
    "UsageMode",
    "UsageTotals",
    "by_model",
    "record",
    "record_in",
    "recent_route",
    "set_route",
    "store_totals",
    "totals",
]

log = logging.getLogger(__name__)

UsageMode = Literal["incremental", "absolute"]
_TOKENS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)
UNKNOWN_MODEL = "unknown"


@dataclass(frozen=True, slots=True)
class UsageDelta:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    api_calls: int = 1
    estimated_cost_usd: float | None = None
    actual_cost_usd: float | None = None
    cost_status: str | None = None
    model: str | None = None
    billing_provider: str | None = None
    billing_base_url: str | None = None

    @property
    def tokens(self) -> tuple[int, int, int, int, int]:
        return (
            self.input_tokens,
            self.output_tokens,
            self.cache_read_tokens,
            self.cache_write_tokens,
            self.reasoning_tokens,
        )

    @property
    def accounts_anything(self) -> bool:
        return (
            any(self.tokens)
            or self.api_calls > 0
            or bool(self.estimated_cost_usd)
            or bool(self.actual_cost_usd)
        )


@dataclass(frozen=True, slots=True)
class Route:
    model: str
    billing_provider: str
    billing_base_url: str


@dataclass(frozen=True, slots=True)
class UsageTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    api_calls: int = 0
    estimated_cost_usd: float = 0.0
    actual_cost_usd: float | None = None
    cost_usd: float = 0.0  # actual where known, else the estimate, row by row


@dataclass(frozen=True, slots=True)
class ModelUsage:
    model: str
    billing_provider: str
    billing_base_url: str
    task: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    reasoning_tokens: int
    api_calls: int
    estimated_cost_usd: float
    actual_cost_usd: float | None
    cost_usd: float


def _now(store: StoreHandle) -> float:
    return store.scope.clock.now().timestamp()


# -- writes -------------------------------------------------------------------------------------


def _upsert_model_usage(
    conn: sqlite3.Connection,
    session_id: str,
    route: tuple[str, str, str],
    task: str,
    delta: UsageDelta,
    api_calls: int,
    now: float,
) -> None:
    conn.execute(
        "INSERT INTO model_usage (session_id, model, billing_provider, billing_base_url, task, "
        "api_call_count, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
        "reasoning_tokens, estimated_cost_usd, actual_cost_usd, cost_status, first_seen, "
        "last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (session_id, model, billing_provider, billing_base_url, task) DO UPDATE SET "
        "api_call_count = api_call_count + excluded.api_call_count, "
        "input_tokens = input_tokens + excluded.input_tokens, "
        "output_tokens = output_tokens + excluded.output_tokens, "
        "cache_read_tokens = cache_read_tokens + excluded.cache_read_tokens, "
        "cache_write_tokens = cache_write_tokens + excluded.cache_write_tokens, "
        "reasoning_tokens = reasoning_tokens + excluded.reasoning_tokens, "
        "estimated_cost_usd = estimated_cost_usd + excluded.estimated_cost_usd, "
        "actual_cost_usd = CASE WHEN excluded.actual_cost_usd IS NULL THEN actual_cost_usd "
        "ELSE COALESCE(actual_cost_usd, 0) + excluded.actual_cost_usd END, "
        "cost_status = COALESCE(excluded.cost_status, cost_status), "
        "last_seen = excluded.last_seen",
        (
            session_id,
            *route,
            task,
            api_calls,
            *delta.tokens,
            float(delta.estimated_cost_usd or 0.0),
            delta.actual_cost_usd,
            delta.cost_status,
            now,
            now,
        ),
    )


def _main_loop_in(
    conn: sqlite3.Connection,
    session_id: str,
    delta: UsageDelta,
    mode: UsageMode,
    row: sqlite3.Row,
    now: float,
) -> None:
    accounts = delta.accounts_anything
    if (
        accounts
        and int(row["api_call_count"]) == 0
        and delta.model
        and delta.billing_provider
        and (row["model"], row["billing_provider"]) != (delta.model, delta.billing_provider)
    ):
        # First accounted call: the route that actually answered replaces the requested one.
        conn.execute(
            "UPDATE sessions SET model = ?, billing_provider = ?, billing_base_url = ? "
            "WHERE id = ?",
            (delta.model, delta.billing_provider, delta.billing_base_url, session_id),
        )
    route_fill = (
        (delta.model, delta.billing_provider, delta.billing_base_url)
        if accounts
        else (None, None, None)
    )
    if mode == "incremental":
        counters = ", ".join(f"{c} = {c} + ?" for c in _TOKENS)
        estimated = "estimated_cost_usd = COALESCE(estimated_cost_usd, 0) + COALESCE(?, 0)"
        actual = (
            "actual_cost_usd = CASE WHEN ? IS NULL THEN actual_cost_usd "
            "ELSE COALESCE(actual_cost_usd, 0) + ? END"
        )
        calls = "api_call_count = api_call_count + ?"
    else:
        counters = ", ".join(f"{c} = ?" for c in _TOKENS)
        estimated = "estimated_cost_usd = COALESCE(?, 0)"
        actual = "actual_cost_usd = CASE WHEN ? IS NULL THEN actual_cost_usd ELSE ? END"
        calls = "api_call_count = ?"
    conn.execute(
        f"UPDATE sessions SET {counters}, {estimated}, {actual}, {calls}, "
        "cost_status = COALESCE(?, cost_status), model = COALESCE(model, ?), "
        "billing_provider = COALESCE(billing_provider, ?), "
        "billing_base_url = COALESCE(billing_base_url, ?) WHERE id = ?",
        (
            *delta.tokens,
            delta.estimated_cost_usd,
            delta.actual_cost_usd,
            delta.actual_cost_usd,
            max(delta.api_calls, 0),
            delta.cost_status,
            *route_fill,
            session_id,
        ),
    )
    if mode == "incremental" and accounts:
        current = conn.execute(
            "SELECT model, billing_provider, billing_base_url FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        route = (
            delta.model or current["model"] or UNKNOWN_MODEL,
            delta.billing_provider or current["billing_provider"] or "",
            delta.billing_base_url or current["billing_base_url"] or "",
        )
        _upsert_model_usage(conn, session_id, route, "", delta, max(delta.api_calls, 0), now)


def record_in(
    conn: sqlite3.Connection,
    session_id: str,
    delta: UsageDelta,
    *,
    mode: UsageMode = "incremental",
    task: str = "",
    now: float,
) -> bool:
    """``record`` on the caller's write transaction."""
    if mode not in ("incremental", "absolute"):
        raise ValueError(f"unknown usage mode {mode!r}")
    row = conn.execute(
        "SELECT model, billing_provider, api_call_count FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        return False
    if task:
        route = (
            delta.model or UNKNOWN_MODEL,
            delta.billing_provider or "",
            delta.billing_base_url or "",
        )
        _upsert_model_usage(conn, session_id, route, task, delta, max(delta.api_calls, 0), now)
        return True
    _main_loop_in(conn, session_id, delta, mode, row, now)
    return True


def record(
    store: StoreHandle,
    session_id: str,
    delta: UsageDelta,
    *,
    mode: UsageMode = "incremental",
    task: str = "",
) -> bool:
    """Account one usage report; False (logged) when the session row does not exist."""
    done = store.write(
        lambda conn: record_in(conn, session_id, delta, mode=mode, task=task, now=_now(store))
    )
    if not done:
        log.warning("usage for unknown session %s was not recorded", session_id)
    return done


def set_route(
    store: StoreHandle,
    session_id: str,
    *,
    billing_provider: str,
    billing_base_url: str,
    model: str | None = None,
) -> bool:
    """Record a route switch (``/model``): unlike ``record`` this overwrites the route."""

    def switch(conn: sqlite3.Connection) -> bool:
        cur = conn.execute(
            "UPDATE sessions SET billing_provider = ?, billing_base_url = ?, "
            "model = COALESCE(?, model) WHERE id = ?",
            (billing_provider, billing_base_url, model, session_id),
        )
        return cur.rowcount == 1

    return store.write(switch)


# -- reads --------------------------------------------------------------------------------------


def _lineage(conn: sqlite3.Connection, session_id: str) -> list[str]:
    try:
        return lineage.lineage_ids_in(conn, session_id)
    except lineage.AmbiguousChain:
        log.warning("usage of %s read from that segment only: its chain is ambiguous", session_id)
        return [session_id]


def _marks(ids: Sequence[str]) -> str:
    return ", ".join("?" for _ in ids)


def _totals_from(row: sqlite3.Row) -> UsageTotals:
    return UsageTotals(
        input_tokens=int(row["input_tokens"]),
        output_tokens=int(row["output_tokens"]),
        cache_read_tokens=int(row["cache_read_tokens"]),
        cache_write_tokens=int(row["cache_write_tokens"]),
        reasoning_tokens=int(row["reasoning_tokens"]),
        api_calls=int(row["api_calls"]),
        estimated_cost_usd=float(row["estimated"]),
        actual_cost_usd=None if row["actual"] is None else float(row["actual"]),
        cost_usd=float(row["cost"]),
    )


_SUMS = (
    "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
    "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
    "COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens, "
    "COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens, "
    "COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens, "
    "COALESCE(SUM(api_call_count), 0) AS api_calls, "
    "COALESCE(SUM(estimated_cost_usd), 0) AS estimated, "
    "SUM(actual_cost_usd) AS actual, "
    "COALESCE(SUM(COALESCE(actual_cost_usd, estimated_cost_usd, 0)), 0) AS cost"
)


def totals(store: StoreHandle, session_id: str) -> UsageTotals:
    """The conversation's main-loop totals, summed over its compression lineage."""
    with store.read() as conn:
        ids = _lineage(conn, session_id)
        row = conn.execute(
            f"SELECT {_SUMS} FROM sessions WHERE id IN ({_marks(ids)})", tuple(ids)
        ).fetchone()
    return _totals_from(row)


def by_model(store: StoreHandle, session_id: str) -> list[ModelUsage]:
    """Per-route and per-task usage over the lineage, main loop and side calls alike."""
    with store.read() as conn:
        ids = _lineage(conn, session_id)
        rows = conn.execute(
            f"SELECT model, billing_provider, billing_base_url, task, {_SUMS} FROM model_usage "
            f"WHERE session_id IN ({_marks(ids)}) "
            "GROUP BY model, billing_provider, billing_base_url, task "
            "ORDER BY task, model, billing_provider, billing_base_url",
            tuple(ids),
        ).fetchall()
    out: list[ModelUsage] = []
    for row in rows:
        sums = _totals_from(row)
        out.append(
            ModelUsage(
                model=str(row["model"]),
                billing_provider=str(row["billing_provider"]),
                billing_base_url=str(row["billing_base_url"]),
                task=str(row["task"]),
                input_tokens=sums.input_tokens,
                output_tokens=sums.output_tokens,
                cache_read_tokens=sums.cache_read_tokens,
                cache_write_tokens=sums.cache_write_tokens,
                reasoning_tokens=sums.reasoning_tokens,
                api_calls=sums.api_calls,
                estimated_cost_usd=sums.estimated_cost_usd,
                actual_cost_usd=sums.actual_cost_usd,
                cost_usd=sums.cost_usd,
            )
        )
    return out


def recent_route(store: StoreHandle, session_id: str) -> Route | None:
    """The most recently used main-loop route of the conversation, as one coherent triple."""
    with store.read() as conn:
        ids = _lineage(conn, session_id)
        row = conn.execute(
            "SELECT model, billing_provider, billing_base_url FROM model_usage "
            f"WHERE session_id IN ({_marks(ids)}) AND task = '' AND model <> ? "
            "AND billing_provider <> '' ORDER BY last_seen DESC, rowid DESC LIMIT 1",
            (*ids, UNKNOWN_MODEL),
        ).fetchone()
    if row is None:
        return None
    return Route(str(row["model"]), str(row["billing_provider"]), str(row["billing_base_url"]))


def _add(a: UsageTotals, b: UsageTotals) -> UsageTotals:
    actual = (
        None
        if a.actual_cost_usd is None and b.actual_cost_usd is None
        else (a.actual_cost_usd or 0.0) + (b.actual_cost_usd or 0.0)
    )
    return UsageTotals(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        cache_read_tokens=a.cache_read_tokens + b.cache_read_tokens,
        cache_write_tokens=a.cache_write_tokens + b.cache_write_tokens,
        reasoning_tokens=a.reasoning_tokens + b.reasoning_tokens,
        api_calls=a.api_calls + b.api_calls,
        estimated_cost_usd=a.estimated_cost_usd + b.estimated_cost_usd,
        actual_cost_usd=actual,
        cost_usd=a.cost_usd + b.cost_usd,
    )


def store_totals(store: StoreHandle, *, include_archived: bool = False) -> UsageTotals:
    """Everything this store has spent: every session segment once, plus side calls."""
    archived = "" if include_archived else " WHERE archived = 0"
    aux_archived = (
        ""
        if include_archived
        else " AND session_id IN (SELECT id FROM sessions WHERE archived = 0)"
    )
    with store.read() as conn:
        main = conn.execute(f"SELECT {_SUMS} FROM sessions{archived}").fetchone()
        side = conn.execute(
            f"SELECT {_SUMS} FROM model_usage WHERE task <> ''{aux_archived}"
        ).fetchone()
    return _add(_totals_from(main), _totals_from(side))
