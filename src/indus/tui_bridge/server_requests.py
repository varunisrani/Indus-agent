"""Server requests over the bridge (P12-30): approval, clarify, sudo and secret cards.

Each agent built by the bridge asks through a ``BridgePort`` wrapped around its session's broker
(``indus.service.broker``). The port gives each kind the deadline a terminal card needs — sudo
``SUDO_DEADLINE_S`` and secret ``SECRET_DEADLINE_S`` always; clarify the asker's value, else
``agent.clarify_timeout`` (default 300 s, ``<= 0`` means no limit); approval the asker's value
(the approval pipeline reads ``approvals.timeout``), else ``DEFAULT_DEADLINE_S`` — and keeps
clarify answers that were locked before a timeout.

The broker sends a request through the session's event sink; the projector writes it as a
``server_request`` notification, and the hub notes clarify requests so ``clarify.lock`` can lock
their questions one by one (the last lock answers the request). A client that never declared
``server_requests`` (or ``server_requests.<kind>``) with ``client.capabilities`` gets no request:
the broker refuses the ask at once with ``RequestUnsupported`` (R05.3). Withdrawals (timeout,
interrupt, turn end, close) are announced once each by the broker as ``server_request.cancelled``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from indus.core.agent import Agent, AgentOptions
from indus.core.events import EventSink
from indus.core.persist import TranscriptStore
from indus.foundation.scope import Scope
from indus.protocol.bridge import (
    ClarifyLockParams,
    ClarifyLockResult,
    ServerRequestDeclineParams,
    ServerRequestDeclineResult,
)
from indus.protocol.errors import INVALID_PARAMS, SERVER_REQUEST_NOT_FOUND
from indus.protocol.events import CancelReason
from indus.protocol.methods import (
    SERVER_REQUESTS_FEATURE,
    ClientCapabilitiesParams,
    ClientCapabilitiesResult,
    ProtocolError,
    ServerRequestAnswerParams,
    ServerRequestAnswerResult,
    negotiate_features,
)
from indus.protocol.requests_port import AnswerModel, RequestWithdrawn
from indus.protocol.server_requests import (
    PERSON_REQUEST_KINDS,
    SERVER_REQUEST_KINDS,
    ApprovalAnswer,
    ApprovalChoice,
    ClarifyAnswer,
    ClarifyRequest,
    SecretAnswer,
    SudoAnswer,
)
from indus.providers.route import RuntimeRoute
from indus.service.broker import RequestModel, ServerRequestBroker
from indus.service.session_service import AgentFactory, SessionService

if TYPE_CHECKING:
    from indus.tui_bridge.bridge import Bridge
    from indus.tui_bridge.dispatch import MethodTable

__all__ = [
    "DEFAULT_DEADLINE_S",
    "SECRET_DEADLINE_S",
    "SUDO_DEADLINE_S",
    "BridgePort",
    "RequestHub",
    "kinds_for",
    "refusal_for",
    "register",
]

log = logging.getLogger(__name__)

SUDO_DEADLINE_S = 120.0
SECRET_DEADLINE_S = 300.0
DEFAULT_DEADLINE_S = 300.0
CLARIFY_KEY = "agent.clarify_timeout"
_FIXED = {"sudo": SUDO_DEADLINE_S, "secret": SECRET_DEADLINE_S}


def kinds_for(features: Sequence[str]) -> list[str]:
    """The request kinds a client's accepted features cover."""
    if SERVER_REQUESTS_FEATURE in features:
        return list(PERSON_REQUEST_KINDS)  # the preview kinds are a desktop window's
    prefix = SERVER_REQUESTS_FEATURE + "."
    return [kind for kind in SERVER_REQUEST_KINDS if prefix + kind in features]


def clarify_deadline(scope: Scope) -> float | None:
    """``agent.clarify_timeout`` seconds; ``<= 0`` means no limit; unreadable → the default."""
    raw: Any = None
    try:
        raw = scope.config.get(CLARIFY_KEY) if scope.config is not None else None
    except Exception:
        log.debug("clarify timeout unreadable", exc_info=True)
    if raw is None:
        return DEFAULT_DEADLINE_S
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_DEADLINE_S
    return value if value > 0 else None


