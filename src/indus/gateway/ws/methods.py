"""The methods the daemon's WebSocket serves (P17-13/14), over the daemon session host.

Every session-addressed call first checks that this connection attached the session
(``Subscriptions.require``): another connection's session reads as unknown. Controls name the
turn they target: ``session.interrupt``, ``session.steer`` and ``server_request.answer`` need
``generation`` here, and a stale one changes nothing (the method's "nothing happened" answer).

``prompt.submit`` admits the text into the session's durable queue. With ``when_busy`` left out
the submitting surface decides what happens while a turn runs (TUI queues, others interrupt);
``queue`` queues; ``reject`` answers ``SESSION_BUSY`` while a turn runs; ``steer`` / ``redirect``
are refused — a steer names its generation through ``session.steer``. ``turn_id`` is the
admission id. A ``request_id`` from the client is the admission request id (under one principal
per surface), so sending the same input again returns its first admission. ``unattended`` (a
finite ``-z`` run) makes the input's turn auto-approve and ``single_query`` (a finite ``chat -q`` or
``-z``) runs it one-shot (``DaemonSessionHost``). An input that
would start at once (idle session, nothing queued) is answered once the worker has claimed it
(at most ``CLAIM_WAIT_S``), so ``generation`` names its turn; an input still unclaimed after that
is answered ``queued`` with the running turn's generation, or none when no turn runs.

``session.close`` only detaches this connection (other viewers and running turns carry on).
``server_request.decline`` records this viewer's refusal (``4404`` "not shown here" when it
advertised ``request.decline``, else ``-32601``): the request stays with the other viewers and is
settled undeliverable when none is left.

``admission.discard`` resolves a lost (``unknown``) turn of a session this connection watches, by
its generation; the lost turn is closed in its transcript with a short note in the same
transaction (``admissions.close_lost_turn``). Another session's admission reads as not found.
``admission.receipt`` reads one input of a watched session (``ADMISSION_NOT_FOUND`` for another
session's). ``admission.cancel`` drops a queued input of a watched session; one already claimed is
answered ``cancelled: false, state: started`` with its generation (a turn is stopped only through
``session.interrupt``), a lost one ``state: unknown``. ``session.usage`` reads a watched
session's counters (``usage_numbers``, the shape the turn-end ``session.usage`` event carries).
A refused submit answers the table code of its ledger reason (``ADMISSION_CONFLICT``,
``QUEUE_FULL``, ``SESSION_REFUSED`` for a deleted session, else ``SESSION_BUSY``); an exact retry
of an input whose turn was lost in a restart answers ``OUTCOME_UNKNOWN`` (never "streaming": that
turn never comes back).

``session.mutate`` changes a watched session (the editor's MCP servers, fork, folder, model,
title, history: ``service.session_mutations``), fenced against its running turn and idempotent by
the client's ``request_id``.

The TUI's pickers (W47-c): ``complete.slash``, ``model.options`` and ``session.list`` answer what
the in-process bridge answers for the profile (``service.picker_reads``). A named ``session_id``
must be one this connection attached (another's reads as unknown); ``session.list`` lists the
profile's stored conversations, as ``--local`` does (every attached client is the owner); its rows
carry the title, the source and the model the session last ran on.

The TUI's other reads (W48-b): ``complete.path`` (``service.path_completion``: the client's folder,
else the attached session's, listed through the profile's terminal backend), ``commands.catalog``
(``picker_reads.command_catalog`` for the connection's surface) and ``config.get``
(``service.client_settings``; the session keys read the attached session's live switches, and
``verbose`` is the profile's tool-progress mode: the daemon keeps none per viewer) answer what the
bridge answers. ``config.set`` is not served (D-16.549).

Server-run slash commands (W48-h): ``slash.exec`` and ``command.dispatch`` for a session this
connection attached run ``service.slash_commands`` with the daemon as host
(``service.daemon_slash``), so they answer what the bridge answers; what a shared daemon does not
run (``picker_reads.DAEMON_REFUSED``, quick ``exec`` commands) is refused with its reason
(``SLASH_NOT_OWNED`` from ``slash.exec``, ``UNSUPPORTED_COMMAND`` from ``command.dispatch``) and
``commands.catalog`` lists it ``unavailable``. The header a status bar shows (``session.info``,
``session.title``) reaches the session's viewers from the host
(``DaemonSessionHost.announce_info``).

``runtime.describe`` answers what the control socket's ``describe`` does, plus the replay epoch.
``maintenance.run`` runs one bulk store operation on the daemon's own store handle
(``service.maintenance_gate.MaintenanceVerb``); a refusal is the result's ``refused``.
"""

from __future__ import annotations

import contextlib
import secrets
from typing import TYPE_CHECKING, Any, Final

