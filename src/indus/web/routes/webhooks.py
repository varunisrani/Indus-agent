"""``/api/webhooks``: the Webhooks page (P18-15).

Routes of the gateway's webhook receiver for the request's profile: the subscribed routes in
``<home>/gateway/webhook_subscriptions.json`` (``state.webhook_subscriptions``, shared with
``indus webhook``) and the routes written in ``gateway.webhook.routes``.

- ``GET /api/webhooks``: the receiver's ``enabled`` flag and base URL, and every route (source,
  URL, auth, events, delivery, ``enabled``, ``secret_set``). Never a secret value: a route file
  holds only the *name* of its secret.
- ``POST /api/webhooks {name, events?, prompt?, deliver?, deliver_only?, auth?, description?}``:
  a new subscribed route with a fresh random signing secret, kept in the profile's secret store
  under the route's generated name. The secret is in this answer once and in no other answer.
- ``DELETE /api/webhooks/{name}``: drops the route and its generated secret.
- ``PUT /api/webhooks/{name}/enabled {enabled}``: flips the route's flag. The receiver re-reads the
  file on every request (it compares the file's stamp), so a disabled route answers 403 at once,
  without a restart.
- ``PUT /api/webhooks/receiver {enabled}``: ``gateway.webhook.enabled`` (takes effect at the next
  daemon start).

Routes from the config file are listed but not changed here (409 ``config_route``).
"""

from __future__ import annotations

import contextlib
import secrets as pysecrets
from typing import Annotated, Any, Final, Literal, NoReturn, cast

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from indus.foundation import config as config_module
from indus.foundation.errors import ConfigError, IndusError
from indus.foundation.scope import Scope
from indus.state.webhook_subscriptions import (
    ROUTE_NAME_RE,
    SubscriptionStore,
    normalise_name,
    secret_name_for,
)
from indus.web.scope import read_scope, write_scope

__all__ = ["ROUTE_LIST", "router"]

router = APIRouter()

ROUTE_LIST: Final[tuple[tuple[str, str, bool], ...]] = (
    ("GET", "/api/webhooks", True),
    ("POST", "/api/webhooks", True),
    ("PUT", "/api/webhooks/receiver", True),
    ("DELETE", "/api/webhooks/{name}", True),
    ("PUT", "/api/webhooks/{name}/enabled", True),
)
"""``(method, path, scoped)`` of every route here."""

SECRET_BYTES: Final = 32

ReadScope = Annotated[Scope, Depends(read_scope)]
WriteScope = Annotated[Scope, Depends(write_scope)]


def _fail(status: int, code: str, message: str) -> NoReturn:
    raise HTTPException(status_code=status, detail={"code": code, "message": message})


def _settings(scope: Scope) -> dict[str, Any]:
    raw = config_module.load(scope).get("gateway.webhook")
    return dict(cast(dict[str, Any], raw)) if isinstance(raw, dict) else {}


def _base_url(settings: dict[str, Any]) -> str:
    host = str(settings.get("host") or "127.0.0.1")
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{settings.get('port', 8644)}"


def _has_secret(scope: Scope, name: object) -> bool:
    if not isinstance(name, str) or not name:
        return False
    try:
        if scope.secrets is not None and scope.secrets.get(name):
            return True
    except Exception:
        return False
    return bool((scope.env.get(name) or "").strip())


def _summary(scope: Scope, name: str, source: str, route: dict[str, Any], base: str) -> Any:
    return {
        "name": name,
        "source": source,
        "url": f"{base}/webhooks/{name}",
        "auth": "insecure" if route.get("insecure") else route.get("auth", "auto"),
        "events": list(route.get("events") or []),
        "prompt": str(route.get("prompt") or ""),
        "deliver": route.get("deliver"),
        "deliver_only": bool(route.get("deliver_only")),
        "description": str(route.get("description") or ""),
        "created_at": route.get("created_at"),
        "enabled": route.get("enabled", True) is not False,
        "secret_set": _has_secret(scope, route.get("secret")),
    }


@router.get("/api/webhooks")
def list_webhooks(scope: ReadScope) -> dict[str, Any]:
    settings = _settings(scope)
    base = _base_url(settings)
    rows: dict[str, Any] = {}
    for name, route in SubscriptionStore(scope).load().items():
        rows[name] = _summary(scope, name, "subscribed", route, base)
    configured = settings.get("routes")
    if isinstance(configured, dict):
        for name, raw in cast(dict[str, Any], configured).items():
            if isinstance(raw, dict):
                rows[str(name)] = _summary(
                    scope, str(name), "config", cast(dict[str, Any], raw), base
                )
    return {
        "receiver_enabled": bool(settings.get("enabled")),
        "base_url": base,
        "routes": [rows[k] for k in sorted(rows)],
    }


class CreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    events: list[str] = Field(default_factory=list[str], max_length=64)
    prompt: str = Field(default="", max_length=16_000)
    deliver: str | None = Field(default=None, max_length=512)
    deliver_only: bool = False
    auth: Literal["auto", "github", "gitlab", "hmac", "hmac_ts"] = "auto"
    description: str = Field(default="", max_length=500)


def _config_routes(scope: Scope) -> set[str]:
    configured = _settings(scope).get("routes")
    return set(cast(dict[str, Any], configured)) if isinstance(configured, dict) else set()


@router.post("/api/webhooks", status_code=201)
def create_webhook(body: CreateBody, scope: WriteScope) -> dict[str, Any]:
    name = normalise_name(body.name)
    if not ROUTE_NAME_RE.match(name):
        _fail(400, "bad_name", "Use lower-case letters, digits, '-' and '_' for the name.")
    deliver = (body.deliver or "").strip() or None
    if body.deliver_only and deliver is None:
        _fail(400, "deliver_required", "Direct delivery needs a real delivery target.")
    store = SubscriptionStore(scope)
    if name in store.load() or name in _config_routes(scope):
        _fail(409, "route_exists", "A route with that name already exists.")
    vault: Any = scope.secrets
    if vault is None or not callable(getattr(vault, "set", None)):
        _fail(500, "no_secret_store", "This profile has no secret store for the signing secret.")
    secret_name = secret_name_for(name)
    secret = pysecrets.token_urlsafe(SECRET_BYTES)
    route: dict[str, Any] = {
        "secret": secret_name,
        "auth": body.auth,
        "events": [e.strip() for e in body.events if e.strip()],
        "prompt": body.prompt,
        "deliver": deliver,
        "deliver_only": body.deliver_only,
        "description": body.description.strip() or f"Added from the dashboard: {name}",
        "created_at": scope.clock.now().isoformat(timespec="seconds"),
        "enabled": True,
    }
    try:
        vault.set(secret_name, secret)
        store.put(name, route)
    except (IndusError, OSError, ValueError):
        _fail(500, "write_failed", "The route could not be saved.")
    summary = _summary(scope, name, "subscribed", route, _base_url(_settings(scope)))
    return {**summary, "secret": secret}


def _subscribed(scope: Scope, name: str) -> tuple[SubscriptionStore, str, dict[str, Any]]:
    key = normalise_name(name)
    store = SubscriptionStore(scope)
    route = store.load().get(key)
    if route is None:
        if key in _config_routes(scope):
            _fail(409, "config_route", "That route is set in the config file; change it there.")
        _fail(404, "no_such_route", "No such webhook route.")
    return store, key, route


@router.delete("/api/webhooks/{name}")
def delete_webhook(name: str, scope: WriteScope) -> dict[str, Any]:
    store, key, _route = _subscribed(scope, name)
    gone = store.remove(key)
    if gone is not None and gone.get("secret") == secret_name_for(key) and scope.secrets:
        vault: Any = scope.secrets
        with contextlib.suppress(IndusError, OSError, AttributeError):
            vault.delete(secret_name_for(key))
    return {"ok": True, "name": key}


class EnabledBody(BaseModel):
    enabled: bool


@router.put("/api/webhooks/{name}/enabled")
def set_enabled(name: str, body: EnabledBody, scope: WriteScope) -> dict[str, Any]:
    store, key, route = _subscribed(scope, name)
    try:
        store.put(key, {**route, "enabled": body.enabled})
    except (OSError, ValueError):
        _fail(500, "write_failed", "The route could not be saved.")
    return {"ok": True, "name": key, "enabled": body.enabled}


@router.put("/api/webhooks/receiver")
def set_receiver(body: EnabledBody, scope: WriteScope) -> dict[str, Any]:
    try:
        config_module.save(scope, {"gateway": {"webhook": {"enabled": body.enabled}}})
    except ConfigError as exc:
        _fail(500, "config_unwritable", f"{exc.message}. Nothing was changed.")
    return {"ok": True, "receiver_enabled": body.enabled, "restart_needed": True}
