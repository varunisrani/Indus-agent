"""A harness for running real turns against the fake model (P04-13..19 tests).

``TurnKit`` wires the pieces a turn needs: a fake clock, a scope, a ``ToolRegistry`` with the
``FakeTools``, a ``FakeModelServer`` playing a script, the real P03 caller (``RouteCaller``), a
stub prompt builder and a transcript store. ``await kit.turn("hi")`` runs one turn to its end
while the driver moves the fake clock whenever the turn is waiting, and returns the
``TurnResult``. The runtime is kept between turns like the Agent would.

``check_alternation(rows)`` is the validator every exit path must pass.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from indus.core.loop import run_turn
from indus.core.model_call import RouteCaller
from indus.core.persist import MemoryStore, TranscriptStore
from indus.core.prompt.builder import BuildContext, PromptParts
from indus.core.prompt.lifecycle import PromptLifecycle
from indus.core.transcript import TranscriptRow
from indus.core.turn_state import LoopOptions, SessionRuntime, TurnResult, TurnState, new_turn_state
from indus.foundation.blocking import awaited_calls
from indus.foundation.scope import Scope
from indus.protocol.events import EventBase
from indus.tools.definitions import definitions
from indus.tools.registry import ToolRegistry
from tests.fakes.clock import FakeClock
from tests.fakes.model import FakeModelServer, Script, Turn, fake_route
from tests.fakes.tools import FakeBroker, FakeTools

PROMPT = "You are the test agent."


class StubBuilder:
    def __init__(self, text: str = PROMPT) -> None:
        self.text = text
        self.calls = 0

    async def build(self, ctx: BuildContext) -> PromptParts:
        self.calls += 1
        return PromptParts(stable=self.text, context="", volatile="", text=self.text)


@dataclass
class TurnKit:
    scope: Scope
    clock: FakeClock
    turns: Sequence[Turn]
    store: TranscriptStore | None = None
    options: LoopOptions = field(default_factory=LoopOptions)
    session_id: str = "s1"
    selection: Sequence[str] | None = None
    cwd: Path | None = None
    step: float = 1.0
    events: list[EventBase] = field(default_factory=list[EventBase])
    runtime: SessionRuntime | None = None
    generation: int = 0
    last_state: TurnState | None = None

    def __post_init__(self) -> None:
        self.server = FakeModelServer(Script(tuple(self.turns), name="kit"), clock=self.clock)
        self.broker = FakeBroker(self.clock)
        self.tools = FakeTools(self.clock, broker=self.broker)
        self.registry = self.tools.register(ToolRegistry(clock=self.clock))
        if self.store is None:
            self.store = MemoryStore()

    def build_runtime(self, caller: RouteCaller) -> SessionRuntime:
        if self.runtime is not None:
            self.runtime.caller = caller
            return self.runtime
        names = list(self.selection) if self.selection is not None else self.registry.names()
        ctx = BuildContext(
            scope=self.scope, model=caller.model, provider="fake", platform="cli", cwd=self.cwd
        )
        self.runtime = SessionRuntime(
            session_id=self.session_id,
            caller=caller,
            clock=self.clock,
            prompts=PromptLifecycle(StubBuilder()),
            prompt_context=ctx,
            store=self.store,
            registry=self.registry,
            tools=definitions(self.registry, names, scope=self.scope),
            scope=self.scope,
            options=self.options,
            source="cli",
        )
        return self.runtime

    async def turn(
        self,
        message: Any,
        *,
        history: Sequence[TranscriptRow] | None = None,
        persist_text: str | None = None,
        prepare: Callable[[TurnState], object] | None = None,
        on_event: Callable[[EventBase, TurnState], object] | None = None,
    ) -> TurnResult:
        self.generation += 1
        async with self.server.client() as client:
            caller = RouteCaller(client, fake_route(self.server), self.clock)
            runtime = self.build_runtime(caller)
            holder: list[TurnState] = []

            def sink(event: EventBase) -> None:
                self.events.append(event)
                if on_event is not None and holder:
                    on_event(event, holder[0])

            state = new_turn_state(
                runtime,
                message=message,
                turn_id=f"turn-{self.generation}",
                generation=self.generation,
                persist_text=persist_text,
                history=history,
                sink=sink,
                broker=self.broker,
            )
            holder.append(state)
            self.last_state = state
            if prepare is not None:
                prepare(state)
            task = asyncio.ensure_future(run_turn(state))
            await drive(task, self.clock, self.step)
            return task.result()


# Real seconds one uninterrupted hold may last before ``drive`` gives up on a stuck call.
HOLD_LIMIT_S = 30.0


async def drive(task: asyncio.Future[Any], clock: FakeClock, step: float = 1.0) -> None:
    """Let ``task`` run; whenever it waits, move the fake clock by ``step``.

    "Waits" means: it has not finished after half a real millisecond and no off-loop call is in
    flight (``indus.foundation.blocking.awaited_calls``: blocking-pool calls and work under
    ``awaiting_off_loop``). While such a call runs on another thread the loop only looks idle, so
    the clock holds still until the call is back (a thread parked in ``FakeClock.sleep_sync`` does
    not hold it); otherwise every deadline on the clock raced against that work would expire
    within a fraction of a real second on a loaded machine. A hold longer than ``HOLD_LIMIT_S``
    real seconds fails, so a stuck call cannot hang the run.
    """
    loop = asyncio.get_running_loop()
    held_since: float | None = None
    advances = 0
    while advances < 200_000:
        done, _ = await asyncio.wait({task}, timeout=0.0005)
        if done:
            return
        in_flight = awaited_calls()
        if in_flight > clock.sync_sleepers:
            now = loop.time()
            if held_since is None:
                held_since = now
            elif now - held_since > HOLD_LIMIT_S:
                raise AssertionError(
                    f"{in_flight} off-loop call(s) kept the fake clock still for over "
                    f"{HOLD_LIMIT_S:g} real seconds"
                )
            continue
        held_since = None
        clock.advance(step)
        advances += 1
    raise AssertionError("turn never finished")


def check_alternation(rows: Sequence[TranscriptRow], *, user_index: int | None = None) -> None:
    """Raise ``AssertionError`` unless ``rows`` alternate and every call has exactly one result.

    Allowed shapes: no two assistant rows in a row; two user rows in a row only when the second
    is a steer row; tool rows only in the run right after the assistant row that called them,
    answering each of its call ids exactly once. ``user_index`` may follow another user row: a
    turn that ended without a reply (an interrupt) leaves its user row last, the next turn's row
    comes right after it, and the wire sanitizer joins the two.
    """
    live = [r for r in rows if r.role != "system"]
    i = 0
    previous: TranscriptRow | None = None
    while i < len(live):
        row = live[i]
        assert row.role in ("user", "assistant", "tool"), row.role
        if row.role == "tool":
            raise AssertionError(f"tool row {i} answers no call right before it: {row}")
        joined_user = row.role == "user" and (row.is_steer or i == user_index)
        if previous is not None and previous.role == row.role and not joined_user:
            raise AssertionError(f"two {row.role} rows in a row at {i}")
        if row.role == "assistant" and row.tool_calls:
            wanted = Counter(row.call_ids())
            got: Counter[str] = Counter()
            j = i + 1
            while j < len(live) and live[j].role == "tool":
                got[live[j].tool_call_id or ""] += 1
                j += 1
            assert got == wanted, f"calls {dict(wanted)} answered by {dict(got)} at row {i}"
            previous = live[j - 1]
            i = j
            continue
        previous = row
        i += 1
