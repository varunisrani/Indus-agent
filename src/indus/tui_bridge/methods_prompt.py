"""Prompt methods of the bridge (P12-29): submit, background, interrupt, steer, redirect.

``prompt.submit`` starts a turn (``streaming``). While a turn runs, ``when_busy`` decides:
``reject`` (the service's ``SESSION_BUSY``), ``queue`` (kept here and sent when the turn ends; the
service's input probe sees it, so automatic turns never jump ahead of it), ``steer`` (guidance for
the running turn; queued instead while the session compresses) or ``redirect``. Images staged with
``image.attach`` ride along as one line per file naming its path.

``session.interrupt`` answers at once — it never waits for an agent still being built; a hard
interrupt also drops the queued prompts. ``session.steer`` on an idle session is not queued (the
client sends it as a prompt).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from indus.protocol.bridge import PromptBackgroundParams, PromptBackgroundResult
from indus.protocol.methods import (
    BusyAction,
    PromptSubmitParams,
    PromptSubmitResult,
    RedirectOutcome,
    SessionCreateParams,
    SessionInterruptParams,
    SessionInterruptResult,
    SessionRedirectParams,
    SessionRedirectResult,
    SessionSteerParams,
    SessionSteerResult,
    SubmitStatus,
)
from indus.service.session_service import SessionService
from indus.tui_bridge.bridge import SessionView

if TYPE_CHECKING:
    from indus.core.agent import RunningTurn
    from indus.tui_bridge.bridge import Bridge
    from indus.tui_bridge.dispatch import MethodTable

__all__ = ["register", "with_images"]

log = logging.getLogger(__name__)


def with_images(text: str, images: list[Path]) -> str:
    """``text`` followed by one line per staged image (the model reads them with its tools)."""
    if not images:
        return text
    lines = [f"[Attached image: {path}]" for path in images]
    return "\n".join([text, "", *lines])


def _running(service: SessionService, session_id: str) -> RunningTurn | None:
    try:
        return service.agent(session_id).current
    except Exception:
        return None


def _ticket(running: RunningTurn, status: SubmitStatus) -> PromptSubmitResult:
    return PromptSubmitResult(turn_id=running.turn_id, generation=running.generation, status=status)


async def _view(bridge: Bridge, service: SessionService, session_id: str) -> SessionView:
    view = bridge.view(session_id)
    if view is not None:
        return view
    if service.current_id(session_id) not in service.attached():
        await service.resume(session_id)
    view = bridge.watch(session_id)
    bridge.projector.info(view)
    return view


async def submit(bridge: Bridge, params: PromptSubmitParams) -> PromptSubmitResult:
    service = await bridge.service()
    view = await _view(bridge, service, params.session_id)
    sid = view.session_id
    running = _running(service, sid)
    if running is not None and params.when_busy is not BusyAction.REJECT:
        text = with_images(params.text, view.images)
        view.images = []
        action = params.when_busy
        if action is BusyAction.QUEUE or (action is BusyAction.STEER and view.compressing):
            view.queue.append(text)
            return _ticket(running, SubmitStatus.QUEUED)
        if action is BusyAction.STEER:
            if service.steer(sid, text).queued:
                return _ticket(running, SubmitStatus.STEERED)
        elif action is BusyAction.REDIRECT:
            outcome = service.redirect(sid, text).outcome
            if outcome is RedirectOutcome.REDIRECTED:
                return _ticket(running, SubmitStatus.REDIRECTED)
            if outcome is RedirectOutcome.STEERED:
                return _ticket(running, SubmitStatus.STEERED)
        started = await service.submit(sid, text)  # the turn ended meanwhile
        return started.model_copy(update={"status": SubmitStatus.STREAMING})
    text = with_images(params.text, view.images)
    started = await service.submit(sid, text)
    view.images = []
    return started


async def background(bridge: Bridge, params: PromptBackgroundParams) -> PromptBackgroundResult:
    service = await bridge.service()
    parent = service.current_id(params.session_id)
    if parent not in service.attached():
        await service.resume(parent)
    cwd = service.agent(parent).options.cwd
    side = await service.create(SessionCreateParams(cwd=str(cwd) if cwd is not None else None))
    bridge.background[side.session_id] = parent
    try:
        await service.submit(side.session_id, params.text)
    except BaseException:
        bridge.background.pop(side.session_id, None)
        await service.close(side.session_id)
        raise
    return PromptBackgroundResult(task_id=side.session_id)


async def interrupt(bridge: Bridge, params: SessionInterruptParams) -> SessionInterruptResult:
    service = bridge.built
    if service is None:
        return SessionInterruptResult(interrupted=False)
    view = bridge.view(params.session_id)
    if params.hard and view is not None:
        view.queue.clear()
    return service.interrupt(params.session_id, params.generation, hard=params.hard)


async def steer(bridge: Bridge, params: SessionSteerParams) -> SessionSteerResult:
    service = await bridge.service()
    view = bridge.view(params.session_id)
    if view is not None and view.compressing and _running(service, view.session_id) is not None:
        view.queue.append(params.text)
        return SessionSteerResult(queued=True)
    return service.steer(params.session_id, params.text)


async def redirect(bridge: Bridge, params: SessionRedirectParams) -> SessionRedirectResult:
    service = await bridge.service()
    return service.redirect(params.session_id, params.text)


def register(table: MethodTable) -> None:
    table.add("prompt.submit", submit)
    table.add("prompt.background", background)
    table.add("session.interrupt", interrupt)
    table.add("session.steer", steer)
    table.add("session.redirect", redirect)
