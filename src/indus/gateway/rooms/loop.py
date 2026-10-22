"""The group-room loop: members take turns in the rooms this gateway drives (P24-06; wave 42).

``RoomPart`` (``Stage.BACKGROUND``; ``PART_FACTORIES`` names ``create``; a root-level part, so a
multiplexing host runs it once) runs ``RoomLoop`` over ``<root>/rooms.db``. Every
``ROOM_POLL_S`` it looks at each live room whose authority is this install and, for each:

1. **lease.** ``acquire_lease`` (renewed every pass, ``LEASE_TTL_S``) under this daemon start's
   instance id; a room another process drives (``LeaseHeld``) is left alone. A new or reclaimed
   lease is followed by ``recover``: a turn found running under another fence becomes
   ``indeterminate`` and is never run again on its own (R03.21; an operator resolves it through
   ``groups.retry``).
2. **turn timing.** ``expire_turns`` fails a silent (3 min) or overlong (3 h) turn; its runner is
   stopped and ``turn.expired`` is logged.
3. **whose turn it is** (``RoomPlanner``, replayed deterministically from the log): a person's
   message (``message.user``) starts a chain: the members it @mentions (``@all`` names everyone),
   or the first member when it mentions none, each get a turn; when every turn the chain is
   waiting for has answered, the member after the last speaker (room order, skipping held members
   and the speaker) takes the next turn. A chain holds at most ``MEMBER_TURNS_PER_MESSAGE`` member
   turns; a silent or failed turn, a stop request (``room.stop_requested``) or a newer person
   message ends it. A stop directive next to a member's @mention (``stop @bot``; ``@all stop``)
   holds that member: it takes no turn until a later person message @mentions it again. Each
   planned turn is admitted with ``admit`` (idempotent per task id: replaying the log never
   queues a turn twice). The first pass over a room admits nothing older than the newest of: its
   tasks, its ``turn.*`` events, its last person message — so a room never replays old chains.
4. **one turn at a time.** With nothing in flight the head of the queue starts (``start``); the
   member's turn runs (``MemberRunner``) with a heartbeat every ``HEARTBEAT_S``; its reply is
   appended as ``message.member`` (silence appends nothing), the task settles, and ``turn.settled``
   / ``turn.failed`` is logged. A turn that ``groups.stop`` asked to stop (``stopping``) has its
   runner stopped and the stop acknowledged (``complete_stop``); a result that won the race is
   kept (``settle_stopping``).
5. **upkeep.** ``prune_terminal`` once an hour per room.

The production runner (``daemon_runner``) runs the member's turn in the member profile's own
session service, when this process serves that profile (the daemon's own profile, or any served
profile on a multiplexing host): a fresh session per turn with the room's recent messages in the
prompt, closed afterwards. A member whose profile is not served here fails its turn with
``member.unavailable`` (the fix: serve every member from one gateway,
``gateway.multiplex_profiles``). ``RoomPart.busy_reason`` keeps an auto-started daemon up while a
room turn is queued or running (idle exit).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
from indus.foundation.profiles import is_valid_id
from indus.gateway.daemon.parts import DaemonPart, Stage
from indus.gateway.outbound.filters import is_silence
from indus.gateway.rooms.driver import (
    DriverError,
    DriverLease,
    LeaseHeld,
    RoomDriver,
    StaleTask,
    Task,
    TaskAttempt,
    TaskConflict,
    TaskKey,
    mentioned_members,
    stop_directives,
)
from indus.state.rooms import Room, RoomError, RoomEvent, RoomStore

if TYPE_CHECKING:
    from indus.gateway.daemon.process import GatewayDaemon

__all__ = [
    "HEARTBEAT_S",
    "LEASE_TTL_S",
    "MEMBER_TURNS_PER_MESSAGE",
    "ROOM_POLL_S",
    "MemberTurn",
    "MemberTurnFailed",
    "MemberUnavailable",
    "Planned",
    "RoomLoop",
    "RoomPart",
    "RoomPlanner",
    "create",
    "daemon_runner",
    "member_handle",
    "member_profile",
]

log = logging.getLogger(__name__)

ROOM_POLL_S: Final = 1.0
LEASE_TTL_S: Final = 60.0
HEARTBEAT_S: Final = 30.0
PRUNE_EVERY_S: Final = 3600.0
MEMBER_TURNS_PER_MESSAGE: Final = 8
"""Member turns one person message may set off before the room waits for a person again."""
TRANSCRIPT_EVENTS: Final = 40
MAX_TRANSCRIPT_BYTES: Final = 64 * 1024
MAIN_THREAD: Final = "main"
ROOMS_PLATFORM: Final = "rooms"
_PAGE: Final = 500
_ENDED: Final = ("turn.settled", "turn.failed", "turn.cancelled", "turn.expired")


class MemberUnavailable(Exception):
    """The member's profile is not served by this process."""


