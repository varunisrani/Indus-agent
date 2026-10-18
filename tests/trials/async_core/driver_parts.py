"""D-04.1 trial, second half (P03-19): the P03 stream driver and fake model in the trial.

Throwaway prototype, not product code. Pieces:

- ``driver_source``: an async-generator source for the P00-13 ``Session`` that runs a real
  ``stream_call`` (SSE parser + transport + driver) over the fake model's ``MockTransport``.
  Each delta is stamped when the fake server hands the frame over, so the latency covers the
  whole path: frame → httpx → SSE → transport → driver → session queue → consumer.
- ``Streamers``: N sessions that keep streaming turns until told to stop, recording per-event
  latency (server frame → driver ``on_event``), the background load for criteria (b) and (d).
- ``run_with_deadline``: a child process with a timeout and a kill backstop (criterion b).
- ``BoundedHookPool``: a fixed-size thread pool for synchronous hooks with a deadline that
  refuses work when full instead of growing (criterion d).
- ``LoopThread``: an event loop on a background thread driven from synchronous code (criterion e).

Time is only measured here; waiting uses the loop's own timeouts (subprocess, executor), a
``FakeClock`` that is never advanced (stalls that never end), or ``threading.Event``.
"""

from __future__ import annotations

import asyncio
import json
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import AsyncGenerator, Callable, Coroutine
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from indus.providers.streaming import stream_call
from indus.providers.timeouts import TimeoutPolicy
from indus.providers.transports import get_transport
from indus.providers.types import ChatRequest, TextDelta
from tests.fakes.clock import FakeClock
from tests.fakes.model import FakeModelServer, Finish, Script, Stall, Text, Turn, fake_route
from tests.trials.async_core.harness import Produced, SourceProbe, TextPiece, percentile

START = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
_DELTA_ID = re.compile(rb'"(?:content|text)":"w(\d+) "')


def numbered_script(deltas: int, *, hang: bool) -> Script:
    """One turn of ``deltas`` text chunks ``w0 `` … ; with ``hang`` it then stalls forever."""
    steps: list[Any] = [Text(tuple(f"w{i} " for i in range(deltas)))]
    steps.append(Stall(10**9) if hang else Finish("stop"))
    return Script((Turn(tuple(steps)),))


@dataclass
class Stamps:
    """Produced-time of each numbered delta, taken when the server hands its frame over."""

    by_id: dict[int, int] = field(default_factory=dict)

    def on_frame(self, turn: int, frame: bytes) -> None:
        match = _DELTA_ID.search(frame)
        if match:
            self.by_id[int(match.group(1))] = time.perf_counter_ns()

    def produced(self, text: str) -> int:
        return self.by_id[int(text.strip()[1:])]


def request() -> ChatRequest:
    return ChatRequest(model="fake-model-1", messages=[{"role": "user", "content": "go"}])


async def driver_source(
    probe: SourceProbe,
    *,
    deltas: int,
    api_mode: str = "chat_completions",
    hang: bool = True,
    store: list[str] | None = None,
) -> AsyncGenerator[Produced]:
    """A trial source backed by ``stream_call`` over the fake model.

    ``probe.closed_ns`` is the moment the fake server saw its response body closed, i.e. the
    HTTP stream really ended. ``store`` gets one JSON line per delta (a stand-in for the store
    write the real turn loop does per event).
    """
    clock = FakeClock(START)  # never advanced: a stall never ends, the watchdog never fires
    stamps = Stamps()
    server = FakeModelServer(
        numbered_script(deltas, hang=hang), clock=clock, yield_every=1, on_frame=stamps.on_frame
    )
    queue: asyncio.Queue[Any] = asyncio.Queue()
    done = object()

    def on_event(event: Any) -> None:
        if isinstance(event, TextDelta):
            queue.put_nowait(event)

    async def call() -> None:
        try:
            async with server.client() as client:
                await stream_call(
                    client,
                    get_transport(api_mode),
                    fake_route(server, api_mode),
                    request(),
                    policy=TimeoutPolicy(),
                    clock=clock,
                    on_event=on_event,
                    route_epoch=lambda: 0,
                )
        finally:
            queue.put_nowait(done)

    task = asyncio.ensure_future(call())
    try:
        while True:
            item = await queue.get()
            if item is done:
                break
            if store is not None:
                store.append(json.dumps({"delta": item.text}))
            probe.produced += 1
            yield Produced(TextPiece(item.text), stamps.produced(item.text))
        task.result()
        probe.close_reason = "finished"
    except asyncio.CancelledError:
        probe.close_reason = "cancelled"
        raise
    except GeneratorExit:
        probe.close_reason = "aclose"
        raise
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        closed = server.streams[0].closed_at if server.streams else None
        probe.closed_ns = round(closed * 1e9) if closed is not None else time.perf_counter_ns()


