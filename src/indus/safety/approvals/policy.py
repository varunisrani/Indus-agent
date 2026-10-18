# pyright: strict
"""The approval pipeline: ``await decide(command, ctx) -> ApprovalDecision`` (P05-08).

Fixed order (floors before every bypass, R02.4):

1. isolated backend (``ctx.isolated``, the environment's flag; false whenever a host path is
   mounted, R02.6): only the hardline floor and the user's deny globs apply, then approved;
2. floors (``floors``): hardline incl. unparseable commands, deleting the interpreter or venv
   Indus runs from, agent-written ``sudo -S`` with no configured password, ``approvals.deny``;
3. a decision the agent's batching prepared for this exact command (used once);
4. auto-approve (the session switch or ``INDUS_YOLO_MODE``) or ``approvals.mode: off``;
5. ``command_allowlist``: exact text, or a glob for one simple command;
6. nobody can answer (cron, single query, unattended, or no broker): the context's policy,
   ``deny`` (default) blocks a dangerous command at once, ``approve`` lets it run (R02.27);
7. detection: not dangerous, or the pattern was approved for this session → approved;
8. a person decides (``human``).

``approvals.mode`` defaults to ``manual`` (D-16.53, one defaults table D-16.35); ``smart`` is
accepted and asks a person like ``manual`` until P08 adds the pre-judge. Every setting is read
through the scope on each call, so a mode change applies to the next command.
"""

from __future__ import annotations

import fnmatch
import logging
from collections.abc import Iterable
from typing import cast

from indus.foundation.scope import Scope, scope_or_none
from indus.safety.approvals import floors
from indus.safety.approvals.decision import (
    ApprovalContext,
    ApprovalDecision,
    Outcome,
    RunContext,
    approval_mode,
    session_key,
    setting,
)
from indus.safety.approvals.detect import Detection, detect, normalise
from indus.safety.approvals.human import ask_human
from indus.safety.approvals.shlex_lite import ShellLimitError, ShellSyntaxError, parse

__all__ = [
    "ApprovalContext",
    "ApprovalDecision",
    "SUDO_SECRET_NAMES",
    "allowlist_match",
    "auto_approve_active",
    "decide",
    "prepare_decision",
]

log = logging.getLogger(__name__)

SUDO_SECRET_NAMES = ("sudo_password", "SUDO_PASSWORD")
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_UNATTENDED_KEYS: dict[RunContext, str] = {
    "cron": "approvals.cron_mode",
    "single_query": "approvals.single_query_mode",
    "unattended": "approvals.unattended_mode",
}
_UNATTENDED_WHY: dict[RunContext, str] = {
    "cron": "scheduled jobs run with nobody present to approve it",
    "single_query": "a one-shot run has nobody present to approve it",
    "unattended": "nobody on this surface can answer an approval request",
}


# -- results ---------------------------------------------------------------------------------------


def _allowed(
    step: str, outcome: Outcome = "allowed", *, pattern_key: str | None = None
) -> ApprovalDecision:
    return ApprovalDecision(
        approved=True, status="approved", outcome=outcome, step=step, pattern_key=pattern_key
    )


def _blocked(
    step: str,
    outcome: Outcome,
    message: str,
    *,
    pattern_key: str | None = None,
    description: str | None = None,
    user_summary: str | None = None,
) -> ApprovalDecision:
    return ApprovalDecision(
        approved=False,
        status="blocked",
        outcome=outcome,
        step=step,
        pattern_key=pattern_key,
        description=description,
        user_summary=user_summary,
        message=message,
    )


def _floor_decision(step: str, outcome: Outcome, hit: floors.FloorHit) -> ApprovalDecision:
    log.warning("approval floor %s blocked a command (%s)", hit.key, hit.description)
    return _blocked(
        step,
        outcome,
        hit.message,
        pattern_key=hit.key,
        description=hit.description,
        user_summary=f"Blocked: {hit.description}.",
    )