from indus.foundation.blocking import run_blocking
from indus.gateway.control_socket import code_version
from indus.protocol.attach import (
    AdmissionCancelParams,
    AdmissionCancelResult,
    AdmissionDiscardParams,
    AdmissionDiscardResult,
    AdmissionReceiptParams,
    AdmissionReceiptResult,
    EventsSinceParams,
    EventsSinceResult,
    MaintenanceRunParams,
    MaintenanceRunResult,
    ReplayGapInfo,
    RuntimeDescribeParams,
    RuntimeDescribeResult,
    SessionMutateParams,
    SessionMutateResult,
    SessionSubscribeParams,
    SessionSubscribeResult,
    SessionsWatchParams,
    SessionsWatchResult,
    SessionUnsubscribeParams,
    SessionUnsubscribeResult,
)
from indus.protocol.bridge import (
    CommandDispatchParams,
    CommandDispatchResult,
    CommandsCatalogParams,
    CommandsCatalogResult,
    CompletePathParams,
    CompleteResult,
    CompleteSlashParams,
    ConfigGetParams,
    ConfigGetResult,
    GatewayPingParams,
    GatewayPingResult,
    ModelOptionsParams,
    ModelOptionsResult,
    ServerRequestDeclineParams,
    ServerRequestDeclineResult,
    SessionCloseParams,
    SessionCloseResult,
    SessionListParams,
    SessionListResult,
    SessionUsageParams,
    SessionUsageResult,
    SlashExecParams,
    SlashExecResult,
    UsageNumbers,
)
from indus.protocol.errors import (
    ADMISSION_NOT_FOUND,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    OUTCOME_UNKNOWN,
    SESSION_BUSY,
    ErrorCode,
    error_for_indus_code,
)
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
from indus.service import client_settings, path_completion, picker_reads, slash_commands
from indus.service.admissions import Receipt, close_lost_turn
from indus.service.commands.registry import Surface
from indus.service.daemon_slash import DaemonSlashHost
from indus.service.event_log import ReplayGap
from indus.service.fences import ControlApplied, ControlStatus, Responded, StaleGeneration
from indus.service.maintenance_gate import MaintenanceVerb
from indus.service.request_redelivery import NOT_SHOWN_CODE, UNRENDERABLE_CODE
from indus.service.rpc import MethodSet, MethodTable
from indus.service.session_service import usage_numbers
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


@methods.method("runtime.describe")
async def describe(conn: WsConnection, params: RuntimeDescribeParams) -> RuntimeDescribeResult:
    del params
    book = conn.endpoint.book
    return RuntimeDescribeResult(
        profile_id=book.profile_id,
        instance_id=book.instance_id,
        code_version=code_version(),
        protocol_version=PROTOCOL_VERSION,
        replay_epoch=conn.host.replay_epoch,
        ws_url=conn.endpoint.ws_url,
        creatable_sources=list(conn.host.creatable_sources()),
        capabilities=list(conn.host.capabilities()),
    )


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
    # P24-14: a session started in a project's folder joins it in ``SessionService.create``.
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


def _refusal_code(reason: str) -> ErrorCode:
    """The table code of a ledger refusal on submit (``SESSION_BUSY`` when it has none)."""
    found = error_for_indus_code(f"runtime.{reason}")
    return SESSION_BUSY if found is INTERNAL_ERROR else found


CLAIM_WAIT_S: Final = 5.0
"""How long ``prompt.submit`` waits for the claim of an input that starts at once."""


