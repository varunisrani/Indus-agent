"""Mixture-of-agents turns (P10-21): reference gathering, the block, trimming, alternation merge,
interrupts and the loop hook."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

import pytest

from indus.core import moa
from indus.core.exit_reasons import ExitReason
from indus.core.moa_presets import Preset, Slot
from indus.foundation.scope import Scope
from indus.providers.classify import ClassifiedError
from indus.providers.retry import RetryFailed
from indus.providers.route import RuntimeRoute
from tests.fakes.clock import FakeClock
from tests.fakes.model import ToolCall, text_turn, tool_batch
from tests.fakes.turn import PROMPT, TurnKit, check_alternation, drive

A, B, C = Slot("alpha", "a-1"), Slot("beta", "b-1"), Slot("gamma", "c-1")


@dataclass
class FakeReferences:
    """A ``ReferenceCaller``: each slot answers after ``delay`` seconds on the fake clock (or
    ``stall`` seconds for slots named in ``stalls``); every call is kept."""

    clock: FakeClock
    delay: float = 2.0
    stalls: dict[str, float] = field(default_factory=dict[str, float])
    fail: dict[str, Exception] = field(default_factory=dict[str, Exception])
    windows: dict[str, int] = field(default_factory=dict[str, int])
    calls: list[tuple[str, list[dict[str, Any]]]] = field(default_factory=list)
    in_flight: int = 0
    most_in_flight: int = 0
    cancelled: list[str] = field(default_factory=list[str])

    async def ask(self, slot: Slot, messages: list[dict[str, Any]]) -> str:
        self.calls.append((slot.provider, messages))
        self.in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self.in_flight)
        try:
            await self.clock.sleep(self.stalls.get(slot.provider, self.delay))
            if slot.provider in self.fail:
                raise self.fail[slot.provider]
            return f"advice from {slot.provider}"
        except asyncio.CancelledError:
            self.cancelled.append(slot.provider)
            raise
        finally:
            self.in_flight -= 1

    async def window(self, slot: Slot) -> int | None:
        return self.windows.get(slot.provider)


def preset(*slots: Slot, timeout: float | None = None, fanout: str = "per_iteration") -> Preset:
    return Preset("trio", tuple(slots), None, timeout, fanout)


@pytest.fixture(autouse=True)
def clean_table() -> Iterator[None]:
    moa.ARMED.entries.clear()
    yield
    moa.ARMED.entries.clear()


def arm(scope: Scope, refs: FakeReferences, the_preset: Preset, **values: Any) -> None:
    plan = moa.MoaPlan(the_preset, refs, timeout_s=the_preset.reference_timeout, **values)
    moa.ARMED.arm(scope, "s1", plan, sticky=values.pop("sticky", False))


def user_text(body: Mapping[str, Any]) -> str:
    users = [m for m in body["messages"] if m["role"] == "user"]
    return str(users[-1]["content"])


# -- the turn ------------------------------------------------------------------------------------


def test_three_references_in_parallel_and_the_block_on_the_user_message(
    scope: Scope, fake_clock: FakeClock
) -> None:
    refs = FakeReferences(fake_clock)
    arm(scope, refs, preset(A, B, C))
    kit = TurnKit(scope, fake_clock, [text_turn("done")])
    result = asyncio.run(kit.turn("plan the release"))
    assert result.exit_reason is ExitReason.COMPLETED
    assert [name for name, _ in refs.calls] == ["alpha", "beta", "gamma"]
    assert refs.most_in_flight == 3
    body = kit.server.model_requests[0].body
    assert body["messages"][0] == {"role": "system", "content": PROMPT}
    sent = user_text(body)
    assert sent.startswith("plan the release\n\n[Advice from 3 other model(s)")
    for i, name in enumerate(["alpha", "beta", "gamma"], 1):
        assert f"Reference {i} ({name}:" in sent and f"advice from {name}" in sent
    # the stored user row is what the person typed; the advice never reaches the store
    assert result.rows[result.current_turn_user_index].content == "plan the release"
    # references see our instructions, then the conversation without the system prompt
    first = refs.calls[0][1]
    assert first[0]["role"] == "system" and first[0]["content"] == moa.REFERENCE_INSTRUCTIONS
    assert first[1:] == [{"role": "user", "content": "plan the release"}]


def test_system_prompt_bytes_match_a_plain_turn(scope: Scope, fake_clock: FakeClock) -> None:
    plain = TurnKit(scope, fake_clock, [text_turn("ok")])
    asyncio.run(plain.turn("hello"))
    arm(scope, FakeReferences(fake_clock), preset(A))
    mixed = TurnKit(scope, fake_clock, [text_turn("ok")])
    asyncio.run(mixed.turn("hello"))
    first = plain.server.model_requests[0].body["messages"][0]
    second = mixed.server.model_requests[0].body["messages"][0]
    assert first == second and first["role"] == "system"
    assert plain.server.model_requests[0].body.get("tools") == mixed.server.model_requests[
        0
    ].body.get("tools")


def test_a_timed_out_reference_is_noted_and_the_turn_goes_on(
    scope: Scope, fake_clock: FakeClock
) -> None:
    refs = FakeReferences(fake_clock, stalls={"beta": 10_000.0})
    arm(scope, refs, preset(A, B, timeout=30.0))
    kit = TurnKit(scope, fake_clock, [text_turn("finished")])
    result = asyncio.run(kit.turn("go"))
    assert result.exit_reason is ExitReason.COMPLETED and result.final_text == "finished"
    sent = user_text(kit.server.model_requests[0].body)
    assert "Reference 1 (alpha:a-1):\nadvice from alpha" in sent
    assert "Reference 2 (beta:b-1): no answer (timed out after 30 s)." in sent
    assert "beta" in refs.cancelled


def test_every_iteration_asks_again_and_tool_rounds_stay_paired(
    scope: Scope, fake_clock: FakeClock
) -> None:
    refs = FakeReferences(fake_clock)
    arm(scope, refs, preset(A))
    kit = TurnKit(
        scope,
        fake_clock,
        [tool_batch(ToolCall("big", {"size": 9_000}, id="c1")), text_turn("done")],
    )
    result = asyncio.run(kit.turn("read a lot"))
    assert result.exit_reason is ExitReason.COMPLETED
    check_alternation(result.rows)
    assert len(refs.calls) == 2  # one ask per model request
    second_view = refs.calls[1][1]
    folded = [m["content"] for m in second_view if m["role"] == "assistant"]
    assert folded and folded[0].startswith('[called big with {"size":9000}]\n[result of big: ')
    assert "characters left out" in folded[0] and len(folded[0]) < moa.TOOL_RESULT_CHARS + 200
    assert second_view[-1] == {"role": "user", "content": moa.ADVICE_REQUEST}
    # the second request's advice rides on the last user message, not a new one
    body = kit.server.model_requests[1].body
    roles = [m["role"] for m in body["messages"]]
    assert roles.count("user") == 1 and roles[-1] == "tool"
    assert "[Advice from 1 other model(s)" in user_text(body)


def test_user_turn_fanout_reuses_the_block(scope: Scope, fake_clock: FakeClock) -> None:
    refs = FakeReferences(fake_clock)
    arm(scope, refs, preset(A, fanout="user_turn"))
    kit = TurnKit(
        scope, fake_clock, [tool_batch(ToolCall("echo", {"text": "hi"}, id="c1")), text_turn("ok")]
    )
    asyncio.run(kit.turn("go"))
    assert len(refs.calls) == 1
    first, second = (user_text(r.body) for r in kit.server.model_requests)
    block = first.split("\n\n", 1)[1]
    assert second == "go\n\n" + moa.EARLIER_ADVICE + block  # reused, marked as earlier advice


def test_an_unarmed_turn_never_asks_and_a_one_turn_arm_is_used_once(
    scope: Scope, fake_clock: FakeClock
) -> None:
    refs = FakeReferences(fake_clock)
    arm(scope, refs, preset(A))
    kit = TurnKit(scope, fake_clock, [text_turn("one"), text_turn("two")])
    asyncio.run(kit.turn("first"))
    asyncio.run(kit.turn("second"))
    assert len(refs.calls) == 1
    assert "Advice" not in user_text(kit.server.model_requests[1].body)


def test_a_sticky_arm_stays_until_disarmed(scope: Scope, fake_clock: FakeClock) -> None:
    refs = FakeReferences(fake_clock)
    plan = moa.MoaPlan(preset(A), refs)
    moa.ARMED.arm(scope, "s1", plan, sticky=True)
    kit = TurnKit(scope, fake_clock, [text_turn("one"), text_turn("two"), text_turn("three")])
    asyncio.run(kit.turn("a"))
    asyncio.run(kit.turn("b"))
    assert moa.ARMED.disarm(scope, "s1")
    asyncio.run(kit.turn("c"))
    assert len(refs.calls) == 2


def test_interrupt_cancels_pending_references(scope: Scope, fake_clock: FakeClock) -> None:
    refs = FakeReferences(fake_clock, stalls={"alpha": 1.0, "beta": 10_000.0, "gamma": 10_000.0})

    async def main() -> list[moa.ReferenceAnswer]:
        stop = asyncio.Event()
        task = asyncio.ensure_future(
            moa.gather(
                [A, B, C], [{"role": "user", "content": "q"}], refs, clock=fake_clock, stop=stop
            )
        )
        await fake_clock.run_until_idle()  # all three are asking now
        await fake_clock.advance_and_settle(1.0)  # alpha answers
        assert not task.done()
        stop.set()
        await drive(task, fake_clock, 0.01)
        return task.result()

    answers = asyncio.run(main())
    assert answers[0].ok and answers[0].text == "advice from alpha"
    assert [a.note for a in answers[1:]] == [moa.SKIPPED_INTERRUPTED] * 2
    assert sorted(refs.cancelled) == ["beta", "gamma"]


def test_interrupt_during_references_ends_the_turn_before_the_request(
    scope: Scope, fake_clock: FakeClock
) -> None:
    refs = FakeReferences(fake_clock, stalls={"alpha": 10_000.0})
    arm(scope, refs, preset(A))
    kit = TurnKit(scope, fake_clock, [text_turn("never")])

    def prepare(state: Any) -> None:
        async def later() -> None:
            await fake_clock.sleep(5.0)
            state.control.interrupt(state.generation)

        asyncio.ensure_future(later())

    result = asyncio.run(kit.turn("go", prepare=prepare))
    assert result.exit_reason is ExitReason.INTERRUPTED_BY_USER
    assert kit.server.model_requests == []
    assert refs.cancelled == ["alpha"]


def test_failed_references_give_a_no_advice_block(fake_clock: FakeClock) -> None:
    refs = FakeReferences(fake_clock, fail={"alpha": RuntimeError("boom")})
    answers = asyncio.run(_gather(refs, [A], fake_clock))
    block = moa.render_block(answers)
    assert block.startswith("[No other model answered on this step (alpha:a-1: failed: boom)")


async def _gather(refs: FakeReferences, slots: Sequence[Slot], clock: FakeClock) -> Any:
    task = asyncio.ensure_future(
        moa.gather(slots, [{"role": "user", "content": "q"}], refs, clock=clock)
    )
    await drive(task, clock)
    return task.result()


# -- the reference view and trimming --------------------------------------------------------------


def test_reference_view_folds_tools_and_clips_results() -> None:
    big = "y" * 10_000
    view = moa.reference_view(
        [
            {"role": "system", "content": "secret prompt"},
            {"role": "user", "content": [{"type": "text", "text": "look"}]},
            {
                "role": "assistant",
                "content": "checking",
                "tool_calls": [
                    {"id": "c1", "function": {"name": "read_file", "arguments": '{"p": 1}'}}
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": big},
        ]
    )
    assert view[0] == {"role": "user", "content": "look"}
    text = view[1]["content"]
    assert text.startswith('checking\n[called read_file with {"p": 1}]\n[result of read_file: ')
    assert "[… 6000 characters left out …]" in text
    assert len(text) < moa.TOOL_RESULT_CHARS + 200
    assert view[-1] == {"role": "user", "content": moa.ADVICE_REQUEST}
    assert moa.clip_result("short") == "short"


def test_trim_keeps_the_newest_and_starts_on_a_user_message() -> None:
    view = []
    for i in range(10):
        view.append({"role": "user", "content": f"question {i} " + "w" * 4_000})
        view.append({"role": "assistant", "content": f"answer {i} " + "w" * 4_000})
    view.append({"role": "user", "content": "last"})
    kept = moa.trim_for_window(view, 12_000)
    assert kept[-1] == {"role": "user", "content": "last"}
    assert kept[0]["role"] == "user" and len(kept) < len(view)
    assert moa.trim_for_window(view, None) == view
    # a window below the reserve keeps the last two messages, user first
    tiny = moa.trim_for_window(view, 100)
    assert tiny == [{"role": "user", "content": "last"}]


def test_trim_uses_each_reference_window(fake_clock: FakeClock) -> None:
    long_view = [{"role": "user", "content": "x" * 40_000}, {"role": "assistant", "content": "a"}]
    long_view.append({"role": "user", "content": "now"})
    refs = FakeReferences(fake_clock, windows={"alpha": 10_000})

    async def main() -> None:
        task = asyncio.ensure_future(moa.gather([A, B], long_view, refs, clock=fake_clock))
        await drive(task, fake_clock)

    asyncio.run(main())
    sent = dict(refs.calls)
    assert [m["content"] for m in sent["alpha"][1:]] == ["now"]
    assert len(sent["beta"]) == 4  # no window known: unchanged


# -- alternation and privacy ---------------------------------------------------------------------


@dataclass
class FakeAux:
    """Just enough of ``AuxClient`` for ``AuxReferences``: it rejects two user messages in a row
    the way a strict chat template does."""

    strict: bool = True
    sent: list[list[dict[str, Any]]] = field(default_factory=list)
    tasks: list[str] = field(default_factory=list[str])

    def route_for(self, candidate: Any) -> RuntimeRoute:
        return RuntimeRoute(
            provider=candidate.provider,
            model=candidate.model,
            api_mode="chat_completions",
            base_url="https://fake.invalid/v1",
        )

    async def call_route(self, task: str, route: RuntimeRoute, messages: Any, **_: Any) -> Any:
        self.tasks.append(task)
        self.sent.append([dict(m) for m in messages])
        roles = [m["role"] for m in messages]
        if self.strict and any(a == b for a, b in zip(roles, roles[1:], strict=False)):
            verdict = ClassifiedError("role_alternation", status=400, retryable=False)
            raise RetryFailed(verdict, error=RuntimeError("roles"), attempts=1, waits=())

        @dataclass
        class Answer:
            text: str

        return Answer("merged fine")


def test_alternation_rejection_merges_and_is_remembered() -> None:
    aux = FakeAux()
    refs = moa.AuxReferences(cast(Any, aux))
    doubled = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "one"},
        {"role": "user", "content": "two"},
    ]
    assert asyncio.run(refs.ask(A, doubled)) == "merged fine"
    assert len(aux.sent) == 2 and aux.sent[1][1] == {"role": "user", "content": "one\n\ntwo"}
    assert aux.tasks == ["moa", "moa"]
    asyncio.run(refs.ask(A, doubled))
    assert len(aux.sent) == 3  # merged before sending: no second rejection
    # another destination is not merged until it rejects
    aux.strict = False
    asyncio.run(refs.ask(B, doubled))
    assert aux.sent[-1] == doubled


def test_merge_alternation_leaves_the_input_alone() -> None:
    msgs = [{"role": "user", "content": "a"}, {"role": "user", "content": [{"text": "b"}]}]
    assert moa.merge_alternation(msgs) == [{"role": "user", "content": "a\n\nb"}]
    assert len(msgs) == 2


def test_privacy_masks_emails_and_formatted_phones() -> None:
    text = "mail ana@example.org or call (555) 123-4567 / +1 555-123-4567; build 2026-07-12 v1.2.3"
    masked = moa.mask_private(text)
    assert "ana@example.org" not in masked and "[email hidden]" in masked
    assert masked.count("[phone hidden]") == 2
    assert "2026-07-12" in masked and "v1.2.3" in masked
    assert moa.mask_private("5551234567 and 10.0.0.1") == "5551234567 and 10.0.0.1"


def test_privacy_modes_on_the_block() -> None:
    answers = [moa.ReferenceAnswer(A, "write to ana@example.org")]
    assert "ana@example.org" in moa.render_block(answers, privacy="display")
    assert "ana@example.org" not in moa.render_block(answers, privacy="full")


def test_attach_block_to_list_content_and_without_a_user_message() -> None:
    out = moa.attach_block([{"role": "user", "content": [{"type": "text", "text": "q"}]}], "B")
    assert out[0]["content"][-1] == {"type": "text", "text": "B"}
    assert moa.attach_block([{"role": "system", "content": "s"}], "B")[-1] == {
        "role": "user",
        "content": "B",
    }


# -- the real auxiliary client -------------------------------------------------------------------


def test_aux_references_over_the_real_client(scope: Scope, fake_clock: FakeClock) -> None:
    import httpx

    from indus.providers.auxiliary import AuxClient
    from tests.fakes.model import FakeModelServer, fake_route, script_of

    seen: list[dict[str, Any]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        import json

        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "try X"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 2},
            },
        )

    recorded: list[tuple[str, str]] = []

    async def record(task: str, route: RuntimeRoute, usage: Any, latency: float) -> None:
        recorded.append((task, route.model))

    server = FakeModelServer(script_of(), clock=fake_clock)
    aux = AuxClient(
        scope,
        fake_route(server),
        clock=fake_clock,
        client_factory=lambda _r: httpx.AsyncClient(transport=httpx.MockTransport(answer)),
        record=record,
    )
    refs = moa.AuxReferences(aux)
    main = fake_route(server)
    slot = Slot(main.provider, "reference-model", reasoning_effort="low")
    text = asyncio.run(refs.ask(slot, [{"role": "user", "content": "q"}]))
    assert text == "try X"
    assert recorded == [("moa", "reference-model")]
    assert seen[0]["model"] == "reference-model" and seen[0]["stream"] is False
