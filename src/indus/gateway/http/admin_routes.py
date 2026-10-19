"""``/api`` routes of the API server: stored sessions and the P14 cron jobs (P15-31).

Sessions (read only): ``GET /api/sessions`` (``limit`` ≤ 200, ``offset``, ``query``) lists
conversations from the profile's store (``state.listing``); ``GET /api/sessions/{id}`` gives one
session row (without its system prompt) plus the background completions the API server recorded
for it (the rows pollers read).

Jobs go through the P14 cron backend (``cron.ports.backend_for``, one way in):
``GET|POST /api/jobs``, ``GET|PATCH|DELETE /api/jobs/{id}``, ``POST /api/jobs/{id}/pause``,
``/resume``, ``/run`` and ``POST /api/cron/fire {"job_id"}``. Job ids are 12 lower-case hex
characters (400 otherwise). Bodies are checked before the store sees them: only ``name`` (≤200),
``schedule``, ``prompt`` (≤5,000), ``deliver``, ``skills`` and ``repeat`` (≥1) are accepted; a
violation is 422. Scripts, working folders and models cannot be set over HTTP. A run (``/run``,
``/api/cron/fire``) starts in the background and answers 202.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from indus.cron import ports
from indus.cron.ports import CronBackend, JobAmbiguous, JobNotFound, StoreUnavailable
from indus.cron.tool import job_view
from indus.foundation.blocking import run_blocking
from indus.gateway.http.serving import BadRequest, read_json
from indus.safety.redaction import redact_output
from indus.state import listing, sessions

if TYPE_CHECKING:
    from indus.gateway.http.api_server import ApiContext

__all__ = ["JOB_ID_RE", "JobCreate", "JobUpdate", "default_cron", "router"]

log = logging.getLogger(__name__)

JOB_ID_RE = re.compile(r"^[0-9a-f]{12}$")
NAME_MAX = 200
PROMPT_MAX = 5_000
LIST_MAX = 200


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=NAME_MAX)
    schedule: str = Field(min_length=1, max_length=500)
    prompt: str = Field(default="", max_length=PROMPT_MAX)
    deliver: str | None = Field(default=None, max_length=500)
    skills: list[str] = Field(default_factory=list[str], max_length=50)
    repeat: int | None = Field(default=None, ge=1)


class JobUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=NAME_MAX)
    schedule: str | None = Field(default=None, min_length=1, max_length=500)
    prompt: str | None = Field(default=None, max_length=PROMPT_MAX)
    deliver: str | None = Field(default=None, max_length=500)
    skills: list[str] | None = Field(default=None, max_length=50)
    repeat: int | None = Field(default=None, ge=1)


def default_cron(ctx: ApiContext) -> Callable[[], CronBackend]:
    def backend() -> CronBackend:
        from indus.service import cron_host

        cron_host.install()
        return ports.backend_for(ctx.scope)

    return backend


def _validated[M: BaseModel](model: type[M], body: object) -> M:
    try:
        return model.model_validate(body)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"]) or None
        raise BadRequest(
            f"{where or 'body'}: {first['msg']}", status=422, code="validation_error", param=where
        ) from None


def _job_id(request: Request) -> str:
    job_id = str(request.path_params["job_id"])
    if not JOB_ID_RE.match(job_id):
        raise BadRequest("job ids are 12 lower-case hex characters", code="invalid_job_id")
    return job_id


def _job_out(job: Any) -> dict[str, Any]:
    view = job_view(job)
    prompt = ports.field(job, "prompt")
    if prompt:
        view["prompt"] = prompt
    return {
        key: (value.isoformat() if hasattr(value, "isoformat") else value)
        for key, value in view.items()
    }


def _session_out(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in row.items() if k not in {"system_prompt", "system_prompt_hash"}}
    return {"object": "indus.session", **out}


def router(ctx: ApiContext) -> APIRouter:
    from indus.gateway.http.api_server import MAX_REQUEST_BYTES

    api = APIRouter()
    cron_of = ctx.cron or default_cron(ctx)

    def store() -> Any:
        if ctx.store is None:
            raise BadRequest(
                "the session store is not available", status=503, code="store_unavailable"
            )
        return ctx.store

    async def cron_call[T](fn: Callable[[CronBackend], T]) -> T:
        def call() -> T:
            return fn(cron_of())

        try:
            return await run_blocking(call, label="cron api")
        except StoreUnavailable as exc:
            raise BadRequest(str(exc), status=503, code="cron_unavailable") from None
        except (JobNotFound, JobAmbiguous):
            raise BadRequest("no job has this id", status=404, code="job_not_found") from None
        except ValueError as exc:
            raise BadRequest(redact_output(str(exc), force=True), code="invalid_job") from None

    # -- sessions -------------------------------------------------------------------------------

    async def list_sessions(request: Request) -> Response:
        query = request.query_params
        try:
            limit = min(LIST_MAX, max(1, int(query.get("limit", "50"))))
            offset = max(0, int(query.get("offset", "0")))
        except ValueError:
            raise BadRequest("limit and offset must be whole numbers") from None
        needle = (query.get("query") or "").strip() or None
        filters = listing.ListFilters(query=needle)
        rows = await run_blocking(
            listing.list_sessions, store(), filters, limit, offset, label="api sessions"
        )
        data = [{"object": "indus.session", **dataclasses.asdict(r)} for r in rows]
        return JSONResponse({"object": "list", "data": data, "limit": limit, "offset": offset})

    async def get_session(request: Request) -> Response:
        session_id = str(request.path_params["session_id"])
        row = await run_blocking(sessions.get, store(), session_id, label="api session")
        if row is None:
            raise BadRequest("no session has this id", status=404, code="session_not_found")
        out = _session_out(row)
        log_ = ctx.completions
        out["completions"] = log_.recent(session_id) if log_ is not None else []
        return JSONResponse(out)

    api.add_api_route("/api/sessions", list_sessions, methods=["GET"])
    api.add_api_route("/api/sessions/{session_id}", get_session, methods=["GET"])

    # -- jobs -----------------------------------------------------------------------------------

    async def list_jobs(request: Request) -> Response:
        include = request.query_params.get("include_disabled", "true").lower() not in {
            "0",
            "false",
            "no",
        }
        jobs = await cron_call(lambda b: b.list_jobs(include_disabled=include))
        return JSONResponse({"object": "list", "data": [_job_out(j) for j in jobs]})

    async def create_job(request: Request) -> Response:
        spec = _validated(JobCreate, await read_json(request, MAX_REQUEST_BYTES))
        fields = spec.model_dump(exclude_none=True)
        if not fields.get("skills"):
            fields.pop("skills", None)
        job = await cron_call(lambda b: b.create(fields))
        return JSONResponse({"job": _job_out(job)}, status_code=201)

    async def get_job(request: Request) -> Response:
        job_id = _job_id(request)
        job = await cron_call(lambda b: b.get(job_id))
        if job is None:
            raise BadRequest("no job has this id", status=404, code="job_not_found")
        return JSONResponse({"job": _job_out(job)})

    async def update_job(request: Request) -> Response:
        job_id = _job_id(request)
        spec = _validated(JobUpdate, await read_json(request, MAX_REQUEST_BYTES))
        changes = spec.model_dump(exclude_unset=True)
        if not changes:
            raise BadRequest("nothing to change", code="no_changes")
        await cron_call(lambda b: b.resolve(job_id))
        job = await cron_call(lambda b: b.update(job_id, changes))
        return JSONResponse({"job": _job_out(job)})

    async def delete_job(request: Request) -> Response:
        job_id = _job_id(request)
        removed = await cron_call(lambda b: b.remove(job_id))
        if not removed:
            raise BadRequest("no job has this id", status=404, code="job_not_found")
        return JSONResponse({"id": job_id, "deleted": True})

    async def pause_job(request: Request) -> Response:
        job_id = _job_id(request)
        await cron_call(lambda b: b.resolve(job_id))
        job = await cron_call(lambda b: b.pause(job_id, "paused over the API"))
        return JSONResponse({"job": _job_out(job)})

    async def resume_job(request: Request) -> Response:
        job_id = _job_id(request)
        await cron_call(lambda b: b.resolve(job_id))
        job = await cron_call(lambda b: b.resume(job_id))
        return JSONResponse({"job": _job_out(job)})

    async def fire(job_id: str) -> Response:
        await cron_call(lambda b: b.resolve(job_id))
        backend = cron_of()

        async def run() -> None:
            try:
                report = await backend.run_now(job_id)
                log.info("API-started run of job %s: %s", job_id, report.status)
            except Exception:
                log.warning("API-started run of job %s failed", job_id, exc_info=True)

        task = asyncio.ensure_future(run())
        ctx.jobs.add(task)  # cancelled and awaited when the API server closes
        task.add_done_callback(ctx.jobs.discard)
        return JSONResponse({"status": "accepted", "job_id": job_id}, status_code=202)

    async def run_job(request: Request) -> Response:
        return await fire(_job_id(request))

    async def cron_fire(request: Request) -> Response:
        body = await read_json(request, MAX_REQUEST_BYTES)
        raw = cast(dict[str, Any], body).get("job_id") if isinstance(body, dict) else None
        if not isinstance(raw, str) or not JOB_ID_RE.match(raw):
            raise BadRequest(
                "'job_id' must be a 12-character hex job id", code="invalid_job_id", param="job_id"
            )
        return await fire(raw)

    api.add_api_route("/api/jobs", list_jobs, methods=["GET"])
    api.add_api_route("/api/jobs", create_job, methods=["POST"])
    api.add_api_route("/api/jobs/{job_id}", get_job, methods=["GET"])
    api.add_api_route("/api/jobs/{job_id}", update_job, methods=["PATCH"])
    api.add_api_route("/api/jobs/{job_id}", delete_job, methods=["DELETE"])
    api.add_api_route("/api/jobs/{job_id}/pause", pause_job, methods=["POST"])
    api.add_api_route("/api/jobs/{job_id}/resume", resume_job, methods=["POST"])
    api.add_api_route("/api/jobs/{job_id}/run", run_job, methods=["POST"])
    api.add_api_route("/api/cron/fire", cron_fire, methods=["POST"])
    return api
