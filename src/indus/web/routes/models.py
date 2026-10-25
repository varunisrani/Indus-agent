"""``/api/model`` and ``/api/providers/login``: the Models page (P18-16).

- ``GET /api/model/info`` (on the shared public list): the configured model id, provider and
  context window (the configured one, and the family table's when the config sets none). Names
  and numbers only: no URL, key or account.
- ``GET /api/model/options``: the provider catalogue (built-in, local, config and plugin
  providers) with ``configured`` (a key in the pool, the secret store or the environment, or a
  sign-in), ``device_login`` (a device flow this page can run) and ``sign_in`` (any documented
  sign-in flow in ``providers.auth.flows``, browser or device, that ``indus auth add <name> --type
  oauth`` runs), and the current main choice.
- ``POST /api/model/set {scope: main|auxiliary, task?, provider, model, base_url?}``: through the
  config writer; a side task must be one the schema knows.
- ``GET /api/model/auxiliary``: each side task's provider, model and base URL, and the main model.
- ``POST /api/model/test-endpoint {base_url, api_key?, key_env?}`` (P20-32/33): asks an
  OpenAI-compatible endpoint for its ``/models`` from here (where the agent runs, so a remote
  desktop tests the remote's view) and answers ``{ok, status, models, message}``; the key is the
  one given, else the stored ``key_env``. Never raises for the endpoint's trouble, never echoes a
  key.
- Provider sign-in by device code (documented flows only, D-16.11): ``POST
  /api/providers/login/start {provider}`` asks the provider for a code and answers ``{session_id,
  user_code, verification_url, expires_in, interval}``; a dashboard thread then polls the token
  endpoint (RFC 8628 pacing from ``providers.auth.device_code``). ``GET
  /api/providers/login/{session_id}`` answers ``{status, message}`` with status ``pending``,
  ``approved``, ``denied``, ``expired``, ``failed`` or ``cancelled``; ``DELETE`` cancels. Approved
  tokens join the provider's credential pool the way ``indus auth add --type oauth`` stores them
  (secret store + ``auth.json`` metadata); no answer ever carries a token. A provider without a
  documented device flow is 400 ``no_device_flow`` (none has one today). Sessions live 15 minutes
  past their end and are bound to the profile that started them.
"""

from __future__ import annotations

import asyncio
import secrets
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Annotated, Any, Final, NoReturn, cast

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from indus.foundation import config as config_module
from indus.foundation.config.schema import AuxiliarySection
from indus.foundation.errors import ConfigError, IndusError
from indus.foundation.redact import redact_text
from indus.foundation.scope import Scope
from indus.providers.auth import device_code
from indus.providers.auth.flows import flows_for
from indus.providers.auth.pool_model import PoolEntry, env_entries, load_pool, new_entry_id
from indus.providers.auth.store import AuthStore
from indus.providers.context_length import family_length
from indus.providers.model_list import LIST_TIMEOUT_SECONDS, parse_models
from indus.providers.registry import Registry
from indus.web.routes.system import add_pool_secret, provider_profile
from indus.web.scope import read_scope, write_scope
from indus.web.state import dashboard_state

__all__ = ["AUX_TASKS", "ROUTE_LIST", "LoginSession", "LoginSessions", "router"]

router = APIRouter()

ROUTE_LIST: Final[tuple[tuple[str, str, bool], ...]] = (
    ("GET", "/api/model/info", True),
    ("GET", "/api/model/options", True),
    ("POST", "/api/model/set", True),
    ("GET", "/api/model/auxiliary", True),
    ("POST", "/api/model/test-endpoint", True),
    ("POST", "/api/providers/login/start", True),
    ("GET", "/api/providers/login/{session_id}", True),
    ("DELETE", "/api/providers/login/{session_id}", True),
)
"""``(method, path, scoped)`` of every route here."""

AUX_TASKS: Final[tuple[str, ...]] = tuple(AuxiliarySection.model_fields)
SESSION_KEEP_S: Final = 15 * 60.0
START_TIMEOUT_S: Final = 30.0
_SESSIONS_KEY: Final = "providers.login_sessions"
_FLOW_KEY: Final = "providers.device_flows"

ReadScope = Annotated[Scope, Depends(read_scope)]
WriteScope = Annotated[Scope, Depends(write_scope)]


def _fail(status: int, code: str, message: str) -> NoReturn:
    raise HTTPException(status_code=status, detail={"code": code, "message": message})


def _section(scope: Scope, key: str) -> dict[str, Any]:
    raw = config_module.load(scope).get(key)
    return dict(cast(dict[str, Any], raw)) if isinstance(raw, dict) else {}


# -- info, options, set ---------------------------------------------------------------------------


