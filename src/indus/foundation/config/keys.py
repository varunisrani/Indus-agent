"""Dotted config keys and the coercion of typed-in values.

Real key names contain dots (model ids such as ``grok-4.6``). In a dotted path ``\\.`` stands for
a dot inside a name and ``\\\\`` for a backslash. While walking a mapping, a name that itself
contains dots is tried before splitting, longest candidate first, so ``models.grok-4.6.x`` reaches
the ``grok-4.6`` entry. Writes refuse to add a new mapping that would sit beside such a name and
hide it (R04.8). `coerce` keeps the literal for string slots (``off`` stays ``"off"``, R04.7) and
refuses malformed list/mapping literals.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from indus.foundation import yamlio
from indus.foundation.config import schema

__all__ = [
    "MISSING",
    "CoercionError",
    "GuardError",
    "KeyPathError",
    "check_write",
    "coerce",
    "get_path",
    "has_path",
    "is_env_shaped",
    "join_key",
    "resolve_path",
    "set_path",
    "split_key",
    "unset_path",
]


class _Missing:
    def __repr__(self) -> str:
        return "MISSING"


MISSING: Any = _Missing()


class KeyPathError(ValueError):
    """A dotted key is malformed or cannot be navigated or written."""


class CoercionError(ValueError):
    """A typed-in value looks like a list or mapping but is not valid YAML."""


class GuardError(ValueError):
    """A write would replace a section or put the wrong shape in a container slot."""


# Slots whose container shape is fixed even before anything is on disk.
KNOWN_CONTAINERS: dict[str, type] = {
    "providers": dict,
    "approvals.deny": list,
}

_SCALAR_WORDS: dict[str, Any] = {
    "true": True,
    "yes": True,
    "on": True,
    "false": False,
    "no": False,
    "off": False,
    "null": None,
    "none": None,
    "~": None,
}

_ENV_SHAPED = re.compile(r"^[A-Z][A-Z0-9_]*$")
_INT = re.compile(r"^[+-]?\d+$")
# An escape pair, a separator, a run of plain characters, or a lone backslash.
_KEY_TOKEN = re.compile(r"\\[.\\]|\.|[^.\\]+|\\")


# -- splitting ----------------------------------------------------------------------------------


def split_key(key: str) -> list[str]:
    """Split a dotted key into segments; ``\\.`` is a literal dot, ``\\\\`` a literal backslash."""
    if not isinstance(key, str) or not key or key != key.strip():  # pyright: ignore[reportUnnecessaryIsInstance]
        raise KeyPathError(f"invalid config key {key!r}: empty or surrounded by spaces")
    segments = [""]
    for token in _KEY_TOKEN.findall(key):
        if token == ".":
            segments.append("")
        elif len(token) == 2 and token[0] == "\\":
            segments[-1] += token[1]
        else:
            segments[-1] += token
    if "" in segments:
        raise KeyPathError(f"invalid config key {key!r}: empty segment")
    return segments


def join_key(parts: list[str]) -> str:
    """Inverse of `split_key`."""
    if not parts or any(not p for p in parts):
        raise KeyPathError("a key needs at least one non-empty segment")
    return ".".join(p.replace("\\", "\\\\").replace(".", "\\.") for p in parts)


def is_env_shaped(key: str) -> bool:
    """True for UPPER_SNAKE names, which belong in ``.env`` rather than ``config.yaml``."""
    return bool(_ENV_SHAPED.match(key))


# -- navigation ---------------------------------------------------------------------------------


def _literal_match(container: Mapping[Any, Any], parts: list[str]) -> tuple[str, int] | None:
    for n in range(len(parts), 0, -1):
        candidate = ".".join(parts[:n])
        if candidate in container:
            return candidate, n
    return None


def _step(node: Any, rest: list[str]) -> tuple[Any, int] | None:
    """The key to follow from ``node`` and how many segments it uses, or None."""
    if isinstance(node, list):
        size = len(cast(list[Any], node))
        head = rest[0]
        if _INT.match(head) and -size <= int(head) < size:
            return int(head), 1
        return None
    if isinstance(node, Mapping):
        return _literal_match(cast(Mapping[Any, Any], node), rest)
    return None


def _locate(doc: Any, parts: list[str]) -> tuple[list[tuple[Any, Any]], Any, Any] | None:
    """Walk ``parts``; return (hops above, container, key) for the addressed leaf, or None."""
    hops: list[tuple[Any, Any]] = []
    node: Any = doc
    pos = 0
    while (step := _step(node, parts[pos:])) is not None:
        name, used = step
        pos += used
        if pos == len(parts):
            return hops, node, name
        hops.append((node, name))
        node = node[name]
    return None


def get_path(doc: Any, key: str, default: Any = MISSING) -> Any:
    """Value at a dotted path; ``KeyError`` when absent and no default is given."""
    found = _locate(doc, split_key(key))
    if found is None:
        if default is MISSING:
            raise KeyError(key)
        return default
    _, container, leaf = found
    return container[leaf]


def has_path(doc: Any, key: str) -> bool:
    return _locate(doc, split_key(key)) is not None


def resolve_path(doc: Any, key: str) -> tuple[Any, ...] | None:
    """The actual key and index sequence a dotted path addresses in ``doc``, or None."""
    found = _locate(doc, split_key(key))
    if found is None:
        return None
    hops, _, leaf = found
    return (*(name for _, name in hops), leaf)


def set_path(doc: dict[str, Any], key: str, value: Any) -> None:
    """Set a value, creating intermediate mappings. Lists are indexed, never grown."""
    parts = split_key(key)
    current: Any = doc
    i = 0
    while i < len(parts):
        remaining = parts[i:]
        last = len(remaining) == 1
        if isinstance(current, list):
            seg = remaining[0]
            items = cast(list[Any], current)
            if not _INT.match(seg) or not -len(items) <= int(seg) < len(items):
                raise KeyPathError(f"{key!r}: {seg!r} is not an index of the existing list")
            if last:
                items[int(seg)] = value
                return
            current = items[int(seg)]
            i += 1
            continue
        if not isinstance(current, dict):
            where = join_key(parts[:i])
            raise KeyPathError(f"{key!r}: {where!r} holds a value, not a section")
        mapping = cast(dict[Any, Any], current)
        match = _literal_match(mapping, remaining)
        if match is not None:
            name, consumed = match
            if i + consumed == len(parts):
                mapping[name] = value
                return
            current = mapping[name]
            i += consumed
            continue
        seg = remaining[0]
        if last:
            mapping[seg] = value
            return
        shadowed = next(
            (k for k in mapping if isinstance(k, str) and k.startswith(seg + ".")), None
        )
        if shadowed is not None:
            raise KeyPathError(
                f"{key!r} would create {seg!r} beside the existing key {shadowed!r}, which "
                f"nothing reads; to address that key write it as {join_key([shadowed])!r}"
            )
        mapping[seg] = {}
        current = mapping[seg]
        i += 1


def unset_path(doc: dict[str, Any], key: str) -> bool:
    """Remove a value; drop mappings left empty by the removal. True when something was removed."""
    found = _locate(doc, split_key(key))
    if found is None:
        return False
    hops, container, leaf = found
    del container[leaf]
    current = container
    for parent, name in reversed(hops):
        if current != {}:
            break
        del parent[name]
        current = parent
    return True


# -- coercion -----------------------------------------------------------------------------------


def _looks_structured(text: str) -> bool:
    stripped = text.lstrip()
    if stripped[:1] in ("[", "{"):
        return True
    if "\n" not in text:
        return False
    for line in text.splitlines():
        item = line.strip()
        if item == "-" or item.startswith("- "):
            return True
        head, sep, _ = item.partition(": ")
        if sep and head and " " not in head and not head.startswith("#"):
            return True
        if item.endswith(":") and item[:-1] and " " not in item[:-1]:
            return True
    return False


def _as_float(text: str) -> float | None:
    try:
        value = float(text)
        exact = Decimal(text)
    except (ValueError, InvalidOperation):
        return None
    if math.isnan(value) or math.isinf(value) or exact != Decimal(repr(value)):
        return None
    return value


def _quoted(text: str) -> bool:
    return len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"')


def coerce(raw: str, default_type: type | None, *, key: str | None = None) -> Any:
    """Turn typed-in text into a value for a slot whose default has ``default_type``."""
    structured = _looks_structured(raw)
    if default_type is str and not (key == "model" and structured):
        return raw
    text = raw.strip()
    if _quoted(text):
        try:
            loaded = yamlio.load_safe(text)
        except yamlio.YamlError:
            return text[1:-1]
        return loaded if isinstance(loaded, str) else text[1:-1]
    word = text.lower()
    if word in _SCALAR_WORDS:
        return _SCALAR_WORDS[word]
    if _INT.match(text.replace("_", "")):
        try:
            return int(text)
        except ValueError:
            pass
    as_float = _as_float(text)
    if as_float is not None:
        return as_float
    if not structured:
        return raw
    try:
        parsed = yamlio.load_safe(raw)
    except yamlio.YamlError as exc:
        raise CoercionError(
            f"value for {key or 'this setting'!r} looks like a list or mapping but is not valid "
            f"YAML ({exc.reason}); nothing was written. Quote it to store plain text."
        ) from exc
    if isinstance(parsed, list | dict):
        return cast(Any, parsed)
    return raw


# -- guards -------------------------------------------------------------------------------------


def _expected_container(doc: dict[str, Any], key: str) -> type | None:
    parts = split_key(key)
    if len(parts) > 1 or key in KNOWN_CONTAINERS:
        known = KNOWN_CONTAINERS.get(key)
        if known is not None:
            return known
        declared = schema.schema_for(key)
        if declared in (list, dict):
            return declared
    existing = get_path(doc, key, None)
    if len(parts) > 1 and isinstance(existing, list | dict):
        return list if isinstance(existing, list) else dict
    return None


def check_write(
    doc: dict[str, Any], key: str, value: Any, *, force: bool = False
) -> tuple[str, Any]:
    """Apply the section and container guards; return the (possibly redirected) key and value.

    ``doc`` is the user's raw config. Raises `GuardError` when the write must not happen.
    """
    parts = split_key(key)
    if len(parts) == 1:
        existing = doc.get(key)
        if key == "model" and not force:
            if isinstance(value, list) or (isinstance(value, dict) and isinstance(existing, dict)):
                raise GuardError(
                    "'model' takes a model id; set model.provider / model.default one by one, "
                    "or replace the whole section with force"
                )
            if isinstance(existing, dict) and not isinstance(value, dict):
                return "model.default", value
        elif isinstance(existing, dict) and not isinstance(value, dict) and not force:
            names = ", ".join(str(k) for k in list(cast(dict[Any, Any], existing))[:8])
            raise GuardError(
                f"'{key}' is a section ({names}); set one of its keys with a dotted path, "
                "or replace the whole section with force"
            )
    expected = _expected_container(doc, key)
    if expected is list and not isinstance(value, list):
        raise GuardError(f"'{key}' must be a list, e.g. '[item, ...]'; nothing was written")
    if expected is dict and not isinstance(value, dict) and not force:
        raise GuardError(f"'{key}' must be a mapping, e.g. '{{name: value}}'; nothing was written")
    return key, cast(Any, value)
