"""``/api/plugins``: the Plugins page's backend (P18-14, over P13's plugin library).

- ``GET /api/plugins``: every plugin the profile can see (bundled, user, entry points; project
  plugins are a session matter) with its state, version, description, where it came from and the
  commit it is pinned to.
- ``POST /api/plugins/install {url, sha}``: a git install (``owner/repo[/sub]`` or a git URL;
  never a local folder or ``file://``) pinned to a full 40-character commit SHA, checked before any
  git command runs. The P13-20 scan decides: a block (``dangerous`` always, ``caution`` unless
  ``force``) is 409 ``scan_blocked`` with the verdict. An install never enables the plugin.
- ``POST /api/plugins/{name}/enable|disable``: through ``plugins.discovery.set_enabled`` and the
  config writer. An enable that needs Python packages or a memory provider (those need a terminal
  to consent or are chosen elsewhere) is refused with a code; capabilities the plugin declares and
  the person has not granted stay ungranted and are listed as ``capabilities_waiting``.

Seam for tests (``DashboardState.extras``): ``plugins.installer`` (``Scope -> Installer``-like:
``install(identifier, ref=, force=, confirm=)`` and ``records()``).
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Final, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from indus.foundation import config as config_module
from indus.foundation.errors import ConfigError
from indus.foundation.redact import redact_text
from indus.foundation.scope import Scope
from indus.plugins import consent
from indus.plugins.discovery import PluginRecord, discover, set_enabled
from indus.plugins.install import InstallBlocked, Installer, InstallError
from indus.web.scope import read_scope, write_scope
from indus.web.state import dashboard_state

__all__ = ["ROUTE_LIST", "router"]

router = APIRouter()

ROUTE_LIST: Final[tuple[tuple[str, str, bool], ...]] = (
    ("GET", "/api/plugins", True),
    ("POST", "/api/plugins/install", True),
    ("POST", "/api/plugins/{name}/enable", True),
    ("POST", "/api/plugins/{name}/disable", True),
)
"""``(method, path, scoped)`` of every route here."""

ReadScope = Annotated[Scope, Depends(read_scope)]
WriteScope = Annotated[Scope, Depends(write_scope)]

_FULL_SHA: Final = re.compile(r"^[0-9a-fA-F]{40}$")
_GIT_URL: Final = re.compile(r"^(?:https?|ssh|git)://[^\s]+$")
_SCP_LIKE: Final = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^\s]+$")
_OWNER_REPO: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]+(?:/[^/\s]+)*/?$")


def _fail(status: int, code: str, message: str, **extra: Any) -> NoReturn:
    raise HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


def _configured(scope: Scope) -> Scope:
    view = config_module.load(scope)
    if view.error is not None:
        _fail(500, "config_unreadable", "config.yaml cannot be read right now; fix it first.")
    return scope.with_config(view)


def _installer(request: Request, scope: Scope) -> Any:
    factory: object = dashboard_state(request.app).extras.get("plugins.installer")
    if callable(factory):
        return factory(scope)
    return Installer(scope)


def _records(scope: Scope) -> list[PluginRecord]:
    found = discover(scope, project_root=scope.home / ".no-project")
    return [record for _key, record in sorted(found.records.items())]


@router.get("/api/plugins")
def list_plugins(request: Request, scope: ReadScope) -> dict[str, Any]:
    live = _configured(scope)
    try:
        installed: dict[str, dict[str, Any]] = _installer(request, live).records()
    except InstallError:
        installed = {}
    rows: list[dict[str, Any]] = []
    for record in _records(live):
        manifest = record.manifest
        meta = installed.get(record.name, {})
        revision = str(meta.get("revision") or "") or None
        rows.append(
            {
                "name": record.name,
                "key": record.key,
                "version": (manifest.version or None) if manifest is not None else None,
                "description": manifest.description if manifest is not None else "",
                "enabled": record.state == "enabled",
                "state": record.state,
                "kind": record.kind,
                "source": redact_text(str(meta.get("source") or "")) or record.source,
                "sha": revision,
                "pinned": meta.get("pinned") is True,
            }
        )
    return {"plugins": rows}


class PluginInstall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2048)
    sha: str = Field(min_length=1, max_length=64)
    force: bool = False


def _git_source(text: str) -> str:
    value = text.strip()
    if _GIT_URL.match(value) or _SCP_LIKE.match(value) or _OWNER_REPO.match(value):
        return value
    _fail(
        400,
        "invalid_source",
        "Give owner/repo or a git URL (https://, ssh://, git@host:path); local folders are "
        "installed from a terminal.",
    )


@router.post("/api/plugins/install")
def install_plugin(request: Request, body: PluginInstall, scope: WriteScope) -> dict[str, Any]:
    if not _FULL_SHA.match(body.sha.strip()):
        _fail(422, "invalid_sha", "A full 40-character commit SHA is required.")
    source = _git_source(body.url)
    live = _configured(scope)
    try:
        result = _installer(request, live).install(
            source, ref=body.sha.strip().lower(), force=body.force, confirm=None
        )
    except InstallBlocked as exc:
        verdict = exc.report.verdict if exc.report is not None else None
        _fail(409, "scan_blocked", redact_text(str(exc)), verdict=verdict)
    except InstallError as exc:
        hint = f" ({exc.hint})" if exc.hint else ""
        _fail(400, "install_failed", redact_text(f"{exc}{hint}"))
    return {
        "ok": True,
        "name": result.name,
        "sha": str(result.record.get("revision") or "") or None,
        "verdict": result.report.verdict,
        "enabled": False,
    }


def _find(scope: Scope, name: str) -> PluginRecord:
    for record in _records(scope):
        if name in (record.key, record.name):
            return record
    _fail(404, "plugin_not_found", "No plugin has this name.")


def _save(scope: Scope, raw: dict[str, Any]) -> None:
    try:
        config_module.save(scope, raw, replace=True)
    except ConfigError as exc:
        _fail(500, "config_unwritable", f"{exc.message}. Nothing was changed.")


@router.post("/api/plugins/{name}/enable")
def enable_plugin(name: str, scope: WriteScope) -> dict[str, Any]:
    live = _configured(scope)
    record = _find(live, name)
    manifest = record.manifest
    if manifest is None:
        _fail(409, "plugin_broken", "The plugin's manifest is missing or broken.")
    if record.handler == "memory":
        _fail(409, "memory_provider", "This is a memory provider; choose it as memory.provider.")
    if manifest.python_dependencies:
        _fail(
            409,
            "dependencies_required",
            "This plugin needs Python packages; enable it with `indus plugins enable` on a "
            "terminal, which asks before installing them.",
        )
    raw = config_module.load(live).user_raw()
    set_enabled(raw, record.key, True)
    waiting = consent.consent_capabilities(raw, manifest, ask=None).waiting
    _save(live, raw)
    return {"ok": True, "name": record.name, "enabled": True, "capabilities_waiting": waiting}


@router.post("/api/plugins/{name}/disable")
def disable_plugin(name: str, scope: WriteScope) -> dict[str, Any]:
    live = _configured(scope)
    record = _find(live, name)
    raw = config_module.load(live).user_raw()
    set_enabled(raw, record.key, False)
    memory: object = raw.get("memory")
    if isinstance(memory, dict) and memory.get("provider") == record.name:  # pyright: ignore[reportUnknownMemberType]
        memory["provider"] = ""
    _save(live, raw)
    return {"ok": True, "name": record.name, "enabled": False}
