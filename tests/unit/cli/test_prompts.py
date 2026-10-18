"""P07-01: wizard prompts: keys, non-TTY refusal, back navigation, paste clean-up."""

from __future__ import annotations

import io as stdio
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from typing import Any

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.vt100 import Vt100_Output

from indus.cli import prompts
from indus.cli.prompts import (
    Back,
    Cancelled,
    Interactive,
    NonInteractive,
    TerminalDriver,
    ask_checklist,
    ask_choice,
    ask_secret,
    ask_text,
    ask_yes_no,
    run_steps,
)
from tests.fakes.prompts import BACK, ScriptedDriver, scripted

LEFT, UP, DOWN, ENTER = "\x1b[D", "\x1b[A", "\x1b[B", "\r"
CTRL_C, CTRL_D, BACKSPACE = "\x03", "\x04", "\x7f"


@pytest.fixture
def stack() -> Iterator[ExitStack]:
    with ExitStack() as opened:
        yield opened


def keyboard(stack: ExitStack, *per_prompt: str, screen: Any = None) -> Interactive:
    """An ``Interactive`` whose terminal driver reads each prompt's keys from its own pipe."""
    queue: list[str] = list(per_prompt)

    def next_input() -> Any:
        pipe = stack.enter_context(create_pipe_input())
        pipe.send_text(queue.pop(0))
        return pipe

    driver = TerminalDriver(input_factory=next_input, output=screen or DummyOutput())
    lines: list[str] = []
    return Interactive(tty=True, driver=driver, say=lines.append)


def recording_screen() -> tuple[Vt100_Output, stdio.StringIO]:
    buffer = stdio.StringIO()
    return Vt100_Output(buffer, lambda: Size(rows=24, columns=80), term="xterm"), buffer


# -- prompt_toolkit driver ----------------------------------------------------------------------


def test_secret_is_masked(stack: ExitStack) -> None:
    secret = "abc" + "123" + "xyz"
    screen, seen = recording_screen()
    io = keyboard(stack, secret + "Q" + BACKSPACE + LEFT + ENTER, screen=screen)
    assert ask_secret(io, "API key") == secret
    drawn = seen.getvalue()
    assert "*" * len(secret) in drawn
    assert secret not in drawn


def test_ctrl_d_ends_secret(stack: ExitStack) -> None:
    io = keyboard(stack, "partial" + CTRL_D)
    assert ask_secret(io, "API key") == "partial"


def test_ctrl_c_cancels_text_and_list(stack: ExitStack) -> None:
    io = keyboard(stack, "half" + CTRL_C, DOWN + CTRL_C)
    with pytest.raises(Cancelled):
        ask_text(io, "Name")
    with pytest.raises(Cancelled):
        ask_choice(io, "Pick", ["a", "b"])


def test_text_default_and_choice_keys(stack: ExitStack) -> None:
    io = keyboard(stack, ENTER, DOWN + DOWN + ENTER, "3" + ENTER, " " + DOWN + " " + ENTER)
    assert ask_text(io, "Name", "kept") == "kept"
    assert ask_choice(io, "Pick", ["a", "b", "c"]) == 2
    assert ask_choice(io, "Pick", ["a", "b", "c"]) == 2
    assert ask_checklist(io, "Tools", ["x", "y", "z"], checked=[2]) == [0, 1, 2]


def test_left_goes_back_only_on_an_empty_line(stack: ExitStack) -> None:
    io = keyboard(stack, "ab" + LEFT + "X" + ENTER, LEFT, UP + LEFT)
    io.nav = prompts.Navigation(section=1)
    assert ask_text(io, "Name") == "aXb"
    with pytest.raises(Back) as first:
        ask_text(io, "Next")
    assert first.value.index == 1
    with pytest.raises(Back):
        ask_choice(io, "Pick", ["a", "b"])


def test_left_is_a_cursor_key_outside_navigation(stack: ExitStack) -> None:
    io = keyboard(stack, LEFT + "ok" + ENTER)
    assert ask_text(io, "Name") == "ok"


def test_closed_input_cancels(stack: ExitStack) -> None:
    pipe = stack.enter_context(create_pipe_input())
    pipe.close()
    io = Interactive(tty=True, driver=TerminalDriver(input_factory=lambda: pipe))
    with pytest.raises(Cancelled):
        ask_text(io, "Name")


# -- helpers above the driver -------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda io: ask_text(io, "q"),
        lambda io: ask_secret(io, "q"),
        lambda io: ask_yes_no(io, "q"),
        lambda io: ask_choice(io, "q", ["a"]),
        lambda io: ask_checklist(io, "q", ["a"]),
    ],
)
def test_no_terminal_raises_with_guidance(call: Callable[[Interactive], object]) -> None:
    driver = ScriptedDriver([])
    io = Interactive(tty=False, guidance="run indus config set ...", driver=driver)
    with pytest.raises(NonInteractive) as raised:
        call(io)
    assert raised.value.guidance == "run indus config set ..."
    assert driver.asked == []


