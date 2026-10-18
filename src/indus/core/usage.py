"""Per-call usage and cost accounting for a session (P08-08, B24).

``CanonicalUsage`` is one call's tokens in fixed buckets: uncached input, output, cache read,
cache write, reasoning, request count, plus the vendor's raw usage. ``prompt_tokens`` is always
``input + cache_read + cache_write``. Vendors differ in what they report:

- Anthropic Messages and Bedrock Converse report the *uncached* input; it is taken as is;
- chat completions, the Responses API and Gemini report the *whole* prompt with the cached part
  inside it, so the cache buckets are taken out of it.

P03's normalised ``Usage`` already carries the whole prompt, so ``CanonicalUsage.from_usage`` only
subtracts; ``normalize(raw, api_mode)`` reads a vendor's raw usage object directly.

``UsageLedger`` is a session's running account. ``record`` takes one answered call (usage may be
missing: the call still counts), prices it (``providers.pricing``), keeps the counters and the last
ten latencies and output sizes, keeps the newest rate-limit headers, tells its listeners (the
context engine and the usage anchor fold real prompt sizes in, P08-01/05), writes one INFO line
and hands a ``UsageDelta`` to the store sink — main-loop calls untagged, side calls under their
task (``title``, ``compression`` …). ``RecordingCaller`` wraps the loop's model caller so every
answered call is recorded without the loop knowing.
"""

from __future__ import annotations

import inspect
import logging
import re
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

from indus.providers.pricing import CostResult, PriceTable, estimate_cost, format_cost
from indus.providers.streaming import CallOutcome, CallResult
from indus.providers.types import ChatRequest, Usage
from indus.state.usage import UsageDelta

if TYPE_CHECKING:
    from indus.core.model_call import ModelCaller, OnEvent, OnWait
    from indus.foundation.clock import Clock

log = logging.getLogger(__name__)

__all__ = [
    "HISTORY",
    "CanonicalUsage",
    "RateLimitBucket",
    "RecordingCaller",
    "UsageLedger",
    "UsageRecord",
    "UsageSnapshot",
    "normalize",
    "parse_rate_limits",
    "parse_reset",
]

HISTORY = 10  # latencies and output sizes kept for averages

type StoreSink = Callable[[UsageDelta, str], object]
type Listener = Callable[["CanonicalUsage | None", str], object]

# Wire formats whose input count leaves the cached part out.
_UNCACHED_INPUT_MODES = frozenset({"anthropic_messages", "bedrock", "bedrock_converse"})


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    number = int(value)
    return number if number > 0 else 0


def _at(raw: Mapping[str, Any], *path: str) -> int:
    node: object = raw
    for key in path:
        if not isinstance(node, Mapping):
            return 0
        node = cast(Mapping[str, object], node).get(key)
    return _count(node)


def _first(raw: Mapping[str, Any], *paths: tuple[str, ...]) -> int:
    for path in paths:
        value = _at(raw, *path)
        if value:
            return value
    return 0


