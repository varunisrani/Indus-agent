"""One agent turn for an HTTP caller, over the session service (P15-30..32).

``SessionDesk`` serves one HTTP surface (the API server or the webhook receiver) from the session
service the daemon built for it:

- ``open(session_id)`` resumes a stored session, or creates one with the surface's toolsets;
- ``start(session_id, text, run_context=...)`` sets who is watching (``interactive`` for runs
  whose client answers approvals, ``unattended`` otherwise: the P05 approval policy then denies a
  flagged command at once, R02.27), subscribes to the session's events and server requests,
  submits the text and returns a ``Turn``;
- a ``Turn`` hands out its own events (``events()``; other turns of the session are filtered out)
  and its outcome (``outcome()``): final text, exit reason, failure and the turn's token counts;
- a session the desk opened is closed after ``idle_close_s`` with no turn running, unless a turn
  starts again (background work delivered later starts its own turn through the service and
  restarts the timer);
- a turn the desk did not start (a background result the service delivered by itself) is written
  to the ``CompletionLog``, the durable row pollers read (P15-31).

Every method runs on the service's event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from indus.core.session_scheduler import TurnEnd
from indus.foundation import fsio
from indus.foundation.clock import Clock
from indus.foundation.profiles import mkdir_in_profile
from indus.foundation.scope import Scope
from indus.protocol.base import ProtocolModel
from indus.protocol.events import EventBase, ExitReason
from indus.protocol.methods import SessionCreateParams
from indus.service.session_service import SessionService

__all__ = [
    "CompletionLog",
    "FileCompletionLog",
    "SessionDesk",
    "Turn",
    "TurnGate",
    "TurnOutcome",
]

log = logging.getLogger(__name__)

type RunContext = Literal["interactive", "unattended"]

_SHORT = frozenset({ExitReason.MAX_ITERATIONS, ExitReason.BUDGET_EXHAUSTED})


# -- outcome ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    session_id: str
    turn_id: str
    generation: int
    text: str
    exit_reason: ExitReason | None  # None: the turn's result could not be read
    failure: str | None = None
    reasoning: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def completed(self) -> bool:
        return self.exit_reason is ExitReason.COMPLETED

    @property
    def interrupted(self) -> bool:
        """Stopped on purpose (a client or the host); a system interruption is a failure."""
        return self.exit_reason is ExitReason.INTERRUPTED_BY_USER

    @property
    def cut_short(self) -> bool:
        return self.exit_reason in _SHORT

    @property
    def unknown(self) -> bool:
        return self.exit_reason is None


# -- the concurrency gate -----------------------------------------------------------------------


class TurnGate:
    """At most ``limit()`` turns at once (0 = no cap); ``enter`` is synchronous (no await between
    the check and the count)."""

    def __init__(self, limit: Callable[[], int]) -> None:
        self._limit = limit
        self.active = 0

    def enter(self) -> bool:
        cap = self._limit()
        if cap > 0 and self.active >= cap:
            return False
        self.active += 1
        return True

    def leave(self) -> None:
        self.active = max(0, self.active - 1)


# -- completions for pollers --------------------------------------------------------------------


class CompletionLog(Protocol):
    def record(self, session_id: str, turn_id: str, text: str, completed: bool) -> None: ...

    def recent(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]: ...


class FileCompletionLog:
    """``<home>/gateway/api_completions.jsonl``: one JSON row per background completion; the
    newest ``keep`` rows are kept."""

    def __init__(self, scope: Scope, *, keep: int = 1_000) -> None:
        self.scope = scope
        self.keep = keep
        self.path = scope.home / "gateway" / "api_completions.jsonl"

    def _rows(self) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        rows: list[dict[str, Any]] = []
        for line in lines:
            with contextlib.suppress(ValueError):
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)  # pyright: ignore[reportUnknownArgumentType]
        return rows

    def record(self, session_id: str, turn_id: str, text: str, completed: bool) -> None:
        mkdir_in_profile(self.scope.home, "gateway")
        row = {
            "session_id": session_id,
            "turn_id": turn_id,
            "completed": completed,
            "text": text,
            "at": self.scope.clock.now().isoformat(timespec="seconds"),
        }
        with fsio.FileLock(self.path.with_suffix(".lock"), timeout=10.0):
            rows = [*self._rows(), row][-self.keep :]
            data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
            fsio.atomic_write_bytes(self.path, data.encode("utf-8"), mode=0o600)

    def recent(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return [r for r in self._rows() if r.get("session_id") == session_id][-limit:]


# -- one turn -----------------------------------------------------------------------------------


type TurnItem = EventBase | ProtocolModel
"""A session event of this turn, or a server request (approval, clarify) it raised."""


class Turn:
    """A submitted turn: its events and its outcome (see the module notes)."""

    def __init__(self, service: SessionService, session_id: str) -> None:
        self.service = service
        self.session_id = session_id
        self.turn_id = ""
        self.generation = 0
        self._queue: asyncio.Queue[TurnItem | None] = asyncio.Queue()
        self._early: list[TurnItem] = []
        self._undo: list[Callable[[], None]] = []
        self._done: asyncio.Future[TurnOutcome] = asyncio.get_running_loop().create_future()
        self._ended = False

    # listeners (service loop)
    def _on_event(self, event: EventBase) -> None:
        if self._ended:
            return
        turn_id = getattr(event, "turn_id", None)
        if not self.turn_id:
            self._early.append(event)
            return
        if turn_id is not None and turn_id != self.turn_id:
            return
        self._queue.put_nowait(event)

    def _on_request(self, request: ProtocolModel) -> None:
        if self._ended:
            return
        turn_id = getattr(request, "turn_id", None)
        if self.turn_id and turn_id is not None and turn_id != self.turn_id:
            return
        self._queue.put_nowait(request)

    def _bind(self, turn_id: str, generation: int) -> None:
        self.turn_id = turn_id
        self.generation = generation
        early, self._early = self._early, []
        for item in early:
            item_turn = getattr(item, "turn_id", None)
            if item_turn is None or item_turn == turn_id:
                self._queue.put_nowait(item)

    def _finish(self, outcome: TurnOutcome) -> None:
        self._ended = True
        for undo in self._undo:
            undo()
        self._undo.clear()
        self._queue.put_nowait(None)
        if not self._done.done():
            self._done.set_result(outcome)

    @property
    def done(self) -> bool:
        return self._done.done()

    async def events(self) -> AsyncIterator[TurnItem]:
        """This turn's events and requests, in order, until the turn is over."""
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def next_item(self) -> TurnItem | None:
        """The next event or request; None once the turn is over."""
        return await self._queue.get()

    async def outcome(self) -> TurnOutcome:
        return await asyncio.shield(self._done)

    def interrupt(self, *, hard: bool = False) -> bool:
        if self.done:
            return False
        try:
            return self.service.interrupt(self.session_id, self.generation, hard=hard).interrupted
        except Exception:
            log.debug("interrupting turn %s failed", self.turn_id, exc_info=True)
            return False


