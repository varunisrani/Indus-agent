"""Trigger arithmetic (P08-02, R01.7)."""

from __future__ import annotations

import random

import pytest

from indus.core.compression.threshold import (
    CompressionSettings,
    effective_window,
    resolve_percent,
    trigger_tokens,
)
from indus.foundation.scope import Scope
from tests.fakes.agent import DictConfig

# (window, output reservation, percent, cap, aux window) -> trigger
TABLE = [
    # the 0.75 floor below 512K; the 64K minimum held to 85 % when it decides
    ((32_000, None, 0.50, None, None), 27_200),
    ((32_000, 8_000, 0.50, None, None), 20_400),
    ((64_000, None, 0.50, None, None), 54_400),
    ((64_000, 16_000, 0.50, None, None), 40_800),
    ((128_000, None, 0.50, None, None), 96_000),
    ((200_000, None, 0.50, None, None), 150_000),
    ((200_000, 32_000, 0.50, None, None), 126_000),
    ((200_000, 250_000, 0.50, None, None), 150_000),  # reservation leaves nothing: whole window
    ((400_000, None, 0.50, None, None), 300_000),
    ((1_000_000, None, 0.50, None, None), 500_000),
    ((1_000_000, 100_000, 0.50, None, None), 450_000),
    # overrides: above 0.85 stays (user intent); at the window → 85 %
    ((200_000, None, 0.90, None, None), 180_000),
    ((1_000_000, None, 0.90, None, None), 900_000),
    ((1_000_000, None, 1.00, None, None), 850_000),
    ((128_000, None, 0.60, None, None), 96_000),  # floor wins over a lower override
    ((1_000_000, None, 0.30, None, None), 300_000),
    # caps
    ((1_000_000, None, 0.50, 100_000, None), 100_000),
    ((1_000_000, None, 0.50, 0, None), 500_000),
    ((200_000, None, 0.50, 900_000, None), 150_000),
    ((200_000, None, 0.50, None, 128_000), 128_000),
    ((200_000, None, 0.50, None, 300_000), 150_000),
    ((64_000, None, 0.50, 50_000, 40_000), 40_000),
]


@pytest.mark.parametrize(("args", "expected"), TABLE)
def test_table(args: tuple[int, int | None, float, int | None, int | None], expected: int) -> None:
    window, reserve, percent, cap, aux = args
    assert trigger_tokens(window, reserve, percent, cap=cap, aux_window=aux) == expected


def test_docs_example_follows_code() -> None:
    # The developer guide says a 200K model triggers at 100K; the 0.75 floor makes it 150K.
    assert trigger_tokens(200_000, None, 0.50) == 150_000
    assert CompressionSettings().trigger(window=200_000, max_output=None, model="m") == 150_000


def test_effective_window() -> None:
    assert effective_window(200_000, None) == 200_000
    assert effective_window(200_000, 0) == 200_000
    assert effective_window(200_000, 50_000) == 150_000
    assert effective_window(200_000, 200_000) == 200_000
    assert trigger_tokens(0, None, 0.5) == 0


def test_model_override_resolution() -> None:
    overrides = {"gpt": 0.6, "gpt-5": 0.7, "openrouter:gpt-5": 0.8, "other:gpt-5-mini": 0.9}
    assert resolve_percent("gpt-5-mini", "openai", overrides, 0.5) == 0.7
    assert resolve_percent("GPT-5-mini", "OpenRouter", overrides, 0.5) == 0.8
    assert resolve_percent("gpt-4o", "openai", overrides, 0.5) == 0.6
    assert resolve_percent("claude-sonnet", "anthropic", overrides, 0.5) == 0.5
    assert resolve_percent("gpt-5", "x", {"gpt-5": 1.5, "gpt": 0.55}, 0.5) == 0.55
    assert resolve_percent("gpt-5", "x", None, 0.5) == 0.5


def test_property_trigger_below_window() -> None:
    rng = random.Random(1907)
    for _ in range(3_000):
        window = rng.randint(1_024, 2_000_000)
        reserve = rng.choice([None, 0, rng.randint(1, 300_000)])
        percent = rng.uniform(0.01, 1.0)
        cap = rng.choice([None, rng.randint(1, 3_000_000)])
        aux = rng.choice([None, rng.randint(1_024, 2_000_000)])
        trigger = trigger_tokens(window, reserve, percent, cap=cap, aux_window=aux)
        usable = effective_window(window, reserve)
        assert 1 <= trigger < usable, (window, reserve, percent, cap, aux, trigger)


def test_settings_from_scope(scope: Scope) -> None:
    config = DictConfig(
        {
            "compression": {
                "threshold": 0.6,
                "threshold_tokens": 90_000,
                "model_thresholds": {"big": 0.4, "bad": "x"},
                "max_attempts": 50,
                "timeout_s": 30,
                "tail_mode": "legacy",
                "in_place": False,
                "abort_on_summary_failure": True,
            },
            "context": {"engine": "lossless"},
        }
    )
    settings = CompressionSettings.from_scope(scope.with_config(config))
    assert settings.threshold == 0.6
    assert settings.threshold_tokens == 90_000
    assert dict(settings.model_thresholds) == {"big": 0.4}
    assert settings.max_attempts == 10
    assert settings.timeout_s == 30.0
    assert settings.tail_mode == "legacy"
    assert settings.in_place is False
    assert settings.abort_on_summary_failure is True
    assert settings.engine == "lossless"
    assert settings.trigger(window=1_000_000, max_output=None, model="big-model") == 90_000
    defaults = CompressionSettings.from_scope(scope.with_config(DictConfig({})))
    assert defaults == CompressionSettings()
