"""D-04.1 criteria (a)–(d) against the real P04 turn loop (P04-23).

The P00/P03 trial (``tests/trials/async_core``) proved the asyncio-first shape on a throwaway
harness. These tests run the same criteria through the code that ships: ``run_turn`` (prologue,
iterations, tool round, parallel executor, exit funnel), P03's ``RouteCaller`` over the fake model
server, ``TurnControl.interrupt``, ``dispatch`` with the shared blocking helper and, for (a) and
(c), the real P02 store.

- (a) an interrupt mid-stream closes the provider stream within 200 ms; alternation stays valid;
- (b) a blocking tool with a timeout is abandoned by timeout + 2 s while the loop keeps serving a
  heartbeat and other sessions. A child process needs more (SIGTERM, then SIGKILL 2 s later):
  that backstop is P05's terminal runner, not here;
- (c) 50 concurrent sessions keep p99 event latency (fake model frame → event sink) under 50 ms;
- (d) a hanging synchronous hook is abandoned at its deadline without holding up other calls.

Criterion (e), a synchronous caller driving and interrupting a turn through ``InProcessHost``, is
``tests/integration/core/test_d04_1_sync_caller.py``.

Every timing test is ``perf`` + ``slow`` and prints ``D041 …`` lines (run with ``-s``); the numbers
and the verdict are in ``docs/notes/P04/d-04-1-decision.md``. Real-time waits go through
``SystemClock.sleep``; nothing here uses a banned sleep.
"""

from __future__ import annotations

import asyncio
import math
import re
import statistics
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any

import pytest

from indus.core.exit_reasons import ExitReason
from indus.core.loop import run_turn
from indus.core.model_call import ModelCaller, OnEvent, OnWait, RouteCaller
from indus.core.persist import StateStore
from indus.core.turn_state import TurnResult, TurnState, new_turn_state
from indus.foundation.blocking import POOLS, run_blocking
from indus.foundation.clock import SystemClock
from indus.foundation.scope import Scope
from indus.protocol.events import EventBase, MessageDelta, ToolCompleted, ToolStarted
from indus.providers.streaming import CallOutcome
from indus.providers.types import ChatRequest
from indus.state import registry
from indus.tools.calls import ToolCall, ToolContext, ToolResult
from indus.tools.hooks import BeforeVerdict, ToolHooks
from indus.tools.registry import ToolTraits
from tests.fakes.clock import FakeClock
from tests.fakes.model import Finish, Stall, Text, Turn, fake_route, text_turn, tool_batch
from tests.fakes.model import ToolCall as ScriptCall
from tests.fakes.turn import TurnKit, check_alternation

CHAT = "chat_completions"
ANTHROPIC = "anthropic_messages"

INTERRUPT_BUDGET_MS = 200.0  # (a)
TOOL_TIMEOUT_S = 1.0  # (b)
ABANDON_SLACK_S = 2.0  # (b): abandoned by timeout + 2 s
LATENCY_BUDGET_MS = 50.0  # (c), and the streaming neighbours in (b)
HOOK_TIMEOUT_S = 1.0  # (d)
HEARTBEAT_S = 0.01
HEARTBEAT_GAP_BUDGET_S = 0.2  # "the loop stays responsive": no gap longer than the (a) budget
STUCK_S = 30.0  # a hung worker gives up after this long at most; every test releases it early

REAL = SystemClock()
TAG = re.compile(r"~d(\d+)~")
TAG_BYTES = re.compile(rb"~d(\d+)~")


# -- measurement helpers -----------------------------------------------------------------------


def p99(values: Sequence[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.99 * len(ordered)) - 1)]


def report(name: str, values_ms: Sequence[float], **extra: object) -> None:
    """One ``D041`` line: count, p50, p99 and max in milliseconds."""
    tail = " ".join(f"{k}={v}" for k, v in extra.items())
    print(
        f"\nD041 {name}: n={len(values_ms)} p50={statistics.median(values_ms):.3f}ms "
        f"p99={p99(values_ms):.3f}ms max={max(values_ms):.3f}ms {tail}".rstrip()
    )