@router.get("/api/model/info")
def model_info(scope: ReadScope) -> dict[str, Any]:
    model = _section(scope, "model")
    name = str(model.get("default") or "")
    configured = model.get("context_length")
    config_ctx = configured if isinstance(configured, int) and configured > 0 else None
    family = family_length(name) if name else None
    return {
        "model": name or None,
        "provider": str(model.get("provider") or "") or None,
        "config_context_length": config_ctx,
        "family_context_length": family,
        "effective_context_length": config_ctx or family,
    }


def _configured(scope: Scope, store: AuthStore, name: str, env_vars: tuple[str, ...]) -> bool:
    try:
        rows, _inherited = store.pool_rows(name)
    except IndusError:
        rows = []
    if rows:
        return True
    for var in env_vars:
        try:
            if scope.secrets is not None and scope.secrets.get(var):
                return True
        except Exception:
            pass
        if (scope.env.get(var) or "").strip():
            return True
    return False


@router.get("/api/model/options")
def model_options(scope: ReadScope) -> dict[str, Any]:
    store = AuthStore(scope.home, env=scope.env)
    rows: list[dict[str, Any]] = []
    for profile in Registry(scope).list_providers():
        flows = flows_for(profile.name)
        rows.append(
            {
                "name": profile.name,
                "label": profile.display_name or profile.name,
                "description": profile.description,
                "family": profile.family,
                "auth_type": profile.auth_type,
                "configured": _configured(scope, store, profile.name, profile.env_vars),
                "device_login": flows.device is not None,
                "sign_in": flows.any,
                "supports_model_listing": profile.supports_model_listing,
            }
        )
    model = _section(scope, "model")
    return {
        "providers": rows,
        "current": {
            "provider": str(model.get("provider") or "") or None,
            "model": str(model.get("default") or "") or None,
        },
    }


class ModelAssignment(BaseModel):
    scope: str = Field(default="main")
    task: str | None = None
    provider: str = Field(default="", max_length=200)
    model: str = Field(default="", max_length=400)
    base_url: str | None = Field(default=None, max_length=2000)


@router.post("/api/model/set")
def set_model(body: ModelAssignment, scope: WriteScope) -> dict[str, Any]:
    kind = body.scope.strip().lower()
    provider, model = body.provider.strip(), body.model.strip()
    if kind == "main":
        if not model:
            _fail(400, "model_required", "Name the model.")
        section: dict[str, Any] = {"default": model}
        if provider:
            if provider != "custom" and Registry(scope).lookup(provider) is None:
                _fail(400, "unknown_provider", "No such provider.")
            section["provider"] = provider
        if body.base_url is not None:
            section["base_url"] = body.base_url.strip()
        change: dict[str, Any] = {"model": section}
    elif kind == "auxiliary":
        task = (body.task or "").strip().lower()
        if task not in AUX_TASKS:
            _fail(400, "unknown_task", f"Unknown side task; one of {', '.join(AUX_TASKS)}.")
        slot: dict[str, Any] = {"provider": provider or "auto", "model": model}
        if body.base_url is not None:
            slot["base_url"] = body.base_url.strip()
        change = {"auxiliary": {task: slot}}
    else:
        _fail(400, "bad_scope", "scope must be main or auxiliary.")
    try:
        config_module.save(scope, change)
    except ConfigError as exc:
        _fail(500, "config_unwritable", f"{exc.message}. Nothing was changed.")
    return {"ok": True, "scope": kind, "provider": provider or None, "model": model or None}


@router.get("/api/model/auxiliary")
def auxiliary(scope: ReadScope) -> dict[str, Any]:
    aux = _section(scope, "auxiliary")
    tasks: list[dict[str, Any]] = []
    for task in AUX_TASKS:
        raw = aux.get(task)
        slot = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
        tasks.append(
            {
                "task": task,
                "provider": str(slot.get("provider") or "auto"),
                "model": str(slot.get("model") or ""),
                "base_url": str(slot.get("base_url") or ""),
                "description": AuxiliarySection.model_fields[task].description or "",
            }
        )
    model = _section(scope, "model")
    return {
        "tasks": tasks,
        "main": {
            "provider": str(model.get("provider") or ""),
            "model": str(model.get("default") or ""),
        },
    }


# -- endpoint test --------------------------------------------------------------------------------

ENDPOINT_MODELS_SHOWN: Final = 200
_ENDPOINT_CLIENT_KEY: Final = "providers.endpoint_client"


class EndpointTest(BaseModel):
    base_url: str = Field(min_length=1, max_length=2000)
    api_key: str | None = Field(default=None, max_length=16384)
    key_env: str | None = Field(default=None, max_length=128)


