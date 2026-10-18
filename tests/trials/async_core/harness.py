"""Throwaway prototype for the D-04.1 trial (asyncio-first core). Not product code.

Pieces:
- ``Session``: history, a generation number, the running turn task and an event queue.
- a turn is one task reading an async-generator source of deltas; every exit (completed,
  interrupted, failed) goes through ``Session._funnel``, which seals history.
- ``scripted_stream`` / ``stubborn_stream``: fake streaming sources whose closing is observable.
- ``SurfaceConsumer``: dequeues a session's events and records delivery latency.

Time is only measured here (``time.perf_counter_ns`` by default, or an injected source); nothing
waits on it. Sources give way to the loop with ``checkpoint()``.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import AsyncGenerator, Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from tests.fakes.clock import FakeClock, checkpoint

NowNs = Callable[[], int]

# -- stream pieces --------------------------------------------------------------------------------


@dataclass(frozen=True)
class TextPiece:
    text: str


@dataclass(frozen=True)
class ToolPiece:
    """One fragment of a tool call's arguments; ``last`` marks the final fragment."""

    call_id: str
    name: str
    fragment: str
    last: bool


Piece = TextPiece | ToolPiece


@dataclass(frozen=True)
class Produced:
    piece: Piece
    produced_ns: int


@dataclass
class SourceProbe:
    """What the fake source reports about itself."""

    produced: int = 0
    closed_ns: int | None = None
    close_reason: str | None = None


async def scripted_stream(
    probe: SourceProbe,
    pieces: Sequence[Piece],
    *,
    every: int = 2,
    hang_at_end: bool = False,
    now_ns: NowNs = time.perf_counter_ns,
) -> AsyncGenerator[Produced]:
    """Yield ``pieces`` one per ``every`` loop iterations; optionally never finish afterwards."""
    try:
        for piece in pieces:
            for _ in range(every):
                await checkpoint()
            probe.produced += 1
            yield Produced(piece, now_ns())
        if hang_at_end:
            await asyncio.Event().wait()  # the next delta never comes
        probe.close_reason = "finished"
    except asyncio.CancelledError:
        probe.close_reason = "cancelled"
        raise
    except GeneratorExit:
        probe.close_reason = "aclose"
        raise
    finally:
        probe.closed_ns = now_ns()


async def stubborn_stream(
    probe: SourceProbe,
    clock: FakeClock,
    *,
    hold_seconds: float,
    now_ns: NowNs,
    every: int = 2,
) -> AsyncGenerator[Produced]:
    """Negative control: on cancellation it holds on for ``hold_seconds`` of clock time."""
    try:
        n = 0
        while True:
            for _ in range(every):
                await checkpoint()
            n += 1
            probe.produced += 1
            yield Produced(TextPiece(f"t{n} "), now_ns())
    except (asyncio.CancelledError, GeneratorExit):
        probe.close_reason = "held"
        await clock.sleep(hold_seconds)
        raise
    finally:
        probe.closed_ns = now_ns()


# -- history ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCallRecord:
    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class Message:
    role: Literal["user", "assistant", "tool"]
    content: str
    tool_calls: tuple[ToolCallRecord, ...] = ()
    tool_call_id: str | None = None
    partial: bool = False
    synthetic: bool = False


def check_alternation(history: Sequence[Message]) -> list[str]:
    """Problems that would make the history unreplayable; empty means valid.

    Rules: starts with a user message; an assistant message follows a user or tool message; every
    tool call on an assistant message is answered by a tool message right after it, before
    anything else; no tool message without a matching open call.
    """
    problems: list[str] = []
    open_calls: list[str] = []
    previous: str | None = None
    for i, msg in enumerate(history):
        if msg.role == "tool":
            if msg.tool_call_id not in open_calls:
                problems.append(f"#{i}: tool result {msg.tool_call_id!r} answers no open call")
            else:
                open_calls.remove(msg.tool_call_id)
        else:
            if open_calls:
                problems.append(f"#{i}: {msg.role} message while calls {open_calls} are unanswered")
                open_calls = []
            if msg.role == "user" and previous not in (None, "assistant", "tool"):
                problems.append(f"#{i}: user message after {previous}")
            if msg.role == "assistant" and previous not in ("user", "tool"):
                problems.append(f"#{i}: assistant message after {previous}")
            open_calls = [c.call_id for c in msg.tool_calls]
        if i == 0 and msg.role != "user":
            problems.append("#0: history must start with a user message")
        previous = msg.role
    if open_calls:
        problems.append(f"end: calls {open_calls} are unanswered")
    return problems


