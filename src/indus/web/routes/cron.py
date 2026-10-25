"""``/api/cron``: the Cron page's backend and the public fire webhook (P18-14).

Jobs go through the P14 backend (``cron.ports.backend_for``; ``service.cron_host`` registers the
real one) under the request's profile scope: list, one, create, update, pause, resume, delete,
trigger, delivery targets and blueprints. Create and update check the prompt (safety scan) and the
delivery value (target grammar) the way ``indus cron create`` does before the store sees them; the
store then checks the schedule (our grammar or a cron line). Answers carry a job view: the fields
the page shows, times as ISO-8601, the last error redacted.

**Runs.** Trigger and the fire webhook start the job in a background thread of this process (the
daemon has no "run this job" verb yet) and answer 202 at once; a job that is already running is
409 ``job_running``.

**Fire webhook.** ``POST /api/cron/fire`` is on the shared public list: an external scheduler calls
it without a dashboard credential. It is authenticated by its own token instead, minted by a
signed-in caller with ``POST /api/cron/jobs/{id}/fire-token``. A token names the profile and the
job and carries an expiry (default ``FIRE_TTL_S``, at most ``FIRE_TTL_MAX_S``); it is signed with
HMAC-SHA256 under a random key this process made, so a restart ends every token. It travels as
``Authorization: Bearer <token>``, is compared in constant time, and is never logged or echoed.
Every token problem (absent, malformed, wrong signature, expired, another job) is the same 401.
A good token for a job that no longer exists answers 200 ``gone``, so the caller stops retrying.

Seams for tests (``DashboardState.extras``): ``cron.backend`` (``Scope -> CronBackend``) and
``cron.runner`` (``(backend, job_id) -> None``, how a run is started).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Annotated, Any, Final, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from indus.cron import blueprints as bps
from indus.cron import ports, targets
from indus.cron import tool as checks
from indus.cron.errors import CronStoreBusy
from indus.cron.ports import CronBackend
from indus.foundation import config as config_module
from indus.foundation.redact import Redactor
from indus.foundation.scope import Scope
from indus.learning.skills.resolver import load_catalog
from indus.web.scope import ProfileUnknown, read_scope, scopes_of, write_scope
from indus.web.state import DashboardState, dashboard_state

__all__ = [
    "FIRE_PATH",
    "FIRE_TTL_MAX_S",
    "FIRE_TTL_S",
    "ROUTE_LIST",
    "FireClaims",
    "mint_fire_token",
    "read_fire_token",
    "router",
]

log = logging.getLogger(__name__)

router = APIRouter()

FIRE_PATH: Final = "/api/cron/fire"

ROUTE_LIST: Final[tuple[tuple[str, str, bool], ...]] = (
    ("GET", "/api/cron/jobs", True),
    ("POST", "/api/cron/jobs", True),
    ("GET", "/api/cron/jobs/{job_id}", True),
    ("PUT", "/api/cron/jobs/{job_id}", True),
    ("DELETE", "/api/cron/jobs/{job_id}", True),
    ("POST", "/api/cron/jobs/{job_id}/pause", True),
    ("POST", "/api/cron/jobs/{job_id}/resume", True),
    ("POST", "/api/cron/jobs/{job_id}/trigger", True),
    ("POST", "/api/cron/jobs/{job_id}/fire-token", True),
    ("GET", "/api/cron/delivery-targets", True),
    ("GET", "/api/cron/blueprints", True),
    ("POST", FIRE_PATH, False),
)
"""``(method, path, scoped)`` of every route here. The fire webhook takes its profile from the
signed token, never from ``?profile=``."""

ReadScope = Annotated[Scope, Depends(read_scope)]
WriteScope = Annotated[Scope, Depends(write_scope)]

FIRE_TTL_S: Final = 300
FIRE_TTL_MAX_S: Final = 3600
FIRE_BODY_LIMIT: Final = 4096
_FIRE_KEY: Final = "cron.fire_key"
PLAIN_BLUEPRINT: Final = "blueprint"
"""Key suffix of a skill's plain ``blueprint`` block (``<skill>:blueprint``)."""
_FIRE_LABEL: Final = b"indus-cron-fire.v1."


