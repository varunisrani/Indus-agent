"""The server-request broker: questions for a person, parked on futures until answered.

One broker per session. ``ask`` turns a question (approval, clarify, sudo, secret) into a server
request with a fresh id, hands it to ``deliver`` (the client transport) and waits on a future keyed
by that id. ``answer`` checks a client's reply against the request it names and resolves the
future. ``cancel_all`` withdraws every open request when a turn is interrupted or ends, sending one
``server_request.cancelled`` event per request through ``emit``.

Rules that keep the agent from stalling (R05.3):

- a kind the client never declared it can answer is refused at once with the protocol's
  ``SERVER_REQUEST_UNSUPPORTED`` error (``RequestUnsupported``); nothing is sent;
- a timeout withdraws the request (``timed_out``) instead of waiting forever;
- the request table is the single gate: whoever removes an entry (answer, timeout, withdrawal,
  the asker being cancelled) decides its outcome, so each request gets at most one outcome and at
  most one cancelled event.

The broker also measures how long the agent sat waiting on a person: ``human_wait_seconds`` counts
the time during which at least one request was open (overlaps once). The tool executor subtracts
it from batch deadlines (P04-14).

Daemon-hosted sessions (P17-07) hand the broker a ``RequestBook``: then the kinds the attached
viewers can render decide what may be asked, the book (not ``deliver``) offers each request to the
viewers and re-offers it on reattach, every answer and withdrawal is reported to the book (which
announces ``request.settled``), and a request the book settles as undeliverable fails its asker
with ``RequestUnsupported`` at once. Request ids are ``srq-<12 hex>``.

Every method runs on the session's event loop; a host on another thread marshals calls onto it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from pydantic import TypeAdapter, ValidationError

from indus.foundation.clock import Clock
from indus.protocol.envelope import ErrorObject
from indus.protocol.errors import (
    ANSWER_KIND_MISMATCH,
    INVALID_PARAMS,
    SERVER_REQUEST_NOT_FOUND,
    SERVER_REQUEST_UNSUPPORTED,
    make_error,
)
from indus.protocol.events import CancelReason, ServerRequestCancelled
from indus.protocol.methods import ProtocolError, ServerRequestAnswerResult
from indus.protocol.requests_port import (
    AnswerModel,
    RequestNotAnswered,
    RequestUnsupported,
    RequestWithdrawn,
    ServerRequestPort,
)
from indus.protocol.server_requests import (
    SERVER_REQUEST_KINDS,
    ApprovalAnswer,
    ApprovalRequest,
    ClarifyAnswer,
    ClarifyRequest,
    PreviewActRequest,
    PreviewReadRequest,
    SecretRequest,
    ServerRequestAnswer,
    SudoRequest,
)
from indus.service.request_redelivery import RequestBook, RequestSettlement, new_request_id

__all__ = [
    "AnswerModel",
    "RequestModel",
    "RequestNotAnswered",
    "RequestUnsupported",
    "RequestWithdrawn",
    "ServerRequestBroker",
    "ServerRequestPort",
]

log = logging.getLogger(__name__)

type RequestModel = (
    ApprovalRequest
    | ClarifyRequest
    | SudoRequest
    | SecretRequest
    | PreviewActRequest
    | PreviewReadRequest
)
"""Any concrete server request."""

_RESERVED_KEYS = frozenset({"kind", "request_id", "session_id", "turn_id", "deadline_s"})
_ANSWERS: TypeAdapter[AnswerModel] = TypeAdapter(ServerRequestAnswer)


@dataclass(slots=True)
class _Open:
    request: RequestModel
    future: asyncio.Future[AnswerModel]


class ServerRequestBroker:
    """Server requests of one session; see the module docstring."""

    def __init__(
        self,
        *,
        session_id: str,
        deliver: Callable[[RequestModel], object],
        emit: Callable[[ServerRequestCancelled], object],
        next_seq: Callable[[], int],
        clock: Clock,
        answerable_kinds: Iterable[str] = (),
        new_id: Callable[[], str] = new_request_id,
        book: RequestBook | None = None,
    ) -> None:
        self.session_id = session_id
        self._deliver = deliver
        self._emit = emit
        self._next_seq = next_seq
        self._clock = clock
        self._new_id = new_id
        self._answerable: frozenset[str] = frozenset()
        self.set_answerable_kinds(answerable_kinds)
        self.asks_nobody = False
        """While true no kind is answerable (``RequestUnsupported`` at once): a one-shot turn of
        the daemon, which asks nobody as the in-process one-shot run does."""
        self._open: dict[str, _Open] = {}
        self._closed = False
        self._wait_total = 0.0
        self._wait_since: float | None = None
        self._book = book
        if book is not None:
            book.bind_undeliverable(self._undeliverable)

    # -- configuration -------------------------------------------------------------------------

    @property
    def answerable_kinds(self) -> frozenset[str]:
        """Kinds that may be asked now: the attached viewers' kinds when a book is used, none
        while ``asks_nobody`` is set."""
        if self.asks_nobody:
            return frozenset()
        if self._book is not None:
            return self._book.answerable_kinds()
        return self._answerable

    def set_answerable_kinds(self, kinds: Iterable[str]) -> None:
        """The kinds the attached client said it can answer (unknown names are ignored)."""
        self._answerable = frozenset(k for k in kinds if k in SERVER_REQUEST_KINDS)

    # -- asking --------------------------------------------------------------------------------

    async def ask(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        timeout: float | None = None,
        turn_id: str | None = None,
    ) -> AnswerModel:
        """Ask the person and wait for the answer model of ``kind``.

        Raises ``RequestUnsupported`` at once when the client cannot answer ``kind``, and
        ``RequestWithdrawn`` when the request times out, is withdrawn by ``cancel_all`` or the
        broker is closed. A bad ``kind`` or ``payload`` is a programming error (``ValueError`` or
        pydantic's ``ValidationError``).
        """
        request = self._build(kind, payload, timeout=timeout, turn_id=turn_id)
        if self._closed:
            raise RequestWithdrawn(request.request_id, CancelReason.SESSION_CLOSED)
        if kind not in self.answerable_kinds:
            raise RequestUnsupported(
                kind,
                make_error(
                    SERVER_REQUEST_UNSUPPORTED,
                    f"the attached client cannot answer {kind} requests",
                ),
            )
        future: asyncio.Future[AnswerModel] = asyncio.get_running_loop().create_future()
        self._register(_Open(request, future))
        try:
            if self._book is not None:
                self._book.offer(request)
            else:
                self._deliver(request)
        except Exception:
            # The request stays open: a client that reconnects gets it with the session.
            log.warning("could not deliver server request %s", request.request_id, exc_info=True)
        try:
            if timeout is None:
                return await future
            return await self._wait(request.request_id, future, timeout)
        except asyncio.CancelledError:
            future.cancel()  # nobody is left to read an outcome
            self._withdraw(request.request_id, CancelReason.INTERRUPTED)
            raise

    async def _wait(
        self, request_id: str, future: asyncio.Future[AnswerModel], timeout: float
    ) -> AnswerModel:
        timer = asyncio.ensure_future(self._clock.sleep(timeout))
        try:
            await asyncio.wait({future, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            timer.cancel()
        if not future.done():
            self._withdraw(request_id, CancelReason.TIMED_OUT)
        return future.result()

    def _build(
        self, kind: str, payload: Mapping[str, Any], *, timeout: float | None, turn_id: str | None
    ) -> RequestModel:
        models = SERVER_REQUEST_KINDS.get(kind)
        if models is None:
            raise ValueError(f"unknown server request kind: {kind!r}")
        clash = _RESERVED_KEYS.intersection(payload)
        if clash:
            raise ValueError(f"payload must not set {sorted(clash)}; the broker fills them")
        fields: dict[str, Any] = {
            **payload,
            "request_id": self._new_id(),
            "session_id": self.session_id,
            "turn_id": turn_id,
            "deadline_s": timeout,
        }
        return cast(RequestModel, models[0].model_validate(fields))

    # -- answering -----------------------------------------------------------------------------

    def answer(
        self, request_id: str, answer: Mapping[str, Any] | AnswerModel
    ) -> ServerRequestAnswerResult:
        """Resolve the open request ``request_id`` with ``answer``.

        Raises ``ProtocolError``: ``SERVER_REQUEST_NOT_FOUND`` (never sent, already answered or
        withdrawn), ``INVALID_PARAMS`` (malformed, or a value the request did not offer) or
        ``ANSWER_KIND_MISMATCH``. ``accepted`` is False only when the asker gave up in the same
        loop iteration.
        """
        entry = self._open.get(request_id)
        if entry is None:
            raise ProtocolError(
                SERVER_REQUEST_NOT_FOUND, f"no open server request {request_id}", path="request_id"
            )
        parsed = _check_answer(entry.request, answer)
        del self._open[request_id]
        self._after_close()
        if self._book is not None:
            self._book.settle(request_id, RequestSettlement.ANSWERED)
        if entry.future.done():
            return ServerRequestAnswerResult(accepted=False)
        entry.future.set_result(parsed)
        return ServerRequestAnswerResult(accepted=True)

    # -- withdrawing ---------------------------------------------------------------------------

    def cancel_all(self, reason: CancelReason) -> int:
        """Withdraw every open request, oldest first; one cancelled event each."""
        return sum(1 for request_id in list(self._open) if self._withdraw(request_id, reason))

    def close(self) -> int:
        """Withdraw everything (``session_closed``) and refuse later asks."""
        self._closed = True
        return self.cancel_all(CancelReason.SESSION_CLOSED)

    def _withdraw(self, request_id: str, reason: CancelReason) -> bool:
        entry = self._open.pop(request_id, None)
        if entry is None:
            return False
        self._after_close()
        if self._book is not None:
            self._book.settle(request_id, RequestSettlement.WITHDRAWN, reason.value)
        try:
            event = ServerRequestCancelled(
                session_id=self.session_id,
                seq=self._next_seq(),
                request_id=request_id,
                reason=reason,
            )
            self._emit(event)
        except Exception:
            log.warning("could not announce withdrawal of %s", request_id, exc_info=True)
        if not entry.future.done():
            entry.future.set_exception(RequestWithdrawn(request_id, reason))
        return True

    def _undeliverable(self, request_id: str, error: ErrorObject) -> None:
        """The book found no viewer able to answer: fail the asker now (no cancelled event)."""
        entry = self._open.pop(request_id, None)
        if entry is None:
            return
        self._after_close()
        if not entry.future.done():
            entry.future.set_exception(RequestUnsupported(entry.request.kind, error))

    # -- state ---------------------------------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def waiting(self) -> bool:
        """True while at least one request waits on a person."""
        return bool(self._open)

    def pending_requests(self) -> list[RequestModel]:
        """The open requests, oldest first (re-delivered on ``session.resume``)."""
        return [entry.request for entry in self._open.values()]

    def human_wait_seconds(self) -> float:
        """Time so far with at least one request open, including the current stretch."""
        if self._wait_since is None:
            return self._wait_total
        return self._wait_total + max(0.0, self._clock.monotonic() - self._wait_since)

    def _register(self, entry: _Open) -> None:
        if not self._open:
            self._wait_since = self._clock.monotonic()
        self._open[entry.request.request_id] = entry

    def _after_close(self) -> None:
        if self._open or self._wait_since is None:
            return
        self._wait_total += max(0.0, self._clock.monotonic() - self._wait_since)
        self._wait_since = None


def _invalid(message: str, path: str) -> ProtocolError:
    return ProtocolError(INVALID_PARAMS, message, path=path)


def _check_answer(request: RequestModel, answer: Mapping[str, Any] | AnswerModel) -> AnswerModel:
    """Validate ``answer`` against ``request``; clarify gaps are filled as skipped."""
    if isinstance(answer, Mapping):
        data = dict(answer)
        data.setdefault("kind", request.kind)
        try:
            parsed = _ANSWERS.validate_python(data)
        except ValidationError as exc:
            first = exc.errors()[0]
            parts = list(first["loc"])
            if parts and parts[0] == data["kind"]:
                parts = parts[1:]  # drop the union's tag step
            loc = ".".join(str(part) for part in parts)
            path = f"answer.{loc}" if loc else "answer"
            raise _invalid(f"{path}: {first['msg']}", path) from exc
    else:
        parsed = answer
    if parsed.kind != request.kind:
        raise ProtocolError(
            ANSWER_KIND_MISMATCH,
            f"request {request.request_id} is {request.kind}, the answer is {parsed.kind}",
            path="answer.kind",
        )
    if isinstance(request, ApprovalRequest):
        assert isinstance(parsed, ApprovalAnswer)
        if parsed.choice not in request.choices:
            raise _invalid(f"choice {parsed.choice.value} was not offered", "answer.choice")
    elif isinstance(request, ClarifyRequest):
        assert isinstance(parsed, ClarifyAnswer)
        return _check_clarify(request, parsed)
    return parsed


def _check_clarify(request: ClarifyRequest, answer: ClarifyAnswer) -> ClarifyAnswer:
    questions = {q.id: q for q in request.questions}
    for key, values in answer.answers.items():
        question = questions.get(key)
        if question is None:
            raise _invalid(f"no question {key!r} in this request", f"answer.answers.{key}")
        if not question.multi_select and len(values) > 1:
            raise _invalid(f"question {key!r} takes one answer", f"answer.answers.{key}")
    filled = {q.id: list(answer.answers.get(q.id, [])) for q in request.questions}
    return ClarifyAnswer(answers=filled)
