# pyright: strict
"""The human step of the approval pipeline: one ``approval`` server request (P05-09).

The request goes through the session's broker (``ServerRequestPort``) with the command masked
on the server and long lines wrapped, never cut (R05.22), and offers once / session / always /
deny. How the ask ends decides the result:

- ``once`` → approved for this run; ``session`` → approved and the pattern key remembered for
  this profile + session; ``always`` → approved and the exact command appended to the profile's
  ``command_allowlist`` through the config writer (a command that carries a secret is approved
  once but not stored);
- ``deny`` → blocked; the person's reason reaches the model word for word;
- no answer within ``approvals.timeout`` → blocked with outcome ``timeout``; a withdrawn request
  (interrupt, turn end, session closed) or one the client cannot show → blocked with outcome
  ``not_answered``. Neither is reported as a refusal or counted as a denial (R02.5).

The broker counts the time a request is open (``human_wait_seconds``); the P04 executor already
subtracts it from batch deadlines. The decision also carries ``waited_s``. A person's approval
carries a one-shot reset for a stale interrupt bit (R02.9,
``ApprovalDecision.clear_stale_interrupt``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import cast

from indus.foundation.config import loader, writer
from indus.foundation.scope import Scope
from indus.protocol.events import CancelReason
from indus.protocol.requests_port import RequestNotAnswered, RequestUnsupported, RequestWithdrawn
from indus.protocol.server_requests import ApprovalAnswer, ApprovalChoice
from indus.safety.approvals.decision import (
    ApprovalContext,
    ApprovalDecision,
    ApprovalMode,
    Outcome,
    StaleInterruptReset,
    session_key,
    setting,
)
from indus.safety.approvals.detect import Detection
from indus.safety.redaction import redact_output

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "WRAP_WIDTH",
    "append_to_allowlist",
    "approval_timeout",
    "ask_human",
    "display_command",
]

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 300.0
MAX_TIMEOUT_S = 365 * 24 * 3600.0
WRAP_WIDTH = 100


def approval_timeout(scope: Scope) -> float:
    """``approvals.timeout`` in seconds; a missing, non-numeric or non-positive value means the
    default (an endless wait could park the turn for good)."""
    raw = setting(scope, "approvals.timeout")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S
    if value <= 0:
        return DEFAULT_TIMEOUT_S
    return min(value, MAX_TIMEOUT_S)


def display_command(text: str, width: int = WRAP_WIDTH) -> str:
    """``text`` with lines longer than ``width`` broken (at a space when one is near), keeping
    every character: removing the added line breaks gives the input back."""
    out: list[str] = []
    for line in text.split("\n"):
        while len(line) > width:
            cut = line.rfind(" ", width // 2, width)
            cut = cut + 1 if cut > 0 else width
            out.append(line[:cut])
            line = line[cut:]
        out.append(line)
    return "\n".join(out)


def _strings(value: object) -> list[str]:
    if isinstance(value, list | tuple):
        items = cast(Iterable[object], value)
        return [item for item in items if isinstance(item, str)]
    return []


def append_to_allowlist(scope: Scope, command: str) -> bool:
    """Add ``command`` to this profile's ``command_allowlist``; False when it could not be
    saved (managed install, unreadable file, write error)."""
    entry = command.strip()
    try:
        current = loader.load(scope)
        if current.failed:
            return False
        entries = _strings(current.get("command_allowlist"))
        if entry in (e.strip() for e in entries):
            return True
        writer.save(scope, {"command_allowlist": [*entries, entry]})
        return True
    except Exception as exc:
        log.warning("could not save the allowlist entry: %s", type(exc).__name__)
        return False


def _summary_minutes(seconds: float) -> str:
    if seconds >= 120 and seconds % 60 == 0:
        return f"{int(seconds // 60)} min"
    return f"{seconds:g} s"


def _not_run(
    outcome: Outcome,
    message: str,
    summary: str,
    detection: Detection,
    waited: float,
    *,
    deny_reason: str | None = None,
) -> ApprovalDecision:
    return ApprovalDecision(
        approved=False,
        status="blocked",
        outcome=outcome,
        step="human",
        pattern_key=detection.pattern_key,
        description=detection.description,
        deny_reason=deny_reason,
        user_summary=summary,
        message=message,
        waited_s=waited,
    )


async def ask_human(
    command: str,
    detection: Detection,
    ctx: ApprovalContext,
    *,
    scope: Scope,
    mode: ApprovalMode = "manual",
    choices: Iterable[ApprovalChoice] | None = None,
) -> ApprovalDecision:
    """Ask the person through ``ctx.broker`` and turn the outcome into a decision."""
    broker = ctx.broker
    if broker is None:  # the pipeline routes broker-less calls to the unattended policy
        raise ValueError("ask_human needs a broker")
    timeout = approval_timeout(scope)
    description = detection.description or "flagged as risky"
    offered = list(choices) if choices is not None else list(ApprovalChoice)
    if ApprovalChoice.DENY not in offered:
        offered.append(ApprovalChoice.DENY)
    payload = {
        "command": display_command(redact_output(command, force=True, scope=scope)),
        "description": redact_output(description, force=True, scope=scope),
        "choices": [choice.value for choice in offered],
    }
    if mode == "smart":
        log.debug("approvals.mode smart: the judge did not approve; asking the person")
    started = scope.clock.monotonic()

    def waited() -> float:
        return max(0.0, scope.clock.monotonic() - started)

    try:
        answer = await broker.ask("approval", payload, timeout=timeout, turn_id=ctx.turn_id)
    except RequestWithdrawn as exc:
        if exc.reason == CancelReason.TIMED_OUT:
            return _not_run(
                "timeout",
                f"BLOCKED: nobody answered the approval request within "
                f"{_summary_minutes(timeout)}, so the command did not run. Silence is not "
                "consent, and this is not a refusal either: tell the user the command is "
                "waiting for their approval, or find another way. Do not retry it on your own.",
                f"No answer within {_summary_minutes(timeout)}; the command did not run.",
                detection,
                waited(),
            )
        return _not_run(
            "not_answered",
            f"BLOCKED: the approval request was withdrawn before anyone answered "
            f"({exc.reason.value.replace('_', ' ')}), so the command did not run. The user did "
            "not refuse it.",
            "The approval request was not answered; the command did not run.",
            detection,
            waited(),
        )
    except RequestUnsupported:
        return _not_run(
            "not_answered",
            "BLOCKED: this command needs the user's approval, but the connected client cannot "
            "show approval requests, so it did not run. The user did not refuse it.",
            "The approval request could not be shown; the command did not run.",
            detection,
            waited(),
        )
    except RequestNotAnswered:
        return _not_run(
            "not_answered",
            "BLOCKED: the approval request was not answered, so the command did not run.",
            "The approval request was not answered; the command did not run.",
            detection,
            waited(),
        )
    if not isinstance(answer, ApprovalAnswer):
        return _not_run(
            "not_answered",
            "BLOCKED: the approval request got an answer of the wrong kind; nothing ran.",
            "The approval request was not answered; the command did not run.",
            detection,
            waited(),
        )

    if answer.choice is ApprovalChoice.DENY or answer.choice not in offered:
        reason = answer.reason if answer.choice is ApprovalChoice.DENY else None
        message = "BLOCKED: the user denied this command. Do not retry or rephrase it."
        if reason:
            message += f' The user said: "{reason}"'
        return _not_run(
            "denied", message, "You denied the command.", detection, waited(), deny_reason=reason
        )

    user_summary: str | None = None
    if answer.choice is ApprovalChoice.SESSION and detection.pattern_key:
        ctx.approvals.approve_pattern(session_key(scope, ctx.session_id), detection.pattern_key)
    elif answer.choice is ApprovalChoice.ALWAYS:
        if redact_output(command, force=True, scope=scope) != command:
            user_summary = "Approved once; not added to the allowlist because it holds a secret."
        elif not append_to_allowlist(scope, command):
            user_summary = "Approved once; the allowlist could not be saved."
    return ApprovalDecision(
        approved=True,
        status="approved",
        outcome="user_approved",
        step="human",
        pattern_key=detection.pattern_key,
        description=detection.description,
        user_approved=True,
        user_summary=user_summary,
        waited_s=waited(),
        stale_interrupt=StaleInterruptReset(),
    )
