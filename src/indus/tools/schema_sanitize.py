"""Route-aware clean-up of tool parameter schemas before they go to a provider.

Providers and local grammar engines each reject a few JSON-Schema shapes. ``sanitize`` works on a
deep copy and fixes only the shapes the target route is known to refuse (``quirks_for``); with no
route every fix applies. Malformed shapes that no provider accepts (bare-string schemas, legacy
boolean ``required`` flags, missing parameters) are always fixed.

Literal data (``default``, ``const``, ``enum``, ``examples``, ``dependentRequired``) is copied,
never walked as a schema. Property keys a route refuses are renamed deterministically
(``property_key_renames``); ``restore_arg_keys`` maps model arguments back to the registered names.

``resolve_ref`` follows a document-local ``$ref`` (a JSON pointer, percent-decoded, ``~0``/``~1``
escapes, through arrays); remote, malformed, dangling or cyclic refs come back unchanged.
"""

from __future__ import annotations

import copy
import logging
import re
from collections.abc import Callable, Iterable, Mapping
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import unquote

if TYPE_CHECKING:
    from indus.providers.route import RuntimeRoute

log = logging.getLogger(__name__)


class Quirk(StrEnum):
    OBJECT_PROPERTIES = "object_properties"  # object nodes need properties + a required list
    TYPE_ARRAYS = "type_arrays"  # ``type: [a, b]`` refused
    NULLABLE_UNIONS = "nullable_unions"  # anyOf/oneOf with a null branch refused
    REF_DEFAULT = "ref_default"  # ``default`` beside ``$ref`` refused
    TOP_COMBINATORS = "top_combinators"  # allOf/anyOf/oneOf/enum/not at the top refused
    PROPERTY_KEYS = "property_keys"  # keys outside [A-Za-z0-9_.-]{1,64} refused
    GRAMMAR_LENGTH = "grammar_length"  # huge maxLength explodes grammar converters


ALL_QUIRKS: frozenset[Quirk] = frozenset(Quirk)

# Above this a grammar converter unrolls the repetition into an unusable grammar; the handler
# enforces the real limit instead.
GRAMMAR_MAX_LENGTH = 1000

_GOOD_KEY = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_BAD_KEY_CHARS = re.compile(r"[^A-Za-z0-9_.-]")
_TYPE_NAMES = frozenset({"object", "string", "number", "integer", "boolean", "array", "null"})
_MAP_KEYS = frozenset(
    {"properties", "$defs", "definitions", "patternProperties", "dependentSchemas"}
)
_CHILD_KEYS = frozenset(
    {
        "items",
        "additionalItems",
        "additionalProperties",
        "unevaluatedItems",
        "unevaluatedProperties",
        "contains",
        "propertyNames",
        "not",
        "if",
        "then",
        "else",
        "anyOf",
        "oneOf",
        "allOf",
        "prefixItems",
    }
)
_TOP_COMBINATORS = ("allOf", "anyOf", "oneOf", "enum", "not")
_UNIONS = ("anyOf", "oneOf")
_CARRIED_META = ("title", "description", "default", "examples")


def quirks_for(route: RuntimeRoute | None) -> frozenset[Quirk]:
    """The fixes a route needs; ``None`` (unknown route) gets all of them."""
    if route is None:
        return ALL_QUIRKS
    found: set[Quirk] = set()
    profile = route.profile
    custom = route.provider == "custom" or (profile is not None and profile.family == "custom")
    try:
        host = route.host.lower()
        local = route.is_local
    except Exception:
        host, local = "", False
    if local or custom:
        found |= {Quirk.OBJECT_PROPERTIES, Quirk.TYPE_ARRAYS, Quirk.GRAMMAR_LENGTH}
    if route.api_mode == "gemini":
        found.add(Quirk.TYPE_ARRAYS)
    if route.api_mode in {"anthropic_messages", "bedrock"}:
        found |= {Quirk.NULLABLE_UNIONS, Quirk.PROPERTY_KEYS}
    if route.api_mode == "responses":
        found.add(Quirk.TOP_COMBINATORS)
    if "fireworks" in route.provider.lower() or "fireworks" in host:
        found.add(Quirk.REF_DEFAULT)
    return frozenset(found)


# -- property keys ------------------------------------------------------------------------------


def safe_key(key: str) -> str:
    return _BAD_KEY_CHARS.sub("_", key)[:64] or "param"


def property_key_renames(properties: Mapping[str, Any]) -> dict[str, str]:
    """``{bad key: new key}`` for one properties map; good keys are absent. Deterministic: same
    map in, same renames out (collisions get ``_2``, ``_3``… in insertion order)."""
    taken = {k for k in properties if _GOOD_KEY.fullmatch(k)}
    renames: dict[str, str] = {}
    for key in properties:
        if key in taken:
            continue
        base = safe_key(key)
        candidate, n = base, 2
        while candidate in taken:
            suffix = f"_{n}"
            candidate, n = base[: 64 - len(suffix)] + suffix, n + 1
        taken.add(candidate)
        renames[key] = candidate
    return renames


