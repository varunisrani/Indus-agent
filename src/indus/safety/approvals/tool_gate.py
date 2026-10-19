# pyright: strict
"""A person's approval for one tool call that is not a shell command (P13-07).

Used by the MCP trust gate (a write-capable tool of an untrusted server) and by a plugin's
``pre_tool_call`` ``approve`` directive. Same order as the computer_use and execute_code gates:

1. no profile → blocked;
2. the session's auto-approve switch (or ``INDUS_YOLO_MODE``) or ``approvals.mode: off`` → runs;
3. nobody can answer (cron, single query, unattended, or no broker) → the context's policy key
   (``approvals.cron_mode`` …; ``deny`` by default);
4. approved earlier in this session under the same ``pattern_key`` → runs;
5. ``smart`` mode with a judge: the judge first, the person only when it does not approve;
6. the person, through the session's broker: once / session / deny. A timeout or a withdrawn
   request blocks without counting as a refusal (P05 human step).

Any error inside the gate blocks the call (fail closed).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, cast

from indus.foundation.scope import Scope
from indus.protocol.requests_port import ServerRequestPort
from indus.protocol.server_requests import ApprovalChoice
from indus.safety.approvals.decision import (
    ApprovalContext,
    ApprovalState,
    RunContext,
    approval_mode,
    session_key,
    setting,
)
from indus.safety.approvals.detect import Detection
from indus.safety.approvals.human import ask_human
from indus.safety.approvals.policy import auto_approve_active
from indus.safety.approvals.smart import ask_judge
from indus.safety.redaction import redact_output

__all__ = ["POLICY_KEYS", "ToolGateVerdict", "approve_tool_call"]

log = logging.getLogger(__name__)

POLICY_KEYS: dict[str, str] = {
    "cron": "approvals.cron_mode",
    "single_query": "approvals.single_query_mode",
    "unattended": "approvals.unattended_mode",
}
_RUN_CONTEXTS = frozenset({"interactive", "cron", "single_query", "unattended"})


@dataclass(frozen=True)
class ToolGateVerdict:
    """``approved`` or not; ``message`` is the text the model gets when the call does not run."""

    approved: bool
    outcome: str
    message: str | None = None


def _run_context(value: object) -> RunContext:
    text = str(value) if value is not None else "interactive"
    return cast(RunContext, text if text in _RUN_CONTEXTS else "interactive")


async def approve_tool_call(
    shown: str,
    description: str,
    pattern_key: str,
    *,
    scope: Scope | None,
    session_id: str | None = None,
    turn_id: str | None = None,
    broker: ServerRequestPort | None = None,
    run_context: object = None,
    judge: Any = None,
    state: ApprovalState | None = None,
) -> ToolGateVerdict:
    """Ask whether the call ``shown`` may run (see the module notes); never raises."""
    try:
        return await _decide(
            shown,
            description,
            pattern_key,
            scope=scope,
            session_id=session_id,
            turn_id=turn_id,
            broker=broker,
            run_context=_run_context(run_context),
            judge=judge,
            state=state,
        )
    except Exception as exc:
        log.error("tool approval could not run: %s: %s", type(exc).__name__, exc)
        return ToolGateVerdict(
            False,
            "error",
            "BLOCKED: the approval check could not run, so the tool call did not run.",
        )


async def _decide(
    shown: str,
    description: str,
    pattern_key: str,
    *,
    scope: Scope | None,
    session_id: str | None,
    turn_id: str | None,
    broker: ServerRequestPort | None,
    run_context: RunContext,
    judge: Any,
    state: ApprovalState | None,
) -> ToolGateVerdict:
    if scope is None:
        return ToolGateVerdict(
            False,
            "no_scope",
            "BLOCKED: this tool call needs the user's approval and there is no profile to ask "
            "for, so it did not run.",
        )
    ctx = ApprovalContext(
        scope=scope,
        session_id=session_id,
        turn_id=turn_id,
        run_context=run_context,
        broker=broker,
        state=state,
        judge=judge,
    )
    mode = approval_mode(scope)
    if auto_approve_active(ctx, scope) or mode == "off":
        return ToolGateVerdict(True, "auto_approved")
    if run_context != "interactive" or broker is None:
        key = POLICY_KEYS["unattended" if run_context == "interactive" else run_context]
        if str(setting(scope, key)).strip().lower() == "approve":
            return ToolGateVerdict(True, "auto_approved")
        return ToolGateVerdict(
            False,
            "unattended_deny",
            f"BLOCKED: {shown} needs the user's approval and nobody here can give it, so it did "
            f"not run. The user can set {key}: approve to allow such calls in this kind of run.",
        )
    if ctx.approvals.pattern_approved(session_key(scope, session_id), pattern_key):
        return ToolGateVerdict(True, "session_approved")
    if mode == "smart" and judge is not None:
        verdict = await ask_judge(judge, shown, description, scope=scope)
        if verdict == "approve":
            return ToolGateVerdict(True, "smart_approved")
    decision = await ask_human(
        shown,
        Detection(True, False, pattern_key, description),
        ctx,
        scope=scope,
        mode=mode,
        choices=(ApprovalChoice.ONCE, ApprovalChoice.SESSION),
    )
    if decision.approved:
        return ToolGateVerdict(True, decision.outcome)
    text = decision.message or "BLOCKED: the tool call was not approved, so it did not run."
    return ToolGateVerdict(False, decision.outcome, redact_output(text, force=True, scope=scope))
