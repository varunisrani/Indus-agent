"""D-04.1 criterion (e): a synchronous caller drives and interrupts a turn (P04-21, P04-23).

The caller lives on the test's own thread and never touches the event loop: it submits a turn
through ``InProcessHost``, waits for the first streamed text, and interrupts the turn while the
fake model keeps the stream open (the stall runs on a fake clock nobody advances, so only the
interrupt can end it). Checked on the real P04 loop:

- the model stream is closed promptly after the interrupt (wall clock, generous bound);
- the transcript alternates and the partial reply is kept;
- the loop thread keeps answering blocking calls from the caller while the turn streams and
  right after the interrupt;
- the session takes the next turn normally.
"""

from __future__ import annotations

import statistics
import threading
import time
from pathlib import Path

from indus.core.exit_reasons import ExitReason
from indus.foundation.scope import Scope
from indus.protocol.events import MessageDelta, TurnDone
from indus.service.host_inprocess import Feed, InProcessHost
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
)
from tests.fakes.turn import check_alternation

WAIT = 10.0
CLOSE_BOUND_S = 0.5  # criterion (a) asks 200 ms in-loop; this adds a thread hop on a shared box
PING_BOUND_S = 0.25


def take(feed: Feed, kind: type[object]) -> object:
    while True:
        item = feed.get(timeout=WAIT)
        if isinstance(item, kind):
            return item


def ping_times(host: InProcessHost, count: int) -> list[float]:
    times: list[float] = []
    for _ in range(count):
        start = time.perf_counter()
        host.run(lambda: None)
        times.append(time.perf_counter() - start)
    return times


def test_sync_caller_drives_and_interrupts_a_streaming_turn(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    stalled = Turn((Text(("Here is the start",)), Stall(10_000.0), Finish("stop")))
    server = FakeModelServer(script_of(stalled, text_turn("second turn fine")), clock=fake_clock)

    def make() -> SessionService:
        return SessionService(
            scope,
            fake_route(server),
            toolsets=[],
            options=fake_options(server, session_id=None),
            lock_dir=tmp_path / "locks",
        )

    caller = threading.current_thread()
    with InProcessHost(make, scope=scope, call_timeout=WAIT) as host:
        assert host.loop_thread is not caller
        sid = host.create().session_id
        feed, _ = host.feed(sid)
        ticket = host.submit(sid, "explain it")
        take(feed, MessageDelta)

        during = ping_times(host, 50)  # the turn is streaming (stalled) right now
        stamp = time.perf_counter()
        assert host.interrupt(sid, ticket.generation).interrupted
        result = host.wait(sid)
        ended = time.perf_counter()
        after = ping_times(host, 20)

        record = server.streams[0]
        assert record.closed_at is not None and not record.completed
        close_latency = record.closed_at - stamp
        assert close_latency < CLOSE_BOUND_S, close_latency
        assert ended - stamp < CLOSE_BOUND_S * 2
        assert max(during) < PING_BOUND_S, max(during)
        assert max(after) < PING_BOUND_S, max(after)

        assert result.exit_reason is ExitReason.INTERRUPTED_BY_USER
        assert result.final_text == "Here is the start"
        check_alternation(result.rows)
        assert [r.role for r in result.rows] == ["user", "assistant"]
        take(feed, TurnDone)

        host.submit(sid, "go on")
        follow = host.wait(sid)
        assert follow.final_text == "second turn fine"
        check_alternation(follow.rows, user_index=follow.current_turn_user_index)
        assert fake_clock.monotonic() < 10_000.0  # nothing waited the stall out

    print(  # numbers for the D-04.1 decision note
        f"\n[d04-1 e] stream closed {close_latency * 1000:.1f} ms after the interrupt; "
        f"ping during stream median {statistics.median(during) * 1000:.2f} ms "
        f"max {max(during) * 1000:.2f} ms; after max {max(after) * 1000:.2f} ms"
    )