def _endpoint_client(request: Request) -> Callable[[], httpx.Client]:
    """A sync HTTP client factory (tests put a mock transport under extras)."""
    found = dashboard_state(request.app).extras.get(_ENDPOINT_CLIENT_KEY)
    if callable(found):
        return cast(Callable[[], httpx.Client], found)
    return lambda: httpx.Client(timeout=LIST_TIMEOUT_SECONDS, follow_redirects=False)


def _stored_key(scope: Scope, name: str) -> str | None:
    try:
        if scope.secrets is not None:
            found = scope.secrets.get(name)
            if found:
                return found
    except Exception:
        pass
    return (scope.env.get(name) or "").strip() or None


@router.post("/api/model/test-endpoint")
def endpoint_test(body: EndpointTest, request: Request, scope: WriteScope) -> dict[str, Any]:
    base = body.base_url.strip().rstrip("/")
    try:
        parsed = httpx.URL(base)
    except (httpx.InvalidURL, ValueError):
        _fail(400, "invalid_url", "The endpoint URL is not valid.")
    if parsed.scheme not in ("http", "https") or not parsed.host:
        _fail(400, "invalid_url", "The endpoint URL must be http:// or https://.")
    if parsed.userinfo:
        _fail(400, "invalid_url", "Put the key in the key field, not in the URL.")
    key = (body.api_key or "").strip() or None
    if key is None and body.key_env:
        key = _stored_key(scope, body.key_env.strip())
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        with _endpoint_client(request)() as client:
            answer = client.get(f"{base}/models", headers=headers, timeout=LIST_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "status": None,
            "models": [],
            "message": f"The endpoint did not answer ({type(exc).__name__}).",
        }
    if answer.status_code in (401, 403):
        message = "The endpoint refused the key." if key else "The endpoint needs a key."
        return {"ok": False, "status": answer.status_code, "models": [], "message": message}
    if answer.status_code >= 400:
        return {
            "ok": False,
            "status": answer.status_code,
            "models": [],
            "message": f"The endpoint answered HTTP {answer.status_code}.",
        }
    try:
        payload = answer.json()
    except (ValueError, UnicodeDecodeError):
        return {
            "ok": False,
            "status": answer.status_code,
            "models": [],
            "message": "The endpoint's answer is not a model list.",
        }
    found = parse_models(payload)
    names = [redact_text(m.id) for m in found[:ENDPOINT_MODELS_SHOWN]]
    return {
        "ok": True,
        "status": answer.status_code,
        "models": names,
        "message": f"The endpoint offers {len(found)} models.",
    }


# -- device-code sign-in --------------------------------------------------------------------------


@dataclass
class LoginSession:
    id: str
    provider: str
    home: str
    status: str = "pending"
    message: str = ""
    finished_at: float | None = None
    cancel: threading.Event = field(default_factory=threading.Event)

    def answer(self) -> dict[str, Any]:
        return {
            "session_id": self.id,
            "provider": self.provider,
            "status": self.status,
            "message": self.message,
        }


class LoginSessions:
    """The live sign-in sessions of one app."""

    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._items: dict[str, LoginSession] = {}
        self._lock = threading.Lock()

    def add(self, provider: str, home: str) -> LoginSession:
        session = LoginSession(secrets.token_urlsafe(18), provider, home)
        with self._lock:
            self._sweep()
            self._items[session.id] = session
        return session

    def get(self, session_id: str, home: str) -> LoginSession | None:
        with self._lock:
            self._sweep()
            found = self._items.get(session_id)
        return found if found is not None and found.home == home else None

    def finish(self, session: LoginSession, status: str, message: str) -> None:
        with self._lock:
            if session.status == "pending":
                session.status = status
                session.message = message
                session.finished_at = self._now()

    def _sweep(self) -> None:
        now = self._now()
        for key, item in list(self._items.items()):
            if item.finished_at is not None and now - item.finished_at > SESSION_KEEP_S:
                del self._items[key]


def _sessions(request: Request) -> LoginSessions:
    extras = dashboard_state(request.app).extras
    found = extras.get(_SESSIONS_KEY)
    if isinstance(found, LoginSessions):
        return found
    return cast(LoginSessions, extras.setdefault(_SESSIONS_KEY, LoginSessions()))


def _device_spec(request: Request, provider: str) -> device_code.DeviceFlowSpec | None:
    """The provider's documented device endpoints (tests add their own under extras)."""
    extra = dashboard_state(request.app).extras.get(_FLOW_KEY)
    if isinstance(extra, dict) and provider in extra:
        return cast(device_code.DeviceFlowSpec, cast(dict[str, Any], extra)[provider])
    return flows_for(provider).device


