"""REPL core (P06-04): keys, Ctrl+C priority (R05.5), busy-input modes, the feed, history."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
from prompt_toolkit.keys import Keys

from indus.cli.repl.app import Repl, ReplOptions
from indus.cli.repl.history import InputHistory
from indus.cli.repl.keys import (
    CtrlC,
    CtrlCPolicy,
    busy_mode,
    continue_line,
    continues_line,
    map_shift_enter,
    paste_preview,
)
from indus.foundation import config
from indus.foundation.scope import Scope
from indus.protocol.errors import SESSION_BUSY
from indus.protocol.events import ExitReason
from indus.protocol.methods import ProtocolError, RedirectOutcome
from tests.fakes.clock import FakeClock
from tests.fakes.service import SESSION
from tests.unit.cli.repl.conftest import Rig

CTRL_C = "\x03"
CTRL_D = "\x04"
ENTER = "\r"
ALT_ENTER = "\x1b\r"
CTRL_J = "\n"


def submitted(rig: Rig) -> list[str]:
    return [call.args[1] for call in rig.host.named("submit")]


# -- pure helpers ---------------------------------------------------------------------------------


def test_ctrl_c_policy_order(fake_clock: FakeClock) -> None:
    policy = CtrlCPolicy(fake_clock)
    assert policy.press(card_open=True, draft=True, busy=True) is CtrlC.CANCEL_CARD
    assert policy.press(card_open=False, draft=True, busy=True) is CtrlC.CLEAR_DRAFT
    assert policy.press(card_open=False, draft=False, busy=True) is CtrlC.INTERRUPT
    fake_clock.advance(1.9)
    assert policy.press(card_open=False, draft=False, busy=True) is CtrlC.EXIT


def test_ctrl_c_policy_idle_needs_two_presses_within_the_window(fake_clock: FakeClock) -> None:
    policy = CtrlCPolicy(fake_clock)
    assert policy.press(card_open=False, draft=False, busy=False) is CtrlC.HINT
    fake_clock.advance(2.0)
    assert policy.press(card_open=False, draft=False, busy=False) is CtrlC.HINT
    fake_clock.advance(0.5)
    assert policy.press(card_open=False, draft=False, busy=False) is CtrlC.EXIT


def test_clearing_a_draft_does_not_count_towards_leaving(fake_clock: FakeClock) -> None:
    policy = CtrlCPolicy(fake_clock)
    assert policy.press(card_open=False, draft=True, busy=False) is CtrlC.CLEAR_DRAFT
    assert policy.press(card_open=False, draft=False, busy=False) is CtrlC.HINT


@pytest.mark.parametrize(
    ("value", "mode"),
    [
        ("interrupt", "interrupt"),
        ("queue", "queue"),
        (" Steer ", "steer"),
        ("redirect", "interrupt"),
        (None, "interrupt"),
        (3, "interrupt"),
    ],
)
def test_busy_mode_normalised(value: object, mode: str) -> None:
    assert busy_mode(value) == mode


def test_backslash_continuation_helpers() -> None:
    assert continues_line("first \\")
    assert continues_line("first \\  ")
    assert not continues_line("a \\ b")
    assert continue_line("first \\ ") == "first \n"


def test_paste_preview_only_for_several_lines() -> None:
    assert paste_preview("one line") is None
    assert paste_preview("\nfirst\nsecond\n") == "pasted 4 lines: first"


def test_shift_enter_sequences_read_as_newline() -> None:
    map_shift_enter()
    assert ANSI_SEQUENCES["\x1b[13;2u"] == (Keys.Escape, Keys.ControlM)
    assert ANSI_SEQUENCES["\x1b[27;2;13~"] == (Keys.Escape, Keys.ControlM)


# -- Ctrl+C / Ctrl+D through pipe input -----------------------------------------------------------


def test_ctrl_c_clears_draft_then_interrupts_then_exits_within_two_seconds(
    rig: Rig, fake_clock: FakeClock
) -> None:
    rig.feed(rig.host.started("t1", generation=7))
    rig.keys("half-typed thought")
    rig.settle()
    rig.keys(CTRL_C)  # 1: the draft goes, the turn keeps running
    rig.settle()
    assert rig.buffer_text() == ""
    assert rig.host.named("interrupt") == []
    rig.keys(CTRL_C)  # 2: interrupt this turn's generation
    call = rig.host.wait_for("interrupt")[0]
    assert call.args == (SESSION, 7)
    assert rig.thread.is_alive()
    fake_clock.advance(1.5)
    rig.keys(CTRL_C)  # 3: within 2 s of the last one -> leave
    assert rig.wait_exit() == 0
    assert submitted(rig) == []


def test_ctrl_c_presses_more_than_two_seconds_apart_do_not_exit(
    rig: Rig, fake_clock: FakeClock
) -> None:
    rig.keys(CTRL_C)
    rig.settle()
    fake_clock.advance(2.5)
    rig.keys(CTRL_C)
    rig.settle()
    assert rig.thread.is_alive()
    assert rig.text().count("press Ctrl+C again to quit") == 2
    rig.keys(CTRL_C)
    assert rig.wait_exit() == 0


def test_ctrl_c_interrupt_seals_the_partial_reply(rig: Rig) -> None:
    rig.feed(rig.host.started("t1", 1), rig.host.delta("half a sen", "t1"))
    rig.keys(CTRL_C)
    rig.host.wait_for("interrupt")
    rig.settle()
    rig.feed(rig.host.delta("tence that arrives late", "t1"))
    text = rig.text()
    assert "half a sen" in text and "(interrupted)" in text
    assert "arrives late" not in text


def test_ctrl_d_with_text_does_nothing(rig: Rig) -> None:
    rig.keys("keep me" + CTRL_D)
    rig.settle()
    assert rig.thread.is_alive()
    assert rig.buffer_text() == "keep me"
    rig.keys(ENTER)
    rig.host.wait_for("submit")
    assert submitted(rig) == ["keep me"]
    rig.keys(CTRL_D)
    assert rig.wait_exit() == 0


# -- composing ------------------------------------------------------------------------------------


def test_alt_enter_and_ctrl_j_insert_newlines(rig: Rig) -> None:
    rig.keys("one" + ALT_ENTER + "two" + CTRL_J + "three" + ENTER)
    rig.host.wait_for("submit")
    assert submitted(rig) == ["one\ntwo\nthree"]


def test_trailing_backslash_continues_the_draft(rig: Rig) -> None:
    rig.keys("first \\" + ENTER)
    rig.settle()
    assert rig.host.named("submit") == []
    assert rig.buffer_text() == "first \n"
    rig.keys("second" + ENTER)
    rig.host.wait_for("submit")
    assert submitted(rig) == ["first \nsecond"]


def test_empty_enter_sends_nothing(rig: Rig) -> None:
    rig.keys(ENTER + "   " + ENTER)
    rig.settle()
    assert rig.host.named("submit") == []


def test_bracketed_paste_keeps_text_and_prints_a_preview(rig: Rig) -> None:
    rig.keys("\x1b[200~line one\nline two\nline three\x1b[201~")
    rig.settle()
    assert rig.buffer_text() == "line one\nline two\nline three"
    assert "pasted 3 lines: line one" in rig.text()


def test_every_line_goes_to_the_command_hook_first(make_rig: Callable[..., Rig]) -> None:
    seen: list[str] = []

    def hook(repl: Repl, line: str) -> bool:
        seen.append(line)
        return line[:1] in "/!" and not line.startswith("/pass")

    rig = make_rig(commands=hook)
    rig.keys("/help" + ENTER + "!ls -la" + ENTER + "/pass along" + ENTER)
    rig.host.wait_for("submit")
    rig.feed(rig.host.started("t1", 1), rig.host.done("t1"))
    rig.keys("plain" + ENTER)
    rig.host.wait_for("submit", 2)
    assert seen == ["/help", "!ls -la", "/pass along", "plain"]
    assert submitted(rig) == ["/pass along", "plain"]


def test_without_a_hook_quit_leaves(rig: Rig) -> None:
    rig.keys("/quit" + ENTER)
    assert rig.wait_exit() == 0
    assert rig.host.named("submit") == []


def test_a_failing_hook_prints_an_error_and_the_repl_stays(make_rig: Callable[..., Rig]) -> None:
    def hook(repl: Repl, line: str) -> bool:
        raise RuntimeError("boom")

    rig = make_rig(commands=hook)
    rig.keys("/explode" + ENTER)
    rig.settle()
    assert "that command failed: RuntimeError: boom" in rig.text()
    assert rig.thread.is_alive()


# -- busy input -----------------------------------------------------------------------------------


def busy_rig(make_rig: Callable[..., Rig], mode: str, *, seen: bool = True) -> Rig:
    options = ReplOptions(busy_mode=busy_mode(mode), busy_hint_seen=seen, refresh_interval=60.0)
    rig = make_rig(options=options)
    rig.keys("start" + ENTER)
    rig.host.wait_for("submit")
    rig.feed(rig.host.started("t1", 1))
    return rig


def test_busy_interrupt_mode_redirects(make_rig: Callable[..., Rig]) -> None:
    rig = busy_rig(make_rig, "interrupt")
    rig.keys("actually use python" + ENTER)
    call = rig.host.wait_for("redirect")[0]
    assert call.args == (SESSION, "actually use python")
    rig.settle()
    assert rig.host.named("interrupt") == []
    assert len(rig.host.named("submit")) == 1


def test_busy_interrupt_mode_falls_back_to_interrupt_and_sends_next(
    make_rig: Callable[..., Rig],
) -> None:
    rig = busy_rig(make_rig, "interrupt")
    rig.host.redirect_outcome = RedirectOutcome.REJECTED
    rig.keys("stop and do this" + ENTER)
    assert rig.host.wait_for("interrupt")[0].args == (SESSION, 1)
    rig.settle()
    assert submitted(rig) == ["start"]
    rig.feed(rig.host.done("t1", ExitReason.INTERRUPTED_BY_USER))
    assert submitted(rig) == ["start", "stop and do this"]


def test_unknown_busy_mode_behaves_as_interrupt(make_rig: Callable[..., Rig]) -> None:
    rig = busy_rig(make_rig, "sideways")
    rig.keys("hello" + ENTER)
    rig.host.wait_for("redirect")


def test_busy_queue_mode_sends_after_the_turn_ends(make_rig: Callable[..., Rig]) -> None:
    rig = busy_rig(make_rig, "queue")
    rig.keys("second" + ENTER + "third" + ENTER)
    rig.settle()
    assert submitted(rig) == ["start"]
    assert list(rig.repl.pending) == ["second", "third"]
    rig.feed(rig.host.done("t1"))
    assert submitted(rig) == ["start", "second"]
    rig.feed(rig.host.started("t2", 2), rig.host.done("t2"))
    assert submitted(rig) == ["start", "second", "third"]
    assert rig.host.named("redirect") == [] and rig.host.named("steer") == []


def test_busy_steer_mode_steers(make_rig: Callable[..., Rig]) -> None:
    rig = busy_rig(make_rig, "steer")
    rig.keys("mind the tests" + ENTER)
    assert rig.host.wait_for("steer")[0].args == (SESSION, "mind the tests")
    rig.settle()
    assert list(rig.repl.pending) == []


def test_busy_steer_mode_queues_when_refused(make_rig: Callable[..., Rig]) -> None:
    rig = busy_rig(make_rig, "steer")
    rig.host.steer_accepts = False
    rig.keys("later please" + ENTER)
    rig.host.wait_for("steer")
    rig.settle()
    assert list(rig.repl.pending) == ["later please"]
    rig.feed(rig.host.done("t1"))
    assert submitted(rig) == ["start", "later please"]


def test_busy_hint_shown_once_and_remembered(make_rig: Callable[..., Rig], scope: Scope) -> None:
    rig = busy_rig(make_rig, "queue", seen=False)
    rig.keys("a" + ENTER + "b" + ENTER)
    rig.settle()
    assert rig.text().count("tip: a message sent during a reply waits") == 1
    assert config.load(scope).get("onboarding.seen.busy_input") is True
    assert ReplOptions.from_scope(scope).busy_hint_seen is True


def test_submit_refused_as_busy_is_kept_for_later(rig: Rig) -> None:
    rig.host.submit_error = ProtocolError(SESSION_BUSY)
    rig.keys("go" + ENTER)
    rig.host.wait_for("submit")
    rig.settle()
    assert list(rig.repl.pending) == ["go"]
    rig.feed(rig.host.started("t9", 9), rig.host.done("t9"))
    assert submitted(rig) == ["go", "go"]


# -- feed, history, options -----------------------------------------------------------------------


def test_feed_items_reach_the_screen_through_the_pump(rig: Rig) -> None:
    rig.host.emit(rig.host.started("t1", 1))
    rig.host.emit(rig.host.delta("from the feed\n", "t1"))
    rig.host.emit(rig.host.done("t1"))
    for _ in range(50):
        rig.settle()
        if "from the feed" in rig.text() and not rig.call(lambda: rig.repl.busy):
            break
    assert "from the feed" in rig.text()
    assert rig.call(lambda: rig.repl.busy) is False


def test_attach_switches_the_feed(rig: Rig) -> None:
    rig.call(lambda: rig.repl.attach("20260101_090000_other1"))
    assert [c.args[0] for c in rig.host.named("feed")] == [SESSION, "20260101_090000_other1"]
    assert [c.args[0] for c in rig.host.named("detach")] == [SESSION]
    assert rig.repl.session_id == "20260101_090000_other1"


def test_input_history_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "home" / "input_history"
    history = InputHistory(path, limit=3)
    for entry in ("one", "two\nlines", "three", "four"):
        history.store_string(entry)
    assert path.stat().st_mode & 0o777 == 0o600
    again = InputHistory(path, limit=3)
    assert list(again.load_history_strings()) == ["four", "three", "two\nlines"]


def test_history_file_is_used_by_the_repl(make_rig: Callable[..., Rig], scope: Scope) -> None:
    options = ReplOptions.from_scope(scope, refresh_interval=60.0)
    rig = make_rig(options=options)
    rig.keys("remember this" + ENTER)
    rig.host.wait_for("submit")
    rig.settle()
    assert '"remember this"' in (scope.home / "input_history").read_text(encoding="utf-8")


def test_options_from_config(scope: Scope) -> None:
    config.save(
        scope,
        {
            "display": {
                "busy_input_mode": "steer",
                "final_response_markdown": "strip",
                "cli_refresh_interval": 0.01,
            }
        },
    )
    options = ReplOptions.from_scope(scope)
    assert options.busy_mode == "steer"
    assert options.final_markdown == "strip"
    assert options.refresh_interval == 0.1
    assert options.history_path == scope.home / "input_history"
    assert options.busy_hint_seen is False


def test_ctrl_j_sends_when_multiline_shortcuts_are_off(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig(options=ReplOptions(multiline_shortcuts=False, refresh_interval=60.0))
    rig.keys("one" + ALT_ENTER + "two" + CTRL_J)
    rig.host.wait_for("submit")
    assert submitted(rig) == ["one\ntwo"]
