"""P04-09: schema-guided argument coercion."""

from __future__ import annotations

import json
import random
from typing import Any

import pytest

from indus.tools.coerce import accepts, allows_null, coerce_args
from indus.tools.schema_sanitize import property_key_renames, resolve_ref
from tests.unit.tools._helpers import params

INT = {"type": "integer"}
NUM = {"type": "number"}
BOOL = {"type": "boolean"}
STR = {"type": "string"}
ARR = {"type": "array", "items": STR}
OBJ = {"type": "object", "properties": {"n": INT}}


@pytest.mark.parametrize(
    ("prop", "given", "expected"),
    [
        (INT, "42", 42),
        (INT, "-7", -7),
        (INT, " 12 ", 12),
        (INT, "4.0", 4),
        (INT, "4.5", "4.5"),
        (INT, "12345678901234567890", 12345678901234567890),
        (INT, "1_000", "1_000"),
        (INT, "0x10", "0x10"),
        (INT, "seven", "seven"),
        (NUM, "4.5", 4.5),
        (NUM, "3", 3),
        (NUM, "1e3", 1000),
        (NUM, "inf", "inf"),
        (NUM, "nan", "nan"),
        (NUM, "-infinity", "-infinity"),
        (BOOL, "true", True),
        (BOOL, "FALSE", False),
        (BOOL, " True ", True),
        (BOOL, "1", "1"),
        (BOOL, "yes", "yes"),
        (STR, "42", "42"),
        (STR, "true", "true"),
        (STR, '["a"]', '["a"]'),
        (ARR, '["a", "b"]', ["a", "b"]),
        (ARR, "solo", ["solo"]),
        (ARR, '["broken', '["broken'),
        (ARR, '{"not": "a list"}', ['{"not": "a list"}']),
        ({"type": "array"}, 5, [5]),
        ({"type": "array"}, True, [True]),
        (ARR, None, None),
        (OBJ, '{"n": 1}', {"n": 1}),
        (OBJ, "[1]", "[1]"),
        (OBJ, "{bad json", "{bad json"),
        (INT, 42, 42),
        (BOOL, False, False),
    ],
)
def test_coercion_table(prop: dict[str, Any], given: Any, expected: Any) -> None:
    out = coerce_args({"x": given}, params(x=prop))
    assert out == {"x": expected}
    assert type(out["x"]) is type(expected)


@pytest.mark.parametrize(
    ("prop", "given", "expected"),
    [
        ({"type": ["integer", "null"]}, "null", None),
        ({"type": "integer", "nullable": True}, "NULL", None),
        ({"anyOf": [INT, {"type": "null"}]}, "null", None),
        (INT, "null", "null"),
        ({"type": ["array", "null"], "items": STR}, "null", None),
        ({"type": ["string", "integer"]}, "42", "42"),
        ({"type": ["integer", "boolean"]}, "true", True),
    ],
)
def test_null_and_type_lists(prop: dict[str, Any], given: Any, expected: Any) -> None:
    assert coerce_args({"x": given}, params(x=prop)) == {"x": expected}


def test_nested_json_strings_parsed_where_containers_expected() -> None:
    schema = params(
        items={
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"tags": {"type": "array", "items": STR}, "note": STR},
            },
        }
    )
    given = {"items": json.dumps([{"tags": '["a"]', "note": '["keep"]'}])}
    assert coerce_args(given, schema) == {"items": [{"tags": ["a"], "note": '["keep"]'}]}


def test_native_containers_normalised_but_identity_kept_when_clean() -> None:
    schema = params(rows={"type": "array", "items": {"type": "array", "items": INT}})
    clean = [[1, 2]]
    assert coerce_args({"rows": clean}, schema)["rows"] is clean
    assert coerce_args({"rows": ["[3]", [4]]}, schema) == {"rows": [[3], [4]]}


def test_unknown_keys_and_schemaless_tools_untouched() -> None:
    assert coerce_args({"other": "42"}, params(x=INT)) == {"other": "42"}
    assert coerce_args({"x": "42"}, None) == {"x": "42"}
    assert coerce_args({"x": "42"}, {"type": "object"}) == {"x": "42"}
    assert coerce_args({}, params(x=INT)) == {}
    assert coerce_args(None, params(x=INT)) == {}


def test_input_is_not_mutated() -> None:
    given = {"x": "42", "y": '["a"]'}
    coerce_args(given, params(x=INT, y=ARR))
    assert given == {"x": "42", "y": '["a"]'}


