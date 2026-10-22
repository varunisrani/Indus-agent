"""Service events → notifications for the client (P12-29).

Every event of a watched session is written as a notification whose method is the event's type and
whose params are the whole event (with ``session_id`` and the session's growing ``seq``). On the way
the projector:

- filters tool events by the session's tool-progress mode: ``off`` keeps only failures (also a
  result reporting an error or a non-zero exit code) and the tools whose card is the result
  (``UI_TOOLS``); ``new`` drops a start (and its completion, unless
  it failed) when the same tool started just before in the turn; ``all`` and ``verbose`` keep all;
- trims tool text: results are redacted, then cut — ``verbose`` keeps the last
  ``VERBOSE_CHARS`` characters / ``VERBOSE_LINES`` lines from a line boundary behind an
  omitted-count note, other modes the first line up to ``PREVIEW_CHARS``; string arguments are
  capped (``ARG_CHARS`` / ``VERBOSE_CHARS``);
- adds ``todo.updated`` from a successful ``todo_list`` completion's JSON;
- after a turn ends: ``message.complete`` with the final reply, ``session.usage``, a
  ``session.title`` when the stored title changed, a deferred model switch, then the next queued
  prompt;
- writes server requests as ``server_request`` notifications (and tells the request hub).

Outgoing payloads are validated against their models: a mismatch raises ``ContractViolation`` in
strict mode and is logged otherwise.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from pydantic import ValidationError

from indus.core.session_scheduler import TurnEnd
from indus.protocol.base import ProtocolModel
from indus.protocol.events import (
    EVENTS,
    EventBase,
    ExitReason,
    MessageComplete,
    SessionInfo,
    SessionTitle,
    SessionUsage,
    Status,
    TodoUpdated,
    ToolCompleted,
    ToolStarted,
    TurnDone,
    TurnStarted,
)
from indus.protocol.server_requests import SERVER_REQUEST_KINDS, SERVER_REQUEST_METHOD
from indus.safety.redaction import redact_output
from indus.service import session_header
from indus.service.session_service import SessionService, usage_numbers
from indus.tui_bridge.bridge import ContractViolation, SessionView

if TYPE_CHECKING:
    from indus.tui_bridge.bridge import Bridge

__all__ = [
    "ARG_CHARS",
    "PREVIEW_CHARS",
    "UI_TOOLS",
    "VERBOSE_CHARS",
    "VERBOSE_LINES",
    "Projector",
    "cut_head",
    "cut_tail",
    "failed_result",
    "session_info",
    "usage_numbers",
]

log = logging.getLogger(__name__)

VERBOSE_CHARS = 1_000
VERBOSE_LINES = 16
PREVIEW_CHARS = 160
ARG_CHARS = 200
TODO_TOOL = "todo_list"
UI_TOOLS = frozenset({"clarify", "write_file", "patch", TODO_TOOL})
"""Tools whose card is the deliverable: shown even when tool progress is off."""


# -- text trimming ---------------------------------------------------------------------------------


def cut_tail(text: str, *, chars: int = VERBOSE_CHARS, lines: int = VERBOSE_LINES) -> str:
    """The end of ``text``: at most ``lines`` lines and ``chars`` characters, starting at a line
    boundary, behind a note saying what was left out (``text`` itself when it fits)."""
    if len(text) <= chars and text.count("\n") < lines:
        return text
    parts = text.split("\n")
    start = len(text) - len("\n".join(parts[-lines:]))
    if len(text) - start > chars:
        start = len(text) - chars
        newline = text.find("\n", start)
        if 0 <= newline < len(text) - 1:
            start = newline + 1
    kept = text[start:]
    dropped_lines = text[:start].count("\n")
    note = f"{dropped_lines} lines, {start} chars" if dropped_lines else f"{start} chars"
    return f"[… {note} omitted]\n{kept}"


def cut_head(text: str, *, chars: int = PREVIEW_CHARS) -> str:
    """The first non-empty line of ``text``, at most ``chars`` characters."""
    first = next((line for line in text.splitlines() if line.strip()), "")
    if len(first) > chars:
        return first[: chars - 1] + "…"
    if first.strip() != text.strip():
        return first + " …"
    return first


def _cap(value: Any, limit: int) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[: limit - 1] + "…"
    if isinstance(value, list):
        return [_cap(item, limit) for item in cast(list[Any], value)]
    if isinstance(value, dict):
        return {key: _cap(item, limit) for key, item in cast(dict[str, Any], value).items()}
    return value


# -- payload helpers -------------------------------------------------------------------------------


def session_info(service: SessionService, view: SessionView) -> dict[str, Any]:
    """``SessionInfo`` fields for an attached session (``service.session_header``)."""
    return session_header.session_info(
        service, view.session_id, title=view.title, tool_progress=view.tool_mode
    )


def failed_result(result: str) -> bool:
    """A JSON result that reports a failure the person should see (an error, ``success: false``
    or a non-zero exit code), even when the call itself counts as done."""
    try:
        data = json.loads(result)
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    body = cast(dict[str, Any], data)
    if body.get("success") is False or body.get("ok") is False:
        return True
    error = body.get("error")
    if isinstance(error, str) and error.strip():
        return True
    code = body.get("exit_code")
    return isinstance(code, int) and not isinstance(code, bool) and code != 0


def _todo_fields(result: str) -> dict[str, Any] | None:
    try:
        data = json.loads(result)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    body = cast(dict[str, Any], data)
    todos = body.get("todos")
    if not isinstance(todos, list):
        return None
    items: list[dict[str, Any]] = []
    for raw in cast(list[Any], todos):
        if not isinstance(raw, dict):
            continue
        entry = cast(dict[str, Any], raw)
        item = {
            "id": str(entry.get("id", "")),
            "content": str(entry.get("content", "")),
            "status": str(entry.get("status", "pending")),
        }
        if entry.get("parent") is not None:
            item["parent"] = str(entry["parent"])
        items.append(item)
    try:
        revision = max(0, int(body.get("revision") or 0))
    except (TypeError, ValueError):
        revision = 0
    if not items and revision == 0:
        return None  # a list that was never written: nothing to show
    return {"todos": items, "revision": revision}


# -- the projector ---------------------------------------------------------------------------------


class Projector:
    """Watches sessions and writes their events (see the module notes)."""

    def __init__(self, bridge: Bridge) -> None:
        self.bridge = bridge
        self.sent = 0

    # -- wiring -----------------------------------------------------------------------------------

    def service_ready(self, service: SessionService) -> None:
        service.on_turn_end(self._turn_ended)
        service.set_input_probe(self._has_queued)
        service.on_session_moved(self._moved)

    def watch(self, service: SessionService, view: SessionView) -> None:
        sid = view.session_id
        view.unsubscribe.append(service.subscribe(sid, lambda event: self._on_event(view, event)))
        view.unsubscribe.append(service.on_request(sid, self._on_request))

    def _has_queued(self, session_id: str) -> bool:
        view = self.bridge.view(session_id)
        return bool(view is not None and view.queue)

    def _moved(self, old: str, new: str) -> None:
        view = self.bridge.views.pop(old, None)
        if view is not None:
            view.session_id = new
            self.bridge.views[new] = view

    # -- output -----------------------------------------------------------------------------------

    def check(self, model: type[ProtocolModel], wire: Mapping[str, Any]) -> bool:
        """True when ``wire`` validates against ``model``; strict mode raises otherwise."""
        try:
            model.model_validate(dict(wire))
        except ValidationError as exc:
            problem = f"{model.__name__} payload does not validate: {exc.errors()[0]['msg']}"
            if self.bridge.strict:
                raise ContractViolation(problem) from exc
            log.warning("contract: %s", problem)
            return False
        return True

    def write_event(self, event: EventBase, wire: Mapping[str, Any] | None = None) -> None:
        body = dict(wire) if wire is not None else event.to_wire()
        kind = str(body.get("type", ""))
        self.check(EVENTS.get(kind, type(event)), body)
        if self.bridge.notify(kind, body):
            self.sent += 1

    def emit(self, view: SessionView, kind: type[EventBase], /, **fields: Any) -> None:
        """Send a bridge event on the session's stream (it gets the session's next seq)."""
        service = self.bridge.built
        if service is None:
            return
        service.notify(view.session_id, kind, **fields)

    def _on_request(self, request: ProtocolModel) -> None:
        self.bridge.requests.delivered(request)
        body = request.to_wire()
        kind = str(body.get("kind", ""))
        models = SERVER_REQUEST_KINDS.get(kind)
        if models is not None:
            self.check(models[0], body)
        self.bridge.notify(SERVER_REQUEST_METHOD, body)

    # -- events -----------------------------------------------------------------------------------

    def _on_event(self, view: SessionView, event: EventBase) -> None:
        if isinstance(event, TurnStarted):
            view.turn_id = event.turn_id
            view.last_tool = None
            view.hidden_calls.clear()
        elif isinstance(event, Status):
            view.status = event.kind.value
        elif isinstance(event, TurnDone):
            view.reasons[event.turn_id] = event.reason.value
            view.status = None
        if isinstance(event, ToolStarted):
            wire = self._tool_started(view, event)
        elif isinstance(event, ToolCompleted):
            wire = self._tool_completed(view, event)
        else:
            wire = event.to_wire()
        if wire is not None:
            self.write_event(event, wire)

    def _tool_started(self, view: SessionView, event: ToolStarted) -> dict[str, Any] | None:
        mode = view.tool_mode
        repeat = view.last_tool == event.name
        view.last_tool = event.name
        if event.name not in UI_TOOLS and (mode == "off" or (mode == "new" and repeat)):
            view.hidden_calls.add(event.call_id)
            return None
        limit = VERBOSE_CHARS if mode == "verbose" else ARG_CHARS
        wire = event.to_wire()
        wire["arguments"] = _cap(dict(event.arguments), limit)
        return wire

    def _tool_completed(self, view: SessionView, event: ToolCompleted) -> dict[str, Any] | None:
        if event.name == TODO_TOOL and event.ok:
            todo = _todo_fields(event.result)
            if todo is not None:
                self.emit(view, TodoUpdated, **todo)
        hidden = event.call_id in view.hidden_calls
        view.hidden_calls.discard(event.call_id)
        if hidden and event.ok and not failed_result(event.result):
            return None
        wire = event.to_wire()
        try:
            clean = redact_output(event.result, force=True, scope=self.bridge.scope)
        except Exception:
            log.warning("tool result could not be redacted; withheld", exc_info=True)
            clean = "[result withheld]"
        wire["result"] = cut_tail(clean) if view.tool_mode == "verbose" else cut_head(clean)
        return wire

    # -- turn end ---------------------------------------------------------------------------------

    async def _turn_ended(self, ended: TurnEnd) -> None:
        bridge = self.bridge
        service = bridge.built
        view = bridge.view(ended.session_id)
        if service is None or view is None:
            await self._report_background(ended)
            return
        reason = view.reasons.pop(ended.turn_id, None) or (
            ExitReason.COMPLETED.value
            if ended.completed
            else ExitReason.INTERRUPTED_BY_SYSTEM.value
        )
        self.emit(
            view, MessageComplete, turn_id=ended.turn_id, text=ended.final_text, reason=reason
        )
        try:
            self.emit(view, SessionUsage, usage=usage_numbers(service, view.session_id))
        except Exception:
            log.debug("no usage for %s", view.session_id, exc_info=True)
        from indus.tui_bridge import methods_config, methods_session

        await methods_session.settle_title(bridge, service, view)
        await methods_config.apply_deferred(bridge, service, view)
        await self._next_queued(service, view)

    async def _next_queued(self, service: SessionService, view: SessionView) -> None:
        if not view.queue or self._running(service, view):
            return
        text = view.queue.popleft()
        try:
            await service.submit(view.session_id, text)
        except Exception as exc:
            log.warning("the queued prompt of %s could not start: %s", view.session_id, exc)
            view.queue.appendleft(text)

    @staticmethod
    def _running(service: SessionService, view: SessionView) -> bool:
        try:
            return service.agent(view.session_id).current is not None
        except Exception:
            return False

    async def _report_background(self, ended: TurnEnd) -> None:
        bridge = self.bridge
        parent = bridge.background.pop(ended.session_id, None)
        service = bridge.built
        if parent is None or service is None:
            return
        from indus.protocol.events import Notice, NoticeLevel

        text = ended.final_text.strip() or "(no reply)"
        service.notify(
            parent,
            Notice,
            level=NoticeLevel.INFO,
            text=f"Background task {ended.session_id} finished:\n{text}",
            key=f"background:{ended.session_id}",
        )
        await service.close(ended.session_id)

    def info(self, view: SessionView) -> None:
        """Send ``session.info`` for the session."""
        service = self.bridge.built
        if service is None:
            return
        try:
            fields = session_info(service, view)
        except Exception:
            log.debug("no session info for %s", view.session_id, exc_info=True)
            return
        self.emit(view, SessionInfo, **fields)

    def title(self, view: SessionView, title: str | None) -> None:
        view.title = title
        self.emit(view, SessionTitle, title=title)
