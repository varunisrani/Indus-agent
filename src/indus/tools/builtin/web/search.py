# pyright: strict
"""``web_search``: limit handling, the per-profile memo with single flight, and the keyless rescue.

The provider is asked for a *bucket* of results (10, 20, 50 or 100) and the answer is cut to the
limit asked, so limits 3 and 7 share one memo entry. The memo keeps successful answers for
``MEMO_TTL_S`` (on the profile's clock), at most ``MEMO_MAX_ENTRIES`` of them, keyed by (profile,
provider, folded query, bucket). Concurrent identical searches on one event loop share a single
provider call; when that call is cancelled the others start their own.

When a provider that needs a key fails and ``web.keyless_rescue`` is on, one keyless provider
serves this call; the answer says so and is never memoised, so the next call tries the user's
provider again. A keyless provider's own failure is not rescued.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from indus.foundation.clock import Clock, SystemClock
from indus.foundation.scope import Scope
from indus.tools.builtin.web.providers import (
    NoProvider,
    ProviderFailed,
    ProviderRegistry,
    SearchHit,
    WebProvider,
    choose,
    config_flag,
    keyless_for,
)

__all__ = [
    "DEFAULT_LIMIT",
    "LIMIT_BUCKETS",
    "MEMO_MAX_ENTRIES",
    "MEMO_TTL_S",
    "SearchMemo",
    "SearchOutcome",
    "bucket_for",
    "clamp_limit",
    "fold_query",
    "run_search",
]

log = logging.getLogger(__name__)

DEFAULT_LIMIT = 5
MAX_LIMIT = 100
LIMIT_BUCKETS = (10, 20, 50, 100)
MEMO_TTL_S = 20 * 60.0
MEMO_MAX_ENTRIES = 500

type MemoKey = tuple[str, str, str, int]


def clamp_limit(value: object) -> int:
    """``value`` as a result count in 1–100; anything that is not a number gives the default."""
    if isinstance(value, bool):
        return DEFAULT_LIMIT
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(number, MAX_LIMIT))


def bucket_for(limit: int) -> int:
    return next((b for b in LIMIT_BUCKETS if limit <= b), LIMIT_BUCKETS[-1])


def fold_query(query: str) -> str:
    return re.sub(r"\s+", " ", query.strip()).casefold()


@dataclass(frozen=True)
class SearchOutcome:
    provider: str
    hits: tuple[SearchHit, ...]
    rescued_from: str | None = None
    note: str | None = None

    @property
    def cacheable(self) -> bool:
        return self.rescued_from is None


class SearchMemo:
    """Successful search answers with expiry, plus the in-flight table for single flight."""

    def __init__(self, *, ttl_s: float = MEMO_TTL_S, max_entries: int = MEMO_MAX_ENTRIES) -> None:
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self._entries: OrderedDict[MemoKey, tuple[float, SearchOutcome]] = OrderedDict()
        self._flights: dict[MemoKey, asyncio.Future[SearchOutcome]] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def lookup(self, key: MemoKey, now: float) -> SearchOutcome | None:
        with self._lock:
            hit = self._entries.get(key)
            if hit is None:
                return None
            if now >= hit[0]:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return hit[1]

    def store(self, key: MemoKey, now: float, outcome: SearchOutcome) -> None:
        if not outcome.cacheable:
            return
        with self._lock:
            for stale in [k for k, (expires, _) in self._entries.items() if now >= expires]:
                del self._entries[stale]
            self._entries.pop(key, None)
            self._entries[key] = (now + self.ttl_s, outcome)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    async def run(
        self, key: MemoKey, clock: Clock, fetch: Callable[[], Awaitable[SearchOutcome]]
    ) -> SearchOutcome:
        """The memoised outcome for ``key``, or ``fetch()`` run once for all concurrent callers."""
        while True:
            hit = self.lookup(key, clock.monotonic())
            if hit is not None:
                return hit
            loop = asyncio.get_running_loop()
            with self._lock:
                flight = self._flights.get(key)
                lead = flight is None
                if flight is None:
                    flight = loop.create_future()
                    flight.add_done_callback(_consume)
                    self._flights[key] = flight
            if not lead and flight.get_loop() is not loop:
                return await self._fetch_and_store(key, clock, fetch, None)  # another loop's call
            if not lead:
                try:
                    return await asyncio.shield(flight)
                except asyncio.CancelledError:
                    if flight.cancelled():
                        continue  # the leading call was cancelled: start over
                    raise
            return await self._fetch_and_store(key, clock, fetch, flight)

    async def _fetch_and_store(
        self,
        key: MemoKey,
        clock: Clock,
        fetch: Callable[[], Awaitable[SearchOutcome]],
        flight: asyncio.Future[SearchOutcome] | None,
    ) -> SearchOutcome:
        try:
            outcome = await fetch()
        except BaseException as exc:
            if flight is not None:
                self._land(key, flight)
                if isinstance(exc, asyncio.CancelledError):
                    flight.cancel()
                else:
                    flight.set_exception(exc)
            raise
        self.store(key, clock.monotonic(), outcome)
        if flight is not None:
            self._land(key, flight)
            flight.set_result(outcome)
        return outcome

    def _land(self, key: MemoKey, flight: asyncio.Future[SearchOutcome]) -> None:
        with self._lock:
            if self._flights.get(key) is flight:
                del self._flights[key]


def _consume(future: asyncio.Future[SearchOutcome]) -> None:
    if not future.cancelled():
        future.exception()  # mark it retrieved: a failure nobody else awaited is not an error


async def _ask(provider: WebProvider, query: str, limit: int, scope: Scope | None) -> SearchOutcome:
    hits = await provider.search(query, limit, scope=scope)
    return SearchOutcome(provider.name, tuple(hits))


async def _with_rescue(
    registry: ProviderRegistry,
    provider: WebProvider,
    query: str,
    fetch_limit: int,
    scope: Scope | None,
) -> SearchOutcome:
    try:
        return await _ask(provider, query, fetch_limit, scope)
    except ProviderFailed as exc:
        failure = str(exc)
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    if provider.keyless or not config_flag(scope, "web.keyless_rescue"):
        raise ProviderFailed(failure)
    spare = keyless_for(registry, scope, "search", besides=provider.name)
    if spare is None:
        raise ProviderFailed(failure)
    log.warning(
        "web_search: %s failed (%s); one keyless try on %s", provider.name, failure, spare.name
    )
    try:
        rescued = await _ask(spare, query, fetch_limit, scope)
    except Exception as exc:
        raise ProviderFailed(
            f"{failure} (the keyless try on {spare.name} failed too: {exc})"
        ) from exc
    note = (
        f"{provider.name} failed for this search ({failure[:300]}); these results come from "
        f"{spare.name}. The next search uses {provider.name} again."
    )
    return SearchOutcome(spare.name, rescued.hits, rescued_from=provider.name, note=note)


async def run_search(
    registry: ProviderRegistry,
    memo: SearchMemo,
    args: dict[str, Any],
    scope: Scope | None,
    *,
    clock: Clock | None = None,
) -> dict[str, Any]:
    """The ``web_search`` result as a dict (``{"error": …}`` on failure)."""
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return {"error": "web_search needs a non-empty query"}
    limit = clamp_limit(args.get("limit", DEFAULT_LIMIT))
    try:
        provider = choose(registry, scope, "search")
    except NoProvider as exc:
        return {"error": str(exc)}
    fetch_limit = bucket_for(limit)
    profile = str(scope.home) if scope is not None else ""
    key: MemoKey = (profile, provider.name, fold_query(query), fetch_limit)
    use_clock = clock or (scope.clock if scope is not None else SystemClock())
    try:
        outcome = await memo.run(
            key,
            use_clock,
            lambda: _with_rescue(registry, provider, query.strip(), fetch_limit, scope),
        )
    except ProviderFailed as exc:
        return {"error": f"web search via {provider.name} failed: {exc}"}
    results = [
        {"position": n, "title": hit.title, "url": hit.url, "snippet": hit.snippet}
        for n, hit in enumerate(outcome.hits[:limit], start=1)
    ]
    body: dict[str, Any] = {
        "query": query.strip(),
        "provider": outcome.provider,
        "results": results,
    }
    if outcome.rescued_from is not None:
        body["rescued_from"] = outcome.rescued_from
    if outcome.note:
        body["note"] = outcome.note
    return body
