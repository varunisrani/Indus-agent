"""Session methods of the bridge (P12-29): a thin mapping onto ``SessionService`` and the store.

- ``session.create`` / ``session.resume`` attach the session, start forwarding its events and send
  ``session.info``; resume also accepts an exact title and returns the display projection of the
  stored conversation (multipart content flattened, image parts as ``[image]``) with row ids.
- ``session.list`` / ``session.most_recent`` read the store's picker listing (machine sources and
  subagents stay hidden unless asked for).
- ``session.close`` detaches (the stored row stays); ``session.history``, ``session.title``,
  ``session.undo``, ``session.compress``, ``session.branch``, ``session.usage`` act on one session.

A title set before the session has a stored row is kept and written after its first turn.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, cast

from indus.core.transcript import TranscriptRow
from indus.foundation.blocking import run_blocking
from indus.protocol.bridge import (
    HistoryMessage,
    HistoryRole,
    SessionBranchParams,
    SessionBranchResult,
    SessionCloseParams,
    SessionCloseResult,
    SessionCompressParams,
    SessionCompressResult,
    SessionHistoryParams,
    SessionHistoryResult,
    SessionListParams,
    SessionListResult,
    SessionMostRecentParams,
    SessionMostRecentResult,
    SessionTitleParams,
    SessionTitleResult,
    SessionUndoParams,
    SessionUndoResult,
    SessionUsageParams,
    SessionUsageResult,
    UsageNumbers,
)
from indus.protocol.errors import (
    INVALID_PARAMS,
    SESSION_BUSY,
    SESSION_NOT_FOUND,
    STORE_FAILED,
)
from indus.protocol.events import SessionUsage
from indus.protocol.methods import (
    ProtocolError,
    SessionCreateParams,
    SessionCreateResult,
    SessionResumeParams,
    SessionResumeResult,
)
from indus.safety.redaction import redact_arguments
from indus.service import picker_reads
from indus.service.session_service import SessionService, usage_numbers
from indus.state import listing, rewind, titles
from indus.state.registry import StoreHandle
from indus.tui_bridge.bridge import SessionView

if TYPE_CHECKING:
    from indus.tui_bridge.bridge import Bridge
    from indus.tui_bridge.dispatch import MethodTable

__all__ = ["display_messages", "flatten_content", "register", "settle_title"]

log = logging.getLogger(__name__)

IMAGE_MARK = "[image]"
_IMAGE_PARTS = frozenset({"image", "image_url", "input_image"})
_SHOWN = {"user": HistoryRole.USER, "assistant": HistoryRole.ASSISTANT, "tool": HistoryRole.TOOL}


# -- display projection ----------------------------------------------------------------------------


def flatten_content(content: object) -> str:
    """Text of a stored message's content: parts joined, image parts as ``[image]``."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        for part in cast(list[Any], content):
            if isinstance(part, str):
                pieces.append(part)
                continue
            if not isinstance(part, dict):
                continue
            entry = cast(dict[str, Any], part)
            kind = str(entry.get("type", ""))
            if kind in _IMAGE_PARTS:
                pieces.append(IMAGE_MARK)
            elif isinstance(entry.get("text"), str):
                pieces.append(str(entry["text"]))
        return "\n".join(piece for piece in pieces if piece)
    return str(content)


def _call_arguments(raw: object) -> dict[str, Any] | None:
    """A stored call's arguments as an object (stored as JSON text or already parsed), masked
    for display the way ``tool.started`` masks them; None when they are not an object."""
    value: object = raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw) if raw.strip() else {}
        except ValueError:
            return None
    if not isinstance(value, dict):
        return None
    return redact_arguments(cast(dict[str, Any], value))


def display_messages(rows: Iterable[TranscriptRow]) -> list[HistoryMessage]:
    """The rows a person sees, in order (system, scaffold and hidden rows dropped). A tool row
    carries its call's arguments, so a transcript rebuilt from history shows the same summary and
    Arguments as the live rows did (W47-a)."""
    names: dict[str, str] = {}
    arguments: dict[str, object] = {}
    out: list[HistoryMessage] = []
    for row in rows:
        role = _SHOWN.get(row.role)
        if role is None or row.scaffold or row.display_kind == "hidden":
            continue
        for call in row.tool_calls or ():
            function = call.get("function")
            if isinstance(function, dict) and call.get("id"):
                spec = cast(dict[str, Any], function)
                names[str(call["id"])] = str(spec.get("name", ""))
                arguments[str(call["id"])] = spec.get("arguments")
        text = flatten_content(row.content)
        name = None
        args = None
        if role is HistoryRole.TOOL:
            call_id = row.tool_call_id or ""
            name = row.name or names.get(call_id) or "tool"
            if call_id in arguments:
                args = _call_arguments(arguments[call_id])
        elif not text.strip():
            continue  # an assistant row with only tool calls
        out.append(
            HistoryMessage(role=role, text=text, name=name, arguments=args, row_id=row.row_id)
        )
    return out


