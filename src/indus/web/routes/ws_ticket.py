"""``POST /api/auth/ws-ticket``: a single-use WebSocket ticket for gated mode (P18-06).

A browser cannot put a header on a WebSocket upgrade, and in gated mode the SPA holds no token, so
before each socket it asks this route — behind the login gate, so only with a verified session
cookie — for a ticket: ``{purpose, profile?}`` → ``{ticket, purpose, expires_in}``. The ticket is
good once, for 30 seconds, for that purpose and that profile only (``service.ws_auth``). The answer
is ``no-store`` and the audit log records the minting without the value.

In loopback mode the SPA uses the session token on its sockets, so this route answers 409
``not_gated``. A purpose outside the dashboard's, a profile that does not exist, or a full book are
refused with plain codes.
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from indus.service.ws_auth import DASHBOARD_PURPOSES, TicketRefused
from indus.web.auth.audit import AuditEvent
from indus.web.auth.gate import session_of
from indus.web.auth.proxy import client_ip
from indus.web.state import dashboard_state

__all__ = ["TicketRequest", "router"]

router = APIRouter()

_NO_STORE: Final = {"cache-control": "no-store"}


class TicketRequest(BaseModel):
    purpose: str = Field(max_length=64)
    profile: str = Field(default="", max_length=128)


@router.post("/api/auth/ws-ticket")
async def mint_ticket(request: Request, body: TicketRequest) -> JSONResponse:
    state = dashboard_state(request.app)
    if not state.gated:
        return JSONResponse(
            {"error": "not_gated", "detail": "Use the session token on this server's sockets."},
            status_code=409,
            headers=_NO_STORE,
        )
    session = session_of(request)
    if session is None:
        return JSONResponse(
            {"error": "unauthenticated", "detail": "Sign in first."},
            status_code=401,
            headers=_NO_STORE,
        )
    if body.purpose not in DASHBOARD_PURPOSES or body.purpose == "web-pub":
        return JSONResponse(
            {"error": "unknown_purpose", "detail": "No socket has that purpose."},
            status_code=400,
            headers=_NO_STORE,
        )
    profile = state.resolve_profile(body.profile)
    if profile is None:
        return JSONResponse(
            {"error": "unknown_profile", "detail": "No such profile."},
            status_code=404,
            headers=_NO_STORE,
        )
    try:
        minted = state.tickets.mint(body.purpose, profile_id=profile.stable_id)
    except TicketRefused as refused:
        state.auth.audit.record(
            AuditEvent.WS_TICKET_REFUSED,
            purpose=body.purpose,
            reason=refused.reason,
            user_id=session.user_id,
            ip=client_ip(request.scope),
        )
        return JSONResponse(
            {"error": "ticket_refused", "detail": "Too many sockets are opening; try again."},
            status_code=429,
            headers=_NO_STORE,
        )
    state.auth.audit.record(
        AuditEvent.WS_TICKET_MINTED,
        purpose=body.purpose,
        profile=profile.name,
        user_id=session.user_id,
        ip=client_ip(request.scope),
    )
    return JSONResponse(
        {"ticket": minted.ticket, "purpose": minted.purpose, "expires_in": minted.expires_in},
        headers=_NO_STORE,
    )