class MemberTurnFailed(Exception):
    """The member's turn ran but did not complete."""


def member_handle(member: Mapping[str, Any]) -> str | None:
    """The member's handle in the room (``handle``, else ``profile``, else ``id``)."""
    for key in ("handle", "profile", "id"):
        value = member.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def member_profile(member: Mapping[str, Any]) -> str | None:
    """The local profile that speaks for the member (``profile``, else a handle that is one)."""
    value = member.get("profile")
    if isinstance(value, str) and value.strip():
        return value.strip()
    handle = member_handle(member)
    return handle if handle is not None and is_valid_id(handle) else None


@dataclass(frozen=True)
class MemberTurn:
    """What the runner gets for one member turn."""

    room_id: str
    room_name: str
    handle: str
    profile: str | None
    prompt: str
    task_id: str
    attempt: int


type MemberRunner = Callable[[MemberTurn], Awaitable[str]]


# -- whose turn it is -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Planned:
    """One member turn the log calls for."""

    source_seq: int
    target: str
    thread_id: str
    turn_id: str
    task_id: str
    prompt: str


def _text(payload: Mapping[str, Any]) -> str:
    value = payload.get("text")
    return value if isinstance(value, str) else ""


class RoomPlanner:
    """The room's turn policy, fed every log event in order (see the module notes). Planned
    turns wait in ``owed`` until ``take``: a later event in the same batch can still drop them (a
    newer person message, a stop request, a hold on that member)."""

    def __init__(self, room: Room) -> None:
        self.room_id = room.room_id
        self.name = room.name
        self.handles: list[str] = [h for h in (member_handle(m) for m in room.members) if h]
        self.held: set[str] = set()
        self.pending: list[str] = []
        self.budget = 0
        self.thread = MAIN_THREAD
        self.lines: deque[tuple[str, str]] = deque(maxlen=TRANSCRIPT_EVENTS)
        self.last_user_seq = 0
        self.last_turn_seq = 0
        self.owed: list[Planned] = []

    def take(self) -> list[Planned]:
        """The turns planned since the last ``take``, in order."""
        owed, self.owed = self.owed, []
        return owed

    def _next_after(self, speaker: str) -> str | None:
        if speaker not in self.handles:
            return None
        start = self.handles.index(speaker)
        for step in range(1, len(self.handles)):
            candidate = self.handles[(start + step) % len(self.handles)]
            if candidate != speaker and candidate not in self.held:
                return candidate
        return None

    def _prompt(self, target: str) -> str:
        others = [h for h in self.handles if h != target]
        lines = [f"{who}: {text}" for who, text in self.lines]
        transcript = "\n".join(lines)
        while len(transcript.encode("utf-8")) > MAX_TRANSCRIPT_BYTES and len(lines) > 1:
            lines.pop(0)
            transcript = "\n".join(lines)
        with_whom = ", ".join(others) if others else "nobody else yet"
        return (
            f"You are {target}, a member of the group room {self.name!r} (with {with_whom}). "
            "The room's latest messages, oldest first:\n\n"
            f"{transcript}\n\n"
            "Write your next message to the room. Answer with nothing at all to stay silent."
        )

    def _plan(self, seq: int, targets: Iterable[str]) -> None:
        for index, target in enumerate(targets):
            if self.budget <= 0:
                break
            self.budget -= 1
            self.pending.append(target)
            self.owed.append(
                Planned(
                    source_seq=seq,
                    target=target,
                    thread_id=self.thread,
                    turn_id=f"{seq}.{index}",
                    task_id=f"turn-{seq}-{index}",
                    prompt=self._prompt(target),
                )
            )

    def feed(self, event: RoomEvent) -> None:
        kind, payload = event.kind, event.payload
        actor = str(event.actor.get("id") or "")
        if kind == "message.user":
            text = _text(payload)
            self.last_user_seq = event.seq
            thread = payload.get("thread_id")
            self.thread = thread if isinstance(thread, str) and thread.strip() else MAIN_THREAD
            self.lines.append((actor or "person", text))
            stops = stop_directives(text, self.handles)
            mentioned = mentioned_members(text, self.handles)
            self.held |= stops
            self.held -= {h for h in mentioned if h not in stops}
            self.pending = []
            self.owed = []  # a newer person message supersedes turns not queued yet
            self.budget = MEMBER_TURNS_PER_MESSAGE
            if mentioned:
                targets = [h for h in mentioned if h not in self.held]
            elif stops:
                targets = []
            else:
                targets = [h for h in self.handles if h not in self.held][:1]
            self._plan(event.seq, targets)
            return
        if kind == "message.member":
            self.lines.append((actor, _text(payload)))
            if actor in self.pending:
                self.pending.remove(actor)
            if self.pending or self.budget <= 0:
                return
            following = self._next_after(actor)
            if following is not None:
                self._plan(event.seq, [following])
            return
        if kind == "member.held":
            member = payload.get("member")
            if isinstance(member, str):
                self.held.add(member)
                if member in self.pending:
                    self.pending.remove(member)
                self.owed = [p for p in self.owed if p.target != member]
        elif kind == "member.released":
            member = payload.get("member")
            if isinstance(member, str):
                self.held.discard(member)
        elif kind == "room.stop_requested":
            self.budget = 0
            self.pending = []
            self.owed = []
        elif kind in _ENDED or kind == "member.unavailable":
            member = payload.get("member")
            source = payload.get("source_seq")
            if isinstance(source, int) and not isinstance(source, bool):
                self.last_turn_seq = max(self.last_turn_seq, source)
            if isinstance(member, str) and member in self.pending:
                self.pending.remove(member)
                if kind != "turn.settled" or payload.get("silent") is True:
                    self.budget = 0  # a silent or failed turn ends the chain


