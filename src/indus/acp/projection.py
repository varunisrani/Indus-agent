"""Session events → ACP ``session/update`` notifications (P19-10).

One ``Projection`` per editor connection. The backend's sink calls ``on_event`` (synchronously, on
the loop) for every event of a held session; the projection works out the updates at once and puts
them on one queue that a single sender task drains in order, so:

- updates reach the editor in event order;
- ``flush()`` waits until everything queued so far is on the wire (a prompt calls it before it
  answers, so no update of its turn arrives after the response);
- ``settled(key, turn_id)`` waits until the turn's ``message.complete`` has been projected.

Rules per event (see the task note for the full table):

- events at or below the session's floor ``seq`` are dropped (the floor is the snapshot's last seq
  for an attached session, whose subscription may replay; nothing for an in-process one), and so
  is any seq seen before;
- ``message.delta`` → ``agent_message_chunk`` with one UUID ``messageId`` per assistant message;
  a tool start ends the message, so text after a tool opens a new id; ``reasoning.delta`` →
  ``agent_thought_chunk`` under the same id;
- ``message.complete`` sends only the part of the final text the editor has not seen, records the
  turn as settled (a turn whose reply a retry receipt already showed is not shown again);
- ``tool.started`` / ``tool.completed`` → ``tool_call`` / ``tool_call_update``; ``turn.done``
  closes the turn's calls still open as ``failed``;
- ``todo.updated`` and a ``todo_list`` result → ``plan``; ``session.usage`` → ``usage_update``;
  a title change → ``session_info_update``; a ``notice`` → a short text segment of its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, cast

from indus.acp import tool_kinds
from indus.protocol.events import (
    EventBase,
    ExitReason,
    MessageComplete,
    MessageDelta,
    Notice,
    ReasoningDelta,
    SessionInfo,
    SessionTitle,
    SessionUsage,
    TodoUpdated,
    ToolCompleted,
    ToolStarted,
    TurnDone,
    TurnStarted,
)

if TYPE_CHECKING:
    from indus.acp.agent import AcpAgent

__all__ = [
    "SETTLE_GRACE_S",
    "TOOLS_CHANGED_KEY",
    "OpenTool",
    "Projection",
    "TurnEnd",
    "plan_entries",
    "remainder",
    "tools_changed_text",
]

log = logging.getLogger(__name__)

SETTLE_GRACE_S: Final = 5.0
"""How long a prompt waits for its turn's ``message.complete`` after the turn ended."""
TOOLS_CHANGED_KEY: Final = "tools_changed"
_KEPT_TURNS: Final = 64
_PLAN_STATUS: Final[Mapping[str, str]] = {
    "pending": "pending",
    "in_progress": "in_progress",
    "completed": "completed",
    "cancelled": "completed",
}


def tools_changed_text(servers: str) -> str:
    return (
        f"Editor MCP tools changed ({servers or 'none'}); the new tool list applies from the "
        "next turn.\n"
    )


def remainder(final: str, streamed: str, shown: list[str]) -> str:
    """The part of a turn's final text the editor has not been shown yet."""
    if not final.strip():
        return ""
    if streamed:
        if final.startswith(streamed):
            return final[len(streamed) :]
        if final.strip() == streamed.strip():
            return ""
        return "\n\n" + final
    if final.strip() in {piece.strip() for piece in shown}:
        return ""  # the final answer is commentary that was already streamed before a tool
    return final


def plan_entries(todos: object) -> list[dict[str, Any]] | None:
    """ACP plan entries for a todo list (``[{id, content, status}]``); None when not a list."""
    if not isinstance(todos, list):
        return None
    entries: list[dict[str, Any]] = []
    for raw in cast(list[object], todos):
        if not isinstance(raw, Mapping):
            continue
        item = cast(Mapping[str, Any], raw)
        content = str(item.get("content") or item.get("id") or "").strip()
        if not content:
            continue
        status = str(item.get("status") or "pending")
        if status == "cancelled":
            content = f"[cancelled] {content}"
        entries.append(
            {
                "content": content,
                "priority": "medium",
                "status": _PLAN_STATUS.get(status, "pending"),
            }
        )
    return entries


