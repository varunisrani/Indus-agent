"""Dashboard plugins: discovery, manifests, static assets and in-process API routes (P24-37).

A plugin's dashboard half is a ``dashboard/`` folder beside its agent half, holding
``manifest.json``, a JS entry (a classic script), optional CSS and an optional Python ``api``
module. ``DashboardPlugins`` (one per app, cached; ``rescan()`` refreshes) finds them in this order
— the first copy of a name wins:

1. user: ``<home>/plugins/<dir>/dashboard/manifest.json``;
2. bundled: the P13 bundled plugin folder;
3. project: ``<cwd>/.indus/plugins/<dir>/dashboard/`` only when ``INDUS_ENABLE_PROJECT_PLUGINS``
   is ``1/true/yes/on`` in the scope's env.

Which plugins are on follows P13's lists: ``plugins.disabled`` always wins; a user or project
plugin must be in ``plugins.enabled``; a bundled one is on unless disabled. Only plugins that are
on are listed and served. A project plugin's ``api`` is never imported (code from a working
folder never runs on the server); a user plugin's only while it is enabled.

Manifest rules (``normalise``): ``name`` matches ``[a-z0-9][a-z0-9_-]{0,63}`` (default: the folder
name); ``entry``, ``css`` and ``api`` are relative paths that stay inside ``dashboard/`` (an unsafe
``api`` drops only the backend; an unsafe entry drops the plugin); ``tab.path`` is ``/<segment>``
and may not be a built-in path unless it is the ``override``; ``override`` must name a built-in
page and never ``/plugins`` (the page that manages plugins); ``tab.position`` is ``end``,
``after:<seg>`` or ``before:<seg>``; ``icon`` is a PascalCase icon name, else ``Puzzle``;
``integrity`` is kept only when it is a well-formed SRI value; ``slots`` keeps slot-shaped names.

Serving: ``GET /api/dashboard/plugins`` lists the manifests (no internal fields);
``POST /api/dashboard/plugins/rescan`` scans again and mounts the API routes of plugins enabled
since. Assets: ``GET /dashboard-plugins/<name>/<path>`` from that plugin's ``dashboard/`` folder;
``..``, a backslash, NUL, an absolute part or a path that resolves outside the folder → 404, and
so does an unknown or switched-off plugin. A plugin's ``api`` module exports ``router`` (an
``APIRouter``); it is imported in this process (D-16.46) and mounted under ``/api/ext/<name>``,
the prefix the plugin-route gate guards: the login gate answers 401 first, then the plugin-route
gate 404 for a plugin that is not on (R09.4).
"""

from __future__ import annotations

import importlib.util
import json
import logging
import mimetypes
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Final, cast

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from indus.foundation import config as config_module
from indus.foundation.scope import Scope
from indus.plugins.discovery import BUNDLED_DIR, PROJECT_PLUGINS_ENV
from indus.plugins.settings import plugins_setting, string_set
from indus.web.auth.gate import PLUGIN_ROUTE_PREFIX
from indus.web.state import dashboard_state

__all__ = [
    "ASSET_PREFIX",
    "BUILTIN_PATHS",
    "EXTRAS_KEY",
    "LIST_PATH",
    "PROTECTED_PATHS",
    "RESCAN_PATH",
    "ROUTE_LIST",
    "DashboardPlugin",
    "DashboardPlugins",
    "install",
    "normalise",
    "router",
]

log = logging.getLogger(__name__)

EXTRAS_KEY: Final = "dashboard_plugins"
LIST_PATH: Final = "/api/dashboard/plugins"
RESCAN_PATH: Final = "/api/dashboard/plugins/rescan"
ASSET_PREFIX: Final = "/dashboard-plugins"

ROUTE_LIST: Final[tuple[tuple[str, str, bool], ...]] = (
    ("GET", LIST_PATH, False),
    ("POST", RESCAN_PATH, False),
)
"""``(method, path, scoped)``: the catalogue is the backend's own, never per request."""

BUILTIN_PATHS: Final = frozenset(
    {
        "/chat",
        "/sessions",
        "/files",
        "/analytics",
        "/models",
        "/logs",
        "/cron",
        "/skills",
        "/plugins",
        "/mcp",
        "/channels",
        "/webhooks",
        "/pairing",
        "/profiles",
        "/config",
        "/env",
        "/system",
        "/docs",
    }
)
"""The SPA's own pages (``apps/web/src/shell/routes.ts``)."""
PROTECTED_PATHS: Final = frozenset({"/plugins"})
"""Built-in pages no plugin may override."""