# -- the loop -------------------------------------------------------------------------------------


@dataclass
class _Flight:
    task: Task
    attempt: TaskAttempt
    runner: asyncio.Task[None]


@dataclass
class _RoomState:
    planner: RoomPlanner
    cursor: int = 0
    floor: int = 0
    lease: DriverLease | None = None
    flight: _Flight | None = None
    queued: bool = False
    pruned_at: float = 0.0
    seen_members: tuple[str, ...] = field(default_factory=tuple[str, ...])


class RoomLoop:
    """Drives the rooms of one store (see the module notes). ``tick`` is one pass."""

    def __init__(
        self,
        store: RoomStore,
        *,
        process_generation: str,
        runner: MemberRunner,
        clock: Clock,
        driver: RoomDriver | None = None,
        poll: float = ROOM_POLL_S,
        lease_ttl: float = LEASE_TTL_S,
        heartbeat: float = HEARTBEAT_S,
    ) -> None:
        self.store = store
        self.driver = driver if driver is not None else RoomDriver(store)
        self.process_generation = process_generation
        self.runner = runner
        self.clock = clock
        self.poll = poll
        self.lease_ttl = lease_ttl
        self.heartbeat_s = heartbeat
        self.rooms: dict[str, _RoomState] = {}
        self._wake = asyncio.Event()

    # -- facts -----------------------------------------------------------------------------------

    def busy_reason(self) -> str | None:
        for room_id, state in self.rooms.items():
            if state.flight is not None:
                return f"a member's turn runs in group room {room_id}"
            if state.queued:
                return f"a member's turn waits in group room {room_id}"
        return None

    def wake(self) -> None:
        self._wake.set()

    # -- running ---------------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("group rooms: a pass failed; trying again")
            self._wake.clear()
            sleeper = asyncio.ensure_future(self.clock.sleep(self.poll))
            waiter = asyncio.ensure_future(self._wake.wait())
            stopper = asyncio.ensure_future(stop.wait())
            try:
                await asyncio.wait({sleeper, waiter, stopper}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for pending in (sleeper, waiter, stopper):
                    pending.cancel()

    async def tick(self) -> None:
        rooms = await run_blocking(lambda: self.store.list_rooms(limit=256), label="rooms")
        live = {room.room_id for room in rooms}
        for gone in [r for r in self.rooms if r not in live]:
            await self._forget(gone)
        for room in rooms:
            if room.authority != self.store.gateway_id:
                continue
            try:
                await self._room(room)
            except (RoomError, DriverError) as exc:
                log.warning("group room %s: %s", room.room_id, exc)

    async def _forget(self, room_id: str) -> None:
        state = self.rooms.pop(room_id, None)
        if state is not None and state.flight is not None:
            state.flight.runner.cancel()
            with contextlib.suppress(BaseException):
                await state.flight.runner

    async def _room(self, room: Room) -> None:
        state = self.rooms.get(room.room_id)
        handles = tuple(h for h in (member_handle(m) for m in room.members) if h)
        if state is None or state.seen_members != handles:
            state = await self._first_sight(room, state)
        lease = await self._lease(room, state)
        if lease is None:
            return
        driver = self.driver
        for key, why in await run_blocking(driver.expire_turns, lease, label="room turns"):
            await self._expired(room, state, key, why)
        await self._admit_new(room, state)
        tasks = await run_blocking(driver.list_tasks, room.room_id, label="room tasks")
        await self._steer(room, state, tasks)
        now = self.clock.monotonic()
        if now - state.pruned_at >= PRUNE_EVERY_S:
            state.pruned_at = now
            await run_blocking(driver.prune_terminal, room.room_id, label="room prune")

    async def _first_sight(self, room: Room, old: _RoomState | None) -> _RoomState:
        """Replay the whole log once (the planner's state), and set the admission floor."""
        planner = RoomPlanner(room)
        events = await self._read_all(room.room_id, 0)
        for event in events:
            planner.feed(event)
        owed = planner.take()
        tasks = await run_blocking(self.driver.list_tasks, room.room_id, label="room tasks")
        floor = max([t.source_seq for t in tasks] + [planner.last_turn_seq, planner.last_user_seq])
        state = _RoomState(
            planner=planner,
            cursor=events[-1].seq if events else 0,
            floor=floor,
            lease=old.lease if old is not None else None,
            flight=old.flight if old is not None else None,
            pruned_at=old.pruned_at if old is not None else 0.0,
            seen_members=tuple(planner.handles),
        )
        self.rooms[room.room_id] = state
        await self._admit(room, state, owed)  # the newest chain may still be owed its turns
        return state

    async def _read_all(self, room_id: str, since: int) -> list[RoomEvent]:
        events: list[RoomEvent] = []
        cursor = since
        while True:
            page = await run_blocking(
                lambda c=cursor: self.store.read_events(room_id, since_seq=c, limit=_PAGE),
                label="room log",
            )
            events.extend(page.events)
            if not page.has_more or not page.events:
                return events
            cursor = page.events[-1].seq

    async def _lease(self, room: Room, state: _RoomState) -> DriverLease | None:
        try:
            lease = await run_blocking(
                lambda: self.driver.acquire_lease(
                    room.room_id,
                    process_generation=self.process_generation,
                    ttl_seconds=self.lease_ttl,
                ),
                label="room lease",
            )
        except LeaseHeld:
            state.lease = None
            return None
        old = state.lease
        if old is None or lease.reclaimed or old.lease_generation != lease.lease_generation:
            found = await run_blocking(self.driver.recover, lease, label="room recovery")
            if found.get("indeterminate"):
                log.warning(
                    "group room %s: %d turn(s) of an earlier run are indeterminate; they are "
                    "not run again (groups.retry runs one on purpose)",
                    room.room_id,
                    len(found["indeterminate"]),
                )
        state.lease = lease
        return lease

    async def _admit_new(self, room: Room, state: _RoomState) -> None:
        events = await self._read_all(room.room_id, state.cursor)
        for event in events:
            state.cursor = event.seq
            state.planner.feed(event)
        await self._admit(room, state, state.planner.take())

    async def _admit(self, room: Room, state: _RoomState, planned: list[Planned]) -> None:
        for turn in planned:
            if turn.source_seq < state.floor:
                continue
            key = TaskKey(room.room_id, turn.task_id, turn.thread_id, turn.turn_id)
            payload: dict[str, Any] = {
                "target": turn.target,
                "prompt": turn.prompt,
                "source_seq": turn.source_seq,
            }
            try:
                await run_blocking(self.driver.admit, key, payload, label="room admit")
            except TaskConflict:
                log.debug("group room %s: turn %s was admitted differently", room.room_id, key)
            except DriverError as exc:
                log.warning("group room %s: a turn could not be queued: %s", room.room_id, exc)

    async def _steer(self, room: Room, state: _RoomState, tasks: list[Task]) -> None:
        flight = state.flight
        if flight is not None:
            mine = next((t for t in tasks if t.key == flight.task.key), None)
            if mine is not None and mine.status == "stopping":
                await self._stop_flight(state, mine)
            elif (mine is None or mine.status not in ("running", "stopping")) and (
                flight.runner.done()
            ):
                state.flight = None
        state.queued = any(t.status == "queued" for t in tasks)
        if state.flight is not None or state.lease is None:
            return
        if any(t.status in ("running", "stopping", "indeterminate") for t in tasks):
            return
        head = next((t for t in tasks if t.status == "queued"), None)
        if head is None:
            return
        lease = state.lease
        try:
            attempt = await run_blocking(
                lambda: self.driver.start(head.key, lease, expected_cancel_gen=head.cancel_gen),
                label="room start",
            )
        except (StaleTask, DriverError) as exc:
            log.debug("group room %s: turn %s did not start: %s", room.room_id, head.key, exc)
            return
        runner = asyncio.ensure_future(self._turn(room, state, head, attempt))
        runner.set_name(f"group room {room.room_id} turn {head.key.task_id}")
        state.flight = _Flight(head, attempt, runner)

    async def _stop_flight(self, state: _RoomState, task: Task) -> None:
        flight = state.flight
        if flight is None:
            return
        flight.runner.cancel()
        with contextlib.suppress(BaseException):
            await flight.runner
        state.flight = None
        if task.cancel_id is None:
            return
        cancel_id = task.cancel_id
        try:
            await run_blocking(
                lambda: self.driver.complete_stop(
                    task.key, cancel_id=cancel_id, expected_cancel_gen=task.cancel_gen
                ),
                label="room stop",
            )
        except (StaleTask, DriverError):
            return
        await self._log_end(task, "turn.cancelled", {"reason": "stopped"}, gen=task.exec_gen)

    async def _expired(self, room: Room, state: _RoomState, key: TaskKey, why: str) -> None:
        flight = state.flight
        if flight is not None and flight.task.key == key:
            flight.runner.cancel()
            with contextlib.suppress(BaseException):
                await flight.runner
            state.flight = None
        task = await run_blocking(self.driver.get_task, key, label="room task")
        log.warning(
            "group room %s: %s's turn expired (%s)", room.room_id, task.payload.get("target"), why
        )
        await self._log_end(task, "turn.expired", {"reason": why}, gen=task.exec_gen)

    # -- one member turn -------------------------------------------------------------------------

    async def _turn(self, room: Room, state: _RoomState, task: Task, attempt: TaskAttempt) -> None:
        target = str(task.payload.get("target"))
        member = next((m for m in room.members if member_handle(m) == target), {"handle": target})
        turn = MemberTurn(
            room_id=room.room_id,
            room_name=room.name,
            handle=target,
            profile=member_profile(member),
            prompt=str(task.payload.get("prompt") or ""),
            task_id=task.key.task_id,
            attempt=attempt.exec_gen,
        )
        await self._log(room, task, "turn.started", {}, gen=attempt.exec_gen)
        beat = asyncio.ensure_future(self._heartbeats(attempt))
        try:
            reply = await self.runner(turn)
        except MemberUnavailable as exc:
            await self._log(
                room, task, "member.unavailable", {"reason": str(exc)[:500]}, gen=attempt.exec_gen
            )
            await self._finish(room, task, attempt, "failed", {"reason": "member_unavailable"})
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            why = str(exc)[:500] or type(exc).__name__
            log.warning("group room %s: %s's turn failed: %s", room.room_id, target, why)
            await self._finish(room, task, attempt, "failed", {"reason": "error", "error": why})
            return
        finally:
            beat.cancel()
        text = (reply or "").strip()
        silent = not text or is_silence(text)
        if not silent:
            try:
                await run_blocking(
                    lambda: self.store.append_event(
                        room.room_id,
                        f"reply:{task.key.task_id}",
                        "message.member",
                        {"kind": "member", "id": target, **_profile_of(turn)},
                        {"text": text, "source_seq": task.source_seq},
                        authority=attempt.lease.gateway_id,
                        epoch=attempt.lease.epoch,
                    ),
                    label="room reply",
                )
            except RoomError as exc:
                log.warning("group room %s: %s's reply was not kept: %s", room.room_id, target, exc)
                await self._finish(room, task, attempt, "failed", {"reason": "reply_refused"})
                return
        await self._finish(room, task, attempt, "settled", {"silent": silent, "chars": len(text)})

    async def _heartbeats(self, attempt: TaskAttempt) -> None:
        while True:
            await self.clock.sleep(self.heartbeat_s)
            try:
                await run_blocking(self.driver.heartbeat, attempt, label="room heartbeat")
            except StaleTask:
                return  # the attempt is over (settled, stopped, expired or fenced)
            except DriverError:
                continue  # the lease is being renewed; the next beat counts

    async def _finish(
        self,
        room: Room,
        task: Task,
        attempt: TaskAttempt,
        status: Any,
        result: dict[str, Any],
    ) -> None:
        settlement = f"settle-{task.key.task_id}-{attempt.exec_gen}"
        try:
            await run_blocking(
                lambda: self.driver.settle(
                    attempt, settlement_id=settlement, status=status, result=result
                ),
                label="room settle",
            )
        except StaleTask:
            current = await run_blocking(self.driver.get_task, task.key, label="room task")
            if current.status != "stopping":
                log.warning(
                    "group room %s: turn %s moved on before it settled (%s)",
                    room.room_id,
                    task.key.task_id,
                    current.status,
                )
                return
            try:  # the result won the race with a stop nobody acknowledged yet
                await run_blocking(
                    lambda: self.driver.settle_stopping(
                        task.key,
                        attempt.lease,
                        expected_exec_gen=current.exec_gen,
                        expected_cancel_gen=current.cancel_gen,
                        settlement_id=settlement,
                        status=status,
                        result=result,
                    ),
                    label="room settle",
                )
            except (StaleTask, DriverError):
                return
        kind = "turn.settled" if status == "settled" else "turn.failed"
        await self._log_end(task, kind, result, gen=attempt.exec_gen)
        self.wake()

    async def _log(
        self, room: Room, task: Task, kind: str, extra: Mapping[str, Any], *, gen: int
    ) -> None:
        payload = {
            "task_id": task.key.task_id,
            "member": task.payload.get("target"),
            "source_seq": task.source_seq,
            **extra,
        }
        try:
            await run_blocking(
                lambda: self.store.append_event(
                    room.room_id,
                    f"{kind}:{task.key.task_id}:{gen}",
                    kind,
                    {"kind": "gateway", "id": room.authority},
                    payload,
                    authority=room.authority,
                    epoch=room.epoch,
                ),
                label="room log",
            )
        except RoomError as exc:
            log.debug("group room %s: %s not logged: %s", room.room_id, kind, exc)

    async def _log_end(self, task: Task, kind: str, extra: Mapping[str, Any], *, gen: int) -> None:
        try:
            room = await run_blocking(self.store.get_room, task.key.room_id, label="room")
        except RoomError:
            return
        await self._log(room, task, kind, extra, gen=gen)

    # -- stop ------------------------------------------------------------------------------------

    async def aclose(self) -> None:
        """Stop the turns this process runs (each fails as ``gateway_stopping`` so the room is
        not left waiting on an indeterminate turn), then release the leases."""
        for room_id, state in list(self.rooms.items()):
            flight = state.flight
            if flight is not None:
                flight.runner.cancel()
                with contextlib.suppress(BaseException):
                    await flight.runner
                state.flight = None
                try:
                    room = await run_blocking(self.store.get_room, room_id, label="room")
                    await self._finish(
                        room, flight.task, flight.attempt, "failed", {"reason": "gateway_stopping"}
                    )
                except (RoomError, DriverError):
                    pass
            lease = state.lease
            if lease is not None:
                with contextlib.suppress(RoomError, DriverError):
                    await run_blocking(self.driver.release_lease, lease, label="room lease")
        self.rooms.clear()


def _profile_of(turn: MemberTurn) -> dict[str, str]:
    return {"profile": turn.profile} if turn.profile else {}


# -- the production runner ------------------------------------------------------------------------


def daemon_runner(daemon: GatewayDaemon) -> MemberRunner:
    """Run a member's turn in the member profile's session service, when this process serves it."""

    async def run(turn: MemberTurn) -> str:
        from indus.gateway.core.shared_host import principal_for
        from indus.protocol.events import ExitReason
        from indus.protocol.methods import SessionCreateParams

        profile = turn.profile
        mux = daemon.mux
        if profile is None:
            raise MemberUnavailable(f"{turn.handle} names no local profile")
        if mux is not None:
            serving = mux.daemon_for(profile)
        else:
            serving = daemon if profile == daemon.scope.profile_id else None
        pipeline = getattr(serving, "pipeline", None) if serving is not None else None
        if pipeline is None:
            raise MemberUnavailable(
                f"profile {profile} is not served by this gateway; serve every member from one "
                "gateway (gateway.multiplex_profiles)"
            )
        sid = await pipeline.create_session(
            SessionCreateParams(toolsets=pipeline.toolsets_for(ROOMS_PLATFORM))
        )
        try:
            shared = pipeline.shared
            if shared is not None:
                # Shielded: a stop that lands while the input is being admitted still learns
                # the admission, and stops exactly that turn.
                admitting = asyncio.ensure_future(
                    shared.submit(
                        sid,
                        turn.prompt,
                        principal=principal_for(ROOMS_PLATFORM, turn.room_id),
                        request_id=f"room-{turn.task_id}-{turn.attempt}-{secrets.token_hex(4)}",
                    )
                )
                try:
                    receipt = await asyncio.shield(admitting)
                    settled = await shared.wait(receipt.admission_id)
                except asyncio.CancelledError:
                    with contextlib.suppress(Exception):
                        await shared.stop(sid, (await admitting).admission_id)
                    raise
                if settled.outcome != "completed":
                    raise MemberTurnFailed(settled.detail or settled.outcome or "failed")
                return settled.final_text or ""
            service = pipeline.service
            starting = asyncio.ensure_future(service.submit(sid, turn.prompt))
            try:
                ticket = await asyncio.shield(starting)
                result = await service.wait(sid, ticket.turn_id)
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    await starting
                    service.interrupt(sid, hard=True)
                raise
            if result.exit_reason is not ExitReason.COMPLETED:
                raise MemberTurnFailed(result.failure_reason or result.exit_reason.value)
            return result.final_text or ""
        finally:
            with contextlib.suppress(Exception):
                await pipeline.close_session(sid)

    return run


# -- the daemon part ------------------------------------------------------------------------------


class RoomPart(DaemonPart):
    """See the module notes."""

    name = "rooms"
    stage = Stage.BACKGROUND

    def __init__(self, *, runner: MemberRunner | None = None, poll: float = ROOM_POLL_S) -> None:
        self._runner = runner
        self._poll = poll
        self.store: RoomStore | None = None
        self.loop: RoomLoop | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self, daemon: GatewayDaemon) -> None:
        store = await run_blocking(RoomStore, daemon.scope, label="room store")
        self.store = store
        self.loop = RoomLoop(
            store,
            process_generation=daemon.instance_id,
            runner=self._runner if self._runner is not None else daemon_runner(daemon),
            clock=daemon.clock,
            poll=self._poll,
        )
        self._stop.clear()
        self._task = asyncio.ensure_future(self.loop.run(self._stop))

    def busy_reason(self) -> str | None:
        return self.loop.busy_reason() if self.loop is not None else None

    async def stop(self, daemon: GatewayDaemon) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            try:
                await asyncio.wait_for(task, 5.0)
            except BaseException:
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        loop, self.loop = self.loop, None
        if loop is not None:
            try:
                await loop.aclose()
            except Exception:
                log.warning("group rooms did not stop cleanly", exc_info=True)
        store, self.store = self.store, None
        if store is not None:
            with contextlib.suppress(Exception):
                store.close()


def create(_daemon: GatewayDaemon) -> RoomPart:
    """``PART_FACTORIES`` entry."""
    return RoomPart()
