"""W48-g-1: the shipped price table is read from a generated JSON twin of the YAML source."""

from __future__ import annotations

import json
from importlib import resources

import pytest

from indus.foundation import yamlio
from indus.providers import pricing, pricing_twin


def _text(name: str) -> str:
    return resources.files("indus.providers").joinpath(name).read_text(encoding="utf-8")


def test_the_checked_in_twin_is_fresh() -> None:
    # Regenerate with `uv run python -m indus.providers.pricing_twin` after editing the YAML.
    assert _text(pricing_twin.TWIN_FILE) == pricing_twin.render(_text(pricing.TABLE_FILE))
    assert pricing_twin.main(["--check"]) == 0


def test_the_twin_holds_the_same_data_as_the_yaml() -> None:
    twin = json.loads(_text(pricing_twin.TWIN_FILE))
    assert twin["table"] == yamlio.load_safe(_text(pricing.TABLE_FILE))


def test_the_default_table_does_not_parse_yaml_when_the_twin_is_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pricing.default_table.cache_clear()
    calls: list[object] = []
    real = yamlio.load_safe
    monkeypatch.setattr(yamlio, "load_safe", lambda *a, **k: calls.append(a) or real(*a, **k))
    try:
        table = pricing.default_table()
    finally:
        pricing.default_table.cache_clear()
    assert calls == []
    assert table == pricing.load_table(_text(pricing.TABLE_FILE))


def test_a_stale_twin_falls_back_to_the_yaml_source() -> None:
    yaml_text = _text(pricing.TABLE_FILE)
    stale = json.loads(_text(pricing_twin.TWIN_FILE))
    stale["table"] = {"vendors": {}}
    edited = yaml_text + "\n# edited after the twin was written\n"
    assert pricing.table_from_twin(edited, json.dumps(stale)) is None
    assert pricing.table_from_twin(yaml_text, "not json") is None
    assert pricing.table_from_twin(yaml_text, _text(pricing_twin.TWIN_FILE)) == pricing.load_table(
        yaml_text
    )
