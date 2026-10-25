"""``/api/providers/login/accounts/{provider}``: a provider's sign-ins, and signing out (W61-M1-2).

Starting, polling and cancelling a device sign-in are ``routes.models`` (``/api/providers/login/
start`` and ``/{session_id}``); this module adds the two reads and writes around them, in the same
profile-scoped family:

- ``GET`` (read scope): ``{provider, label, signed_in, signed_in_at, flows: {browser, device},
  accounts: [{id, label, source, status, expires_at, expired, plan}]}``. ``accounts`` lists the
  signed-in credentials of the pool (API keys are the Keys page's); ``plan`` is the ChatGPT plan
  named in an ``openai-codex`` token's claims, else null.
- ``DELETE`` (write scope): signs out: every signed-in credential of the provider, its stored
  tokens and the ``providers.<name>`` sign-in state are deleted; API keys and env keys stay.
  Answers ``{provider, removed, signed_in: false}``.

No answer carries a token or anything derived from one beyond the plan name. A credential pool the
profile inherits from the default profile is read here and refused for sign-out (409
``inherited_pool``), as the Keys page does.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Annotated, Any, Final, NoReturn, cast

from fastapi import APIRouter, Depends, HTTPException

from indus.foundation import config as config_module
from indus.foundation.errors import IndusError
from indus.foundation.scope import Scope
from indus.foundation.secrets import open_secret_store
from indus.providers.auth import chatgpt
from indus.providers.auth.device_code import TokenSet
from indus.providers.auth.flows import flows_for
from indus.providers.auth.pool_model import (
    Pool,
    PoolEntry,
    SecretWriter,
    edit_pool,
    env_entries,
    load_pool,
    secret_name,
)
from indus.providers.auth.store import AuthStore
from indus.providers.profile import ProviderProfile
from indus.web.routes.system import provider_profile
from indus.web.scope import read_scope, write_scope

__all__ = ["ROUTE_LIST", "router"]

router = APIRouter()

ACCOUNTS_PATH: Final = "/api/providers/login/accounts/{provider}"

ROUTE_LIST: Final[tuple[tuple[str, str, bool], ...]] = (
    ("GET", ACCOUNTS_PATH, True),
    ("DELETE", ACCOUNTS_PATH, True),
)
"""``(method, path, scoped)`` of every route here."""

ReadScope = Annotated[Scope, Depends(read_scope)]
WriteScope = Annotated[Scope, Depends(write_scope)]


def _fail(status: int, code: str, message: str) -> NoReturn:
    raise HTTPException(status_code=status, detail={"code": code, "message": message})


def _env_rows(scope: Scope, profile: ProviderProfile) -> list[PoolEntry]:
    try:
        return env_entries(profile, scope.secrets, scope.env)
    except IndusError:
        return env_entries(profile, None, scope.env)


def _writer(scope: Scope) -> SecretWriter:
    current = scope.secrets
    if callable(getattr(current, "set", None)) and callable(getattr(current, "delete", None)):
        return cast(SecretWriter, current)
    mode = str(config_module.load(scope).get("security.secret_store") or "auto")
    return open_secret_store(scope.home, scope.profile_id, mode=mode)


def _tokens(scope: Scope, provider: str, entry: PoolEntry) -> TokenSet | None:
    try:
        stored = scope.secrets.get(secret_name(provider, entry.id)) if scope.secrets else None
        return TokenSet.from_secret(stored) if stored else None
    except Exception:
        return None


def _when(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _account(scope: Scope, provider: str, entry: PoolEntry, now: datetime) -> dict[str, Any]:
    tokens = _tokens(scope, provider, entry)
    expires = tokens.expires_at if tokens is not None else _when(entry.expires_at)
    return {
        "id": entry.id,
        "label": entry.label,
        "source": entry.source,
        "status": entry.status,
        "expires_at": expires.isoformat() if expires is not None else None,
        "expired": expires is not None and expires <= now,
        "plan": chatgpt.plan(tokens.access_token) if tokens is not None else None,
    }


@router.get(ACCOUNTS_PATH)
def accounts(provider: str, scope: ReadScope) -> dict[str, Any]:
    profile = provider_profile(scope, provider)
    store = AuthStore(scope.home, env=scope.env)
    try:
        pool, _inherited = load_pool(store, profile.name, _env_rows(scope, profile))
        state, _from_root = store.provider_state(profile.name)
    except IndusError as exc:
        _fail(500, "auth_unreadable", exc.message)
    now = scope.clock.now()
    rows = [_account(scope, profile.name, e, now) for e in pool.entries if e.auth_type == "oauth"]
    flows = flows_for(profile.name)
    since = (state or {}).get("signed_in_at")
    return {
        "provider": profile.name,
        "label": profile.label,
        "signed_in": bool(rows),
        "signed_in_at": str(since) if since and rows else None,
        "flows": {"browser": flows.pkce is not None, "device": flows.device is not None},
        "accounts": rows,
    }


@router.delete(ACCOUNTS_PATH)
def sign_out(provider: str, scope: WriteScope) -> dict[str, Any]:
    profile = provider_profile(scope, provider)
    store = AuthStore(scope.home, env=scope.env)
    try:
        pool, inherited = load_pool(store, profile.name, _env_rows(scope, profile))
    except IndusError as exc:
        _fail(500, "auth_unreadable", exc.message)
    if inherited and any(e.auth_type == "oauth" for e in pool.entries):
        _fail(409, "inherited_pool", "These sign-ins belong to the default profile.")
    removed: list[PoolEntry] = []

    def change(current: Pool, doc: dict[str, Any]) -> None:
        removed.extend(e for e in current.entries if e.auth_type == "oauth")
        current.entries[:] = [e for e in current.entries if e.auth_type != "oauth"]
        providers = doc.get("providers")
        if isinstance(providers, dict):
            cast(dict[str, Any], providers).pop(profile.name, None)
        if doc.get("active_provider") == profile.name:
            doc.pop("active_provider", None)

    try:
        edit_pool(store, profile.name, change, _env_rows(scope, profile))
    except IndusError as exc:
        _fail(500, "pool_unwritable", exc.message)
    writer = _writer(scope)
    for entry in removed:
        with contextlib.suppress(IndusError):
            writer.delete(secret_name(profile.name, entry.id))
    return {"provider": profile.name, "removed": len(removed), "signed_in": False}