@dataclass(frozen=True)
class CanonicalUsage:
    """One call's tokens in fixed buckets (see the module notes); ``raw`` is the vendor's own
    usage object, kept for pricing details (served tier, cache lifetimes, reported cost)."""

    # prompt side: uncached input plus the two cache buckets make up the whole prompt
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # answer side: reasoning is part of the output, counted again on its own
    output_tokens: int = 0
    reasoning_tokens: int = 0
    request_count: int = 1
    raw: Mapping[str, Any] | None = field(default=None, compare=False, repr=False)

    @classmethod
    def whole_prompt(
        cls,
        prompt: int,
        output: int,
        cache_read: int,
        cache_write: int,
        reasoning: int,
        raw: Mapping[str, Any] | None = None,
    ) -> CanonicalUsage:
        """From a report whose prompt count includes the cached part."""
        cached = cache_read + cache_write
        if cached > prompt:
            log.debug("cache buckets (%d) exceed the reported prompt (%d)", cached, prompt)
        return cls(
            input_tokens=max(0, prompt - cached),
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            output_tokens=output,
            reasoning_tokens=reasoning,
            raw=raw,
        )

    @classmethod
    def from_usage(
        cls, usage: Usage | None, *, raw: Mapping[str, Any] | None = None
    ) -> CanonicalUsage | None:
        """From P03's ``Usage`` (whole prompt); ``None`` when the call reported no usage."""
        if usage is None:
            return None
        return cls.whole_prompt(
            usage.prompt_tokens,
            usage.completion_tokens,
            usage.cache_read_tokens,
            usage.cache_write_tokens,
            usage.reasoning_tokens,
            raw,
        )

    @property
    def cached_tokens(self) -> int:
        """Prompt tokens that touched the cache (read or written)."""
        return self.cache_read_tokens + self.cache_write_tokens

    @property
    def prompt_tokens(self) -> int:
        return self.cached_tokens + self.input_tokens

    @property
    def completion_tokens(self) -> int:
        """``output_tokens`` under the P03 ``Usage`` name (the usage anchor reads either)."""
        return self.output_tokens

    @property
    def total_tokens(self) -> int:
        return sum((self.output_tokens, self.prompt_tokens))

    @property
    def cache_hit_percent(self) -> float | None:
        """Share of the prompt read from the cache, or ``None`` without a prompt."""
        whole = self.prompt_tokens
        return None if not whole else self.cache_read_tokens * 100.0 / whole

    def __add__(self, other: CanonicalUsage) -> CanonicalUsage:
        names = (
            "input_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "output_tokens",
            "reasoning_tokens",
            "request_count",
        )
        return CanonicalUsage(**{n: getattr(self, n) + getattr(other, n) for n in names})


def normalize(raw: Mapping[str, Any], api_mode: str) -> CanonicalUsage:
    """Canonical buckets from a vendor's raw usage object, by wire format.

    ``anthropic_messages``: ``input_tokens`` (uncached), ``cache_read_input_tokens``,
    ``cache_creation_input_tokens``, ``output_tokens``. ``bedrock`` (also ``bedrock_converse``):
    ``inputTokens``
    (uncached), ``cacheReadInputTokens``, ``cacheWriteInputTokens``, ``outputTokens``.
    ``responses``: ``input_tokens`` (whole) with ``input_tokens_details.{cached_tokens,
    cache_write_tokens}``, ``output_tokens``, ``output_tokens_details.reasoning_tokens``.
    ``gemini``: ``promptTokenCount`` (whole, cached content included), ``cachedContentTokenCount``,
    ``candidatesTokenCount`` + ``thoughtsTokenCount`` (thinking is billed as output).
    Anything else is read as chat completions: ``prompt_tokens`` (whole) with
    ``prompt_tokens_details.cached_tokens`` (or DeepSeek's ``prompt_cache_hit_tokens``, or the
    top-level ``cache_read_input_tokens`` / ``cached_tokens`` some proxies and servers send),
    ``prompt_tokens_details.cache_write_tokens``, ``completion_tokens`` and
    ``completion_tokens_details.reasoning_tokens``; servers that name the totals
    ``input_tokens`` / ``output_tokens`` on this wire are read too.
    """
    mode = api_mode.strip().lower()
    if mode == "anthropic_messages":
        return CanonicalUsage(
            input_tokens=_at(raw, "input_tokens"),
            cache_read_tokens=_at(raw, "cache_read_input_tokens"),
            cache_write_tokens=_at(raw, "cache_creation_input_tokens"),
            output_tokens=_at(raw, "output_tokens"),
            raw=raw,
        )
    if mode in ("bedrock", "bedrock_converse"):
        return CanonicalUsage(
            input_tokens=_at(raw, "inputTokens"),
            cache_read_tokens=_at(raw, "cacheReadInputTokens"),
            cache_write_tokens=_at(raw, "cacheWriteInputTokens"),
            output_tokens=_at(raw, "outputTokens"),
            raw=raw,
        )
    if mode == "responses":
        return CanonicalUsage.whole_prompt(
            _at(raw, "input_tokens"),
            _at(raw, "output_tokens"),
            _at(raw, "input_tokens_details", "cached_tokens"),
            _at(raw, "input_tokens_details", "cache_write_tokens"),
            _at(raw, "output_tokens_details", "reasoning_tokens"),
            raw,
        )
    if mode == "gemini":
        thoughts = _at(raw, "thoughtsTokenCount")
        return CanonicalUsage.whole_prompt(
            _at(raw, "promptTokenCount"),
            _at(raw, "candidatesTokenCount") + thoughts,
            _at(raw, "cachedContentTokenCount"),
            0,
            thoughts,
            raw,
        )
    return CanonicalUsage.whole_prompt(
        _first(raw, ("prompt_tokens",), ("input_tokens",)),
        _first(raw, ("completion_tokens",), ("output_tokens",)),
        _first(
            raw,
            ("prompt_tokens_details", "cached_tokens"),
            ("prompt_cache_hit_tokens",),
            ("cache_read_input_tokens",),
            ("cached_tokens",),
        ),
        _first(
            raw,
            ("prompt_tokens_details", "cache_write_tokens"),
            ("cache_creation_input_tokens",),
        ),
        _at(raw, "completion_tokens_details", "reasoning_tokens"),
        raw,
    )


