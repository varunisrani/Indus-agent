"""The REPL's side of slash dispatch, the bang shell, /queue and completion (P06-04 + P06-09)."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from indus.cli.repl import slash_bridge
from indus.cli.repl.app import Repl
from indus.cli.repl.slash_bridge import SlashCompleter, wire_slash
from indus.cli.slash.context import SlashContext
from indus.foundation.scope import Scope
from indus.protocol.server_requests import ApprovalChoice, ApprovalRequest
from indus.safety.approvals import APPROVALS, session_key
from indus.service.commands.registry import Completion as Candidate
from indus.state import registry
from indus.state.registry import StoreHandle
from tests.fakes.service import SESSION
from tests.unit.cli.repl.conftest import Rig
from tests.unit.cli.slash._fakes import FakePort, seed

ENTER = "\r"
OLD1 = "20260101_080000_old001"
OLD2 = "20260101_080500_old002"


@pytest.fixture
def store(scope: Scope) -> Iterator[StoreHandle]:
    handle = registry.open_unshared(scope)
    yield handle
    handle.close()
    registry.close_all()


@pytest.fixture
def port() -> FakePort:
    return FakePort()


@pytest.fixture
def ctx(scope: Scope, store: StoreHandle, port: FakePort) -> Iterator[SlashContext]:
    made = SlashContext(scope=scope, port=port, session_id=SESSION, out=print, store=store)
    yield made
    for sid in (SESSION, "new1", OLD1, OLD2):
        APPROVALS.clear_session(session_key(scope, sid))


@pytest.fixture
def resumed() -> list[str]:
    return []


@pytest.fixture
def slash_rig(make_rig: Callable[..., Rig], ctx: SlashContext, resumed: list[str]) -> Rig:
    def setup(repl: Repl) -> None:
        wire_slash(repl, ctx, on_resume=resumed.append)

    return make_rig(setup=setup)


def submitted(rig: Rig) -> list[str]:
    return [call.args[1] for call in rig.host.named("submit")]


def test_quit_leaves(slash_rig: Rig) -> None:
    slash_rig.keys("/quit" + ENTER)
    assert slash_rig.wait_exit() == 0


def test_new_switches_the_feed_without_resume(
    slash_rig: Rig, resumed: list[str], port: FakePort
) -> None:
    slash_rig.keys("/new" + ENTER)
    slash_rig.host.wait_for("feed", 2)
    slash_rig.settle()
    assert slash_rig.repl.session_id == "new1"
    assert port.names()[:2] == ["create", "stop_processes"]
    assert resumed == []
    assert "Started a new session (new1)." in slash_rig.text()


def test_new_with_history_outside_a_terminal_keeps_the_session(
    slash_rig: Rig, store: StoreHandle, port: FakePort
) -> None:
    seed(store, SESSION, 1)
    slash_rig.keys("/new" + ENTER)
    slash_rig.settle(6)
    assert "Kept the current session." in slash_rig.text()
    assert port.names() == []


def test_resume_by_title_runs_the_resume_follow_up(
    slash_rig: Rig, resumed: list[str], store: StoreHandle
) -> None:
    seed(store, OLD1, 1, title="older work")
    slash_rig.keys("/resume older work" + ENTER)
    slash_rig.host.wait_for("feed", 2)
    slash_rig.settle()
    assert resumed == [OLD1]
    assert slash_rig.host.named("feed")[-1].args == (OLD1,)
    assert slash_rig.repl.title == "older work"


def test_a_number_after_resume_picks_a_row_and_plain_text_still_reaches_the_model(
    slash_rig: Rig, resumed: list[str], store: StoreHandle
) -> None:
    seed(store, OLD1, 1)
    seed(store, OLD2, 1)
    slash_rig.keys("/resume" + ENTER)
    slash_rig.settle(6)
    assert "Type a number to resume that session" in slash_rig.text()
    slash_rig.keys("2" + ENTER)
    slash_rig.host.wait_for("feed", 2)
    slash_rig.settle()
    assert len(resumed) == 1 and resumed[0] in (OLD1, OLD2)
    slash_rig.keys("hello" + ENTER)
    slash_rig.host.wait_for("submit")
    assert submitted(slash_rig) == ["hello"]


def test_retry_submits_and_undo_prefills(slash_rig: Rig, store: StoreHandle) -> None:
    seed(store, SESSION, 2)
    slash_rig.keys("/retry" + ENTER)
    slash_rig.host.wait_for("submit")
    assert submitted(slash_rig) == ["q2"]
    slash_rig.feed(slash_rig.host.started("t1", 1), slash_rig.host.done("t1"))
    slash_rig.keys("/undo" + ENTER)
    slash_rig.settle(6)
    assert slash_rig.buffer_text() == "q1"


def test_clear_empties_the_ring_and_switches(slash_rig: Rig) -> None:
    slash_rig.call(lambda: slash_rig.repl.print("old line"))
    slash_rig.keys("/clear" + ENTER)
    slash_rig.host.wait_for("feed", 2)
    slash_rig.settle()
    assert "old line" not in list(slash_rig.repl.renderer.ring)
    assert slash_rig.repl.session_id == "new1"


def test_bang_lines_run_in_the_shell_and_never_reach_the_model(slash_rig: Rig) -> None:
    slash_rig.keys("!echo bridged-output" + ENTER)
    for _ in range(40):
        slash_rig.settle()
        if "bridged-output" in slash_rig.text():
            break
    assert "bridged-output" in slash_rig.text()
    assert submitted(slash_rig) == []


def test_queued_text_goes_out_at_turn_end(slash_rig: Rig) -> None:
    slash_rig.keys("first" + ENTER)
    slash_rig.host.wait_for("submit")
    slash_rig.feed(slash_rig.host.started("t1", 1))
    slash_rig.keys("/queue then this" + ENTER)
    slash_rig.settle(6)
    assert submitted(slash_rig) == ["first"]
    slash_rig.feed(slash_rig.host.done("t1"))
    assert submitted(slash_rig) == ["first", "then this"]


def test_reasoning_display_and_yolo_reach_the_repl(slash_rig: Rig, scope: Scope) -> None:
    slash_rig.keys("/reasoning show" + ENTER)
    slash_rig.settle(6)
    assert slash_rig.repl.renderer.show_reasoning is True
    slash_rig.keys("/yolo" + ENTER)
    slash_rig.settle(6)
    assert slash_rig.repl.auto_approve is True


def test_questions_outside_a_terminal_section_answer_no(rig: Rig, ctx: SlashContext) -> None:
    wire_slash(rig.repl, ctx)
    assert ctx.confirm("Start over?") is False
    request = ApprovalRequest(
        request_id="r1",
        session_id=SESSION,
        command="rm -r build",
        description="deletes a folder",
        choices=[ApprovalChoice.ONCE, ApprovalChoice.DENY],
    )
    assert ctx.ask_approval is not None and ctx.ask_approval(request) is None


def test_approval_card_reads_digits_from_the_terminal(rig: Rig) -> None:
    typed = iter(["9", "1"])
    ask = slash_bridge.approval_with(rig.repl, lambda _prompt: next(typed))
    request = ApprovalRequest(
        request_id="r1",
        session_id=SESSION,
        command="chmod +x run.sh && ./run.sh",
        description="marks a file executable and runs it",
        choices=[ApprovalChoice.ONCE, ApprovalChoice.DENY],
    )
    answer = ask(request)
    assert answer is not None and answer.choice is ApprovalChoice.ONCE
    assert "chmod +x run.sh" in rig.text()


def test_confirm_reads_yes(rig: Rig) -> None:
    assert slash_bridge.confirm_with(lambda _p: "y")("Go?") is True
    assert slash_bridge.confirm_with(lambda _p: "")("Go?") is False
    assert slash_bridge.confirm_with(lambda _p: None)("Go?") is False


def test_completer_offers_command_names_once() -> None:
    completer = SlashCompleter(lambda text: ["/queue", "/quit", "/queue"])
    found = list(completer.get_completions(Document("/qu"), CompleteEvent()))
    assert [(c.text, c.start_position) for c in found] == [("/queue", -3), ("/quit", -3)]
    assert list(completer.get_completions(Document("plain"), CompleteEvent())) == []


def test_completer_takes_registry_candidates_and_replaces_the_last_word() -> None:
    completer = SlashCompleter(lambda text: [Candidate("show"), Candidate("hide", "note")])
    found = list(completer.get_completions(Document("/reasoning sh"), CompleteEvent()))
    assert [(c.text, c.start_position) for c in found] == [("show", -2), ("hide", -2)]


def test_wire_slash_sets_a_completer_from_the_registry(ctx: SlashContext, rig: Rig) -> None:
    repl = rig.repl
    wire_slash(repl, ctx)
    assert repl.completer is not None
    found = list(repl.completer.get_completions(Document("/hi"), CompleteEvent()))
    assert [c.text for c in found] == ["/history"]
    sub = list(repl.completer.get_completions(Document("/reasoning s"), CompleteEvent()))
    assert "show" in [c.text for c in sub]
