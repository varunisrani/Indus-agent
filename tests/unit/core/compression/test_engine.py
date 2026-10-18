"""The compression engine, its gates and its loop wiring (P08-05, R01.9)."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable, Sequence
from typing import Any

from indus.core.compression import texts
from indus.core.compression.anchor import TokenMeter
from indus.core.compression.cooldown import CooldownBook, MemoryCounters
from indus.core.compression.engine import (
    BuiltinEngine,
    CompressRequest,
    Outcome,
    build_engine,
)
from indus.core.compression.threshold import CompressionSettings
from indus.core.exit_reasons import ExitReason
from indus.core.persist import StateStore
from indus.core.transcript import TranscriptRow
from indus.core.turn_state import TurnState
from indus.foundation.scope import Scope
from indus.protocol.events import Notice, Status, StatusKind
from indus.providers.types import Usage
from indus.state import messages, registry
from tests.fakes.agent import run_driven
from tests.fakes.aux_model import FakeAuxModel, Stall, overloaded
from tests.fakes.clock import FakeClock
from tests.fakes.model import text_turn
from tests.fakes.turn import StubBuilder, TurnKit, check_alternation
from tests.unit.core.compression._transcripts import conversation

WINDOW = 64_000  # trigger 54,400


def make(
    clock: FakeClock, *steps: Any, window: int = WINDOW, **settings: Any
) -> tuple[BuiltinEngine, FakeAuxModel]:
    aux = FakeAuxModel(*steps, clock=clock)
    chosen = CompressionSettings(**settings)
    engine = BuiltinEngine(
        settings=chosen,
        meter=TokenMeter(),
        book=CooldownBook(
            MemoryCounters(),
            clock,
            timeout_s=chosen.timeout_s,
            abort_on_failure=chosen.abort_on_summary_failure,
        ),
        clock=clock,
        summarizer=aux,
        window=window,
        model="test-model",
    )
    return engine, aux


class Commits:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(
        self, new: list[TranscriptRow], held: list[TranscriptRow]
    ) -> list[TranscriptRow]:
        self.calls += 1
        return [r.clone() for r in new]


def big_rows(seed: int = 1, exchanges: int = 30) -> list[TranscriptRow]:
    return conversation(random.Random(seed), exchanges, big=20_000)


def run(coro: Any, clock: FakeClock | None = None) -> Any:
    if clock is None:
        return asyncio.run(coro)
    return asyncio.run(run_driven(coro, clock, step=30.0))


async def anchor_over(engine: BuiltinEngine, rows: Sequence[TranscriptRow], tokens: int) -> None:
    await engine.update_from_response(Usage(prompt_tokens=tokens, completion_tokens=10), rows)


def is_handoff(row: TranscriptRow) -> bool:
    return isinstance(row.content, str) and row.content.startswith(texts.SUMMARY_OPEN)


# -- compress --------------------------------------------------------------------------------------


def test_compress_produces_a_valid_shorter_transcript(fake_clock: FakeClock) -> None:
    engine, aux = make(fake_clock)
    rows = big_rows()
    commit = Commits()
    outcome: Outcome = run(engine.compress(rows, CompressRequest(), commit))
    assert outcome.status == "compressed" and not outcome.fallback
    assert commit.calls == 1 and len(aux.prompts) == 1
    assert outcome.rows[0].role == "system"
    assert sum(1 for r in outcome.rows if is_handoff(r)) == 1
    check_alternation(outcome.rows)
    assert outcome.after < outcome.before
    newest_user = [r for r in rows if r.role == "user"][-1]
    assert any(r.content == newest_user.content for r in outcome.rows)
    assert engine.compactions == 1 and engine.attempts == 1
    assert engine.meter.anchor is None and engine.meter.awaiting_real


def test_property_alternation_after_assemble(fake_clock: FakeClock) -> None:
    rng = random.Random(4242)
    for round_ in range(60):
        rows = conversation(
            rng,
            rng.randint(3, 25),
            big=rng.choice([300, 5_000, 20_000]),
            pending=rng.random() < 0.3,
        )
        steps: list[Any] = [rng.choice(["## Goal\nnotes", RuntimeError("boom")])]
        engine, _ = make(
            fake_clock,
            *steps,
            window=rng.choice([32_000, 64_000, 200_000]),
            tail_mode=rng.choice(["lean", "legacy"]),
            min_tail_user_messages=rng.randint(0, 2),
        )
        keep = rng.choice([None, None, rng.randint(1, 6)])
        outcome = run(engine.compress(rows, CompressRequest(keep_last=keep), Commits()))
        assert outcome.status in ("compressed", "nothing"), (round_, outcome)
        if outcome.status == "compressed":
            check_alternation(outcome.rows)
            again = run(engine.compress(outcome.rows, CompressRequest(gate="manual"), Commits()))
            if again.status == "compressed":
                check_alternation(again.rows)


def test_second_compaction_updates_the_notes(fake_clock: FakeClock) -> None:
    engine, aux = make(fake_clock, "## Goal\nfirst notes", "## Goal\nsecond notes")
    first = run(engine.compress(big_rows(), CompressRequest(), Commits()))
    grown = first.rows + big_rows(seed=2, exchanges=20)[1:]
    second = run(engine.compress(grown, CompressRequest(gate="manual"), Commits()))
    assert second.status == "compressed"
    assert "first notes" in aux.prompts[1] and texts.UPDATE_TASK in aux.prompts[1]
    assert sum(1 for r in second.rows if is_handoff(r)) == 1


def test_preview_and_nothing_to_compress(fake_clock: FakeClock) -> None:
    engine, aux = make(fake_clock)
    preview = run(
        engine.compress(big_rows(), CompressRequest(gate="manual", preview=True), Commits())
    )
    assert preview.status == "preview" and preview.condensed > 0 and not aux.prompts
    tiny = conversation(random.Random(1), 1)
    nothing = run(engine.compress(tiny, CompressRequest(gate="manual"), Commits()))
    assert nothing.status == "nothing" and nothing.reason == texts.NOTHING_TO_COMPRESS
    assert engine.attempts == 0


def test_todo_and_focus_reach_the_handoff(fake_clock: FakeClock) -> None:
    engine, aux = make(fake_clock)
    engine.todo = lambda: "[Task list carried over from before]\n- [ ] t1: finish (pending)"
    outcome = run(
        engine.compress(big_rows(), CompressRequest(gate="manual", focus="the parser"), Commits())
    )
    handoff = next(r for r in outcome.rows if is_handoff(r))
    assert "t1: finish" in str(handoff.content)
    assert 'Focus: "the parser"' in aux.prompts[0]


# -- gates -----------------------------------------------------------------------------------------


def test_estimates_wait_and_anchored_figures_compress(fake_clock: FakeClock) -> None:
    engine, _ = make(fake_clock)
    rows = big_rows()
    verdict = run(engine.should_compress(rows, gate="preflight"))
    assert verdict.action == "wait"  # an estimate is not evidence (R01.8)
    run(anchor_over(engine, rows, 60_000))
    assert run(engine.should_compress(rows, gate="pressure")).action == "compress"
    run(anchor_over(engine, rows, 20_000))
    assert run(engine.should_compress(rows, gate="pressure")).action == "ok"


def test_attempt_cap_and_rearm(fake_clock: FakeClock) -> None:
    engine, _ = make(fake_clock, max_attempts=1)
    engine.begin_turn()
    rows = big_rows()
    first = run(engine.compress(rows, CompressRequest(), Commits()))
    assert first.status == "compressed" and engine.attempts == 1
    grown = first.rows + big_rows(seed=3, exchanges=15)[1:]
    run(anchor_over(engine, first.rows, 70_000))  # still over: ineffective, no re-arm
    blocked = run(engine.should_compress(grown, gate="pressure"))
    assert blocked.action == "blocked" and "attempts" in blocked.reason
    run(anchor_over(engine, first.rows, 20_000))  # a real reading under the trigger re-arms
    assert engine.attempts == 0
    assert run(engine.should_compress(grown, gate="pressure")).action == "compress"


def test_breaker_after_two_ineffective(fake_clock: FakeClock) -> None:
    engine, _ = make(fake_clock, max_attempts=10)
    rows = big_rows()
    for _ in range(2):
        outcome = run(engine.compress(rows, CompressRequest(), Commits()))
        assert outcome.status == "compressed"
        run(anchor_over(engine, outcome.rows, 80_000))  # the provider says: still over
        rows = outcome.rows + big_rows(seed=5, exchanges=10)[1:]
    assert run(engine.book.read()).ineffective_count == 2
    blocked = run(engine.should_compress(rows, gate="pressure"))
    assert blocked.action == "blocked" and "did not help" in blocked.reason
    fake_clock.advance(299)
    assert run(engine.should_compress(rows, gate="pressure")).action == "blocked"
    fake_clock.advance(2)
    assert run(engine.should_compress(rows, gate="pressure")).action == "compress"  # one probe
    assert run(engine.book.read()).ineffective_count == 1


def test_manual_compress_clears_the_cooldown(fake_clock: FakeClock) -> None:
    engine, _ = make(fake_clock, overloaded(), "## Goal\nnotes")
    rows = big_rows()
    first = run(engine.compress(rows, CompressRequest(), Commits()))
    assert first.status == "aborted" and first.failure_kind == "overload"
    run(anchor_over(engine, rows, 60_000))
    blocked = run(engine.should_compress(rows, gate="pressure"))
    assert blocked.action == "blocked" and "cooldown" in blocked.reason
    manual = run(engine.compress(rows, CompressRequest(gate="manual"), Commits()))
    assert manual.status == "compressed"
    assert run(engine.book.remaining()) == 0.0


def test_repeated_stall_commits_the_fallback(fake_clock: FakeClock) -> None:
    engine, aux = make(fake_clock, Stall(), Stall())
    rows = big_rows()
    first = run(engine.compress(rows, CompressRequest(), Commits()), fake_clock)
    assert first.status == "aborted" and first.failure_kind == "stall"
    fake_clock.advance(1_000)
    second = run(engine.compress(rows, CompressRequest(), Commits()), fake_clock)
    assert second.status == "compressed" and second.fallback
    handoff = next(r for r in second.rows if is_handoff(r))
    assert texts.USERS_HEADING in str(handoff.content)
    assert len(aux.prompts) == 2


def test_three_overloads_commit_the_fallback(fake_clock: FakeClock) -> None:
    engine, _ = make(fake_clock, overloaded(), overloaded(), overloaded())
    rows = big_rows()
    statuses = []
    for _ in range(3):
        statuses.append(run(engine.compress(rows, CompressRequest(gate="manual"), Commits())))
    assert [o.status for o in statuses] == ["aborted", "aborted", "compressed"]
    assert statuses[-1].fallback


def test_abort_on_summary_failure_keeps_the_transcript(fake_clock: FakeClock) -> None:
    engine, _ = make(fake_clock, RuntimeError("boom"), abort_on_summary_failure=True)
    commit = Commits()
    outcome = run(engine.compress(big_rows(), CompressRequest(), commit))
    assert outcome.status == "aborted" and commit.calls == 0


def test_overflow_bypasses_the_cooldown_once_without_clearing_it(fake_clock: FakeClock) -> None:
    engine, _ = make(fake_clock, overloaded(), "## Goal\nnotes")
    rows = big_rows()
    run(engine.compress(rows, CompressRequest(), Commits()))
    assert run(engine.book.remaining()) > 0
    outcome = run(engine.compress(rows, CompressRequest(gate="overflow"), Commits()))
    assert outcome.status == "compressed"
    assert run(engine.book.remaining()) > 0  # bypassed, not cleared (R01.8)


def test_store_failure_aborts_and_refunds(fake_clock: FakeClock) -> None:
    engine, _ = make(fake_clock)

    async def refuse(new: list[TranscriptRow], held: list[TranscriptRow]) -> list[TranscriptRow]:
        raise messages.TranscriptMoved("moved")

    outcome = run(engine.compress(big_rows(), CompressRequest(), refuse))
    assert outcome.status == "aborted" and outcome.failure_kind == "store"
    assert engine.attempts == 0 and engine.compactions == 0


def test_build_engine_follows_config(scope: Scope, fake_clock: FakeClock) -> None:
    off = build_engine(
        scope, session_id="s", clock=fake_clock, settings=CompressionSettings(enabled=False)
    )
    assert off is None
    on = build_engine(
        scope,
        session_id="s",
        clock=fake_clock,
        settings=CompressionSettings(engine="someplugin"),
        window=200_000,
        model="m",
        base_url="https://api.example.test/v1",
    )
    assert on is not None and on.name == "builtin" and on.trigger == 150_000
    assert on.meter.image_key == "m@api.example.test"


# -- the loop --------------------------------------------------------------------------------------


def history() -> list[TranscriptRow]:
    return [r for r in big_rows(seed=11, exchanges=25) if r.role != "system"]


Wired = tuple[TurnKit, FakeAuxModel, list[BuiltinEngine], Callable[[TurnState], None]]


def wired_kit(scope: Scope, clock: FakeClock, store: Any = None) -> Wired:
    kit = TurnKit(scope, clock, [text_turn("all done")], store=store)
    kit.step = 0.01
    aux = FakeAuxModel("## Goal\nfinish the refactor", clock=clock)
    holder: list[BuiltinEngine] = []

    def prepare(state: TurnState) -> None:
        if state.runtime.context is None:
            engine = build_engine(
                scope,
                session_id=state.session_id,
                clock=clock,
                store=state.runtime.store,
                summarizer=aux,
                window=WINDOW,
                model="test-model",
            )
            assert engine is not None
            engine.meter.usage_less = True  # this provider never reports usage: estimates decide
            state.runtime.context = engine
            holder.append(engine)

    return kit, aux, holder, prepare


def test_loop_preflight_compresses_and_rebuilds_once(scope: Scope, fake_clock: FakeClock) -> None:
    kit, aux, holder, prepare = wired_kit(scope, fake_clock)
    result = asyncio.run(kit.turn("please continue", history=history(), prepare=prepare))
    assert result.exit_reason is ExitReason.COMPLETED, result.final_text
    assert len(aux.prompts) == 1
    assert holder[0].compactions == 1
    runtime = kit.runtime
    assert runtime is not None
    builder = runtime.prompts._builder  # pyright: ignore[reportPrivateUsage]
    assert isinstance(builder, StubBuilder) and builder.calls == 2  # first build + one rebuild
    check_alternation(result.rows)
    assert sum(1 for r in result.rows if is_handoff(r)) == 1
    assert result.rows[result.current_turn_user_index].content == "please continue"
    kinds = [e.kind for e in kit.events if isinstance(e, Status)]
    assert StatusKind.COMPRESSING in kinds
    notices = [e for e in kit.events if isinstance(e, Notice) and e.key == "session.compressed"]
    assert len(notices) == 1
    stored = asyncio.run(kit.store.load("s1"))  # type: ignore[union-attr]
    assert [r.content for r in stored] == [r.content for r in result.rows]


def test_loop_in_place_archives_compacted_rows(scope: Scope, fake_clock: FakeClock) -> None:
    handle = registry.open_unshared(scope)
    try:
        kit, aux, _, prepare = wired_kit(scope, fake_clock, store=StateStore(handle))
        result = asyncio.run(kit.turn("please continue", history=history(), prepare=prepare))
        assert result.exit_reason is ExitReason.COMPLETED
        assert len(aux.prompts) == 1
        with handle.read() as conn:
            archived = conn.execute(
                "SELECT COUNT(*) FROM messages "
                "WHERE session_id = 's1' AND active = 0 AND compacted = 1"
            ).fetchone()[0]
            live = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = 's1' AND active = 1"
            ).fetchone()[0]
        assert archived > 0
        loaded = asyncio.run(StateStore(handle).load("s1"))
        assert len(loaded) == live
        check_alternation(loaded)
        assert any(is_handoff(r) for r in loaded)
    finally:
        handle.close()


def test_in_place_write_carries_rows_that_arrived_meanwhile(scope: Scope) -> None:
    from indus.state import compression, sessions

    handle = registry.open_unshared(scope)
    try:
        sessions.create(handle, "s1", "cli")
        ids = [
            messages.append(handle, "s1", {"role": role, "content": f"{role} {n}"})
            for n, role in enumerate(["user", "assistant", "user", "assistant", "user"])
        ]
        late = messages.append(handle, "s1", {"role": "assistant", "content": "arrived late"})
        new_rows = [
            {"role": "user", "content": texts.SUMMARY_OPEN + " notes " + texts.SUMMARY_CLOSE},
            {"role": "assistant", "content": "assistant 3"},
            {"role": "user", "content": "user 4"},
        ]
        new_ids = compression.compact_in_place(handle, "s1", new_rows, ids)
        live = messages.get(handle, "s1")
        assert [m["content"] for m in live][-1] == "arrived late"
        assert [int(m["id"]) for m in live] == new_ids and late not in new_ids
        with handle.read() as conn:
            flags = dict(
                conn.execute(
                    "SELECT id, compacted FROM messages WHERE session_id = 's1' AND active = 0"
                ).fetchall()
            )
        assert {i: flags[i] for i in ids} == dict.fromkeys(ids, 1)
        assert flags[late] == 0  # the clone source is hidden, not counted as compacted
        row = sessions.get(handle, "s1")
        assert row is not None and row["message_count"] == 4
        try:
            compression.compact_in_place(handle, "s1", new_rows, ids)
        except messages.TranscriptMoved:
            pass
        else:
            raise AssertionError("a moved transcript must be refused")
    finally:
        handle.close()


def test_loop_rotating_mode_continues_in_a_child(scope: Scope, fake_clock: FakeClock) -> None:
    handle = registry.open_unshared(scope)
    try:
        kit, aux, _, prepare = wired_kit(scope, fake_clock, store=StateStore(handle))

        def rotating(state: TurnState) -> None:
            prepare(state)
            engine = state.runtime.context
            assert isinstance(engine, BuiltinEngine)
            engine.settings = CompressionSettings(in_place=False)

        result = asyncio.run(kit.turn("please continue", history=history(), prepare=rotating))
        assert result.exit_reason is ExitReason.COMPLETED, result.final_text
        assert len(aux.prompts) == 1
        child = result.session_id
        assert child != "s1"
        from indus.state import sessions

        parent = sessions.get(handle, "s1")
        assert parent is not None and parent["end_reason"] == "compression"
        loaded = asyncio.run(StateStore(handle).load(child))
        check_alternation(loaded)
        assert loaded[-1].content == "all done"
    finally:
        handle.close()


def test_agent_manual_compress(scope: Scope, fake_clock: FakeClock) -> None:
    from indus.core.agent import Agent
    from indus.core.persist import MemoryStore
    from tests.fakes.agent import fake_options, run_driven
    from tests.fakes.model import FakeModelServer, Script, fake_route

    server = FakeModelServer(Script((text_turn("ok"),)), clock=fake_clock)
    store = MemoryStore()
    aux = FakeAuxModel("## Goal\nmanual notes", clock=fake_clock)

    async def main() -> tuple[Outcome, Outcome, list[TranscriptRow]]:
        agent = await Agent.start(
            scope, fake_route(server), [], fake_options(server, store=store, summarizer=aux)
        )
        await store.ensure_session("s1", source="cli", system_prompt="p", fields={})
        for row in history():
            await store.append("s1", row)
        preview = await agent.compress(preview=True)
        done = await agent.compress("the refactor", keep_last=4)
        await agent.close()
        return preview, done, await store.load("s1")

    preview, done, stored = asyncio.run(run_driven(main(), fake_clock))
    assert preview.status == "preview" and preview.condensed > 0
    assert done.status == "compressed"
    assert len(aux.prompts) == 1 and 'Focus: "the refactor"' in aux.prompts[0]
    assert sum(1 for r in stored if is_handoff(r)) == 1
    check_alternation(stored)


class SmallWindowAux:
    """The ``AuxClient`` shape (P08-17): ``call`` + async ``window_for(task, main_window)``."""

    async def call(self, task: str, messages: Any) -> Any:
        return "## Goal\nnotes"

    async def window_for(self, task: str, main_window: int) -> int:
        return 32_000


def test_smaller_aux_window_lowers_the_trigger(scope: Scope, fake_clock: FakeClock) -> None:
    from indus.core.compression.summarize import AuxSummarizer

    engine = build_engine(
        scope,
        session_id="s",
        clock=fake_clock,
        summarizer=AuxSummarizer(SmallWindowAux()),
        window=200_000,
        model="m",
    )
    assert engine is not None
    run(engine.on_session_start([]))  # the window is asked once, when the session starts
    assert engine.trigger == 32_000
    engine.set_route(window=1_000_000, max_output=None, model="m2", provider="p")
    assert engine.trigger == 32_000  # the summariser's window stays the ceiling


def test_route_change_clears_the_old_routes_cooldown(fake_clock: FakeClock) -> None:
    engine, _ = make(fake_clock, overloaded())
    rows = big_rows()
    run(engine.compress(rows, CompressRequest(), Commits()))
    assert run(engine.book.remaining()) > 0
    engine.set_route(window=128_000, max_output=None, model="other", provider="p")
    run(anchor_over(engine, rows, 120_000))
    assert run(engine.should_compress(rows, gate="pressure")).action == "compress"
    assert run(engine.book.remaining()) == 0.0
