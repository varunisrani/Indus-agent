"""``/api/daemon``: the profile daemon's state and its start / stop / restart / drain buttons
(P18-15).

Nothing here signals a process. The daemon is reached only through its control socket
(``service.gateway_control``; a well-formed ``identify`` answer is liveness, R06.18) and started
only through the attach ensure (``service.gateway_ensure``, the same path a client's first attach
takes).

- ``GET /api/daemon``: ``{running, pid, profile, code_version, state, draining}``.
- ``POST /api/daemon/start|stop|restart``: background actions (``web.actions``; 202 with the action
  name, tail it with ``/api/actions/{name}/status``). Start = ensure. Stop = the owner-only control
  verb ``stop`` (``{reason, restart}``), then wait until the socket stops answering. Restart = stop
  with ``restart: true``, then ensure. A daemon that does not know the ``stop`` verb makes the
  action fail with a plain message naming ``indus gateway stop``.
- ``POST /api/daemon/drain {action: drain|cancel}``: the service-bearer route (P18-04's
  ``DRAIN_ROUTE``); sends the daemon's ``drain`` verb (``{on}``) and echoes its answer. It acts on
  the backend's own profile unless ``?profile=`` names another one (a bearer caller does not know
  the profile list), so it is the one write route here without ``write_scope``.

Seams (``DashboardState.extras``): ``daemon.query`` replaces ``gateway_control.query`` and
``daemon.ensure`` replaces ``gateway_ensure.ensure`` (tests).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from typing import Annotated, Any, Final, NoReturn, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from indus.foundation.scope import Scope
from indus.service import gateway_control
from indus.service.gateway_control import ControlReply
from indus.web.actions import ActionBusy, Say, actions_of
from indus.web.auth.service_bearer import DRAIN_ROUTE
from indus.web.scope import read_scope, write_scope
from indus.web.state import dashboard_state

__all__ = [
    "ROUTE_LIST",
    "STOP_VERB",
    "Query",
    "daemon_view",
    "query_of",
    "router",
]

router = APIRouter()

ROUTE_LIST: Final[tuple[tuple[str, str, bool], ...]] = (
    ("GET", "/api/daemon", True),
    ("POST", "/api/daemon/start", True),
    ("POST", "/api/daemon/stop", True),
    ("POST", "/api/daemon/restart", True),
    ("POST", DRAIN_ROUTE, True),
)
"""``(method, path, scoped)`` of every route here."""

STOP_VERB: Final = "stop"
DRAIN_VERB: Final = "drain"
STOP_WAIT_S: Final = 60.0
_POLL_S: Final = 0.25
_NOT_SUPPORTED: Final = frozenset({"unknown_verb", "unsupported"})
_TICK: Final = threading.Event()  # never set: a pause between polls of the socket

type Query = Callable[..., ControlReply]

ReadScope = Annotated[Scope, Depends(read_scope)]
WriteScope = Annotated[Scope, Depends(write_scope)]


def _fail(status: int, code: str, message: str) -> NoReturn:
    raise HTTPException(status_code=status, detail={"code": code, "message": message})


def query_of(extras: Mapping[str, Any]) -> Query:
    found = extras.get("daemon.query")
    return cast(Query, found) if callable(found) else gateway_control.query


def _ensure_of(extras: Mapping[str, Any]) -> Callable[..., Any]:
    found = extras.get("daemon.ensure")
    if callable(found):
        return cast(Callable[..., Any], found)
    from indus.service.gateway_ensure import ensure

    return ensure


def daemon_view(query: Query, scope: Scope) -> dict[str, Any]:
    """What the control socket says about the daemon serving ``scope``."""
    who = query(scope.home, "identify", timeout=2.0)
    if not who.ok or who.result is None:
        return {"running": False, "state": "stopped", "pid": None}
    found = who.result
    view: dict[str, Any] = {
        "running": True,
        "pid": found.get("pid"),
        "profile": found.get("profile"),
        "code_version": found.get("code_version"),
        "state": "running",
        "draining": False,
    }
    status = query(scope.home, "status", timeout=2.0)
    if status.ok and status.result is not None:
        state = status.result.get("state")
        if isinstance(state, str):
            view["state"] = state
            view["draining"] = state == "draining"
    return view


@router.get("/api/daemon")
def get_daemon(request: Request, scope: ReadScope) -> dict[str, Any]:
    return daemon_view(query_of(dashboard_state(request.app).extras), scope)


def _env(scope: Scope) -> dict[str, str]:
    return {k: v for k, v in scope.env.items() if not k.startswith("INDUS_DASHBOARD_")}


def _start(ensure: Callable[..., Any], scope: Scope, say: Say) -> int:
    say("starting the daemon")
    verdict = ensure(scope.home, _env(scope), mode="start")
    state = str(getattr(verdict, "state", "failed"))
    detail = str(getattr(verdict, "detail", "") or getattr(verdict, "reason", ""))
    say(f"daemon: {state}" + (f" ({detail})" if detail else ""))
    return 0 if state == "ready" else 1


def _stop(query: Query, scope: Scope, say: Say, *, restart: bool) -> int:
    who = query(scope.home, "identify", timeout=2.0)
    if not who.ok:
        say("the daemon is not running")
        return 0 if restart else 1
    reply = query(scope.home, STOP_VERB, {"reason": "dashboard", "restart": restart}, timeout=10.0)
    if not reply.ok:
        if reply.error in _NOT_SUPPORTED:
            say(
                "this daemon does not take a remote stop; run `indus gateway stop` "
                "(or `indus gateway restart`) in a terminal"
            )
        else:
            say(f"the daemon refused to stop: {reply.error}")
        return 1
    say("stop requested; waiting for the daemon to finish")
    deadline = time.monotonic() + STOP_WAIT_S
    while time.monotonic() < deadline:
        if not query(scope.home, "identify", timeout=1.0).ok:
            say("the daemon stopped")
            return 0
        _TICK.wait(_POLL_S)
    say("the daemon is still running after the wait")
    return 1


def _action(request: Request, scope: Scope, name: str, job: Callable[[Say], int]) -> Any:
    try:
        actions_of(dashboard_state(request.app).extras).start_job(scope, name, job)
    except ActionBusy:
        _fail(409, "action_running", f"{name} is already running for this profile.")
    return {"ok": True, "action": name, "profile": scope.profile_id}


@router.post("/api/daemon/start", status_code=202)
def start(request: Request, scope: WriteScope) -> Any:
    ensure = _ensure_of(dashboard_state(request.app).extras)
    return _action(request, scope, "daemon-start", lambda say: _start(ensure, scope, say))


@router.post("/api/daemon/stop", status_code=202)
def stop(request: Request, scope: WriteScope) -> Any:
    query = query_of(dashboard_state(request.app).extras)
    return _action(
        request, scope, "daemon-stop", lambda say: _stop(query, scope, say, restart=False)
    )


@router.post("/api/daemon/restart", status_code=202)
def restart(request: Request, scope: WriteScope) -> Any:
    extras = dashboard_state(request.app).extras
    query, ensure = query_of(extras), _ensure_of(extras)

    def job(say: Say) -> int:
        code = _stop(query, scope, say, restart=True)
        if code != 0:
            return code
        return _start(ensure, scope, say)

    return _action(request, scope, "daemon-restart", job)


class DrainBody(BaseModel):
    action: str = "drain"


@router.post(DRAIN_ROUTE)
def drain(request: Request, scope: ReadScope, body: DrainBody | None = None) -> dict[str, Any]:
    action = (body.action if body is not None else "drain").strip().lower()
    if action not in ("drain", "cancel"):
        _fail(400, "bad_action", "action must be drain or cancel.")
    query = query_of(dashboard_state(request.app).extras)
    reply = query(scope.home, DRAIN_VERB, {"on": action == "drain"}, timeout=5.0)
    if not reply.ok:
        if not reply.answered:
            _fail(409, "daemon_not_running", "The daemon is not running.")
        _fail(502, "drain_refused", f"The daemon refused: {reply.error}.")
    result = reply.result or {}
    return {
        "ok": True,
        "action": action,
        "draining": bool(result.get("draining")),
        "active_turns": result.get("active_turns"),
    }