def _fail(status: int, code: str, message: str, **extra: Any) -> NoReturn:
    raise HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


# -- the backend -----------------------------------------------------------------------------------


def _backend(state: DashboardState, scope: Scope) -> CronBackend:
    factory: object = state.extras.get("cron.backend")
    if callable(factory):
        return factory(scope)  # pyright: ignore[reportReturnType]
    from indus.service import cron_host

    cron_host.install()
    return ports.backend_for(scope)


def _call[T](state: DashboardState, scope: Scope, work: Callable[[CronBackend], T]) -> T:
    try:
        return work(_backend(state, scope))
    except ports.StoreUnavailable as exc:
        _fail(503, "cron_unavailable", str(exc))
    except CronStoreBusy:
        _fail(503, "cron_busy", "The job store is busy right now; try again.")
    except LookupError:
        _fail(404, "job_not_found", "No job has this id.")
    except ValueError as exc:
        _fail(400, "invalid_job", Redactor(scope).redact(str(exc), force=True))


def _iso(value: object) -> str | None:
    if isinstance(value, datetime):
        return ports.iso(value)
    return str(value) if value else None


def job_out(job: Any, scope: Scope) -> dict[str, Any]:
    """The page's view of one job."""
    times, done = ports.repeat_of(job)
    display = str(ports.field(job, "schedule_display") or ports.schedule_kind(job) or "")
    error = ports.field(job, "last_error")
    redactor = Redactor(scope)
    return {
        "id": ports.field(job, "id"),
        "name": ports.field(job, "name") or "",
        "prompt": ports.field(job, "prompt") or "",
        "schedule": display,
        "schedule_display": display,
        "deliver": ports.field(job, "deliver") or targets.LOCAL,
        "skills": list(ports.field(job, "skills") or []),
        "repeat": {"times": times, "completed": done},
        "state": ports.field(job, "state") or "scheduled",
        "enabled": bool(ports.field(job, "enabled", True)),
        "paused_reason": ports.field(job, "paused_reason"),
        "last_run_at": _iso(ports.field(job, "last_run_at")),
        "next_run_at": _iso(ports.field(job, "next_run_at")),
        "last_status": ports.field(job, "last_status"),
        "last_error": redactor.redact(str(error), force=True) if error else None,
    }


# -- bodies ----------------------------------------------------------------------------------------


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=200)
    prompt: str = Field(min_length=1, max_length=5000)
    schedule: str = Field(min_length=1, max_length=500)
    deliver: str | None = Field(default=None, max_length=500)
    skills: list[str] | None = Field(default=None, max_length=50)
    repeat: int | None = Field(default=None, ge=1)


class JobUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=200)
    prompt: str | None = Field(default=None, min_length=1, max_length=5000)
    schedule: str | None = Field(default=None, min_length=1, max_length=500)
    deliver: str | None = Field(default=None, max_length=500)
    skills: list[str] | None = Field(default=None, max_length=50)
    repeat: int | None = Field(default=None, ge=1)


def _checked(fields: dict[str, Any]) -> dict[str, Any]:
    """The prompt and delivery checks of ``indus cron create``; empty name means none."""
    if "name" in fields and not (fields["name"] or "").strip():
        fields["name"] = None
    problem = checks.prompt_problem(fields.get("prompt"))
    if problem is not None:
        _fail(400, "prompt_refused", problem)
    if fields.get("deliver") is not None:
        value = targets.join_tokens(targets.split_tokens(fields["deliver"])) or targets.LOCAL
        problem = checks.deliver_problem(value)
        if problem is not None:
            _fail(400, "invalid_deliver", problem)
        fields["deliver"] = value
    return fields


# -- jobs ------------------------------------------------------------------------------------------


