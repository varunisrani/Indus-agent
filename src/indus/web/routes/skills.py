"""``/api/skills`` and ``/api/tools/toolsets``: the Skills page's backend (P18-14).

- ``GET /api/skills``: every loadable skill of the profile (plugin skills left out) with its
  category, source (``local``, ``bundled``, ``hub``, ``external``…) and whether it is enabled.
- ``POST /api/skills/toggle {name, enabled}``: writes ``skills.disabled`` through the config
  writer (P01); an unknown name is 404.
- ``POST /api/skills/create {name, content, category?}`` (W58-J2-3): one ``create`` through the
  ``skill_manage`` tool's own handler, so its name, format, path and lock checks, its
  ``skills.write_approval`` gate (a held write answers ``staged: true``) and its ledger all apply.
  A refusal is 400 ``skill_refused`` with the tool's reason; nothing is written then.
- ``GET /api/tools/toolsets``: the plain toolsets (no bundles, postures or ``core``) with their
  tools, and whether the CLI's tool selection (``platform_toolsets.cli``, else its bundle) offers
  every one of them; ``selection`` is that list as saved (``saved`` false: the bundle).
- ``PUT /api/tools/toolsets {toolsets}`` (W60-L2-2): saves ``platform_toolsets.cli`` through the
  config writer. Every name must be a toolset the resolver knows (a typo is 400
  ``unknown_toolset`` naming it, nothing written); an empty list is 400 (``indus tools`` is the
  place to switch every toolset off).
- ``GET /api/tools/keys`` (W60-L2-1): the key and address variables our tools declare
  (``indus.tools.key_needs``), per backend, each with ``is_set`` only; no value or preview.
- Hub (P13-26/27): ``GET /api/skills/hub/search?q=`` (catalogue and taps, then the live fallbacks;
  a search downloads nothing, so ``verdict`` is null), ``GET /api/skills/hub/inspect?identifier=``
  (fetch into quarantine, scan, report, discard), ``POST /api/skills/hub/install
  {identifier, force?}``, ``POST /api/skills/hub/uninstall {name}``, ``POST /api/skills/hub/update
  {name?}``. An install the policy blocks is 409 ``scan_blocked`` with the verdict: ``dangerous``
  is never installed, ``force`` lifts ``caution`` only. Network trouble is 502 ``hub_unavailable``.

Hub work runs on its own event loop in the worker thread FastAPI gives a sync route. Seam for
tests (``DashboardState.extras``): ``skills.hub_sources`` (``Scope -> SourceSet``).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine, Mapping
from typing import Annotated, Any, Final, NoReturn, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from indus.foundation import config as config_module
from indus.foundation.errors import ConfigError, ConfigManaged
from indus.foundation.redact import redact_text
from indus.foundation.scope import Scope
from indus.learning.skills import bundled_sync
from indus.learning.skills.hub import install as hub
from indus.learning.skills.hub.lock import HubLock
from indus.learning.skills.hub.sources import HubError, SourceSet
from indus.learning.skills.manage_tool import skill_manage
from indus.learning.skills.resolver import TIER_LOCAL, SkillRecord, load_catalog
from indus.learning.skills.usage import hub_names
from indus.tools import toolsets
from indus.tools.key_needs import tool_key_needs
from indus.web.routes.config_env import set_flags
from indus.web.scope import read_scope, write_scope
from indus.web.state import DashboardState, dashboard_state

__all__ = ["ROUTE_LIST", "router"]

router = APIRouter()

ROUTE_LIST: Final[tuple[tuple[str, str, bool], ...]] = (
    ("GET", "/api/skills", True),
    ("POST", "/api/skills/toggle", True),
    ("POST", "/api/skills/create", True),
    ("GET", "/api/tools/toolsets", True),
    ("PUT", "/api/tools/toolsets", True),
    ("GET", "/api/tools/keys", True),
    ("GET", "/api/skills/hub/search", True),
    ("GET", "/api/skills/hub/inspect", True),
    ("POST", "/api/skills/hub/install", True),
    ("POST", "/api/skills/hub/uninstall", True),
    ("POST", "/api/skills/hub/update", True),
)
"""``(method, path, scoped)`` of every route here."""

ReadScope = Annotated[Scope, Depends(read_scope)]
WriteScope = Annotated[Scope, Depends(write_scope)]

SEARCH_LIMIT: Final = 50
TOOLSET_PLATFORM: Final = "cli"


def _fail(status: int, code: str, message: str, **extra: Any) -> NoReturn:
    raise HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


def _configured(scope: Scope) -> Scope:
    """``scope`` carrying the settings as they are on disk now."""
    view = config_module.load(scope)
    if view.error is not None:
        _fail(500, "config_unreadable", "config.yaml cannot be read right now; fix it first.")
    return scope.with_config(view)


# -- installed skills ------------------------------------------------------------------------------


def _source(record: SkillRecord, shipped: set[str], from_hub: set[str]) -> str:
    if record.tier == TIER_LOCAL and record.name in from_hub:
        return "hub"
    if record.tier == TIER_LOCAL and record.name in shipped:
        return "bundled"
    return record.tier_name


def _skills(scope: Scope) -> list[SkillRecord]:
    return [r for r in load_catalog(scope).loadable() if r.plugin is None]


@router.get("/api/skills")
def list_skills(scope: ReadScope) -> dict[str, Any]:
    live = _configured(scope)
    shipped = set(bundled_sync.read_manifest(live.home))
    from_hub = hub_names(hub.skills_root(live))
    rows = [
        {
            "name": record.load_name or record.name,
            "description": record.description,
            "category": record.category or "",
            "enabled": not record.disabled,
            "source": _source(record, shipped, from_hub),
        }
        for record in _skills(live)
    ]
    return {"skills": rows}


class SkillToggle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    enabled: bool


def _disabled_list(scope: Scope) -> list[str]:
    raw: object = config_module.load(scope).get("skills.disabled", [])
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw]  # pyright: ignore[reportUnknownVariableType]


@router.post("/api/skills/toggle")
def toggle_skill(body: SkillToggle, scope: WriteScope) -> dict[str, Any]:
    live = _configured(scope)
    names = {r.load_name or r.name for r in _skills(live)} | {r.name for r in _skills(live)}
    if body.name not in names:
        _fail(404, "skill_not_found", "No skill has this name.")
    current = _disabled_list(live)
    wanted = [n for n in current if n != body.name]
    if not body.enabled:
        wanted.append(body.name)
    if wanted != current:
        try:
            config_module.save(live, {"skills": {"disabled": wanted}})
        except ConfigError as exc:
            _fail(500, "config_unwritable", f"{exc.message}. Nothing was changed.")
    return {"ok": True, "name": body.name, "enabled": body.enabled}


class SkillCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=256 * 1024)
    category: str | None = Field(default=None, max_length=64)


@router.post("/api/skills/create")
def create_skill(body: SkillCreate, scope: WriteScope) -> dict[str, Any]:
    live = _configured(scope)
    args: dict[str, Any] = {"action": "create", "name": body.name, "content": body.content}
    category = (body.category or "").strip()
    if category:
        args["category"] = category
    raw: object = json.loads(skill_manage(args, scope=live))
    answer = cast(dict[str, Any], raw) if isinstance(raw, dict) else {"error": None}
    if "error" in answer:
        reason = answer.get("error") or "The skill was not saved."
        _fail(400, "skill_refused", redact_text(str(reason)))
    staged = answer.get("staged") is True
    return {
        "ok": True,
        "name": body.name.strip(),
        "staged": staged,
        "message": redact_text(str(answer.get("message") or "")),
    }


# -- toolsets --------------------------------------------------------------------------------------


def _plain_toolsets() -> list[str]:
    rows: list[str] = []
    for name, entry in sorted(toolsets.TOOLSETS.items()):
        if entry.includes or entry.posture or name == toolsets.CORE:
            continue
        if name.startswith(toolsets.BUNDLE_PREFIX):
            continue
        rows.append(name)
    return rows


def _saved_selection(scope: Scope) -> list[str] | None:
    """``platform_toolsets.cli`` as saved; ``None`` when the CLI uses its bundle."""
    saved: object = config_module.load(scope).user_raw().get("platform_toolsets")
    if isinstance(saved, Mapping):
        chosen: object = saved.get(TOOLSET_PLATFORM)  # pyright: ignore[reportUnknownMemberType]
        if isinstance(chosen, list):
            return [str(n) for n in chosen]  # pyright: ignore[reportUnknownVariableType]
    return None


def _cli_selection(scope: Scope) -> list[str]:
    saved = _saved_selection(scope)
    return saved if saved is not None else [f"{toolsets.BUNDLE_PREFIX}{TOOLSET_PLATFORM}"]


@router.get("/api/tools/toolsets")
def list_toolsets(scope: ReadScope) -> dict[str, Any]:
    saved = _saved_selection(scope)
    chosen = _cli_selection(scope)
    offered = toolsets.select(chosen, scope=scope, platform=TOOLSET_PLATFORM).tools
    rows: list[dict[str, Any]] = []
    for name in _plain_toolsets():
        entry = toolsets.TOOLSETS[name]
        tools = sorted(toolsets.resolve(name, None, scope))
        rows.append(
            {
                "name": name,
                "label": name.replace("_", " ").replace("-", " ").title(),
                "description": entry.description,
                "enabled": bool(tools) and set(tools) <= offered,
                "tools": tools,
            }
        )
    selection = {
        "platform": TOOLSET_PLATFORM,
        "toolsets": chosen,
        "saved": saved is not None,
    }
    return {"toolsets": rows, "selection": selection}


class ToolsetSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    toolsets: list[str] = Field(max_length=200)


@router.put("/api/tools/toolsets")
def save_toolsets(body: ToolsetSelection, scope: WriteScope) -> dict[str, Any]:
    live = _configured(scope)
    names = list(dict.fromkeys(n.strip() for n in body.toolsets if n.strip()))
    if not names:
        _fail(400, "empty_selection", "Name at least one toolset. Nothing was changed.")
    unknown = [n for n in names if not toolsets.is_known(n, None, live)]
    if unknown:
        _fail(
            400,
            "unknown_toolset",
            f"Not a toolset: {', '.join(unknown)}. Nothing was changed.",
            names=unknown,
        )
    try:
        config_module.save(live, {"platform_toolsets": {TOOLSET_PLATFORM: names}})
    except ConfigManaged:
        _fail(403, "config_managed", "This installation is managed; nothing was changed.")
    except ConfigError as exc:
        _fail(500, "config_unwritable", f"{exc.message}. Nothing was changed.")
    return {"ok": True, "platform": TOOLSET_PLATFORM, "toolsets": names}


# -- tool keys -------------------------------------------------------------------------------------


@router.get("/api/tools/keys")
def list_tool_keys(scope: ReadScope) -> dict[str, Any]:
    groups = tool_key_needs(None, scope)
    flags = set_flags(scope, {need.name for group in groups for need in group.needs})
    return {
        "tools": [
            {
                "toolset": group.toolset,
                "backend": group.backend,
                "label": group.label,
                "vars": [
                    {"name": need.name, "kind": need.kind, "is_set": flags[need.name]}
                    for need in group.needs
                ],
            }
            for group in groups
        ]
    }


# -- hub -------------------------------------------------------------------------------------------


def _hub[T](
    state: DashboardState, scope: Scope, work: Callable[[SourceSet], Coroutine[Any, Any, T]]
) -> T:
    """Run ``work`` with fresh hub sources on its own loop (this is a worker thread)."""
    factory: object = state.extras.get("skills.hub_sources")

    async def main() -> T:
        if callable(factory):
            sources: SourceSet = factory(scope)  # pyright: ignore[reportAssignmentType]
        else:
            from indus.learning.skills.hub.sources import build_sources

            sources = build_sources(scope)
        try:
            return await work(sources)
        finally:
            await sources.aclose()

    try:
        return asyncio.run(main())
    except HubError as exc:
        _fail(502, "hub_unavailable", redact_text(str(exc)))


def _installed(scope: Scope) -> tuple[set[str], set[str]]:
    entries = HubLock(hub.skills_root(scope)).installed()
    identifiers = {str(e.get("identifier", "")) for e in entries.values()}
    return set(entries), identifiers


@router.get("/api/skills/hub/search")
def hub_search(request: Request, scope: ReadScope, q: str = "") -> dict[str, Any]:
    query = q.strip()
    if not query:
        return {"results": []}
    from indus.learning.skills.hub.sources import search_sources

    live = _configured(scope)
    state = dashboard_state(request.app)
    outcome = _hub(state, live, lambda s: search_sources(s, query, limit=SEARCH_LIMIT))
    names, identifiers = _installed(live)
    results = [
        {
            "identifier": row.identifier,
            "name": row.name,
            "description": row.description,
            "source": row.source,
            "trust": row.trust,
            "verdict": None,
            "installed": row.identifier in identifiers or row.name in names,
            "update_available": False,
        }
        for row in outcome.rows
    ]
    return {"results": results, "timed_out": list(outcome.timed_out)}


@router.get("/api/skills/hub/inspect")
def hub_inspect(request: Request, scope: ReadScope, identifier: str) -> dict[str, Any]:
    live = _configured(scope)
    state = dashboard_state(request.app)
    preview = _hub(state, live, lambda s: hub.inspect(live, s, identifier))
    return {
        "identifier": preview.bundle.identifier,
        "name": preview.name,
        "source": preview.bundle.source,
        "trust": preview.bundle.trust,
        "verdict": preview.report.verdict,
        "findings": len(preview.report.findings),
        "categories": list(preview.report.categories),
        "allowed": preview.allowed,
        "reason": preview.reason,
    }


class HubInstall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identifier: str = Field(min_length=1, max_length=2048)
    force: bool = False


@router.post("/api/skills/hub/install")
def hub_install(request: Request, body: HubInstall, scope: WriteScope) -> dict[str, Any]:
    live = _configured(scope)
    state = dashboard_state(request.app)
    options = hub.InstallOptions(force=body.force)
    result = _hub(state, live, lambda s: hub.install(live, s, body.identifier, options))
    verdict = result.preview.report.verdict if result.preview is not None else None
    message = redact_text(result.message)
    if result.status == "blocked":
        _fail(409, "scan_blocked", message, verdict=verdict)
    if result.status == "exists":
        _fail(409, "already_installed", message, verdict=verdict)
    if not result.ok:
        _fail(400, "install_failed", message or "The skill could not be installed.")
    return {"ok": True, "name": result.name, "verdict": verdict}


class HubName(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)


@router.post("/api/skills/hub/uninstall")
def hub_uninstall(body: HubName, scope: WriteScope) -> dict[str, Any]:
    live = _configured(scope)
    try:
        message = hub.uninstall(live, body.name)
    except HubError as exc:
        _fail(404, "not_a_hub_skill", redact_text(str(exc)))
    return {"ok": True, "message": message}


class HubUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)


@router.post("/api/skills/hub/update")
def hub_update(
    request: Request, scope: WriteScope, body: HubUpdate | None = None
) -> dict[str, Any]:
    live = _configured(scope)
    state = dashboard_state(request.app)
    name = body.name if body is not None else None
    report = _hub(state, live, lambda s: hub.update(live, s, name))
    return {
        "ok": not report.failed,
        "updated": report.updated,
        "kept_local": report.kept_local,
        "failed": [{"name": n, "message": redact_text(m)} for n, m in report.failed],
    }