# -- rate-limit headers ----------------------------------------------------------------------------

_HEADER = re.compile(r"^x-ratelimit-(limit|remaining|reset)-(requests|tokens)(-[a-z0-9]+)?$")
_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")
_UNIT_SECONDS = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}


def parse_reset(text: str) -> float | None:
    """Seconds until a limit resets: plain seconds (``"12"``, ``"0.5"``) or a duration made of
    ``h``/``m``/``s``/``ms`` parts (``"6m0s"``, ``"1h2m3.5s"``, ``"20ms"``); ``None`` otherwise."""
    value = text.strip().lower()
    if not value:
        return None
    try:
        number = float(value)
    except ValueError:
        pass
    else:
        return number if number >= 0 and number == number else None
    total = 0.0
    position = 0
    for match in _DURATION_PART.finditer(value):
        if match.start() != position:
            return None
        total += float(match.group(1)) * _UNIT_SECONDS[match.group(2)]
        position = match.end()
    return total if position == len(value) and position > 0 else None


@dataclass(frozen=True)
class RateLimitBucket:
    """One limit window (``requests``, ``tokens``, or a suffixed variant like ``requests-1h``)."""

    limit: int | None = None
    remaining: int | None = None
    reset_s: float | None = None


def _int(text: str) -> int | None:
    try:
        return int(text.strip())
    except ValueError:
        return None


def parse_rate_limits(headers: Mapping[str, str]) -> dict[str, RateLimitBucket]:
    """Every ``x-ratelimit-{limit,remaining,reset}-{requests,tokens}[-suffix]`` header, grouped
    by bucket name. Unparseable values are left out."""
    found: dict[str, dict[str, Any]] = {}
    for name, value in headers.items():
        match = _HEADER.match(name.strip().lower())
        if match is None:
            continue
        kind, unit, suffix = match.groups()
        bucket = unit + (suffix or "")
        parsed = parse_reset(value) if kind == "reset" else _int(value)
        if parsed is None:
            continue
        found.setdefault(bucket, {})["reset_s" if kind == "reset" else kind] = parsed
    return {bucket: RateLimitBucket(**values) for bucket, values in sorted(found.items())}


# -- the ledger ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class UsageRecord:
    """One recorded call."""

    usage: CanonicalUsage | None
    cost: CostResult
    latency_s: float
    task: str
    model: str
    provider: str
    base_url: str
    response_id: str
    delta: UsageDelta