# -- background streaming sessions ---------------------------------------------------------------


@dataclass
class Streamers:
    """``count`` sessions streaming turn after turn through the driver until ``stop`` is set."""

    count: int = 10
    deltas_per_turn: int = 100
    latencies_ms: list[float] = field(default_factory=list)
    turns: int = 0

    async def run(self, stop: asyncio.Event) -> None:
        await asyncio.gather(*(self._session(i, stop) for i in range(self.count)))

    async def _session(self, n: int, stop: asyncio.Event) -> None:
        api_mode = "chat_completions" if n % 2 == 0 else "anthropic_messages"
        while not stop.is_set():
            clock = FakeClock(START)
            stamps = Stamps()
            server = FakeModelServer(
                numbered_script(self.deltas_per_turn, hang=False),
                clock=clock,
                yield_every=1,
                on_frame=stamps.on_frame,
            )

            def on_event(event: Any, stamps: Stamps = stamps) -> None:
                if isinstance(event, TextDelta):
                    delay = time.perf_counter_ns() - stamps.produced(event.text)
                    self.latencies_ms.append(delay / 1e6)

            async with server.client() as client:
                await stream_call(
                    client,
                    get_transport(api_mode),
                    fake_route(server, api_mode),
                    request(),
                    policy=TimeoutPolicy(),
                    clock=clock,
                    on_event=on_event,
                    route_epoch=lambda: 0,
                )
            self.turns += 1

    @property
    def p99(self) -> float:
        return percentile(self.latencies_ms, 99)

    def summary(self) -> str:
        return (
            f"streamers={self.count} turns={self.turns} events={len(self.latencies_ms)} "
            f"p50={percentile(self.latencies_ms, 50):.3f} ms p99={self.p99:.3f} ms "
            f"max={max(self.latencies_ms):.3f} ms"
        )


# -- (b) child process with a deadline ------------------------------------------------------------

# A child that blocks on stdin; with ``ignore_term`` it also ignores SIGTERM, so only the kill
# backstop can end it (the worst case).
CHILD = (
    "import signal, sys\n"
    "if sys.argv[1] == 'ignore_term':\n"
    "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "print('ready', flush=True)\n"
    "sys.stdin.read()\n"
)


@dataclass(frozen=True)
class ChildResult:
    timeout: float
    ended_after: float | None  # seconds from start to the child's exit; None = still alive
    returncode: int | None
    how: str  # "exited", "terminated", "killed", "alive"


async def run_with_deadline(
    *, timeout: float, ignore_term: bool, backstop: float | None = 2.0, observe: float = 3.0
) -> ChildResult:
    """Start the child, stop it at ``timeout`` (SIGTERM), kill it ``backstop`` s later.

    ``backstop=None`` is the negative control: no kill, the child is only watched for
    ``observe`` more seconds, then cleaned up.
    """
    start = time.perf_counter()
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        CHILD,
        "ignore_term" if ignore_term else "plain",
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert proc.stdout is not None
    await proc.stdout.readline()  # the child is up (and has set its signal handler)
    how = "exited"
    try:
        await asyncio.wait_for(proc.wait(), timeout - (time.perf_counter() - start))
    except TimeoutError:
        proc.send_signal(signal.SIGTERM)
        how = "terminated"
        wait = backstop if backstop is not None else observe
        try:
            await asyncio.wait_for(proc.wait(), wait)
        except TimeoutError:
            if backstop is None:
                ended = None
                proc.kill()
                await proc.wait()
                return ChildResult(timeout, ended, None, "alive")
            proc.kill()
            how = "killed"
            await proc.wait()
    return ChildResult(timeout, time.perf_counter() - start, proc.returncode, how)