@router.get("/api/cron/jobs")
def list_jobs(request: Request, scope: ReadScope) -> dict[str, Any]:
    state = dashboard_state(request.app)
    jobs = _call(state, scope, lambda b: b.list_jobs(include_disabled=True))
    return {"jobs": [job_out(job, scope) for job in jobs]}


@router.post("/api/cron/jobs", status_code=201)
def create_job(request: Request, body: JobCreate, scope: WriteScope) -> dict[str, Any]:
    state = dashboard_state(request.app)
    fields = _checked(body.model_dump(exclude_none=True))
    fields.setdefault("deliver", targets.LOCAL)
    if not fields.get("skills"):
        fields.pop("skills", None)
    job = _call(state, scope, lambda b: b.create(fields))
    return job_out(job, scope)


def _job(state: DashboardState, scope: Scope, job_id: str) -> Any:
    found = _call(state, scope, lambda b: b.get(job_id))
    if found is None:
        _fail(404, "job_not_found", "No job has this id.")
    return found


@router.get("/api/cron/jobs/{job_id}")
def get_job(request: Request, job_id: str, scope: ReadScope) -> dict[str, Any]:
    return job_out(_job(dashboard_state(request.app), scope, job_id), scope)


@router.put("/api/cron/jobs/{job_id}")
def update_job(request: Request, job_id: str, body: JobUpdate, scope: WriteScope) -> dict[str, Any]:
    state = dashboard_state(request.app)
    changes = _checked(body.model_dump(exclude_unset=True))
    if not changes:
        _fail(400, "no_changes", "Nothing to change.")
    _job(state, scope, job_id)
    job = _call(state, scope, lambda b: b.update(job_id, changes))
    return job_out(job, scope)


@router.delete("/api/cron/jobs/{job_id}")
def delete_job(request: Request, job_id: str, scope: WriteScope) -> dict[str, Any]:
    state = dashboard_state(request.app)
    _job(state, scope, job_id)
    if not _call(state, scope, lambda b: b.remove(job_id)):
        _fail(404, "job_not_found", "No job has this id.")
    return {"ok": True}


@router.post("/api/cron/jobs/{job_id}/pause")
def pause_job(request: Request, job_id: str, scope: WriteScope) -> dict[str, Any]:
    state = dashboard_state(request.app)
    _job(state, scope, job_id)
    job = _call(state, scope, lambda b: b.pause(job_id, "paused from the dashboard"))
    return job_out(job, scope)


@router.post("/api/cron/jobs/{job_id}/resume")
def resume_job(request: Request, job_id: str, scope: WriteScope) -> dict[str, Any]:
    state = dashboard_state(request.app)
    _job(state, scope, job_id)
    job = _call(state, scope, lambda b: b.resume(job_id))
    return job_out(job, scope)


# -- runs ------------------------------------------------------------------------------------------


def _run_in_thread(backend: CronBackend, job_id: str) -> None:
    def work() -> None:
        try:
            report = asyncio.run(backend.run_now(job_id))
            log.info("dashboard-started run of job %s: %s", job_id, report.status)
        except Exception:
            log.warning("dashboard-started run of job %s failed", job_id, exc_info=True)

    thread = threading.Thread(target=work, name=f"indus-cron-run-{job_id}", daemon=True)
    thread.start()


def _start_run(state: DashboardState, scope: Scope, job_id: str) -> None:
    """Start ``job_id`` in the background; 409 while it already runs."""
    backend = _backend(state, scope)
    if _call(state, scope, lambda b: b.is_running(job_id)):
        _fail(409, "job_running", "The job is running already.")
    runner: object = state.extras.get("cron.runner")
    if callable(runner):
        runner(backend, job_id)
    else:
        _run_in_thread(backend, job_id)


@router.post("/api/cron/jobs/{job_id}/trigger", status_code=202)
def trigger_job(request: Request, job_id: str, scope: WriteScope) -> dict[str, Any]:
    state = dashboard_state(request.app)
    job = _job(state, scope, job_id)
    _start_run(state, scope, str(ports.field(job, "id")))
    return job_out(job, scope)