class _Locks:
    """Locked answers of one open clarify request."""

    def __init__(self, request: ClarifyRequest) -> None:
        self.request = request
        self.locked: dict[str, list[str]] = {}

    def remaining(self) -> list[str]:
        return [q.id for q in self.request.questions if q.id not in self.locked]

    def answer(self) -> ClarifyAnswer:
        return ClarifyAnswer(
            answers={q.id: list(self.locked.get(q.id, [])) for q in self.request.questions}
        )


class RequestHub:
    """The bridge's side of server requests (see the module notes)."""

    def __init__(self, bridge: Bridge) -> None:
        self.bridge = bridge
        self.kinds: list[str] = []
        self.clarify: dict[str, _Locks] = {}

    # -- capabilities -----------------------------------------------------------------------------

    def set_features(self, features: Sequence[str]) -> list[str]:
        """Accept ``features``; returns the accepted ones and applies the kinds they cover."""
        accepted = negotiate_features(list(features))
        self.kinds = kinds_for(accepted)
        service = self.bridge.built
        if service is not None:
            service.set_answerable_kinds(self.kinds)
        return accepted

    def service_ready(self, service: SessionService) -> None:
        service.set_answerable_kinds(self.kinds)

    # -- deadlines ---------------------------------------------------------------------------------

    def deadline_for(self, kind: str, asked: float | None) -> float | None:
        fixed = _FIXED.get(kind)
        if fixed is not None:
            return fixed
        if asked is not None:
            return asked
        if kind == "clarify":
            return clarify_deadline(self.bridge.scope)
        return DEFAULT_DEADLINE_S

    # -- delivery and clarify locks ----------------------------------------------------------------

    def delivered(self, request: object) -> None:
        if isinstance(request, ClarifyRequest):
            self.clarify[request.request_id] = _Locks(request)

    def drop(self, request_id: str) -> _Locks | None:
        return self.clarify.pop(request_id, None)

    def lock(self, service: SessionService, params: ClarifyLockParams) -> list[str]:
        locks = self.clarify.get(params.request_id)
        if locks is None:
            raise ProtocolError(
                SERVER_REQUEST_NOT_FOUND,
                f"no open clarify request {params.request_id}",
                path="request_id",
            )
        question = next((q for q in locks.request.questions if q.id == params.question_id), None)
        if question is None:
            raise ProtocolError(
                INVALID_PARAMS, f"no question {params.question_id!r}", path="question_id"
            )
        if not question.multi_select and len(params.answer) > 1:
            raise ProtocolError(
                INVALID_PARAMS, f"question {params.question_id!r} takes one answer", path="answer"
            )
        locks.locked[params.question_id] = list(params.answer)
        remaining = locks.remaining()
        if not remaining:
            self.drop(params.request_id)
            service.answer_server_request(params.session_id, params.request_id, locks.answer())
        return remaining

    def merge_locked(self, request_id: str, answer: Mapping[str, Any]) -> Mapping[str, Any]:
        """A final clarify answer keeps the answers locked earlier for questions it leaves out."""
        locks = self.drop(request_id)
        if locks is None or answer.get("kind", "clarify") != "clarify":
            return answer
        given = answer.get("answers")
        merged = dict(locks.locked)
        if isinstance(given, Mapping):
            merged.update({str(k): v for k, v in dict(given).items()})
        return {**answer, "answers": merged}

    # -- agents ------------------------------------------------------------------------------------

    def agent_factory(
        self,
        route: RuntimeRoute,
        store: TranscriptStore,
        *,
        seams: Callable[[Agent], None] | None = None,
    ) -> AgentFactory:
        """An agent factory for ``SessionService`` that puts a ``BridgePort`` between each agent
        and its broker (``seams`` adds host wiring to the opened agent)."""

        async def build(
            session_id: str,
            sink: EventSink,
            broker: ServerRequestBroker,
            options: AgentOptions,
            selection: Sequence[str] | None,
        ) -> Agent:
            chosen = route
            model = options.session_fields.get("model")
            if isinstance(model, str) and model and model != route.model:
                chosen = replace(route, model=model)
            port = BridgePort(broker, self)
            full = replace(options, session_id=session_id, store=store, broker=port)
            agent = await Agent.start(self.bridge.scope, chosen, selection, full, sink)
            if seams is not None:
                seams(agent)
            return agent

        return build


