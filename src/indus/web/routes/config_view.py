"""What the config and keys routes show (P18-12): the schema form, redaction, the effective view
and the key catalog.

- ``schema_form()`` walks the Pydantic config models: every leaf setting becomes
  ``{type, description, category, options?}`` keyed by its dotted path; the category is the
  top-level section, in the models' order. Free-form mappings (custom providers, per-platform
  maps) have no fixed fields and are left to the file.
- ``form_tree(view)`` is the settings as the form edits them: defaults with the user's own file
  laid over, *unexpanded* (a ``${NAME}`` reference stays a reference, so no secret reaches the page
  through one), ``_``-prefixed internals dropped, and literal values under credential-shaped keys
  replaced by ``PLACEHOLDER``. ``restore_placeholders`` puts the stored values back on the way in.
- ``effective_entries(view)`` lists every leaf with where its value comes from.
- ``key_catalog()`` is the keys page's list of known names (bundled provider keys and their
  base-URL overrides); ``is_credential_name`` decides which names go to the secret store.
"""

from __future__ import annotations

import copy
import re
import types
import typing
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, Union, cast

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from indus.foundation.config import schema
from indus.foundation.config.loader import ConfigView, deep_merge
from indus.providers import bundled

__all__ = [
    "PLACEHOLDER",
    "KeyMeta",
    "ValueSource",
    "effective_entries",
    "form_tree",
    "is_credential_name",
    "is_secret_setting",
    "key_catalog",
    "restore_placeholders",
    "schema_form",
]

PLACEHOLDER: Final = "••••••••"
"""Stands in for a stored credential in ``GET /api/config``; sent back, it keeps the value."""

type ValueSource = Literal["default", "file", "env", "overlay"]

_SOURCE: Final[dict[str, ValueSource]] = {
    "default": "default",
    "user": "file",
    "env-expanded": "env",
}
_SECRET_WORDS: Final = frozenset(
    {"key", "apikey", "token", "secret", "password", "passwd", "credential", "credentials"}
)
_NOT_A_SECRET_TAIL: Final = frozenset(
    {"env", "name", "names", "url", "id", "file", "path", "type", "mode", "tokens"}
)
_WORDS: Final = re.compile(r"[_\-.\s]+")
_CREDENTIAL_SUFFIXES: Final = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")


# -- the schema form ----------------------------------------------------------------------------


def _members(annotation: object) -> list[object]:
    if typing.get_origin(annotation) in (Union, types.UnionType):
        return [a for a in typing.get_args(annotation) if a is not type(None)]
    return [annotation]


def _model_of(annotation: object) -> type[BaseModel] | None:
    for member in _members(annotation):
        if isinstance(member, type) and issubclass(member, BaseModel):
            return member
    return None


def _leaf(annotation: object) -> dict[str, Any] | None:
    members = _members(annotation)
    plain = members[0] if members else annotation
    origin = typing.get_origin(plain)
    if origin is Literal:
        return {"type": "enum", "options": [str(v) for v in typing.get_args(plain)]}
    if origin in (list, tuple, set, frozenset):
        return {"type": "list"}
    if plain is bool:
        return {"type": "boolean"}
    if plain is int:
        return {"type": "integer"}
    if plain is float:
        return {"type": "number"}
    if plain is str:
        return {"type": "string"}
    return None  # mappings and open shapes are edited in the file


def _walk(path: str, info: FieldInfo, category: str, out: dict[str, dict[str, Any]]) -> None:
    model = _model_of(info.annotation)
    if model is not None:
        for name, child in model.model_fields.items():
            _walk(f"{path}.{child.alias or name}", child, category, out)
        return
    leaf = _leaf(info.annotation)
    if leaf is None:
        return
    out[path] = {**leaf, "description": info.description or "", "category": category}


def schema_form() -> dict[str, Any]:
    """``{fields, category_order}`` for the config page."""
    fields: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for name, info in schema.ConfigModel.model_fields.items():
        key = info.alias or name
        if key.startswith("_"):
            continue
        before = len(fields)
        _walk(key, info, key, fields)
        if len(fields) > before:
            order.append(key)
    return {"fields": fields, "category_order": order}


