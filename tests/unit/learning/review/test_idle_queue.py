"""P09-15 (F9): deferring reviews bound for a local model server."""

from __future__ import annotations

import asyncio

from indus.foundation.scope import Scope
from indus.learning.review.counters import ReviewRequest
from indus.learning.review.fork import ReviewRunner
from indus.learning.review.idle_queue import Deferred, IdleQueue
from indus.learning.review.prompts import NOTHING
from indus.providers.route import RuntimeRoute
from tests.fakes.agent import run_driven
from tests.fakes.clock import FakeClock, checkpoint
from tests.unit.learning._helpers_a import with_config
from tests.unit.learning.review.helpers import ScriptedCaller, make_parent, text


def _queue(clock: FakeClock) -> tuple[IdleQueue[str], list[Deferred[str]]]:
    sent: list[Deferred[str]] = []
    return IdleQueue(clock, sent.append), sent


def test_newer_snapshot_replaces_the_waiting_one_and_keeps_its_time(fake_clock: FakeClock) -> None:
    queue, sent = _queue(fake_clock)
    queue.note_turn_started()  # a live turn keeps the process busy
    first = queue.enqueue("s1", "snapshot-1")
    fake_clock.advance(100)
    second = queue.enqueue("s1", "snapshot-2")
    assert len(queue) == 1
    assert second.item == "snapshot-2" and second.enqueued_at == first.enqueued_at
    fake_clock.advance(1600)
    assert queue.pump() == 0  # busy and not old enough yet (1,700 s)
    fake_clock.advance(100)
    assert queue.pump() == 1  # 1,800 s: runs even though a turn is live
    assert [d.item for d in sent] == ["snapshot-2"]


def test_runs_after_sixty_quiet_seconds(fake_clock: FakeClock) -> None:
    queue, sent = _queue(fake_clock)
    queue.note_turn_started()
    queue.enqueue("s1", "snap")
    fake_clock.advance(500)
    queue.note_turn_finished()
    fake_clock.advance(59)
    assert queue.pump() == 0
    fake_clock.advance(1)
    assert queue.pump() == 1
    assert sent[0].item == "snap"


def test_requeue_keeps_a_newer_waiting_review(fake_clock: FakeClock) -> None:
    queue, _ = _queue(fake_clock)
    old = Deferred("s1", "old", 0.0)
    queue.requeue(old)
    assert queue.waiting("s1") == old
    newer = queue.enqueue("s1", "new")
    queue.requeue(old)
    assert queue.waiting("s1") == newer


def test_run_polls_until_empty(fake_clock: FakeClock) -> None:
    queue, sent = _queue(fake_clock)
    queue.enqueue("s1", "snap")

    async def body() -> None:
        await queue.run()

    asyncio.run(run_driven(body(), fake_clock, step=5.0))
    assert [d.item for d in sent] == ["snap"]
    assert fake_clock.monotonic() >= 60


LOCAL = RuntimeRoute(
    provider="local", model="fake-model", api_mode="chat_completions",
    base_url="http://127.0.0.1:8080/v1",
)  # fmt: skip


def test_runner_defers_reviews_on_a_local_server(scope: Scope, fake_clock: FakeClock) -> None:
    async def body() -> None:
        parent = make_parent(scope, ScriptedCaller([text("hi")]))
        parent.runtime.route = LOCAL
        await parent.turn("hello")
        store = parent.runtime.store
        assert store is not None
        rows = tuple(await store.load("s1"))
        fork = ScriptedCaller([text(NOTHING)])
        runner = ReviewRunner(parent.review_parent(caller=fork))
        request = ReviewRequest(frozenset({"skills"}), rows, "s1")
        assert runner.start(request) is None
        assert len(runner.queue) == 1 and fork.requests == []
        refine = ReviewRequest(frozenset({"skills"}), rows, "s1", attended=True, reason="refine")
        task = runner.start(refine)  # /refine never waits
        assert task is not None
        await task
        assert len(fork.requests) == 1
        while runner.queue or runner.busy:  # 60 quiet seconds later the deferred one runs
            await checkpoint()
        assert len(fork.requests) == 2

    asyncio.run(run_driven(body(), fake_clock, step=5.0))


def test_defer_never_runs_at_once(scope: Scope) -> None:
    never = with_config(scope, {"learning": {"review": {"defer": "never"}}})

    async def body() -> None:
        parent = make_parent(never, ScriptedCaller([text("hi")]))
        parent.runtime.route = LOCAL
        await parent.turn("hello")
        store = parent.runtime.store
        assert store is not None
        fork = ScriptedCaller([text(NOTHING)])
        runner = ReviewRunner(parent.review_parent(caller=fork))
        request = ReviewRequest(frozenset({"skills"}), tuple(await store.load("s1")), "s1")
        task = runner.start(request)
        assert task is not None
        await task
        assert len(fork.requests) == 1

    asyncio.run(body())
