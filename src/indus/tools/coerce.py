"""Repair argument types a model got wrong, guided by the tool's parameter schema.

Only certain repairs are made; anything ambiguous keeps the model's value and the handler decides.

- integer: a whole-number string (``"42"``, ``"-3"``, ``"4.0"``) → ``int``; ``"4.5"`` stays.
- number: a JSON-number-shaped string → ``int`` or ``float``; ``"inf"``, ``"nan"``, ``"1_000"``
  stay.
- boolean: ``"true"`` / ``"false"`` in any case → ``bool``; ``"1"``, ``"yes"`` stay.
- array / object: JSON text of that kind is parsed, then nested JSON text is parsed wherever the
  schema expects a container.
- array: a bare scalar becomes a one-item list; a string that starts like a JSON array but does not
  parse stays as it is (ambiguous).
- ``"null"`` becomes ``None`` only where the schema allows null.
- local ``$ref``s are followed; keys renamed by the schema sanitiser are mapped back first.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from typing import Any, cast

from indus.tools.schema_sanitize import resolve_ref, restore_arg_keys

log = logging.getLogger(__name__)

_INT_TEXT = re.compile(r"-?(?:0|[1-9][0-9]*)")
_NUMBER_TEXT = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_NO_CHANGE = object()


def _schema_types(schema: Mapping[str, Any]) -> list[str]:
    declared = schema.get("type")
    if isinstance(declared, str):
        return [declared]
    if isinstance(declared, list):
        return [t for t in cast(list[Any], declared) if isinstance(t, str)]
    return []


def allows_null(schema: Mapping[str, Any]) -> bool:
    if "null" in _schema_types(schema) or schema.get("nullable") is True:
        return True
    for key in ("anyOf", "oneOf"):
        branches = schema.get(key)
        if isinstance(branches, list) and any(
            isinstance(b, Mapping) and cast(Mapping[str, Any], b).get("type") == "null"
            for b in cast(list[Any], branches)
        ):
            return True
    return False


def accepts(schema: Any, kind: str, root: Any = None, _seen: frozenset[int] = frozenset()) -> bool:
    """Does ``schema`` admit JSON type ``kind`` directly or through any union branch?"""
    if not isinstance(schema, Mapping):
        return False
    mapping = cast(Mapping[str, Any], schema)
    if id(mapping) in _seen:
        return False
    seen = _seen | {id(mapping)}
    top: Any = mapping if root is None else root
    node = cast(Mapping[str, Any], resolve_ref(mapping, top))
    if kind in _schema_types(node):
        return True
    for key in ("anyOf", "oneOf", "allOf"):
        branches = node.get(key)
        if isinstance(branches, list) and any(
            accepts(b, kind, top, seen) for b in cast(list[Any], branches)
        ):
            return True
    return False


def _to_number(text: str, *, whole_only: bool) -> Any:
    stripped = text.strip()
    if _INT_TEXT.fullmatch(stripped):
        return int(stripped)
    if not _NUMBER_TEXT.fullmatch(stripped):
        return _NO_CHANGE
    value = float(stripped)
    if value != value or value in (float("inf"), float("-inf")):
        return _NO_CHANGE
    if value.is_integer():
        return int(value)
    return _NO_CHANGE if whole_only else value


def _to_bool(text: str) -> Any:
    lowered = text.strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return _NO_CHANGE


def _to_json(text: str, want: type) -> Any:
    try:
        parsed = json.loads(text)
    except ValueError:
        return _NO_CHANGE
    return parsed if isinstance(parsed, want) else _NO_CHANGE


def _scalar(text: str, kind: str) -> Any:
    if kind == "integer":
        return _to_number(text, whole_only=True)
    if kind == "number":
        return _to_number(text, whole_only=False)
    if kind == "boolean":
        return _to_bool(text)
    if kind == "array":
        return _to_json(text, list)
    if kind == "object":
        return _to_json(text, dict)
    return _NO_CHANGE


def _nested(value: Any, schema: Any, root: Any) -> Any:
    """Parse JSON text where the schema expects a container, then walk into containers."""
    same: Any = value
    if not isinstance(schema, Mapping):
        return same
    node = cast(Mapping[str, Any], resolve_ref(schema, root))
    if isinstance(value, str):
        head = value.strip()[:1]
        wants_list = head == "[" and accepts(node, "array", root)
        wants_map = head == "{" and accepts(node, "object", root)
        if not (wants_list or wants_map) or accepts(node, "string", root):
            return same
        parsed = _to_json(value, list if wants_list else dict)
        if parsed is _NO_CHANGE:
            return same
        value = same = parsed
    if isinstance(value, list):
        items = node.get("items")
        if not isinstance(items, Mapping):
            return same
        listed = cast(list[Any], value)
        walked = [_nested(item, items, root) for item in listed]
        return walked if any(a is not b for a, b in zip(walked, listed, strict=True)) else same
    if isinstance(value, dict):
        props = node.get("properties")
        if not isinstance(props, Mapping):
            return same
        mapping = cast(dict[str, Any], value)
        prop_map = cast(Mapping[str, Any], props)
        walked_map = {
            k: _nested(v, prop_map[k], root) if k in prop_map else v for k, v in mapping.items()
        }
        changed = any(walked_map[k] is not v for k, v in mapping.items())
        return walked_map if changed else same
    return same


def _coerce_one(value: Any, schema: Mapping[str, Any], root: Mapping[str, Any]) -> Any:
    kinds = _schema_types(schema)
    if value is None:
        return value
    if isinstance(value, str):
        if allows_null(schema) and value.strip().lower() == "null":
            return None
        if "string" in kinds:
            return value  # already valid as text: any repair would be a guess
        for kind in kinds:
            repaired = _scalar(value, kind)
            if repaired is not _NO_CHANGE:
                return _nested(repaired, schema, root)
        if kinds == ["array"]:
            if value.strip().startswith("["):
                log.info("argument looks like a broken JSON array; left as text")
                return value
            return [value]
        return _nested(value, schema, root) if not kinds else value
    if isinstance(value, (list, dict)):
        return _nested(value, schema, root)
    if kinds == ["array"]:
        return [value]
    return value


def coerce_args(
    args: Mapping[str, Any] | None, parameters: Mapping[str, Any] | None
) -> dict[str, Any]:
    """A new argument dict with schema-certain type repairs applied; the input is not changed."""
    if not args:
        return dict(args or {})
    if not isinstance(parameters, Mapping):
        return dict(args)
    props = parameters.get("properties")
    if not isinstance(props, Mapping) or not props:
        return dict(args)
    prop_map = cast(Mapping[str, Any], props)
    restored = restore_arg_keys(parameters, args)
    out: dict[str, Any] = {}
    for key, value in restored.items():
        schema = prop_map.get(key)
        if not isinstance(schema, Mapping):
            out[key] = value
            continue
        resolved = resolve_ref(schema, parameters)
        out[key] = _coerce_one(value, cast(Mapping[str, Any], resolved), parameters)
    return out
