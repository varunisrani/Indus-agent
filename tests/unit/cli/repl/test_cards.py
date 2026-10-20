"""Approval, clarify, sudo and secret cards (P06-07): R05.11 digits, R05.22 wrap not clip."""

from __future__ import annotations

from typing import Any

import pytest
from prompt_toolkit.filters import to_filter

from indus.cli.repl.cards import (
    CANCEL_REASON,
    COMMAND_LINES,
    ApprovalCard,
    ClarifyCard,
    describe_answer,
    open_card,
    wrap_keep_all,
)
from indus.protocol.errors import SERVER_REQUEST_NOT_FOUND
from indus.protocol.events import CancelReason, ServerRequestCancelled
from indus.protocol.methods import ProtocolError
from indus.protocol.server_requests import (
    ApprovalAnswer,
    ApprovalChoice,
    ApprovalRequest,
    ClarifyAnswer,
    ClarifyQuestion,
    ClarifyRequest,
    SecretAnswer,
    SecretRequest,
    SudoAnswer,
    SudoRequest,
)
from tests.fakes.clock import FakeClock
from tests.fakes.service import SESSION
from tests.unit.cli.repl.conftest import Rig, load_stream

CTRL_C = "\x03"
ENTER = "\r"


def approval(request_id: str = "sr_1", **fields: Any) -> ApprovalRequest:
    fields.setdefault("command", "rm -rf build/")
    fields.setdefault("description", "deletes files recursively")
    return ApprovalRequest(
        request_id=request_id, session_id=SESSION, turn_id="t1", deadline_s=300.0, **fields
    )


def answers(rig: Rig) -> list[Any]:
    return [call.args[2] for call in rig.host.named("answer")]


def answered(rig: Rig, count: int = 1) -> list[Any]:
    rig.host.wait_for("answer", count)
    return answers(rig)


# -- approval through pipe input ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("digit", "choice"),
    [
        ("1", ApprovalChoice.ONCE),
        ("2", ApprovalChoice.SESSION),
        ("3", ApprovalChoice.ALWAYS),
    ],
)
def test_each_digit_maps_to_its_answer(rig: Rig, digit: str, choice: ApprovalChoice) -> None:
    rig.feed(approval())
    rig.keys(digit)
    assert answered(rig) == [ApprovalAnswer(choice=choice)]
    call = rig.host.named("answer")[0]
    assert call.args[:2] == (SESSION, "sr_1")
    rig.settle()
    assert rig.call(lambda: rig.repl.card) is None


def test_four_denies_and_asks_for_a_reason(rig: Rig) -> None:
    rig.feed(approval())
    rig.keys("4")
    rig.settle()
    assert rig.host.named("answer") == []
    rig.keys("use the trash folder instead" + ENTER)
    assert answered(rig) == [
        ApprovalAnswer(choice=ApprovalChoice.DENY, reason="use the trash folder instead")
    ]
    rig.settle()  # the note is printed after the host saw the answer (``_answer_card``)
    assert "denied: use the trash folder instead" in rig.text()


def test_deny_without_a_reason(rig: Rig) -> None:
    rig.feed(approval())
    rig.keys("4" + ENTER)
    assert answered(rig) == [ApprovalAnswer(choice=ApprovalChoice.DENY, reason=None)]


def test_digits_follow_the_offered_order(rig: Rig) -> None:
    rig.feed(approval(choices=[ApprovalChoice.ONCE, ApprovalChoice.DENY]))
    rig.keys("3")  # not offered: ignored, typed as nothing
    rig.settle()
    assert rig.host.named("answer") == []
    rig.keys("2" + ENTER)
    assert answered(rig) == [ApprovalAnswer(choice=ApprovalChoice.DENY)]


def test_typed_words_work_and_letters_do_not(rig: Rig) -> None:
    rig.feed(approval())
    rig.keys("o" + ENTER)  # R05.11: no letter shortcuts
    rig.settle()
    assert rig.host.named("answer") == []
    assert "Press a number from 1 to 4." in rig.text()
    rig.keys("session" + ENTER)
    assert answered(rig) == [ApprovalAnswer(choice=ApprovalChoice.SESSION)]


def test_ctrl_c_on_the_card_denies_with_cancel_and_keeps_running(rig: Rig) -> None:
    rig.feed(rig.host.started("t1", 3), approval())
    rig.keys(CTRL_C)
    assert answered(rig) == [ApprovalAnswer(choice=ApprovalChoice.DENY, reason=CANCEL_REASON)]
    rig.settle()
    assert rig.thread.is_alive()
    assert rig.host.named("interrupt") == []  # the card took the press, not the turn