@dataclass(frozen=True)
class UsageSnapshot:
    """The ledger's state for ``/usage``, the status bar and ``-z --usage-file``."""

    api_calls: int
    usage: CanonicalUsage
    cost_usd: Decimal
    cost_status: str
    cost_label: str
    unpriced_calls: int
    last_prompt_tokens: int | None
    latencies_s: tuple[float, ...]
    output_sizes: tuple[int, ...]
    rate_limits: Mapping[str, RateLimitBucket]
    side_calls: Mapping[str, int]

    @property
    def average_latency_s(self) -> float | None:
        return sum(self.latencies_s) / len(self.latencies_s) if self.latencies_s else None

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready numbers (cost as a float, ``None`` when nothing could be priced)."""
        usage = self.usage
        priced = self.api_calls > self.unpriced_calls or self.cost_status == "included"
        return {
            "api_calls": self.api_calls,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
            "cache_write_tokens": usage.cache_write_tokens,
            "reasoning_tokens": usage.reasoning_tokens,
            "prompt_tokens": usage.prompt_tokens,
            "total_tokens": usage.total_tokens,
            "cost_usd": float(self.cost_usd) if priced else None,
            "cost_status": self.cost_status,
            "cost_label": self.cost_label,
        }


def _route_parts(route: object) -> tuple[str, str, str]:
    return (
        str(getattr(route, "model", "") or ""),
        str(getattr(route, "provider", "") or ""),
        str(getattr(route, "base_url", "") or ""),
    )


class UsageLedger:
    """A session's usage account (see the module notes)."""

    def __init__(
        self,
        *,
        clock: Clock,
        session_id: str = "",
        store: StoreSink | None = None,
        table: PriceTable | None = None,
    ) -> None:
        self.clock = clock
        self.session_id = session_id
        self.store = store
        self.table = table
        self.api_calls = 0
        self.usage = CanonicalUsage(request_count=0)
        self.cost_usd = Decimal(0)
        self.unpriced_calls = 0
        self.statuses: set[str] = set()
        self.last_prompt_tokens: int | None = None
        self.latencies: deque[float] = deque(maxlen=HISTORY)
        self.output_sizes: deque[int] = deque(maxlen=HISTORY)
        self.rate_limits: dict[str, RateLimitBucket] = {}
        self.side_calls: dict[str, int] = {}
        self._listeners: list[Listener] = []

    def add_listener(self, listener: Listener) -> Callable[[], None]:
        """Call ``listener(usage, task)`` for every answered call — ``usage`` is None when the
        provider reported none — and await it when it is a coroutine; returns a remover."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    # -- recording ---------------------------------------------------------------------------------

    async def record(
        self,
        usage: Usage | CanonicalUsage | None,
        *,
        route: object,
        latency_s: float,
        task: str = "",
        response_id: str = "",
        raw: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> UsageRecord:
        """Account one answered call (see the module notes)."""
        canonical = (
            usage
            if isinstance(usage, CanonicalUsage)
            else CanonicalUsage.from_usage(usage, raw=raw)
        )
        if isinstance(usage, CanonicalUsage) and raw is not None and usage.raw is None:
            canonical = replace(usage, raw=raw)
        model, provider, base_url = _route_parts(route)
        if canonical is None:
            cost = CostResult(None, "unknown", "no usage", ("the provider reported no usage",))
        else:
            cost = estimate_cost(
                canonical,
                model=model,
                base_url=base_url,
                on=self.clock.now().date(),
                table=self.table,
            )
        if task:
            self.side_calls[task] = self.side_calls.get(task, 0) + 1
        else:
            self._fold_main(canonical, cost, latency_s)
        if headers:
            limits = parse_rate_limits(headers)
            if limits:
                self.rate_limits.update(limits)
        delta = _delta(canonical, cost, model, provider, base_url)
        record = UsageRecord(
            canonical, cost, latency_s, task, model, provider, base_url, response_id, delta
        )
        self._log(record)
        for listener in list(self._listeners):
            try:
                outcome = listener(canonical, task)
                if inspect.isawaitable(outcome):
                    await cast(Awaitable[object], outcome)
            except Exception:
                log.warning("a usage listener failed", exc_info=True)
        await self._store(delta, task)
        return record

    def _fold_main(self, usage: CanonicalUsage | None, cost: CostResult, latency_s: float) -> None:
        self.api_calls += 1
        self.latencies.append(latency_s)
        self.statuses.add(cost.status)
        if cost.amount is not None:
            self.cost_usd += cost.amount
        else:
            self.unpriced_calls += 1
        if usage is None:
            return
        self.usage = self.usage + usage
        self.last_prompt_tokens = usage.prompt_tokens
        self.output_sizes.append(usage.output_tokens)

    def _log(self, record: UsageRecord) -> None:
        usage = record.usage
        where = f"model={record.model} provider={record.provider or 'unknown'}"
        tag = f" task={record.task}" if record.task else ""
        if usage is None:
            log.info(
                "model call #%d%s: %s in=? out=? latency=%.1fs usage=missing",
                self.api_calls,
                tag,
                where,
                record.latency_s,
            )
            return
        extra = ""
        hit = usage.cache_hit_percent
        if usage.cache_read_tokens and hit is not None:
            extra += f" cache={usage.cache_read_tokens}/{usage.prompt_tokens} ({hit:.0f}%)"
        if usage.cache_write_tokens:
            extra += f" cache_write={usage.cache_write_tokens}"
        if record.response_id:
            extra += f" id={record.response_id}"
        log.info(
            "model call #%d%s: %s in=%d out=%d total=%d latency=%.1fs cost=%s%s",
            self.api_calls,
            tag,
            where,
            usage.prompt_tokens,
            usage.output_tokens,
            usage.total_tokens,
            record.latency_s,
            record.cost.label,
            extra,
        )

    async def _store(self, delta: UsageDelta, task: str) -> None:
        if self.store is None:
            return
        try:
            result = self.store(delta, task)
            if inspect.isawaitable(result):
                await cast(Awaitable[object], result)
        except Exception as exc:
            log.warning("usage of session %s was not stored: %s", self.session_id, exc)

    # -- reading -----------------------------------------------------------------------------------

    @property
    def cost_status(self) -> str:
        """``unknown`` when any call could not be priced; else the weakest known status."""
        if self.unpriced_calls or not self.statuses:
            return "unknown"
        for status in ("estimated", "actual", "included"):
            if status in self.statuses:
                return status
        return "unknown"

    def snapshot(self) -> UsageSnapshot:
        status = self.cost_status
        if status == "included":
            label = "included"
        elif status == "unknown":
            label = "unknown" if not self.cost_usd else f"{format_cost(self.cost_usd)}+ (partial)"
        else:
            label = format_cost(self.cost_usd)
        return UsageSnapshot(
            api_calls=self.api_calls,
            usage=replace(self.usage, request_count=self.api_calls),
            cost_usd=self.cost_usd,
            cost_status=status,
            cost_label=label,
            unpriced_calls=self.unpriced_calls,
            last_prompt_tokens=self.last_prompt_tokens,
            latencies_s=tuple(self.latencies),
            output_sizes=tuple(self.output_sizes),
            rate_limits=dict(self.rate_limits),
            side_calls=dict(self.side_calls),
        )


def _delta(
    usage: CanonicalUsage | None, cost: CostResult, model: str, provider: str, base_url: str
) -> UsageDelta:
    estimated: float | None = None
    actual: float | None = None
    if cost.amount is not None:
        if cost.status == "actual":
            actual = float(cost.amount)
        else:
            estimated = float(cost.amount)
    tokens = usage or CanonicalUsage()
    return UsageDelta(
        input_tokens=tokens.input_tokens,
        output_tokens=tokens.output_tokens,
        cache_read_tokens=tokens.cache_read_tokens,
        cache_write_tokens=tokens.cache_write_tokens,
        reasoning_tokens=tokens.reasoning_tokens,
        api_calls=1,
        estimated_cost_usd=estimated,
        actual_cost_usd=actual,
        cost_status=cost.status,
        model=model or None,
        billing_provider=provider or None,
        billing_base_url=base_url or None,
    )


# -- the recording caller --------------------------------------------------------------------------


@dataclass
class RecordingCaller:
    """A ``ModelCaller`` that records every answered call into ``ledger``.

    ``route_of`` names the route the call went to (read when the call ends); ``task`` tags side
    calls made through a main-route caller (``compression`` when no auxiliary client is wired). A
    call that ended without an answer (route moved, raised) is not recorded here: there is no
    response to count.
    """

    inner: ModelCaller
    ledger: UsageLedger
    route_of: Callable[[], object]
    task: str = ""

    @property
    def model(self) -> str:
        return self.inner.model

    async def call(
        self, request: ChatRequest, *, on_event: OnEvent, on_wait: OnWait | None = None
    ) -> CallOutcome:
        clock = self.ledger.clock
        started = clock.monotonic()
        outcome = await self.inner.call(request, on_event=on_event, on_wait=on_wait)
        if isinstance(outcome, CallResult):
            response = outcome.response
            data = response.provider_data
            raw = data.get("usage") if isinstance(data.get("usage"), Mapping) else None
            tier = data.get("service_tier")
            if isinstance(tier, str):
                raw = {**cast(Mapping[str, Any], raw or {}), "service_tier": tier}
            ident = data.get("id")
            await self.ledger.record(
                response.usage,
                route=self.route_of(),
                latency_s=max(0.0, clock.monotonic() - started),
                task=self.task,
                response_id=ident if isinstance(ident, str) else "",
                raw=cast(Mapping[str, Any] | None, raw),
            )
        return outcome
