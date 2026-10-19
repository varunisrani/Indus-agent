"""Goals, loops, heartbeats, side questions and mixture-of-agents arming for one session service
(P10-21..26): the surface-independent side of ``/goal``, ``/subgoal``, ``/loop``, ``/heartbeat``,
``/btw`` and ``/moa``.

``SessionAutomation(service)`` wires the core drivers to the service, which is their
``SessionHost``: metadata goes to the service's store (``session_meta``; in memory for a memory
store), the goal and ``--until`` judge is the host's auxiliary client (task ``goal_judge``), goal
gates run through the terminal tool, and one ``SessionScheduler`` task polls due loops and
heartbeats. ``start()`` must run on the service's loop; ``stop()`` ends the scheduler.

``command(name, session_id, args)`` runs one command and answers a ``CommandReply`` (lines to show,
and text the surface sends as the next message — the goal's objective, a resumed goal's
continuation, a ``/moa`` prompt). Every surface (classic CLI, TUI bridge, gateway) calls this one
method; only printing differs.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Final

from indus.core import moa
from indus.core.goals import (
    AuxJudge,
    CommandReply,
    GateRunner,
    GoalDriver,
    GoalJudge,
    TerminalGates,
)
from indus.core.heartbeat import HeartbeatDriver
from indus.core.loops import DEFAULT_MAX_TICKS, DEFAULT_MIN_INTERVAL_S, LoopDriver
from indus.core.moa_presets import MoaSettings, settings_for
from indus.core.persist import StateStore
from indus.core.session_scheduler import MemoryMeta, MetaStore, SessionScheduler, StoreMeta
from indus.core.side_question import SideQuestionUnavailable, answer_side_question
from indus.foundation.errors import IndusError
from indus.providers.auxiliary import AuxClient
from indus.service.session_service import SessionService

__all__ = ["COMMANDS", "SessionAutomation"]

log = logging.getLogger(__name__)

COMMANDS: Final = ("goal", "subgoal", "loop", "heartbeat", "btw", "moa")
"""The slash commands this module answers (canonical registry names)."""

MOA_USAGE: Final = "/moa [preset] <prompt> · /moa on [preset] · /moa off · /moa status"


class _NoJudge:
    """Without an auxiliary client every judge call fails (goals fail open, then pause)."""

    async def __call__(self, objective: str, subgoals: Sequence[str], response: str) -> str:
        raise SideQuestionUnavailable("no auxiliary model is configured for the goal judge")


def _config_int(service: SessionService, key: str, default: int) -> int:
    view = service.scope.config
    if view is None:
        return default
    try:
        value = view.get(key)
    except Exception:
        return default
    return value if isinstance(value, int) and not isinstance(value, bool) else default


class SessionAutomation:
    """The P10 session features of one service (see the module notes)."""

    def __init__(
        self,
        service: SessionService,
        *,
        aux: AuxClient | None = None,
        meta: MetaStore | None = None,
        judge: GoalJudge | None = None,
        gates: GateRunner | None = None,
        settings: MoaSettings | None = None,
    ) -> None:
        self.service = service
        scope = service.scope
        clock = scope.clock
        self.aux = aux if aux is not None else service.aux
        store = service.store
        if meta is None:
            meta = StoreMeta(store.handle) if isinstance(store, StateStore) else MemoryMeta()
        self.meta = meta
        if judge is None:
            judge = AuxJudge(self.aux) if self.aux is not None else _NoJudge()
        self.goals = GoalDriver(
            service,
            meta,
            clock,
            judge=judge,
            gates=gates if gates is not None else TerminalGates(scope),
            default_max_turns=max(1, _config_int(service, "goals.max_turns", 20)),
        )
        self.loops = LoopDriver(
            service,
            meta,
            clock,
            judge=judge,
            min_interval_s=_config_int(
                service, "loops.min_interval_seconds", DEFAULT_MIN_INTERVAL_S
            ),
            max_ticks=_config_int(service, "loops.max_ticks", DEFAULT_MAX_TICKS),
            held=self._goal_active,
        )
        self.heartbeats = HeartbeatDriver(service, meta, clock)
        self.scheduler = SessionScheduler(service, meta, clock, [self.loops, self.heartbeats])
        self._settings = settings
        self._undo_moved: Callable[[], None] | None = None

    def start(self) -> None:
        """Listen to turn ends and session moves, start the scheduler (on the service's loop)."""
        self.goals.attach()
        self.loops.attach()
        if self._undo_moved is None:
            self._undo_moved = self.service.on_session_moved(self.move)
        self.scheduler.start()

    async def stop(self) -> None:
        self.goals.detach()
        self.loops.detach()
        if self._undo_moved is not None:
            self._undo_moved()
            self._undo_moved = None
        await self.scheduler.stop()

    async def move(self, old: str, new: str) -> None:
        """A rotating compaction continued ``old`` in ``new``: goal, loops, heartbeats and a
        ``/moa`` arm follow the conversation."""
        moa.ARMED.move(self.service.scope, old, new)
        try:
            await self.goals.store.move(old, new)
            await self.loops.move(old, new)
            await self.heartbeats.move(old, new)
        except Exception:
            log.warning("could not carry goals and loops from %s to %s", old, new, exc_info=True)

    async def _goal_active(self, session_id: str) -> bool:
        state = await self.goals.store.load(session_id)
        return state is not None and state.status == "active"

    @property
    def moa_settings(self) -> MoaSettings:
        return self._settings if self._settings is not None else settings_for(self.service.scope)

    async def command(self, name: str, session_id: str, args: str) -> CommandReply:
        """Run ``/name args`` for the session (see the module notes)."""
        session_id = self.service.current_id(session_id)
        if name == "goal":
            return await self.goals.goal_command(session_id, args)
        if name == "subgoal":
            return await self.goals.subgoal_command(session_id, args)
        if name == "loop":
            reply = await self.loops.command(session_id, args)
            await self.scheduler.poll_once()  # a new loop's first run starts now when idle
            return reply
        if name == "heartbeat":
            return await self.heartbeats.command(session_id, args)
        if name == "btw":
            return await self._btw(session_id, args)
        if name == "moa":
            return self._moa(session_id, args)
        raise ValueError(f"/{name} is not an automation command")

    async def _btw(self, session_id: str, question: str) -> CommandReply:
        if not question.strip():
            return CommandReply(("Ask something: /btw <question>",))
        agent = None
        if session_id in self.service.attached():
            agent = self.service.agent(session_id)
        try:
            answer = await answer_side_question(
                question,
                session_id=session_id,
                agent=agent,
                store=self.service.store,
                aux=self.aux,
            )
        except IndusError as exc:
            return CommandReply((f"No side answer: {exc.message}",))
        return CommandReply(tuple(answer.text.splitlines() or [answer.text]))

    def _plan(self, session_id: str, preset_name: str | None) -> moa.MoaPlan | str:
        settings = self.moa_settings
        preset = settings.preset(preset_name)
        if preset is None:
            if preset_name:
                return f"No MoA preset is called {preset_name!r}. indus moa list shows them."
            if not settings.presets:
                return "No MoA preset yet. Make one with: indus moa configure <name>"
            return "Several MoA presets and no default: name one, or set moa.default_preset."
        if not preset.enabled_references:
            return f"MoA preset {preset.name!r} has no enabled reference model."
        aux = None
        if session_id in self.service.attached():
            aux = self.service.agent(session_id).aux
        aux = aux or self.aux
        if aux is None:
            return "Mixture of agents needs the auxiliary model client, which is not set up here."
        return moa.MoaPlan(
            preset,
            moa.AuxReferences(aux),
            timeout_s=settings.timeout_for(preset),
            privacy=settings.privacy_filter,
        )

    def _moa(self, session_id: str, args: str) -> CommandReply:
        scope = self.service.scope
        word, _, rest = args.strip().partition(" ")
        lowered = word.lower()
        if lowered in ("", "status"):
            armed = moa.ARMED.peek(scope, session_id)
            names = ", ".join(sorted(self.moa_settings.presets)) or "none yet"
            if armed is None:
                state = "off"
            else:
                plan, sticky = armed
                state = f"{'on for every turn' if sticky else 'armed for the next turn'}"
                state += f" (preset {plan.preset.name})"
            return CommandReply((f"Mixture of agents: {state}. Presets: {names}.", MOA_USAGE))
        if lowered == "off" and not rest.strip():
            done = moa.ARMED.disarm(scope, session_id)
            return CommandReply(("Mixture of agents is off." if done else "It was not on.",))
        if lowered == "on" and len(rest.split()) <= 1:
            plan = self._plan(session_id, rest.strip() or None)
            if isinstance(plan, str):
                return CommandReply((plan,))
            moa.ARMED.arm(scope, session_id, plan, sticky=True)
            return CommandReply(
                (
                    f"Mixture of agents is on (preset {plan.preset.name}, "
                    f"{len(plan.preset.enabled_references)} reference model(s)) until /moa off.",
                )
            )
        presets = self.moa_settings.presets
        name: str | None = None
        prompt = args.strip()
        if word in presets and rest.strip():
            name, prompt = word, rest.strip()
        plan = self._plan(session_id, name)
        if isinstance(plan, str):
            return CommandReply((plan,))
        moa.ARMED.arm(scope, session_id, plan, sticky=False)
        refs = ", ".join(slot.label for slot in plan.preset.enabled_references)
        return CommandReply(
            (f"Mixture of agents for this turn ({plan.preset.name}): {refs}",), prompt
        )
