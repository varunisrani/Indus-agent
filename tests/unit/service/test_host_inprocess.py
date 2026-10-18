"""InProcessHost: a synchronous caller drives the session service on its loop thread (P04-21)."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from indus.core.exit_reasons import ExitReason
from indus.foundation.scope import Scope, current_scope
from indus.protocol.errors import SESSION_BUSY
from indus.protocol.events import MessageDelta, TurnDone
from indus.protocol.methods import ProtocolError
from indus.protocol.server_requests import ApprovalRequest
from indus.service.host_inprocess import ALL_KINDS, Feed, InProcessHost
from indus.service.session_service import SessionService
from tests.fakes.agent import fake_options
from tests.fakes.clock import FakeClock
from tests.fakes.model import (
    FakeModelServer,
    Finish,
    Stall,
    Text,
    Turn,
    fake_route,
    script_of,
    text_turn,
    tool_batch,
)
from tests.fakes.turn import check_alternation
from tests.unit.service.test_session_service import confirm

WAIT = 10.0


def host_for(
    scope: Scope,
    clock: FakeClock,
    tmp_path: Path,
    *turns: Turn,
    inline: Mapping[str, Any] | None = None,
) -> InProcessHost:
    server = FakeModelServer(script_of(*turns), clock=clock)

    def make() -> SessionService:
        return SessionService(
            scope,
            fake_route(server),
            toolsets=[],
            options=fake_options(server, session_id=None, inline=dict(inline or {})),
            lock_dir=tmp_path / "locks",
        )

    return InProcessHost(make, scope=scope, call_timeout=WAIT)


def take(feed: Feed, kind: type[object]) -> object:
    while True:
        item = feed.get(timeout=WAIT)
        if isinstance(item, kind):
            return item


def test_the_host_runs_the_service_on_its_own_thread(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    host = host_for(scope, fake_clock, tmp_path)
    with host:
        thread = host.loop_thread
        assert thread is not None and thread.is_alive()
        assert thread is not threading.current_thread()
        assert host.service.answerable_kinds == ALL_KINDS
        assert set(ALL_KINDS) == {"approval", "clarify", "sudo", "secret"}
        assert host.run(lambda: threading.current_thread()) is thread
        assert host.run(lambda: current_scope().profile_id) == scope.profile_id
    assert not thread.is_alive()
    host.stop()  # a second stop is harmless


def test_a_sync_caller_submits_and_reads_events(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    with host_for(scope, fake_clock, tmp_path, text_turn("hello ", "there")) as host:
        sid = host.create().session_id
        feed, stop_feed = host.feed(sid)
        ticket = host.submit(sid, "hi")
        result = host.wait(sid)
        assert result.final_text == "hello there" and result.turn_id == ticket.turn_id
        delta = take(feed, MessageDelta)
        assert isinstance(delta, MessageDelta) and delta.turn_id == ticket.turn_id
        take(feed, TurnDone)
        stop_feed()
        assert [type(e) for e in host.events(sid)][-1] is TurnDone


def test_a_sync_caller_interrupts_mid_stream_from_another_thread(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    stalled = Turn((Text(("partial",)), Stall(1000.0), Finish("stop")))
    with host_for(scope, fake_clock, tmp_path, stalled, text_turn("after")) as host:
        sid = host.create().session_id
        feed, _ = host.feed(sid)
        ticket = host.submit(sid, "go")
        take(feed, MessageDelta)
        with pytest.raises(ProtocolError) as busy:
            host.submit(sid, "again")
        assert busy.value.error is SESSION_BUSY
        answers: list[bool] = []
        worker = threading.Thread(
            target=lambda: answers.append(host.interrupt(sid, ticket.generation).interrupted)
        )
        worker.start()
        worker.join(WAIT)
        assert answers == [True]
        result = host.wait(sid)
        assert result.exit_reason is ExitReason.INTERRUPTED_BY_USER
        check_alternation(result.rows)
        host.submit(sid, "next")
        assert host.wait(sid).final_text == "after"


def test_a_sync_caller_answers_an_approval(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    turns = (tool_batch(("confirm", {})), text_turn("done"))
    with host_for(scope, fake_clock, tmp_path, *turns, inline={"confirm": confirm}) as host:
        sid = host.create().session_id
        feed, _ = host.feed(sid)
        host.submit(sid, "clean")
        request = take(feed, ApprovalRequest)
        assert isinstance(request, ApprovalRequest)
        assert host.answer_server_request(sid, request.request_id, {"choice": "session"}).accepted
        result = host.wait(sid)
        assert result.final_text == "done"
        assert "approved: session" in str([r.content for r in result.rows if r.role == "tool"])


def test_blocking_calls_from_the_loop_thread_are_refused(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    with host_for(scope, fake_clock, tmp_path) as host:
        sid = host.create().session_id
        inner: Callable[[], object] = lambda: host.events(sid)  # noqa: E731
        with pytest.raises(RuntimeError, match="own loop thread"):
            host.run(inner)


def test_calls_after_stop_are_refused(scope: Scope, fake_clock: FakeClock, tmp_path: Path) -> None:
    host = host_for(scope, fake_clock, tmp_path).start()
    host.stop()
    with pytest.raises(RuntimeError, match="not running"):
        host.create()
