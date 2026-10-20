"""``/api/messaging`` and ``/api/pairing``: the Channels and Pairing pages (P18-15, R06.15).

Channels. One row per platform the gateway knows. The gateway is a sibling layer, so its registry
(``gateway.registry``) and its settings reader (``gateway.config``) are loaded by module name, as
the CLI's setup wizard does; their rules stay the only ones (an explicit ``enabled: false`` beats
credentials, R06.15). Every request reads a fresh env view of the profile (``.env`` re-read), so
an answer right after a write already shows it.

A platform's fields are of three kinds: ``credential`` (kept in the profile's secret store, shown
only as set/unset with a masked preview), ``setting`` (a value the platform needs that is not a
secret, in ``.env``, shown as is) and ``policy`` (allowed users, allow-all, allow bots, home
channel; ``.env``). ``PUT /api/messaging/platforms/{platform} {enabled?, env?, clear_env?}`` checks
the whole body before writing anything: every name must be one of that platform's fields, and a
credential sent back as its own masked preview is refused. Then cleared names go (secret store and
``.env``), non-blank values are written (blank keeps what is stored) and ``enabled`` goes to
``platforms.<name>.enabled`` through the config writer. No answer carries a credential.

``state``: ``disabled`` (switched off in config), ``not_configured`` (credentials missing),
``daemon_stopped``, ``pending_restart`` (the running daemon has not reported it yet), or what the
running daemon reports (``connected``, ``retrying``, ``fatal``, ``paused``).

Pairing (``state.pairing``): pending requests (request id, user, age; never a code) and approved
users; approve by the code the person received or by request id (404 unknown or expired, 429 while
the platform is locked out after failed codes); revoke; clear pending.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Final, NoReturn, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from indus.foundation import config as config_module
from indus.foundation.envfile import EnvNameError, check_writable_name, read_env_file, set_value
from indus.foundation.envload import build_env, env_file_path
from indus.foundation.errors import ConfigError, IndusError
from indus.foundation.redact import mask
from indus.foundation.scope import Scope
from indus.state.pairing import PairingStore
from indus.web.routes.daemon import query_of
from indus.web.scope import read_scope, write_scope
from indus.web.state import dashboard_state

__all__ = ["HIDDEN_PLATFORMS", "ROUTE_LIST", "PlatformInfo", "catalog", "router"]

log = logging.getLogger(__name__)

router = APIRouter()

ROUTE_LIST: Final[tuple[tuple[str, str, bool], ...]] = (
    ("GET", "/api/messaging/platforms", True),
    ("PUT", "/api/messaging/platforms/{platform}", True),
    ("POST", "/api/messaging/platforms/{platform}/test", True),
    ("GET", "/api/pairing", True),
    ("POST", "/api/pairing/approve", True),
    ("POST", "/api/pairing/revoke", True),
    ("POST", "/api/pairing/clear-pending", True),
)
"""``(method, path, scoped)`` of every route here."""

HIDDEN_PLATFORMS: Final = frozenset({"api_server", "webhook"})
"""Platforms with their own page or settings (the API server, the webhook receiver)."""
MAX_VALUE_CHARS: Final = 16_384

ReadScope = Annotated[Scope, Depends(read_scope)]
WriteScope = Annotated[Scope, Depends(write_scope)]


def _fail(status: int, code: str, message: str) -> NoReturn:
    raise HTTPException(status_code=status, detail={"code": code, "message": message})


# -- the platform catalogue -----------------------------------------------------------------------


@dataclass(frozen=True)
class PlatformInfo:
    name: str
    label: str
    hint: str
    install_hint: str
    installed: bool
    credentials: tuple[str, ...]
    settings: tuple[str, ...]
    policy: tuple[str, ...]

    def kind_of(self, key: str) -> str | None:
        if key in self.credentials:
            return "credential"
        if key in self.settings:
            return "setting"
        if key in self.policy:
            return "policy"
        return None

    @property
    def fields(self) -> tuple[str, ...]:
        return (*self.credentials, *self.settings, *self.policy)


def _gateway(module: str) -> Any:
    return importlib.import_module(f"indus.gateway.{module}")


def catalog() -> list[PlatformInfo]:
    """The gateway's platforms (empty when the gateway package cannot be loaded)."""
    try:
        registry_mod: Any = _gateway("registry")
        config_mod: Any = _gateway("config")
        entries: list[Any] = list(registry_mod.PlatformRegistry().entries())
    except Exception as exc:  # the page shows nothing rather than an error
        log.warning("the platform registry could not be loaded: %s", type(exc).__name__)
        return []
    creds = cast(Mapping[str, tuple[str, ...]], config_mod.BUILTIN_CREDENTIALS)
    public = cast(Mapping[str, tuple[str, ...]], config_mod.BUILTIN_PUBLIC_ENV)
    found: list[PlatformInfo] = []
    for entry in entries:
        name = str(entry.name)
        if name in HIDDEN_PLATFORMS:
            continue
        credentials = tuple(creds.get(name, ()))
        settings = tuple(n for n in public.get(name, ()) if n not in credentials)
        policy = tuple(
            n
            for n in (
                entry.allowlist_env,
                entry.allow_all_env,
                entry.allow_bots_env,
                entry.home_env,
                entry.group_users_env,
                entry.group_chats_env,
            )
            if n and n not in credentials and n not in settings
        )
        try:
            installed = bool(entry.check_fn())
        except Exception:
            installed = False
        found.append(
            PlatformInfo(
                name=name,
                label=str(entry.label or name),
                hint=str(entry.platform_hint or ""),
                install_hint=str(entry.install_hint or ""),
                installed=installed,
                credentials=credentials,
                settings=settings,
                policy=policy,
            )
        )
    return found


