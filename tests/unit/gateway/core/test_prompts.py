"""P15-22: approvals and clarify questions over chat (E8; R06.1, R06.6, R02.5, R05.22)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

import pytest

from indus.core.turn_state import TurnState
from indus.foundation.scope import Scope
from indus.gateway.adapters.base import (
    ApprovalPrompt,
    Capabilities,
    ClarifyPrompt,
    approval_callback,
    clarify_callback,
)
from indus.gateway.core.events import InboundEvent, Source
from indus.gateway.core.prompts import (
    MAX_COMMAND_LINES,
    ChatPrompts,
    approval_text,
    command_lines,
    parse_choice_reply,
)
from indus.gateway.core.send import SendResult
from indus.protocol.requests_port import RequestWithdrawn
from indus.protocol.server_requests import ApprovalAnswer, ApprovalRequest, ClarifyAnswer
from indus.service import approval_copy as copy
from indus.tools.calls import ToolCall, ToolResult
from tests.fakes.clock import FakeClock
from tests.fakes.model import text_turn, tool_batch
from tests.fakes.platform.core import Button, FailureKind
from tests.unit.gateway.core import _kit
from tests.unit.gateway.core._kit import ALLOWED, Kit, build_kit, dm, model_inputs

OTHER = "201"
ADMIN = "300"
GROUP = "-900"
THREAD = "77"


def run(coro: Any) -> None:
    asyncio.run(coro)


class Outcomes:
    """Inline tools that ask through the broker and record what came back."""

    def __init__(self) -> None:
        self.seen: list[str] = []
        self.answers: list[dict[str, list[str]]] = []

    def approval(self, command: str = "rm -r build", *, timeout: float | None = None) -> Any:
        async def tool(call: ToolCall, state: TurnState) -> ToolResult:
            broker = state.control.broker
            assert broker is not None
            try:
                answer = await broker.ask(
                    "approval",
                    {"command": command, "description": "deletes files"},
                    timeout=timeout,
                    turn_id=state.turn_id,
                )
            except RequestWithdrawn as exc:
                self.seen.append(f"not answered: {exc.reason.value}")
                return ToolResult("not answered", is_error=True)
            assert isinstance(answer, ApprovalAnswer)
            self.seen.append(f"{answer.choice.value}:{answer.reason or ''}")
            return ToolResult(f"answer {answer.choice.value}")

        return tool

    def clarify(self, *questions: dict[str, Any]) -> Any:
        async def tool(call: ToolCall, state: TurnState) -> ToolResult:
            broker = state.control.broker
            assert broker is not None
            answer = await broker.ask(
                "clarify", {"questions": list(questions)}, turn_id=state.turn_id
            )
            assert isinstance(answer, ClarifyAnswer)
            self.answers.append(answer.answers)
            return ToolResult("answered")

        return tool


class ButtonAdapter(_kit.FakeChatAdapter):
    """The kit's adapter with buttons and edits (Telegram-like)."""

    capabilities: ClassVar[Capabilities] = Capabilities(
        max_len=4096, supports_buttons=True, supports_edit=True
    )
    broken_buttons: ClassVar[bool] = False

    async def _send_approval(
        self, chat_id: str, prompt: ApprovalPrompt, meta: dict[str, Any]
    ) -> SendResult:
        if self.broken_buttons:
            raise RuntimeError("Bad Request: can't parse inline keyboard")
        buttons = tuple(
            Button(a.label, approval_callback(prompt.request_id, a.choice)) for a in prompt.actions
        )
        sent = await self.fake.post(chat_id, prompt.text, buttons=buttons)
        return SendResult.sent(sent.message_id)

    async def _send_clarify(
        self, chat_id: str, prompt: ClarifyPrompt, meta: dict[str, Any]
    ) -> SendResult:
        buttons = tuple(
            Button(label, clarify_callback(prompt.clarify_id, n))
            for n, label in enumerate(prompt.choices)
        )
        sent = await self.fake.post(chat_id, prompt.question, buttons=buttons)
        return SendResult.sent(sent.message_id)

    async def _edit(
        self, chat_id: str, message_id: str, text: str, final: bool, meta: dict[str, Any]
    ) -> SendResult:
        await self.fake.edit(chat_id, message_id, text)
        message = self.fake.message(message_id)
        if message is not None:
            message.buttons = ()
        return SendResult.sent(message_id)