def over_budget(values_ms: Sequence[float], budget_ms: float) -> list[float]:
    return [v for v in values_ms if v > budget_ms]


def tagged(count: int, *, base: int = 0) -> tuple[str, ...]:
    """``count`` text chunks, each carrying a tag unique within one session's script."""
    return tuple(f"~d{base + i}~ " for i in range(count))


class DeltaLatency:
    """Frame → sink latency: the fake server stamps each tagged frame as it emits it, the event
    sink looks the tag up when the ``MessageDelta`` carrying it arrives."""

    def __init__(self) -> None:
        self.sent: dict[int, float] = {}
        self.ms: list[float] = []

    def on_frame(self, _turn: int, frame: bytes) -> None:
        now = time.perf_counter()
        for match in TAG_BYTES.finditer(frame):
            self.sent.setdefault(int(match.group(1)), now)

    def on_event(self, event: EventBase) -> None:
        if not isinstance(event, MessageDelta):
            return
        now = time.perf_counter()
        for match in TAG.finditer(event.text):
            sent = self.sent.pop(int(match.group(1)), None)
            if sent is not None:
                self.ms.append((now - sent) * 1000)


class Heartbeat:
    """A task ticking every ``HEARTBEAT_S`` of real time; records the gaps between ticks."""

    def __init__(self) -> None:
        self.gaps: list[float] = []
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def _beat(self) -> None:
        last = time.perf_counter()
        while not self._stop.is_set():
            await REAL.sleep(HEARTBEAT_S)
            now = time.perf_counter()
            self.gaps.append(now - last)
            last = now

    def start(self) -> None:
        self._task = asyncio.ensure_future(self._beat())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task

    @property
    def max_gap(self) -> float:
        return max(self.gaps, default=0.0)


def responsive(beat: Heartbeat) -> bool:
    return bool(beat.gaps) and beat.max_gap < HEARTBEAT_GAP_BUDGET_S


async def wait_idle(pool_name: str, baseline: int = 0, limit_s: float = 5.0) -> None:
    """Wait (real time) until the named blocking pool is back to ``baseline`` busy workers."""
    pool = POOLS[pool_name]
    end = time.monotonic() + limit_s
    while pool.busy > baseline:
        if time.monotonic() > end:
            raise AssertionError(f"{pool!r} never went idle")
        await REAL.sleep(0.005)


# -- running real turns --------------------------------------------------------------------------


def make_kit(
    scope: Scope,
    clock: FakeClock,
    turns: Sequence[Turn],
    *,
    session_id: str = "s1",
    store: StateStore | None = None,
    latency: DeltaLatency | None = None,
) -> TurnKit:
    """A ``TurnKit`` whose fake server gives way to the loop before every frame."""
    kit = TurnKit(scope, clock, turns, store=store, session_id=session_id)
    kit.server.yield_every = 1
    if latency is not None:
        kit.server.on_frame = latency.on_frame
    return kit


async def run_once(
    kit: TurnKit,
    message: str,
    *,
    api_mode: str = CHAT,
    on_event: Callable[[EventBase, TurnState], object] | None = None,
    hooks: ToolHooks | None = None,
    wrap: Callable[[ModelCaller], ModelCaller] | None = None,
) -> TurnResult:
    """One real turn, awaited directly: nothing moves the fake clock, so the only timers that can
    fire are real-time ones (the tool and hook deadlines run on loop time)."""
    kit.generation += 1
    async with kit.server.client() as client:
        caller = RouteCaller(client, fake_route(kit.server, api_mode), kit.clock)
        runtime = kit.build_runtime(caller)
        if wrap is not None:
            runtime.caller = wrap(caller)
        if hooks is not None:
            runtime.hooks = hooks
        holder: list[TurnState] = []

        def sink(event: EventBase) -> None:
            kit.events.append(event)
            if on_event is not None and holder:
                on_event(event, holder[0])

        state = new_turn_state(
            runtime,
            message=message,
            turn_id=f"turn-{kit.generation}",
            generation=kit.generation,
            sink=sink,
            broker=kit.broker,
        )
        holder.append(state)
        kit.last_state = state
        return await run_turn(state)