def _platform(name: str) -> PlatformInfo:
    wanted = name.strip().lower()
    for info in catalog():
        if info.name == wanted:
            return info
    _fail(404, "unknown_platform", "No such messaging platform.")


# -- reading the profile --------------------------------------------------------------------------


def _fresh(scope: Scope) -> Scope:
    """``scope`` with its env view rebuilt from the profile's ``.env`` now."""
    try:
        return scope.with_env(build_env(scope.home, dict(scope.env.base)))
    except (IndusError, OSError):
        return scope


def _secret(scope: Scope, name: str) -> str | None:
    if scope.secrets is not None:
        try:
            value = scope.secrets.get(name)
        except Exception:
            value = None
        if value and value.strip():
            return value.strip()
    return None


def _value(scope: Scope, name: str) -> str | None:
    found = _secret(scope, name)
    if found is not None:
        return found
    value = scope.env.get(name)
    return value.strip() if value and value.strip() else None


def _enablement(scope: Scope) -> Mapping[str, Any]:
    try:
        settings: Any = _gateway("config").load_gateway_settings(scope)
        return cast(Mapping[str, Any], settings.platforms)
    except Exception as exc:
        log.warning("gateway settings could not be read: %s", type(exc).__name__)
        return {}


def _live(request: Request, scope: Scope) -> tuple[bool, dict[str, str]]:
    """``(daemon running, {platform: reported state})`` from the control socket."""
    query = query_of(dashboard_state(request.app).extras)
    if not query(scope.home, "identify", timeout=2.0).ok:
        return False, {}
    status = query(scope.home, "status", timeout=2.0)
    reported: dict[str, str] = {}
    raw = status.result.get("platforms") if status.ok and status.result else None
    if isinstance(raw, dict):
        for key, row in cast(dict[str, Any], raw).items():
            if isinstance(row, dict):
                state = cast(dict[str, Any], row).get("state")
                if isinstance(state, str):
                    reported[str(key)] = state
    return True, reported


def _field(scope: Scope, info: PlatformInfo, key: str) -> dict[str, Any]:
    kind = info.kind_of(key) or "setting"
    value = _value(scope, key)
    row: dict[str, Any] = {
        "key": key,
        "kind": kind,
        "required": key in info.credentials or key in info.settings,
        "is_set": value is not None,
        "is_secret": kind == "credential",
        "preview": None,
        "value": None,
    }
    if value is not None:
        if kind == "credential":
            row["preview"] = mask(value)
        else:
            row["value"] = value
    return row


