"""Keys of the classic REPL (P06-04).

- Enter sends; a line ending in ``\\`` (spaces after it allowed) becomes a newline instead.
- Alt+Enter and Ctrl+J insert a newline (``display.cli_multiline_shortcuts: false`` makes Ctrl+J
  send instead, the older habit); so does Shift+Enter when the terminal sends one of the
  two extended forms (kitty ``CSI 13;2u``, xterm ``CSI 27;2;13~``) — we map them but never ask the
  terminal to switch protocols.
- Ctrl+G and Ctrl+X Ctrl+E open the draft in ``$EDITOR``.
- Ctrl+D leaves on an empty draft; with text it does nothing. Ctrl+Z suspends (Unix).
- Ctrl+C follows ``CtrlCPolicy`` (R05.5): a card first, then the draft, then the running turn,
  then leaving — which takes a second press within ``EXIT_WINDOW`` seconds.
- While a card that takes quick picks is open, a digit on an empty line is a pick (R05.11).
- A bracketed paste of several lines stays in the draft; the REPL prints a one-line preview.
"""

from __future__ import annotations

import re
import signal
from enum import StrEnum
from typing import Literal, Protocol

from prompt_toolkit.filters import Condition
from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.keys import Keys

from indus.foundation.clock import Clock

__all__ = [
    "BUSY_MODES",
    "EXIT_WINDOW",
    "BusyMode",
    "CtrlC",
    "CtrlCPolicy",
    "KeyTarget",
    "build_key_bindings",
    "busy_mode",
    "continue_line",
    "continues_line",
    "map_shift_enter",
    "paste_preview",
]

EXIT_WINDOW = 2.0
"""Seconds within which a second Ctrl+C leaves."""

type BusyMode = Literal["interrupt", "queue", "steer"]
BUSY_MODES: tuple[BusyMode, ...] = ("interrupt", "queue", "steer")

_CONTINUATION = re.compile(r"\\[ \t]*$")
SHIFT_ENTER_SEQUENCES = ("\x1b[13;2u", "\x1b[27;2;13~")


def busy_mode(value: object) -> BusyMode:
    """``display.busy_input_mode`` normalised; anything unknown means ``interrupt``."""
    text = value.strip().lower() if isinstance(value, str) else ""
    for mode in BUSY_MODES:
        if text == mode:
            return mode
    return "interrupt"


def continues_line(text: str) -> bool:
    """True when Enter should turn the trailing backslash into a newline."""
    return bool(_CONTINUATION.search(text))


def continue_line(text: str) -> str:
    return _CONTINUATION.sub("", text) + "\n"


def paste_preview(text: str, width: int = 60) -> str | None:
    """The one-line note for a pasted block of several lines, else None."""
    lines = text.split("\n")
    if len(lines) < 2:
        return None
    first = next((line.strip() for line in lines if line.strip()), "")
    if len(first) > width:
        first = first[: width - 1] + "…"
    return f"pasted {len(lines)} lines: {first}"


def map_shift_enter() -> None:
    """Read the extended Shift+Enter forms as Alt+Enter (a newline), replacing prompt_toolkit's
    own reading of one of them as plain Enter. Idempotent."""
    for sequence in SHIFT_ENTER_SEQUENCES:
        ANSI_SEQUENCES[sequence] = (Keys.Escape, Keys.ControlM)


class CtrlC(StrEnum):
    """What a Ctrl+C press does."""

    CANCEL_CARD = "cancel_card"
    CLEAR_DRAFT = "clear_draft"
    INTERRUPT = "interrupt"
    EXIT = "exit"
    HINT = "hint"


class CtrlCPolicy:
    """The Ctrl+C priority order (R05.5): card → draft → running turn → leave.

    Leaving needs a press within ``window`` seconds of the previous one that reached the turn or
    leave steps; a lone press while idle only earns a hint. Cancelling a card or clearing a draft
    does not count towards the double press.
    """

    def __init__(self, clock: Clock, window: float = EXIT_WINDOW) -> None:
        self.clock = clock
        self.window = window
        self._last: float | None = None

    def press(self, *, card_open: bool, draft: bool, busy: bool) -> CtrlC:
        if card_open:
            return CtrlC.CANCEL_CARD
        if draft:
            return CtrlC.CLEAR_DRAFT
        now = self.clock.monotonic()
        recent = self._last is not None and now - self._last < self.window
        self._last = now
        if recent:
            return CtrlC.EXIT
        return CtrlC.INTERRUPT if busy else CtrlC.HINT


class KeyTarget(Protocol):
    """What the key bindings drive (the REPL)."""

    def card_takes_digits(self) -> bool: ...

    @property
    def multiline_shortcuts(self) -> bool: ...

    def key_enter(self, event: KeyPressEvent) -> None: ...

    def key_ctrl_c(self, event: KeyPressEvent) -> None: ...

    def key_digit(self, event: KeyPressEvent, digit: int) -> None: ...

    def key_paste(self, event: KeyPressEvent) -> None: ...


def _newline(event: KeyPressEvent) -> None:
    event.current_buffer.insert_text("\n")


def _ctrl_d(event: KeyPressEvent) -> None:
    if not event.current_buffer.text:
        event.app.exit(result=0)


def _suspend(event: KeyPressEvent) -> None:
    if hasattr(signal, "SIGTSTP"):
        event.app.suspend_to_background()


def _editor(event: KeyPressEvent) -> None:
    event.current_buffer.open_in_editor(validate_and_handle=False)


def build_key_bindings(target: KeyTarget) -> KeyBindings:
    """The REPL's key bindings (see the module notes)."""
    map_shift_enter()
    kb = KeyBindings()
    kb.add("enter", eager=True)(target.key_enter)
    kb.add("escape", "enter", eager=True)(_newline)
    if target.multiline_shortcuts:
        kb.add("c-j", eager=True)(_newline)
    else:
        kb.add("c-j", eager=True)(target.key_enter)
    kb.add("c-c", eager=True)(target.key_ctrl_c)
    kb.add("c-d", eager=True)(_ctrl_d)
    kb.add("c-z", eager=True)(_suspend)
    kb.add("c-g")(_editor)
    kb.add("c-x", "c-e")(_editor)
    kb.add(Keys.BracketedPaste, eager=True)(target.key_paste)

    picking = Condition(lambda: target.card_takes_digits())
    for digit in range(1, 10):

        def pick(event: KeyPressEvent, digit: int = digit) -> None:
            target.key_digit(event, digit)

        kb.add(str(digit), filter=picking, eager=True)(pick)
    return kb