class BridgePort:
    """``ServerRequestPort`` of one agent: deadlines per kind, locked clarify answers kept."""

    def __init__(self, broker: ServerRequestBroker, hub: RequestHub) -> None:
        self.broker = broker
        self.hub = hub

    async def ask(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        timeout: float | None = None,
        turn_id: str | None = None,
    ) -> AnswerModel:
        deadline = self.hub.deadline_for(kind, timeout)
        try:
            return await self.broker.ask(kind, payload, timeout=deadline, turn_id=turn_id)
        except RequestWithdrawn as exc:
            locks = self.hub.drop(exc.request_id)
            if (
                kind == "clarify"
                and exc.reason is CancelReason.TIMED_OUT
                and locks
                and locks.locked
            ):
                log.info(
                    "clarify %s timed out; keeping %d locked answer(s)",
                    exc.request_id,
                    len(locks.locked),
                )
                return locks.answer()
            raise

    def cancel_all(self, reason: CancelReason) -> int:
        return self.broker.cancel_all(reason)

    def human_wait_seconds(self) -> float:
        return self.broker.human_wait_seconds()

    def pending_requests(self) -> list[RequestModel]:
        return self.broker.pending_requests()


# -- handlers -------------------------------------------------------------------------------------


async def capabilities(
    bridge: Bridge, params: ClientCapabilitiesParams
) -> ClientCapabilitiesResult:
    return ClientCapabilitiesResult(features=bridge.requests.set_features(params.features))


async def answer(bridge: Bridge, params: ServerRequestAnswerParams) -> ServerRequestAnswerResult:
    service = await bridge.service()
    given: Mapping[str, Any] = params.answer.model_dump(mode="json")
    if params.answer.kind == "clarify":
        given = bridge.requests.merge_locked(params.request_id, given)
    return service.answer_server_request(params.session_id, params.request_id, given)


async def clarify_lock(bridge: Bridge, params: ClarifyLockParams) -> ClarifyLockResult:
    service = await bridge.service()
    return ClarifyLockResult(remaining=bridge.requests.lock(service, params))


def refusal_for(kind: str, reason: str | None) -> AnswerModel:
    """The answer that refuses a request of ``kind``."""
    if kind == "approval":
        return ApprovalAnswer(
            choice=ApprovalChoice.DENY, reason=reason or "the client cannot show this request"
        )
    if kind == "clarify":
        return ClarifyAnswer(answers={})
    if kind == "sudo":
        return SudoAnswer()
    return SecretAnswer()


async def decline(bridge: Bridge, params: ServerRequestDeclineParams) -> ServerRequestDeclineResult:
    service = await bridge.service()
    port = service.agent(params.session_id).broker
    pending = port.pending_requests() if isinstance(port, BridgePort) else []
    request = next((r for r in pending if r.request_id == params.request_id), None)
    if request is None:
        raise ProtocolError(
            SERVER_REQUEST_NOT_FOUND,
            f"no open server request {params.request_id}",
            path="request_id",
        )
    bridge.requests.drop(params.request_id)
    answered = service.answer_server_request(
        params.session_id, params.request_id, refusal_for(request.kind, params.reason)
    )
    return ServerRequestDeclineResult(accepted=answered.accepted)


def register(table: MethodTable) -> None:
    table.add("client.capabilities", capabilities)
    table.add("server_request.decline", decline)
    table.add("server_request.answer", answer)
    table.add("clarify.lock", clarify_lock)