def _row(
    scope: Scope,
    info: PlatformInfo,
    settings: Mapping[str, Any],
    running: bool,
    reported: Mapping[str, str],
) -> dict[str, Any]:
    entry = settings.get(info.name)
    enabled = bool(getattr(entry, "enabled", False))
    explicit = getattr(entry, "explicit", None)
    configured = bool(info.credentials or info.settings) and all(
        _value(scope, key) is not None for key in (*info.credentials, *info.settings)
    )
    if explicit is False:
        state = "disabled"
    elif not enabled:
        state = "not_configured"
    elif not running:
        state = "daemon_stopped"
    else:
        state = reported.get(info.name, "pending_restart")
    return {
        "id": info.name,
        "name": info.label,
        "description": info.hint,
        "installed": info.installed,
        "install_hint": info.install_hint or None,
        "enabled": enabled,
        "explicitly_disabled": explicit is False,
        "configured": configured,
        "state": state,
        "reason": str(getattr(entry, "reason", "") or "") or None,
        "daemon_running": running,
        "fields": [_field(scope, info, key) for key in info.fields],
    }


@router.get("/api/messaging/platforms")
def list_platforms(request: Request, scope: ReadScope) -> dict[str, Any]:
    fresh = _fresh(scope)
    settings = _enablement(fresh)
    running, reported = _live(request, fresh)
    rows = [_row(fresh, info, settings, running, reported) for info in catalog()]
    return {"platforms": rows, "daemon_running": running}


# -- writing --------------------------------------------------------------------------------------


class PlatformUpdate(BaseModel):
    enabled: bool | None = None
    env: dict[str, str] = Field(default_factory=dict[str, str])
    clear_env: list[str] = Field(default_factory=list[str])


def _check_body(scope: Scope, info: PlatformInfo, body: PlatformUpdate) -> dict[str, str]:
    """The values to write; refuses the whole body on the first problem."""
    for key in [*body.clear_env, *body.env]:
        if info.kind_of(key) is None:
            _fail(400, "unknown_field", f"{key} is not a setting of {info.label}.")
        try:
            check_writable_name(key)
        except EnvNameError:
            _fail(400, "refused_name", f"{key} cannot be set here.")
    updates: dict[str, str] = {}
    for key, raw in body.env.items():
        value = raw.strip()
        if not value:
            continue  # blank keeps what is stored
        if len(value) > MAX_VALUE_CHARS or any(c in value for c in "\r\n\x00"):
            _fail(400, "bad_value", f"The value for {key} is not usable.")
        if info.kind_of(key) == "credential":
            stored = _value(scope, key)
            if stored is not None and value == mask(stored):
                _fail(
                    400,
                    "redacted_value",
                    f"{key} was sent as its masked preview; type the real value or leave it blank.",
                )
        updates[key] = value
    return updates


def _store(scope: Scope) -> Any:
    store = scope.secrets
    if store is None or not callable(getattr(store, "set", None)):
        _fail(500, "no_secret_store", "This profile has no secret store to keep credentials in.")
    return store


@router.put("/api/messaging/platforms/{platform}")
def update_platform(platform: str, body: PlatformUpdate, scope: WriteScope) -> dict[str, Any]:
    info = _platform(platform)
    fresh = _fresh(scope)
    updates = _check_body(fresh, info, body)
    env_path = env_file_path(scope.home)
    try:
        for key in body.clear_env:
            if info.kind_of(key) == "credential":
                _store(scope).delete(key)
            if key in read_env_file(env_path):
                set_value(env_path, key, None)
        for key, value in updates.items():
            if info.kind_of(key) == "credential":
                _store(scope).set(key, value)
            else:
                set_value(env_path, key, value)
        if body.enabled is not None:
            config_module.save(scope, {"platforms": {info.name: {"enabled": body.enabled}}})
    except ConfigError as exc:
        _fail(500, "config_unwritable", f"{exc.message}.")
    except (IndusError, OSError) as exc:
        log.warning("platform update failed for %s: %s", info.name, type(exc).__name__)
        _fail(500, "write_failed", "The settings could not be saved.")
    log.info(
        "messaging platform %s updated: enabled=%s set=%s cleared=%s",
        info.name,
        body.enabled,
        sorted(updates),
        sorted(body.clear_env),
    )
    return {"ok": True, "platform": info.name, "restart_needed": True}