@pytest.fixture
def handle(scope: Scope) -> Iterator[registry.StoreHandle]:
    store = registry.open_unshared(scope)
    yield store
    store.close()


# -- (a) interrupt mid-stream ----------------------------------------------------------------------

A_RUNS = 20
A_AFTER = 20  # interrupt once this many deltas reached the sink


def long_text() -> Turn:
    return Turn((Text(tagged(400)), Finish("stop")))


def idle_text() -> Turn:
    return Turn((Text(tagged(A_AFTER)), Stall(1000.0), Finish("stop")))


def cut_tool_call() -> Turn:
    call = ScriptCall(name="effect", arguments={"tag": "never"}, id="call_cut")
    return Turn((Text(("Let me do it. ",)), call, Stall(1000.0), Finish("tool_calls")))


# variant → (script, api mode, deltas before the interrupt, extra real delay before it)
A_VARIANTS: dict[str, tuple[Callable[[], list[Turn]], str, int, float]] = {
    "chat-text": (lambda: [long_text()], CHAT, A_AFTER, 0.0),
    "anthropic-text": (lambda: [long_text()], ANTHROPIC, A_AFTER, 0.0),
    "idle-stream": (lambda: [idle_text()], CHAT, A_AFTER, 0.02),
    "cut-tool-call": (lambda: [cut_tool_call()], CHAT, 1, 0.02),
    "after-tool-round": (
        lambda: [tool_batch(("echo", {"text": "x"})), long_text()],
        CHAT,
        A_AFTER,
        0.0,
    ),
}


class SlowToCancel:
    """Negative control: a caller that holds on to its cancellation for 300 ms before passing it
    on to the stream (what a stuck worker thread would look like)."""

    def __init__(self, inner: ModelCaller) -> None:
        self.inner = inner

    @property
    def model(self) -> str:
        return self.inner.model

    async def call(
        self, request: ChatRequest, *, on_event: OnEvent, on_wait: OnWait | None = None
    ) -> CallOutcome:
        task = asyncio.ensure_future(self.inner.call(request, on_event=on_event, on_wait=on_wait))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.shield(REAL.sleep(0.3))
            task.cancel()
            await asyncio.wait({task})
            raise


class InterruptAfter:
    """Event hook: once ``after`` deltas reached the sink, interrupt the turn ``delay`` seconds
    later, on the loop (as the in-process host does), and stamp the moment."""

    def __init__(self, after: int, delay: float) -> None:
        self.after = after
        self.delay = delay
        self.seen = 0
        self.fired_at: float | None = None

    def fire(self, state: TurnState) -> None:
        self.fired_at = time.perf_counter()
        assert state.control.interrupt(state.generation)

    def __call__(self, event: EventBase, state: TurnState) -> None:
        if not isinstance(event, MessageDelta):
            return
        self.seen += 1
        if self.seen == self.after:
            asyncio.get_running_loop().call_later(self.delay, self.fire, state)


