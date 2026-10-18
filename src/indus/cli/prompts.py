"""Questions for interactive commands: text, secrets, yes/no, one choice, a checklist.

Every helper takes an ``Interactive``. Without a terminal any helper raises ``NonInteractive``
carrying the command's guidance text, so the command prints it and exits 1 instead of waiting
for an answer nobody can type (R04.19). Ctrl+C raises ``Cancelled``.

The drawing and key handling sit behind a ``PromptDriver``; ``TerminalDriver`` uses
prompt_toolkit (a masked line shows one ``*`` per character; lists move with the arrow keys).
On a dumb terminal lists become numbered menus answered with a line prompt.

Back navigation: ``run_steps(io, [(label, fn), ...])`` runs sections in order. Inside it the left
arrow (on an empty line, or anywhere in a list) goes back. On the first prompt of a section it
returns to the previous section's last prompt; on a later prompt the section starts again with
the earlier answers replayed silently, so the prompt before the current one is asked again. An
answer is replayed only to the same question; when a save made earlier in the section changed
the flow, replay stops there and the remaining questions are asked again.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import click

if TYPE_CHECKING:
    from indus.cli.args import CliState

__all__ = [
    "Back",
    "BackPressed",
    "Cancelled",
    "Interactive",
    "Navigation",
    "NonInteractive",
    "PromptDriver",
    "TerminalDriver",
    "ask_checklist",
    "ask_choice",
    "ask_secret",
    "ask_text",
    "ask_yes_no",
    "confirm",
    "io_factory",
    "io_for",
    "run_steps",
    "sanitise",
]


class Cancelled(Exception):
    """The person pressed Ctrl+C (or Escape in a list), or input ended."""


class BackPressed(Exception):
    """Raised by a driver when the left arrow asks to go back (helpers turn it into ``Back``)."""


class Back(Exception):
    """Go back from the prompt with this index in the current section (see ``run_steps``)."""

    def __init__(self, index: int) -> None:
        super().__init__(index)
        self.index = index


class NonInteractive(Exception):
    """No terminal to ask on; ``guidance`` says what to run instead."""

    def __init__(self, guidance: str) -> None:
        super().__init__(guidance)
        self.guidance = guidance


# -- text clean-up ----------------------------------------------------------------------------

_PASTE_MARKERS = re.compile(r"\x1b\[\s*20[01]~")
_QUOTES = ("'", '"')


def sanitise(text: str, *, key: bool = False) -> str:
    """Remove bracketed-paste markers and carriage returns; for keys also line breaks, outer
    whitespace and one pair of surrounding quotes."""
    out = _PASTE_MARKERS.sub("", text).replace("\r", "")
    if not key:
        return out
    out = out.replace("\n", "").strip()
    if len(out) >= 2 and out[0] == out[-1] and out[0] in _QUOTES:
        out = out[1:-1].strip()
    return out


# -- drivers -----------------------------------------------------------------------------------


class PromptDriver(Protocol):
    """Draws one question and returns the raw answer. Raises ``Cancelled`` or ``BackPressed``."""

    def text(self, question: str, *, secret: bool, allow_back: bool) -> str: ...

    def choose(
        self, question: str, rows: Sequence[str], default: int, *, allow_back: bool
    ) -> int: ...

    def checklist(
        self, question: str, rows: Sequence[str], checked: set[int], *, allow_back: bool
    ) -> list[int]: ...


class TerminalDriver:
    """prompt_toolkit questions. ``input_factory`` / ``output`` are for tests (pipe input)."""

    def __init__(
        self,
        *,
        input_factory: Callable[[], Any] | None = None,
        output: Any = None,
    ) -> None:
        self._input_factory = input_factory
        self._output = output

    def _io(self) -> dict[str, Any]:
        found: dict[str, Any] = {}
        if self._input_factory is not None:
            found["input"] = self._input_factory()
        if self._output is not None:
            found["output"] = self._output
        return found

    def text(self, question: str, *, secret: bool, allow_back: bool) -> str:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.application import get_app
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.key_binding import KeyBindings

        keys = KeyBindings()

        @Condition
        def may_go_back() -> bool:
            return allow_back and not get_app().current_buffer.text

        @keys.add("left", filter=may_go_back)
        def _back(event: Any) -> None:
            event.app.exit(exception=BackPressed())

        @keys.add("c-d")
        def _end(event: Any) -> None:
            event.current_buffer.validate_and_handle()

        @keys.add("c-c")
        def _cancel(event: Any) -> None:
            event.app.exit(exception=Cancelled())

        session: PromptSession[str] = PromptSession(
            question, is_password=secret, key_bindings=keys, **self._io()
        )
        try:
            return session.prompt()
        except (KeyboardInterrupt, EOFError) as exc:
            raise Cancelled() from exc

    def _run_list(
        self,
        question: str,
        rows: Sequence[str],
        cursor: int,
        *,
        checked: set[int] | None,
        allow_back: bool,
    ) -> Any:
        from prompt_toolkit.application import Application
        from prompt_toolkit.formatted_text import StyleAndTextTuples
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import HSplit, Layout, Window
        from prompt_toolkit.layout.controls import FormattedTextControl

        state = {"at": max(0, min(cursor, len(rows) - 1))}
        marks = checked

        def lines() -> StyleAndTextTuples:
            out: StyleAndTextTuples = [("bold", question + "\n")]
            for i, row in enumerate(rows):
                pointer = "> " if i == state["at"] else "  "
                box = "" if marks is None else ("[x] " if i in marks else "[ ] ")
                style = "reverse" if i == state["at"] else ""
                out.append((style, f"{pointer}{box}{row}\n"))
            hint = "Space toggles, Enter confirms" if marks is not None else "Enter chooses"
            back = ", Left goes back" if allow_back else ""
            out.append(("italic", f"  (Up/Down move, {hint}{back}, Ctrl+C cancels)"))
            return out

        keys = KeyBindings()

        @keys.add("up")
        def _up(event: Any) -> None:
            state["at"] = (state["at"] - 1) % len(rows)

        @keys.add("down")
        def _down(event: Any) -> None:
            state["at"] = (state["at"] + 1) % len(rows)

        for digit in "123456789":

            @keys.add(digit)
            def _jump(event: Any) -> None:
                index = int(event.key_sequence[0].key) - 1
                if index < len(rows):
                    state["at"] = index

        if marks is not None:

            @keys.add(" ")
            def _toggle(event: Any) -> None:
                marks.symmetric_difference_update({state["at"]})

        @keys.add("enter")
        def _done(event: Any) -> None:
            event.app.exit(result=sorted(marks) if marks is not None else state["at"])

        if allow_back:

            @keys.add("left")
            def _back(event: Any) -> None:
                event.app.exit(exception=BackPressed())

        @keys.add("c-c")
        @keys.add("escape")
        def _cancel(event: Any) -> None:
            event.app.exit(exception=Cancelled())

        app: Application[Any] = Application(
            layout=Layout(HSplit([Window(FormattedTextControl(lines), wrap_lines=True)])),
            key_bindings=keys,
            full_screen=False,
            erase_when_done=True,
            **self._io(),
        )
        try:
            return app.run()
        except (KeyboardInterrupt, EOFError) as exc:
            raise Cancelled() from exc

    def choose(self, question: str, rows: Sequence[str], default: int, *, allow_back: bool) -> int:
        result: int = self._run_list(question, rows, default, checked=None, allow_back=allow_back)
        return result

    def checklist(
        self, question: str, rows: Sequence[str], checked: set[int], *, allow_back: bool
    ) -> list[int]:
        result: list[int] = self._run_list(
            question, rows, 0, checked=set(checked), allow_back=allow_back
        )
        return result


# -- navigation --------------------------------------------------------------------------------

_NOTHING: Any = object()


@dataclass
class Navigation:
    """Prompt counter and answers of the running section (owned by ``run_steps``)."""

    section: int = 0
    index: int = 0
    answers: list[Any] = field(default_factory=list[Any])
    replay: list[Any] = field(default_factory=list[Any])

    def enter(self, section: int, replay: Sequence[Any]) -> None:
        self.section = section
        self.index = 0
        self.answers = []
        self.replay = copy.deepcopy(list(replay))

    def begin(self, asked: tuple[str, str]) -> tuple[int, Any, bool]:
        """``(prompt index, replayed answer or nothing, may go back)`` for the next prompt.

        ``asked`` is ``(kind, question)``. A recorded answer is replayed only for the same kind
        and question; at the first difference (an earlier save changed the flow) the rest of the
        replay is dropped and the questions are asked again.
        """
        index = self.index
        self.index += 1
        replayed: Any = _NOTHING
        if index < len(self.replay):
            recorded_ask, value = self.replay[index]
            if recorded_ask == asked:
                replayed = copy.deepcopy(value)
            else:
                del self.replay[index:]
        return index, replayed, self.section > 0 or index > 0

    def resolve(self, index: int, asked: tuple[str, str], value: Any) -> None:
        """Record an answer; any later answer of this section is forgotten."""
        self.answers[index:] = [(asked, copy.deepcopy(value))]


def _echo(line: str) -> None:
    click.echo(line)


@dataclass
class Interactive:
    """Where questions go: ``tty`` (both ends a terminal), the driver, and the guidance printed
    when there is no terminal."""

    tty: bool
    guidance: str = ""
    driver: PromptDriver = field(default_factory=TerminalDriver)
    dumb: bool = False
    say: Callable[[str], None] = _echo
    nav: Navigation | None = None

    @classmethod
    def for_terminal(
        cls,
        *,
        stdin_isatty: Callable[[], bool],
        stdout_isatty: Callable[[], bool],
        env: Mapping[str, str],
        guidance: str = "",
    ) -> Interactive:
        tty = stdin_isatty() and stdout_isatty()
        dumb = env.get("TERM", "").strip().lower() == "dumb"
        return cls(tty=tty, guidance=guidance, dumb=dumb)

    def require(self) -> None:
        """Raise ``NonInteractive`` unless there is a terminal."""
        if not self.tty:
            raise NonInteractive(self.guidance)


def _terminal_io(state: CliState, guidance: str) -> Interactive:
    return Interactive.for_terminal(
        stdin_isatty=state.stdin_isatty,
        stdout_isatty=state.stdout_isatty,
        env=state.scope.env,
        guidance=guidance,
    )


io_factory: Callable[[CliState, str], Interactive] = _terminal_io
"""Builds the ``Interactive`` management commands ask on (tests swap in a scripted one)."""


def io_for(state: CliState, guidance: str = "") -> Interactive:
    """The questions of a running command; ``guidance`` is printed when there is no terminal."""
    return io_factory(state, guidance)


def _navigated[T](io: Interactive, kind: str, question: str, ask: Callable[[bool], T]) -> T:
    io.require()
    nav = io.nav
    if nav is None:
        try:
            return ask(False)
        except BackPressed as exc:  # a driver that ignores allow_back
            raise Cancelled() from exc
    asked = (kind, question)
    index, replayed, allow_back = nav.begin(asked)
    if replayed is not _NOTHING:
        nav.resolve(index, asked, replayed)
        result: T = replayed
        return result
    try:
        value = ask(allow_back)
    except BackPressed as exc:
        raise Back(index) from exc
    nav.resolve(index, asked, value)
    return value


def run_steps(io: Interactive, steps: Sequence[tuple[str, Callable[[], None]]]) -> None:
    """Run ``steps`` with back navigation (see the module notes). ``Cancelled`` passes through."""
    previous = io.nav
    nav = Navigation()
    io.nav = nav
    answers: dict[int, list[Any]] = {}
    replay: dict[int, list[Any]] = {}
    at = 0
    try:
        while at < len(steps):
            label, action = steps[at]
            nav.enter(at, replay.pop(at, []))
            try:
                action()
            except Back as back:
                answers[at] = list(nav.answers)
                if back.index > 0:
                    target, keep = at, back.index - 1
                else:
                    target = max(0, at - 1)
                    keep = max(0, len(answers.get(target, [])) - 1)
                replay[target] = answers.get(target, [])[:keep]
                where = label if target == at else steps[target][0]
                io.say(f"Back to {where}.")
                at = target
                continue
            answers[at] = list(nav.answers)
            at += 1
    finally:
        io.nav = previous


# -- helpers -----------------------------------------------------------------------------------


def _label(question: str, default: str | None) -> str:
    return f"{question} [{default}]: " if default else f"{question}: "


def ask_text(
    io: Interactive,
    question: str,
    default: str | None = None,
    *,
    validate: Callable[[str], str | None] | None = None,
) -> str:
    """A line of text; blank keeps ``default``. ``validate`` returns a problem to re-ask with."""

    def ask(allow_back: bool) -> str:
        while True:
            raw = io.driver.text(_label(question, default), secret=False, allow_back=allow_back)
            answer = sanitise(raw).strip() or (default or "")
            problem = validate(answer) if validate is not None else None
            if problem is None:
                return answer
            io.say(problem)

    return _navigated(io, "text", question, ask)


def ask_secret(io: Interactive, question: str) -> str:
    """A secret typed with one ``*`` per character; blank means "none"."""

    def ask(allow_back: bool) -> str:
        raw = io.driver.text(f"{question}: ", secret=True, allow_back=allow_back)
        return sanitise(raw, key=True)

    return _navigated(io, "secret", question, ask)


_YES = frozenset({"y", "yes"})
_NO = frozenset({"n", "no"})


def ask_yes_no(io: Interactive, question: str, default: bool = True) -> bool:
    """``[Y/n]`` or ``[y/N]``; blank keeps the default; anything else asks again."""
    hint = "Y/n" if default else "y/N"

    def ask(allow_back: bool) -> bool:
        while True:
            raw = io.driver.text(f"{question} [{hint}]: ", secret=False, allow_back=allow_back)
            word = sanitise(raw).strip().lower()
            if not word:
                return default
            if word in _YES:
                return True
            if word in _NO:
                return False
            io.say("Please answer y or n.")

    return _navigated(io, "yes_no", question, ask)


def confirm(io: Interactive, question: str, *, assume_yes: bool = False) -> bool | None:
    """True to go ahead, False when the answer is no (or Ctrl+C), None when there is no terminal.

    With ``assume_yes`` (a ``-y`` / ``-f`` flag) nothing is asked. Without a terminal
    ``io.guidance`` goes to stderr and the caller exits 1 without changing anything (R04.19).
    """
    if assume_yes:
        return True
    if not io.tty:
        if io.guidance:
            click.echo(io.guidance, err=True)
        return None
    try:
        return ask_yes_no(io, question, default=False)
    except Cancelled:
        io.say("")
        return False


def _numbered_choice(io: Interactive, question: str, rows: Sequence[str], default: int) -> int:
    io.say(question)
    for i, row in enumerate(rows, 1):
        io.say(f"  {i}. {row}")
    while True:
        raw = io.driver.text(
            f"Choice [1-{len(rows)}] ({default + 1}): ", secret=False, allow_back=False
        )
        word = sanitise(raw).strip()
        if not word:
            return default
        if word.isdigit() and 1 <= int(word) <= len(rows):
            return int(word) - 1
        io.say(f"Please enter a number from 1 to {len(rows)}.")


def ask_choice(io: Interactive, question: str, rows: Sequence[str], default: int = 0) -> int:
    """One row's index (arrow list, or a numbered menu on a dumb terminal)."""
    if not rows:
        raise ValueError("a choice needs at least one row")
    default = max(0, min(default, len(rows) - 1))

    def ask(allow_back: bool) -> int:
        if io.dumb:
            return _numbered_choice(io, question, rows, default)
        picked = io.driver.choose(question, rows, default, allow_back=allow_back)
        io.say(f"{question} {rows[picked]}")
        return picked

    return _navigated(io, "choice", question, ask)


