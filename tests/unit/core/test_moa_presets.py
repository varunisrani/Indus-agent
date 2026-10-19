"""MoA presets (P10-22): strict validation listing every problem, tolerant reading, fan-out."""

from __future__ import annotations

from typing import Any

import pytest

from indus.core import moa_presets as mp

KNOWN = {"openai", "anthropic", "openrouter"}


def known(name: str) -> bool:
    return name in KNOWN


def ref(provider: str = "openai", model: str = "m1", **extra: Any) -> dict[str, Any]:
    return {"provider": provider, "model": model, **extra}


def problems_of(name: str, raw: object, **values: Any) -> list[str]:
    return mp.validate_preset(name, raw, known_provider=known, **values)[1]


def test_a_good_preset() -> None:
    preset, problems = mp.validate_preset(
        "review",
        {
            "reference_models": [
                ref(),
                ref("anthropic", "m2", enabled=False, reasoning_effort="high"),
            ],
            "aggregator": ref("openrouter", "big"),
            "reference_timeout": 90,
            "fanout": "every_n:3",
        },
        known_provider=known,
    )
    assert problems == []
    assert preset == mp.Preset(
        "review",
        (mp.Slot("openai", "m1"), mp.Slot("anthropic", "m2", False, "high")),
        mp.Slot("openrouter", "big"),
        90.0,
        "every_n:3",
    )
    assert preset is not None and preset.enabled_references == (mp.Slot("openai", "m1"),)
    assert mp.Slot("anthropic", "m2", False, "high").label == "anthropic:m2 (reasoning high)"


def test_empty_preset() -> None:
    assert problems_of("p", {}) == ["preset 'p': no reference models (add at least one)"]
    assert problems_of("p", {"reference_models": []}) == [
        "preset 'p': no reference models (add at least one)"
    ]
    assert problems_of("p", {"reference_models": [ref(enabled=False)]}) == [
        "preset 'p': every reference model is disabled"
    ]


def test_unknown_provider() -> None:
    assert problems_of("p", {"reference_models": [ref("nowhere")]}) == [
        "preset 'p' reference 1: no provider is called 'nowhere'"
    ]


def test_nested_moa_slots_are_refused() -> None:
    raw = {"reference_models": [ref("moa", "x"), ref("other", "y")]}
    assert problems_of("p", raw, preset_names=["p", "other"]) == [
        "preset 'p' reference 1: 'moa' is a mixture of agents itself; presets cannot be nested",
        "preset 'p' reference 2: 'other' is a mixture of agents itself; presets cannot be nested",
    ]
    aggregated = {"reference_models": [ref()], "aggregator": ref("moa", "z")}
    assert problems_of("p", aggregated) == [
        "preset 'p' aggregator: 'moa' is a mixture of agents itself; presets cannot be nested"
    ]


def test_every_problem_is_listed_at_once() -> None:
    raw = {
        "reference_models": [ref("nowhere"), {"provider": "openai"}, "text", ref(enabled="maybe")],
        "aggregator": {"model": "x"},
        "reference_timeout": -1,
        "fanout": "sometimes",
    }
    assert problems_of("bad name!", raw) == [
        "preset 'bad name!': the name must be letters, digits, '-', '_' or '.'",
        "preset 'bad name!' reference 1: no provider is called 'nowhere'",
        "preset 'bad name!' reference 2: model is missing",
        "preset 'bad name!' reference 3: must be a mapping with provider and model",
        "preset 'bad name!' reference 4: enabled must be true or false",
        "preset 'bad name!' aggregator: provider is missing",
        "preset 'bad name!' reference_timeout: must be more than 0 seconds",
        "preset 'bad name!' fanout: must be per_iteration, user_turn or every_n:N (N at least 1)",
    ]


def test_section_validation_and_tolerant_reading() -> None:
    raw = {
        "default_preset": "gone",
        "reference_timeout": "soon",
        "privacy_filter": "loud",
        "presets": {"good": {"reference_models": [ref()]}, "broken": {"reference_models": []}},
    }
    settings, problems = mp.validate_section(raw, known_provider=known)
    assert problems == [
        "preset 'broken': no reference models (add at least one)",
        "moa.default_preset: no preset is called 'gone'",
        "moa.reference_timeout: must be a number of seconds",
        "moa.privacy_filter: must be empty, display or full",
    ]
    assert list(settings.presets) == ["good"] and settings.default_preset == ""
    tolerant = mp.read_settings(raw)
    assert list(tolerant.presets) == ["good"] and tolerant.problems == tuple(problems)
    assert mp.read_settings(None) == mp.MoaSettings()
    assert mp.read_settings({"presets": "nope"}).problems == (
        "moa.presets: must be a mapping of name → preset",
    )


def test_default_preset_choice_and_timeouts() -> None:
    one = mp.Preset("solo", (mp.Slot("openai", "m"),))
    two = mp.Preset("pair", (mp.Slot("openai", "m"),), reference_timeout=5.0)
    assert mp.MoaSettings(presets={"solo": one}).preset() is one
    both = mp.MoaSettings(presets={"solo": one, "pair": two}, reference_timeout=60.0)
    assert both.preset() is None and both.preset("pair") is two
    assert mp.MoaSettings(presets={"solo": one, "pair": two}, default_preset="pair").preset() is two
    assert both.timeout_for(one) == 60.0 and both.timeout_for(two) == 5.0


@pytest.mark.parametrize(
    ("fanout", "runs"),
    [
        ("per_iteration", [True, True, True, True]),
        ("user_turn", [True, False, False, False]),
        ("every_n:2", [True, False, True, False]),
        ("every_n:3", [True, False, False, True]),
    ],
)
def test_fanout(fanout: str, runs: list[bool]) -> None:
    assert [mp.fanout_runs(fanout, i) for i in range(1, 5)] == runs


def test_fanout_parsing() -> None:
    assert mp.parse_fanout(None) == ("per_iteration", None)
    assert mp.parse_fanout("every_n:1") == ("per_iteration", None)
    assert mp.parse_fanout("USER_TURN") == ("user_turn", None)
    assert mp.parse_fanout("every_n:0")[1] is not None


def test_slot_text_and_config_round_trip() -> None:
    assert mp.parse_slot_text("OpenRouter:vendor/model:free") == mp.Slot(
        "openrouter", "vendor/model:free"
    )
    assert mp.parse_slot_text("no-colon") is None and mp.parse_slot_text(":m") is None
    preset = mp.Preset("p", (mp.Slot("openai", "m"),), mp.Slot("anthropic", "a"), 30.0, "user_turn")
    again, problems = mp.validate_preset("p", mp.preset_to_config(preset), known_provider=known)
    assert problems == [] and again == preset


def test_privacy_modes() -> None:
    assert [mp.privacy_mode(v) for v in ("", "display", "FULL", True, None, "x")] == [
        "",
        "display",
        "full",
        "full",
        "",
        "",
    ]
