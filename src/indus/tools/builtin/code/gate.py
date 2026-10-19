# pyright: strict
"""Whole-script approval for ``execute_code`` (E9), before anything is written or started.

A script can start processes and change files without going through ``terminal`` and its
command checks, so the script as a whole is approved once, with the P05 approval building blocks:

1. an isolated backend (a container with no host mount): allowed — the sandbox is the boundary;
2. the session's auto-approve switch, ``INDUS_YOLO_MODE`` or ``approvals.mode: off``: allowed;
3. nobody can answer (no broker, or a cron, one-shot or unattended run): the matching
   ``approvals.*_mode`` policy, ``deny`` by default;
4. already approved for this session (pattern ``execute_code``): allowed;
5. ``smart`` mode with a judge: the judge may approve this run;
6. otherwise the person is asked (once / session / deny; "always" is not offered, a script is
   not a command for the allowlist).

What the script then does through ``indus_tools`` still meets every tool's own checks; an
in-script ``terminal`` call meets the full command pipeline and its hardline floor.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import cast

from indus.foundation.scope import Scope
from indus.protocol.requests_port import ServerRequestPort
from indus.protocol.server_requests import ApprovalChoice
from indus.safety.approvals import ApprovalContext, Detection, session_key
from indus.safety.approvals.decision import RunContext, approval_mode, setting
from indus.safety.approvals.human import ask_human
from indus.safety.approvals.policy import auto_approve_active
from indus.safety.approvals.smart import ask_judge
from indus.safety.redaction import redact_output

log = logging.getLogger(__name__)

PATTERN_KEY = "execute_code"
DESCRIPTION = (
    "a Python script (execute_code): it can start programs and change files without the "
    "per-command checks, so the whole script is approved for this run"
)
_POLICY_KEYS: dict[str, str] = {
    "cron": "approvals.cron_mode",
    "single_query": "approvals.single_query_mode",
    "unattended": "approvals.unattended_mode",
}
_RUN_CONTEXTS: frozenset[str] = frozenset({"interactive", "cron", "single_query", "unattended"})


@dataclass(frozen=True)
class GateRequest:
    code: str
    scope: Scope
    session_id: str | None
    turn_id: str | None
    broker: object | None
    run_context: object | None
    isolated: bool
    judge: object | None = None


@dataclass(frozen=True)
class GateVerdict:
    approved: bool
    error: str | None = None
    user_summary: str | None = None
    note: str | None = None  # for an approved run that a person or the judge looked at
    user_approved: bool = False


def script_as_command(code: str) -> str:
    """How the script is shown to the person and the judge: a heredoc of the source."""
    return f"python3 - <<'INDUS_SCRIPT'\n{code}\nINDUS_SCRIPT"


def _run_context(value: object) -> RunContext:
    if isinstance(value, str) and value in _RUN_CONTEXTS:
        return cast(RunContext, value)
    return "interactive"


async def approve_script(request: GateRequest) -> GateVerdict:
    """The decision for one script; never raises (an unusable pipeline blocks)."""
    try:
        return await _decide(request)
    except Exception as exc:
        log.error("execute_code approval could not run: %s: %s", type(exc).__name__, exc)
        return GateVerdict(
            False, error="Blocked: the approval check could not run, so the script was not run."
        )


async def _decide(request: GateRequest) -> GateVerdict:
    scope = request.scope
    run_context = _run_context(request.run_context)
    broker = cast(ServerRequestPort | None, request.broker)
    ctx = ApprovalContext(
        scope=scope,
        session_id=request.session_id,
        turn_id=request.turn_id,
        run_context=run_context,
        broker=broker,
        isolated=request.isolated,
        judge=request.judge,
    )
    if request.isolated:
        return GateVerdict(True)
    mode = approval_mode(scope)
    if auto_approve_active(ctx, scope) or mode == "off":
        return GateVerdict(True)
    if run_context != "interactive" or broker is None:
        kind = run_context if run_context != "interactive" else "unattended"
        key = _POLICY_KEYS[kind]
        if str(setting(scope, key)).strip().lower() == "approve":
            return GateVerdict(True)
        return GateVerdict(
            False,
            error=(
                "BLOCKED: execute_code runs a whole Python script, which needs approval, and "
                "nobody here can give it. Nothing ran. Use the tools directly, or the user sets "
                f"{key}: approve to allow scripts in this kind of run."
            ),
            user_summary="Blocked: a script with nobody here to approve it.",
        )
    session = session_key(scope, request.session_id)
    if ctx.approvals.pattern_approved(session, PATTERN_KEY):
        return GateVerdict(True)
    command = script_as_command(request.code)
    if mode == "smart" and request.judge is not None:
        verdict = await ask_judge(
            request.judge,  # pyright: ignore[reportArgumentType]
            command,
            DESCRIPTION,
            scope=scope,
        )
        if verdict == "approve":
            return GateVerdict(True, note="The safety reviewer approved this script for this run.")
    detection = Detection(True, False, PATTERN_KEY, DESCRIPTION)
    decision = await ask_human(
        command,
        detection,
        ctx,
        scope=scope,
        mode=mode,
        choices=(ApprovalChoice.ONCE, ApprovalChoice.SESSION),
    )
    if decision.approved:
        return GateVerdict(True, note="The user approved running this script.", user_approved=True)
    message = decision.message or "BLOCKED: the script was not approved, so nothing ran."
    return GateVerdict(
        False,
        error=redact_output(message, force=True, scope=scope),
        user_summary=decision.user_summary,
    )


__all__ = [
    "DESCRIPTION",
    "PATTERN_KEY",
    "GateRequest",
    "GateVerdict",
    "approve_script",
    "script_as_command",
]