def test_for_terminal_needs_both_ends() -> None:
    io = Interactive.for_terminal(
        stdin_isatty=lambda: True, stdout_isatty=lambda: False, env={"TERM": "dumb"}
    )
    assert not io.tty and io.dumb


def test_yes_no_words() -> None:
    lines: list[str] = []
    io = scripted("", "maybe", "N", "yes", lines=lines)
    assert ask_yes_no(io, "Go?") is True
    assert ask_yes_no(io, "Go?") is False
    assert ask_yes_no(io, "Go?", default=False) is True
    assert lines == ["Please answer y or n."]


def test_validation_asks_again() -> None:
    lines: list[str] = []
    io = scripted("x", "12", lines=lines)
    got = ask_text(io, "Number", validate=lambda s: None if s.isdigit() else "digits only")
    assert got == "12" and lines == ["digits only"]


def test_numbered_menus_on_a_dumb_terminal() -> None:
    lines: list[str] = []
    io = scripted("9", "2", "", "1,3", "", lines=lines)
    io.dumb = True
    assert ask_choice(io, "Pick", ["a", "b"]) == 1
    assert ask_choice(io, "Pick", ["a", "b"], default=1) == 1
    assert ask_checklist(io, "Tools", ["x", "y", "z"], checked=[1]) == [0, 1, 2]
    assert "  1. a" in lines and "Please enter a number from 1 to 2." in lines


@pytest.mark.parametrize(
    ("raw", "key", "clean"),
    [
        ("\x1b[200~pasted\x1b[201~", False, "pasted"),
        ("line\r", False, "line"),
        ('  "abc-123"  ', True, "abc-123"),
        ("'abc'", True, "abc"),
        ("\x1b[200~ab\ncd\x1b[201~\r\n", True, "abcd"),
        ('"', True, '"'),
        ("a'b'", True, "a'b'"),
    ],
)
def test_sanitise_table(raw: str, key: bool, clean: str) -> None:
    assert prompts.sanitise(raw, key=key) == clean


# -- navigation -------------------------------------------------------------------------------


def test_back_within_a_section_replays() -> None:
    io = scripted("a1", "b1", BACK, "a2", "b2", "c2")
    driver = io.driver
    assert isinstance(driver, ScriptedDriver)
    got: list[tuple[str, str, str]] = []

    def section() -> None:
        a = ask_text(io, "A")
        b = ask_text(io, "B")
        c = ask_text(io, "C")
        got.append((a, b, c))

    run_steps(io, [("Only", section)])
    # Back at C re-asks B (A is replayed silently); the new B answer drops the old later ones.
    assert driver.asked == ["A: ", "B: ", "C: ", "B: ", "C: "]
    assert got == [("a1", "a2", "b2")]


def test_back_from_first_prompt_goes_to_previous_section() -> None:
    io = scripted("one", "two", BACK, "two-again", "three")
    driver = io.driver
    assert isinstance(driver, ScriptedDriver)
    seen: dict[str, list[str]] = {"first": [], "second": []}

    def first() -> None:
        seen["first"].append(ask_text(io, "X") + "+" + ask_text(io, "Y"))

    def second() -> None:
        seen["second"].append(ask_text(io, "Z"))

    run_steps(io, [("First", first), ("Second", second)])
    assert driver.asked == ["X: ", "Y: ", "Z: ", "Y: ", "Z: "]
    assert seen == {"first": ["one+two", "one+two-again"], "second": ["three"]}


def test_no_back_on_the_very_first_prompt() -> None:
    flags: list[bool] = []

    class Spy(ScriptedDriver):
        def text(self, question: str, *, secret: bool, allow_back: bool) -> str:
            flags.append(allow_back)
            return super().text(question, secret=secret, allow_back=allow_back)

    io = Interactive(tty=True, driver=Spy(["a", "b"]), say=lambda _l: None)

    def section() -> None:
        ask_text(io, "A")
        ask_text(io, "B")

    run_steps(io, [("S", section)])
    assert flags == [False, True]
    assert io.nav is None


def test_cancel_passes_through_run_steps() -> None:
    io = scripted("a")

    def section() -> None:
        ask_text(io, "A")
        ask_text(io, "B")

    with pytest.raises(Exception, match="no scripted answer"):
        run_steps(io, [("S", section)])
    io2 = Interactive(tty=True, driver=ScriptedDriver([]), say=lambda _l: None)

    def cancelled() -> None:
        raise Cancelled()

    with pytest.raises(Cancelled):
        run_steps(io2, [("S", cancelled)])
    assert io2.nav is None


def test_replay_stops_where_the_flow_changed() -> None:
    io = scripted("a1", "b1", "c1", BACK, "kept", "c2", "d2")
    driver = io.driver
    assert isinstance(driver, ScriptedDriver)
    runs: list[int] = []

    def section() -> None:
        runs.append(1)
        ask_text(io, "A")
        # On the second run an earlier save changed the flow: another question comes second.
        ask_text(io, "Keep it?" if len(runs) > 1 else "B")
        ask_text(io, "C")
        ask_text(io, "D")

    run_steps(io, [("Only", section)])
    assert driver.asked == ["A: ", "B: ", "C: ", "D: ", "Keep it?: ", "C: ", "D: "]