@router.post("/api/messaging/platforms/{platform}/test")
def test_platform(platform: str, request: Request, scope: WriteScope) -> dict[str, Any]:
    info = _platform(platform)
    fresh = _fresh(scope)
    running, reported = _live(request, fresh)
    row = _row(fresh, info, _enablement(fresh), running, reported)
    state = row["state"]
    label = info.label
    messages = {
        "disabled": f"{label} is switched off. Turn it on, then restart the daemon.",
        "not_configured": "Missing: "
        + (
            ", ".join(f["key"] for f in row["fields"] if f["required"] and not f["is_set"])
            or "setup"
        ),
        "daemon_stopped": "The daemon is not running. Start it to connect this platform.",
        "pending_restart": "The daemon has not connected it yet. Restart the daemon.",
        "connected": f"{label} is connected.",
        "retrying": f"{label} is reconnecting.",
        "fatal": f"{label} could not connect; check its settings and the daemon's log.",
        "paused": f"{label} is paused.",
    }
    if not info.installed and state not in ("disabled",):
        message = f"{label} needs its library: {info.install_hint or 'install the extra'}."
        return {"ok": False, "state": state, "message": message}
    return {"ok": state == "connected", "state": state, "message": messages.get(state, state)}


# -- pairing --------------------------------------------------------------------------------------


@router.get("/api/pairing")
def list_pairing(scope: ReadScope) -> dict[str, Any]:
    store = PairingStore(scope)
    return {
        "pending": [
            {
                "platform": p.platform,
                "request_id": p.request_id,
                "user_id": p.user_id,
                "user_name": p.user_name,
                "age_seconds": round(p.age_seconds),
            }
            for p in store.list_pending()
        ],
        "approved": [
            {
                "platform": a.platform,
                "user_id": a.user_id,
                "user_name": a.user_name,
                "approved_at": a.approved_at,
            }
            for a in store.list_approved()
        ],
    }


class ApproveBody(BaseModel):
    platform: str = Field(min_length=1, max_length=64)
    code: str | None = Field(default=None, max_length=64)
    request_id: str | None = Field(default=None, max_length=64)


@router.post("/api/pairing/approve")
def approve(body: ApproveBody, scope: WriteScope) -> dict[str, Any]:
    store = PairingStore(scope)
    try:
        if body.request_id:
            result = store.approve_request(body.platform, body.request_id)
        elif body.code:
            result = store.approve_code(body.platform, body.code)
        else:
            _fail(400, "code_required", "Give the pairing code (or the request id).")
    except ValueError:
        _fail(400, "bad_platform", "That is not a platform name.")
    if result.status == "locked_out":
        _fail(429, "locked_out", "Too many wrong codes for this platform; try again later.")
    if not result.approved:
        _fail(404, "no_such_code", "That code or request is unknown or has expired.")
    return {"ok": True, "user": {"user_id": result.user_id, "user_name": result.user_name}}


class RevokeBody(BaseModel):
    platform: str = Field(min_length=1, max_length=64)
    user_id: str = Field(min_length=1, max_length=256)


@router.post("/api/pairing/revoke")
def revoke(body: RevokeBody, scope: WriteScope) -> dict[str, Any]:
    try:
        gone = PairingStore(scope).revoke(body.platform, body.user_id)
    except ValueError:
        _fail(400, "bad_platform", "That is not a platform name.")
    if not gone:
        _fail(404, "not_approved", "That user is not approved on this platform.")
    return {"ok": True}


class ClearBody(BaseModel):
    platform: str | None = Field(default=None, max_length=64)


@router.post("/api/pairing/clear-pending")
def clear_pending(scope: WriteScope, body: ClearBody | None = None) -> dict[str, Any]:
    platform = body.platform if body is not None else None
    try:
        cleared = PairingStore(scope).clear_pending(platform or None)
    except ValueError:
        _fail(400, "bad_platform", "That is not a platform name.")
    return {"ok": True, "cleared": cleared}
