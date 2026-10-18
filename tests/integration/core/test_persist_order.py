"""Persist order under crashes and store errors, on the real P02 store (P04-16, ACC-A2-1).

Each case runs a turn whose model calls a side-effecting tool and then answers. The store is made
to fail at one write point:

- ``Crash`` (a ``BaseException``) models the process dying there: nothing after it runs. A new
  "process" then opens the same store, resumes the session and finishes a turn;
- an ordinary store error must end the turn ``persistence_failed`` with valid live rows.

Guarantees checked at every point: the user row is on disk before any provider call; the tool's
side effect never happens before its call row is committed and never happens twice; after the
restart the stored transcript alternates and every remaining call has exactly one result.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from indus.core.exit_reasons import ExitReason
from indus.core.persist import StateStore
from indus.core.repair import repair_transcript
from indus.foundation.scope import Scope
from indus.state import registry
from tests.fakes.clock import FakeClock
from tests.fakes.model import text_turn, tool_batch
from tests.fakes.store import Crash, RecordingStore
from tests.fakes.turn import TurnKit, check_alternation

POINTS = ("session", "user", "calls", "tool", "final")
# How many side effects may have happened when the store dies at each point.
EFFECTS_BY_POINT = {"session": 0, "user": 0, "calls": 0, "tool": 1, "final": 1}


@pytest.fixture
def handle(scope: Scope) -> Iterator[registry.StoreHandle]:
    store = registry.open_unshared(scope)
    yield store
    store.close()


def first_kit(scope: Scope, clock: FakeClock, store: RecordingStore) -> TurnKit:
    turns = [tool_batch(("effect", {"tag": "send"})), text_turn("sent it")]
    kit = TurnKit(scope, clock, turns, store=store)
    kit.step = 0.01
    kit.tools.on_effect = lambda marker: (store.shared or []).append(marker)
    return kit


def resume(scope: Scope, clock: FakeClock, handle: registry.StoreHandle) -> TurnKit:
    store = RecordingStore(StateStore(handle))
    kit = TurnKit(scope, clock, [text_turn("resumed fine")], store=store)
    kit.step = 0.01
    return kit


def stored(handle: registry.StoreHandle) -> list[object]:
    return asyncio.run(StateStore(handle).load("s1"))  # type: ignore[return-value]


@pytest.mark.parametrize("point", POINTS)
def test_crash_at_each_write_point_then_restart(
    point: str, scope: Scope, fake_clock: FakeClock, handle: registry.StoreHandle
) -> None:
    order: list[str] = []
    store = RecordingStore(StateStore(handle), shared=order)
    store.fail_at(point, exc=Crash(point))
    kit = first_kit(scope, fake_clock, store)
    with pytest.raises(Crash):
        asyncio.run(kit.turn("please send it"))

    effects = [e for e in order if e.startswith("effect:")]
    assert len(effects) == EFFECTS_BY_POINT[point]
    if effects:
        assert order.index("stored:calls") < order.index(effects[0])
    if point in ("session", "user"):
        assert kit.server.model_requests == []
    else:
        assert order.index("stored:user") == 1  # right after the session row, before any call

    # A new process resumes the session from the store.
    again = resume(scope, fake_clock, handle)
    result = asyncio.run(again.turn("are you done?"))
    assert result.exit_reason is ExitReason.COMPLETED
    assert again.tools.effects == []  # nothing is ever re-run on resume
    rows = stored(handle)
    contents = [getattr(r, "content", None) for r in rows]
    if point not in ("session", "user"):
        assert "please send it" in contents  # the user row survived
    check_alternation(rows, user_index=result.current_turn_user_index)  # type: ignore[arg-type]
    check_alternation(result.rows, user_index=result.current_turn_user_index)
    _, report = repair_transcript(rows)  # type: ignore[arg-type]
    # Only the reply-less first turn's user row may still be joined with the next one.
    assert report.total == report.counts["merged_users"] <= 1, report.counts
    if point == "tool":
        unknown = [r for r in rows if "may or may not" in str(getattr(r, "content", ""))]
        assert len(unknown) == 1  # the model is told the call's outcome is unknown


@pytest.mark.parametrize("point", POINTS)
def test_store_error_at_each_write_point(
    point: str, scope: Scope, fake_clock: FakeClock, handle: registry.StoreHandle
) -> None:
    order: list[str] = []
    store = RecordingStore(StateStore(handle), shared=order)
    store.fail_at(point)
    kit = first_kit(scope, fake_clock, store)
    result = asyncio.run(kit.turn("please send it"))
    assert result.exit_reason is ExitReason.PERSISTENCE_FAILED
    assert result.failure is not None and result.failure.retryable
    effects = [e for e in order if e.startswith("effect:")]
    assert len(effects) == EFFECTS_BY_POINT[point]
    check_alternation(result.rows, user_index=result.current_turn_user_index)
    if point in ("session", "user"):
        assert kit.server.model_requests == []
    again = resume(scope, fake_clock, handle)
    resumed = asyncio.run(again.turn("status?"))
    assert resumed.exit_reason is ExitReason.COMPLETED
    assert again.tools.effects == []
    check_alternation(stored(handle), user_index=resumed.current_turn_user_index)  # type: ignore[arg-type]


def test_interrupted_tool_batch_survives_a_restart(
    scope: Scope, fake_clock: FakeClock, handle: registry.StoreHandle
) -> None:
    store = RecordingStore(StateStore(handle))
    turns = [tool_batch(("slow", {"seconds": 500}), ("effect", {"tag": "never"}))]
    kit = TurnKit(scope, fake_clock, turns, store=store)
    kit.step = 0.5
    from indus.protocol.events import ToolStarted

    def stop(event: object, state: object) -> None:
        if isinstance(event, ToolStarted):
            state.control.interrupt(state.generation)  # type: ignore[attr-defined]

    result = asyncio.run(kit.turn("go", on_event=stop))
    assert result.exit_reason is ExitReason.INTERRUPTED_BY_USER
    assert kit.tools.effects == []
    rows = stored(handle)
    check_alternation(rows)  # type: ignore[arg-type]
    assert sum(1 for r in rows if getattr(r, "role", "") == "tool") == 2
