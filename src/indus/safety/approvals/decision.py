# pyright: strict
"""What an approval decision looks like, what it is decided for, and the per-session state.

Shared by the pipeline (``policy``) and the human step (``human``) so neither imports the other.

``ApprovalState`` keeps, per profile and session (R02.8): the pattern keys a person approved
for the session, the auto-approve switch, and decisions prepared for one exact command by the
agent's batching. One process-wide instance (``APPROVALS``) serves every session; tests pass
their own through ``ApprovalContext.state``.

Settings are read through the scope on every call (``setting``), so a change made with
``/approvals`` or by editing ``config.yaml`` applies to the next command.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, cast

from indus.foundation.config import loader
from indus.foundation.config.schema import default_for
from indus.foundation.scope import Scope
from indus.protocol.requests_port import ServerRequestPort

__all__ = [
    "APPROVALS",
    "ApprovalContext",
    "ApprovalDecision",
    "ApprovalMode",
    "ApprovalState",
    "InterruptFlag",
    "Outcome",
    "RunContext",
    "SessionKey",
    "StaleInterruptReset",
    "approval_mode",
    "session_key",
    "setting",
]

log = logging.getLogger(__name__)

type ApprovalMode = Literal["manual", "smart", "off"]
type RunContext = Literal["interactive", "cron", "single_query", "unattended"]
type Status = Literal["approved", "blocked", "pending"]
type Outcome = Literal[
    "allowed",  # nothing needed a person
    "auto_approved",  # auto-approve switch, mode off, or an approve-mode unattended context
    "allowlisted",  # command_allowlist
    "session_approved",  # pattern approved earlier in this session
    "user_approved",  # a person said once / session / always
    "smart_approved",  # the smart judge approved it for this run (P08-14)
    "prepared",  # a decision prepared for this exact command
    "hardline",  # never runs
    "runtime_delete",  # would delete the interpreter Indus runs from
    "sudo_guess",  # agent-written sudo -S with no configured password
    "user_deny_rule",  # approvals.deny
    "unattended_deny",  # nobody can answer and the context's policy is deny
    "denied",  # a person said deny
    "smart_denied",  # the smart judge refused it and the person, asked, said deny too
    "timeout",  # nobody answered in time
    "not_answered",  # withdrawn or undeliverable
    "no_scope",  # no profile to decide for
    "pending",  # an interactive context with no way to ask right now
]

type SessionKey = tuple[str, str]
"""(profile home, session id or ``default``)."""

MAX_SESSIONS = 1024


def session_key(scope: Scope, session_id: str | None) -> SessionKey:
    return (str(scope.home), session_id or "default")


class InterruptFlag(Protocol):
    """What ``StaleInterruptReset`` needs: a flag that can be read and cleared."""

    def is_set(self) -> bool: ...

    def clear(self) -> None: ...


@dataclass
class StaleInterruptReset:
    """One-shot: clears an interrupt bit that landed while a person was deciding (R02.9).

    The terminal tool calls it once, after the approval and before its retry loop. Later calls
    (from inside retries) do nothing, so a genuine interrupt during a retry is never swallowed.
    Pass the command's own interrupt bit, never the turn's stop event: an interrupt of the turn
    withdraws the approval request, so it never reaches an approved decision.
    """

    used: bool = False

    def apply(self, flag: InterruptFlag | None) -> bool:
        if self.used:
            return False
        self.used = True
        if flag is not None and flag.is_set():
            flag.clear()
            return True
        return False


@dataclass(frozen=True)
class ApprovalDecision:
    """The pipeline's answer for one command.

    ``message`` is addressed to the model (what happened and what to do next); ``user_summary``
    is one plain sentence for the person. ``step`` names the pipeline step that decided.
    """

    approved: bool
    status: Status
    outcome: Outcome
    step: str
    pattern_key: str | None = None
    description: str | None = None
    user_approved: bool = False
    deny_reason: str | None = None
    user_summary: str | None = None
    message: str | None = None
    waited_s: float = 0.0
    stale_interrupt: StaleInterruptReset | None = field(default=None, compare=False, repr=False)

    def clear_stale_interrupt(self, flag: InterruptFlag | None) -> bool:
        """Clear ``flag`` once if a person approved this command (see ``StaleInterruptReset``)."""
        if self.stale_interrupt is None:
            return False
        return self.stale_interrupt.apply(flag)

    @property
    def counts_as_denial(self) -> bool:
        """Only a judgement that the command is bad counts as a denial; a timeout or a withdrawn
        question never does (R02.5). The smart breaker itself counts judge verdicts (P08-14)."""
        return self.outcome in ("denied", "smart_denied")


class ApprovalState:
    """Per-session approval memory (see the module notes). Thread-safe."""

    def __init__(self, max_sessions: int = MAX_SESSIONS) -> None:
        self._lock = threading.Lock()
        self._max = max_sessions
        self._patterns: OrderedDict[SessionKey, set[str]] = OrderedDict()
        self._auto: dict[SessionKey, bool] = {}
        self._prepared: dict[SessionKey, dict[str, ApprovalDecision]] = {}

    # -- session-approved patterns -------------------------------------------------------------

    def approve_pattern(self, key: SessionKey, pattern_key: str) -> None:
        with self._lock:
            patterns = self._patterns.setdefault(key, set())
            patterns.add(pattern_key)
            self._patterns.move_to_end(key)
            while len(self._patterns) > self._max:
                self._patterns.popitem(last=False)

    def pattern_approved(self, key: SessionKey, pattern_key: str | None) -> bool:
        if not pattern_key:
            return False
        with self._lock:
            return pattern_key in self._patterns.get(key, set())

    # -- auto-approve ----------------------------------------------------------------------------

    def auto_approve(self, key: SessionKey) -> bool:
        with self._lock:
            return self._auto.get(key, False)

    def set_auto_approve(
        self, key: SessionKey, enabled: bool, *, persist: Callable[[bool], object] | None = None
    ) -> bool:
        """Switch auto-approve for one session; ``persist`` (the session-row writer) runs first,
        so a resume racing this call never revives a switch being turned off. A failed write
        costs only the restore; the live switch still changes."""
        if persist is not None:
            try:
                persist(enabled)
            except Exception:
                log.warning("could not store the auto-approve switch", exc_info=True)
        with self._lock:
            if enabled:
                self._auto[key] = True
            else:
                self._auto.pop(key, None)
        return enabled

    def restore_auto_approve(self, key: SessionKey, persisted: bool) -> bool:
        """Re-arm a stored switch when a session is resumed; True when this call turned it on."""
        if not persisted:
            return False
        with self._lock:
            if self._auto.get(key):
                return False
            self._auto[key] = True
            return True

    # -- prepared decisions (agent-side batching) -------------------------------------------------

    def prepare(self, key: SessionKey, command: str, decision: ApprovalDecision) -> None:
        with self._lock:
            self._prepared.setdefault(key, {})[command] = decision

    def take_prepared(self, key: SessionKey, command: str) -> ApprovalDecision | None:
        with self._lock:
            table = self._prepared.get(key)
            if not table:
                return None
            decision = table.pop(command, None)
            if not table:
                self._prepared.pop(key, None)
            return decision

    # -- lifecycle -------------------------------------------------------------------------------

    def clear_session(self, key: SessionKey) -> None:
        """Forget everything about one session (``/new``, session end). Auto-approve is off for
        the next session unless its stored row says otherwise."""
        with self._lock:
            self._patterns.pop(key, None)
            self._auto.pop(key, None)
            self._prepared.pop(key, None)


APPROVALS = ApprovalState()


@dataclass(frozen=True)
class ApprovalContext:
    """Who asks, from where, under which policy.

    ``run_context``: ``interactive`` when a person may be watching (CLI, TUI, desktop, chat),
    otherwise the unattended kind. ``broker``: the session's server-request port (the tool
    context offers it as ``broker``); without one nobody can be asked. ``isolated``: the
    environment's ``BackendFlags.isolated`` (false whenever a host path is mounted, R02.6).
    ``cwd``: where relative paths in the command start (optional). ``judge``: the smart
    pre-judge (``smart.Judge``, bound to the session's auxiliary model by the host); without one
    ``smart`` mode asks the person like ``manual``.
    """

    scope: Scope | None = None
    session_id: str | None = None
    turn_id: str | None = None
    run_context: RunContext = "interactive"
    broker: ServerRequestPort | None = None
    isolated: bool = False
    cwd: str | None = None
    state: ApprovalState | None = None
    judge: Any = None

    @property
    def approvals(self) -> ApprovalState:
        return self.state if self.state is not None else APPROVALS


def setting(scope: Scope, key: str) -> Any:
    """``key`` from the scope's config, read fresh, else the built-in default.

    A ``ConfigView`` loaded from this profile's file is re-loaded through the loader's cache
    (cheap: keyed by the file's signature), so an edit to ``config.yaml`` applies at once.
    """
    config: Any = scope.config
    if isinstance(config, loader.ConfigView) and config.path == loader.config_path(scope):
        config = loader.load(scope)
    value = config.get(key) if config is not None else None
    return default_for(key) if value is None else value


_MODES: frozenset[str] = frozenset({"manual", "smart", "off"})


def approval_mode(scope: Scope) -> ApprovalMode:
    """``approvals.mode`` normalised: YAML 1.1 reads a bare ``off`` as ``False``; anything
    unknown falls back to ``manual`` (asking is the safe side)."""
    raw = setting(scope, "approvals.mode")
    if isinstance(raw, bool):
        return "manual" if raw else "off"
    text = str(raw).strip().lower()
    if text in _MODES:
        return cast(ApprovalMode, text)
    if text:
        log.warning("unknown approvals.mode %r; using manual", raw)
    return "manual"
