# pyright: strict
"""Finding plugins and deciding which ones may load (P13-14). Nothing is imported here.

Sources, in this order:

1. bundled: ``src/indus/plugins/bundled/``;
2. user: ``<home>/plugins/``;
3. project: ``<project>/.indus/plugins/``, only with ``INDUS_ENABLE_PROJECT_PLUGINS`` (scope env)
   or ``plugins.project: true``, and never in gateway mode (which warns instead);
4. Python packages advertising the ``indus.plugins`` entry-point group (D-16.39).

A key found again in a later source replaces the earlier record (a user copy overrides the bundled
one); memory providers are the exception: the first one found stays. Folders named after a
provider category (``SUBCATEGORIES``) hold their plugins one level down, keyed
``<category>/<folder>``.

The gate: ``plugins.disabled`` always wins. Memory providers, context engines and model providers
are handed to their own selection (by their own config) and never loaded by the general loader.
Bundled backends and platforms load without being listed. Every other plugin is only listed until
its key or name is in ``plugins.enabled``.
"""

from __future__ import annotations

import importlib.metadata
import logging
from collections.abc import Iterable, MutableMapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from indus.foundation.scope import Scope
from indus.plugins.manifest import PluginManifest, manifest_from_mapping, read_manifest
from indus.plugins.settings import plugins_setting, string_set

__all__ = [
    "BUNDLED_DIR",
    "ENTRY_POINT_GROUP",
    "PROJECT_PLUGINS_ENV",
    "SPECIALISED",
    "SUBCATEGORIES",
    "Discovery",
    "PluginRecord",
    "discover",
    "set_enabled",
]

log = logging.getLogger(__name__)

BUNDLED_DIR = Path(__file__).resolve().parent / "bundled"
ENTRY_POINT_GROUP = "indus.plugins"
PROJECT_PLUGINS_ENV = "INDUS_ENABLE_PROJECT_PLUGINS"
PROJECT_PLUGINS_DIR = Path(".indus") / "plugins"

# Category folder → how its plugins are treated.
BACKEND_CATEGORIES = frozenset(
    {"image_gen", "video_gen", "tts", "stt", "web", "browser", "computer_use", "backends"}
)
SPECIALISED = frozenset({"memory", "context_engine", "model_providers"})
SUBCATEGORIES = BACKEND_CATEGORIES | SPECIALISED | {"platforms"}

State = Literal["enabled", "disabled", "not-enabled", "broken"]
Handler = Literal["general", "memory", "context_engine", "model_providers"]
_TRUE = frozenset({"1", "true", "yes", "on"})


