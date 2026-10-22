"""Cards: how the classic REPL asks the person what the agent needs to know (P06-07).

A server request (approval, clarify, sudo, secret) opens a card. The card prints its body into the
scrollback once, then owns the prompt line until it produces an answer model for
``answer_server_request``. The REPL feeds it what the person does:

- ``on_digit(n)`` — a digit pressed on an empty line (approval quick picks, R05.11);
- ``submit(text)`` — Enter with the typed text;
- ``cancel()`` — Ctrl+C: the card's "no" answer, never a crash.

Each returns a ``CardStep``: ``answer`` is set when the card is finished; ``show`` holds lines to
print (the next clarify question, a hint after a bad pick). A step with neither means "ignored".

Approval: the command arrives redacted and wrapped by the server (R05.22); the card wraps it again
to the terminal width without dropping a character, shows up to ``COMMAND_LINES`` lines and says
how many more there are (``view`` prints them all). Picks are numbered in the order the request
lists them (normally 1 once, 2 session, 3 always, 4 deny). Deny asks for an optional reason that
travels to the model unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from rich.text import Text

from indus.cli.repl.textutil import clean, one_line
from indus.protocol.requests_port import AnswerModel
from indus.protocol.server_requests import (
    ApprovalAnswer,
    ApprovalChoice,
    ApprovalRequest,
    ClarifyAnswer,
    ClarifyQuestion,
    ClarifyRequest,
    PreviewActAnswer,
    PreviewReadAnswer,
    SecretAnswer,
    SecretRequest,
    ServerRequestBase,
    SudoAnswer,
    SudoRequest,
)

__all__ = [
    "CANCEL_REASON",
    "CHOICE_LABELS",
    "COMMAND_LINES",
    "ApprovalCard",
    "Card",
    "CardStep",
    "ClarifyCard",
    "MaskedCard",
    "describe_answer",
    "open_card",
    "wrap_keep_all",
]

COMMAND_LINES = 10
"""Command lines shown on the approval card before the "more" count."""
MIN_WRAP = 20
CANCEL_REASON = "The person pressed Ctrl+C at the approval prompt instead of answering."

CHOICE_LABELS: dict[ApprovalChoice, str] = {
    ApprovalChoice.ONCE: "allow once",
    ApprovalChoice.SESSION: "allow for the rest of this session",
    ApprovalChoice.ALWAYS: "always allow this command (saved)",
    ApprovalChoice.DENY: "deny",
}


@dataclass(frozen=True)
class CardStep:
    """What a card did with an input (see the module notes)."""

    answer: AnswerModel | None = None
    show: tuple[Text, ...] = ()

    @property
    def ignored(self) -> bool:
        return self.answer is None and not self.show


def _note(text: str, style: str = "dim") -> Text:
    return Text(f"  {text}", style=style)


def wrap_keep_all(text: str, width: int) -> list[str]:
    """``text`` broken into lines of at most ``width`` characters, preferring a break after a
    space; joining each source line's pieces gives that line back exactly."""
    width = max(1, width)
    out: list[str] = []
    for line in text.split("\n"):
        while len(line) > width:
            cut = line.rfind(" ", width // 2, width)
            cut = cut + 1 if cut > 0 else width
            out.append(line[:cut])
            line = line[cut:]
        out.append(line)
    return out


class Card:
    """Base for every card."""

    masked = False
    """True when typed text must be hidden (passwords, secrets)."""

    def __init__(self, request: ServerRequestBase, opened_at: float) -> None:
        self.request = request
        self.opened_at = opened_at

    @property
    def request_id(self) -> str:
        return self.request.request_id

    @property
    def takes_digits(self) -> bool:
        """True when a digit pressed on an empty line is a pick rather than text."""
        return False

    def deadline(self) -> float | None:
        """When the server stops waiting (clock monotonic seconds), if it said."""
        if self.request.deadline_s is None:
            return None
        return self.opened_at + self.request.deadline_s

    def remaining(self, now: float) -> int | None:
        end = self.deadline()
        if end is None:
            return None
        return max(0, int(end - now + 0.999))

    def body(self, width: int) -> list[Text]:
        raise NotImplementedError

    def label(self) -> str:
        """The prompt text while the card is open (without the countdown)."""
        raise NotImplementedError

    def prompt(self, now: float) -> str:
        left = self.remaining(now)
        suffix = f" [{left}s]" if left is not None else ""
        return f"{self.label()}{suffix} › "

    def on_digit(self, digit: int) -> CardStep:
        return CardStep()

    def submit(self, text: str) -> CardStep:
        raise NotImplementedError

    def cancel(self) -> AnswerModel:
        raise NotImplementedError


# -- approval ---------------------------------------------------------------------------------


class ApprovalCard(Card):
    """Once / session / always / deny for a risky command."""

    def __init__(self, request: ApprovalRequest, opened_at: float, width: int = 80) -> None:
        super().__init__(request, opened_at)
        self.approval = request
        self.choices: list[ApprovalChoice] = list(request.choices)
        self.asking_reason = False
        self.wrap_width = max(MIN_WRAP, width - 8)

    @property
    def takes_digits(self) -> bool:
        return not self.asking_reason

    def command_lines(self) -> list[str]:
        return wrap_keep_all(clean(self.approval.command), self.wrap_width)

    def body(self, width: int) -> list[Text]:
        self.wrap_width = max(MIN_WRAP, width - 8)
        title = one_line(self.approval.description) or "this command needs your approval"
        lines = [Text(""), Text(f"⚠ Approval needed: {title}", style="bold yellow")]
        command = self.command_lines()
        for line in command[:COMMAND_LINES]:
            lines.append(Text(f"    {line}", style="bold"))
        hidden = len(command) - COMMAND_LINES
        if hidden > 0:
            noun = "line" if hidden == 1 else "lines"
            lines.append(_note(f"  … {hidden} more {noun} (type view to see all)"))
        for number, choice in enumerate(self.choices, start=1):
            lines.append(Text(f"  {number}  {CHOICE_LABELS[choice]}"))
        return lines

    def label(self) -> str:
        if self.asking_reason:
            return "reason for denying (Enter to skip)"
        return f"pick 1-{len(self.choices)}"

    def _pick(self, choice: ApprovalChoice) -> CardStep:
        if choice is ApprovalChoice.DENY:
            self.asking_reason = True
            return CardStep(show=(_note("Tell the agent why (optional), then press Enter."),))
        return CardStep(answer=ApprovalAnswer(choice=choice))

    def on_digit(self, digit: int) -> CardStep:
        if self.asking_reason or not 1 <= digit <= len(self.choices):
            return CardStep()
        return self._pick(self.choices[digit - 1])

    def submit(self, text: str) -> CardStep:
        typed = text.strip()
        if self.asking_reason:
            return CardStep(answer=ApprovalAnswer(choice=ApprovalChoice.DENY, reason=typed or None))
        if typed.isdigit():
            step = self.on_digit(int(typed))
            if not step.ignored:
                return step
        word = typed.lower()
        for choice in self.choices:
            if word == choice.value:
                return self._pick(choice)
        if word in ("view", "v"):
            lines = [Text(f"    {line}", style="bold") for line in self.command_lines()]
            return CardStep(show=(_note("Full command:"), *lines))
        return CardStep(show=(_note(f"Press a number from 1 to {len(self.choices)}."),))

    def cancel(self) -> AnswerModel:
        return ApprovalAnswer(choice=ApprovalChoice.DENY, reason=CANCEL_REASON)


# -- clarify ----------------------------------------------------------------------------------


@dataclass
class _Progress:
    index: int = 0
    answers: dict[str, list[str]] = field(default_factory=dict[str, list[str]])


class ClarifyCard(Card):
    """One to five questions, asked one after another."""

    def __init__(self, request: ClarifyRequest, opened_at: float) -> None:
        super().__init__(request, opened_at)
        self.questions: Sequence[ClarifyQuestion] = request.questions
        self.progress = _Progress()

    @property
    def current(self) -> ClarifyQuestion:
        return self.questions[self.progress.index]

    def _question_lines(self) -> list[Text]:
        q = self.current
        count = len(self.questions)
        head = f"? {one_line(q.text)}"
        if count > 1:
            head = f"? ({self.progress.index + 1}/{count}) {one_line(q.text)}"
        lines = [Text(head, style="bold")]
        for number, choice in enumerate(q.choices or (), start=1):
            lines.append(Text(f"  {number}  {one_line(choice)}"))
        if q.choices and q.multi_select:
            hint = "Type the numbers you want (e.g. 1 3), or your own answer. Enter alone skips."
        elif q.choices:
            hint = "Type a number or your own answer. Enter alone skips."
        else:
            hint = "Type your answer. Enter alone skips."
        lines.append(_note(hint))
        return lines

    def body(self, width: int) -> list[Text]:
        several = len(self.questions) > 1
        intro = "The agent has questions" if several else "The agent has a question"
        return [Text(""), Text(intro, style="bold cyan"), *self._question_lines()]

    def label(self) -> str:
        return "answer"

    def _parse(self, typed: str) -> list[str] | None:
        q = self.current
        if not typed:
            return []
        choices = q.choices or []
        tokens = typed.replace(",", " ").split()
        if choices and tokens and all(t.isdigit() for t in tokens):
            picks = [int(t) for t in tokens]
            if any(not 1 <= p <= len(choices) for p in picks):
                return None
            if len(picks) > 1 and not q.multi_select:
                return None
            unique = list(dict.fromkeys(picks))
            return [choices[p - 1] for p in unique]
        return [typed]

    def submit(self, text: str) -> CardStep:
        picked = self._parse(text.strip())
        if picked is None:
            count = len(self.current.choices or ())
            many = " (one or more)" if self.current.multi_select else ""
            return CardStep(show=(_note(f"Pick a number from 1 to {count}{many}."),))
        self.progress.answers[self.current.id] = picked
        self.progress.index += 1
        if self.progress.index >= len(self.questions):
            return CardStep(answer=ClarifyAnswer(answers=dict(self.progress.answers)))
        return CardStep(show=tuple(self._question_lines()))

    def cancel(self) -> AnswerModel:
        answers = dict(self.progress.answers)
        for q in self.questions:
            answers.setdefault(q.id, [])
        return ClarifyAnswer(answers=answers)


# -- sudo / secret ------------------------------------------------------------------------------


class MaskedCard(Card):
    """A hidden one-line entry: the sudo password or a secret value."""

    masked = True

    def body(self, width: int) -> list[Text]:
        request = self.request
        if isinstance(request, SudoRequest):
            title = f"🔒 Administrator password needed: {one_line(request.reason)}"
        elif isinstance(request, SecretRequest):
            title = f"🔑 {request.name} needed: {one_line(request.prompt)}"
        else:  # pragma: no cover - open_card only builds these two
            title = "🔒 Input needed"
        hint = "What you type stays hidden. Enter alone cancels."
        return [Text(""), Text(title, style="bold yellow"), _note(hint)]

    def label(self) -> str:
        return "password" if isinstance(self.request, SudoRequest) else "value"

    def _answer(self, value: str | None) -> AnswerModel:
        if isinstance(self.request, SudoRequest):
            return SudoAnswer(password=value)
        return SecretAnswer(value=value)

    def submit(self, text: str) -> CardStep:
        return CardStep(answer=self._answer(text if text else None))

    def cancel(self) -> AnswerModel:
        return self._answer(None)


def open_card(request: ServerRequestBase, opened_at: float, width: int = 80) -> Card | None:
    """The card for ``request``, or None for a kind this REPL does not show."""
    if isinstance(request, ApprovalRequest):
        return ApprovalCard(request, opened_at, width)
    if isinstance(request, ClarifyRequest):
        return ClarifyCard(request, opened_at)
    if isinstance(request, SudoRequest | SecretRequest):
        return MaskedCard(request, opened_at)
    return None


def describe_answer(answer: AnswerModel) -> str:
    """One line saying what was answered (never the secret itself)."""
    match answer:
        case ApprovalAnswer(choice=ApprovalChoice.DENY, reason=reason):
            return "denied" + (f": {one_line(reason)}" if reason else "")
        case ApprovalAnswer(choice=choice):
            return CHOICE_LABELS[choice].replace(" (saved)", "") + " — approved"
        case ClarifyAnswer(answers=answers):
            given = sum(1 for picks in answers.values() if picks)
            return f"answered {given} of {len(answers)}"
        case SudoAnswer(password=password):
            return "password sent" if password else "no password given"
        case SecretAnswer(value=value):
            return "value saved" if value else "skipped"
        case PreviewActAnswer(ok=ok) | PreviewReadAnswer(ok=ok):  # a desktop window's, never here
            return "preview answered" if ok else "preview refused"