# -- session and turn ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    session_id: str
    generation: int
    kind: Literal["text", "tool_call", "turn_end"]
    seq: int
    produced_ns: int


@dataclass(frozen=True)
class TurnRecord:
    generation: int
    outcome: Literal["completed", "interrupted", "failed"]
    interrupt_ns: int | None
    exit_ns: int
    dropped_calls: tuple[str, ...]
    error: str | None = None


@dataclass
class _CallBuffer:
    call_id: str
    name: str
    arguments: str = ""
    complete: bool = False


@dataclass
class _TurnState:
    text: list[str] = field(default_factory=list)
    calls: dict[str, _CallBuffer] = field(default_factory=dict)


SYNTHETIC_INTERRUPTED = "[tool not run: the turn was interrupted]"
SYNTHETIC_NOT_RUN = "[tool not run]"


class Session:
    def __init__(
        self,
        session_id: str,
        now_ns: NowNs = time.perf_counter_ns,
        *,
        await_between_deltas: bool = False,
    ) -> None:
        self.session_id = session_id
        self.now_ns = now_ns
        # True models a turn that awaits other work (a store write, a send) between deltas, so a
        # cancel can land outside the source; the source is then closed through aclose().
        self.await_between_deltas = await_between_deltas
        self.history: list[Message] = []
        self.generation = 0
        self.events: asyncio.Queue[Event] = asyncio.Queue()
        self.turns: list[TurnRecord] = []
        self._task: asyncio.Task[TurnRecord] | None = None
        self._interrupt_for: int | None = None
        self._interrupt_ns: int | None = None
        self._seq = 0

    def start_turn(
        self, user_text: str, source: AsyncGenerator[Produced]
    ) -> asyncio.Task[TurnRecord]:
        if self._task is not None and not self._task.done():
            raise RuntimeError("a turn is already running")
        self.generation += 1
        self._interrupt_for = None
        self._interrupt_ns = None
        self.history.append(Message("user", user_text))
        self._task = asyncio.create_task(
            self._run(self.generation, source), name=f"turn-{self.session_id}-{self.generation}"
        )
        return self._task

    def interrupt(self, generation: int) -> bool:
        """Cancel the turn of ``generation``; a stale generation is ignored (returns False)."""
        task = self._task
        if generation != self.generation or task is None or task.done():
            return False
        if self._interrupt_for == generation:
            return True
        self._interrupt_for = generation
        self._interrupt_ns = self.now_ns()
        task.cancel()
        return True

    def _post(
        self, generation: int, kind: Literal["text", "tool_call", "turn_end"], ns: int
    ) -> None:
        self._seq += 1
        self.events.put_nowait(Event(self.session_id, generation, kind, self._seq, ns))

    async def _run(self, generation: int, source: AsyncGenerator[Produced]) -> TurnRecord:
        state = _TurnState()
        outcome: Literal["completed", "interrupted", "failed"] = "completed"
        error: str | None = None
        foreign_cancel = False
        try:
            try:
                async for produced in source:
                    self._apply(generation, state, produced)
                    if self.await_between_deltas:
                        await checkpoint()
            finally:
                await source.aclose()  # no-op if the source already finished or unwound
        except asyncio.CancelledError:
            outcome = "interrupted"
            task = asyncio.current_task()
            if self._interrupt_for == generation and task is not None:
                task.uncancel()
            else:
                foreign_cancel = True  # shutdown, not our interrupt: seal, then propagate
        except Exception as exc:
            outcome = "failed"
            error = repr(exc)
        record = self._funnel(generation, state, outcome, error)
        if foreign_cancel:
            raise asyncio.CancelledError
        return record

    def _apply(self, generation: int, state: _TurnState, produced: Produced) -> None:
        piece = produced.piece
        if isinstance(piece, TextPiece):
            state.text.append(piece.text)
            self._post(generation, "text", produced.produced_ns)
        else:
            buf = state.calls.setdefault(piece.call_id, _CallBuffer(piece.call_id, piece.name))
            buf.arguments += piece.fragment
            buf.complete = piece.last
            self._post(generation, "tool_call", produced.produced_ns)

    def _funnel(
        self,
        generation: int,
        state: _TurnState,
        outcome: Literal["completed", "interrupted", "failed"],
        error: str | None,
    ) -> TurnRecord:
        """The single exit: seal the assistant message and answer every kept call."""
        finished = tuple(
            ToolCallRecord(c.call_id, c.name, c.arguments)
            for c in state.calls.values()
            if c.complete
        )
        dropped = tuple(c.call_id for c in state.calls.values() if not c.complete)
        self.history.append(
            Message(
                "assistant",
                "".join(state.text),
                tool_calls=finished,
                partial=outcome != "completed",
            )
        )
        note = SYNTHETIC_NOT_RUN if outcome == "completed" else SYNTHETIC_INTERRUPTED
        for call in finished:  # the trial runs no tools: every call gets a synthetic answer
            self.history.append(Message("tool", note, tool_call_id=call.call_id, synthetic=True))
        exit_ns = self.now_ns()
        self._post(generation, "turn_end", exit_ns)
        record = TurnRecord(generation, outcome, self._interrupt_ns, exit_ns, dropped, error)
        self.turns.append(record)
        return record


