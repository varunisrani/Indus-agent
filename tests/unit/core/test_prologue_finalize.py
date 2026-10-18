"""Prologue and finalize (P04-16): staging, sidecar, hydration, scaffold strip, guarded steps."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest

from indus.core import finalize as finalize_module
from indus.core.exit_reasons import ExitReason
from indus.core.persist import MemoryStore, StateStore
from indus.core.transcript import TranscriptRow
from indus.core.turn_state import TurnState
from indus.foundation.scope import Scope
from indus.providers.types import ChatRequest
from indus.state import registry
from tests.fakes.clock import FakeClock
from tests.fakes.model import text_turn, tool_batch
from tests.fakes.store import RecordingStore
from tests.fakes.turn import TurnKit, check_alternation


class Watching:
    """Caller wrapper that writes ``call`` into a shared order list."""

    def __init__(self, inner: Any, order: list[str]) -> None:
        self.inner = inner
        self.order = order

    @property
    def model(self) -> str:
        return self.inner.model

    async def call(self, request: ChatRequest, **kw: Any) -> Any:
        self.order.append("call")
        return await self.inner.call(request, **kw)


def test_user_row_is_stored_before_the_first_call(scope: Scope, fake_clock: FakeClock) -> None:
    order: list[str] = []
    store = RecordingStore(MemoryStore(), shared=order)
    k = TurnKit(scope, fake_clock, [text_turn("hi")], store=store)

    def watch(state: TurnState) -> None:
        state.runtime.caller = Watching(state.runtime.caller, order)

    asyncio.run(k.turn("hello", prepare=watch))
    assert order == ["stored:session", "stored:user", "call", "stored:final"]


@pytest.mark.parametrize("point", ["session", "user"])
def test_a_failed_start_write_means_no_provider_call(
    point: str, scope: Scope, fake_clock: FakeClock
) -> None:
    store = RecordingStore(MemoryStore())
    store.fail_at(point)
    k = TurnKit(scope, fake_clock, [text_turn("never")], store=store)
    result = asyncio.run(k.turn("hello"))
    assert result.exit_reason is ExitReason.PERSISTENCE_FAILED
    assert result.failure is not None and result.failure.reason == "persistence"
    assert k.server.model_requests == []


def test_surrogates_are_scrubbed(scope: Scope, fake_clock: FakeClock) -> None:
    k = TurnKit(scope, fake_clock, [text_turn("ok")])
    result = asyncio.run(k.turn("bad \ud800 text", persist_text=None))
    assert result.rows[0].content == "bad � text"
    assert k.server.model_requests[0].body["messages"][-1]["content"] == "bad � text"


def test_persist_text_is_stored_while_the_wire_sees_the_prefix(
    scope: Scope, fake_clock: FakeClock
) -> None:
    k = TurnKit(scope, fake_clock, [text_turn("ok")])
    result = asyncio.run(k.turn("[spoken] hello there", persist_text="hello there"))
    user = result.rows[0]
    assert user.content == "hello there" and user.api_content == "[spoken] hello there"
    assert k.server.model_requests[0].body["messages"][-1]["content"] == "[spoken] hello there"
    assert isinstance(k.store, MemoryStore)
    assert k.store.rows["s1"][0].content == "hello there"


def test_notes_ride_the_user_message_only(scope: Scope, fake_clock: FakeClock) -> None:
    k = TurnKit(scope, fake_clock, [text_turn("ok"), text_turn("again")])

    def stage(state: TurnState) -> None:
        state.runtime.prompts.notes.stage_must_deliver("[note: the skills changed]")

    first = asyncio.run(k.turn("hi", prepare=stage))
    assert first.rows[0].content == "hi"
    assert first.rows[0].api_content == "hi\n\n[note: the skills changed]"
    sent = k.server.model_requests[0].body["messages"]
    assert sent[0]["content"] == "You are the test agent."
    assert sent[-1]["content"].endswith("[note: the skills changed]")
    second = asyncio.run(k.turn("next", history=first.rows))
    assert second.rows[-2].api_content is None  # one-shot


def test_hydrators_run_once_on_the_first_resumed_turn(scope: Scope, fake_clock: FakeClock) -> None:
    seen: list[int] = []

    def hydrate(rows: Sequence[TranscriptRow]) -> None:
        seen.append(len(rows))

    history = [
        TranscriptRow(role="user", content="old"),
        TranscriptRow(role="assistant", content="ok"),
    ]
    k = TurnKit(scope, fake_clock, [text_turn("a"), text_turn("b")])

    def prepare(state: TurnState) -> None:
        state.runtime.hydrators = {"todo": hydrate}

    first = asyncio.run(k.turn("one", history=history, prepare=prepare))
    asyncio.run(k.turn("two", history=first.rows, prepare=prepare))
    assert seen == [2]


def test_scaffold_rows_never_reach_the_store(scope: Scope, fake_clock: FakeClock) -> None:
    store = RecordingStore(MemoryStore())
    turns = [text_turn("half ", finish="length"), text_turn("and whole")]
    k = TurnKit(scope, fake_clock, turns, store=store)
    result = asyncio.run(k.turn("q"))
    assert store.log == ["session", "user", "final"]
    assert all(not r.scaffold for r in result.rows)
    assert isinstance(store.inner, MemoryStore)
    assert [r.content for r in store.inner.rows["s1"]] == ["q", "half and whole"]


def test_reasoning_is_taken_from_this_turn_only(scope: Scope, fake_clock: FakeClock) -> None:
    history = [
        TranscriptRow(role="user", content="old"),
        TranscriptRow(role="assistant", content="ok", reasoning="old thoughts"),
    ]
    k = TurnKit(scope, fake_clock, [text_turn("new")])
    result = asyncio.run(k.turn("q", history=history))
    assert result.reasoning is None
    assert result.current_turn_user_index == 2


def test_repaired_history_is_written_back(scope: Scope, fake_clock: FakeClock) -> None:
    inner = MemoryStore()
    broken = [
        TranscriptRow(role="user", content="one"),
        TranscriptRow(role="user", content="two"),
        TranscriptRow(
            role="assistant",
            tool_calls=[
                {"id": "x", "type": "function", "function": {"name": "echo", "arguments": "{}"}}
            ],
        ),
        TranscriptRow(role="user", content="later"),
    ]
    for row in broken:
        asyncio.run(inner.append("s1", row))
    store = RecordingStore(inner)
    k = TurnKit(scope, fake_clock, [text_turn("fresh")], store=store)
    result = asyncio.run(k.turn("three"))
    assert "replace" in store.log
    check_alternation(inner.rows["s1"], user_index=1)
    sent = k.server.model_requests[0].body["messages"]
    assert [m["role"] for m in sent] == ["system", "user"]
    assert sent[1]["content"] == "one\n\ntwo\n\nlater\n\nthree"
    assert [r.content for r in inner.rows["s1"]] == ["one\n\ntwo\n\nlater", "three", "fresh"]
    assert result.rows[result.current_turn_user_index].content == "three"


def test_a_failing_finalize_step_keeps_the_answer(
    scope: Scope, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(state: TurnState) -> None:
        raise RuntimeError("tail broke")

    monkeypatch.setattr(finalize_module, "_close_tail", broken)
    k = TurnKit(scope, fake_clock, [text_turn("the answer")])
    result = asyncio.run(k.turn("q"))
    assert result.final_text == "the answer"
    assert result.exit_reason is ExitReason.COMPLETED
    assert result.cleanup_errors and result.cleanup_errors[0].startswith("close_tail")


def test_stream_text_recovers_an_empty_final(scope: Scope, fake_clock: FakeClock) -> None:
    k = TurnKit(scope, fake_clock, [text_turn("x")])
    asyncio.run(k.turn("q"))
    state = k.last_state
    assert state is not None
    state.exit, state.final_text, state.failure = None, None, None
    state.control.phase = "model"
    state.streamed_text = "shown already"
    result = asyncio.run(finalize_module.finalize_turn(state))
    assert result.final_text == "shown already"
    assert result.exit_reason is ExitReason.COMPLETED
    assert result.rows[-1].content == "shown already"


def test_real_store_round_trip(scope: Scope, fake_clock: FakeClock) -> None:
    handle = registry.open_unshared(scope)
    try:
        store = StateStore(handle)
        k = TurnKit(
            scope, fake_clock, [tool_batch(("echo", {"text": "a"})), text_turn("done")], store=store
        )
        k.step = 0.01
        result = asyncio.run(k.turn("hello"))
        assert result.exit_reason is ExitReason.COMPLETED
        loaded = asyncio.run(store.load("s1"))
        assert [r.role for r in loaded] == ["user", "assistant", "tool", "assistant"]
        check_alternation(loaded)
        from indus.state import sessions

        row = sessions.get(handle, "s1")
        assert row is not None and row["system_prompt"] == "You are the test agent."
    finally:
        handle.close()
