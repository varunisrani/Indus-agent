"""P04-10: tool definitions, aliasing, cross-references and the route-aware sanitiser."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.providers.route import RuntimeRoute
from indus.tools.definitions import ToolDefinitions, definitions
from indus.tools.registry import CrossRef, ToolRegistry
from indus.tools.schema_sanitize import (
    ALL_QUIRKS,
    GRAMMAR_MAX_LENGTH,
    Quirk,
    quirks_for,
    sanitize,
    sanitize_parameters,
)
from tests.fakes.clock import FakeClock
from tests.unit.tools._helpers import CountingProbe, echo, params, schema


@pytest.fixture
def reg(fake_clock: FakeClock) -> ToolRegistry:
    registry = ToolRegistry(clock=fake_clock)
    registry.register("zeta", "demo", schema("last", q={"type": "string"}), echo)
    registry.register("alpha", "demo", schema("first"), echo)
    registry.register("mid", "demo", schema("middle"), echo)
    return registry


def route(api_mode: str = "chat_completions", **kw: Any) -> RuntimeRoute:
    return RuntimeRoute(
        provider=kw.pop("provider", "openai"),
        model="m",
        api_mode=api_mode,
        base_url=kw.pop("base_url", "https://api.example.com/v1"),
        **kw,
    )


def names(tools: list[dict[str, Any]]) -> list[str]:
    return [t["function"]["name"] for t in tools]


# -- shape and filtering ------------------------------------------------------------------------


def test_sorted_function_entries(reg: ToolRegistry, scope: Scope) -> None:
    tools = definitions(reg, ["zeta", "alpha", "mid"], scope=scope)
    assert names(tools) == ["alpha", "mid", "zeta"]
    assert all(t["type"] == "function" for t in tools)
    assert tools[2]["function"]["description"] == "last"
    assert tools[2]["function"]["parameters"]["properties"] == {"q": {"type": "string"}}


def test_unknown_names_skipped(reg: ToolRegistry, scope: Scope) -> None:
    assert names(definitions(reg, ["alpha", "ghost"], scope=scope)) == ["alpha"]


def test_unavailable_tools_skipped_one_probe_per_pass(reg: ToolRegistry, scope: Scope) -> None:
    probe = CountingProbe(False)
    reg.register("g1", "gated", schema(), echo, probe=probe)
    reg.register("g2", "gated", schema(), echo, probe=probe)
    reg.invalidate_probes()
    builder = ToolDefinitions(reg)
    assert names(builder.build(["alpha", "g1", "g2"], scope=scope)) == ["alpha"]
    assert probe.calls == 1


def test_unscoped_build_drops_probed_tools(reg: ToolRegistry) -> None:
    reg.register("g", "gated", schema(), echo, probe=CountingProbe(True))
    assert names(ToolDefinitions(reg).build(["alpha", "g"])) == ["alpha"]


def test_schema_overrides_merged_and_failures_ignored(reg: ToolRegistry, scope: Scope) -> None:
    reg.register(
        "dyn",
        "demo",
        schema("static"),
        echo,
        schema_overrides=lambda s: {"description": f"for {s.profile_id if s else '-'}"},
    )

    def broken(s: Scope | None) -> dict[str, Any]:
        raise RuntimeError("config unreadable")

    reg.register("dyn2", "demo", schema("static two"), echo, schema_overrides=broken)
    tools = {t["function"]["name"]: t for t in definitions(reg, ["dyn", "dyn2"], scope=scope)}
    assert tools["dyn"]["function"]["description"] == "for work"
    assert tools["dyn2"]["function"]["description"] == "static two"


# -- aliasing (R01.13) --------------------------------------------------------------------------


def test_appending_to_a_returned_list_never_changes_the_next(
    reg: ToolRegistry, scope: Scope
) -> None:
    first = definitions(reg, ["alpha", "mid"], scope=scope)
    first.append({"type": "function", "function": {"name": "extra"}})
    first[0]["function"]["description"] = "edited"
    first[1]["type"] = "changed"
    second = definitions(reg, ["alpha", "mid"], scope=scope)
    assert names(second) == ["alpha", "mid"]
    assert second[0]["function"]["description"] == "first"
    assert second[1]["type"] == "function"
    assert second is not first and second[0] is not first[0]


def test_memo_hit_and_generation_invalidates(reg: ToolRegistry, scope: Scope) -> None:
    builder = ToolDefinitions(reg)
    builder.build(["alpha"], scope=scope)
    builder.build(["alpha"], scope=scope)
    assert len(builder) == 1
    reg.register("alpha", "demo", schema("renamed"), echo)
    assert builder.build(["alpha"], scope=scope)[0]["function"]["description"] == "renamed"


def test_probe_flip_shows_on_next_call(
    reg: ToolRegistry, scope: Scope, fake_clock: FakeClock
) -> None:
    probe = CountingProbe(True, False)
    reg.register("g", "gated", schema(), echo, probe=probe)
    builder = ToolDefinitions(reg)
    assert "g" in names(builder.build(["g"], scope=scope))
    fake_clock.advance(120)
    assert names(builder.build(["g"], scope=scope)) == []


# -- cross-references (R02.21) ------------------------------------------------------------------


def test_cross_reference_only_when_the_tool_survives(reg: ToolRegistry, scope: Scope) -> None:
    reg.register(
        "browse",
        "browser",
        schema("Open a page."),
        echo,
        cross_refs=(CrossRef("quick_search", "For quick lookups quick_search is cheaper."),),
    )
    reg.register("quick_search", "web", schema("Search."), echo, probe=CountingProbe(False))
    reg.invalidate_probes()
    alone = definitions(reg, ["browse", "quick_search"], scope=scope)
    assert names(alone) == ["browse"]
    assert alone[0]["function"]["description"] == "Open a page."
    assert "quick_search" not in json.dumps(alone)
    together = definitions(reg, ["browse", "alpha"], scope=scope)
    assert "quick_search" not in json.dumps(together)


def test_cross_reference_added_when_present(reg: ToolRegistry, scope: Scope) -> None:
    reg.register(
        "browse",
        "browser",
        schema("Open a page."),
        echo,
        cross_refs=(CrossRef("alpha", "alpha also helps."),),
    )
    out = definitions(reg, ["browse", "alpha"], scope=scope)
    assert out[1]["function"]["description"] == "Open a page. alpha also helps."


def test_rewrite_can_drop_or_change(reg: ToolRegistry, scope: Scope) -> None:
    reg.register(
        "hostexec",
        "browser",
        schema("Run code on the host."),
        echo,
        rewrite=lambda fn, present: fn if "terminal" in present else None,
    )
    assert names(definitions(reg, ["hostexec", "alpha"], scope=scope)) == ["alpha"]
    reg.register("terminal", "terminal", schema(), echo)
    assert "hostexec" in names(definitions(reg, ["hostexec", "terminal"], scope=scope))


def test_rewrite_that_raises_drops_the_tool(reg: ToolRegistry, scope: Scope) -> None:
    def broken(fn: dict[str, Any], present: frozenset[str]) -> dict[str, Any] | None:
        raise KeyError("x")

    reg.register("fragile", "demo", schema(), echo, rewrite=broken)
    assert names(definitions(reg, ["fragile", "alpha"], scope=scope)) == ["alpha"]


# -- golden stability ---------------------------------------------------------------------------


def test_output_is_byte_stable_across_runs(fake_clock: FakeClock, scope: Scope) -> None:
    def build() -> str:
        registry = ToolRegistry(clock=fake_clock)
        for name in ("b", "c", "a"):
            registry.register(
                name, "demo", schema(f"tool {name}", x={"type": ["string", "null"]}), echo
            )
        return json.dumps(definitions(registry, ["c", "a", "b"], scope=scope), sort_keys=False)

    first = build()
    assert first == build()
    assert first.index('"a"') < first.index('"b"') < first.index('"c"')


# -- route quirks -------------------------------------------------------------------------------


def test_quirks_per_route() -> None:
    assert quirks_for(None) == ALL_QUIRKS
    assert quirks_for(route()) == frozenset()
    assert Quirk.NULLABLE_UNIONS in quirks_for(route("anthropic_messages"))
    assert Quirk.PROPERTY_KEYS in quirks_for(route("anthropic_messages"))
    assert quirks_for(route("responses")) == {Quirk.TOP_COMBINATORS}
    assert quirks_for(route("gemini")) == {Quirk.TYPE_ARRAYS}
    local = quirks_for(route(base_url="http://127.0.0.1:8080/v1"))
    assert {Quirk.OBJECT_PROPERTIES, Quirk.TYPE_ARRAYS, Quirk.GRAMMAR_LENGTH} <= local
    assert Quirk.OBJECT_PROPERTIES in quirks_for(route(provider="custom"))
    assert Quirk.REF_DEFAULT in quirks_for(route(provider="fireworks"))
    assert Quirk.REF_DEFAULT in quirks_for(route(base_url="https://api.fireworks.ai/v1"))


def test_definitions_apply_only_the_routes_quirks(reg: ToolRegistry, scope: Scope) -> None:
    reg.register("typed", "demo", schema(x={"type": ["integer", "null"]}), echo)
    plain = definitions(reg, ["typed"], route(), scope=scope)
    assert plain[0]["function"]["parameters"]["properties"]["x"] == {"type": ["integer", "null"]}
    local = definitions(reg, ["typed"], route(base_url="http://localhost:1/v1"), scope=scope)
    assert local[0]["function"]["parameters"]["properties"]["x"] == {
        "type": "integer",
        "nullable": True,
    }


# -- sanitiser table ----------------------------------------------------------------------------


def clean(parameters: Any, *quirks: Quirk) -> dict[str, Any]:
    return sanitize_parameters(parameters, quirks or ALL_QUIRKS)


def test_object_without_properties() -> None:
    out = clean(params(meta={"type": "object"}), Quirk.OBJECT_PROPERTIES)
    assert out["properties"]["meta"] == {"type": "object", "properties": {}, "required": []}
    assert out["required"] == []
    untouched = clean(params(meta={"type": "object"}), Quirk.TYPE_ARRAYS)
    assert untouched["properties"]["meta"] == {"type": "object"}


def test_required_pruned_to_existing_properties() -> None:
    given = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a", "gone"]}
    assert clean(given, Quirk.OBJECT_PROPERTIES)["required"] == ["a"]


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (["string", "null"], {"type": "string", "nullable": True}),
        (["integer"], {"type": "integer"}),
        (["number", "string"], {"anyOf": [{"type": "number"}, {"type": "string"}]}),
        (["null"], {"type": "null"}),
        ([], {"type": "object"}),
    ],
)
def test_type_arrays(given: list[str], expected: dict[str, Any]) -> None:
    assert clean(params(x={"type": given}), Quirk.TYPE_ARRAYS)["properties"]["x"] == expected


def test_bare_string_schemas_always_fixed() -> None:
    out = clean(params(a="string", b="object", c="whatever"), Quirk.TYPE_ARRAYS)
    assert out["properties"] == {
        "a": {"type": "string"},
        "b": {"type": "object", "properties": {}},
        "c": {},
    }


def test_nullable_union_collapsed_with_meta_carried() -> None:
    given = params(
        x={
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "maybe text",
            "default": None,
        },
        y={"anyOf": [{"$ref": "#/$defs/T"}, {"type": "null"}], "default": "t"},
        z={"anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}]},
    )
    out = clean(given, Quirk.NULLABLE_UNIONS)["properties"]
    assert out["x"] == {
        "type": "string",
        "nullable": True,
        "description": "maybe text",
        "default": None,
    }
    assert out["y"] == {"$ref": "#/$defs/T", "nullable": True}
    assert "anyOf" in out["z"]  # two real branches: left alone


def test_default_beside_ref_dropped() -> None:
    given = params(x={"$ref": "#/$defs/T", "default": "a"}, y={"type": "string", "default": "b"})
    out = clean(given, Quirk.REF_DEFAULT)["properties"]
    assert out["x"] == {"$ref": "#/$defs/T"}
    assert out["y"]["default"] == "b"


def test_top_level_combinators_removed_nested_kept() -> None:
    given = {
        **params(x={"oneOf": [{"type": "string"}, {"type": "integer"}]}),
        "anyOf": [{"required": ["x"]}],
        "allOf": [{}],
        "enum": [1],
        "not": {},
    }
    out = clean(given, Quirk.TOP_COMBINATORS)
    assert not {"anyOf", "allOf", "oneOf", "enum", "not"} & set(out)
    assert "oneOf" in out["properties"]["x"]


def test_bad_property_keys_renamed_with_required() -> None:
    given = {
        "type": "object",
        "required": ["user name", "ok"],
        "properties": {
            "user name": {"type": "string"},
            "user_name": {"type": "string"},
            "ok": {"type": "integer"},
            "x" * 80: {"type": "string"},
        },
    }
    out = clean(given, Quirk.PROPERTY_KEYS)
    assert list(out["properties"]) == ["user_name_2", "user_name", "ok", "x" * 64]
    assert out["required"] == ["user_name_2", "ok"]


def test_legacy_boolean_required_lifted() -> None:
    given = params(a={"type": "string", "required": True}, b={"type": "string", "required": False})
    out = clean(given, Quirk.TYPE_ARRAYS)
    assert out["required"] == ["a"]
    assert "required" not in out["properties"]["a"]
    assert "required" not in out["properties"]["b"]


def test_literal_defaults_and_consts_never_treated_as_schemas() -> None:
    given = params(
        mode={"type": "object", "default": {"type": ["a", "b"], "required": True}},
        pick={"const": "string", "enum": ["object", "string"], "examples": ["array"]},
        deps={"type": "object", "dependentRequired": {"a": ["b"]}},
    )
    out = clean(given)["properties"]
    assert out["mode"]["default"] == {"type": ["a", "b"], "required": True}
    assert out["pick"] == {"const": "string", "enum": ["object", "string"], "examples": ["array"]}
    assert out["deps"]["dependentRequired"] == {"a": ["b"]}


def test_huge_max_length_removed_for_grammar_routes_only() -> None:
    given = params(
        long={"type": "string", "maxLength": GRAMMAR_MAX_LENGTH + 1},
        short={"type": "string", "maxLength": 40},
    )
    out = clean(given, Quirk.GRAMMAR_LENGTH)["properties"]
    assert "maxLength" not in out["long"] and out["short"]["maxLength"] == 40
    assert clean(given, Quirk.TYPE_ARRAYS)["properties"]["long"]["maxLength"] > GRAMMAR_MAX_LENGTH


@pytest.mark.parametrize("missing", [None, "text", ["list"]])
def test_missing_or_bad_parameters_become_empty_object(missing: Any) -> None:
    out = sanitize([{"type": "function", "function": {"name": "t", "parameters": missing}}])
    assert out[0]["function"]["parameters"] == {"type": "object", "properties": {}, "required": []}


def test_nested_children_walked() -> None:
    given = params(
        rows={"type": "array", "items": {"type": ["string", "null"]}},
        extra={"type": "object", "properties": {}, "additionalProperties": {"type": ["integer"]}},
    )
    out = clean(given, Quirk.TYPE_ARRAYS)["properties"]
    assert out["rows"]["items"] == {"type": "string", "nullable": True}
    assert out["extra"]["additionalProperties"] == {"type": "integer"}


def test_sanitize_never_mutates_input() -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "t",
                "parameters": params(
                    a={"type": ["string", "null"], "required": True},
                    b={"anyOf": [{"type": "string"}, {"type": "null"}]},
                ),
            },
        }
    ]
    before = copy.deepcopy(tools)
    out = sanitize(tools)
    assert tools == before
    assert out[0]["function"] is not tools[0]["function"]


def test_well_formed_schema_unchanged_under_all_quirks() -> None:
    given = {
        "type": "object",
        "properties": {"q": {"type": "string", "description": "query"}},
        "required": ["q"],
    }
    assert clean(given) == given


def test_schema_dependencies_walked_name_lists_kept() -> None:
    given = {
        **params(a={"type": "string"}),
        "dependencies": {"a": ["b"], "c": {"type": ["string", "null"]}},
    }
    out = clean(given, Quirk.TYPE_ARRAYS)
    assert out["dependencies"] == {"a": ["b"], "c": {"type": "string", "nullable": True}}
