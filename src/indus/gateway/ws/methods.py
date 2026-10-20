"""The methods the daemon's WebSocket serves (P17-13/14), over the daemon session host.

Every session-addressed call first checks that this connection attached the session
(``Subscriptions.require``): another connection's session reads as unknown. Controls name the
turn they target: ``session.interrupt``, ``session.steer`` and ``server_request.answer`` need
``generation`` here, and a stale one changes nothing (the method's "nothing happened" answer).

``prompt.submit`` admits the text into the session's durable queue. With ``when_busy`` left out
the submitting surface decides what happens while a turn runs (TUI queues, others interrupt);
``queue`` queues; ``reject`` answers ``SESSION_BUSY`` while a turn runs; ``steer`` / ``redirect``
are refused — a steer names its generation through ``session.steer``. ``turn_id`` is the
admission id.

``session.close`` only detaches this connection (other viewers and running turns carry on).
``server_request.decline`` records this viewer's refusal (``4404`` "not shown here" when it
advertised ``request.decline``, else ``-32601``): the request stays with the other viewers and is
settled undeliverable when none is left.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from indus.protocol.attach import (
    EventsSinceParams,
    EventsSinceResult,
    ReplayGapInfo,
    SessionSubscribeParams,
    SessionSubscribeResult,
    SessionsWatchParams,
    SessionsWatchResult,
    SessionUnsubscribeParams,
    SessionUnsubscribeResult,
)
from indus.protocol.bridge import (
    GatewayPingParams,
    GatewayPingResult,
    ServerRequestDeclineParams,
    ServerRequestDeclineResult,
    SessionCloseParams,
    SessionCloseResult,
)
from indus.protocol.errors import INVALID_PARAMS, SESSION_BUSY
from indus.protocol.methods import (
    PROTOCOL_VERSION,
    BusyAction,
    ClientCapabilitiesParams,
    ClientCapabilitiesResult,
    PromptSubmitParams,
    PromptSubmitResult,
    ProtocolError,
    ServerRequestAnswerParams,
    ServerRequestAnswerResult,
    SessionCreateParams,
    SessionCreateResult,
    SessionInterruptParams,
    SessionInterruptResult,
    SessionResumeParams,
    SessionResumeResult,
    SessionSteerParams,
    SessionSteerResult,
    SubmitStatus,
    negotiate_features,
)
from indus.service.event_log import ReplayGap
from indus.service.fences import ControlApplied, ControlStatus, Responded, StaleGeneration
from indus.service.request_redelivery import NOT_SHOWN_CODE, UNRENDERABLE_CODE
from indus.service.rpc import MethodSet, MethodTable
from indus.service.viewers import DECLINE_CAPABILITY, BusyMode
from indus.state.runtime_ledger import LedgerRefusal

if TYPE_CHECKING:
    from indus.gateway.ws.server import WsConnection

__all__ = ["methods", "served", "table"]

methods = MethodSet["WsConnection"]()


def _generation(value: int | None) -> int:
    if value is None:
        raise ProtocolError(
            INVALID_PARAMS, "the daemon needs the generation of the turn", path="generation"
        )
    return value


def _took(result: ControlApplied | StaleGeneration) -> bool:
    """A control reached its turn (or waits for it); False for a stale or refused one."""
    return isinstance(result, ControlApplied) and result.status is not ControlStatus.REJECTED


def _gap(gap: ReplayGap | None) -> ReplayGapInfo | None:
    if gap is None:
        return None
    return ReplayGapInfo(reason=gap.reason, replay_epoch=gap.replay_epoch, last_seq=gap.last_seq)


# -- connection -----------------------------------------------------------------------------------


@methods.method("gateway.ping")
async def ping(conn: WsConnection, params: GatewayPingParams) -> GatewayPingResult:
    del conn, params
    return GatewayPingResult(protocol_version=PROTOCOL_VERSION)


@methods.method("client.capabilities")
async def capabilities(
    conn: WsConnection, params: ClientCapabilitiesParams
) -> ClientCapabilitiesResult:
    conn.subs.configure(surface=params.surface, features=params.features)
    enabled = set(negotiate_features(params.features))
    if DECLINE_CAPABILITY in params.features:
        enabled.add(DECLINE_CAPABILITY)
    return ClientCapabilitiesResult(features=sorted(enabled))


# -- sessions -------------------------------------------------------------------------------------


@methods.method("session.create")
async def create(conn: WsConnection, params: SessionCreateParams) -> SessionCreateResult:
    result = await conn.host.create(params, source=conn.subs.surface)
    conn.subs.adopt(result.session_id, cursor=0)
    return result


@methods.method("session.resume")
async def resume(conn: WsConnection, params: SessionResumeParams) -> SessionResumeResult:
    result = await conn.host.resume(params.session_id, source=conn.subs.surface)
    attached = conn.subs.adopt(result.session_id)
    return result.model_copy(update={"last_seq": attached.last_seq})


@methods.method("session.close")
async def close(conn: WsConnection, params: SessionCloseParams) -> SessionCloseResult:
    return SessionCloseResult(closed=await conn.subs.unsubscribe(params.session_id))


@methods.method("session.subscribe")
async def subscribe(conn: WsConnection, params: SessionSubscribeParams) -> SessionSubscribeResult:
    found = await conn.subs.subscribe(params.session_id, params.epoch, params.cursor)
    return SessionSubscribeResult(
        session_id=found.session_id,
        replay_epoch=found.replay_epoch,
        last_seq=found.last_seq,
        replayed=found.replayed,
        gap=_gap(found.gap),
        reoffered=found.reoffered,
    )


@methods.method("session.unsubscribe")
async def unsubscribe(
    conn: WsConnection, params: SessionUnsubscribeParams
) -> SessionUnsubscribeResult:
    return SessionUnsubscribeResult(unsubscribed=await conn.subs.unsubscribe(params.session_id))


@methods.method("sessions.watch")
async def watch(conn: WsConnection, params: SessionsWatchParams) -> SessionsWatchResult:
    return SessionsWatchResult(watching=conn.subs.watch(params.enabled))


@methods.method("events.since")
async def events_since(conn: WsConnection, params: EventsSinceParams) -> EventsSinceResult:
    found = conn.subs.since(params.session_id, params.epoch, params.cursor)
    return EventsSinceResult(
        session_id=found.session_id,
        replay_epoch=found.replay_epoch,
        last_seq=found.last_seq,
        events=list(found.events),
        gap=_gap(found.gap),
        reoffered=found.reoffered,
    )


# -- input and controls ---------------------------------------------------------------------------


def _busy_mode(conn: WsConnection, key: str, params: PromptSubmitParams) -> BusyMode | None:
    if "when_busy" not in params.model_fields_set:
        return None  # the surface decides (R05.18)
    action = params.when_busy
    if action is BusyAction.QUEUE:
        return "queue"
    if action is BusyAction.REJECT:
        if conn.host.admissions.running(key) is not None:
            raise ProtocolError(SESSION_BUSY, "a turn is running in this session")
        return "queue"
    raise ProtocolError(
        INVALID_PARAMS,
        "steer the running turn with session.steer and its generation",
        path="when_busy",
    )


@methods.method("prompt.submit")
async def submit(conn: WsConnection, params: PromptSubmitParams) -> PromptSubmitResult:
    key = conn.subs.require(params.session_id)
    busy = _busy_mode(conn, key, params)
    was_running = conn.host.admissions.running(key)
    try:
        receipt = await conn.host.submit(
            key,
            params.text,
            principal=f"{conn.subs.surface}:{conn.connection_id}",
            request_id=f"ws-{secrets.token_hex(8)}",
            connection_id=conn.connection_id,
            surface=conn.subs.surface,
            busy=busy,
        )
    except LedgerRefusal as refusal:
        raise ProtocolError(SESSION_BUSY, f"the input was not admitted: {refusal.reason}") from None
    running = conn.host.admissions.running(key)
    generation = receipt.generation
    if generation is None and running is not None:
        generation = running.generation
    starts_now = (
        receipt.state == "started"
        or (was_running is None and receipt.blocked_by is None and receipt.position in (None, 1))
    ) and receipt.state != "terminal"
    return PromptSubmitResult(
        turn_id=receipt.admission_id,
        generation=generation or 0,
        status=SubmitStatus.STREAMING if starts_now else SubmitStatus.QUEUED,
    )


@methods.method("session.interrupt")
async def interrupt(conn: WsConnection, params: SessionInterruptParams) -> SessionInterruptResult:
    key = conn.subs.require(params.session_id)
    result = conn.host.interrupt(key, _generation(params.generation), hard=params.hard)
    return SessionInterruptResult(
        interrupted=isinstance(result, ControlApplied)
        and result.status is not ControlStatus.REJECTED
    )


@methods.method("session.steer")
async def steer(conn: WsConnection, params: SessionSteerParams) -> SessionSteerResult:
    key = conn.subs.require(params.session_id)
    result = conn.host.steer(key, _generation(params.generation), params.text)
    return SessionSteerResult(queued=_took(result))


@methods.method("server_request.answer")
async def answer(
    conn: WsConnection, params: ServerRequestAnswerParams
) -> ServerRequestAnswerResult:
    key = conn.subs.require(params.session_id)
    result = conn.host.respond(
        key, params.request_id, _generation(params.generation), params.answer
    )
    return ServerRequestAnswerResult(accepted=isinstance(result, Responded) and result.accepted)


@methods.method("server_request.decline")
async def decline(
    conn: WsConnection, params: ServerRequestDeclineParams
) -> ServerRequestDeclineResult:
    key = conn.subs.require(params.session_id)
    code = NOT_SHOWN_CODE if DECLINE_CAPABILITY in conn.subs.capabilities else UNRENDERABLE_CODE
    return ServerRequestDeclineResult(
        accepted=conn.host.refuse(key, conn.connection_id, params.request_id, code)
    )


def served() -> list[str]:
    """Method names the daemon's WebSocket serves."""
    return methods.names()


def table() -> MethodTable[WsConnection]:
    """A fresh table with every handler of this module."""
    built = MethodTable["WsConnection"]()
    methods.register(built)
    return built