def _client_factory(request: Request) -> Callable[[], Any]:
    found = dashboard_state(request.app).extras.get("providers.http_client")
    return cast(Callable[[], Any], found) if callable(found) else device_code.make_client


class LoginStart(BaseModel):
    provider: str = Field(min_length=1, max_length=100)
    label: str | None = Field(default=None, max_length=100)


def _store_tokens(scope: Scope, provider: str, tokens: device_code.TokenSet, label: str) -> None:
    profile = provider_profile(scope, provider)
    store = AuthStore(scope.home, env=scope.env)
    pool, _inherited = load_pool(store, profile.name, env_entries(profile, None, scope.env))
    entry = PoolEntry(
        id=new_entry_id(pool.ids()),
        label=label or f"sign-in-{len(pool) + 1}",
        auth_type="oauth",
        source="device_code",
        expires_at=tokens.expires_at.isoformat() if tokens.expires_at else None,
    )
    add_pool_secret(scope, profile, tokens.to_secret(), entry)
    with store.transaction() as doc:
        providers = cast(dict[str, Any], doc.setdefault("providers", {}))
        providers[profile.name] = {
            "signed_in_at": scope.clock.now().isoformat(),
            "flow": "device_code",
        }


@router.post("/api/providers/login/start")
def login_start(body: LoginStart, request: Request, scope: WriteScope) -> dict[str, Any]:
    provider = body.provider.strip().lower()
    provider_profile(scope, provider)
    spec = _device_spec(request, provider)
    if spec is None:
        _fail(
            400,
            "no_device_flow",
            "This provider documents no device sign-in; add an API key instead.",
        )
    make_client = _client_factory(request)
    sessions = _sessions(request)
    session = sessions.add(provider, str(scope.home))
    label = (body.label or "").strip()
    started: Future[device_code.DeviceAuthorization] = Future()

    async def flow() -> None:
        async with make_client() as client:
            try:
                grant = await device_code.request_device_authorization(client, spec)
            except device_code.DeviceFlowError as exc:
                started.set_exception(exc)
                return
            started.set_result(grant)
            try:
                tokens = await device_code.poll_for_token(client, spec, grant, clock=scope.clock)
            except device_code.AccessDenied:
                sessions.finish(session, "denied", "The sign-in was refused.")
                return
            except device_code.DeviceCodeExpired:
                sessions.finish(session, "expired", "The code expired before it was approved.")
                return
            except device_code.DeviceFlowError as exc:
                sessions.finish(session, "failed", exc.message)
                return
        if session.cancel.is_set():
            return
        try:
            await asyncio.to_thread(_store_tokens, scope, provider, tokens, label)
        except (IndusError, HTTPException, OSError):
            sessions.finish(session, "failed", "Signed in, but the credential could not be saved.")
            return
        sessions.finish(session, "approved", "Signed in; the credential was added.")

    def run() -> None:
        loop = asyncio.new_event_loop()
        try:
            task = loop.create_task(flow())
            while not task.done():
                loop.run_until_complete(asyncio.wait([task], timeout=0.25))
                if session.cancel.is_set() and not task.done():
                    task.cancel()
                    loop.run_until_complete(asyncio.wait([task]))
            if not task.cancelled() and task.exception() is not None and not started.done():
                started.set_exception(task.exception() or RuntimeError("sign-in failed"))
        finally:
            loop.close()

    threading.Thread(target=run, name=f"indus-login-{provider}", daemon=True).start()
    try:
        grant = started.result(timeout=START_TIMEOUT_S)
    except device_code.DeviceFlowError as exc:
        sessions.finish(session, "failed", exc.message)
        _fail(502, "sign_in_unavailable", exc.message)
    except Exception:
        session.cancel.set()
        sessions.finish(session, "failed", "The provider's sign-in service did not answer.")
        _fail(502, "sign_in_unavailable", "The provider's sign-in service did not answer.")
    return {
        **session.answer(),
        "user_code": grant.user_code,
        "verification_url": grant.url_to_show,
        "expires_in": grant.expires_in,
        "interval": grant.interval,
    }


@router.get("/api/providers/login/{session_id}")
def login_status(session_id: str, request: Request, scope: ReadScope) -> dict[str, Any]:
    found = _sessions(request).get(session_id, str(scope.home))
    if found is None:
        _fail(404, "no_such_session", "No such sign-in session.")
    return found.answer()


@router.delete("/api/providers/login/{session_id}")
def login_cancel(session_id: str, request: Request, scope: WriteScope) -> dict[str, Any]:
    sessions = _sessions(request)
    found = sessions.get(session_id, str(scope.home))
    if found is None:
        _fail(404, "no_such_session", "No such sign-in session.")
    found.cancel.set()
    sessions.finish(found, "cancelled", "The sign-in was cancelled.")
    return found.answer()