# -- fire tokens -----------------------------------------------------------------------------------


class FireClaims(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: str
    job_id: str
    expires_at: float


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _fire_key(state: DashboardState) -> bytes:
    key: object = state.extras.setdefault(_FIRE_KEY, secrets.token_bytes(32))
    if not isinstance(key, bytes) or len(key) < 32:
        raise RuntimeError("the fire-token key is not usable")
    return key


def _signature(key: bytes, payload: str) -> bytes:
    return hmac.new(key, _FIRE_LABEL + payload.encode("ascii"), hashlib.sha256).digest()


def mint_fire_token(key: bytes, claims: FireClaims) -> str:
    payload = _b64(json.dumps(claims.model_dump(), separators=(",", ":")).encode("utf-8"))
    return f"{payload}.{_b64(_signature(key, payload))}"


def read_fire_token(key: bytes, token: str, now: float) -> FireClaims | None:
    """The claims of a token this key signed that has not expired; None otherwise."""
    payload, dot, signature = token.partition(".")
    if not dot or not payload or not signature or len(token) > 1024:
        return None
    try:
        given = _unb64(signature)
        expected = _signature(key, payload)
        if not hmac.compare_digest(given, expected):
            return None
        claims = FireClaims.model_validate_json(_unb64(payload))
    except (ValueError, UnicodeError):
        return None
    return claims if now < claims.expires_at else None


class FireTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ttl_s: int = Field(default=FIRE_TTL_S, ge=1, le=FIRE_TTL_MAX_S)


@router.post("/api/cron/jobs/{job_id}/fire-token")
def fire_token(
    request: Request, job_id: str, scope: WriteScope, body: FireTokenRequest | None = None
) -> JSONResponse:
    state = dashboard_state(request.app)
    job = _job(state, scope, job_id)
    ttl = (body or FireTokenRequest()).ttl_s
    claims = FireClaims(
        profile=scope.profile_id,
        job_id=str(ports.field(job, "id")),
        expires_at=scope.clock.now().timestamp() + ttl,
    )
    token = mint_fire_token(_fire_key(state), claims)
    return JSONResponse(
        {"token": token, "expires_in": ttl, "path": FIRE_PATH},
        headers={"cache-control": "no-store"},
    )


def _bearer(request: Request) -> str:
    raw = request.headers.get("authorization", "")
    scheme, _, value = raw.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


async def _small_json(request: Request) -> Mapping[str, Any]:
    data = b""
    async for chunk in request.stream():
        data += chunk
        if len(data) > FIRE_BODY_LIMIT:
            _fail(413, "body_too_large", "The request body is too large.")
    if not data.strip():
        return {}
    try:
        parsed: object = json.loads(data)
    except ValueError:
        _fail(400, "invalid_body", "The body must be JSON.")
    if not isinstance(parsed, dict):
        _fail(400, "invalid_body", "The body must be a JSON object.")
    return {str(k): v for k, v in parsed.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]


def _denied() -> NoReturn:
    _fail(401, "invalid_fire_token", "The fire token is not valid.")


def _fire_job(state: DashboardState, scope: Scope, job_id: str) -> JSONResponse:
    found = _call(state, scope, lambda b: b.get(job_id))
    if found is None:
        return JSONResponse({"status": "gone", "job_id": job_id})
    _start_run(state, scope, job_id)
    return JSONResponse({"status": "accepted", "job_id": job_id}, status_code=202)


@router.post(FIRE_PATH, status_code=202)
async def fire(request: Request) -> JSONResponse:
    state = dashboard_state(request.app)
    scopes = scopes_of(request)
    now = scopes.base.clock.now().timestamp()
    claims = read_fire_token(_fire_key(state), _bearer(request), now)
    if claims is None:
        _denied()
    body = await _small_json(request)
    named = body.get("job_id")
    if named is not None and named != claims.job_id:
        _denied()
    try:
        scope = scopes.resolve(claims.profile)
    except ProfileUnknown:
        return JSONResponse({"status": "gone", "job_id": claims.job_id})
    return await run_in_threadpool(_fire_job, state, scope, claims.job_id)


# -- targets and blueprints ------------------------------------------------------------------------


def _platform_rows(scope: Scope) -> list[dict[str, Any]]:
    view = config_module.load(scope)
    section: object = view.get("platforms", {})
    if not isinstance(section, Mapping):
        return []
    rows: list[dict[str, Any]] = []
    for name, raw in sorted(section.items()):  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        if not isinstance(raw, Mapping) or not isinstance(name, str):
            continue
        settings: Mapping[str, Any] = raw  # pyright: ignore[reportUnknownVariableType]
        home: object = settings.get("home_channel")
        chat = home.get("chat_id") if isinstance(home, Mapping) else None  # pyright: ignore[reportUnknownMemberType]
        enabled = settings.get("enabled")
        if enabled is None and not chat:
            continue  # never set up: not offered
        rows.append(
            {
                "id": name,
                "label": name.replace("_", " ").title(),
                "kind": "email" if name == "email" else "platform",
                "available": bool(chat) and enabled is not False,
            }
        )
    return rows


@router.get("/api/cron/delivery-targets")
def delivery_targets(scope: ReadScope) -> dict[str, Any]:
    local = {"id": targets.LOCAL, "label": "Save locally", "kind": "local", "available": True}
    try:
        platforms = _platform_rows(scope)
    except Exception as exc:  # the dropdown keeps "local" whatever the config says
        log.warning("delivery targets unavailable: %s", type(exc).__name__)
        platforms = []
    return {"targets": [local, *platforms]}


@router.get("/api/cron/blueprints")
def blueprints(scope: ReadScope) -> dict[str, Any]:
    """The suggested jobs: the starter catalogue, then the skills' (W55-F2-1).

    ``cron.blueprints.list_blueprints`` gives the starter catalogue and the skills' slotted
    blueprints, prefilled from their defaults; a skill whose front matter carries a plain
    ``blueprint`` block (a schedule and a prompt, no slots) and has no slotted one follows as is.
    A disabled skill offers neither. Every row has a ``key`` unique in the list.
    """
    catalog = load_catalog(scope.with_config(config_module.load(scope)))
    off = {
        name.lower()
        for record in catalog.loadable()
        if record.disabled
        for name in (record.name, record.load_name)
        if name
    }
    rows: list[dict[str, Any]] = []
    slotted: set[str] = set()
    for bp in bps.list_blueprints(scope):
        if bp.skill and bp.skill in off:
            continue
        spec = bps.prefill(bp)
        if bp.skill:
            slotted.add(bp.skill)
        rows.append(
            {
                "key": bp.key,
                "title": bp.title,
                "source": bp.source,
                "skill": bp.skill,
                "description": bp.description,
                "schedule": spec["schedule"],
                "prompt": spec["prompt"] or None,
                "fields": bps.form(bp)["fields"],
            }
        )
    for record in catalog.loadable():
        if record.disabled or record.frontmatter is None:
            continue
        blueprint: object = record.frontmatter.indus.blueprint
        if not isinstance(blueprint, Mapping):
            continue
        skill = record.load_name or record.name
        if skill in slotted:
            continue
        plan: Mapping[str, Any] = blueprint  # pyright: ignore[reportUnknownVariableType]
        schedule = plan.get("schedule")
        rows.append(
            {
                "key": f"{skill}:{PLAIN_BLUEPRINT}",
                "title": skill,
                "source": "skill",
                "skill": skill,
                "description": record.description,
                "schedule": str(schedule) if schedule else None,
                "prompt": str(plan.get("prompt") or "") or None,
                "fields": [],
            }
        )
    return {"blueprints": rows}
