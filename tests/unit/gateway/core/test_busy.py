"""P15-12: the two busy guards and the busy-input modes (R06.1)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from indus.core.turn_state import TurnState
from indus.foundation.scope import Scope
from indus.gateway.core.busy import (
    CONTROL_COMMANDS,
    ONE_TIME_TIP,
    PendingQueue,
    ack_text,
    busy_mode,
    is_control,
)
from indus.gateway.core.commands import BUSY_REJECT
from indus.gateway.core.events import Attachment, MessageKind
from indus.protocol.requests_port import RequestNotAnswered
from indus.protocol.server_requests import ApprovalAnswer
from indus.tools.calls import ToolCall, ToolResult
from tests.fakes.clock import FakeClock
from tests.fakes.model import Finish, Stall, Text, Turn, text_turn, tool_batch
from tests.unit.gateway.core._kit import Kit, build_kit, dm, model_inputs


def run(coro: Any) -> None:
    asyncio.run(coro)


def stalled(text: str, seconds: float = 1000.0) -> Turn:
    return Turn((Text((text,)), Stall(seconds), Finish("stop")))


async def confirm(call: ToolCall, state: TurnState) -> ToolResult:
    """An inline tool that asks for approval through the broker."""
    broker = state.control.broker
    assert broker is not None
    try:
        answer = await broker.ask(
            "approval", {"command": "rm -r build", "description": "clean up"}, turn_id=state.turn_id
        )
    except RequestNotAnswered as exc:
        return ToolResult(f"not approved: {exc}", is_error=True)
    assert isinstance(answer, ApprovalAnswer)
    return ToolResult(f"approved: {answer.choice.value}")


async def running(kit: Kit, text: str = "start") -> None:
    """Start a turn for the default chat and wait until the model has it."""
    before = len(kit.server.requests)
    kit.pipeline.spawn(kit.pipeline.handle(dm(text, message_id="start")))
    await kit.until(lambda: len(kit.server.requests) > before)


# -- helpers ---------------------------------------------------------------------------------------


def test_control_commands_and_modes() -> None:
    assert {"stop", "new", "queue", "status", "approve", "deny"} == CONTROL_COMMANDS
    assert is_control(dm("/stop")) and is_control(dm("/reset"))  # alias of /new
    assert is_control(dm("/q later"))  # alias of /queue
    assert not is_control(dm("/help")) and not is_control(dm("stop"))
    assert not is_control(dm("/stop", allow_control=False))
    assert busy_mode("QUEUE") == "queue" and busy_mode("nonsense") == "interrupt"


def test_pending_queue_merges_photo_bursts_and_is_capped() -> None:
    queue = PendingQueue(cap=3)

    def voice(n: int) -> Any:
        return dm("", kind=MessageKind.VOICE, media=(Attachment(ref=f"/v{n}.ogg"),))

    def photo(ref: str, text: str = "") -> Any:
        return dm(text, kind=MessageKind.PHOTO, media=(Attachment(ref=ref),))

    assert queue.push("k", voice(1)) and queue.push("k", voice(2))
    assert queue.depth("k") == 2  # voice notes are separate messages
    assert queue.push("k", photo("/a.jpg"))
    assert queue.push("k", photo("/b.jpg", "caption"))
    assert queue.depth("k") == 3  # the second photo joined the first (an album)
    merged = queue.items("k")[-1]
    assert [m.ref for m in merged.media] == ["/a.jpg", "/b.jpg"] and merged.text == "caption"
    assert not queue.push("k", voice(3))  # over the cap
    queue.push_front("k", dm("urgent"))
    popped = queue.pop("k")
    assert popped is not None and popped.text == "urgent"
    assert queue.clear("k") == 3 and queue.pop("k") is None


# -- R06.1: /approve unblocks a turn at both guard levels ------------------------------------------


@pytest.mark.parametrize("level", ["adapter", "runner"])
def test_a_turn_waiting_for_approval_is_unblocked_by_approve(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path, level: str
) -> None:
    async def main() -> None:
        kit = await build_kit(
            scope,
            fake_clock,
            tmp_path,
            tool_batch(("confirm", {})),
            text_turn("cleaned"),
            inline={"confirm": confirm},
        )
        try:
            kit.pipeline.spawn(kit.pipeline.handle(dm("clean up")))
            await kit.until(lambda: any("I need your approval" in t for t in kit.wire()))
            assert kit.pipeline.busy(kit.key())
            approve = dm("/approve", message_id="a1")
            if level == "adapter":
                await kit.pipeline.accept(approve)  # guard 1: inline, not a background task
            else:
                await kit.pipeline.handle(approve)  # guard 2
            assert "Approved." in kit.wire()
            await kit.until(lambda: "cleaned" in kit.wire())
        finally:
            await kit.close()

    run(main())


def test_control_commands_run_inline_while_busy(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(scope, fake_clock, tmp_path, stalled("working"))
        try:
            await running(kit)
            tasks = len(kit.pipeline._tasks)  # pyright: ignore[reportPrivateUsage]
            await kit.pipeline.accept(dm("/status", message_id="s"))
            assert any(t.startswith("Conversation:") for t in kit.wire())  # answered already
            assert len(kit.pipeline._tasks) == tasks  # pyright: ignore[reportPrivateUsage]
            await kit.pipeline.accept(dm("/stop", message_id="x"))
            assert "Stopped." in kit.wire()
            await kit.until(lambda: not kit.pipeline.busy(kit.key()))
        finally:
            await kit.close()

    run(main())


def test_commands_while_busy_use_the_busy_policy(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(scope, fake_clock, tmp_path, stalled("working"))
        try:
            await running(kit)
            await kit.pipeline.accept(dm("/model other", message_id="1"))  # policy: reject
            await kit.until(lambda: BUSY_REJECT.format(name="model") in kit.wire())
            await kit.pipeline.accept(dm("/help", message_id="2"))  # policy: dispatch
            await kit.until(lambda: any(t.startswith("Commands you can run") for t in kit.wire()))
            await kit.pipeline.accept(dm("/whoami", message_id="3"))  # idle-only table
            await kit.until(lambda: BUSY_REJECT.format(name="whoami") in kit.wire())
        finally:
            await kit.close()

    run(main())


# -- modes -----------------------------------------------------------------------------------------


def test_interrupt_mode_redirects_with_one_merged_text_and_one_ack(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(
            scope,
            fake_clock,
            tmp_path,
            stalled("going the wrong way"),
            text_turn("corrected answer"),
        )
        try:
            await running(kit, "plan my trip")
            for n, piece in enumerate(("actually", "go to Rome", "not Paris")):
                await kit.pipeline.accept(dm(piece, message_id=f"b{n}"))
                await fake_clock.advance_and_settle(0.1)
            await kit.until(lambda: "corrected answer" in kit.wire())
            acks = [s.text for s in kit.sender.sent if s.kind == "ack"]
            assert len(acks) == 1
            assert acks[0].startswith(ack_text("redirected"))
            assert "display.busy_input_mode" in acks[0]  # the one-time tip
            assert any("actually\ngo to Rome\nnot Paris" in m for m in model_inputs(kit.server))
            await kit.pipeline.accept(dm("again", message_id="c"))  # idle now: a normal turn
        finally:
            await kit.close()

    run(main())


def test_queue_mode_runs_each_follow_up_as_its_own_turn_in_order(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(
            scope,
            fake_clock,
            tmp_path,
            stalled("first", 5.0),
            text_turn("second"),
            text_turn("third"),
            settings={"busy_mode": "queue"},
        )
        try:
            await running(kit, "one")
            await kit.pipeline.accept(dm("two", message_id="2"))
            await kit.pipeline.accept(dm("three", message_id="3"))
            assert kit.pipeline.pending.depth(kit.key()) == 2
            await kit.until(lambda: "third" in kit.wire())
            assert model_inputs(kit.server) == ["one", "two", "three"]
            finals = kit.sender.texts("final")
            assert finals == ["first", "second", "third"]
            acks = kit.sender.texts("ack")
            assert len(acks) == 2 and acks[1] == ack_text("queue")  # tip only on the first
        finally:
            await kit.close()

    run(main())


def test_steer_mode_lands_in_the_running_turn(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(
            scope,
            fake_clock,
            tmp_path,
            stalled("drafting", 3.0),
            text_turn("used metric units"),
            settings={"busy_mode": "steer", "busy_tip_seen": True},
        )
        try:
            await running(kit, "write a recipe")
            await kit.pipeline.accept(dm("use metric units", message_id="2"))
            await kit.until(lambda: ack_text("steer") in kit.wire())
            await kit.until(lambda: "used metric units" in kit.wire())
            assert "use metric units" in model_inputs(kit.server)[-1]
            assert kit.pipeline.pending.depth(kit.key()) == 0
        finally:
            await kit.close()

    run(main())


def test_steer_before_the_turn_started_falls_back_to_the_queue(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(
            scope,
            fake_clock,
            tmp_path,
            text_turn("queued answer"),
            settings={"busy_mode": "steer", "busy_ack": False},
        )
        try:
            claim = kit.pipeline.claims.claim(kit.key())  # set up, nothing running yet
            assert claim is not None
            await kit.pipeline.accept(dm("later please", message_id="2"))
            await kit.until(lambda: kit.pipeline.pending.depth(kit.key()) == 1)
            kit.pipeline.claims.release(claim)
            assert kit.pipeline.drain(kit.key())
            await kit.until(lambda: kit.wire() == ["queued answer"])
        finally:
            await kit.close()

    run(main())


def test_media_in_interrupt_mode_stops_the_turn_and_runs_next(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(
            scope,
            fake_clock,
            tmp_path,
            stalled("slow"),
            text_turn("nice photo"),
            settings={"busy_tip_seen": True},
        )
        try:
            await running(kit)
            photo = dm("look", kind=MessageKind.PHOTO, media=(Attachment(ref="/tmp/p.jpg"),))
            await kit.pipeline.accept(photo)
            await kit.until(lambda: "nice photo" in kit.wire())
            assert ack_text("interrupted") in kit.wire()
            assert model_inputs(kit.server)[-1] == "look"
        finally:
            await kit.close()

    run(main())


def test_acks_can_be_turned_off_and_the_tip_shows_once(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(
            scope,
            fake_clock,
            tmp_path,
            stalled("one", 5.0),
            stalled("two", 5.0),
            stalled("three", 5.0),
            settings={"busy_mode": "queue", "busy_ack": False},
        )
        try:
            await running(kit)
            await kit.pipeline.accept(dm("quiet follow-up", message_id="2"))
            await kit.settle(3)
            assert kit.sender.texts("ack") == []
            kit.set(busy_ack=True)
            await kit.pipeline.accept(dm("loud follow-up", message_id="3"))
            await kit.pipeline.accept(dm("another", message_id="4"))
            await kit.until(lambda: len(kit.sender.texts("ack")) == 2)
            first, second = kit.sender.texts("ack")
            tip = ONE_TIME_TIP.split("{")[0]
            assert tip in first and tip not in second
            await kit.until(lambda: kit.scope.home.joinpath("config.yaml").exists())
            assert "busy_input: true" in kit.scope.home.joinpath("config.yaml").read_text()
        finally:
            await kit.close()

    run(main())


def test_unauthorized_messages_to_a_busy_chat_are_dropped(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(scope, fake_clock, tmp_path, stalled("working"))
        try:
            await running(kit)
            stranger = dm("hijack", user="999")
            stranger = stranger.model_copy(
                update={"source": stranger.source.model_copy(update={"chat_id": "200"})}
            )
            await kit.pipeline.accept(stranger)
            await kit.settle(10)
            assert kit.pipeline.pending.depth(kit.key()) == 0
            assert len(kit.server.requests) == 1
        finally:
            await kit.close()

    run(main())


def test_an_idle_queue_command_from_the_adapter_does_not_hold_up_reading(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(scope, fake_clock, tmp_path, stalled("long reply"))
        try:
            await kit.pipeline.accept(dm("/queue a long job"))  # inline control command
            assert kit.pipeline.busy(kit.key())  # returned while the turn starts in a task
            await kit.until(lambda: kit.server.requests)
            await kit.pipeline.accept(dm("/stop", message_id="s"))
            assert "Stopped." in kit.wire()
        finally:
            await kit.close()

    run(main())
