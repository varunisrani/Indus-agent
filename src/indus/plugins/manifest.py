# pyright: strict
"""What a plugin says about itself: ``plugin.yaml`` (or ``.yml``) or the portable ``plugin.json``.

Reading is forgiving: a field with the wrong type or an unknown field is dropped with a warning,
and the plugin keeps loading with everything else it declared. Only two things hold a plugin back,
each with a plain reason: a file that cannot be parsed at all (``ManifestRead.error``) and a
``requires_indus`` range the running version does not meet (``version_gate``).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from indus.foundation import yamlio

__all__ = [
    "MANIFEST_FILES",
    "SUPPORTED_MANIFEST_VERSION",
    "EnvRequirement",
    "ManifestRead",
    "PluginKind",
    "PluginManifest",
    "PluginRequirement",
    "manifest_from_mapping",
    "read_manifest",
    "satisfies",
    "version_gate",
]

log = logging.getLogger(__name__)

SUPPORTED_MANIFEST_VERSION = 2
# Looked for in this order; the first that exists is the manifest.
MANIFEST_FILES = ("plugin.yaml", "plugin.yml", "plugin.json")

PluginKind = Literal["standalone", "backend", "platform", "exclusive"]
_KINDS: frozenset[str] = frozenset({"standalone", "backend", "platform", "exclusive"})


class EnvRequirement(BaseModel):
    """A variable the plugin needs; ``secret`` ones are asked for masked (P13-22)."""

    model_config = ConfigDict(frozen=True)

    name: str
    prompt: str = ""
    secret: bool = False


class PluginRequirement(BaseModel):
    """Another plugin that must load first; ``version`` is a range like ``>=1.2``."""

    model_config = ConfigDict(frozen=True)

    name: str
    version: str = ""


class PluginManifest(BaseModel):
    """The parsed manifest plus where it was found (``source``, ``path``, ``key``, ``category``)."""

    model_config = ConfigDict(frozen=True)

    name: str
    version: str = ""
    description: str = ""
    author: str = ""
    license: str = ""
    homepage: str = ""
    tags: tuple[str, ...] = ()
    kind: PluginKind = "standalone"
    requires_env: tuple[str | EnvRequirement, ...] = ()
    provides_tools: tuple[str, ...] = ()
    provides_hooks: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    manifest_version: int = 1
    api_version: int | None = None
    requires_plugins: tuple[PluginRequirement, ...] = ()
    requires_indus: str = ""
    python_dependencies: tuple[str, ...] = ()
    config_schema: dict[str, Any] = Field(default_factory=dict[str, Any])
    emits: tuple[str, ...] = ()
    listens: tuple[str, ...] = ()
    skill_namespace: str = ""
    update_url: str = ""
    # Where it came from, filled by discovery: bundled, user, project or entrypoint.
    source: str = ""
    path: str = ""
    key: str = ""
    category: str = ""

    @property
    def plugin_id(self) -> str:
        """The id config keys use (``plugins.enabled``, ``plugins.entries.<id>``)."""
        return self.key or self.name

    def env_names(self) -> list[str]:
        return [e if isinstance(e, str) else e.name for e in self.requires_env]


@dataclass(frozen=True)
class ManifestRead:
    """One folder's reading: ``manifest`` is None when the file could not be parsed."""

    manifest: PluginManifest | None
    warnings: tuple[str, ...] = ()
    error: str | None = None


# -- lenient field checks -----------------------------------------------------------------------

_STRS = TypeAdapter(tuple[str, ...])

_ORIGIN_FIELDS = frozenset({"source", "path", "key", "category"})


def _as_text(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str | int | float):
        return str(value)
    return None


def _author_text(value: object) -> str | None:
    if isinstance(value, Mapping):
        parts = cast(Mapping[str, object], value)
        return ", ".join(str(parts[k]) for k in ("name", "email", "url") if parts.get(k))
    return _as_text(value)


def _env_entry(item: object) -> str | EnvRequirement | None:
    if isinstance(item, str) and item.strip():
        return item.strip()
    if isinstance(item, Mapping):
        try:
            return EnvRequirement.model_validate(item)
        except ValidationError:
            return None
    return None