def test_refs_followed_for_scalars_and_containers() -> None:
    schema = {
        "type": "object",
        "$defs": {"Count": INT, "Point": {"type": "object", "properties": {"x": NUM}}},
        "properties": {
            "count": {"$ref": "#/$defs/Count"},
            "point": {"$ref": "#/$defs/Point"},
            "points": {"type": "array", "items": {"$ref": "#/$defs/Point"}},
        },
    }
    out = coerce_args({"count": "3", "point": '{"x": 1.5}', "points": ['{"x": 2}']}, schema)
    assert out == {"count": 3, "point": {"x": 1.5}, "points": [{"x": 2}]}


def test_ref_pointer_forms() -> None:
    root = {
        "a/b": {"type": "integer"},
        "t~n": {"type": "boolean"},
        "list": [{"type": "string"}, {"type": "number"}],
        "space key": {"type": "integer"},
    }
    assert resolve_ref({"$ref": "#/a~1b"}, root) == INT
    assert resolve_ref({"$ref": "#/t~0n"}, root) == BOOL
    assert resolve_ref({"$ref": "#/list/1"}, root) == NUM
    assert resolve_ref({"$ref": "#/space%20key"}, root) == INT
    assert resolve_ref({"$ref": "#/list/01"}, root) == {"$ref": "#/list/01"}
    assert resolve_ref({"$ref": "#/list/9"}, root) == {"$ref": "#/list/9"}
    assert resolve_ref({"$ref": "#/bad~2escape"}, root) == {"$ref": "#/bad~2escape"}
    assert resolve_ref({"$ref": "#Anchor"}, root) == {"$ref": "#Anchor"}
    assert resolve_ref({"$ref": "other.json#/x"}, root) == {"$ref": "other.json#/x"}
    assert resolve_ref({"$ref": "#/a~1b", "description": "kept"}, root) == {
        "type": "integer",
        "description": "kept",
    }


def test_cyclic_and_dangling_refs_left_alone() -> None:
    root: dict[str, Any] = {"$defs": {"A": {"$ref": "#/$defs/B"}, "B": {"$ref": "#/$defs/A"}}}
    assert resolve_ref({"$ref": "#/$defs/A"}, root) == {"$ref": "#/$defs/A"}
    schema = params(x={"$ref": "#/$defs/Missing"})
    assert coerce_args({"x": "5"}, schema) == {"x": "5"}


def test_recursive_union_keeps_other_branches() -> None:
    node: dict[str, Any] = {"anyOf": [{"$ref": "#/$defs/Node"}, {"type": "array"}]}
    root = {"$defs": {"Node": node}}
    assert accepts(node, "array", root)
    assert not accepts(node, "object", root)


def test_renamed_keys_mapped_back() -> None:
    schema = params(**{"user name": STR, "max count": INT, "ok_key": INT})
    renamed = property_key_renames(schema["properties"])
    assert renamed == {"user name": "user_name", "max count": "max_count"}
    out = coerce_args({"user_name": "a", "max_count": "3", "ok_key": "1"}, schema)
    assert out == {"user name": "a", "max count": 3, "ok_key": 1}


def test_allows_null_variants() -> None:
    assert allows_null({"type": "null"})
    assert allows_null({"oneOf": [STR, {"type": "null"}]})
    assert not allows_null(STR)


def test_seeded_random_values_already_typed_never_change() -> None:
    # Property check written as a seeded table (hypothesis is not installed): any value that
    # already has the schema's type comes back equal and of the same type.
    rng = random.Random(4108)
    makers: dict[str, Any] = {
        "integer": lambda: rng.randint(-(10**12), 10**12),
        "number": lambda: rng.uniform(-1e6, 1e6),
        "boolean": lambda: rng.random() < 0.5,
        "string": lambda: "".join(rng.choice("ab1{[ \"'") for _ in range(rng.randint(0, 12))),
        "array": lambda: [rng.randint(0, 9) for _ in range(rng.randint(0, 4))],
    }
    for _ in range(400):
        kind = rng.choice(sorted(makers))
        value = makers[kind]()
        prop: dict[str, Any] = {"type": kind}
        if kind == "array":
            prop["items"] = INT
        out = coerce_args({"v": value}, params(v=prop))
        assert out["v"] == value and type(out["v"]) is type(value), (kind, value)


def test_seeded_random_integer_text_round_trips() -> None:
    rng = random.Random(9)
    for _ in range(300):
        number = rng.randint(-(10**18), 10**18)
        assert coerce_args({"n": str(number)}, params(n=INT)) == {"n": number}


def test_union_that_admits_text_keeps_json_looking_text() -> None:
    either = {"anyOf": [STR, {"type": "array", "items": STR}]}
    assert coerce_args({"x": '["a"]'}, params(x=either)) == {"x": '["a"]'}
    only_list = {"anyOf": [{"type": "array", "items": STR}, {"type": "null"}]}
    assert coerce_args({"x": '["a"]'}, params(x=only_list)) == {"x": ["a"]}