@dataclass
class OpenTool:
    name: str
    args: Mapping[str, Any]
    turn_id: str


@dataclass(frozen=True)
class TurnEnd:
    turn_id: str
    reason: ExitReason
    failure: str | None = None


@dataclass
class _View:
    floor: int = -1
    epoch: str | None = None
    message_id: str | None = None
    streamed: dict[str, str] = field(default_factory=dict[str, str])
    shown: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    tools: dict[str, OpenTool] = field(default_factory=dict[str, OpenTool])
    title: str | None = None
    usage: Mapping[str, Any] | None = None
    ended: OrderedDict[str, TurnEnd] = field(default_factory=OrderedDict[str, TurnEnd])
    settled: OrderedDict[str, asyncio.Event] = field(
        default_factory=OrderedDict[str, asyncio.Event]
    )
    receipted: set[str] = field(default_factory=set[str])
    receipted_generations: set[int] = field(default_factory=set[int])
    turns: OrderedDict[int, str] = field(default_factory=OrderedDict[int, str])
    """Generation → turn id (from ``turn.started``)."""
    broken: asyncio.Event = field(default_factory=asyncio.Event)

    def settle_event(self, turn_id: str) -> asyncio.Event:
        found = self.settled.get(turn_id)
        if found is None:
            found = self.settled[turn_id] = asyncio.Event()
            while len(self.settled) > _KEPT_TURNS:
                self.settled.popitem(last=False)
        return found


type _Item = tuple[str, dict[str, Any]] | Callable[[], None]