# -- the desk -----------------------------------------------------------------------------------


@dataclass
class _Idle:
    handle: asyncio.Task[None] | None = None


@dataclass
class SessionDesk:
    """The sessions one HTTP surface runs (see the module notes)."""

    service: SessionService
    clock: Clock
    toolsets: Sequence[str] | None = None
    idle_close_s: float = 900.0
    completions: CompletionLog | None = None
    _mine: set[str] = field(default_factory=set[str])
    _started: set[str] = field(default_factory=set[str])
    _running: dict[str, Turn] = field(default_factory=dict[str, Turn])
    _idle: dict[str, _Idle] = field(default_factory=dict[str, _Idle])
    _tasks: set[asyncio.Task[Any]] = field(default_factory=set[asyncio.Task[Any]])
    _undo: Callable[[], None] | None = None
    _closed: bool = False

    def __post_init__(self) -> None:
        self._undo = self.service.on_turn_end(self._turn_ended)

    # -- sessions -------------------------------------------------------------------------------

    async def open(self, session_id: str | None) -> str:
        """Resume ``session_id`` (ProtocolError when unknown or busy) or create a new session."""
        if session_id:
            result = await self.service.resume(session_id)
            sid = result.session_id
        else:
            toolsets = list(self.toolsets) if self.toolsets is not None else None
            created = await self.service.create(SessionCreateParams(toolsets=toolsets))
            sid = created.session_id
        self._mine.add(sid)
        self._schedule_close(sid)
        return sid

    def tool_names(self, session_id: str) -> list[str]:
        return list(self.service.agent(session_id).tool_names)

    def running(self, session_id: str) -> Turn | None:
        return self._running.get(self.service.current_id(session_id))

    async def start(self, session_id: str, text: str, *, run_context: RunContext) -> Turn:
        sid = self.service.current_id(session_id)
        agent = self.service.agent(sid)
        agent.runtime.run_context = run_context
        turn = Turn(self.service, sid)
        turn._undo.append(self.service.subscribe(sid, turn._on_event))  # pyright: ignore[reportPrivateUsage]
        turn._undo.append(self.service.on_request(sid, turn._on_request))  # pyright: ignore[reportPrivateUsage]
        before = agent.usage.usage
        self._cancel_close(sid)
        try:
            ticket = await self.service.submit(sid, text)
        except BaseException:
            turn._finish(TurnOutcome(sid, "", 0, "", None, "not started"))  # pyright: ignore[reportPrivateUsage]
            self._schedule_close(sid)
            raise
        turn._bind(ticket.turn_id, ticket.require_generation())  # pyright: ignore[reportPrivateUsage]
        self._started.add(ticket.turn_id)
        self._running[sid] = turn
        self._spawn(self._wait(turn, before))
        return turn

    async def _wait(self, turn: Turn, before: Any) -> None:
        sid = turn.session_id
        try:
            result = await self.service.wait(sid, turn.turn_id)
        except BaseException as exc:
            log.warning("the result of turn %s could not be read: %r", turn.turn_id, exc)
            if self._running.get(sid) is turn:
                del self._running[sid]
            turn._finish(TurnOutcome(sid, turn.turn_id, turn.generation, "", None, str(exc)))  # pyright: ignore[reportPrivateUsage]
            if not isinstance(exc, Exception):
                raise
            self._schedule_close(sid)
            return
        else:
            after = self.service.agent(result.session_id).usage.usage
            prompt = _prompt_tokens(after) - _prompt_tokens(before)
            completion = int(after.output_tokens) - int(before.output_tokens)
            outcome = TurnOutcome(
                session_id=result.session_id,
                turn_id=turn.turn_id,
                generation=turn.generation,
                text=result.final_text or "",
                exit_reason=result.exit_reason,
                failure=result.failure_reason,
                reasoning=result.reasoning,
                prompt_tokens=max(0, prompt),
                completion_tokens=max(0, completion),
            )
        finally:
            if self._running.get(sid) is turn:
                del self._running[sid]
        turn._finish(outcome)  # pyright: ignore[reportPrivateUsage]
        self._mine.add(outcome.session_id)
        self._schedule_close(outcome.session_id)

    # -- background completions -----------------------------------------------------------------

    def _turn_ended(self, ended: TurnEnd) -> None:
        if ended.turn_id in self._started:
            self._started.discard(ended.turn_id)
            return
        if ended.session_id not in self._mine:
            return
        if self.completions is not None and ended.final_text.strip():
            try:
                self.completions.record(
                    ended.session_id, ended.turn_id, ended.final_text, ended.completed
                )
            except Exception:
                log.warning("could not record the completion of %s", ended.turn_id, exc_info=True)
        self._schedule_close(ended.session_id)

    # -- idle closing ---------------------------------------------------------------------------

    def _cancel_close(self, session_id: str) -> None:
        idle = self._idle.pop(session_id, None)
        if idle is not None and idle.handle is not None:
            idle.handle.cancel()

    def _schedule_close(self, session_id: str) -> None:
        if self._closed:
            return
        self._cancel_close(session_id)
        idle = _Idle()
        due = self.clock.monotonic() + self.idle_close_s
        idle.handle = self._spawn(self._close_later(session_id, due))
        self._idle[session_id] = idle

    async def _close_later(self, session_id: str, due: float) -> None:
        delay = due - self.clock.monotonic()
        if delay > 0:
            await self.clock.sleep(delay)
        self._idle.pop(session_id, None)
        if session_id in self._running or not self.service.is_idle(session_id):
            return
        self._mine.discard(session_id)
        await self.service.close(session_id)

    # -- tasks ----------------------------------------------------------------------------------

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task: asyncio.Task[Any] = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    @property
    def active(self) -> int:
        return len(self._running)

    async def aclose(self) -> None:
        """Interrupt running turns, stop the timers; the service itself is the daemon's."""
        self._closed = True
        if self._undo is not None:
            self._undo()
            self._undo = None
        for turn in list(self._running.values()):
            turn.interrupt(hard=True)
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


def _prompt_tokens(usage: Any) -> int:
    return int(usage.input_tokens) + int(usage.cache_read_tokens) + int(usage.cache_write_tokens)