# -- the form tree and redaction ----------------------------------------------------------------


def is_secret_setting(path: str) -> bool:
    """The last segment of a dotted setting names a credential (``api_key``, ``bot_token``,
    ``password_hash``); ``max_tokens`` or ``key_env`` do not."""
    words = [w for w in _WORDS.split(path.rsplit(".", 1)[-1].lower()) if w]
    if not words or words[-1] in _NOT_A_SECRET_TAIL:
        return False
    return any(w in _SECRET_WORDS for w in words)


def _hides(path: str, value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and not value.strip().startswith("$")
        and is_secret_setting(path)
    )


def _redacted(node: object, path: str) -> object:
    if isinstance(node, dict):
        mapping = cast(dict[str, Any], node)
        return {k: _redacted(v, f"{path}.{k}" if path else str(k)) for k, v in mapping.items()}
    if isinstance(node, list):
        return [_redacted(v, path) for v in cast(list[Any], node)]
    return PLACEHOLDER if _hides(path, node) else node


def _public(tree: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in tree.items() if not str(k).startswith("_")}


def form_tree(view: ConfigView) -> dict[str, Any]:
    """Defaults + the user's file, unexpanded, internals dropped, credentials hidden."""
    merged = deep_merge(schema.defaults_tree(), view.user_raw())
    return cast(dict[str, Any], _redacted(_public(merged), ""))


def defaults_form() -> dict[str, Any]:
    return _public(schema.defaults_tree())


def restore_placeholders(incoming: Mapping[str, Any], stored: Mapping[str, Any]) -> dict[str, Any]:
    """``incoming`` with every ``PLACEHOLDER`` swapped back for the stored value (dropped when
    nothing is stored there), so an untouched hidden field keeps its credential."""
    out: dict[str, Any] = {}
    for key, value in incoming.items():
        here: object = stored.get(key) if isinstance(stored, Mapping) else None
        if isinstance(value, dict):
            inner = cast(Mapping[str, Any], here) if isinstance(here, dict) else {}
            out[key] = restore_placeholders(cast(Mapping[str, Any], value), inner)
        elif value == PLACEHOLDER:
            if here is not None:
                out[key] = copy.deepcopy(here)
        else:
            out[key] = copy.deepcopy(value)
    return out


# -- the effective view ---------------------------------------------------------------------------


def _leaves(node: object, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], object]]:
    if isinstance(node, dict) and node:
        for key, value in cast(dict[str, Any], node).items():
            yield from _leaves(value, (*path, str(key)))
    elif path:
        yield path, node


def effective_entries(view: ConfigView) -> list[dict[str, Any]]:
    """Every leaf setting: ``{key, value, source}``; a value read from the environment is shown as
    the reference that reads it, and credentials stay hidden."""
    merged = _public(deep_merge(schema.defaults_tree(), view.user_raw()))
    entries: list[dict[str, Any]] = []
    for path, value in _leaves(merged):
        key = ".".join(path)
        source = _SOURCE.get(view.source(key) or "user", "file")
        shown = _redacted(value, key)
        entries.append({"key": key, "value": shown, "source": source})
    return entries


# -- the key catalog ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KeyMeta:
    category: str
    description: str
    advanced: bool
    is_password: bool
    url: str | None = None


def key_catalog() -> dict[str, KeyMeta]:
    """Names the keys page always lists: each bundled provider's key names and its base-URL
    override (an advanced, non-secret setting)."""
    out: dict[str, KeyMeta] = {}
    for profile in bundled.PROFILES:
        if profile.hidden:
            continue
        label = profile.display_name or profile.name
        for name in profile.env_vars:
            out.setdefault(name, KeyMeta("provider", f"API key for {label}", False, True))
        if profile.base_url_env_var:
            out.setdefault(
                profile.base_url_env_var,
                KeyMeta("provider", f"Endpoint URL override for {label}", True, False),
            )
    return out


def is_credential_name(name: str) -> bool:
    """A name whose value is a credential: the usual suffixes or a bundled provider key name."""
    upper = name.upper()
    if upper.endswith(_CREDENTIAL_SUFFIXES):
        return True
    return any(upper in profile.env_vars for profile in bundled.PROFILES)
