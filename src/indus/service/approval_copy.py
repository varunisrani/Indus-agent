"""What a surface says when the agent asks a person to approve a command or answer a question.

One module, so every surface (chat platforms first, P15-22) tells the person the same three things
about an approval: what would run, why it was flagged, and that no answer means it does not run.
Each ending has its own sentence, so a denial never reads like a timeout or a withdrawn request
(R02.5). Plain text only: a surface adds its own markup around these strings.
"""

from __future__ import annotations

from collections.abc import Iterable

__all__ = [
    "ACTION_LABELS",
    "APPROVAL_HEADER",
    "CLARIFY_DONE",
    "CLARIFY_FREE_HINT",
    "CLARIFY_HEADER",
    "CLARIFY_NOT_ASKED",
    "CLARIFY_TIMED_OUT",
    "DONE_LABEL",
    "MORE_LABEL",
    "NOT_REQUESTER",
    "NOT_SHOWN_REASON",
    "NOT_WAITING",
    "OTHER_LABEL",
    "OTHER_PROMPT",
    "REASON_LABEL",
    "approved",
    "approved_many",
    "clarify_deadline_line",
    "clarify_hint",
    "clarify_withdrawn",
    "deadline_line",
    "denied",
    "more_lines",
    "not_offered",
    "out_of_range",
    "picked_so_far",
    "text_hint",
    "timed_out",
    "window_text",
    "withdrawn",
]

APPROVAL_HEADER = "Indus needs your OK to run a command"
REASON_LABEL = "Why it was flagged"
ACTION_LABELS: dict[str, str] = {
    "once": "Run once",
    "session": "Allow for this conversation",
    "always": "Always allow",
    "deny": "Deny",
}
NOT_WAITING = "That request is no longer waiting."
NOT_REQUESTER = "Only the person who asked for this, or an admin, can answer it."
NOT_SHOWN_REASON = "the approval prompt could not be shown in the chat"

CLARIFY_HEADER = "A question before I go on"
CLARIFY_FREE_HINT = "Reply with your answer."
CLARIFY_TIMED_OUT = "No answer in time, so I went on without it."
CLARIFY_DONE = "Thanks, got it."
CLARIFY_NOT_ASKED = "[the question could not be shown in the chat]"
OTHER_LABEL = "Other…"
MORE_LABEL = "More…"
DONE_LABEL = "Done"
OTHER_PROMPT = "Type your answer."

_WITHDRAWN: dict[str, str] = {
    "interrupted": "the reply was stopped",
    "turn_ended": "the reply ended",
    "session_closed": "the conversation was closed",
}


def _unit(count: int, name: str) -> str:
    return f"{count} {name}" if count == 1 else f"{count} {name}s"


def window_text(seconds: float) -> str:
    """``300`` → "5 minutes", ``45`` → "45 seconds", ``7200`` → "2 hours"."""
    whole = max(0, round(seconds))
    if whole < 90:
        return _unit(whole, "second")
    minutes = round(whole / 60)
    if minutes < 90:
        return _unit(minutes, "minute")
    hours = round(whole / 3600)
    return _unit(hours, "hour")


def deadline_line(seconds: float) -> str:
    return f"No answer within {window_text(seconds)} means it will not run."


def more_lines(count: int) -> str:
    return f"(+{count} more {'line' if count == 1 else 'lines'})"


def text_hint(choices: Iterable[str]) -> str:
    """How to answer on a surface without buttons, listing only the choices offered."""
    offered = set(choices)
    ways = ["/approve to run it once"]
    keep = [f"/approve {word}" for word in ("session", "always") if word in offered]
    if keep:
        ways.append(" or ".join(keep) + " to keep allowing it")
    ways.append("/approve all for every waiting command")
    ways.append("/deny [reason] to refuse")
    return "Reply " + ", ".join(ways[:-1]) + ", or " + ways[-1] + "."


def approved(choice: str) -> str:
    if choice == "session":
        return "Approved for the rest of this conversation."
    if choice == "always":
        return "Approved, and this command will not need asking again."
    return "Approved: running it once."


def approved_many(count: int) -> str:
    return f"Approved {_unit(count, 'waiting command')}."


def denied(reason: str | None) -> str:
    text = "Denied, so the command was not run."
    return f"{text} Reason: {reason}" if reason else text


def timed_out(seconds: float) -> str:
    return (
        f"No answer within {window_text(seconds)}, so the command was not run. "
        "Ask again if you still want it."
    )


def withdrawn(reason: str) -> str:
    if reason == "answered_elsewhere":
        return "This request was answered somewhere else."
    why = _WITHDRAWN.get(reason, "it was withdrawn")
    return f"Request withdrawn because {why}; the command was not run."


def not_offered(word: str, choices: Iterable[str]) -> str:
    offered = ", ".join(f"/approve {c}" for c in choices if c not in ("once", "deny"))
    extra = f" or {offered}" if offered else ""
    return f"'{word}' isn't offered for this command. Reply /approve{extra}, or /deny."


def clarify_hint(*, multi: bool, more: bool) -> str:
    if multi:
        text = "Reply with one or more numbers (e.g. 1, 3), the options' text, or your own answer."
    else:
        text = "Reply with a number, the option's text, or your own answer."
    return f"{text} Reply 'more' for more options." if more else text


def clarify_deadline_line(seconds: float) -> str:
    return f"No answer within {window_text(seconds)} means I go on without it."


def clarify_withdrawn(reason: str) -> str:
    why = _WITHDRAWN.get(reason, "it was withdrawn")
    return f"Question withdrawn because {why}."


def out_of_range(count: int) -> str:
    return f"Pick a number from 1 to {count}, or type your own answer."


def picked_so_far(picked: Iterable[str]) -> str:
    chosen = ", ".join(picked) or "nothing yet"
    return f"Picked: {chosen}. Press {DONE_LABEL} when finished."