# -- settings --------------------------------------------------------------------------------------


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, Iterable):
        items = cast(Iterable[object], value)
        return [item for item in items if isinstance(item, str) and item.strip()]
    return []


def auto_approve_active(ctx: ApprovalContext, scope: Scope) -> bool:
    """The session switch, or ``INDUS_YOLO_MODE`` in the scope's environment."""
    if ctx.approvals.auto_approve(session_key(scope, ctx.session_id)):
        return True
    return scope.env.get("INDUS_YOLO_MODE", "").strip().lower() in _TRUTHY


def _sudo_password_configured(scope: Scope) -> bool:
    if scope.secrets is None:
        return False
    for name in SUDO_SECRET_NAMES:
        try:
            if scope.secrets.get(name):
                return True
        except Exception:
            log.debug("secret store lookup failed", exc_info=True)
    return False


def _unattended_policy(ctx: ApprovalContext, scope: Scope) -> str:
    kind: RunContext = ctx.run_context if ctx.run_context != "interactive" else "unattended"
    raw = setting(scope, _UNATTENDED_KEYS[kind])
    return "approve" if str(raw).strip().lower() == "approve" else "deny"


# -- allowlist -------------------------------------------------------------------------------------


def _single_simple_command(command: str) -> bool:
    """One simple command with no operators, substitutions, redirections or code payloads:
    the only shape an allowlist glob may cover."""
    try:
        commands = parse(normalise(command))
    except (ShellSyntaxError, ShellLimitError):
        return False
    real = [c for c in commands if c.words or c.redirects]
    if len(real) != 1 or len(commands) != 1:
        return False
    only = real[0]
    if only.redirects or only.op not in ("", "\n"):
        return False
    for index, word in enumerate(only.words):
        if word.subs:
            return False
        previous = only.words[index - 1].text if index else ""
        if previous in ("-c", "-e", "--command", "--eval") and any(
            ch in word.text for ch in ";&|<>`$()!\n \t"
        ):
            return False
    return True


def allowlist_match(command: str, entries: Iterable[str]) -> str | None:
    """The ``command_allowlist`` entry covering ``command``: its exact text, or a glob when the
    command is a single simple command (``git *`` never covers ``git log; rm -rf ~``)."""
    text = command.strip()
    if not text:
        return None
    globs: list[str] = []
    for entry in entries:
        rule = entry.strip()
        if rule == text:
            return entry
        if any(ch in rule for ch in "*?["):
            globs.append(rule)
    if globs and _single_simple_command(text):
        for rule in globs:
            if fnmatch.fnmatchcase(text, rule):
                return rule
    return None


# -- batching --------------------------------------------------------------------------------------


def prepare_decision(ctx: ApprovalContext, command: str, decision: ApprovalDecision) -> None:
    """Record a decision the agent already reached for ``command`` (one batch prompt for several
    commands); the next ``decide`` for exactly this text in this session uses it once. Floors
    still run first."""
    scope = ctx.scope if ctx.scope is not None else scope_or_none()
    if scope is None:
        return
    ctx.approvals.prepare(session_key(scope, ctx.session_id), command, decision)


# -- the pipeline ----------------------------------------------------------------------------------


def _hardline_or_deny(
    command: str, detection: Detection, deny_globs: list[str], step: str
) -> ApprovalDecision | None:
    hit = floors.hardline_hit(detection)
    if hit is not None:
        return _floor_decision(step, "hardline", hit)
    rule = floors.match_deny_glob(command, deny_globs)
    if rule is not None:
        message = (
            f"BLOCKED: this command matches the user's deny rule '{rule}' (approvals.deny). "
            "It never runs through Indus, whatever the approval settings. Do not retry or "
            "rephrase it; find another way or ask the user."
        )
        return _floor_decision(
            step,
            "user_deny_rule",
            floors.FloorHit("user_deny_rule", f"matches the deny rule '{rule}'", message),
        )
    return None


