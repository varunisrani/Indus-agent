"""Persistent goals: ``/goal`` and ``/subgoal`` (P10-24).

A session holds at most one goal, stored in its metadata (``session_meta`` namespace ``goals``,
key ``goal``), so it survives a restart and comes back on resume.

After a turn of the session ends normally while the goal is active (``GoalDriver.on_turn_end``):

1. the turn is counted against the goal's budget (``max_turns``, 20 by default);
2. the quality gates run first, in order (``GateRunner``: the terminal tool with its approvals,
   ``GATE_TIMEOUT_S`` each, the last ``GATE_OUTPUT_CHARS`` of output kept). A failing gate's output
   becomes the next message; after ``GATE_RETRIES`` further failures in a row the goal pauses;
3. otherwise the judge (auxiliary task ``goal_judge``, ``JUDGE_TIMEOUT_S``, ``JUDGE_MAX_TOKENS``,
   the reply clipped to ``RESPONSE_CHARS``) answers ``{"done": bool, "reason": str, "next"?: str}``.
   Done → the goal is complete. A judge error fails open (the work goes on); ``MAX_PARSE_FAILURES``
   unreadable verdicts or ``MAX_TRANSPORT_FAILURES`` failed calls in a row pause the goal with a
   hint about ``auxiliary.goal_judge``;
4. the budget used up → paused with a notice; else the continuation — an ordinary user message,
   never a system prompt or toolset change — is started through the session host, only when the
   session is idle and no other turn started meanwhile, so a message the person sends always wins.

Each change sends ``goal.update`` (the TUI goal bar). A user command bumps ``revision``; a verdict
that arrives after the goal was changed meanwhile (paused, cleared, replaced) is dropped.

The continuation and judge texts are our own.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Final, Literal, Protocol, cast

from indus.core.session_scheduler import MetaStore, SessionHost, TurnEnd
from indus.foundation.clock import Clock
from indus.foundation.scope import Scope
from indus.protocol.events import GoalUpdate, Notice, NoticeLevel

__all__ = [
    "DEFAULT_MAX_TURNS",
    "GATE_OUTPUT_CHARS",
    "GATE_RETRIES",
    "GATE_TIMEOUT_S",
    "JUDGE_MAX_TOKENS",
    "JUDGE_TASK",
    "JUDGE_TIMEOUT_S",
    "MAX_PARSE_FAILURES",
    "MAX_TRANSPORT_FAILURES",
    "NAMESPACE",
    "RESPONSE_CHARS",
    "AuxJudge",
    "CommandReply",
    "Decision",
    "Gate",
    "GateRun",
    "GateRunner",
    "GoalDriver",
    "GoalJudge",
    "GoalState",
    "GoalStore",
    "TerminalGates",
    "Verdict",
    "continuation_text",
    "evaluate",
    "parse_goal_args",
    "parse_verdict",
]

log = logging.getLogger(__name__)

NAMESPACE: Final = "goals"
KEY: Final = "goal"
DEFAULT_MAX_TURNS: Final = 20
JUDGE_TASK: Final = "goal_judge"
JUDGE_TIMEOUT_S: Final = 30.0
JUDGE_MAX_TOKENS: Final = 4_096
RESPONSE_CHARS: Final = 4_000
GATE_TIMEOUT_S: Final = 300
GATE_RETRIES: Final = 3
GATE_OUTPUT_CHARS: Final = 3_000
MAX_PARSE_FAILURES: Final = 3
MAX_TRANSPORT_FAILURES: Final = 5

type Status = Literal["active", "paused", "done", "cleared"]

CONTINUE_TEXT: Final = (
    "[Standing goal — keep going]\nGoal: {objective}\n{criteria}{hint}\n"
    "Take the next concrete step toward this goal. When the goal is fully reached, say so "
    "plainly and stop. If you need something from me to go on, ask and stop."
)
GATE_TEXT: Final = (
    "[Standing goal — a required check failed]\nGoal: {objective}\n\n"
    "This check must pass before the goal counts as done, and it just failed "
    "(exit {exit_code}, failure {failures} of {allowed}):\n  $ {command}\n"
    "Last output:\n```\n{output}\n```\n"
    "Find and fix the cause, then run the check again. Do not call the goal done while a check "
    "fails; if the check itself is wrong, say so and stop."
)
JUDGE_SYSTEM: Final = (
    "You check whether an assistant has reached a goal it was given. Read the goal, any extra "
    "criteria and the assistant's latest reply. Reply with one JSON object and nothing else:\n"
    '{"done": true or false, "reason": "<one sentence>", "next": "<the next step, optional>"}\n'
    "Say done only when the reply shows the goal and every extra criterion met, with concrete "
    "evidence (an output line, a file excerpt, a test result) rather than a bare claim."
)
JUDGE_USER: Final = "Goal:\n{objective}\n{criteria}\nThe assistant's latest reply:\n{response}"
CONFIG_HINT: Final = (
    "Point auxiliary.goal_judge at a model that follows JSON instructions (provider and model in "
    "config.yaml), then /goal resume."
)


# -- state ----------------------------------------------------------------------------------------


@dataclass
class Gate:
    """A shell command that must pass before the goal may count as done."""

    command: str
    timeout_s: int = GATE_TIMEOUT_S
    failures: int = 0
    last_exit: int | None = None
    last_output: str = ""


@dataclass
class GoalState:
    objective: str
    status: Status = "active"
    turns_used: int = 0
    max_turns: int = DEFAULT_MAX_TURNS
    subgoals: list[str] = field(default_factory=list[str])
    gates: list[Gate] = field(default_factory=list[Gate])
    parse_failures: int = 0
    transport_failures: int = 0
    last_reason: str = ""
    paused_reason: str = ""
    revision: int = 0
    created_at: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: object) -> GoalState | None:
        if not isinstance(raw, Mapping):
            return None
        data = cast(Mapping[str, Any], raw)
        objective = data.get("objective")
        if not isinstance(objective, str) or not objective.strip():
            return None
        status = data.get("status")
        gates: list[Gate] = []
        raw_gates: list[object] = list(data.get("gates") or [])
        for item in raw_gates:
            if isinstance(item, Mapping):
                g = cast(Mapping[str, Any], item)
                command = g.get("command")
                if isinstance(command, str) and command.strip():
                    gates.append(
                        Gate(
                            command,
                            _int(g.get("timeout_s"), GATE_TIMEOUT_S),
                            _int(g.get("failures"), 0),
                            g.get("last_exit") if isinstance(g.get("last_exit"), int) else None,
                            str(g.get("last_output") or ""),
                        )
                    )
        raw_subgoals: list[object] = list(data.get("subgoals") or [])
        subgoals = [str(s) for s in raw_subgoals if str(s).strip()]
        return cls(
            objective=objective,
            status=status if status in ("active", "paused", "done", "cleared") else "paused",
            turns_used=_int(data.get("turns_used"), 0),
            max_turns=_int(data.get("max_turns"), DEFAULT_MAX_TURNS) or DEFAULT_MAX_TURNS,
            subgoals=subgoals,
            gates=gates,
            parse_failures=_int(data.get("parse_failures"), 0),
            transport_failures=_int(data.get("transport_failures"), 0),
            last_reason=str(data.get("last_reason") or ""),
            paused_reason=str(data.get("paused_reason") or ""),
            revision=_int(data.get("revision"), 0),
            created_at=float(data.get("created_at") or 0.0),
        )

    def status_line(self) -> str:
        parts = [f"{self.turns_used}/{self.max_turns} turns"]
        if self.subgoals:
            parts.append(f"{len(self.subgoals)} extra criteria")
        if self.gates:
            parts.append(f"{len(self.gates)} check(s)")
        meta = ", ".join(parts)
        if self.status == "paused":
            why = f" — {self.paused_reason}" if self.paused_reason else ""
            return f"Goal paused ({meta}{why}): {self.objective}"
        if self.status == "done":
            return f"Goal reached ({meta}): {self.objective}"
        return f"Goal active ({meta}): {self.objective}"


def _int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return default


class GoalStore:
    """Reads and writes one session's goal in the metadata store."""

    def __init__(self, meta: MetaStore) -> None:
        self.meta = meta

    async def load(self, session_id: str) -> GoalState | None:
        try:
            raw = await self.meta.get(session_id, NAMESPACE, KEY)
        except Exception:
            log.warning("could not read the goal of session %s", session_id, exc_info=True)
            return None
        state = GoalState.from_json(raw)
        return None if state is None or state.status == "cleared" else state

    async def save(self, session_id: str, state: GoalState) -> None:
        await self.meta.put(session_id, NAMESPACE, KEY, state.to_json(), due_at=None)

    async def remove(self, session_id: str) -> bool:
        return await self.meta.delete(session_id, NAMESPACE, KEY)

    async def move(self, old: str, new: str) -> bool:
        """Carry the goal to the session a rotating compaction continued in (P08-05)."""
        state = await self.load(old)
        if state is None or await self.load(new) is not None:
            return False
        await self.save(new, state)
        await self.remove(old)
        return True