async def make(
    scope: Scope,
    clock: FakeClock,
    tmp_path: Path,
    tool: Any,
    *,
    buttons: bool = False,
    monkeypatch: pytest.MonkeyPatch | None = None,
    timeout: Callable[[], float] | None = None,
    **kw: Any,
) -> tuple[Kit, ChatPrompts]:
    if buttons:
        assert monkeypatch is not None
        monkeypatch.setattr(_kit, "FakeChatAdapter", ButtonAdapter)
    kit = await build_kit(
        scope,
        clock,
        tmp_path,
        tool_batch(("ask", {})),
        text_turn("all done"),
        text_turn("second reply"),
        inline={"ask": tool},
        **kw,
    )
    prompts = ChatPrompts(kit.pipeline, timeout=timeout)
    prompts.install()
    return kit, prompts


async def asking(kit: Kit, prompts: ChatPrompts, event: InboundEvent | None = None) -> None:
    await kit.pipeline.accept(event or dm("clean up", message_id="start"))
    await kit.until(lambda: bool(prompts.open_cards()))
    await kit.until(lambda: prompts.open_cards()[0].message_id is not None)


async def finish(kit: Kit, prompts: ChatPrompts) -> None:
    await prompts.aclose()
    await kit.close()


def in_thread(text: str, user: str, *, mentioned: bool = False, mid: str = "t1") -> InboundEvent:
    return InboundEvent(
        source=Source(
            platform="telegram",
            chat_id=GROUP,
            chat_type="thread",
            thread_id=THREAD,
            user_id=user,
        ),
        text=text,
        message_id=mid,
        metadata={"mentioned": True} if mentioned else {},
    )


def press(
    data: str, user: str = ALLOWED, *, chat: str | None = None, bot: bool = False
) -> InboundEvent:
    return InboundEvent(
        source=Source(
            platform="telegram",
            chat_id=chat or user,
            chat_type="dm",
            user_id=user,
            is_bot=bot,
        ),
        text="",
        metadata={"button": data},
    )


# -- rendering (R05.22) ----------------------------------------------------------------------------


def test_a_long_command_is_masked_wrapped_and_counted(scope: Scope) -> None:
    secret = "ghp_" + "Zx9" * 12  # built at run time
    body = "\n".join(f"echo step {n}" for n in range(14))
    command = f"curl -H 'Authorization: token {secret}' https://example.invalid\n{body}"
    lines = command_lines(command, scope)
    assert len(lines) == MAX_COMMAND_LINES + 1
    assert lines[-1] == "(+5 more lines)"
    assert all(secret not in line for line in lines)
    wide = command_lines("x" * 250, scope)
    assert all(len(line) <= 100 for line in wide) and "".join(wide) == "x" * 250
    fenced = command_lines("echo ```\nrm -rf /", scope)
    assert all("```" not in line for line in fenced)

    request = ApprovalRequest(
        request_id="sr_1", session_id="s1", command=command, description="uses a token"
    )
    card = approval_text(request, scope, 300, buttons=False)
    assert card.startswith(copy.APPROVAL_HEADER)
    assert "Why it was flagged: uses a token" in card
    assert "No answer within 5 minutes means it will not run." in card
    assert "/approve session" in card and "/deny [reason]" in card
    assert secret not in card
    with_buttons = approval_text(request, scope, 300, buttons=True)
    assert "/approve" not in with_buttons


