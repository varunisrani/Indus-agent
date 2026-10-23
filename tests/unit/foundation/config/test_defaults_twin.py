"""W50-f-1: config defaults from a generated twin, deferred section models, partial validation."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.config import defaults_twin, schema
from indus.foundation.config.loader import deep_merge


def test_the_checked_in_twin_is_fresh() -> None:
    # Regenerate with `uv run python -m indus.foundation.config.defaults_twin` after editing
    # schema.py or versions.py.
    assert defaults_twin.main(["--check"]) == 0


def test_the_twin_and_defaults_equal_the_models_type_for_type() -> None:
    built = schema.build_defaults()
    twin = json.loads(schema.DEFAULTS_TWIN.read_text(encoding="utf-8"))
    assert twin["source_sha256"] == schema.source_digest()
    assert schema.same_value(twin["defaults"], built)
    assert schema.same_value(dict(schema.DEFAULTS), built)
    assert list(schema.DEFAULTS) == list(built)


def test_the_digest_covers_the_schema_and_the_pinned_versions() -> None:
    names = [path.name for path in schema.defaults_sources()]
    assert names == ["schema.py", "versions.py"]


_PROBE = (
    "import json\n"
    "from indus.foundation.config import schema\n"
    "models = [schema.ConfigModel, *schema._Section.__subclasses__()]\n"
    "at_import = sorted(m.__name__ for m in models if m.__pydantic_complete__)\n"
    "warnings = schema.validate(dict(schema.DEFAULTS))\n"
    "after = sorted(m.__name__ for m in models if m.__pydantic_complete__)\n"
    "schema.validate({**schema.DEFAULTS, 'agent': {'max_turns': 3}})\n"
    "adapters = sorted(schema._ADAPTERS)\n"
    "print(json.dumps({'at_import': at_import, 'warnings': warnings, 'after': after,"
    " 'adapters': adapters, 'whole': schema.ConfigModel.__pydantic_complete__}))\n"
)


def test_importing_the_schema_builds_no_config_validator() -> None:
    # Before W50-f-1 the import built ConfigModel (and so every section) to make DEFAULTS.
    done = subprocess.run(
        [sys.executable, "-c", _PROBE], capture_output=True, text=True, timeout=120, check=True
    )
    seen = json.loads(done.stdout.strip().splitlines()[-1])
    assert seen["at_import"] == []
    assert seen["warnings"] == []
    assert seen["after"] == []  # the defaults are valid by construction: nothing to build
    # One changed setting builds a validator for that setting alone, never the whole model.
    assert seen["adapters"] == ["agent"]
    assert seen["whole"] is False


def _twin_text(defaults: object, digest: str) -> str:
    return json.dumps({"note": "x", "source_sha256": digest, "defaults": defaults})


def test_a_twin_for_other_sources_or_of_the_wrong_shape_is_not_used() -> None:
    digest = schema.source_digest()
    assert schema.defaults_from_twin(_twin_text({"a": 1}, digest), digest) == {"a": 1}
    assert schema.defaults_from_twin(_twin_text({"a": 1}, "0" * 64), digest) is None
    assert schema.defaults_from_twin(_twin_text(["a"], digest), digest) is None
    assert schema.defaults_from_twin("[1, 2]", digest) is None
    assert schema.defaults_from_twin("not json", digest) is None


@pytest.mark.parametrize("twin", [None, "not json", "stale"])
def test_a_missing_broken_or_stale_twin_builds_the_defaults_from_the_models(
    twin: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "defaults.json"
    if twin == "stale":
        path.write_text(_twin_text({"agent": {}}, "0" * 64), encoding="utf-8")
    elif twin is not None:
        path.write_text(twin, encoding="utf-8")
    monkeypatch.setattr(schema, "DEFAULTS_TWIN", path)
    assert schema.same_value(schema._load_defaults(), schema.build_defaults())  # pyright: ignore[reportPrivateUsage]


def test_render_refuses_a_default_json_cannot_carry() -> None:
    with pytest.raises(defaults_twin.TwinError):
        defaults_twin.render({"a": (1, 2)}, "0" * 64)


def test_same_value_tells_types_apart() -> None:
    assert schema.same_value({"a": [1, "x", None]}, {"a": [1, "x", None]})
    assert not schema.same_value(1, True)
    assert not schema.same_value(1, 1.0)
    assert not schema.same_value({"a": 1, "b": 2}, {"b": 2, "a": 1})  # order is part of it
    assert not schema.same_value([1], (1,))


_CASES: list[dict[str, Any]] = [
    {},
    {"model": "test-model"},
    {"model": {"default": "x", "provider": "y"}},
    {"model": {"default": "x", "bogus": 1}},
    {"model": 5},
    {"agent": {"max_turns": "lots"}, "approvals": {"mode": "maybe"}},
    {"agent": {"zzz": 1}, "display": {"yyy": 2}},
    {"agent": {"zzz": 1}, "gateway": {"api_server": {"port": 70000}}},
    {"hooks_auto_accept": 0},
    {"hooks_auto_accept": "sometimes"},
    {"file_read_max_chars": True},
    {"file_read_max_chars": 1.0},
    {"file_read_max_chars": "many"},
    {"_config_version": "x"},
    {"config_version": 2},
    {"config_version": "x", "agent": {"max_turns": "lots"}},
    {"unknown_top": {"a": 1}},
    {"providers": {"x": "not a mapping"}},
    {"command_allowlist": "ls"},
    {"platform_toolsets": {"cli": "web"}},
    {"terminal": {"modal_mode": "nope"}},
    {"terminal": {"background_memory_max": ["x"]}},
    {"dashboard": {"trusted_proxies": ["10.0.0.1", "0.0.0.0/0"]}},
    {"gateway": {"bot_loop_guard": {"window_seconds": 0}}, "pm": {"timeout_s": 0}},
    {"timezone": None},
]


@pytest.mark.parametrize("case", _CASES)
def test_validate_matches_the_whole_model_check(case: dict[str, Any]) -> None:
    whole = schema._validate_whole  # pyright: ignore[reportPrivateUsage]
    merged = deep_merge(schema.defaults_tree(), case)
    assert schema.validate(case) == whole(case)
    assert schema.validate(merged) == whole(merged)


def test_the_cases_cover_errors_extras_and_clean_input() -> None:
    whole = schema._validate_whole  # pyright: ignore[reportPrivateUsage]
    results = [whole(case) for case in _CASES]
    assert any(r == [] for r in results)
    assert any(any("unknown setting" in w for w in r) for r in results)
    assert any(len(r) > 1 and not any("unknown setting" in w for w in r) for r in results)