def _restore(schema: Any, value: Any, top: Mapping[str, Any]) -> Any:
    """``value`` with renamed keys mapped back; the same object when nothing changed."""
    same: Any = value
    node = resolve_ref(schema, top)
    if not isinstance(node, Mapping):
        return same
    shape = cast(Mapping[str, Any], node)
    if isinstance(value, list):
        items = shape.get("items")
        listed = cast(list[Any], value)
        if not isinstance(items, Mapping):
            return same
        walked = [_restore(items, v, top) for v in listed]
        return walked if any(a is not b for a, b in zip(walked, listed, strict=True)) else same
    props = shape.get("properties")
    if not isinstance(value, Mapping) or not isinstance(props, Mapping):
        return same
    props_map = cast(Mapping[str, Any], props)
    mapping = cast(Mapping[str, Any], value)
    back = {new: old for old, new in property_key_renames(props_map).items()}
    out: dict[str, Any] = {}
    changed = False
    for key, item in mapping.items():
        original = back.get(key, key)
        sub = props_map.get(original)
        restored = _restore(sub, item, top) if isinstance(sub, Mapping) else item
        changed = changed or original != key or restored is not item
        out[original] = restored
    return out if changed else same


def restore_arg_keys(
    parameters: Mapping[str, Any], args: Mapping[str, Any], root: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Map renamed keys in model arguments back to the registered names (recursively)."""
    top = parameters if root is None else root
    return dict(cast(Mapping[str, Any], _restore(parameters, args, top)))


# -- $ref ---------------------------------------------------------------------------------------

_ARRAY_INDEX = re.compile(r"0|[1-9][0-9]*")
_BAD_ESCAPE = re.compile(r"~(?![01])")


def resolve_ref(node: Any, root: Any, _seen: frozenset[str] = frozenset()) -> Any:
    """Follow a local ``$ref`` in ``node`` against ``root``; siblings of the ref are kept where
    the target does not set them."""
    same: Any = node
    if not isinstance(node, Mapping) or not isinstance(root, Mapping):
        return same
    schema = cast(Mapping[str, Any], node)
    ref = schema.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#") or ref in _seen:
        return same
    pointer = unquote(ref[1:])
    if pointer and not pointer.startswith("/"):
        return same
    target = cast(Any, root)
    for raw in pointer.split("/")[1:]:
        if _BAD_ESCAPE.search(raw):
            return same
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(target, Mapping) and part in target:
            target = cast(Mapping[str, Any], target)[part]
        elif isinstance(target, list) and _ARRAY_INDEX.fullmatch(part):
            items = cast(list[Any], target)
            if int(part) >= len(items):
                return same
            target = items[int(part)]
        else:
            return same
    if not isinstance(target, Mapping):
        return same
    found = cast(Any, target)
    resolved = resolve_ref(found, root, _seen | {ref})
    extra = {k: v for k, v in schema.items() if k != "$ref"}
    if not extra or not isinstance(resolved, Mapping):
        return resolved
    merged = dict(cast(Mapping[str, Any], resolved))
    for key, value in extra.items():
        merged.setdefault(key, value)
    return merged


# -- walking ------------------------------------------------------------------------------------


def _empty_object(quirks: frozenset[Quirk]) -> dict[str, Any]:
    if Quirk.OBJECT_PROPERTIES in quirks:
        return {"type": "object", "properties": {}, "required": []}
    return {"type": "object", "properties": {}}


def _flatten_type_list(types: list[Any], out: dict[str, Any]) -> None:
    named = [t for t in types if isinstance(t, str) and t != "null"]
    nullable = "null" in types
    if not named:
        out["type"] = "null" if nullable else "object"
        return
    if len(named) == 1:
        out["type"] = named[0]
    else:
        out["anyOf"] = [{"type": t} for t in named]
    if nullable:
        out.setdefault("nullable", True)


def _clean(node: Any, quirks: frozenset[Quirk]) -> Any:
    if isinstance(node, str):
        if node == "object":
            return _empty_object(quirks)
        return {"type": node} if node in _TYPE_NAMES else {}
    if isinstance(node, list):
        return [_clean(item, quirks) for item in cast(list[Any], node)]
    if not isinstance(node, Mapping):
        return node
    schema = cast(Mapping[str, Any], node)
    props_in = schema.get("properties")
    renames: dict[str, str] = {}
    if Quirk.PROPERTY_KEYS in quirks and isinstance(props_in, Mapping):
        renames = property_key_renames(cast(Mapping[str, Any], props_in))
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "type" and isinstance(value, list) and Quirk.TYPE_ARRAYS in quirks:
            _flatten_type_list(cast(list[Any], value), out)
        elif key in _MAP_KEYS and isinstance(value, Mapping):
            names = renames if key == "properties" else {}
            out[key] = {
                names.get(k, k): _clean(v, quirks)
                for k, v in cast(Mapping[str, Any], value).items()
            }
        elif key in _CHILD_KEYS:
            out[key] = value if isinstance(value, bool) else _clean(value, quirks)
        elif key == "dependencies" and isinstance(value, Mapping):
            # values are either a schema (walked) or a list of property names (literal)
            out[key] = {
                k: _clean(v, quirks) if isinstance(v, Mapping) else copy.deepcopy(v)
                for k, v in cast(Mapping[str, Any], value).items()
            }
        elif key == "required":
            if isinstance(value, bool):
                continue  # a legacy per-property flag; lifted into the parent below
            if isinstance(value, list):
                listed_required = cast(list[Any], value)
                out[key] = [renames.get(r, r) if isinstance(r, str) else r for r in listed_required]
            else:
                out[key] = copy.deepcopy(value)
        elif (
            key == "maxLength"
            and Quirk.GRAMMAR_LENGTH in quirks
            and isinstance(value, int)
            and value > GRAMMAR_MAX_LENGTH
        ):
            continue
        else:
            literal = cast(Any, value)
            out[key] = copy.deepcopy(literal)  # literal data, extension keys, descriptions
    if isinstance(props_in, Mapping):
        flagged = [
            renames.get(k, k)
            for k, v in cast(Mapping[str, Any], props_in).items()
            if isinstance(v, Mapping) and cast(Mapping[str, Any], v).get("required") is True
        ]
        if flagged:
            have = out.get("required")
            listed: list[Any] = list(cast(list[Any], have)) if isinstance(have, list) else []
            out["required"] = listed + [k for k in flagged if k not in listed]
    if Quirk.REF_DEFAULT in quirks and "$ref" in out:
        out.pop("default", None)
    if Quirk.OBJECT_PROPERTIES in quirks and out.get("type") == "object":
        if not isinstance(out.get("properties"), dict):
            out["properties"] = {}
        required = out.get("required")
        props = cast(dict[str, Any], out["properties"])
        out["required"] = (
            [r for r in cast(list[Any], required) if isinstance(r, str) and r in props]
            if isinstance(required, list)
            else []
        )
    return out


def _bottom_up(node: Any, fn: Callable[[dict[str, Any]], Any]) -> Any:
    if isinstance(node, list):
        return [_bottom_up(item, fn) for item in cast(list[Any], node)]
    if not isinstance(node, dict):
        return node
    return fn({k: _bottom_up(v, fn) for k, v in cast(dict[str, Any], node).items()})


def _is_null_branch(branch: Any) -> bool:
    return isinstance(branch, dict) and cast(dict[str, Any], branch).get("type") == "null"


def collapse_nullable_unions(node: Any) -> Any:
    """``anyOf``/``oneOf`` of one schema plus a null branch → that schema with ``nullable``."""

    def collapse(schema: dict[str, Any]) -> Any:
        for key in _UNIONS:
            branches = schema.get(key)
            if not isinstance(branches, list):
                continue
            listed = cast(list[Any], branches)
            kept = [b for b in listed if not _is_null_branch(b)]
            if len(kept) != 1 or len(kept) == len(listed):
                continue
            only = kept[0]
            single: dict[str, Any] = (
                dict(cast(dict[str, Any], only)) if isinstance(only, dict) else {}
            )
            single.setdefault("nullable", True)
            for meta in _CARRIED_META:
                if meta in schema and meta not in single:
                    if meta == "default" and "$ref" in single:
                        continue
                    single[meta] = schema[meta]
            return _bottom_up(single, collapse)
        return schema

    return _bottom_up(node, collapse)


def sanitize_parameters(parameters: Any, quirks: Iterable[Quirk] = ALL_QUIRKS) -> dict[str, Any]:
    """A cleaned deep copy of one tool's ``parameters`` (always an object schema)."""
    wanted = frozenset(quirks)
    if not isinstance(parameters, Mapping):
        return _empty_object(wanted)
    top = _clean(parameters, wanted)
    top = cast(dict[str, Any], top) if isinstance(top, dict) else _empty_object(wanted)
    top["type"] = "object"
    if not isinstance(top.get("properties"), dict):
        top["properties"] = {}
    if Quirk.OBJECT_PROPERTIES in wanted and not isinstance(top.get("required"), list):
        top["required"] = []
    if Quirk.NULLABLE_UNIONS in wanted:
        top = cast(dict[str, Any], collapse_nullable_unions(top))
    if Quirk.TOP_COMBINATORS in wanted:
        for key in _TOP_COMBINATORS:
            top.pop(key, None)
    return top


def sanitize(
    tools: Iterable[Mapping[str, Any]], quirks: Iterable[Quirk] = ALL_QUIRKS
) -> list[dict[str, Any]]:
    """Deep copies of ``{type: function, function: {...}}`` entries with clean parameters."""
    wanted = frozenset(quirks)
    out: list[dict[str, Any]] = []
    for tool in tools:
        entry = copy.deepcopy(dict(tool))
        fn = entry.get("function")
        if isinstance(fn, dict):
            fn_dict = cast(dict[str, Any], fn)
            fn_dict["parameters"] = sanitize_parameters(fn_dict.get("parameters"), wanted)
        out.append(entry)
    return out