def _numbered_checklist(
    io: Interactive, question: str, rows: Sequence[str], checked: set[int]
) -> list[int]:
    marks = set(checked)
    while True:
        io.say(question)
        for i, row in enumerate(rows, 1):
            io.say(f"  {i}. [{'x' if i - 1 in marks else ' '}] {row}")
        raw = io.driver.text(
            "Numbers to switch (comma-separated), Enter to accept: ", secret=False, allow_back=False
        )
        words = [w.strip() for w in sanitise(raw).split(",") if w.strip()]
        if not words:
            return sorted(marks)
        if all(w.isdigit() and 1 <= int(w) <= len(rows) for w in words):
            marks.symmetric_difference_update({int(w) - 1 for w in words})
            continue
        io.say(f"Please enter numbers from 1 to {len(rows)}.")


def ask_checklist(
    io: Interactive, question: str, rows: Sequence[str], checked: Sequence[int] = ()
) -> list[int]:
    """Indices of the rows left checked (sorted)."""
    start = {i for i in checked if 0 <= i < len(rows)}

    def ask(allow_back: bool) -> list[int]:
        if io.dumb:
            return _numbered_checklist(io, question, rows, start)
        return sorted(io.driver.checklist(question, rows, set(start), allow_back=allow_back))

    return _navigated(io, "checklist", question, ask)