async def interrupt_runs(
    scope: Scope,
    clock: FakeClock,
    handle: registry.StoreHandle,
    variant: str,
    *,
    runs: int = A_RUNS,
    wrap: Callable[[ModelCaller], ModelCaller] | None = None,
) -> tuple[list[float], list[float]]:
    """``runs`` fresh sessions interrupted mid-stream; (close ms, exit ms) per run."""
    script, api_mode, after, delay = A_VARIANTS[variant]
    store = StateStore(handle)
    close_ms: list[float] = []
    exit_ms: list[float] = []
    for n in range(runs):
        session_id = f"a-{variant}-{n}"
        kit = make_kit(scope, clock, script(), session_id=session_id, store=store)
        trigger = InterruptAfter(after, delay)
        result = await run_once(kit, "write a lot", api_mode=api_mode, on_event=trigger, wrap=wrap)
        returned = time.perf_counter()
        fired = trigger.fired_at
        assert fired is not None, f"{variant}: the interrupt never fired"
        record = kit.server.streams[-1]
        assert record.closed_at is not None and not record.completed
        close_ms.append((record.closed_at - fired) * 1000)
        exit_ms.append((returned - fired) * 1000)

        assert result.exit_reason is ExitReason.INTERRUPTED_BY_USER
        check_alternation(result.rows)
        stored = await store.load(session_id)
        check_alternation(stored)
        assert [r.role for r in stored] == [r.role for r in result.rows]
        assert stored[-1].role == "assistant" and not stored[-1].tool_calls
        if variant == "cut-tool-call":
            assert kit.tools.effects == []
            assert [r.role for r in stored] == ["user", "assistant"]
        if variant == "after-tool-round":
            assert [r.role for r in stored] == ["user", "assistant", "tool", "assistant"]
    return close_ms, exit_ms


@pytest.mark.perf
@pytest.mark.slow
@pytest.mark.parametrize("variant", list(A_VARIANTS))
def test_a_interrupt_mid_stream_closes_within_200ms(
    variant: str, scope: Scope, fake_clock: FakeClock, handle: registry.StoreHandle
) -> None:
    close_ms, exit_ms = asyncio.run(interrupt_runs(scope, fake_clock, handle, variant))
    report(f"a/{variant}/close", close_ms)
    report(f"a/{variant}/exit", exit_ms)
    assert over_budget(close_ms, INTERRUPT_BUDGET_MS) == []
    assert over_budget(exit_ms, INTERRUPT_BUDGET_MS) == []


@pytest.mark.perf
@pytest.mark.slow
def test_a_control_a_caller_holding_cancellation_is_flagged(
    scope: Scope, fake_clock: FakeClock, handle: registry.StoreHandle
) -> None:
    close_ms, _ = asyncio.run(
        interrupt_runs(scope, fake_clock, handle, "idle-stream", runs=2, wrap=SlowToCancel)
    )
    report("a/control/close", close_ms)
    assert len(over_budget(close_ms, INTERRUPT_BUDGET_MS)) == 2


# -- (b) blocking tool with a timeout -------------------------------------------------------------

B_NEIGHBOURS = 4
B_NEIGHBOUR_TURNS = 400
B_DELTAS = 100