def test_the_draft_is_held_while_the_card_is_open(rig: Rig) -> None:
    rig.keys("my unfinished draft")
    rig.settle()
    rig.feed(approval())
    assert rig.buffer_text() == ""
    rig.keys("1")
    answered(rig)
    rig.settle()
    assert rig.buffer_text() == "my unfinished draft"
    assert rig.host.named("submit") == []


def test_long_command_wraps_without_clipping(rig: Rig) -> None:
    command = "echo " + " ".join(f"word{n:03d}" for n in range(60))
    rig.feed(approval(command=command))
    lines = [line[4:] for line in rig.text().splitlines() if line.startswith("    ")]
    assert len(lines) > 1
    assert all(len(line) <= 80 - 8 for line in lines)
    assert "".join(lines) == command
    rig.keys("1")
    answered(rig)


def test_very_long_commands_show_ten_lines_and_a_count_and_view_shows_all() -> None:
    command = "\n".join(f"step {n}" for n in range(14))
    card = ApprovalCard(approval(command=command), 0.0, 80)
    body = [line.plain for line in card.body(80)]
    shown = [line for line in body if line.startswith("    step")]
    assert len(shown) == COMMAND_LINES
    assert any("4 more lines (type view to see all)" in line for line in body)
    step = card.submit("view")
    assert step.answer is None
    assert [t.plain for t in step.show if t.plain.startswith("    step")] == [
        f"    step {n}" for n in range(14)
    ]


def test_wrap_keeps_every_character() -> None:
    text = "x" * 50 + " " + "y" * 7 + "\n" + "z" * 130
    pieces = wrap_keep_all(text, 20)
    assert all(len(p) <= 20 for p in pieces)
    assert "".join(pieces) == text.replace("\n", "")


def test_timeout_closes_the_card_with_no_answer(rig: Rig, fake_clock: FakeClock) -> None:
    rig.feed(approval())
    fake_clock.advance(299)
    rig.call(rig.repl.tick)
    assert rig.call(lambda: rig.repl.card) is not None
    fake_clock.advance(2)
    rig.call(rig.repl.tick)
    assert rig.call(lambda: rig.repl.card) is None
    assert "no answer — the question timed out" in rig.text()
    assert rig.host.named("answer") == []


def test_countdown_in_the_prompt(fake_clock: FakeClock) -> None:
    card = ApprovalCard(approval(), fake_clock.monotonic(), 80)
    assert card.prompt(fake_clock.monotonic()) == "pick 1-4 [300s] › "
    fake_clock.advance(10.5)
    assert card.prompt(fake_clock.monotonic()) == "pick 1-4 [290s] › "


def test_a_withdrawn_request_closes_its_card(rig: Rig) -> None:
    rig.feed(approval())
    rig.feed(
        ServerRequestCancelled(
            session_id=SESSION, seq=9, request_id="sr_1", reason=CancelReason.INTERRUPTED
        )
    )
    assert rig.call(lambda: rig.repl.card) is None
    assert "question withdrawn: the reply was interrupted" in rig.text()


def test_a_second_request_waits_for_the_first(rig: Rig) -> None:
    rig.feed(approval("sr_1"), approval("sr_2", command="ls"))
    rig.keys("1")
    rig.host.wait_for("answer")
    rig.settle()
    card = rig.call(lambda: rig.repl.card)
    assert card is not None and card.request_id == "sr_2"
    rig.keys("2")
    answered(rig, 2)
    assert [c.args[1] for c in rig.host.named("answer")] == ["sr_1", "sr_2"]


def test_an_answer_the_host_refuses_closes_the_card(rig: Rig) -> None:
    rig.host.answer_error = ProtocolError(SERVER_REQUEST_NOT_FOUND)
    rig.feed(approval())
    rig.keys("1")
    rig.host.wait_for("answer")
    rig.settle()
    assert rig.call(lambda: rig.repl.card) is None
    assert "that question was already closed" in rig.text()