_NAME: Final = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SEGMENT: Final = re.compile(r"^/[a-z0-9][a-z0-9_-]{0,63}$")
_ICON: Final = re.compile(r"^[A-Z][A-Za-z0-9]{0,63}$")
_SRI: Final = re.compile(r"^(sha256|sha384|sha512)-[A-Za-z0-9+/]+={0,2}$")
_SLOT: Final = re.compile(r"^[a-z][a-z-]{0,63}(:(top|bottom))?$")
_TRUE: Final = frozenset({"1", "true", "yes", "on"})
_SETTINGS_TTL_S: Final = 2.0
DEFAULT_ICON: Final = "Puzzle"
DEFAULT_ENTRY: Final = "dist/index.js"


@dataclass(frozen=True)
class DashboardPlugin:
    """One discovered plugin, normalised; ``folder``/``api_file`` never leave the server."""

    name: str
    label: str
    description: str
    icon: str
    version: str
    tab: Mapping[str, Any]
    slots: tuple[str, ...]
    entry: str
    css: str | None
    integrity: str | None
    source: str
    folder: Path
    api_file: str | None = None

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "icon": self.icon,
            "version": self.version,
            "tab": dict(self.tab),
            "slots": list(self.slots),
            "entry": self.entry,
            "css": self.css,
            "has_api": self.api_file is not None and self.source != "project",
            "source": self.source,
        }
        if self.integrity is not None:
            out["integrity"] = self.integrity
        return out


class ManifestRefused(ValueError):
    """The manifest cannot be used; the message says why (logged, the plugin is skipped)."""


