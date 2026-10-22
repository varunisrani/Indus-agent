# pyright: strict
"""Stop gates (A28): checks that may send the agent back to work before a text answer is final.

When the model answers with text and no tool calls, the loop asks ``StopGates.run``. Gates run in
registration order (the LSP gate first, then verify-on-stop, then the ``pre_verify`` plugin hook);
the first one that returns a message wins and the turn goes on: ``continuation_rows`` gives the
candidate answer as an interim assistant row (``finish_reason`` ``verification_required``) and the
message as a user row after it, so roles keep alternating. A gate whose message must survive a
later replay (the LSP gate) is ``durable``; the others' rows are scaffold, dropped when the turn's
final answer is stored. Each gate fires at most ``limit`` times per turn; the candidate answer is
the fallback if the budget then runs out (the loop keeps it). A gate that raises is skipped.

Gates here (P24-17): ``VerifyOnStopGate`` (``verify.on_stop``, off by default) runs the
workspace's ``build`` and ``test`` phases when the turn edited code files and, on failure, hands
the failing command's output back (at most 2 times per turn); ``PreVerifyGate`` asks the
``pre_verify`` plugin hook after code edits (at most 3 times per turn). The LSP gate lives in
``core.stop_gates_lsp`` (P24-16).
"""

from __future__ import annotations

import logging
import os
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import PurePath
from typing import Protocol

from indus.core.transcript import TranscriptRow
from indus.tools.builtin.exec_host import ExecHost
from indus.tools.lsp.servers import find_git_root
from indus.tools.verify.recipes import Recipe, detect_recipe
from indus.tools.verify.runner import VerifyResult, run_verify

__all__ = [
    "FINISH_REASON",
    "MAX_HOOK_NUDGES",
    "MAX_VERIFY_NUDGES",
    "HostFor",
    "PreVerifyGate",
    "PreVerifyHook",
    "StopContext",
    "StopDecision",
    "StopGate",
    "StopGates",
    "VerifyOnStopGate",
    "code_paths",
    "continuation_rows",
    "is_prose",
]

log = logging.getLogger(__name__)

FINISH_REASON = "verification_required"
MAX_VERIFY_NUDGES = 2
MAX_HOOK_NUDGES = 3
MAX_TRACKED_TURNS = 256
MAX_PATHS_SHOWN = 8
ON_STOP_PHASES = ("build", "test")

# Files with nothing to run: a turn that touched only these never triggers a verify gate.
PROSE_SUFFIXES = frozenset(
    {".md", ".markdown", ".mdx", ".rst", ".txt", ".text", ".adoc", ".org", ".log", ".csv", ".tsv"}
)
PROSE_NAMES = frozenset(
    {"license", "licence", "notice", "authors", "contributors", "changelog", "codeowners"}
)


def is_prose(path: str) -> bool:
    name = PurePath(path).name
    suffix = PurePath(name).suffix.lower()
    return suffix in PROSE_SUFFIXES or (not suffix and name.lower() in PROSE_NAMES)


def code_paths(paths: Iterable[str]) -> list[str]:
    return sorted({p for p in paths if p and not is_prose(p)})


@dataclass(frozen=True, slots=True)
class StopContext:
    """What a gate sees: who answers, the candidate answer and the files this turn edited."""

    session_id: str | None
    turn_id: str | None
    answer: str
    edited: tuple[str, ...] = ()
    task_id: str | None = None
    platform: str = ""
    model: str = ""

    @property
    def owner(self) -> str | None:
        return self.session_id or self.task_id