# -- surface consumer -----------------------------------------------------------------------------


class SurfaceConsumer:
    """Reads a session's events like a UI would and records produced→dequeued latency."""

    def __init__(self, session: Session, now_ns: NowNs | None = None) -> None:
        self.session = session
        self.now_ns = now_ns or session.now_ns
        self.latencies_ns: list[int] = []
        self.deltas_seen = 0
        self._wanted = 0
        self._reached = asyncio.Event()

    async def seen(self, count: int) -> None:
        """Wait (on an event, not a timer) until ``count`` delta events were dequeued."""
        self._wanted = count
        if self.deltas_seen >= count:
            return
        self._reached.clear()
        await self._reached.wait()

    async def run(self, turns: int = 1) -> None:
        ended = 0
        while ended < turns:
            event = await self.session.events.get()
            if event.kind == "turn_end":
                ended += 1
                continue
            self.latencies_ns.append(self.now_ns() - event.produced_ns)
            self.deltas_seen += 1
            if self._wanted and self.deltas_seen >= self._wanted:
                self._reached.set()


# -- measurements ----------------------------------------------------------------------------------


def percentile(values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile."""
    if not values:
        raise ValueError("no values")
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


@dataclass(frozen=True)
class InterruptResult:
    close_ms: float
    exit_ms: float
    record: TurnRecord
    probe: SourceProbe
    history: tuple[Message, ...]
    problems: tuple[str, ...]


def budget_violations(results: Sequence[InterruptResult], budget_ms: float) -> list[str]:
    """Runs where closing the source or reaching the funnel took longer than ``budget_ms``."""
    out: list[str] = []
    for i, r in enumerate(results):
        if r.close_ms > budget_ms:
            out.append(f"run {i}: source closed after {r.close_ms:.3f} ms")
        if r.exit_ms > budget_ms:
            out.append(f"run {i}: exit funnel reached after {r.exit_ms:.3f} ms")
    return out


def _interrupt_result(session: Session, record: TurnRecord, probe: SourceProbe) -> InterruptResult:
    if record.interrupt_ns is None or probe.closed_ns is None:
        raise AssertionError(f"turn was not interrupted or source never closed: {record}")
    return InterruptResult(
        close_ms=(probe.closed_ns - record.interrupt_ns) / 1e6,
        exit_ms=(record.exit_ns - record.interrupt_ns) / 1e6,
        record=record,
        probe=probe,
        history=tuple(session.history),
        problems=tuple(check_alternation(session.history)),
    )


def text_pieces(count: int) -> list[Piece]:
    return [TextPiece(f"w{i} ") for i in range(count)]


def tool_call_pieces() -> list[Piece]:
    """Some text, one finished tool call, then a second call cut after its first fragment."""
    return [
        TextPiece("Let me look. "),
        ToolPiece("call-1", "read_file", '{"path": ', last=False),
        ToolPiece("call-1", "read_file", '"notes.txt"', last=False),
        ToolPiece("call-1", "read_file", "}", last=True),
        ToolPiece("call-2", "search", '{"query": "par', last=False),
    ]


async def run_interrupt_once(
    pieces: Sequence[Piece],
    *,
    interrupt_after: int,
    every: int = 2,
    hang_at_end: bool = True,
    await_between_deltas: bool = False,
) -> InterruptResult:
    """Stream ``pieces``, interrupt once the surface saw ``interrupt_after`` deltas."""
    session = Session("interrupt", await_between_deltas=await_between_deltas)
    probe = SourceProbe()
    consumer = SurfaceConsumer(session)
    consuming = asyncio.create_task(consumer.run())
    source = scripted_stream(probe, pieces, every=every, hang_at_end=hang_at_end)
    turn = session.start_turn("hello", source)
    await consumer.seen(interrupt_after)
    assert session.interrupt(session.generation)
    record = await turn
    await consuming
    return _interrupt_result(session, record, probe)


async def run_stubborn_control(clock: FakeClock, hold_seconds: float = 1.0) -> InterruptResult:
    """Negative control on virtual time: the source holds cancellation for ``hold_seconds``."""

    def now_ns() -> int:
        return round(clock.monotonic() * 1e9)

    session = Session("control", now_ns=now_ns)
    probe = SourceProbe()
    consumer = SurfaceConsumer(session)
    consuming = asyncio.create_task(consumer.run())
    source = stubborn_stream(probe, clock, hold_seconds=hold_seconds, now_ns=now_ns)
    turn = session.start_turn("hello", source)
    await consumer.seen(5)
    assert session.interrupt(session.generation)
    await clock.run_until_idle()
    if turn.done():
        raise AssertionError("the stubborn source unwound without holding")
    clock.advance(hold_seconds)
    record = await turn
    await consuming
    return _interrupt_result(session, record, probe)


@dataclass(frozen=True)
class FanoutResult:
    sessions: int
    deltas_per_session: int
    latencies_ms: tuple[float, ...]
    wall_ms: float

    @property
    def p50(self) -> float:
        return percentile(self.latencies_ms, 50)

    @property
    def p99(self) -> float:
        return percentile(self.latencies_ms, 99)

    @property
    def max(self) -> float:
        return max(self.latencies_ms)


async def run_fanout(sessions: int = 50, deltas: int = 200, every: int = 2) -> FanoutResult:
    """``sessions`` turns streaming ``deltas`` each, all in this loop, one consumer per session."""
    started = time.perf_counter_ns()
    consumers: list[SurfaceConsumer] = []
    tasks: list[asyncio.Task[object]] = []
    for i in range(sessions):
        session = Session(f"s{i}")
        consumer = SurfaceConsumer(session)
        consumers.append(consumer)
        tasks.append(asyncio.create_task(consumer.run()))
        probe = SourceProbe()
        tasks.append(
            session.start_turn("go", scripted_stream(probe, text_pieces(deltas), every=every))
        )
    await asyncio.gather(*tasks)
    wall_ms = (time.perf_counter_ns() - started) / 1e6
    latencies = tuple(ns / 1e6 for c in consumers for ns in c.latencies_ns)
    return FanoutResult(sessions, deltas, latencies, wall_ms)