class Hang:
    """A blocking call that waits until released (or ``STUCK_S``); counts how many are stuck."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Event()
        self.entered = 0
        self.left = 0
        self._lock = threading.Lock()

    def block(self) -> str:
        with self._lock:
            self.entered += 1
        self.started.set()
        try:
            self.release.wait(STUCK_S)
        finally:
            with self._lock:
                self.left += 1
        return "late"

    @property
    def stuck(self) -> int:
        with self._lock:
            return self.entered - self.left


def neighbour_script(turns: int) -> list[Turn]:
    return [Turn((Text(tagged(B_DELTAS, base=t * B_DELTAS)), Finish("stop"))) for t in range(turns)]


async def keep_streaming(
    kit: TurnKit, latency: DeltaLatency, stop: asyncio.Event, api_mode: str
) -> int:
    """Run turn after turn until ``stop``; returns how many completed."""
    done = 0
    while not stop.is_set() and kit.server.turns_left:
        result = await run_once(
            kit, "stream", api_mode=api_mode, on_event=lambda e, _s: latency.on_event(e)
        )
        assert result.exit_reason is ExitReason.COMPLETED
        done += 1
    return done


@pytest.mark.perf
@pytest.mark.slow
def test_b_blocking_tool_abandoned_while_loop_stays_responsive(
    scope: Scope, fake_clock: FakeClock
) -> None:
    hang = Hang()
    kit = make_kit(scope, fake_clock, [tool_batch(("hang", {})), text_turn("gave up on it")])
    kit.registry.register(
        "hang",
        "fake",
        {"description": "blocks its thread", "parameters": {"type": "object", "properties": {}}},
        lambda args: hang.block(),
        timeout_s=TOOL_TIMEOUT_S,
    )
    latencies = [DeltaLatency() for _ in range(B_NEIGHBOURS)]
    neighbours = [
        make_kit(
            scope,
            fake_clock,
            neighbour_script(B_NEIGHBOUR_TURNS),
            session_id=f"b-n{i}",
            latency=latencies[i],
        )
        for i in range(B_NEIGHBOURS)
    ]
    stamps: dict[str, float] = {}

    def on_event(event: EventBase, _state: TurnState) -> None:
        if isinstance(event, ToolStarted) and event.name == "hang":
            stamps["started"] = time.perf_counter()
        elif isinstance(event, ToolCompleted) and event.name == "hang":
            stamps["completed"] = time.perf_counter()

    async def scenario() -> tuple[TurnResult, Heartbeat, list[int], int]:
        tools = POOLS["tools"]
        baseline = tools.busy
        beat = Heartbeat()
        beat.start()
        stop = asyncio.Event()
        streams = [
            asyncio.ensure_future(
                keep_streaming(n, latencies[i], stop, CHAT if i % 2 else ANTHROPIC)
            )
            for i, n in enumerate(neighbours)
        ]
        try:
            result = await run_once(kit, "run the slow thing", on_event=on_event)
            still_stuck = tools.busy - baseline
        finally:
            stop.set()
            counts = list(await asyncio.gather(*streams))
            await beat.stop()
            hang.release.set()
        await wait_idle("tools", baseline)
        return result, beat, counts, still_stuck

    result, beat, counts, still_stuck = asyncio.run(scenario())
    abandon_s = stamps["completed"] - stamps["started"]
    neighbour_ms = [ms for lat in latencies for ms in lat.ms]
    report(
        "b/neighbour-latency",
        neighbour_ms,
        turns=sum(counts),
        abandon_s=f"{abandon_s:.3f}",
        heartbeat_max_gap_ms=f"{beat.max_gap * 1000:.1f}",
        heartbeat_p99_gap_ms=f"{p99(beat.gaps) * 1000:.1f}",
        ticks=len(beat.gaps),
    )
    assert TOOL_TIMEOUT_S <= abandon_s <= TOOL_TIMEOUT_S + ABANDON_SLACK_S
    assert still_stuck >= 1, "the worker thread should still be stuck (abandoned, not killed)"
    assert hang.entered == 1 and hang.stuck == 0
    assert responsive(beat), f"heartbeat gap {beat.max_gap:.3f} s"
    assert all(c >= 1 for c in counts), counts
    assert p99(neighbour_ms) < LATENCY_BUDGET_MS
    assert result.exit_reason is ExitReason.COMPLETED
    assert result.final_text == "gave up on it"
    assert [r.role for r in result.rows] == ["user", "assistant", "tool", "assistant"]
    assert "did not finish within" in str(result.rows[2].content)
    check_alternation(result.rows)


@pytest.mark.perf
@pytest.mark.slow
def test_b_control_a_handler_blocking_the_loop_is_flagged(
    scope: Scope, fake_clock: FakeClock
) -> None:
    async def blocker(args: Mapping[str, Any]) -> str:
        threading.Event().wait(0.3)  # wrong on purpose: blocks the event loop thread
        return "done"

    kit = make_kit(scope, fake_clock, [tool_batch(("blocker", {})), text_turn("ok")])
    kit.registry.register(
        "blocker",
        "fake",
        {"description": "blocks the loop", "parameters": {"type": "object", "properties": {}}},
        blocker,
    )

    async def scenario() -> Heartbeat:
        beat = Heartbeat()
        beat.start()
        await REAL.sleep(0.05)
        await run_once(kit, "go")
        await beat.stop()
        return beat

    beat = asyncio.run(scenario())
    print(f"\nD041 b/control: heartbeat_max_gap_ms={beat.max_gap * 1000:.1f}")
    assert not responsive(beat)


# -- (c) 50 concurrent sessions --------------------------------------------------------------------

C_SESSIONS = 50
C_DELTAS = 200


@pytest.mark.perf
@pytest.mark.slow
def test_c_fifty_sessions_keep_p99_event_latency_under_50ms(
    scope: Scope,
    fake_clock: FakeClock,
    handle: registry.StoreHandle,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = StateStore(handle)
    latencies = [DeltaLatency() for _ in range(C_SESSIONS)]
    kits = [
        make_kit(
            scope,
            fake_clock,
            [Turn((Stall(1.0), Text(tagged(C_DELTAS)), Finish("stop")))],
            session_id=f"c-{n:02d}",
            store=store,
            latency=latencies[n],
        )
        for n in range(C_SESSIONS)
    ]

    async def one(n: int) -> TurnResult:
        mode = CHAT if n % 2 == 0 else ANTHROPIC
        return await run_once(
            kits[n], "stream to me", api_mode=mode, on_event=lambda e, _s: latencies[n].on_event(e)
        )

    async def release_together() -> None:
        # Start barrier: every stream opens with a 1 s stall on the fake clock; once all 50 are
        # open, advancing the clock lets them stream at the same time (the store's writer thread
        # would otherwise stagger the prologues and the streams would barely overlap). A stream
        # whose body has not reached its stall yet parks after the first advance, so advance in
        # small steps until every stream has sent a tagged frame (stale detection is minutes
        # away). Header frames go out before the stall, so only tagged frames count.
        while sum(len(k.server.streams) for k in kits) < C_SESSIONS:
            await REAL.sleep(0.001)
        for _ in range(60):
            if all(lat.sent or lat.ms for lat in latencies):
                return
            fake_clock.advance(1.0)
            await REAL.sleep(0.002)
        raise AssertionError("the start barrier never released every stream")

    async def scenario() -> tuple[list[TurnResult], float]:
        started = time.perf_counter()
        barrier = asyncio.ensure_future(release_together())
        results = await asyncio.gather(*(one(n) for n in range(C_SESSIONS)))
        await barrier
        return list(results), time.perf_counter() - started

    with caplog.at_level("WARNING"):
        results, wall = asyncio.run(scenario())
    all_ms = [ms for lat in latencies for ms in lat.ms]
    refused = [r.getMessage() for r in caplog.records if "workers of the" in r.getMessage()]
    refused += [str(r.failure) for r in results if r.failure and "pool" in str(r.failure)]
    failures = {r.exit_reason.value: 0 for r in results}
    for r in results:
        failures[r.exit_reason.value] += 1
    streams = [k.server.streams[0] for k in kits]
    first_close = min(r.closed_at or 0.0 for r in streams)
    overlap = sum(1 for r in streams if r.opened_at <= first_close)
    report(
        "c/frame-to-sink",
        all_ms,
        sessions=C_SESSIONS,
        open_together=overlap,
        wall_ms=f"{wall * 1000:.0f}",
        exits=failures,
        pool_full=len(refused),
    )
    assert refused == []
    assert overlap == C_SESSIONS  # all 50 streams were open at once
    expected = "".join(tagged(C_DELTAS)).strip()
    for n, result in enumerate(results):
        assert result.exit_reason is ExitReason.COMPLETED, (n, result.failure)
        assert result.final_text == expected
        check_alternation(result.rows)
    assert len(all_ms) == C_SESSIONS * C_DELTAS
    assert p99(all_ms) < LATENCY_BUDGET_MS

    async def stored() -> None:
        for n in range(C_SESSIONS):
            rows = await store.load(f"c-{n:02d}")
            assert [r.role for r in rows] == ["user", "assistant"]
            check_alternation(rows)

    asyncio.run(stored())


# -- (d) a hanging synchronous hook ---------------------------------------------------------------


class SyncHookSeam:
    """The shape P13-17 plugs in: a synchronous plugin ``pre_tool_call`` hook run on the ``hooks``
    pool with a deadline. A deadline miss (or a full pool) raises, and the non-empty seam fails
    closed: the call is blocked (R07.18)."""

    def __init__(self, hook: Callable[[str, Mapping[str, Any]], object]) -> None:
        self.hook = hook

    @property
    def empty(self) -> bool:
        return False

    async def before(self, call: ToolCall, ctx: ToolContext) -> BeforeVerdict | None:
        await run_blocking(
            self.hook,
            call.name,
            dict(call.args),
            pool="hooks",
            deadline=HOOK_TIMEOUT_S,
            label=f"pre-call hook for {call.name}",
        )
        return None

    async def after(
        self, call: ToolCall, ctx: ToolContext, result: ToolResult, duration_s: float
    ) -> None:
        return None

    async def transform(
        self, call: ToolCall, ctx: ToolContext, result: ToolResult
    ) -> ToolResult | None:
        return None


def add_probe(kit: TurnKit, ran: dict[str, float]) -> None:
    """A read-only tool that stamps when it ran (so two calls form a parallel batch)."""

    async def probe(args: Mapping[str, Any]) -> str:
        ran[str(args.get("q"))] = time.perf_counter()
        return f"probed {args.get('q')}"

    kit.registry.register(
        "probe",
        "fake",
        {
            "description": "stamps when it runs",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
        probe,
        traits=ToolTraits(read_only=True),
    )


def completed_at(kit: TurnKit, stamps: list[float], q: str) -> float:
    """The perf stamp of the ``ToolCompleted`` event for the call asked ``q``."""
    started = {e.call_id: e.arguments.get("q") for e in kit.events if isinstance(e, ToolStarted)}
    for event, stamp in zip(kit.events, stamps, strict=True):
        if isinstance(event, ToolCompleted) and started.get(event.call_id) == q:
            return stamp
    raise AssertionError(f"no completed call for {q!r}")


def stamped(kit: TurnKit, stamps: list[float]) -> Callable[[EventBase, TurnState], None]:
    def on_event(_event: EventBase, _state: TurnState) -> None:
        stamps.append(time.perf_counter())

    return on_event


@pytest.mark.perf
@pytest.mark.slow
def test_d_hanging_sync_hook_abandoned_without_blocking_others(
    scope: Scope, fake_clock: FakeClock
) -> None:
    hang = Hang()

    def plugin_hook(name: str, args: Mapping[str, Any]) -> None:
        if args.get("q") == "hang":
            hang.block()

    seam = SyncHookSeam(plugin_hook)
    both = tool_batch(("probe", {"q": "hang"}), ("probe", {"q": "fine"}))
    one = tool_batch(("lookup", {"q": "other"}))
    kit_a = make_kit(scope, fake_clock, [both, text_turn("a done")], session_id="d-a")
    ran: dict[str, float] = {}
    add_probe(kit_a, ran)
    kit_b = make_kit(scope, fake_clock, [one, text_turn("b done")], session_id="d-b")
    kit_c = make_kit(
        scope,
        fake_clock,
        [tool_batch(("lookup", {"q": "hang"})), text_turn("c done")],
        session_id="d-c",
    )
    kit_d = make_kit(scope, fake_clock, [one, text_turn("d done")], session_id="d-d")
    stamps: dict[str, list[float]] = {k: [] for k in "abcd"}

    async def scenario() -> dict[str, Any]:
        hooks_pool = POOLS["hooks"]
        baseline = hooks_pool.busy
        beat = Heartbeat()
        beat.start()
        out: dict[str, Any] = {}
        try:
            t0 = time.perf_counter()
            task_a = asyncio.ensure_future(
                run_once(kit_a, "look both up", hooks=seam, on_event=stamped(kit_a, stamps["a"]))
            )
            # B starts once A's hook hangs and A's sibling call has run (one hook worker stuck,
            # the rest free).
            while not (hang.started.is_set() and "fine" in ran):
                await REAL.sleep(0.001)
            out["b"] = await run_once(
                kit_b, "look it up", hooks=seam, on_event=stamped(kit_b, stamps["b"])
            )
            out["b_done_s"] = time.perf_counter() - t0
            out["a"] = await task_a
            out["t0"] = t0
            out["stuck_after_a"] = hooks_pool.busy - baseline
            # Two hung hooks at once: they hold two of the eight hook workers (P13-17); another
            # session's hooked call still runs at once instead of waiting behind them.
            hang.started.clear()
            task_c = asyncio.ensure_future(
                run_once(kit_c, "hang again", hooks=seam, on_event=stamped(kit_c, stamps["c"]))
            )
            while hang.stuck < 2:
                await REAL.sleep(0.001)
            d0 = time.perf_counter()
            out["d"] = await run_once(
                kit_d, "look it up", hooks=seam, on_event=stamped(kit_d, stamps["d"])
            )
            out["d_s"] = time.perf_counter() - d0
            out["c"] = await task_c
            out["threads"] = hooks_pool.threads
        finally:
            await beat.stop()
            hang.release.set()
        await wait_idle("hooks", baseline)
        out["beat"] = beat
        return out

    out = asyncio.run(scenario())
    a, b, c, d = out["a"], out["b"], out["c"], out["d"]
    beat: Heartbeat = out["beat"]
    t0: float = out["t0"]
    hung_s = completed_at(kit_a, stamps["a"], "hang") - t0
    sibling_s = ran["fine"] - t0
    print(
        f"\nD041 d: hung_call_blocked_s={hung_s:.3f} sibling_ran_s={sibling_s:.3f} "
        f"other_session_done_s={out['b_done_s']:.3f} third_call_s={out['d_s']:.3f} "
        f"heartbeat_max_gap_ms={beat.max_gap * 1000:.1f} hook_threads={out['threads']}"
    )
    # The hung hook is abandoned at its deadline and its call is blocked (fail closed).
    assert HOOK_TIMEOUT_S <= hung_s <= HOOK_TIMEOUT_S + ABANDON_SLACK_S
    hung_row, fine_row = (r for r in a.rows if r.role == "tool")
    assert "DeadlineMissed" in str(hung_row.content)
    assert "probed fine" in str(fine_row.content)
    assert "hang" not in ran  # the blocked call never ran
    assert out["stuck_after_a"] >= 1
    # Nothing else waited for it: the sibling call ran and the other session finished long
    # before the deadline. (The batch's results are committed together, so the sibling's result
    # reaches the model with the batch, R01.14.)
    assert sibling_s < HOOK_TIMEOUT_S / 2
    assert out["b_done_s"] < HOOK_TIMEOUT_S / 2
    assert responsive(beat), f"heartbeat gap {beat.max_gap:.3f} s"
    for result, final in ((a, "a done"), (b, "b done"), (c, "c done"), (d, "d done")):
        assert result.exit_reason is ExitReason.COMPLETED
        assert result.final_text == final
        check_alternation(result.rows)
    assert "same answer" in str(next(r for r in b.rows if r.role == "tool").content)
    # Two hook workers stuck: the next hooked call still runs at once (the pool bound itself is
    # pinned in tests/unit/foundation/test_blocking.py), and the pool never grew.
    assert "same answer" in str(next(r for r in d.rows if r.role == "tool").content)
    assert out["d_s"] < HOOK_TIMEOUT_S / 2
    assert out["threads"] <= POOLS["hooks"].workers