# -- the judge ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    done: bool
    reason: str = ""
    next: str | None = None


def parse_verdict(raw: str) -> Verdict | None:
    """The judge's verdict; None when no JSON object with a boolean ``done`` can be read."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z]*\s*|\s*```$", "", text)
    decoder = json.JSONDecoder()
    for start in [i for i, ch in enumerate(text) if ch == "{"]:
        try:
            data, _end = decoder.raw_decode(text, start)
        except ValueError:
            continue
        if not isinstance(data, Mapping):
            continue
        found = cast(Mapping[str, Any], data)
        done = found.get("done")
        if not isinstance(done, bool):
            return None
        reason = found.get("reason")
        hint = found.get("next")
        return Verdict(
            done,
            reason.strip() if isinstance(reason, str) else "",
            hint.strip() if isinstance(hint, str) and hint.strip() else None,
        )
    return None


def _criteria(subgoals: Sequence[str]) -> str:
    if not subgoals:
        return ""
    lines = "\n".join(f"- {i}. {text}" for i, text in enumerate(subgoals, 1))
    return f"\nExtra criteria (all must also be met):\n{lines}\n"


class GoalJudge(Protocol):
    """Asks the judge model; returns its raw reply text, raises when the call fails."""

    async def __call__(self, objective: str, subgoals: Sequence[str], response: str) -> str: ...


class AuxJudge:
    """``GoalJudge`` over the host's auxiliary client (task ``goal_judge``)."""

    def __init__(self, aux: Any, scope: Scope | None = None) -> None:
        self.aux = aux
        self.scope = scope

    def _timeout(self) -> float:
        task_config = getattr(self.aux, "task_config", None)
        if task_config is not None:
            value = task_config(JUDGE_TASK).get("timeout_s")
            if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
                return float(value)
        return JUDGE_TIMEOUT_S

    async def __call__(self, objective: str, subgoals: Sequence[str], response: str) -> str:
        clipped = response if len(response) <= RESPONSE_CHARS else response[-RESPONSE_CHARS:]
        user = JUDGE_USER.format(
            objective=objective, criteria=_criteria(subgoals), response=clipped
        )
        reply = await self.aux.call(
            JUDGE_TASK,
            [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": user}],
            max_tokens=JUDGE_MAX_TOKENS,
            temperature=0.0,
            timeout_s=self._timeout(),
        )
        return str(reply.text)


# -- gates ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GateRun:
    exit_code: int
    output: str

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


class GateRunner(Protocol):
    async def __call__(self, command: str, *, timeout_s: int, session_id: str) -> GateRun: ...


class TerminalGates:
    """``GateRunner`` through the terminal tool (backends, approvals, redaction, P05)."""

    def __init__(self, scope: Scope, *, broker_for: Any = None) -> None:
        self.scope = scope
        self.broker_for = broker_for

    async def __call__(self, command: str, *, timeout_s: int, session_id: str) -> GateRun:
        from indus.tools.builtin.terminal import TerminalTool

        broker = self.broker_for(session_id) if self.broker_for is not None else None
        raw = await TerminalTool()(
            {"command": command, "timeout": timeout_s},
            scope=self.scope,
            session_id=session_id,
            broker=broker,
        )
        try:
            data = json.loads(raw)
        except ValueError:
            return GateRun(-1, _tail(raw))
        empty: dict[str, Any] = {}
        body: Mapping[str, Any] = (
            cast(Mapping[str, Any], data) if isinstance(data, Mapping) else empty
        )
        code: object = body.get("exit_code")
        output = str(body.get("output") or "")
        if body.get("error"):
            output = f"{output}\n{body['error']}".strip()
        return GateRun(code if isinstance(code, int) else -1, _tail(output))


def _tail(text: str, limit: int = GATE_OUTPUT_CHARS) -> str:
    return text if len(text) <= limit else text[-limit:]


# -- one evaluation -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    """What follows a turn: ``continue`` with ``message``, ``done``, ``paused`` or ``idle``."""

    action: Literal["continue", "done", "paused", "idle"]
    message: str | None = None
    notice: str = ""
    reason: str = ""


def continuation_text(state: GoalState, hint: str | None = None) -> str:
    extra = f"\nSuggested next step: {hint}\n" if hint else ""
    return CONTINUE_TEXT.format(
        objective=state.objective, criteria=_criteria(state.subgoals), hint=extra
    )


def _pause(state: GoalState, why: str, notice: str) -> Decision:
    state.status = "paused"
    state.paused_reason = why
    return Decision("paused", notice=notice, reason=why)


def _budget_pause(state: GoalState, extra: str = "") -> Decision:
    why = f"turn budget used ({state.turns_used}/{state.max_turns})"
    return _pause(
        state,
        why,
        f"Goal paused: {state.turns_used}/{state.max_turns} turns used{extra}. "
        "/goal resume keeps going, /goal clear stops.",
    )


async def _check_gates(state: GoalState, gates: GateRunner, session_id: str) -> Decision | None:
    for gate in state.gates:
        try:
            run = await gates(gate.command, timeout_s=gate.timeout_s, session_id=session_id)
        except Exception as exc:
            log.warning("goal check %r could not run", gate.command, exc_info=True)
            run = GateRun(-1, f"the check could not run: {type(exc).__name__}: {exc}")
        gate.last_exit, gate.last_output = run.exit_code, run.output
        if run.passed:
            gate.failures = 0
            continue
        gate.failures += 1
        if gate.failures > GATE_RETRIES:
            return _pause(
                state,
                f"check failing: {gate.command}",
                f"Goal paused: the check `{gate.command}` failed {gate.failures} times in a "
                f"row (exit {run.exit_code}). Last output:\n{run.output[-600:]}",
            )
        message = GATE_TEXT.format(
            objective=state.objective,
            exit_code=run.exit_code,
            failures=gate.failures,
            allowed=GATE_RETRIES + 1,
            command=gate.command,
            output=run.output,
        )
        return Decision("continue", message, reason=f"check failed: {gate.command}")
    return None


async def evaluate(
    state: GoalState,
    final_text: str,
    *,
    judge: GoalJudge,
    gates: GateRunner | None,
    session_id: str,
) -> Decision:
    """Count the turn, run the gates, then ask the judge; changes ``state`` (see the module
    notes)."""
    if state.status != "active":
        return Decision("idle")
    state.turns_used += 1
    if state.gates and gates is not None:
        failed = await _check_gates(state, gates, session_id)
        if failed is not None:
            if failed.action == "continue" and state.turns_used >= state.max_turns:
                return _budget_pause(state, " (a required check still fails)")
            state.last_reason = failed.reason
            return failed
    if not final_text.strip():
        # Nothing to judge (a reply with no text): go on without counting a judge failure.
        if state.turns_used >= state.max_turns:
            return _budget_pause(state)
        return Decision("continue", continuation_text(state), reason="the reply had no text")
    verdict: Verdict | None = None
    try:
        raw = await judge(state.objective, state.subgoals, final_text)
    except Exception as exc:
        state.transport_failures += 1
        state.parse_failures = 0
        state.last_reason = f"judge call failed: {type(exc).__name__}"
        log.info("goal judge failed (%s); going on", exc)
    else:
        state.transport_failures = 0
        verdict = parse_verdict(raw)
        if verdict is None:
            state.parse_failures += 1
            state.last_reason = "the judge's answer could not be read"
        else:
            state.parse_failures = 0
            state.last_reason = verdict.reason
    if verdict is not None and verdict.done:
        state.status = "done"
        return Decision("done", notice=f"Goal reached: {verdict.reason or state.objective}")
    if state.transport_failures >= MAX_TRANSPORT_FAILURES:
        return _pause(
            state,
            f"judge unreachable {state.transport_failures} times in a row",
            f"Goal paused: the goal judge failed {state.transport_failures} times in a row. "
            + CONFIG_HINT,
        )
    if state.parse_failures >= MAX_PARSE_FAILURES:
        return _pause(
            state,
            f"judge answers unreadable {state.parse_failures} times in a row",
            f"Goal paused: the goal judge gave {state.parse_failures} answers in a row that "
            "were not the JSON verdict. " + CONFIG_HINT,
        )
    if state.turns_used >= state.max_turns:
        return _budget_pause(state)
    hint = verdict.next if verdict is not None else None
    return Decision("continue", continuation_text(state, hint), reason=state.last_reason)


# -- commands -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandReply:
    """What a slash command answers: lines to show, and text to send as the next message."""

    lines: tuple[str, ...]
    submit: str | None = None


_MAX_TURNS = re.compile(r"(?:^|\s)--max-turns(?:\s+|=)(\S+)")
_GATE = re.compile(r"""(?:^|\s)--gate(?:\s+|=)(?:"([^"]*)"|'([^']*)'|(\S+))""")


def parse_goal_args(text: str) -> tuple[str, int | None, list[str]] | str:
    """``(objective, max turns, gate commands)``, or a problem text."""
    rest = text
    max_turns: int | None = None
    found = _MAX_TURNS.search(rest)
    if found:
        raw = found.group(1)
        if not raw.isdigit() or int(raw) < 1:
            return f"--max-turns needs a whole number above 0, not {raw!r}"
        max_turns = int(raw)
        rest = rest[: found.start()] + rest[found.end() :]
    gates: list[str] = []
    while (found := _GATE.search(rest)) is not None:
        command = next(g for g in found.groups() if g is not None).strip()
        if not command:
            return '--gate needs a command, e.g. --gate "pytest -q"'
        gates.append(command)
        rest = rest[: found.start()] + rest[found.end() :]
    objective = " ".join(rest.split())
    if not objective:
        return 'Say what the goal is, e.g. /goal make the tests pass --gate "pytest -q"'
    return objective, max_turns, gates


GOAL_USAGE: Final = (
    '/goal <objective> [--max-turns N] [--gate "<command>"]... · '
    "/goal status | pause | resume | clear"
)
SUBGOAL_USAGE: Final = "/subgoal add <text> | done <number> | list"


class GoalDriver:
    """Runs goals for the sessions of one host (see the module notes)."""

    def __init__(
        self,
        host: SessionHost,
        meta: MetaStore,
        clock: Clock,
        *,
        judge: GoalJudge,
        gates: GateRunner | None = None,
        default_max_turns: int = DEFAULT_MAX_TURNS,
    ) -> None:
        self.host = host
        self.store = GoalStore(meta)
        self.clock = clock
        self.judge = judge
        self.gates = gates
        self.default_max_turns = default_max_turns
        self._undo = None

    def attach(self) -> None:
        """Listen to the host's turn ends (once)."""
        if self._undo is None:
            self._undo = self.host.on_turn_end(self.on_turn_end)

    def detach(self) -> None:
        if self._undo is not None:
            self._undo()
            self._undo = None

    def _update(self, session_id: str, state: GoalState, reason: str = "") -> None:
        self.host.notify(
            session_id,
            GoalUpdate,
            status=state.status,
            goal=state.objective,
            turns_used=state.turns_used,
            max_turns=state.max_turns,
            reason=reason,
        )

    async def on_turn_end(self, ended: TurnEnd) -> None:
        if not ended.completed:
            return
        sid = ended.session_id
        state = await self.store.load(sid)
        if state is None or state.status != "active":
            return
        revision = state.revision
        decision = await evaluate(
            state, ended.final_text, judge=self.judge, gates=self.gates, session_id=sid
        )
        now = await self.store.load(sid)
        if now is None or now.revision != revision or now.status != "active":
            log.info("goal of session %s changed while it was judged; verdict dropped", sid)
            return
        await self.store.save(sid, state)
        self._update(sid, state, decision.reason)
        if decision.notice:
            level = NoticeLevel.INFO if decision.action == "done" else NoticeLevel.WARNING
            self.host.notify(sid, Notice, level=level, text=decision.notice, key="goal")
        if decision.action != "continue" or decision.message is None:
            return
        started = await self.host.inject(
            sid, decision.message, origin="goal", if_generation=ended.generation
        )
        if not started:
            log.info("goal continuation for %s not sent: another message came first", sid)

    # -- slash commands --------------------------------------------------------------------------

    async def goal_command(self, session_id: str, args: str) -> CommandReply:
        word = args.strip().split(maxsplit=1)[0].lower() if args.strip() else ""
        state = await self.store.load(session_id)
        if word in ("", "status"):
            if state is None:
                return CommandReply(("No goal is set.", GOAL_USAGE))
            return CommandReply((state.status_line(),))
        if word == "pause" and args.strip().lower() == "pause":
            if state is None or state.status != "active":
                return CommandReply(("No active goal to pause.",))
            state.status, state.paused_reason = "paused", "paused by you"
            return await self._changed(session_id, state, f"Goal paused: {state.objective}")
        if word == "resume" and args.strip().lower() == "resume":
            if state is None or state.status == "active":
                return CommandReply(("No paused goal to resume.",))
            state.status, state.paused_reason = "active", ""
            state.turns_used = state.parse_failures = state.transport_failures = 0
            for gate in state.gates:
                gate.failures = 0
            reply = await self._changed(session_id, state, f"Goal resumed: {state.objective}")
            return CommandReply(reply.lines, continuation_text(state))
        if word == "clear" and args.strip().lower() == "clear":
            if state is None:
                return CommandReply(("No goal is set.",))
            state.status = "cleared"
            state.revision += 1
            await self.store.save(session_id, state)
            self._update(session_id, state, "cleared by you")
            return CommandReply(("Goal cleared.",))
        parsed = parse_goal_args(args)
        if isinstance(parsed, str):
            return CommandReply((parsed,))
        objective, max_turns, gate_commands = parsed
        fresh = GoalState(
            objective=objective,
            max_turns=max_turns or self.default_max_turns,
            gates=[Gate(cmd) for cmd in gate_commands],
            revision=(state.revision + 1) if state is not None else 1,
            created_at=self.clock.now().timestamp(),
        )
        lines = [f"Goal set ({fresh.max_turns} turns): {objective}"]
        if state is not None and state.status in ("active", "paused"):
            lines.append("(it replaces the previous goal)")
        lines.extend(f"Check before done: $ {cmd}" for cmd in gate_commands)
        await self.store.save(session_id, fresh)
        self._update(session_id, fresh, "set")
        return CommandReply(tuple(lines), objective)

    async def _changed(self, session_id: str, state: GoalState, line: str) -> CommandReply:
        state.revision += 1
        await self.store.save(session_id, state)
        self._update(session_id, state, state.paused_reason)
        return CommandReply((line,))

    async def subgoal_command(self, session_id: str, args: str) -> CommandReply:
        state = await self.store.load(session_id)
        if state is None or state.status not in ("active", "paused"):
            return CommandReply(("Set a goal first: " + GOAL_USAGE,))
        word, _, rest = args.strip().partition(" ")
        word = word.lower()
        rest = rest.strip()
        if word in ("", "list"):
            if not state.subgoals:
                return CommandReply(("No extra criteria. " + SUBGOAL_USAGE,))
            return CommandReply(tuple(f"{i}. {t}" for i, t in enumerate(state.subgoals, 1)))
        if word == "add":
            if not rest:
                return CommandReply((SUBGOAL_USAGE,))
            state.subgoals.append(rest)
            reply = await self._changed(
                session_id, state, f"Criterion {len(state.subgoals)} added."
            )
            return reply
        if word == "done":
            if not rest.isdigit() or not 1 <= int(rest) <= len(state.subgoals):
                return CommandReply((f"Give a criterion number from 1 to {len(state.subgoals)}.",))
            met = state.subgoals.pop(int(rest) - 1)
            return await self._changed(session_id, state, f"Criterion met and removed: {met}")
        return CommandReply((SUBGOAL_USAGE,))
