"""Per-turn loop guardrails for tool calls (A32).

The controller keys calls by a stable hash of ``(tool, canonical args)`` and counts, per turn:

- exact failures (same signature failed again): warn at ``exact_warn``, block at ``exact_block``;
- failures of one tool with any arguments: warn at ``same_tool_warn``, halt at ``same_tool_halt``
  (tools whose failures are ordinary output, such as a shell, never halt on this counter);
- read-only no-progress (same signature, same result): warn at ``idle_warn``, block at
  ``idle_block``;
- per-turn caps on ``web_search`` calls and subagent spawns (``0`` turns a cap off).

Blocks and halts are hard stops and apply only where nobody is watching (``attended`` False);
attended surfaces get warnings only. Caps apply everywhere. A successful mutating tool counts as
progress and clears the failure streaks. A call repeating the previous call's signature with a
byte-identical result (long enough, not an error) is replaced in the context by a short pointer.

The controller only decides; the tool round appends ``guidance`` to the result as it is built (so a
stored row never changes later) and ends the turn on ``halt``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from indus.core import loop_texts as texts

__all__ = [
    "ATTENDED_SURFACES",
    "GuardDecision",
    "GuardrailConfig",
    "ToolGuard",
    "is_attended",
]

type Action = Literal["allow", "warn", "block", "halt"]

ATTENDED_SURFACES = frozenset({"cli", "tui", "desktop", "acp", "api_server", "subagent"})
NOISY_FAILURE_TOOLS = frozenset({"terminal", "process_manage", "execute_code", "web_extract"})
STUB_MIN_CHARS = 512
_ARGS_PREVIEW = 120


def is_attended(platform: str | None) -> bool:
    """Someone watches this surface (warn only). Unknown or empty names count as attended."""
    name = (platform or "").strip().lower()
    return not name or name in ATTENDED_SURFACES


@dataclass(frozen=True)
class GuardrailConfig:
    exact_warn: int = 2
    exact_block: int = 5
    same_tool_warn: int = 3
    same_tool_halt: int = 8
    idle_warn: int = 2
    idle_block: int = 5
    max_web_searches: int = 50
    max_subagents: int = 50
    hard_stops: bool = False
    warnings: bool = True

    @classmethod
    def for_platform(cls, platform: str | None, **overrides: Any) -> GuardrailConfig:
        """Hard stops on for unattended surfaces (cron, gateway, webhook, …)."""
        return cls(hard_stops=not is_attended(platform), **overrides)


@dataclass(frozen=True)
class GuardDecision:
    action: Action = "allow"
    code: str = "allow"
    message: str = ""
    tool: str = ""
    count: int = 0

    @property
    def runs(self) -> bool:
        return self.action in ("allow", "warn")

    @property
    def stops_turn(self) -> bool:
        return self.action in ("block", "halt")


ALLOW = GuardDecision()


def canonical_args(args: object) -> str:
    value: Mapping[str, Any] = cast(Mapping[str, Any], args) if isinstance(args, Mapping) else {}
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def signature(tool: str, args: object) -> str:
    return _digest(f"{tool}\x00{canonical_args(args)}")


def _result_digest(text: str) -> str:
    try:
        parsed = json.loads(text)
    except ValueError:
        return _digest(text)
    return _digest(json.dumps(parsed, sort_keys=True, separators=(",", ":"), default=str))


def _spawns(args: Mapping[str, Any]) -> int:
    action = str(args.get("action") or "").strip().lower()
    if action in ("list", "status", "stop", "steer"):
        return 0
    tasks: object = args.get("tasks")
    return len(cast(list[object], tasks)) if isinstance(tasks, list) and tasks else 1


@dataclass
class ToolGuard:
    """One turn's counters (``reset`` starts a new turn)."""

    config: GuardrailConfig = field(default_factory=GuardrailConfig)
    halt: GuardDecision | None = None
    _exact: dict[str, int] = field(default_factory=dict[str, int])
    _same_tool: dict[str, int] = field(default_factory=dict[str, int])
    _idle: dict[str, tuple[str, int]] = field(default_factory=dict[str, tuple[str, int]])
    _progressed: set[str] = field(default_factory=set[str])
    _last: tuple[str, str] | None = None
    _last_call_id: str = ""
    _searches: int = 0
    _spawned: int = 0

    def reset(self) -> None:
        fresh = ToolGuard(self.config)
        self.__dict__.update(fresh.__dict__)

    # -- decisions -----------------------------------------------------------------------------

    def _decide(
        self, action: Action, code: str, template: str, tool: str, count: int, **extra: Any
    ) -> GuardDecision:
        decision = GuardDecision(
            action, code, template.format(tool=tool, count=count, **extra), tool, count
        )
        if decision.stops_turn and self.halt is None:
            self.halt = decision
        return decision

    def before(
        self, tool: str, args: Mapping[str, Any] | None, *, read_only: bool
    ) -> GuardDecision:
        """Decide whether a call may run (caps first, then the hard-stop counters)."""
        args = args if isinstance(args, Mapping) else {}
        capped = self._cap(tool, args)
        if capped is not None:
            return capped
        if not self.config.hard_stops:
            return ALLOW
        sig = signature(tool, args)
        exact = 0 if sig in self._progressed else self._exact.get(sig, 0)
        if exact >= self.config.exact_block:
            return self._decide(
                "block", "exact_failure_block", texts.GUARD_EXACT_BLOCK, tool, exact
            )
        idle = self._idle.get(sig) if read_only else None
        if idle is not None and idle[1] >= self.config.idle_block:
            return self._decide(
                "block", "no_progress_block", texts.GUARD_NO_PROGRESS_BLOCK, tool, idle[1]
            )
        return ALLOW

    def _cap(self, tool: str, args: Mapping[str, Any]) -> GuardDecision | None:
        if tool == "web_search":
            cap = self.config.max_web_searches
            if cap and self._searches >= cap:
                return self._decide(
                    "block", "web_search_cap", texts.GUARD_CAP, tool, self._searches, cap=cap
                )
            self._searches += 1
        elif tool == "delegate_task":
            cap = self.config.max_subagents
            spawns = _spawns(args)
            if cap and spawns and self._spawned >= cap:
                return self._decide(
                    "block", "subagent_cap", texts.GUARD_CAP, tool, self._spawned, cap=cap
                )
            self._spawned += spawns
        return None

    def after(
        self,
        tool: str,
        args: Mapping[str, Any] | None,
        result_text: str | None,
        *,
        failed: bool,
        read_only: bool,
        mutating: bool,
        call_id: str = "",
    ) -> tuple[GuardDecision, str | None]:
        """Record a finished call; returns the decision and an optional pointer stub."""
        args = args if isinstance(args, Mapping) else {}
        sig = signature(tool, args)
        stub = self._stub(tool, args, sig, result_text, failed, call_id)
        config = self.config
        if failed:
            if sig in self._progressed:
                self._progressed.discard(sig)
                self._exact.pop(sig, None)
            exact = self._exact[sig] = self._exact.get(sig, 0) + 1
            same = self._same_tool[tool] = self._same_tool.get(tool, 0) + 1
            self._idle.pop(sig, None)
            if (
                config.hard_stops
                and tool not in NOISY_FAILURE_TOOLS
                and same >= config.same_tool_halt
            ):
                return self._decide(
                    "halt", "same_tool_halt", texts.GUARD_SAME_TOOL_HALT, tool, same
                ), stub
            if config.warnings and exact >= config.exact_warn:
                return self._decide(
                    "warn", "exact_failure_warn", texts.GUARD_EXACT_WARN, tool, exact
                ), stub
            if config.warnings and same >= config.same_tool_warn:
                return self._decide(
                    "warn", "same_tool_warn", texts.GUARD_SAME_TOOL_WARN, tool, same
                ), stub
            return GuardDecision(tool=tool, count=exact), stub
        self._exact.pop(sig, None)
        self._same_tool.pop(tool, None)
        if mutating:
            self._progressed.update(self._exact)
            self._same_tool.clear()
        if not read_only or mutating:
            self._idle.pop(sig, None)
            return GuardDecision(tool=tool), stub
        digest = _result_digest(result_text or "")
        previous = self._idle.get(sig)
        repeats = previous[1] + 1 if previous is not None and previous[0] == digest else 1
        self._idle[sig] = (digest, repeats)
        if config.warnings and repeats >= config.idle_warn:
            decision = self._decide(
                "warn", "no_progress_warn", texts.GUARD_NO_PROGRESS_WARN, tool, repeats
            )
            return decision, stub
        return GuardDecision(tool=tool, count=repeats), stub

    def _stub(
        self,
        tool: str,
        args: Mapping[str, Any],
        sig: str,
        result_text: str | None,
        failed: bool,
        call_id: str,
    ) -> str | None:
        if result_text is None:
            self._last = None
            return None
        key = (sig, _digest(result_text))
        repeat = self._last == key
        if not repeat:
            self._last = key
            self._last_call_id = call_id
        if not repeat or failed or len(result_text) < STUB_MIN_CHARS:
            return None
        preview = canonical_args(args)
        if len(preview) > _ARGS_PREVIEW:
            preview = preview[:_ARGS_PREVIEW] + "…"
        ref = f" ({self._last_call_id})" if self._last_call_id else ""
        return texts.GUARD_STUB.format(tool=tool, ref=ref, args=preview)


def guidance(decision: GuardDecision) -> str:
    """The suffix appended to a tool result for a warn/halt decision ("" otherwise)."""
    if decision.action not in ("warn", "halt") or not decision.message:
        return ""
    label = texts.GUARD_LABEL_STOP if decision.action == "halt" else texts.GUARD_LABEL_WARN
    return f"\n\n[{label}: {decision.message}]"


def blocked_result(decision: GuardDecision) -> str:
    """The error text a blocked call gets instead of running."""
    return json.dumps(
        {"error": decision.message, "guardrail": decision.code, "count": decision.count},
        ensure_ascii=False,
    )