def _requirement(item: object) -> PluginRequirement | None:
    if isinstance(item, str) and item.strip():
        return PluginRequirement(name=item.strip())
    if isinstance(item, Mapping):
        entry = cast(Mapping[str, object], item)
        name = entry.get("name")
        version = entry.get("version", "")
        if isinstance(name, str) and name.strip() and isinstance(version, str | int | float):
            return PluginRequirement(name=name.strip(), version=str(version).strip())
    return None


def _strings(value: object) -> tuple[str, ...] | None:
    try:
        items = _STRS.validate_python(value)
    except ValidationError:
        return None
    return tuple(s.strip() for s in items if s.strip())


def _list_of[T](convert: Callable[[object], T | None]) -> Callable[[object], tuple[T, ...] | None]:
    def check(value: object) -> tuple[T, ...] | None:
        if not isinstance(value, list | tuple):
            return None
        items: list[T] = []
        for item in cast(list[object], value):
            converted = convert(item)
            if converted is None:
                return None
            items.append(converted)
        return tuple(items)

    return check


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _mapping(value: object) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return {str(k): v for k, v in cast(Mapping[object, Any], value).items()}
    return None


def _kind(value: object) -> str | None:
    text = value.strip().lower() if isinstance(value, str) else None
    return text if text in _KINDS else None


# Field name → (checker returning None when the value is unusable, what it should be).
_CHECKS: dict[str, tuple[Callable[[object], object | None], str]] = {
    "name": (_as_text, "a string"),
    "version": (_as_text, "a string"),
    "description": (_as_text, "a string"),
    "author": (_author_text, "a string or {name, email, url}"),
    "license": (_as_text, "a string"),
    "homepage": (_as_text, "a string"),
    "tags": (_strings, "a list of strings"),
    "kind": (_kind, "one of " + ", ".join(sorted(_KINDS))),
    "requires_env": (_list_of(_env_entry), "a list of names or {name, prompt, secret}"),
    "provides_tools": (_strings, "a list of strings"),
    "provides_hooks": (_strings, "a list of strings"),
    "capabilities": (_strings, "a list of strings"),
    "manifest_version": (_int, "an integer"),
    "api_version": (_int, "an integer"),
    "requires_plugins": (_list_of(_requirement), "a list of names or {name, version}"),
    "requires_indus": (_as_text, "a version range string"),
    "python_dependencies": (_strings, "a list of requirement strings"),
    "config_schema": (_mapping, "a mapping"),
    "emits": (_strings, "a list of strings"),
    "listens": (_strings, "a list of strings"),
    "skill_namespace": (_as_text, "a string"),
    "update_url": (_as_text, "a string"),
}


def manifest_from_mapping(
    data: Mapping[str, object],
    *,
    fallback_name: str,
    source: str = "",
    path: str = "",
    key: str = "",
    category: str = "",
) -> tuple[PluginManifest, list[str]]:
    """Build a manifest from parsed data; every problem becomes a warning, never an error."""
    warnings: list[str] = []
    label = key or fallback_name
    values: dict[str, object] = {}
    for name, raw in data.items():
        check = _CHECKS.get(name)
        if check is None:
            warnings.append(f"plugin {label}: unknown manifest field {name!r} ignored")
            continue
        if raw is None:
            continue
        converted = check[0](raw)
        if converted is None:
            warnings.append(f"plugin {label}: {name} must be {check[1]}; ignored")
            continue
        values[name] = converted
    name = values.get("name")
    if not isinstance(name, str) or not name.strip():
        warnings.append(f"plugin {label}: manifest has no name; using the folder name")
        values["name"] = fallback_name
    else:
        values["name"] = name.strip()
    version = values.get("manifest_version")
    if isinstance(version, int) and version > SUPPORTED_MANIFEST_VERSION:
        warnings.append(
            f"plugin {label}: manifest_version {version} is newer than this Indus reads "
            f"({SUPPORTED_MANIFEST_VERSION}); reading what it knows"
        )
    values.update(source=source, path=path, key=key or str(values["name"]), category=category)
    return PluginManifest.model_validate(values), warnings