def test_recorded_approval_stream_through_the_repl(rig: Rig) -> None:
    items = load_stream("approval_request")
    rig.feed(*items[:3])  # started, tool.started, the approval request
    rig.keys("1")
    assert answered(rig) == [ApprovalAnswer(choice=ApprovalChoice.ONCE)]
    rig.feed(*items[3:])
    text = rig.text()
    assert "Approval needed: sends data to a remote host" in text
    assert "rec0…mnop" in text and "0123456789" not in text
    assert "  Uploaded." in text


# -- clarify --------------------------------------------------------------------------------------


def clarify(*questions: ClarifyQuestion) -> ClarifyRequest:
    return ClarifyRequest(request_id="sr_c", session_id=SESSION, questions=list(questions))


def test_clarify_single_multi_free_and_skip(rig: Rig) -> None:
    rig.feed(
        clarify(
            ClarifyQuestion(id="lang", text="Which language?", choices=["Python", "Go", "Rust"]),
            ClarifyQuestion(
                id="ci", text="Which CI?", choices=["lint", "tests", "docs"], multi_select=True
            ),
            ClarifyQuestion(id="name", text="Project name?"),
            ClarifyQuestion(id="extra", text="Anything else?"),
        )
    )
    rig.keys("2" + ENTER)
    rig.keys("1 3" + ENTER)
    rig.keys("indus-demo" + ENTER)
    rig.keys(ENTER)
    assert answered(rig) == [
        ClarifyAnswer(
            answers={"lang": ["Go"], "ci": ["lint", "docs"], "name": ["indus-demo"], "extra": []}
        )
    ]
    text = rig.text()
    assert "? (1/4) Which language?" in text and "? (2/4) Which CI?" in text


def test_clarify_rejects_out_of_range_and_many_picks_for_single_choice() -> None:
    card = ClarifyCard(
        clarify(ClarifyQuestion(id="q", text="Pick", choices=["a", "b"])), opened_at=0.0
    )
    assert card.submit("3").show and card.submit("1 2").show
    assert card.submit("own words").answer == ClarifyAnswer(answers={"q": ["own words"]})


def test_ctrl_c_on_clarify_skips_the_rest(rig: Rig) -> None:
    rig.feed(
        clarify(
            ClarifyQuestion(id="a", text="First?", choices=["x", "y"]),
            ClarifyQuestion(id="b", text="Second?"),
        )
    )
    rig.keys("1" + ENTER)
    rig.settle()
    rig.keys(CTRL_C)
    assert answered(rig) == [ClarifyAnswer(answers={"a": ["x"], "b": []})]


# -- sudo / secret --------------------------------------------------------------------------------


def test_sudo_entry_is_masked_and_sent(rig: Rig) -> None:
    rig.feed(SudoRequest(request_id="sr_s", session_id=SESSION, reason="install a package"))
    assert rig.call(lambda: rig.repl.masked) is True
    session = rig.repl.prompt_session
    assert session is not None
    assert rig.call(lambda: to_filter(session.is_password)()) is True
    rig.keys("pw-typed-here" + ENTER)
    assert answered(rig) == [SudoAnswer(password="pw-typed-here")]
    # The host sees the answer before the REPL thread prints its note (``_answer_card``): let the
    # loop finish that callback before reading the screen.
    rig.settle()
    text = rig.text()
    assert "pw-typed-here" not in text
    assert "password sent" in text


def test_sudo_enter_alone_or_ctrl_c_sends_no_password(rig: Rig) -> None:
    rig.feed(SudoRequest(request_id="sr_1", session_id=SESSION, reason="x"))
    rig.keys(ENTER)
    rig.host.wait_for("answer")
    rig.feed(SudoRequest(request_id="sr_2", session_id=SESSION, reason="y"))
    rig.keys(CTRL_C)
    assert answered(rig, 2) == [SudoAnswer(password=None), SudoAnswer(password=None)]


def test_secret_value_and_skip() -> None:
    request = SecretRequest(request_id="sr_k", session_id=SESSION, name="DEMO_TOKEN", prompt="p")
    card = open_card(request, 0.0)
    assert card is not None and card.masked
    assert card.submit("value").answer == SecretAnswer(value="value")
    assert card.cancel() == SecretAnswer(value=None)


def test_describe_answer_never_shows_secrets() -> None:
    assert describe_answer(SudoAnswer(password="hunter-two")) == "password sent"
    assert describe_answer(SecretAnswer(value="v")) == "value saved"
    assert describe_answer(ApprovalAnswer(choice=ApprovalChoice.ALWAYS)) == (
        "always allow this command — approved"
    )