def child_violations(
    result: ChildResult, *, backstop: float = 2.0, slack: float = 0.5
) -> list[str]:
    """The child must be gone by ``timeout + backstop`` (plus scheduling slack)."""
    if result.ended_after is None:
        return [f"child still alive after {result.timeout + backstop:.1f}s"]
    limit = result.timeout + backstop + slack
    if result.ended_after > limit:
        return [f"child ended after {result.ended_after:.3f}s (> {limit:.1f}s)"]
    return []


# -- (d) bounded pool for synchronous hooks -------------------------------------------------------


class PoolFull(RuntimeError):
    """Every worker is busy (or stuck); the hook is refused instead of growing the pool."""


@dataclass(frozen=True)
class HookOutcome:
    status: str  # "done" or "abandoned"
    value: Any = None
    waited: float = 0.0


class BoundedHookPool:
    def __init__(self, workers: int) -> None:
        self.workers = workers
        self._slots = threading.BoundedSemaphore(workers)
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="trial-hook")

    @property
    def threads(self) -> int:
        return len(self._executor._threads)  # pyright: ignore[reportPrivateUsage]

    async def run(self, hook: Callable[[], Any], *, deadline: float | None) -> HookOutcome:
        if not self._slots.acquire(blocking=False):
            raise PoolFull(f"all {self.workers} hook workers are busy")

        def guarded() -> Any:
            try:
                return hook()
            finally:
                self._slots.release()  # a stuck hook keeps its slot until it really returns

        start = time.perf_counter()
        future = asyncio.get_running_loop().run_in_executor(self._executor, guarded)
        if deadline is None:
            return HookOutcome("done", await future, time.perf_counter() - start)
        try:
            value = await asyncio.wait_for(asyncio.shield(future), deadline)
        except TimeoutError:
            return HookOutcome("abandoned", None, time.perf_counter() - start)
        return HookOutcome("done", value, time.perf_counter() - start)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)


# -- (e) a loop on a background thread, driven by synchronous code ---------------------------------


class LoopThread:
    """``asyncio`` loop running on its own thread; sync code submits coroutines to it."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._main, name="trial-loop", daemon=True)
        self._thread.start()

    def _main(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit[T](self, coro: Coroutine[Any, Any, T]) -> Future[T]:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def close(self) -> None:
        async def drain() -> None:
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        self.submit(drain()).result(timeout=5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)
        self.loop.close()


@dataclass
class DrivenTurn:
    """A streaming turn living in the loop thread; the REPL side holds this handle."""

    server: FakeModelServer
    task: asyncio.Task[Any]
    first_deltas: threading.Event
    interrupted_flag: bool = False


async def start_driven_turn(
    *, deltas_before_signal: int = 5, honour_interrupt: bool = True
) -> DrivenTurn:
    """Start a turn that streams some text then stalls forever (until interrupted)."""
    clock = FakeClock(START)
    server = FakeModelServer(numbered_script(50, hang=True), clock=clock, yield_every=1)
    seen = threading.Event()
    count = [0]

    def on_event(event: Any) -> None:
        if isinstance(event, TextDelta):
            count[0] += 1
            if count[0] >= deltas_before_signal:
                seen.set()

    async def turn() -> Any:
        async with server.client() as client:
            coro = stream_call(
                client,
                get_transport("chat_completions"),
                fake_route(server, "chat_completions"),
                request(),
                policy=TimeoutPolicy(),
                clock=clock,
                on_event=on_event,
                route_epoch=lambda: 0,
            )
            if honour_interrupt:
                return await coro
            # Negative control: the turn shields its call, so a cancel never reaches the stream.
            return await asyncio.shield(asyncio.ensure_future(coro))

    task = asyncio.ensure_future(turn())
    return DrivenTurn(server, task, seen)


async def interrupt_turn(turn: DrivenTurn) -> None:
    turn.task.cancel()
    await asyncio.gather(turn.task, return_exceptions=True)


def stream_closed_after(turn: DrivenTurn, since: float) -> float | None:
    closed = turn.server.streams[0].closed_at if turn.server.streams else None
    return None if closed is None else closed - since


__all__ = [
    "BoundedHookPool",
    "ChildResult",
    "DrivenTurn",
    "HookOutcome",
    "LoopThread",
    "PoolFull",
    "Stamps",
    "Streamers",
    "child_violations",
    "driver_source",
    "interrupt_turn",
    "numbered_script",
    "run_with_deadline",
    "start_driven_turn",
    "stream_closed_after",
]