class StopGate(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def limit(self) -> int: ...

    @property
    def durable(self) -> bool: ...

    async def check(self, ctx: StopContext, attempt: int) -> str | None:
        """The message that sends the agent back to work, or None to let the answer stand."""
        ...


@dataclass(frozen=True, slots=True)
class StopDecision:
    gate: str
    nudge: str
    durable: bool
    attempt: int


class StopGates:
    """The registry the loop asks before accepting a text answer (see the module notes)."""

    def __init__(self, gates: Iterable[StopGate] = ()) -> None:
        self._gates: list[StopGate] = list(gates)
        self._counts: OrderedDict[tuple[str, str], dict[str, int]] = OrderedDict()

    def register(self, gate: StopGate) -> None:
        if any(g.name == gate.name for g in self._gates):
            raise ValueError(f"stop gate {gate.name!r} is registered already")
        self._gates.append(gate)

    @property
    def names(self) -> list[str]:
        return [g.name for g in self._gates]

    def fired(self, ctx: StopContext) -> dict[str, int]:
        return dict(self._counts.get(self._key(ctx), {}))

    @staticmethod
    def _key(ctx: StopContext) -> tuple[str, str]:
        return (ctx.owner or "", ctx.turn_id or "")

    async def run(self, ctx: StopContext) -> StopDecision | None:
        key = self._key(ctx)
        counts = self._counts.pop(key, None) or {}
        self._counts[key] = counts
        while len(self._counts) > MAX_TRACKED_TURNS:
            self._counts.popitem(last=False)
        for gate in self._gates:
            done = counts.get(gate.name, 0)
            if done >= gate.limit:
                continue
            try:
                nudge = await gate.check(ctx, done)
            except Exception as exc:
                log.warning("stop gate %s failed: %s: %s", gate.name, type(exc).__name__, exc)
                continue
            if nudge and nudge.strip():
                counts[gate.name] = done + 1
                log.debug("stop gate %s sends the agent back (attempt %d)", gate.name, done + 1)
                return StopDecision(gate.name, nudge.strip(), gate.durable, done + 1)
        return None

    def end_turn(self, ctx: StopContext) -> None:
        self._counts.pop(self._key(ctx), None)


def continuation_rows(
    decision: StopDecision, answer: str, *, reasoning: str | None = None
) -> tuple[TranscriptRow, TranscriptRow]:
    """The interim answer row and the gate's message row, in that order. Both are scaffold for
    a gate that is not durable, so dropping them at the end of the turn keeps roles alternating."""
    interim = TranscriptRow(
        role="assistant",
        content=answer,
        reasoning=reasoning,
        finish_reason=FINISH_REASON,
        scaffold=not decision.durable,
        extra={"stop_gate": decision.gate},
    )
    nudge = TranscriptRow(
        role="user",
        content=decision.nudge,
        scaffold=not decision.durable,
        extra={"stop_gate": decision.gate},
    )
    return interim, nudge


# -- verify-on-stop (P24-17) ----------------------------------------------------------------------

type HostFor = Callable[[StopContext], Awaitable[ExecHost | None]]
type VerifyRun = Callable[[ExecHost, str, Recipe], Awaitable[VerifyResult]]


def workspace_of(path: str) -> str:
    """The folder verify runs in for an edited file: its git work tree, else its folder."""
    return find_git_root(path) or os.path.dirname(os.path.abspath(path))


def _paths_block(paths: Sequence[str]) -> str:
    lines = [f"- {p}" for p in paths[:MAX_PATHS_SHOWN]]
    if len(paths) > MAX_PATHS_SHOWN:
        lines.append(f"- … and {len(paths) - MAX_PATHS_SHOWN} more")
    return "\n".join(lines)


class VerifyOnStopGate:
    """Runs the workspace's build and test phases after code edits; a failure sends the agent
    back with the output (see the module notes)."""

    name = "verify_on_stop"
    durable = False

    def __init__(
        self,
        host_for: HostFor,
        *,
        enabled: bool | Callable[[], bool] = False,
        limit: int = MAX_VERIFY_NUDGES,
        phases: Sequence[str] = ON_STOP_PHASES,
        phase_timeout: float | None = None,
        runner: VerifyRun | None = None,
    ) -> None:
        self._host_for = host_for
        self._enabled = enabled
        self.limit = limit
        self.phases = tuple(phases)
        self.phase_timeout = phase_timeout
        self._runner = runner

    def enabled(self) -> bool:
        return self._enabled() if callable(self._enabled) else bool(self._enabled)

    async def _run(self, host: ExecHost, root: str, recipe: Recipe) -> VerifyResult:
        if self._runner is not None:
            return await self._runner(host, root, recipe)
        if self.phase_timeout is not None:
            return await run_verify(
                host, root, recipe, phases=self.phases, phase_timeout=self.phase_timeout
            )
        return await run_verify(host, root, recipe, phases=self.phases)

    async def check(self, ctx: StopContext, attempt: int) -> str | None:
        if not self.enabled():
            return None
        changed = code_paths(ctx.edited)
        if not changed:
            return None
        roots = list(dict.fromkeys(workspace_of(p) for p in changed))
        host: ExecHost | None = None
        for root in roots:
            recipe = detect_recipe(root)
            if recipe is None or not any(getattr(recipe, phase) for phase in self.phases):
                continue
            host = host or await self._host_for(ctx)
            if host is None:
                return None
            result = await self._run(host, root, recipe)
            if result.ok:
                continue
            return self._nudge(root, recipe, result, changed)
        return None

    @staticmethod
    def _nudge(root: str, recipe: Recipe, result: VerifyResult, changed: Sequence[str]) -> str:
        failed = next((p for p in result.phases if not p.ok), None)
        lines = [
            "[System note: you edited code this turn and the workspace's checks fail "
            f"({recipe.name} in {root}). Read the failure, fix the code, then give your final "
            "answer. If it cannot be fixed, say what blocks it instead of calling the work done.]",
            "",
            "Changed files:",
            _paths_block(changed),
        ]
        if result.refused:
            lines += ["", f"Verification could not run: {result.refused}"]
        elif failed is not None:
            state = "timed out" if failed.timed_out else f"exit code {failed.exit_code}"
            if failed.refused:
                state = f"refused: {failed.refused}"
            lines += ["", f"Failed: [{failed.phase}] {failed.command} ({state})"]
            if failed.tail.strip():
                lines += ["Output (end):", failed.tail.rstrip()]
        return "\n".join(lines)


# -- the pre_verify plugin hook (P24-17) ----------------------------------------------------------

type PreVerifyHook = Callable[..., Awaitable[str | None]]


class PreVerifyGate:
    """After code edits, a ``pre_verify`` plugin hook answering ``{action: continue, message}``
    sends the agent back with that message (at most ``limit`` times per turn)."""

    name = "pre_verify"
    durable = False

    def __init__(self, hook: PreVerifyHook, *, limit: int = MAX_HOOK_NUDGES) -> None:
        self._hook = hook
        self.limit = max(0, limit)

    async def check(self, ctx: StopContext, attempt: int) -> str | None:
        changed = code_paths(ctx.edited)
        if not changed:
            return None
        return await self._hook(
            session_id=ctx.session_id or "",
            platform=ctx.platform,
            model=ctx.model,
            attempt=attempt,
            final_response=ctx.answer,
            changed_paths=changed,
        )
