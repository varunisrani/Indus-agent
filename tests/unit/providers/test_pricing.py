"""Cost from the price table: status rules, tiers, periods, rendering (P08-08)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from indus.providers.pricing import (
    PriceTableError,
    default_table,
    estimate_cost,
    format_cost,
    load_table,
)


@dataclass(frozen=True)
class Buckets:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    raw: Mapping[str, Any] | None = field(default=None)

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens


TABLE = load_table(
    """
version: 1
vendors:
  acme:
    hosts: [api.acme.example]
    source: https://acme.example/pricing
    checked: "2026-10-18"
    models:
      flat: {input: "2", output: "10", cache_read: "0.20"}
      tiered:
        {input: "1", output: "4", cache_read: "0.10",
         above: {tokens: 1000, input: "2", output: "8"}}
      writes: {input: "4", output: "20", cache_read: "0.40", cache_write: "5", cache_write_1h: "8",
               fast: {input: "8", output: "40", cache_read: "0.80", cache_write: "10"}}
      dated:
        {input: "1", output: "2",
         changes: [{from: "2026-12-01", input: "3"}]}
"""
)
VENDOR = "https://api.acme.example/v1"
ONE_MILLION_IN = Buckets(input_tokens=1_000_000)


def test_cost_status_table() -> None:
    usage = Buckets(input_tokens=1000, output_tokens=500)
    rows = {
        "vendor host": (VENDOR, "flat", "estimated"),
        "vendor model on a proxy": ("https://proxy.example/v1", "flat", "unknown"),
        "vendor host over http": ("http://api.acme.example/v1", "flat", "unknown"),
        "vendor host on another port": ("https://api.acme.example:8443/v1", "flat", "unknown"),
        "lookalike host": ("https://api.acme.example.evil.test/v1", "flat", "unknown"),
        "custom host": ("https://llm.internal.example/v1", "anything", "unknown"),
        "loopback server": ("http://127.0.0.1:1234/v1", "local-model", "included"),
        "model not in table": (VENDOR, "brand-new", "unknown"),
    }
    for name, (url, model, expected) in rows.items():
        result = estimate_cost(usage, model=model, base_url=url, table=TABLE)
        assert result.status == expected, name
        if expected == "unknown":
            assert result.amount is None, name


def test_aggregator_reported_cost_is_actual() -> None:
    reported = Buckets(input_tokens=10, raw={"cost": 0.0123})
    result = estimate_cost(reported, model="x", base_url="https://agg.example/api/v1", table=TABLE)
    assert (result.status, result.amount) == ("actual", Decimal("0.0123"))
    silent = Buckets(input_tokens=10, raw={"cost": "n/a"})
    other = estimate_cost(silent, model="x", base_url="https://agg.example/api/v1", table=TABLE)
    assert other.status == "unknown"


def test_estimate_arithmetic() -> None:
    usage = Buckets(input_tokens=1_000_000, output_tokens=100_000, cache_read_tokens=500_000)
    result = estimate_cost(usage, model="flat", base_url=VENDOR, table=TABLE)
    assert result.amount == Decimal("2") + Decimal("1") + Decimal("0.1")
    assert result.status == "estimated"
    assert result.checked == "2026-10-18"


def test_dated_model_id_falls_back_to_the_bare_row() -> None:
    a = estimate_cost(ONE_MILLION_IN, model="flat-20250929", base_url=VENDOR, table=TABLE)
    b = estimate_cost(ONE_MILLION_IN, model="flat-2024-08-06", base_url=VENDOR, table=TABLE)
    assert a.amount == b.amount == Decimal("2")


def test_tier_above_threshold_prices_the_whole_request() -> None:
    small = Buckets(input_tokens=1000, output_tokens=1000)
    big = Buckets(input_tokens=900, cache_read_tokens=101, output_tokens=1000)
    low = estimate_cost(small, model="tiered", base_url=VENDOR, table=TABLE)
    high = estimate_cost(big, model="tiered", base_url=VENDOR, table=TABLE)
    assert low.amount == Decimal(1000) * 1 / 1_000_000 + Decimal(1000) * 4 / 1_000_000
    # above the tier: input and output double, the cache read keeps its base rate (not listed)
    assert high.amount == (
        Decimal(900) * 2 / 1_000_000
        + Decimal(101) * Decimal("0.10") / 1_000_000
        + Decimal(1000) * 8 / 1_000_000
    )


def test_scheduled_change_applies_from_its_date() -> None:
    before = estimate_cost(
        ONE_MILLION_IN, model="dated", base_url=VENDOR, on=date(2026, 11, 30), table=TABLE
    )
    after = estimate_cost(
        ONE_MILLION_IN, model="dated", base_url=VENDOR, on=date(2026, 12, 1), table=TABLE
    )
    assert (before.amount, after.amount) == (Decimal("1"), Decimal("3"))


def test_missing_rate_for_used_bucket_is_unknown_never_zero() -> None:
    writes = Buckets(input_tokens=10, cache_write_tokens=10)
    result = estimate_cost(writes, model="flat", base_url=VENDOR, table=TABLE)
    assert result.status == "unknown"
    assert "cache write" in result.notes[0]


def test_one_hour_cache_writes_use_their_own_rate() -> None:
    raw = {"cache_creation": {"ephemeral_1h_input_tokens": 400_000}}
    usage = Buckets(cache_write_tokens=1_000_000, raw=raw)
    result = estimate_cost(usage, model="writes", base_url=VENDOR, table=TABLE)
    assert result.amount == Decimal("0.6") * 5 + Decimal("0.4") * 8


def test_fast_mode_and_served_tier() -> None:
    fast = Buckets(input_tokens=1_000_000, raw={"speed": "fast"})
    assert estimate_cost(fast, model="writes", base_url=VENDOR, table=TABLE).amount == Decimal(8)
    no_fast = estimate_cost(fast, model="flat", base_url=VENDOR, table=TABLE)
    assert no_fast.status == "unknown"


def test_served_tier_unknown_is_unknown_cost() -> None:
    for tier, expected in (("default", "estimated"), ("flex", "unknown"), ("priority", "unknown")):
        usage = Buckets(input_tokens=10, raw={"service_tier": tier})
        assert estimate_cost(usage, model="flat", base_url=VENDOR, table=TABLE).status == expected


@pytest.mark.parametrize(
    ("amount", "label"),
    [
        (None, "n/a"),
        (Decimal(0), "$0.00"),
        (Decimal("0.0046"), "$0.0046"),
        (Decimal("0.00004"), "<$0.0001"),
        (Decimal("0.00005"), "<$0.0001"),
        (Decimal("0.01"), "$0.01"),
        (Decimal("1.234"), "$1.23"),
    ],
)
def test_format_cost_table(amount: Decimal | None, label: str) -> None:
    assert format_cost(amount) == label


def test_sub_cent_never_renders_as_zero() -> None:
    for text in ("0.00001", "0.00009", "0.0001", "0.0099"):
        assert format_cost(Decimal(text)) not in ("$0.00", "$0.0000")


def test_bad_table_rows_are_refused() -> None:
    bad = (
        "vendors: {a: {hosts: [h.example], models: {m: {input: 1.5}}}}",
        'vendors: {a: {hosts: [h.example], models: {m: {input: "-1"}}}}',
        "vendors: {a: {hosts: [], models: {}}}",
        "vendors: {a: {hosts: [h.example], models: {m: {above: {tokens: 0}}}}}",
    )
    for text in bad:
        with pytest.raises(PriceTableError):
            load_table(text)


def test_shipped_table_loads_and_prices_vendor_hosts() -> None:
    table = default_table()
    names = {vendor.name for vendor in table.vendors}
    assert {"anthropic", "openai", "google"} <= names
    for vendor in table.vendors:
        assert vendor.source.startswith("https://") and vendor.checked
    usage = Buckets(input_tokens=1_000_000, output_tokens=1_000_000)
    claude = estimate_cost(usage, model="claude-opus-5-5", base_url="https://api.anthropic.com")
    assert (claude.status, claude.amount) == ("estimated", Decimal(24))
    gemini = estimate_cost(
        usage,
        model="gemini-2.5-pro",
        base_url="https://generativelanguage.googleapis.com/v1beta",
    )
    assert gemini.status == "estimated"
    # the same model id on an aggregator host is not list-priced
    proxied = estimate_cost(usage, model="gpt-5", base_url="https://openrouter.ai/api/v1")
    assert proxied.status == "unknown"