# -- helpers ---------------------------------------------------------------------------------------


async def _handle(bridge: Bridge) -> StoreHandle:
    handle = await bridge.any_store_handle()
    if handle is None:
        raise ProtocolError(STORE_FAILED, "this connection has no session store")
    return handle


def _busy_check(service: SessionService, session_id: str) -> None:
    try:
        running = service.agent(session_id).current is not None
    except ProtocolError:
        return
    if running:
        raise ProtocolError(SESSION_BUSY, "stop the running turn first")


async def _stored_title(bridge: Bridge, session_id: str) -> str | None:
    handle = bridge.store_handle()
    if handle is None:
        return None
    try:
        return await run_blocking(titles.get_title, handle, session_id, label="session title")
    except Exception:
        log.debug("title of %s unreadable", session_id, exc_info=True)
        return None


async def settle_title(bridge: Bridge, service: SessionService, view: SessionView) -> None:
    """After a turn: write a title that waited for the stored row, and tell the client when the
    stored title changed (automatic titles arrive this way)."""
    handle = bridge.store_handle()
    if handle is None:
        return
    pending = view.pending_title
    if pending is not None:
        try:
            if await run_blocking(titles.set_title, handle, view.session_id, pending):
                view.pending_title = None
        except titles.TitleTaken as exc:
            log.info("pending title of %s not stored: %s", view.session_id, exc)
            view.pending_title = None
        except Exception:
            log.debug("pending title not stored yet", exc_info=True)
    stored = await _stored_title(bridge, view.session_id)
    if stored is not None and stored != view.title:
        bridge.projector.title(view, stored)


async def _attach(bridge: Bridge, service: SessionService, session_id: str) -> SessionView:
    view = bridge.watch(session_id)
    if view.title is None:
        view.title = await _stored_title(bridge, view.session_id)
    bridge.projector.info(view)
    return view


# -- handlers --------------------------------------------------------------------------------------


async def create(bridge: Bridge, params: SessionCreateParams) -> SessionCreateResult:
    service = await bridge.service()
    created = await service.create(params)
    view = bridge.watch(created.session_id)
    if params.title:
        view.title = titles.sanitize(params.title) or params.title
    bridge.projector.info(view)
    return created


async def resume(bridge: Bridge, params: SessionResumeParams) -> SessionResumeResult:
    service = await bridge.service()
    target = params.session_id
    try:
        resumed = await service.resume(target)
    except ProtocolError as exc:
        if exc.error is not SESSION_NOT_FOUND:
            raise
        handle = bridge.store_handle()
        by_title = (
            await run_blocking(titles.resolve_title, handle, target, label="title lookup")
            if handle is not None
            else None
        )
        if by_title is None:
            raise
        resumed = await service.resume(by_title)
    view = await _attach(bridge, service, resumed.session_id)
    rows = await service.store.load(resumed.session_id)
    return resumed.model_copy(update={"title": view.title, "messages": display_messages(rows)})


async def list_sessions(bridge: Bridge, params: SessionListParams) -> SessionListResult:
    handle = await _handle(bridge)
    return await run_blocking(picker_reads.stored_sessions, handle, params, label="session list")


async def most_recent(bridge: Bridge, params: SessionMostRecentParams) -> SessionMostRecentResult:
    handle = await bridge.any_store_handle()
    if handle is None:
        return SessionMostRecentResult()
    try:
        found = await run_blocking(
            listing.list_sessions, handle, listing.ListFilters(), 5, label="recent session"
        )
    except Exception:
        log.warning("the newest session could not be read", exc_info=True)
        return SessionMostRecentResult()
    for summary in found:
        if summary.source != "unknown":
            return SessionMostRecentResult(session=picker_reads.session_row(summary))
    return SessionMostRecentResult()


