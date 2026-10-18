"""Interrupt, steer and redirect (P04-19)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from indus.core import loop_texts as texts
from indus.core.exit_reasons import ExitReason
from indus.core.interrupts import DISPLAY_HIDDEN, TurnControl, redirect_rows, steer_row
from indus.core.persist import MemoryStore
from indus.core.prompt.texts import STEER_OPEN
from indus.core.turn_state import TurnState
from indus.foundation.scope import Scope
from indus.protocol.events import CancelReason, EventBase, MessageDelta, ToolStarted
from tests.fakes.clock import FakeClock, checkpoint
from tests.fakes.model import Finish, Reasoning, Stall, Text, Turn, text_turn, tool_batch
from tests.fakes.store import RecordingStore
from tests.fakes.turn import TurnKit, check_alternation


def stalled(*chunks: str, reasoning: str | None = None) -> Turn:
    steps: list[object] = []
    if reasoning:
        steps.append(Reasoning((reasoning,)))
    if chunks:
        steps.append(Text(tuple(chunks)))
    steps += [Stall(1000.0), Finish("stop")]
    return Turn(tuple(steps))  # type: ignore[arg-type]


def on_first(kind: type[EventBase], action: Callable[[TurnState], object]):
    fired: list[bool] = []

    def hook(event: EventBase, state: TurnState) -> None:
        if isinstance(event, kind) and not fired:
            fired.append(True)
            asyncio.get_running_loop().call_soon(action, state)

    return hook


# -- interrupt -------------------------------------------------------------------------------------


def test_interrupt_mid_stream_closes_the_stream_quickly(
    scope: Scope, fake_clock: FakeClock
) -> None:
    store = RecordingStore(MemoryStore())
    k = TurnKit(scope, fake_clock, [stalled("Partial ", "answer")], store=store)
    stamps: list[float] = []

    def stop(state: TurnState) -> None:
        stamps.append(time.perf_counter())
        assert state.control.interrupt(state.generation)

    result = asyncio.run(k.turn("tell me", on_event=on_first(MessageDelta, stop)))
    assert result.exit_reason is ExitReason.INTERRUPTED_BY_USER
    record = k.server.streams[0]
    assert record.closed_at is not None and not record.completed
    assert record.closed_at - stamps[0] < 0.2
    assert fake_clock.monotonic() < 1000.0
    assert [r.role for r in result.rows] == ["user", "assistant"]
    assert result.rows[-1].content == "Partial answer"
    assert result.final_text == "Partial answer"
    check_alternation(result.rows)
    assert store.log == ["session", "user", "final"]
    assert k.broker.cancelled[0] is CancelReason.INTERRUPTED


def test_stale_generation_is_ignored(scope: Scope, fake_clock: FakeClock) -> None:
    k = TurnKit(scope, fake_clock, [text_turn("done")])
    seen: list[bool] = []

    def old(state: TurnState) -> None:
        seen.append(state.control.interrupt(state.generation - 1))

    result = asyncio.run(k.turn("hi", on_event=on_first(MessageDelta, old)))
    assert seen == [False]
    assert result.exit_reason is ExitReason.COMPLETED


def test_interrupt_during_tools_cancels_running_and_skips_the_rest(
    scope: Scope, fake_clock: FakeClock
) -> None:
    turns = [tool_batch(("slow", {"seconds": 500}), ("effect", {"tag": "later"}))]
    k = TurnKit(scope, fake_clock, turns)

    def stop(state: TurnState) -> None:
        state.control.interrupt(state.generation)

    result = asyncio.run(k.turn("go", on_event=on_first(ToolStarted, stop)))
    assert result.exit_reason is ExitReason.INTERRUPTED_BY_USER
    first, second = (r for r in result.rows if r.role == "tool")
    assert "cancelled" in str(first.content) and "skipped" in str(second.content)
    assert k.tools.effects == []
    check_alternation(result.rows)
    assert isinstance(k.store, MemoryStore)
    check_alternation(k.store.rows["s1"])


def test_interrupt_before_the_first_call(scope: Scope, fake_clock: FakeClock) -> None:
    k = TurnKit(scope, fake_clock, [text_turn("never")])

    def early(state: TurnState) -> None:
        state.control.interrupt(state.generation)

    result = asyncio.run(k.turn("hi", prepare=early))
    assert result.exit_reason is ExitReason.INTERRUPTED_BY_USER
    assert k.server.model_requests == []
    assert [r.role for r in result.rows] == ["user"]


# -- steer -----------------------------------------------------------------------------------------


def test_steer_during_tools_becomes_its_own_row_after_the_results(
    scope: Scope, fake_clock: FakeClock
) -> None:
    store = RecordingStore(MemoryStore())
    turns = [tool_batch(("slow", {"seconds": 5}), ("echo", {})), text_turn("noted")]
    k = TurnKit(scope, fake_clock, turns, store=store)

    def steer(state: TurnState) -> None:
        assert state.control.steer("also check the logs")

    result = asyncio.run(k.turn("go", on_event=on_first(ToolStarted, steer)))
    assert result.exit_reason is ExitReason.COMPLETED
    roles = [(r.role, r.is_steer) for r in result.rows]
    assert roles == [
        ("user", False),
        ("assistant", False),
        ("tool", False),
        ("tool", False),
        ("user", True),
        ("assistant", False),
    ]
    steer_text = str(result.rows[4].content)
    assert steer_text.startswith(STEER_OPEN) and "also check the logs" in steer_text
    assert all("logs" not in str(r.content) for r in result.rows if r.role == "tool")
    assert store.log == ["session", "user", "calls", "tool", "tool", "steer", "final"]
    sent = k.server.model_requests[1].body["messages"]
    assert sent[-1]["role"] == "user" and "also check the logs" in sent[-1]["content"]
    check_alternation(result.rows)
    reloaded = asyncio.run(store.load("s1"))
    assert reloaded[4].is_steer


def test_steer_during_a_text_answer_is_left_for_the_next_turn(
    scope: Scope, fake_clock: FakeClock
) -> None:
    k = TurnKit(scope, fake_clock, [text_turn("an ", "answer")])

    def steer(state: TurnState) -> None:
        state.control.steer("one more thing")

    result = asyncio.run(k.turn("hi", on_event=on_first(MessageDelta, steer)))
    assert result.exit_reason is ExitReason.COMPLETED
    assert result.pending_steer == "one more thing"
    assert all(not r.is_steer for r in result.rows)


def test_hard_interrupt_drops_steer_ordinary_keeps_it(scope: Scope, fake_clock: FakeClock) -> None:
    for hard, expected in ((True, None), (False, "keep this")):
        k = TurnKit(scope, fake_clock, [tool_batch(("slow", {"seconds": 50}))])

        def act(state: TurnState, hard: bool = hard) -> None:
            state.control.steer("keep this")
            state.control.interrupt(state.generation, hard=hard)
            assert state.control.steer("too late") is (not hard)

        result = asyncio.run(k.turn("go", on_event=on_first(ToolStarted, act)))
        assert result.exit_reason is ExitReason.INTERRUPTED_BY_USER
        expect = expected if not expected else "keep this\ntoo late"
        assert result.pending_steer == expect
        check_alternation(result.rows)


def test_steer_buffer_and_tickets() -> None:
    control = TurnControl(generation=1)
    assert not control.steer("   ")
    control.steer("a", ticket="t1")
    control.steer("b")
    control.steer("c", ticket="t2")
    assert control.withdraw_steer("t1") is True
    assert control.withdraw_steer("t1") is False  # unknown now
    assert control.drain_steer() == "b\nc"
    assert control.withdraw_steer("t2") is False  # consumed by the drain
    control.steer("d", ticket="t3")
    control.steer("e")
    assert control.leftover_steer() == "e"
    assert control.withdraw_steer("t3") is True
    control.steer("f", ticket="t4")
    control.interrupt(1, hard=True)
    assert control.withdraw_steer("t4") is True  # dropped unread
    assert control.leftover_steer() is None


def test_concurrent_steers_keep_all_text(scope: Scope, fake_clock: FakeClock) -> None:
    control = TurnControl()
    for n in range(20):
        control.steer(f"note {n}")
    drained = control.drain_steer() or ""
    assert drained.splitlines() == [f"note {n}" for n in range(20)]


# -- redirect --------------------------------------------------------------------------------------


def test_redirect_keeps_partial_text_and_never_stores_reasoning(
    scope: Scope, fake_clock: FakeClock
) -> None:
    turns = [stalled("Bonjour, je ", reasoning="secret chain of thought"), text_turn("Hello!")]
    k = TurnKit(scope, fake_clock, turns)

    def correct(state: TurnState) -> None:
        assert state.control.redirect("answer in English")

    result = asyncio.run(k.turn("greet me", on_event=on_first(MessageDelta, correct)))
    assert result.exit_reason is ExitReason.COMPLETED and result.final_text == "Hello!"
    roles = [r.role for r in result.rows]
    assert roles == ["user", "assistant", "user", "assistant"]
    checkpoint_row, correction = result.rows[1], result.rows[2]
    assert checkpoint_row.content == "Bonjour, je"
    assert correction.content == "answer in English"
    assert correction.api_content is not None
    assert texts.REDIRECT_NOTE in correction.api_content
    assert "Bonjour, je" in correction.api_content
    assert isinstance(k.store, MemoryStore)
    for row in k.store.rows["s1"]:
        assert "secret chain" not in f"{row.content}{row.api_content}{row.reasoning}"
    sent = k.server.model_requests[1].body["messages"]
    assert sent[-1]["content"] == correction.api_content
    assert result.api_calls == 1 and k.last_state is not None
    assert k.last_state.restart_count == 1
    check_alternation(result.rows)


def test_redirect_before_any_text_uses_a_hidden_placeholder(
    scope: Scope, fake_clock: FakeClock
) -> None:
    k = TurnKit(scope, fake_clock, [stalled(), text_turn("ok")])

    async def watch(state: TurnState) -> None:
        while state.control.phase != "model" or state.control._model_task is None:  # pyright: ignore[reportPrivateUsage]
            await checkpoint()
        assert state.control.redirect("first")
        assert state.control.redirect("second")

    def prepare(state: TurnState) -> None:
        asyncio.get_running_loop().create_task(watch(state))

    result = asyncio.run(k.turn("hi", prepare=prepare))
    placeholder, correction = result.rows[1], result.rows[2]
    assert placeholder.display_kind == DISPLAY_HIDDEN and placeholder.content == ""
    assert placeholder.api_content == texts.REDIRECT_PLACEHOLDER
    assert correction.api_content is not None
    assert texts.REDIRECT_MORE in correction.api_content
    assert correction.content == f"first\n\n{texts.REDIRECT_MORE}\nsecond"
    check_alternation(result.rows)


def test_redirect_during_tools_becomes_steer(scope: Scope, fake_clock: FakeClock) -> None:
    turns = [tool_batch(("slow", {"seconds": 5})), text_turn("ok")]
    k = TurnKit(scope, fake_clock, turns)

    def correct(state: TurnState) -> None:
        assert state.control.redirect("change of plan")

    result = asyncio.run(k.turn("go", on_event=on_first(ToolStarted, correct)))
    steer = next(r for r in result.rows if r.is_steer)
    assert "change of plan" in str(steer.content)
    assert result.exit_reason is ExitReason.COMPLETED


def test_redirect_without_a_live_call_is_refused(scope: Scope, fake_clock: FakeClock) -> None:
    control = TurnControl()
    assert control.redirect("x") is False
    k = TurnKit(scope, fake_clock, [text_turn("done")])
    asyncio.run(k.turn("hi"))
    assert k.last_state is not None and k.last_state.control.redirect("late") is False
    stopped = TurnControl(generation=3, phase="model")
    stopped.interrupt(3, hard=True)
    assert stopped.redirect("after stop") is False


def test_redirect_rows_shapes() -> None:
    looping = "the same words again " * 60
    rows = redirect_rows(looping, "stop that", tail_role="user")
    assert rows[0].display_kind == DISPLAY_HIDDEN
    assert rows[1].api_content is not None and texts.REDIRECT_LOOPED in rows[1].api_content
    assert "the same words again" not in rows[1].api_content
    only_user = redirect_rows("shown", "fix", tail_role="assistant")
    assert [r.role for r in only_user] == ["user"]
    assert steer_row("x").is_steer


def test_steer_accepted_before_a_redirect_lands_before_the_correction(
    scope: Scope, fake_clock: FakeClock
) -> None:
    k = TurnKit(scope, fake_clock, [stalled("Partial"), text_turn("ok")])

    def both(state: TurnState) -> None:
        state.control.steer("earlier note")
        state.control.redirect("the correction")

    result = asyncio.run(k.turn("go", on_event=on_first(MessageDelta, both)))
    kinds = [(r.role, r.is_steer) for r in result.rows]
    assert kinds == [
        ("user", False),
        ("user", True),
        ("assistant", False),
        ("user", False),
        ("assistant", False),
    ]
    assert result.pending_steer is None
    check_alternation(result.rows)
