"""A fake ``SessionHost`` for the goals, loops and heartbeat tests (P10-24..26).

Sessions in ``sessions`` are attached; ``busy`` ones run a turn; ``waiting`` ones have input
queued. ``inject`` starts a "turn" (the session becomes busy, its generation grows) and keeps the
text; ``end_turn`` finishes it and tells the listeners; ``submit`` is a message the person sent.
Events from ``notify`` land in ``events``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from indus.core.session_scheduler import TurnEnd, TurnEndListener
from indus.protocol.events import EventBase


@dataclass
class FakeHost:
    sessions: set[str] = field(default_factory=lambda: {"s1"})
    busy: set[str] = field(default_factory=set[str])
    waiting: set[str] = field(default_factory=set[str])
    gens: dict[str, int] = field(default_factory=dict[str, int])
    injected: list[tuple[str, str, str]] = field(default_factory=list)
    events: list[EventBase] = field(default_factory=list[EventBase])
    listeners: list[TurnEndListener] = field(default_factory=list[TurnEndListener])

    def attached(self) -> Sequence[str]:
        return sorted(self.sessions)

    def is_idle(self, session_id: str) -> bool:
        return (
            session_id in self.sessions
            and session_id not in self.busy
            and session_id not in self.waiting
        )

    def generation(self, session_id: str) -> int:
        return self.gens.get(session_id, 0)

    async def inject(
        self, session_id: str, text: str, *, origin: str, if_generation: int | None = None
    ) -> bool:
        if not self.is_idle(session_id):
            return False
        if if_generation is not None and self.generation(session_id) != if_generation:
            return False
        self.injected.append((session_id, text, origin))
        self._start(session_id)
        return True

    def notify(self, session_id: str, event: type[EventBase], **fields: Any) -> None:
        self.events.append(event(session_id=session_id, seq=len(self.events), **fields))

    def on_turn_end(self, listener: TurnEndListener) -> Callable[[], None]:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    # -- driving ---------------------------------------------------------------------------------

    def _start(self, session_id: str) -> None:
        self.gens[session_id] = self.generation(session_id) + 1
        self.busy.add(session_id)

    def submit(self, session_id: str) -> None:
        """The person sends a message: a turn starts."""
        self._start(session_id)

    async def end_turn(self, session_id: str, text: str, *, completed: bool = True) -> None:
        self.busy.discard(session_id)
        ended = TurnEnd(
            session_id,
            f"turn-{self.generation(session_id)}",
            text,
            completed,
            self.generation(session_id),
        )
        for listener in list(self.listeners):
            outcome = listener(ended)
            if asyncio.iscoroutine(outcome):
                await outcome

    def texts(self) -> list[str]:
        return [text for _sid, text, _origin in self.injected]

    def of_type(self, kind: type[EventBase]) -> list[Any]:
        return [e for e in self.events if isinstance(e, kind)]