@methods.method("prompt.submit")
async def submit(conn: WsConnection, params: PromptSubmitParams) -> PromptSubmitResult:
    key = conn.subs.require(params.session_id)
    busy = _busy_mode(conn, key, params)
    was_running = conn.host.admissions.running(key)
    if params.request_id is not None:
        # The client's own id: one principal per surface (every attached client is the owner),
        # so an explicit retry over a new connection is an exact retry (P17-17).
        principal, request_id = f"attach:{conn.subs.surface}", params.request_id
    else:
        principal = f"{conn.subs.surface}:{conn.connection_id}"
        request_id = f"ws-{secrets.token_hex(8)}"
    try:
        receipt = await conn.host.submit(
            key,
            params.text,
            principal=principal,
            request_id=request_id,
            connection_id=conn.connection_id,
            surface=conn.subs.surface,
            busy=busy,
            unattended=params.unattended,
            single_query=params.single_query,
        )
    except LedgerRefusal as refusal:
        raise ProtocolError(
            _refusal_code(refusal.reason), f"the input was not admitted: {refusal.reason}"
        ) from None
    if receipt.state == "unknown":
        raise ProtocolError(
            OUTCOME_UNKNOWN, "this input's turn was lost in a restart; it is never run again"
        )
    starts_now = (
        receipt.state == "started"
        or (was_running is None and receipt.blocked_by is None and receipt.position in (None, 1))
    ) and receipt.state != "terminal"
    if starts_now and receipt.state == "queued":
        # Idle session: the worker claims it at once. Answer with that turn's generation, never
        # a placeholder (a client's controls name it).
        receipt = await conn.host.claimed(receipt.admission_id, CLAIM_WAIT_S)
        if receipt.state == "unknown":
            raise ProtocolError(
                OUTCOME_UNKNOWN, "this input's turn was lost in a restart; it is never run again"
            )
    generation = receipt.generation
    if receipt.state == "queued":
        starts_now = False
        running = conn.host.admissions.running(key)
        generation = running.generation if running is not None else None
    return PromptSubmitResult(
        turn_id=receipt.admission_id,
        generation=generation,
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


@methods.method("admission.discard")
async def discard(conn: WsConnection, params: AdmissionDiscardParams) -> AdmissionDiscardResult:
    key = conn.subs.require(params.session_id)
    try:
        receipt = await conn.host.receipt(params.admission_id)
    except LedgerRefusal:
        return AdmissionDiscardResult(discarded=False, reason="not_found")
    if receipt.session_id != key:
        return AdmissionDiscardResult(discarded=False, reason="not_found")  # another session's
    clock = conn.host.store.scope.clock
    try:
        await conn.host.discard_unknown(
            params.admission_id,
            params.generation,
            terminal_write=close_lost_turn(now=lambda: clock.now().timestamp()),
        )
    except LedgerRefusal as refusal:
        reason = "stale_generation" if refusal.reason == "stale_generation" else "not_found"
        return AdmissionDiscardResult(discarded=False, reason=reason)
    return AdmissionDiscardResult(discarded=True)


def _receipt_result(receipt: Receipt) -> AdmissionReceiptResult:
    return AdmissionReceiptResult(
        admission_id=receipt.admission_id,
        session_id=receipt.session_id,
        state=receipt.state,
        generation=receipt.generation,
        position=receipt.position,
        outcome=receipt.outcome,
        blocked_by=receipt.blocked_by,
        retired=receipt.retired,
    )


@methods.method("admission.receipt")
async def receipt(conn: WsConnection, params: AdmissionReceiptParams) -> AdmissionReceiptResult:
    key = conn.subs.require(params.session_id)
    try:
        found = await conn.host.receipt(params.admission_id)
    except LedgerRefusal:
        found = None
    if found is None or found.session_id != key:
        raise ProtocolError(ADMISSION_NOT_FOUND, path="admission_id")  # or another session's
    return _receipt_result(found)


@methods.method("admission.cancel")
async def cancel(conn: WsConnection, params: AdmissionCancelParams) -> AdmissionCancelResult:
    key = conn.subs.require(params.session_id)
    try:
        found = await conn.host.receipt(params.admission_id)
    except LedgerRefusal:
        found = None
    if found is None or found.session_id != key:
        raise ProtocolError(ADMISSION_NOT_FOUND, path="admission_id")  # or another session's
    if found.state == "queued":
        with contextlib.suppress(LedgerRefusal):  # claimed meanwhile: answered as it stands
            found = await conn.host.admissions.cancel_queued(params.admission_id)
        if found.state != "terminal":
            found = await conn.host.receipt(params.admission_id)
    return AdmissionCancelResult(
        cancelled=found.state == "terminal" and found.outcome == "cancelled",
        state=found.state,
        generation=found.generation,
        outcome=found.outcome,
    )


@methods.method("session.usage")
async def usage(conn: WsConnection, params: SessionUsageParams) -> SessionUsageResult:
    key = conn.subs.require(params.session_id)
    numbers = usage_numbers(conn.host.service, conn.host.service.current_id(key))
    return SessionUsageResult(usage=UsageNumbers.model_validate(numbers))


@methods.method("session.mutate")
async def mutate(conn: WsConnection, params: SessionMutateParams) -> SessionMutateResult:
    key = conn.subs.require(params.session_id)
    return await conn.host.mutations.mutate(key, params)


@methods.method("maintenance.run")
async def maintenance(conn: WsConnection, params: MaintenanceRunParams) -> MaintenanceRunResult:
    store = conn.host.store
    verb = MaintenanceVerb(conn.scope, store=lambda: store, release=lambda _store: None)
    answer = dict(await verb.handle(params.model_dump(exclude_none=True)))
    refused, detail = answer.pop("refused", None), answer.pop("detail", None)
    return MaintenanceRunResult(
        op=params.op,
        refused=str(refused) if refused is not None else None,
        detail=str(detail) if detail is not None else None,
        report=answer,
    )


# -- the TUI's pickers (W47-c) --------------------------------------------------------------------

_COMMAND_SURFACE: Final[dict[str, Surface]] = {"cli": "cli", "gui": "desktop"}
"""The command registry's surface for a connection's surface kind (the TUI's otherwise)."""


@methods.method("complete.slash")
async def complete_slash(conn: WsConnection, params: CompleteSlashParams) -> CompleteResult:
    if params.session_id is not None:
        conn.subs.require(params.session_id)
    surface = _COMMAND_SURFACE.get(conn.subs.surface, "tui")
    return await run_blocking(
        lambda: picker_reads.slash_completions(
            conn.scope,
            params.text,
            surface=surface,
            session_id=params.session_id,
            quick_exec=False,  # the daemon does not run them (W48-h)
        ),
        label="slash completion",
    )


@methods.method("model.options")
async def model_options(conn: WsConnection, params: ModelOptionsParams) -> ModelOptionsResult:
    if params.session_id is not None:
        key = conn.subs.require(params.session_id)
        service = conn.host.service
        choice = picker_reads.session_choice(service, service.current_id(key))
    else:
        choice = await run_blocking(picker_reads.profile_choice, conn.scope, label="model route")
    return await picker_reads.provider_options(conn.scope, choice)


@methods.method("session.list")
async def list_sessions(conn: WsConnection, params: SessionListParams) -> SessionListResult:
    return await run_blocking(
        picker_reads.stored_sessions, conn.host.store, params, label="session list"
    )


# -- the TUI's other reads (W48-b) ----------------------------------------------------------------


def _attached_id(conn: WsConnection, session_id: str | None) -> str | None:
    """The live id of a session this connection attached (None when none is named)."""
    if session_id is None:
        return None
    return conn.host.service.current_id(conn.subs.require(session_id))


@methods.method("complete.path")
async def complete_path(conn: WsConnection, params: CompletePathParams) -> CompleteResult:
    sid = _attached_id(conn, params.session_id)
    folder = path_completion.session_folder(conn.host.service, sid)
    named = params if sid is None else params.model_copy(update={"session_id": sid})
    return await path_completion.complete_path(conn.scope, named, folder=folder)


@methods.method("commands.catalog")
async def commands_catalog(
    conn: WsConnection, params: CommandsCatalogParams
) -> CommandsCatalogResult:
    sid = _attached_id(conn, params.session_id)
    surface = _COMMAND_SURFACE.get(conn.subs.surface, "tui")
    return await run_blocking(
        lambda: picker_reads.command_catalog(
            conn.scope,
            surface=surface,
            skills=picker_reads.skill_table(conn.scope, sid),
            refused=picker_reads.DAEMON_REFUSED,
            quick_exec=False,
        ),
        label="command catalogue",
    )


# -- server-run slash commands (W48-h) ------------------------------------------------------------


def _slash_host(conn: WsConnection, session_id: str) -> DaemonSlashHost:
    key = conn.subs.require(session_id)
    surface = _COMMAND_SURFACE.get(conn.subs.surface, "tui")
    return DaemonSlashHost(conn.host, key, scope=conn.scope, surface=surface)


@methods.method("slash.exec")
async def slash_exec(conn: WsConnection, params: SlashExecParams) -> SlashExecResult:
    host = _slash_host(conn, params.session_id)
    named = params.model_copy(update={"session_id": host.service.current_id(host.key)})
    return await slash_commands.slash_exec(host, slash_commands.HANDLERS, named)


@methods.method("command.dispatch")
async def command_dispatch(
    conn: WsConnection, params: CommandDispatchParams
) -> CommandDispatchResult:
    host = _slash_host(conn, params.session_id)
    named = params.model_copy(update={"session_id": host.service.current_id(host.key)})
    return await slash_commands.dispatch(host, slash_commands.HANDLERS, named)


@methods.method("config.get")
async def config_get(conn: WsConnection, params: ConfigGetParams) -> ConfigGetResult:
    """Read-only: ``config.set`` stays with the in-process bridge (D-16.549)."""
    sid = _attached_id(conn, params.session_id)
    live: dict[str, Any] | None = None
    if sid is not None and client_settings.needs_session(params):
        # The daemon keeps no per-viewer tool-progress mode: the profile's applies.
        verbose = await run_blocking(
            client_settings.default_tool_mode, conn.scope, label="settings read"
        )
        live = client_settings.session_switches(conn.scope, conn.host.service, sid, verbose)
    return await run_blocking(
        client_settings.read_config, conn.scope, params, live, label="settings read"
    )


def served() -> list[str]:
    """Method names the daemon's WebSocket serves."""
    return methods.names()


def table() -> MethodTable[WsConnection]:
    """A fresh table with every handler of this module."""
    built = MethodTable["WsConnection"]()
    methods.register(built)
    return built