class EntryPointLike(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def value(self) -> str: ...


@dataclass(frozen=True)
class PluginRecord:
    """One discovered plugin and the gate's verdict on it."""

    key: str
    source: str
    manifest: PluginManifest | None
    state: State
    handler: Handler = "general"
    gated: bool = True
    reason: str = ""
    error: str | None = None

    @property
    def name(self) -> str:
        return self.manifest.name if self.manifest is not None else self.key.rsplit("/", 1)[-1]

    @property
    def kind(self) -> str:
        return self.manifest.kind if self.manifest is not None else "standalone"


@dataclass
class Discovery:
    records: dict[str, PluginRecord] = field(default_factory=dict[str, PluginRecord])
    warnings: list[str] = field(default_factory=list[str])

    def loadable(self) -> list[PluginRecord]:
        """What the general loader imports: enabled, general, with a manifest, in key order."""
        return [
            r
            for _, r in sorted(self.records.items())
            if r.state == "enabled" and r.handler == "general" and r.manifest is not None
        ]

    def category(self, name: str) -> list[PluginRecord]:
        """Records of one category folder (``memory``, ``tts``...), in key order."""
        return [
            r
            for _, r in sorted(self.records.items())
            if r.manifest is not None and r.manifest.category == name
        ]

    def get(self, key: str) -> PluginRecord | None:
        return self.records.get(key)


# -- scanning -----------------------------------------------------------------------------------


def _children(folder: Path) -> list[Path]:
    try:
        return sorted(p for p in folder.iterdir() if p.is_dir())
    except FileNotFoundError:
        return []
    except OSError as exc:
        log.warning("plugin folder %s could not be read: %s", folder, exc)
        return []


def _dunder(path: Path) -> bool:
    return path.name.startswith("__") and path.name.endswith("__")


def _scan(root: Path, source: str) -> list[tuple[str, PluginManifest | None, str | None]]:
    """(key, manifest, error) for every plugin folder under ``root``."""
    found: list[tuple[str, PluginManifest | None, str | None]] = []
    for child in _children(root):
        if _dunder(child) or child.name.startswith("."):
            continue
        read = read_manifest(child, source=source)
        if read is not None:
            key = read.manifest.key if read.manifest is not None else child.name
            found.append((key, read.manifest, read.error))
            continue
        if child.name not in SUBCATEGORIES:
            log.debug("%s has no plugin manifest; skipped", child)
            continue
        for plugin in _children(child):
            if _dunder(plugin) or plugin.name.startswith("."):
                continue
            inner = read_manifest(plugin, source=source, prefix=child.name)
            if inner is None:
                log.debug("%s has no plugin manifest; skipped", plugin)
                continue
            key = f"{child.name}/{plugin.name}"
            found.append((key, inner.manifest, inner.error))
    return found


def _entry_points(given: Iterable[EntryPointLike] | None) -> list[EntryPointLike]:
    if given is not None:
        return list(given)
    try:
        return list(importlib.metadata.entry_points(group=ENTRY_POINT_GROUP))
    except Exception as exc:
        log.debug("entry points of %s could not be listed: %s", ENTRY_POINT_GROUP, exc)
        return []


def _entry_point_manifest(ep: EntryPointLike) -> PluginManifest | None:
    """A manifest from package metadata only; the target module is not imported."""
    try:
        dist: Any = getattr(ep, "dist", None)
        meta: Any = getattr(dist, "metadata", None)
        summary = meta.get("Summary", "") if meta is not None else ""
        data: dict[str, object] = {
            "name": ep.name,
            "version": str(getattr(dist, "version", "") or ""),
            "description": str(summary or ""),
        }
        manifest, _ = manifest_from_mapping(
            data, fallback_name=ep.name, source="entrypoint", path=ep.value, key=ep.name
        )
    except Exception as exc:
        log.debug("entry point %r skipped: %s", getattr(ep, "name", "?"), exc)
        return None
    return manifest


def _project_allowed(scope: Scope, gateway: bool, warnings: list[str]) -> bool:
    opted = scope.env.get(PROJECT_PLUGINS_ENV, "").strip().lower() in _TRUE or (
        plugins_setting(scope, "project", False) is True
    )
    if not opted:
        return False
    if gateway:
        text = "project plugins are never loaded by the gateway; the opt-in is ignored here"
        warnings.append(text)
        log.warning("%s", text)
        return False
    return True


# -- the gate -----------------------------------------------------------------------------------


def _handler_of(manifest: PluginManifest) -> Handler:
    if manifest.category == "memory" or manifest.kind == "exclusive":
        return "memory"
    if manifest.category == "context_engine":
        return "context_engine"
    if manifest.category == "model_providers":
        return "model_providers"
    return "general"


def _effective_kind(manifest: PluginManifest) -> str:
    if manifest.kind != "standalone":
        return manifest.kind
    if manifest.category in BACKEND_CATEGORIES:
        return "backend"
    if manifest.category == "platforms":
        return "platform"
    return manifest.kind


def _judge(
    key: str,
    source: str,
    manifest: PluginManifest | None,
    error: str | None,
    enabled: frozenset[str],
    disabled: frozenset[str],
) -> PluginRecord:
    if manifest is None:
        return PluginRecord(key, source, None, "broken", reason=error or "", error=error)
    names = {key, manifest.name}
    handler = _handler_of(manifest)
    if names & disabled:
        return PluginRecord(key, source, manifest, "disabled", handler, reason="disabled in config")
    if handler != "general":
        return PluginRecord(
            key,
            source,
            manifest,
            "enabled",
            handler,
            gated=False,
            reason=f"selected through its own {handler} setting",
        )
    if source == "bundled" and _effective_kind(manifest) in {"backend", "platform"}:
        return PluginRecord(key, source, manifest, "enabled", gated=False)
    if names & enabled:
        return PluginRecord(key, source, manifest, "enabled")
    return PluginRecord(
        key,
        source,
        manifest,
        "not-enabled",
        reason=f"not in plugins.enabled (enable it with `indus plugins enable {key}`)",
    )


def discover(
    scope: Scope,
    *,
    bundled_dir: Path | None = None,
    project_root: Path | None = None,
    gateway: bool = False,
    entry_points: Iterable[EntryPointLike] | None = None,
) -> Discovery:
    """Every plugin this profile can see, with the gate's verdict. Imports nothing.

    ``project_root`` is the session's working folder (default: the process cwd); ``gateway`` is
    true for the messaging gateway, which never reads project plugins. ``entry_points`` replaces
    the installed entry-point list (tests).
    """
    result = Discovery()
    enabled = string_set(plugins_setting(scope, "enabled", []))
    disabled = string_set(plugins_setting(scope, "disabled", []))
    sources: list[tuple[str, Path]] = [
        ("bundled", bundled_dir if bundled_dir is not None else BUNDLED_DIR),
        ("user", scope.home / "plugins"),
    ]
    if _project_allowed(scope, gateway, result.warnings):
        root = project_root if project_root is not None else Path.cwd()
        sources.append(("project", root / PROJECT_PLUGINS_DIR))
    found: list[tuple[str, str, PluginManifest | None, str | None]] = []
    for source, folder in sources:
        found.extend((key, source, m, e) for key, m, e in _scan(folder, source))
    for ep in _entry_points(entry_points):
        manifest = _entry_point_manifest(ep)
        if manifest is not None:
            found.append((manifest.key, "entrypoint", manifest, None))
    for key, source, manifest, error in found:
        record = _judge(key, source, manifest, error, enabled, disabled)
        current = result.records.get(key)
        if current is not None:
            if record.handler == "memory" or current.handler == "memory":
                log.info(
                    "memory provider %s from %s ignored: the %s copy was found first",
                    key,
                    source,
                    current.source,
                )
                continue
            log.info("plugin %s from %s overrides the %s copy", key, source, current.source)
        result.records[key] = record
    return result


# -- toggling -----------------------------------------------------------------------------------


def _name_list(section: MutableMapping[str, Any], key: str) -> list[str]:
    value = section.get(key)
    if not isinstance(value, list):
        value = []
        section[key] = value
    return cast(list[str], value)


def set_enabled(
    tree: MutableMapping[str, Any], key: str, enabled: bool, toolsets: Sequence[str] = ()
) -> None:
    """Enable or disable ``key`` in a config tree (in place).

    Enabling adds it to ``plugins.enabled`` and drops it from ``plugins.disabled``; disabling does
    the reverse. The plugin's ``toolsets`` are added to (or removed from) every saved per-platform
    tool selection in ``platform_toolsets``; a platform with no saved selection is left alone.
    """
    section = tree.get("plugins")
    if not isinstance(section, MutableMapping):
        section = {}
        tree["plugins"] = section
    plugins = cast(MutableMapping[str, Any], section)
    add, drop = ("enabled", "disabled") if enabled else ("disabled", "enabled")
    wanted = _name_list(plugins, add)
    if key not in wanted:
        wanted.append(key)
    unwanted = _name_list(plugins, drop)
    unwanted[:] = [k for k in unwanted if k != key]
    selections = tree.get("platform_toolsets")
    if not toolsets or not isinstance(selections, MutableMapping):
        return
    for platform, chosen in cast(MutableMapping[str, Any], selections).items():
        if not isinstance(chosen, list):
            continue
        names = cast(list[str], chosen)
        if enabled:
            names.extend(t for t in toolsets if t not in names)
        else:
            names[:] = [t for t in names if t not in toolsets]
        log.debug("platform %s tool selection now %s", platform, names)
