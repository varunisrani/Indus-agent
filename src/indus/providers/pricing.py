"""What a model call cost, and how sure we are about it (P08-08).

The prices live in ``pricing_table.yaml`` next to this module: our own table, one block per vendor
with the vendor's price page and the day it was read. ``estimate_cost`` turns one call's token
buckets into a ``CostResult`` whose ``status`` never pretends:

- ``actual``: the provider itself reported what it billed (a numeric ``cost`` in the raw usage,
  as aggregators with usage accounting do);
- ``estimated``: the request went to a vendor's own host over HTTPS on port 443 and the table has
  every rate the usage needs;
- ``included``: a server on this machine (loopback) — nobody sends an invoice;
- ``unknown``: anything else (a proxy or custom host serving a vendor's model, a model or a served
  tier the table does not have, tokens in a bucket without a published rate).

The vendor is chosen by the request's host, never by the provider's name, so a key-compatible
proxy can never borrow a vendor's list price. ``format_cost`` renders an amount so that a cost
below a cent never reads as ``$0.00``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import cache
from importlib import resources
from typing import Any, Literal, Protocol

from indus.foundation import yamlio
from indus.providers.hosts import is_loopback, try_parse

log = logging.getLogger(__name__)

__all__ = [
    "CostResult",
    "CostStatus",
    "ModelPrice",
    "PriceTable",
    "Rates",
    "TokenBuckets",
    "VendorPrices",
    "default_table",
    "estimate_cost",
    "format_cost",
    "load_table",
]

type CostStatus = Literal["actual", "estimated", "included", "unknown"]

TABLE_FILE = "pricing_table.yaml"
MILLION = Decimal(1_000_000)
CENT = Decimal("0.01")
_STANDARD_TIERS = frozenset({"", "default", "standard", "auto"})
_DATE_SUFFIXES = (re.compile(r"-\d{8}$"), re.compile(r"-\d{4}-\d{2}-\d{2}$"))
_RATE_KEYS = ("input", "output", "cache_read", "cache_write", "cache_write_1h")

SOURCE_TABLE = "list price"
SOURCE_REPORTED = "provider-reported"
SOURCE_LOCAL = "local endpoint"
SOURCE_NONE = "no price"


class TokenBuckets(Protocol):
    """What pricing reads from a usage record (``core.usage.CanonicalUsage`` fits)."""

    @property
    def input_tokens(self) -> int: ...

    @property
    def output_tokens(self) -> int: ...

    @property
    def cache_read_tokens(self) -> int: ...

    @property
    def cache_write_tokens(self) -> int: ...

    @property
    def prompt_tokens(self) -> int: ...

    @property
    def raw(self) -> Mapping[str, Any] | None: ...


# -- the table ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Rates:
    """USD per million tokens; ``None`` = no published price for that bucket."""

    input: Decimal | None = None
    output: Decimal | None = None
    cache_read: Decimal | None = None
    cache_write: Decimal | None = None
    cache_write_1h: Decimal | None = None

    def merged(self, over: Rates) -> Rates:
        """``self`` with every rate ``over`` names replaced."""
        return Rates(
            *(
                getattr(over, key) if getattr(over, key) is not None else getattr(self, key)
                for key in _RATE_KEYS
            )
        )


@dataclass(frozen=True)
class ModelPrice:
    base: Rates
    above_tokens: int | None = None
    above: Rates | None = None
    fast: Rates | None = None
    changes: tuple[tuple[date, Rates], ...] = ()

    def on(self, day: date | None) -> ModelPrice:
        """The price in force on ``day`` (scheduled changes applied in date order)."""
        if day is None or not self.changes:
            return self
        base = self.base
        for start, rates in sorted(self.changes, key=lambda item: item[0]):
            if start <= day:
                base = base.merged(rates)
        return ModelPrice(base, self.above_tokens, self.above, self.fast)


@dataclass(frozen=True)
class VendorPrices:
    name: str
    hosts: frozenset[str]
    source: str
    checked: str
    models: Mapping[str, ModelPrice] = field(default_factory=dict[str, ModelPrice], repr=False)

    def lookup(self, model: str) -> ModelPrice | None:
        """Exact id first, then the id without a trailing date (``-20250929``, ``-2024-08-06``)."""
        name = model.strip().lower()
        found = self.models.get(name)
        if found is not None:
            return found
        for pattern in _DATE_SUFFIXES:
            bare = pattern.sub("", name)
            if bare != name and bare in self.models:
                return self.models[bare]
        return None


@dataclass(frozen=True)
class PriceTable:
    vendors: tuple[VendorPrices, ...] = ()

    def vendor_for(self, base_url: str) -> VendorPrices | None:
        """The vendor whose own host ``base_url`` names, over HTTPS on port 443; else ``None``."""
        parsed = try_parse(base_url)
        if parsed is None or parsed.scheme != "https" or parsed.effective_port != 443:
            return None
        for vendor in self.vendors:
            if parsed.host in vendor.hosts:
                return vendor
        return None


class PriceTableError(ValueError):
    """The price table data is malformed."""


def _decimal(value: object, where: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise PriceTableError(f"{where}: a rate must be a decimal string, got {value!r}")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise PriceTableError(f"{where}: {value!r} is not a number") from exc
    if not number.is_finite() or number < 0:
        raise PriceTableError(f"{where}: {value!r} is not a usable rate")
    return number


def _rates(raw: Mapping[str, Any], where: str) -> Rates:
    values = {key: _decimal(raw[key], f"{where}.{key}") for key in _RATE_KEYS if key in raw}
    return Rates(**values)


def _mapping(value: object, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PriceTableError(f"{where}: expected a mapping")
    return value  # pyright: ignore[reportUnknownVariableType]


def _model(raw: Mapping[str, Any], where: str) -> ModelPrice:
    above_tokens: int | None = None
    above: Rates | None = None
    if "above" in raw:
        tier = _mapping(raw["above"], f"{where}.above")
        tokens = tier.get("tokens")
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
            raise PriceTableError(f"{where}.above.tokens must be a positive integer")
        above_tokens, above = tokens, _rates(tier, f"{where}.above")
    fast = None
    if "fast" in raw:
        fast = _rates(_mapping(raw["fast"], f"{where}.fast"), f"{where}.fast")
    changes: list[tuple[date, Rates]] = []
    for index, change in enumerate(raw.get("changes") or ()):
        entry = _mapping(change, f"{where}.changes[{index}]")
        try:
            start = date.fromisoformat(str(entry.get("from")))
        except ValueError as exc:
            raise PriceTableError(f"{where}.changes[{index}].from is not a date") from exc
        changes.append((start, _rates(entry, f"{where}.changes[{index}]")))
    return ModelPrice(_rates(raw, where), above_tokens, above, fast, tuple(changes))


def load_table(text: str) -> PriceTable:
    """Parse price table YAML (see the file's header for the shape)."""
    data = _mapping(yamlio.load_safe(text), "table")
    vendors: list[VendorPrices] = []
    for name, raw_vendor in _mapping(data.get("vendors") or {}, "vendors").items():
        vendor = _mapping(raw_vendor, f"vendors.{name}")
        hosts = frozenset(str(h).strip().lower() for h in vendor.get("hosts") or ())
        if not hosts:
            raise PriceTableError(f"vendors.{name}: no hosts")
        models = {
            str(model).lower(): _model(_mapping(raw, f"{name}.{model}"), f"{name}.{model}")
            for model, raw in _mapping(vendor.get("models") or {}, f"{name}.models").items()
        }
        vendors.append(
            VendorPrices(
                name=str(name),
                hosts=hosts,
                source=str(vendor.get("source") or ""),
                checked=str(vendor.get("checked") or ""),
                models=models,
            )
        )
    return PriceTable(tuple(vendors))


@cache
def default_table() -> PriceTable:
    """The table shipped with the package (read once)."""
    text = resources.files("indus.providers").joinpath(TABLE_FILE).read_text(encoding="utf-8")
    return load_table(text)


# -- costing -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class CostResult:
    amount: Decimal | None
    status: CostStatus
    source: str
    notes: tuple[str, ...] = ()
    checked: str = ""

    @property
    def label(self) -> str:
        if self.status == "unknown":
            return "unknown"
        if self.status == "included":
            return "included"
        return format_cost(self.amount)


def format_cost(amount: Decimal | None) -> str:
    """``$1.23``; below a cent four decimals (``$0.0046``); a tiny positive amount ``<$0.0001``;
    only an exact zero is ``$0.00``; ``None`` is ``n/a``."""
    if amount is None:
        return "n/a"
    if amount == 0:
        return "$0.00"
    if amount < CENT:
        text = f"${amount:.4f}"
        return text if text != "$0.0000" else "<$0.0001"
    return f"${amount:.2f}"


def _unknown(source: str, *notes: str) -> CostResult:
    return CostResult(None, "unknown", source, tuple(notes))


def _reported(raw: Mapping[str, Any] | None) -> Decimal | None:
    if raw is None:
        return None
    value = raw.get("cost")
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def _served_tier(raw: Mapping[str, Any] | None) -> str:
    if raw is None:
        return ""
    tier = raw.get("service_tier")
    return tier.strip().lower() if isinstance(tier, str) else ""


def _one_hour_writes(raw: Mapping[str, Any] | None) -> int:
    """Anthropic splits cache writes by lifetime in ``cache_creation``; the 1-hour share."""
    if raw is None:
        return 0
    split = raw.get("cache_creation")
    if not isinstance(split, Mapping):
        return 0
    value = split.get("ephemeral_1h_input_tokens")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return value if isinstance(value, int) and value > 0 else 0


def estimate_cost(
    usage: TokenBuckets,
    *,
    model: str,
    base_url: str,
    on: date | None = None,
    table: PriceTable | None = None,
) -> CostResult:
    """The cost of one call (see the module notes for the status rules)."""
    raw = usage.raw
    reported = _reported(raw)
    if reported is not None:
        return CostResult(reported, "actual", SOURCE_REPORTED)
    if is_loopback(base_url):
        return CostResult(Decimal(0), "included", SOURCE_LOCAL)
    table = table if table is not None else default_table()
    vendor = table.vendor_for(base_url)
    if vendor is None:
        return _unknown(SOURCE_NONE, "the endpoint is not a vendor host with a price table")
    price = vendor.lookup(model)
    if price is None:
        return _unknown(SOURCE_NONE, f"{model} is not in the {vendor.name} price table")
    price = price.on(on)
    rates = price.base
    above = price.above
    if (
        above is not None
        and price.above_tokens is not None
        and (usage.prompt_tokens > price.above_tokens)
    ):
        rates = rates.merged(above)
    if raw is not None and raw.get("speed") == "fast":
        if price.fast is None:
            return _unknown(SOURCE_TABLE, "fast-mode prices are not in the table for this model")
        rates = rates.merged(price.fast)
    tier = _served_tier(raw)
    if tier not in _STANDARD_TIERS:
        return _unknown(SOURCE_TABLE, f"the served tier {tier!r} is not in the table")

    long_writes = min(_one_hour_writes(raw), usage.cache_write_tokens)
    buckets = (
        ("input", usage.input_tokens, rates.input),
        ("output", usage.output_tokens, rates.output),
        ("cache read", usage.cache_read_tokens, rates.cache_read),
        ("cache write", usage.cache_write_tokens - long_writes, rates.cache_write),
        ("one-hour cache write", long_writes, rates.cache_write_1h or rates.cache_write),
    )
    amount = Decimal(0)
    for name, tokens, rate in buckets:
        if not tokens:
            continue
        if rate is None:
            return _unknown(SOURCE_TABLE, f"no {name} price for {model}")
        amount += Decimal(tokens) * rate / MILLION
    return CostResult(amount, "estimated", SOURCE_TABLE, checked=vendor.checked)