class Projection:
    """See the module notes."""

    def __init__(self, agent: AcpAgent) -> None:
        self.agent = agent
        self._views: dict[str, _View] = {}
        self._queue: asyncio.Queue[_Item] = asyncio.Queue()
        self._pump: asyncio.Task[None] | None = None

    # -- state per session ------------------------------------------------------------------------

    def view(self, key: str) -> _View:
        found = self._views.get(key)
        if found is None:
            found = self._views[key] = _View()
        return found

    def track(self, key: str, floor: int, epoch: str | None = None) -> None:
        """A (re)attached session: events at or below ``floor`` were in its snapshot (of replay
        ``epoch``; another epoch numbers its events anew)."""
        view = self.view(key)
        same = epoch is None or view.epoch is None or epoch == view.epoch
        view.floor = max(view.floor, floor) if same else floor
        view.epoch = epoch or view.epoch
        view.broken = asyncio.Event()

    def usage_of(self, key: str) -> Mapping[str, Any] | None:
        return self.view(key).usage

    def ended(self, key: str, turn_id: str) -> TurnEnd | None:
        return self.view(key).ended.get(turn_id)

    def open_tools(self, key: str) -> list[OpenTool]:
        return list(self.view(key).tools.values())

    def broken(self, key: str) -> asyncio.Event:
        return self.view(key).broken

    # -- the sender -------------------------------------------------------------------------------

    def _put(self, item: _Item) -> None:
        self._queue.put_nowait(item)
        if self._pump is None or self._pump.done():
            self._pump = self.agent.spawn(self._drain())

    def send(self, key: str, update: dict[str, Any]) -> None:
        """Queue one update for the session (sent in order by the sender task)."""
        self._put((key, update))

    async def _drain(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if callable(item):
                item()
                continue
            key, update = item
            held = self.agent.sessions.get(key)
            if held is None:
                continue
            try:
                await self.agent.update(held.editor_id, update)
            except Exception:
                log.debug("an update for %s was not delivered", held.editor_id, exc_info=True)

    async def flush(self) -> None:
        """Return once every update queued before this call has been sent."""
        loop = asyncio.get_running_loop()
        reached: asyncio.Future[None] = loop.create_future()

        def mark() -> None:
            if not reached.done():
                reached.set_result(None)

        self._put(mark)
        await reached

    async def reply_shown(self, key: str, generation: int, *, grace: float) -> bool:
        """True once the reply of the turn with ``generation`` has been projected (waits up to
        ``grace`` seconds for it); the queue is flushed either way."""
        done = self.view(key).settle_event(f"gen:{generation}")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(done.wait(), timeout=grace)
        await self.flush()
        return done.is_set()

    async def settled(self, key: str, turn_id: str, *, grace: float = SETTLE_GRACE_S) -> None:
        """Wait until ``turn_id``'s reply is projected (``message.complete``), at most ``grace``
        seconds, then until everything queued is sent."""
        done = self.view(key).settle_event(turn_id)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(done.wait(), timeout=grace)
        await self.flush()

    # -- message text -----------------------------------------------------------------------------

    def _message_id(self, view: _View) -> str:
        if view.message_id is None:
            view.message_id = str(uuid.uuid4())
        return view.message_id

    def _chunk(self, key: str, view: _View, text: str, *, thought: bool = False) -> None:
        kind = "agent_thought_chunk" if thought else "agent_message_chunk"
        self.send(
            key,
            {
                "sessionUpdate": kind,
                "content": {"type": "text", "text": text},
                "messageId": self._message_id(view),
            },
        )

    def _close_segment(self, view: _View, turn_id: str) -> None:
        streamed = view.streamed.pop(turn_id, "")
        if streamed.strip():
            view.shown.setdefault(turn_id, []).append(streamed)
        view.message_id = None

    def say(self, key: str, text: str) -> None:
        """A text segment of its own (a notice, a command's reply)."""
        view = self.view(key)
        view.message_id = None
        self._chunk(key, view, text)
        view.message_id = None

    def show_receipt(self, key: str, generation: int | None, text: str) -> None:
        """A prompt whose turn settled before this editor saw its reply: its final text, once
        (a ``message.complete`` of that turn arriving later is not shown again)."""
        view = self.view(key)
        if generation is not None:
            view.receipted_generations.add(generation)
            turn_id = view.turns.get(generation)
            if turn_id is not None:
                view.receipted.add(turn_id)
        if text.strip():
            self.say(key, text)

    def title(self, key: str, title: str | None) -> None:
        """``session_info_update`` when the title changed."""
        if not isinstance(title, str) or not title.strip():
            return
        view = self.view(key)
        if title == view.title:
            return
        view.title = title
        self.send(key, {"sessionUpdate": "session_info_update", "title": title})

    def known_title(self, key: str, title: str | None) -> None:
        """The title the editor already has (from a snapshot): no update for it."""
        if isinstance(title, str) and title.strip():
            self.view(key).title = title

    # -- events -----------------------------------------------------------------------------------

    def on_event(self, key: str, event: EventBase) -> None:
        view = self.view(key)
        if event.seq <= view.floor:
            return
        view.floor = event.seq
        try:
            self._project(key, view, event)
        except Exception:
            log.warning("an event of %s could not be shown in the editor", key, exc_info=True)

    def on_gap(self, key: str, info: Mapping[str, Any] | None = None) -> None:
        """The session's stream skipped events: its waiters fail as a transport failure, and
        the seq floor moves to where the stream goes on (another daemon run numbers anew)."""
        view = self.view(key)
        view.broken.set()
        last = (info or {}).get("last_seq")
        if isinstance(last, int) and not isinstance(last, bool):
            view.floor = last
        epoch = (info or {}).get("replay_epoch")
        if isinstance(epoch, str):
            view.epoch = epoch

    def _project(self, key: str, view: _View, event: EventBase) -> None:
        if isinstance(event, TurnStarted):
            view.turns[event.generation] = event.turn_id
            while len(view.turns) > _KEPT_TURNS:
                view.turns.popitem(last=False)
            if event.generation in view.receipted_generations:
                view.receipted.add(event.turn_id)
        elif isinstance(event, MessageDelta):
            if event.text:
                view.streamed[event.turn_id] = view.streamed.get(event.turn_id, "") + event.text
                self._chunk(key, view, event.text)
        elif isinstance(event, ReasoningDelta):
            if event.text:
                self._chunk(key, view, event.text, thought=True)
        elif isinstance(event, ToolStarted):
            self._tool_started(key, view, event)
        elif isinstance(event, ToolCompleted):
            self._tool_completed(key, view, event)
        elif isinstance(event, TurnDone):
            self._turn_done(key, view, event)
        elif isinstance(event, MessageComplete):
            self._message_complete(key, view, event)
        elif isinstance(event, SessionUsage):
            self._usage(key, view, event.usage.model_dump(mode="json"))
        elif isinstance(event, SessionTitle | SessionInfo):
            self.title(key, event.title)
        elif isinstance(event, TodoUpdated):
            entries = plan_entries([item.model_dump(mode="json") for item in event.todos])
            if entries is not None:
                self.send(key, {"sessionUpdate": "plan", "entries": entries})
        elif isinstance(event, Notice):
            if event.key == TOOLS_CHANGED_KEY:
                self.say(key, tools_changed_text(event.text))
            elif event.text.strip():
                self.say(key, event.text.rstrip() + "\n")

    def _tool_started(self, key: str, view: _View, event: ToolStarted) -> None:
        self._close_segment(view, event.turn_id)
        args: dict[str, Any] = dict(event.arguments)
        view.tools[event.call_id] = OpenTool(event.name, args, event.turn_id)
        update: dict[str, Any] = {
            "sessionUpdate": "tool_call",
            "toolCallId": event.call_id,
            "title": tool_kinds.title(event.name, args),
            "kind": tool_kinds.kind_of(event.name),
            "status": "in_progress",
            "rawInput": args,
        }
        content = tool_kinds.start_content(event.name, args)
        if content:
            update["content"] = content
        found = tool_kinds.locations(args)
        if found:
            update["locations"] = found
        self.send(key, update)

    def _tool_completed(self, key: str, view: _View, event: ToolCompleted) -> None:
        opened = view.tools.pop(event.call_id, None)
        name = opened.name if opened is not None else event.name
        update: dict[str, Any] = {
            "sessionUpdate": "tool_call_update",
            "toolCallId": event.call_id,
            "kind": tool_kinds.kind_of(name),
            "status": "completed" if event.ok else "failed",
        }
        content = tool_kinds.result_content(name, event.result)
        if content:
            update["content"] = content
        self.send(key, update)
        if name == "todo_list" and event.ok:
            try:
                data = json.loads(event.result)
            except ValueError:
                return
            todos = cast(Mapping[str, Any], data).get("todos") if isinstance(data, dict) else None
            entries = plan_entries(todos)
            if entries is not None:
                self.send(key, {"sessionUpdate": "plan", "entries": entries})

    def _turn_done(self, key: str, view: _View, event: TurnDone) -> None:
        for call_id, opened in list(view.tools.items()):
            if opened.turn_id != event.turn_id:
                continue
            del view.tools[call_id]
            self.send(
                key,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": call_id,
                    "kind": tool_kinds.kind_of(opened.name),
                    "status": "failed",
                    "content": [tool_kinds.text_content(tool_kinds.ABANDONED_TEXT)],
                },
            )
        failure = event.failure.reason if event.failure is not None else None
        view.ended[event.turn_id] = TurnEnd(event.turn_id, event.reason, failure)
        while len(view.ended) > _KEPT_TURNS:
            view.ended.popitem(last=False)

    def _message_complete(self, key: str, view: _View, event: MessageComplete) -> None:
        streamed = view.streamed.pop(event.turn_id, "")
        shown = view.shown.pop(event.turn_id, [])
        if event.turn_id not in view.receipted:
            rest = remainder(event.text, streamed, shown)
            if rest:
                self._chunk(key, view, rest)
        view.message_id = None
        done = view.settle_event(event.turn_id)
        self._put(done.set)
        for generation, turn_id in view.turns.items():
            if turn_id == event.turn_id:
                self._put(view.settle_event(f"gen:{generation}").set)

    def _usage(self, key: str, view: _View, numbers: Mapping[str, Any]) -> None:
        view.usage = numbers
        size = numbers.get("context_window")
        used = numbers.get("context_tokens")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            return
        used_n = used if isinstance(used, int) and not isinstance(used, bool) and used > 0 else 0
        self.send(key, {"sessionUpdate": "usage_update", "size": size, "used": used_n})
