"""The in-process host: a session service on its own event loop thread (P04-21, D-04.1 (e)).

A synchronous caller — the classic REPL — cannot await. ``InProcessHost`` starts a daemon thread
that runs an asyncio loop, builds the ``SessionService`` on it, and offers the service's methods
as plain blocking calls: each one is handed to the loop with ``run_coroutine_threadsafe`` and the
caller waits for the answer. A ``ProtocolError`` raised on the loop is raised again in the caller.
Because the turn runs on the loop thread and the caller only waits on futures, the caller can
submit a turn on one thread and interrupt it from another while it streams.

The host answers every server-request kind (approval, clarify, sudo, secret): its owner is a
person at a terminal (R05.3). Events and requests reach the caller through ``feed(session)``, a
thread-safe queue the loop fills.

Never call the blocking methods from the loop thread itself: that would wait on itself, so it is
refused with ``RuntimeError``.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from collections.abc import Callable, Coroutine, Mapping
from types import TracebackType
from typing import Any, Self

from indus.core.turn_state import TurnResult
from indus.cron.store import CronStore
from indus.foundation.scope import Scope, thread_with_scope, use_scope
from indus.protocol.base import ProtocolModel
from indus.protocol.events import EventBase
from indus.protocol.methods import (
    PromptSubmitResult,
    ServerRequestAnswerResult,
    SessionCreateParams,
    SessionCreateResult,
    SessionInterruptResult,
    SessionRedirectResult,
    SessionResumeResult,
    SessionSteerResult,
)
from indus.protocol.requests_port import AnswerModel
from indus.protocol.server_requests import PERSON_REQUEST_KINDS
from indus.service.cron_refs import register_cron_references
from indus.service.session_service import SessionService

__all__ = ["ALL_KINDS", "DEFAULT_CALL_TIMEOUT", "Feed", "InProcessHost"]

log = logging.getLogger(__name__)

ALL_KINDS: tuple[str, ...] = PERSON_REQUEST_KINDS
"""Every kind a person answers (the preview kinds are a desktop window's, P24-25)."""
"""Every server-request kind; the in-process host can answer all of them."""

DEFAULT_CALL_TIMEOUT = 60.0
"""How long a blocking call waits for the loop before ``TimeoutError`` (None: for ever)."""

type Feed = queue.Queue[EventBase | ProtocolModel]


class InProcessHost:
    """A ``SessionService`` on a dedicated loop thread, driven by blocking calls."""

    def __init__(
        self,
        make_service: Callable[[], SessionService],
        *,
        scope: Scope | None = None,
        name: str = "indus-host",
        call_timeout: float | None = DEFAULT_CALL_TIMEOUT,
    ) -> None:
        self._make_service = make_service
        self._scope = scope
        self._name = name
        self.call_timeout = call_timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._service: SessionService | None = None
        self._stopped = False
        self._undo_refs: Callable[[], None] | None = None

    # -- lifecycle --------------------------------------------------------------------------------

    def start(self) -> Self:
        """Start the loop thread and build the service on it."""
        if self._thread is not None:
            return self
        if self._scope is not None:
            # The curator never archives a skill a cron job of this profile names (P14-03).
            self._undo_refs = register_cron_references(CronStore(self._scope))
        self._thread = thread_with_scope(self._run, scope=self._scope, name=self._name, daemon=True)
        self._thread.start()
        if not self._ready.wait(10.0):
            raise RuntimeError("the host loop did not start")
        self._service = self.call(self._build())
        return self

    async def _build(self) -> SessionService:
        service = self._make_service()
        service.set_answerable_kinds(ALL_KINDS)
        return service

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        loop.call_soon(self._ready.set)
        try:
            loop.run_forever()
        finally:
            try:
                loop.run_until_complete(_wind_down())
            finally:
                loop.close()

    def stop(self, timeout: float = 10.0) -> None:
        """Close every session, stop the loop and join the thread (once)."""
        if self._stopped or self._thread is None:
            return
        self._stopped = True
        if self._undo_refs is not None:
            self._undo_refs()
            self._undo_refs = None
        loop = self._loop
        try:
            if self._service is not None:
                self.call(self._service.aclose(), timeout=timeout)
        except Exception:
            log.warning("closing the session service failed", exc_info=True)
        finally:
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(loop.stop)
            self._thread.join(timeout)

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    # -- marshalling ------------------------------------------------------------------------------

    @property
    def service(self) -> SessionService:
        if self._service is None:
            raise RuntimeError("the host is not started")
        return self._service

    @property
    def loop_thread(self) -> threading.Thread | None:
        return self._thread

    def call[T](self, coro: Coroutine[Any, Any, T], *, timeout: float | None = -1.0) -> T:
        """Run ``coro`` on the loop and wait for its result (re-raising its error here)."""
        loop = self._loop
        if loop is None or loop.is_closed() or self._thread is None:
            coro.close()
            raise RuntimeError("the host is not running")
        if threading.current_thread() is self._thread:
            coro.close()
            raise RuntimeError("a blocking host call from the host's own loop thread would hang")
        future = asyncio.run_coroutine_threadsafe(self._scoped(coro), loop)
        wait = self.call_timeout if timeout == -1.0 else timeout
        try:
            return future.result(wait)
        except TimeoutError:
            future.cancel()
            raise

    async def _scoped[T](self, coro: Coroutine[Any, Any, T]) -> T:
        # The task copies the calling thread's context; the host's own scope must win (R01.16).
        if self._scope is None:
            return await coro
        with use_scope(self._scope):
            return await coro

    def run[T](self, fn: Callable[[], T], *, timeout: float | None = -1.0) -> T:
        """Run the plain callable ``fn`` on the loop thread and wait for its result."""

        async def step() -> T:
            return fn()

        return self.call(step(), timeout=timeout)

    # -- the service, blocking --------------------------------------------------------------------

    def create(self, params: SessionCreateParams | None = None) -> SessionCreateResult:
        return self.call(self.service.create(params))

    def resume(self, session_id: str) -> SessionResumeResult:
        return self.call(self.service.resume(session_id))

    def submit(self, session_id: str, text: str) -> PromptSubmitResult:
        return self.call(self.service.submit(session_id, text))

    def wait(
        self, session_id: str, turn_id: str | None = None, *, timeout: float | None = -1.0
    ) -> TurnResult:
        return self.call(self.service.wait(session_id, turn_id), timeout=timeout)

    def interrupt(
        self, session_id: str, generation: int | None = None, *, hard: bool = False
    ) -> SessionInterruptResult:
        return self.run(lambda: self.service.interrupt(session_id, generation, hard=hard))

    def steer(self, session_id: str, text: str) -> SessionSteerResult:
        return self.run(lambda: self.service.steer(session_id, text))

    def redirect(self, session_id: str, text: str) -> SessionRedirectResult:
        return self.run(lambda: self.service.redirect(session_id, text))

    def answer_server_request(
        self, session_id: str, request_id: str, answer: Mapping[str, Any] | AnswerModel
    ) -> ServerRequestAnswerResult:
        return self.run(lambda: self.service.answer_server_request(session_id, request_id, answer))

    def events(self, session_id: str, since: int | None = None) -> list[EventBase]:
        return self.run(lambda: self.service.events(session_id, since))

    def close_session(self, session_id: str) -> None:
        self.call(self.service.close(session_id))

    def feed(self, session_id: str) -> tuple[Feed, Callable[[], None]]:
        """A queue that receives the session's events and server requests from now on, and the
        callable that stops it."""
        feed: Feed = queue.Queue()

        def attach() -> Callable[[], None]:
            off_events = self.service.subscribe(session_id, feed.put)
            off_requests = self.service.on_request(session_id, feed.put)

            def detach() -> None:
                off_events()
                off_requests()

            return detach

        detach = self.run(attach)
        return feed, lambda: self.run(detach)


async def _wind_down() -> None:
    """Last step on the loop thread: stop whatever still runs, then the async generators."""
    me = asyncio.current_task()
    stragglers = {task for task in asyncio.all_tasks() if task is not me}
    for straggler in stragglers:
        straggler.cancel()
    if stragglers:
        await asyncio.wait(stragglers)
    await asyncio.get_running_loop().shutdown_asyncgens()