def test_choice_reply_parsing_table() -> None:
    options = ["staging", "production", "local dev"]
    single = [
        ("2", ["production"]),
        ("2.", ["production"]),
        ("Production", ["production"]),
        ("  local dev ", ["local dev"]),
        ("9", None),
        ("0", None),
        ("somewhere else", ["somewhere else"]),
    ]
    for text, expected in single:
        assert parse_choice_reply(text, options, False) == expected, text
    multi = [
        ("1, 3", ["staging", "local dev"]),
        ("1 3", ["staging", "local dev"]),
        ("3,1,3", ["local dev", "staging"]),
        ("staging, local dev", ["staging", "local dev"]),
        ("production", ["production"]),
        ("1, 7", None),
        ("3 apples", ["3 apples"]),
        ("staging and more", ["staging and more"]),
    ]
    for text, expected in multi:
        assert parse_choice_reply(text, options, True) == expected, text
    assert parse_choice_reply("anything at all", [], False) == ["anything at all"]


# -- R06.6: a bare "yes" never approves ------------------------------------------------------------


def test_plain_yes_never_approves_and_reaches_the_agent(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        seen = Outcomes()
        kit, prompts = await make(
            scope, fake_clock, tmp_path, seen.approval(), settings={"busy_mode": "queue"}
        )
        try:
            await asking(kit, prompts)
            for n, text in enumerate(("yes", "Yes!", "approve")):
                await kit.pipeline.accept(dm(text, message_id=f"y{n}"))
            await kit.settle()
            assert len(prompts.open_cards()) == 1 and seen.seen == []
            assert not any(copy.approved("once") in t for t in kit.wire())
            await kit.pipeline.accept(dm("/approve", message_id="a1"))
            await kit.until(lambda: seen.seen == ["once:"])
            assert copy.approved("once") in kit.wire()
            # the queued "yes" became an ordinary message for the agent
            await kit.until(lambda: any("yes" in m for m in model_inputs(kit.server)[1:]))
        finally:
            await finish(kit, prompts)

    run(main())


# -- R06.1: /approve while busy -------------------------------------------------------------------


@pytest.mark.parametrize("level", ["adapter", "runner"])
def test_approve_while_busy_answers_inline(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path, level: str
) -> None:
    async def main() -> None:
        seen = Outcomes()
        kit, prompts = await make(scope, fake_clock, tmp_path, seen.approval())
        try:
            await asking(kit, prompts)
            assert kit.pipeline.busy(kit.key())
            tasks = len(kit.pipeline._tasks)  # pyright: ignore[reportPrivateUsage]
            approve = dm("/approve session", message_id="a1")
            if level == "adapter":
                await kit.pipeline.accept(approve)  # the front door, in the adapter's await
                assert len(kit.pipeline._tasks) <= tasks  # pyright: ignore[reportPrivateUsage]
            else:
                await kit.pipeline.handle(approve)  # guard 2 (the command's own handler)
            await kit.until(lambda: seen.seen == ["session:"])
            await kit.until(lambda: "all done" in kit.wire())
        finally:
            await finish(kit, prompts)

    run(main())


# -- who may answer --------------------------------------------------------------------------------


def test_only_the_requester_or_an_admin_answers_in_a_shared_thread(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        seen = Outcomes()
        kit, prompts = await make(
            scope,
            fake_clock,
            tmp_path,
            seen.approval(),
            env={"INDUS_TELEGRAM_ALLOWED_USERS": f"{ALLOWED},{OTHER},{ADMIN}"},
            extra={"group_allow_admin_from": [ADMIN]},
        )
        try:
            await asking(kit, prompts, in_thread("clean up", ALLOWED, mentioned=True))
            card = prompts.open_cards()[0]
            assert card.requester == ALLOWED
            await kit.pipeline.accept(in_thread("/approve", OTHER, mid="o1"))
            await kit.settle()
            assert seen.seen == [] and prompts.open_cards()
            assert copy.NOT_REQUESTER in kit.fake.texts(GROUP)
            await kit.pipeline.accept(in_thread("/deny not now", ADMIN, mid="d1"))
            await kit.until(lambda: seen.seen == ["deny:not now"])
            assert copy.denied("not now") in kit.fake.texts(GROUP)
        finally:
            await finish(kit, prompts)

    run(main())


def test_a_typed_approval_that_skips_the_front_door_keeps_the_requester_rule(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    """Security (integration fix): ``/approve`` handed straight to ``handle()`` (not through
    ``accept()``'s front door) reaches the command's own handler, which applies the same
    requester-or-admin rule; a bot never answers."""

    async def main() -> None:
        seen = Outcomes()
        kit, prompts = await make(
            scope,
            fake_clock,
            tmp_path,
            seen.approval(),
            env={"INDUS_TELEGRAM_ALLOWED_USERS": f"{ALLOWED},{OTHER}"},
        )
        try:
            await asking(kit, prompts, in_thread("clean up", ALLOWED, mentioned=True))
            await kit.pipeline.handle(in_thread("/approve", OTHER, mid="o1"))
            await kit.settle()
            assert seen.seen == [] and prompts.open_cards()
            assert copy.NOT_REQUESTER in kit.fake.texts(GROUP)
            bot = in_thread("/approve", ALLOWED, mid="b1")
            bot = bot.model_copy(update={"source": bot.source.model_copy(update={"is_bot": True})})
            await kit.pipeline.handle(bot)
            await kit.settle()
            assert seen.seen == []
            await kit.pipeline.handle(in_thread("/approve", ALLOWED, mid="o2"))
            await kit.until(lambda: seen.seen == ["once:"])
        finally:
            await finish(kit, prompts)

    run(main())


def test_with_tiers_off_nobody_but_the_requester_answers(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        seen = Outcomes()
        kit, prompts = await make(
            scope,
            fake_clock,
            tmp_path,
            seen.approval(),
            env={"INDUS_TELEGRAM_ALLOWED_USERS": f"{ALLOWED},{OTHER}"},
        )
        try:
            await asking(kit, prompts, in_thread("clean up", ALLOWED, mentioned=True))
            await kit.pipeline.accept(in_thread("/approve", OTHER, mid="o1"))
            await kit.settle()
            assert seen.seen == []
            await kit.pipeline.accept(in_thread("/approve", ALLOWED, mid="o2"))
            await kit.until(lambda: seen.seen == ["once:"])
        finally:
            await finish(kit, prompts)

    run(main())


# -- buttons ---------------------------------------------------------------------------------------


def test_button_presses(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def main() -> None:
        seen = Outcomes()
        kit, prompts = await make(
            scope, fake_clock, tmp_path, seen.approval(), buttons=True, monkeypatch=monkeypatch
        )
        try:
            await asking(kit, prompts)
            card = prompts.open_cards()[0]
            assert card.buttons and card.message_id
            message = kit.fake.message(card.message_id)
            assert message is not None
            labels = [b.label for b in message.buttons]
            assert labels == ["Run once", "Allow for this conversation", "Always allow", "Deny"]
            assert "/approve" not in message.text
            once = approval_callback(card.request_id, "once")
            await kit.pipeline.accept(press(once, OTHER, chat=ALLOWED))  # not the requester
            await kit.pipeline.accept(press(once, "bot9", chat=ALLOWED, bot=True))
            await kit.settle()
            assert seen.seen == [] and prompts.open_cards()
            await kit.pipeline.accept(press(once))
            await kit.until(lambda: seen.seen == ["once:"])
            edited = kit.fake.message(card.message_id)
            assert edited is not None and edited.buttons == ()
            assert copy.approved("once") in edited.text
            await kit.pipeline.accept(press(once))  # a second press: nothing is waiting
            await kit.until(lambda: copy.NOT_WAITING in kit.wire())
            assert seen.seen == ["once:"]
        finally:
            await finish(kit, prompts)

    run(main())


def test_a_failed_button_card_falls_back_to_text(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ButtonAdapter, "broken_buttons", True)

    async def main() -> None:
        seen = Outcomes()
        kit, prompts = await make(
            scope, fake_clock, tmp_path, seen.approval(), buttons=True, monkeypatch=monkeypatch
        )
        try:
            await asking(kit, prompts)
            assert not prompts.open_cards()[0].buttons
            assert any("/approve" in t and copy.APPROVAL_HEADER in t for t in kit.wire())
            await kit.pipeline.accept(dm("/approve always", message_id="a1"))
            await kit.until(lambda: seen.seen == ["always:"])
        finally:
            await finish(kit, prompts)

    run(main())


def test_a_card_that_cannot_be_shown_is_denied_at_once(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        seen = Outcomes()
        kit, prompts = await make(scope, fake_clock, tmp_path, seen.approval())
        try:
            kit.fake.fail(FailureKind.FORBIDDEN, op="send", times=50)
            await kit.pipeline.accept(dm("clean up", message_id="start"))
            await kit.until(lambda: bool(seen.seen))
            assert seen.seen == [f"deny:{copy.NOT_SHOWN_REASON}"]
            assert not prompts.open_cards()
        finally:
            await finish(kit, prompts)

    run(main())


# -- endings (R02.5) -------------------------------------------------------------------------------


def test_a_timed_out_request_says_it_did_not_run_and_is_not_a_denial(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        seen = Outcomes()
        kit, prompts = await make(scope, fake_clock, tmp_path, seen.approval(timeout=60))
        try:
            await asking(kit, prompts)
            assert any(
                "No answer within 60 seconds means it will not run." in t for t in kit.wire()
            )
            await kit.until(lambda: bool(seen.seen))
            assert seen.seen == ["not answered: timed_out"]  # the broker's own deadline
            await kit.until(lambda: copy.timed_out(60) in kit.wire())
            assert sum(copy.timed_out(60) == t for t in kit.wire()) == 1
        finally:
            await finish(kit, prompts)

    run(main())


def test_without_a_deadline_the_gateway_denies_after_the_timeout(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        seen = Outcomes()
        kit, prompts = await make(
            scope, fake_clock, tmp_path, seen.approval(), timeout=lambda: 30.0
        )
        try:
            await asking(kit, prompts)
            await kit.until(lambda: bool(seen.seen))
            assert seen.seen == ["deny:no answer in time"]
            await kit.until(lambda: copy.timed_out(30) in kit.wire())
        finally:
            await finish(kit, prompts)

    run(main())


def test_withdrawn_and_denied_are_reported_differently(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        seen = Outcomes()
        kit, prompts = await make(scope, fake_clock, tmp_path, seen.approval())
        try:
            await asking(kit, prompts)
            await kit.pipeline.accept(dm("/stop", message_id="s1"))
            await kit.until(lambda: copy.withdrawn("interrupted") in kit.wire())
            assert seen.seen == ["not answered: interrupted"]
            assert not any(t.startswith("Denied") for t in kit.wire())
            assert not prompts.open_cards()
        finally:
            await finish(kit, prompts)

    run(main())


def test_deny_with_a_reason(scope: Scope, fake_clock: FakeClock, tmp_path: Path) -> None:
    async def main() -> None:
        seen = Outcomes()
        kit, prompts = await make(scope, fake_clock, tmp_path, seen.approval())
        try:
            await asking(kit, prompts)
            await kit.pipeline.accept(dm("/deny too risky", message_id="d1"))
            await kit.until(lambda: seen.seen == ["deny:too risky"])
            denial = copy.denied("too risky")
            assert denial in kit.wire() and denial != copy.withdrawn("interrupted")
            assert "not run" in denial
        finally:
            await finish(kit, prompts)

    run(main())


# -- clarify ---------------------------------------------------------------------------------------


def test_clarify_by_text(scope: Scope, fake_clock: FakeClock, tmp_path: Path) -> None:
    async def main() -> None:
        seen = Outcomes()
        tool = seen.clarify(
            {"id": "env", "text": "Which environment?", "choices": ["staging", "production"]},
            {
                "id": "checks",
                "text": "Which checks?",
                "choices": ["lint", "tests", "docs"],
                "multi_select": True,
            },
            {"id": "note", "text": "Anything else?"},
        )
        kit, prompts = await make(scope, fake_clock, tmp_path, tool)
        try:
            await asking(kit, prompts)
            assert any("1. staging" in t and "(1/3)" in t for t in kit.wire())
            await kit.pipeline.accept(dm("5", message_id="c0"))
            await kit.until(lambda: copy.out_of_range(2) in kit.wire())
            await kit.pipeline.accept(dm("2", message_id="c1"))
            await kit.until(lambda: any("(2/3)" in t for t in kit.wire()))
            await kit.pipeline.accept(dm("1, 3", message_id="c2"))
            await kit.until(lambda: any("(3/3)" in t for t in kit.wire()))
            await kit.pipeline.accept(dm("ship it after lunch", message_id="c3"))
            await kit.until(lambda: bool(seen.answers))
            assert seen.answers == [
                {
                    "env": ["production"],
                    "checks": ["lint", "docs"],
                    "note": ["ship it after lunch"],
                }
            ]
            await kit.until(lambda: "all done" in kit.wire())
        finally:
            await finish(kit, prompts)

    run(main())


def test_clarify_buttons_with_paging_and_other(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def main() -> None:
        seen = Outcomes()
        options = [f"option {n}" for n in range(1, 13)]
        tool = seen.clarify(
            {"id": "pick", "text": "Pick one", "choices": options},
            {"id": "why", "text": "Why?", "choices": ["speed", "cost"]},
        )
        kit, prompts = await make(
            scope, fake_clock, tmp_path, tool, buttons=True, monkeypatch=monkeypatch
        )
        try:
            await asking(kit, prompts)
            card = prompts.open_cards()[0]
            first = kit.fake.message(card.message_id or "")
            assert first is not None
            labels = [b.label for b in first.buttons]
            assert labels[:10] == options[:10] and labels[10:] == [
                copy.MORE_LABEL,
                copy.OTHER_LABEL,
            ]
            await kit.pipeline.accept(press(clarify_callback(card.request_id, 10)))  # More
            await kit.until(lambda: any("11. option 11" in t for t in kit.wire()))
            second = kit.fake.message(card.message_id or "")
            assert second is not None
            assert [b.label for b in second.buttons][:2] == ["option 11", "option 12"]
            await kit.pipeline.accept(press(clarify_callback(card.request_id, 1)))  # option 12
            await kit.until(lambda: any("Why?" in t for t in kit.wire()))
            other = len(prompts.open_cards()[0].actions) - 1
            await kit.pipeline.accept(press(clarify_callback(card.request_id, other)))
            await kit.until(lambda: copy.OTHER_PROMPT in kit.wire())
            await kit.pipeline.accept(dm("both, honestly", message_id="w1"))
            await kit.until(lambda: bool(seen.answers))
            assert seen.answers == [{"pick": ["option 12"], "why": ["both, honestly"]}]
        finally:
            await finish(kit, prompts)

    run(main())


def test_someone_elses_text_is_not_an_answer(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        seen = Outcomes()
        tool = seen.clarify({"id": "q", "text": "Ready?", "choices": ["yes", "no"]})
        kit, prompts = await make(
            scope,
            fake_clock,
            tmp_path,
            tool,
            env={"INDUS_TELEGRAM_ALLOWED_USERS": f"{ALLOWED},{OTHER}"},
            settings={"busy_mode": "queue"},
        )
        try:
            await asking(kit, prompts, in_thread("go", ALLOWED, mentioned=True))
            await kit.pipeline.accept(in_thread("no", OTHER, mentioned=True, mid="x1"))
            await kit.settle()
            assert seen.answers == [] and prompts.open_cards()
            await kit.pipeline.accept(in_thread("1", ALLOWED, mid="x2"))
            await kit.until(lambda: seen.answers == [{"q": ["yes"]}])
        finally:
            await finish(kit, prompts)

    run(main())