async def close(bridge: Bridge, params: SessionCloseParams) -> SessionCloseResult:
    service = bridge.built
    if service is None:
        return SessionCloseResult(closed=False)
    current = service.current_id(params.session_id)
    attached = current in service.attached()
    bridge.forget(current)
    await service.close(current)
    return SessionCloseResult(closed=attached)


async def history(bridge: Bridge, params: SessionHistoryParams) -> SessionHistoryResult:
    service = await bridge.service()
    rows = await service.store.load(service.current_id(params.session_id))
    return SessionHistoryResult(messages=display_messages(rows))


async def title(bridge: Bridge, params: SessionTitleParams) -> SessionTitleResult:
    service = await bridge.service()
    session_id = service.current_id(params.session_id)
    view = bridge.view(session_id)
    handle = bridge.store_handle()
    if params.title is None:
        if view is not None and view.pending_title is not None:
            return SessionTitleResult(title=view.pending_title, pending=True)
        return SessionTitleResult(title=await _stored_title(bridge, session_id))
    clean = titles.sanitize(params.title)
    if handle is None:
        raise ProtocolError(STORE_FAILED, "this connection has no session store")
    try:
        written = await run_blocking(titles.set_title, handle, session_id, clean, label="title")
    except titles.TitleTaken as exc:
        raise ProtocolError(INVALID_PARAMS, str(exc), path="title") from exc
    if not written:
        if session_id not in service.attached():
            raise ProtocolError(SESSION_NOT_FOUND, f"no session {session_id}", path="session_id")
        if view is not None:
            view.pending_title = clean
            bridge.projector.title(view, clean)
        return SessionTitleResult(title=clean, pending=True)
    if view is not None:
        bridge.projector.title(view, clean)
    return SessionTitleResult(title=clean)


async def undo(bridge: Bridge, params: SessionUndoParams) -> SessionUndoResult:
    service = await bridge.service()
    session_id = service.current_id(params.session_id)
    _busy_check(service, session_id)
    handle = await _handle(bridge)
    have = await run_blocking(
        rewind.recent_user_messages, handle, session_id, params.count, label="undo check"
    )
    if not have:
        return SessionUndoResult(removed=0)
    try:
        taken = await run_blocking(
            rewind.rewind, handle, session_id, -min(params.count, len(have)), None, label="undo"
        )
    except rewind.RewindUnavailable:
        return SessionUndoResult(removed=0)
    return SessionUndoResult(removed=taken.removed, prefill=taken.target_text or "")


async def compress(bridge: Bridge, params: SessionCompressParams) -> SessionCompressResult:
    service = await bridge.service()
    outcome = await service.compress(params.session_id, params.focus)
    after = service.current_id(params.session_id)
    message = outcome.reason or {
        "compressed": f"Compressed {outcome.before} messages into {outcome.after}.",
        "nothing": "Nothing to compress yet.",
    }.get(outcome.status, "")
    return SessionCompressResult(status=outcome.status, message=message, session_id=after)


async def branch(bridge: Bridge, params: SessionBranchParams) -> SessionBranchResult:
    service = await bridge.service()
    session_id = service.current_id(params.session_id)
    _busy_check(service, session_id)
    handle = await _handle(bridge)
    try:
        child = await run_blocking(
            lambda: rewind.branch(handle, session_id, title=params.title), label="branch"
        )
    except titles.TitleTaken as exc:
        raise ProtocolError(INVALID_PARAMS, str(exc), path="title") from exc
    except rewind.BranchRefused as exc:
        raise ProtocolError(SESSION_NOT_FOUND, str(exc), path="session_id") from exc
    return SessionBranchResult(session_id=child, title=await _stored_title(bridge, child))


async def usage(bridge: Bridge, params: SessionUsageParams) -> SessionUsageResult:
    service = await bridge.service()
    numbers = usage_numbers(service, params.session_id)
    view = bridge.view(params.session_id)
    if view is not None:
        bridge.projector.emit(view, SessionUsage, usage=numbers)
    return SessionUsageResult(usage=UsageNumbers.model_validate(numbers))


def register(table: MethodTable) -> None:
    table.add("session.create", create)
    table.add("session.resume", resume)
    table.add("session.list", list_sessions)
    table.add("session.most_recent", most_recent)
    table.add("session.close", close)
    table.add("session.history", history)
    table.add("session.title", title)
    table.add("session.undo", undo)
    table.add("session.compress", compress)
    table.add("session.branch", branch)
    table.add("session.usage", usage)