def _safe_relative(raw: object, folder: Path) -> str | None:
    """``raw`` when it is a relative POSIX path that stays inside ``folder``, else None."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if "\\" in text or "\x00" in text or text.startswith("/"):
        return None
    pure = PurePosixPath(text)
    if pure.is_absolute() or ".." in pure.parts or (pure.parts and ":" in pure.parts[0]):
        return None
    try:
        (folder / text).resolve().relative_to(folder.resolve())
    except (OSError, ValueError):
        return None
    return text


def _position(raw: object) -> str:
    if isinstance(raw, str):
        text = raw.strip()
        if text == "end":
            return text
        kind, _, segment = text.partition(":")
        if kind in ("after", "before") and _SEGMENT.match(f"/{segment}"):
            return text
    return "end"


def normalise(data: Mapping[str, Any], folder: Path, source: str) -> DashboardPlugin:
    """The plugin a manifest describes (see the module notes); ``ManifestRefused`` otherwise."""
    name = data.get("name", folder.parent.name)
    if not isinstance(name, str) or not _NAME.match(name):
        raise ManifestRefused(f"name {name!r} is not a plugin name")
    raw_tab: object = data.get("tab")
    tab_in = cast(Mapping[str, Any], raw_tab) if isinstance(raw_tab, Mapping) else {}
    path = tab_in.get("path", f"/{name}")
    if not isinstance(path, str) or not _SEGMENT.match(path):
        raise ManifestRefused(f"tab.path {path!r} must be one segment like /{name}")
    tab: dict[str, Any] = {"path": path, "position": _position(tab_in.get("position"))}
    override = tab_in.get("override")
    if override is not None:
        if not isinstance(override, str) or override not in BUILTIN_PATHS:
            raise ManifestRefused(f"tab.override {override!r} is not a built-in page")
        if override in PROTECTED_PATHS:
            raise ManifestRefused(f"{override} cannot be overridden")
        tab["override"] = override
    elif path in BUILTIN_PATHS:
        raise ManifestRefused(f"tab.path {path} is a built-in page (use tab.override)")
    if tab_in.get("hidden") is True:
        tab["hidden"] = True
    entry = _safe_relative(data.get("entry", DEFAULT_ENTRY), folder)
    if entry is None:
        raise ManifestRefused("entry must be a file inside the plugin's dashboard folder")
    css_raw = data.get("css")
    css = _safe_relative(css_raw, folder) if css_raw is not None else None
    if css_raw is not None and css is None:
        raise ManifestRefused("css must be a file inside the plugin's dashboard folder")
    api_raw = data.get("api")
    api = _safe_relative(api_raw, folder) if api_raw is not None else None
    if api_raw is not None and api is None:
        log.warning("dashboard plugin %s: unsafe api path refused; no backend routes", name)
    icon = data.get("icon")
    integrity = data.get("integrity")
    slots_raw: object = data.get("slots")
    slots = (
        tuple(s for s in cast(list[object], slots_raw) if isinstance(s, str) and _SLOT.match(s))
        if isinstance(slots_raw, list)
        else ()
    )
    label = data.get("label", name)
    description = data.get("description", "")
    version = data.get("version", "0.0.0")
    return DashboardPlugin(
        name=name,
        label=label if isinstance(label, str) and label.strip() else name,
        description=description if isinstance(description, str) else "",
        icon=icon if isinstance(icon, str) and _ICON.match(icon) else DEFAULT_ICON,
        version=version if isinstance(version, str) else "0.0.0",
        tab=tab,
        slots=slots,
        entry=entry,
        css=css,
        integrity=(
            integrity
            if isinstance(integrity, str)
            and all(_SRI.match(p) for p in integrity.split())
            and integrity.strip()
            else None
        ),
        source=source,
        folder=folder,
        api_file=api,
    )


def _children(root: Path) -> list[Path]:
    try:
        return sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name)
    except OSError:
        return []


@dataclass
class DashboardPlugins:
    """The backend's plugin catalogue (see the module notes)."""

    scope: Scope
    bundled_dir: Path = BUNDLED_DIR
    project_root: Callable[[], Path] = Path.cwd
    importer: Callable[[str, Path], Any] | None = None
    monotonic: Callable[[], float] = time.monotonic
    _found: list[DashboardPlugin] | None = field(default=None, repr=False)
    _mounted: set[str] = field(default_factory=set[str], repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _settings: tuple[float, frozenset[str], frozenset[str]] | None = field(default=None, repr=False)

    # -- discovery --

    def _sources(self) -> list[tuple[Path, str]]:
        sources = [(self.scope.home / "plugins", "user"), (self.bundled_dir, "bundled")]
        if self.scope.env.get(PROJECT_PLUGINS_ENV, "").strip().lower() in _TRUE:
            sources.append((self.project_root() / ".indus" / "plugins", "project"))
        return sources

    def _scan(self) -> list[DashboardPlugin]:
        found: list[DashboardPlugin] = []
        seen: set[str] = set()
        for root, source in self._sources():
            for child in _children(root):
                manifest = child / "dashboard" / "manifest.json"
                if not manifest.is_file():
                    continue
                try:
                    data: object = json.loads(manifest.read_text(encoding="utf-8-sig"))
                    if not isinstance(data, dict):
                        raise ManifestRefused("the manifest is not a JSON object")
                    plugin = normalise(cast(dict[str, Any], data), child / "dashboard", source)
                except (OSError, ValueError) as exc:
                    log.warning("dashboard plugin %s skipped: %s", child.name, exc)
                    continue
                if plugin.name in seen:
                    continue
                seen.add(plugin.name)
                found.append(plugin)
        return found

    def all(self) -> list[DashboardPlugin]:
        """Every discovered plugin, on or off (cached)."""
        with self._lock:
            if self._found is None:
                self._found = self._scan()
            return list(self._found)

    def rescan(self) -> list[DashboardPlugin]:
        with self._lock:
            self._found = self._scan()
            self._settings = None
        return self.listed()

    # -- the on/off rule --

    def _lists(self) -> tuple[frozenset[str], frozenset[str]]:
        now = self.monotonic()
        cached = self._settings
        if cached is not None and now - cached[0] < _SETTINGS_TTL_S:
            return cached[1], cached[2]
        try:
            scope = self.scope.with_config(config_module.load(self.scope))
            enabled = string_set(plugins_setting(scope, "enabled", []))
            disabled = string_set(plugins_setting(scope, "disabled", []))
        except Exception:
            log.warning("plugin settings could not be read; dashboard plugins are off")
            enabled, disabled = frozenset[str](), frozenset({"*"})
        self._settings = (now, enabled, disabled)
        return enabled, disabled

    def is_on(self, plugin: DashboardPlugin) -> bool:
        enabled, disabled = self._lists()
        names = {plugin.name, plugin.folder.parent.name}
        if "*" in disabled or names & disabled:
            return False
        if plugin.source == "bundled":
            return True
        return bool(names & enabled)

    def listed(self) -> list[DashboardPlugin]:
        return [p for p in self.all() if self.is_on(p)]

    def get(self, name: str) -> DashboardPlugin | None:
        for plugin in self.all():
            if plugin.name == name:
                return plugin if self.is_on(plugin) else None
        return None

    def api_allowed(self, name: str) -> bool:
        """For the plugin-route gate: the plugin is on, has a mounted backend, and is no
        project plugin."""
        plugin = self.get(name)
        return plugin is not None and plugin.source != "project" and name in self._mounted

    # -- assets --

    def asset(self, name: str, relative: str) -> Path | None:
        plugin = self.get(name)
        if plugin is None:
            return None
        safe = _safe_relative(relative, plugin.folder)
        if safe is None:
            return None
        target = (plugin.folder / safe).resolve()
        return target if target.is_file() else None

    # -- backends --

    def _import(self, plugin: DashboardPlugin) -> APIRouter | None:
        assert plugin.api_file is not None
        path = plugin.folder / plugin.api_file
        if self.importer is not None:
            module = self.importer(plugin.name, path)
        else:
            spec = importlib.util.spec_from_file_location(
                f"indus_dashboard_plugin_{plugin.name.replace('-', '_')}", path
            )
            if spec is None or spec.loader is None:
                raise ImportError(f"{path.name} is not a Python module")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        found: object = getattr(module, "router", None)
        return found if isinstance(found, APIRouter) else None

    def mount(self, app: FastAPI) -> list[str]:
        """Import and mount the backend of every plugin that is on and not mounted yet."""
        done: list[str] = []
        for plugin in self.listed():
            if plugin.api_file is None or plugin.source == "project":
                continue
            if plugin.name in self._mounted:
                continue
            try:
                plugin_router = self._import(plugin)
            except Exception as exc:
                log.warning("dashboard plugin %s: api not mounted (%s)", plugin.name, exc)
                continue
            if plugin_router is None:
                log.warning("dashboard plugin %s: api has no APIRouter `router`", plugin.name)
                continue
            prefix = f"{PLUGIN_ROUTE_PREFIX.rstrip('/')}/{plugin.name}"
            app.include_router(plugin_router, prefix=prefix)
            _move_catch_alls_last(app)
            self._mounted.add(plugin.name)
            done.append(plugin.name)
        return done


_CATCH_ALL_PATHS: Final = frozenset({"/api", "/api/{rest:path}", "/{rest:path}"})


def _move_catch_alls_last(app: FastAPI) -> None:
    """Routes added after the SPA mount must still win over its catch-alls."""
    routes = app.router.routes
    catch = [r for r in routes if getattr(r, "path", None) in _CATCH_ALL_PATHS]
    if not catch:
        return
    kept = [r for r in routes if r not in catch]
    routes[:] = [*kept, *catch]


# -- routes ------------------------------------------------------------------------------------

router = APIRouter()


def _catalogue(request: Request) -> DashboardPlugins:
    found: object = dashboard_state(request.app).extras.get(EXTRAS_KEY)
    if not isinstance(found, DashboardPlugins):
        raise RuntimeError("the dashboard plugin catalogue is not installed")
    return found


def _listing(plugins: Iterable[DashboardPlugin]) -> JSONResponse:
    return JSONResponse(
        {"plugins": [p.public() for p in plugins]}, headers={"cache-control": "no-store"}
    )


@router.get(LIST_PATH)
async def list_plugins(request: Request) -> JSONResponse:
    return _listing(_catalogue(request).listed())


@router.post(RESCAN_PATH)
async def rescan_plugins(request: Request) -> JSONResponse:
    catalogue = _catalogue(request)
    listed = catalogue.rescan()
    catalogue.mount(request.app)
    return _listing(listed)


def _not_found() -> JSONResponse:
    return JSONResponse({"error": "not_found", "detail": "No such plugin file."}, status_code=404)


@router.get(ASSET_PREFIX + "/{name}/{rest:path}", include_in_schema=False)
async def plugin_asset(name: str, rest: str, request: Request) -> Response:
    raw_path: str = request.scope.get("raw_path", b"").decode("latin-1")
    if "%2e" in raw_path.lower() or "%2f" in raw_path.lower() or "%5c" in raw_path.lower():
        return _not_found()
    found = _catalogue(request).asset(name, rest)
    if found is None:
        return _not_found()
    media, _ = mimetypes.guess_type(found.name)
    if found.suffix in (".js", ".mjs"):
        media = "text/javascript"
    return FileResponse(
        found,
        media_type=media or "application/octet-stream",
        headers={"cache-control": "no-cache", "x-content-type-options": "nosniff"},
    )


def install(app: FastAPI, catalogue: DashboardPlugins) -> None:
    """Put ``catalogue`` on the app and mount the backends of the plugins that are on."""
    dashboard_state(app).extras[EXTRAS_KEY] = catalogue
    catalogue.mount(app)