async def decide(command: str, ctx: ApprovalContext) -> ApprovalDecision:
    """Decide whether ``command`` may run now; see the module notes for the order."""
    scope = ctx.scope if ctx.scope is not None else scope_or_none()
    if scope is None:
        return _blocked(
            "scope",
            "no_scope",
            "BLOCKED: no profile is active for this call, so its approval settings are "
            "unknown. Nothing ran.",
        )
    if not command.strip():
        return _allowed("empty")
    home = scope.env.get("HOME") or None
    detection = detect(command, user_home=home, indus_home=str(scope.home))
    deny_globs = _strings(setting(scope, "approvals.deny"))

    # 1. isolated backend
    if ctx.isolated:
        return _hardline_or_deny(command, detection, deny_globs, "isolated") or _allowed("isolated")

    # 2. floors
    hard = floors.hardline_hit(detection)
    if hard is not None:
        return _floor_decision("floor", "hardline", hard)
    target = floors.runtime_delete_target(command, home=home, cwd=ctx.cwd)
    if target is not None:
        description = f"deletes {target}, which Indus itself runs from"
        return _floor_decision(
            "floor",
            "runtime_delete",
            floors.FloorHit(
                "runtime_delete",
                description,
                f"BLOCKED (never runs): this command {description}; Indus could not start "
                "again afterwards. Do not retry or rephrase it.",
            ),
        )
    if floors.sudo_stdin_guess(command) and not _sudo_password_configured(scope):
        return _floor_decision(
            "floor",
            "sudo_guess",
            floors.FloorHit(
                "sudo_guess",
                "pipes a password into sudo -S",
                "BLOCKED: do not pipe passwords into 'sudo -S'. Run the command with plain "
                "'sudo'; Indus supplies the password itself when the user has stored one "
                "(secret 'sudo_password') or asks the user for it.",
            ),
        )
    denied = _hardline_or_deny(command, detection, deny_globs, "floor")
    if denied is not None:
        return denied

    key = session_key(scope, ctx.session_id)

    # 3. prepared batch decision
    prepared = ctx.approvals.take_prepared(key, command)
    if prepared is not None:
        return prepared

    # 4. auto-approve / mode off
    mode = approval_mode(scope)
    if auto_approve_active(ctx, scope) or mode == "off":
        return _allowed("auto", "auto_approved")

    # 5. permanent allowlist
    rule = allowlist_match(command, _strings(setting(scope, "command_allowlist")))
    if rule is not None:
        return _allowed("allowlist", "allowlisted")

    # 6. nobody can answer
    if ctx.run_context != "interactive" or ctx.broker is None:
        if _unattended_policy(ctx, scope) == "approve" or not detection.dangerous:
            outcome: Outcome = "auto_approved" if detection.dangerous else "allowed"
            return _allowed("unattended", outcome)
        kind: RunContext = ctx.run_context if ctx.run_context != "interactive" else "unattended"
        setting_key = _UNATTENDED_KEYS[kind]
        return _blocked(
            "unattended",
            "unattended_deny",
            f"BLOCKED: flagged as dangerous ({detection.description}) but "
            f"{_UNATTENDED_WHY[kind]}. Nothing ran. Find another way that avoids this "
            f"command; to allow such commands here the user sets {setting_key}: approve.",
            pattern_key=detection.pattern_key,
            description=detection.description,
            user_summary="Blocked: a risky command with nobody here to approve it.",
        )

    # 7. detection and session approvals
    if not detection.dangerous:
        return _allowed("detect")
    if ctx.approvals.pattern_approved(key, detection.pattern_key):
        return _allowed("session", "session_approved", pattern_key=detection.pattern_key)

    # 8. a person decides (smart pre-judge arrives in P08)
    return await ask_human(command, detection, ctx, scope=scope, mode=mode)