def _load_file(file: Path) -> object:
    text = file.read_text(encoding="utf-8-sig")
    if file.suffix == ".json":
        return json.loads(text)
    return yamlio.load_safe(text)


def read_manifest(folder: Path, *, source: str, prefix: str = "") -> ManifestRead | None:
    """The manifest of ``folder``; None when the folder has no manifest file at all.

    ``prefix`` is the category folder (``image_gen``) for plugins one level down; it becomes part
    of the key (``image_gen/<folder>``). Warnings are also logged.
    """
    file = next((folder / n for n in MANIFEST_FILES if (folder / n).is_file()), None)
    if file is None:
        return None
    key_hint = f"{prefix}/{folder.name}" if prefix else folder.name
    try:
        data = _load_file(file)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        error = f"{file.name} could not be read: {exc}"
        log.warning("plugin %s: %s", key_hint, error)
        return ManifestRead(None, error=error)
    if data is None:
        data = {}
    if not isinstance(data, Mapping):
        error = f"{file.name} must hold a mapping at the top, not {type(data).__name__}"
        log.warning("plugin %s: %s", key_hint, error)
        return ManifestRead(None, error=error)
    raw = cast(Mapping[object, object], data)
    fields = {str(k): v for k, v in raw.items() if str(k) not in _ORIGIN_FIELDS}
    named = fields.get("name")
    key = (
        f"{prefix}/{folder.name}"
        if prefix
        else (named.strip() if isinstance(named, str) and named.strip() else folder.name)
    )
    manifest, warnings = manifest_from_mapping(
        fields,
        fallback_name=folder.name,
        source=source,
        path=str(folder),
        key=key,
        category=prefix,
    )
    for text in warnings:
        log.warning("%s", text)
    return ManifestRead(manifest, tuple(warnings))


# -- version ranges -----------------------------------------------------------------------------

_CLAUSE = re.compile(r"^(>=|<=|==|!=|>|<)?\s*v?([0-9][0-9A-Za-z.+-]*)$")
_LEADING_DIGITS = re.compile(r"^(\d+)")


def _numbers(version: str) -> tuple[int, int, int] | None:
    """``1.2.3rc1`` / ``v1.2`` / ``1.2.3+local`` → (1, 2, 3); None when a part has no digits."""
    core = re.split(r"[+-]", version.strip().lstrip("v"), maxsplit=1)[0]
    parts = core.split(".")[:3]
    while len(parts) < 3:
        parts.append("0")
    numbers: list[int] = []
    for part in parts:
        match = _LEADING_DIGITS.match(part)
        if match is None:
            return None
        numbers.append(int(match.group(1)))
    return numbers[0], numbers[1], numbers[2]


_COMPARE: dict[str, Callable[[tuple[int, int, int], tuple[int, int, int]], bool]] = {
    ">=": lambda a, b: a >= b,
    ">": lambda a, b: a > b,
    "<=": lambda a, b: a <= b,
    "<": lambda a, b: a < b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def satisfies(spec: str, version: str) -> bool:
    """Does ``version`` meet ``spec`` (clauses joined by commas, all must hold)?

    A bare version is a floor (``0.3`` means ``>=0.3``). Anything that cannot be read is let
    through: a manifest typo must not lock a plugin out.
    """
    current = _numbers(version)
    if current is None:
        return True
    for clause in (c.strip() for c in spec.split(",")):
        if not clause:
            continue
        match = _CLAUSE.match(clause)
        if match is None:
            log.warning("version range clause %r not understood; ignored", clause)
            continue
        wanted = _numbers(match.group(2))
        if wanted is None:
            continue
        if not _COMPARE[match.group(1) or ">="](current, wanted):
            return False
    return True


def version_gate(manifest: PluginManifest, running: str) -> str | None:
    """Why ``manifest`` cannot run on Indus ``running``, or None when it can."""
    spec = manifest.requires_indus.strip()
    if not spec or satisfies(spec, running):
        return None
    return f"needs Indus {spec}, this is {running}"
