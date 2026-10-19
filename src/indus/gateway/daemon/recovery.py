"""Boot recovery: settle what the previous process left before any new turn runs (P15-26).

Runs inside the startup-restore window (the intake holds inbound messages meanwhile and replays
them afterwards, in order), after the RECOVERY parts (the delivery-ledger sweep):

1. Crash-left turns. A route still carrying the durable active-turn marker means the previous
   process died mid-turn: a graceful stop always clears it.
   - The transcript already ends with a final assistant reply written after the turn started:
     that reply is owed, never regenerated. The marker is cleared first, then the reply goes out
     once with ``RECOVERED_LABEL`` through ``crash_reply_sink`` (the ledger can take this over).
   - Otherwise the turn's side effects are unknown (R06.19, R03.21): nothing is replayed. The
     route is paused (``resume_pending`` with reason ``unknown_turn``; the marker's token stays as
     the pause's generation). The chat's first message gets ``UNKNOWN_NOTICE`` and is not run;
     later ones are dropped quietly; commands still work. ``/reset`` (``/new``) rotates the route,
     which ends the pause; an operator can ``resolve_unknown`` with the token (generation check).
2. Restart-interrupted routes (flagged by a stop or restart, ``lifecycle.INTERRUPTED_REASON``):
   each is resumed at most once. The flag is cleared before the resume starts, so a crash during
   the resume is an unknown turn on the next boot, never a second resume. Skipped (flag kept, the
   person's next message runs normally) when the flag is older than ``RESUME_FRESH``, the owner is
   no longer authorized, the platform is not connected, or the restart-loop guard tripped.
   The chat gets ``HEADS_UP``; the session gets an internal turn (``RESUME_NOTE``).
3. Restart notices: when this boot follows an earlier run, one ``BACK_ONLINE`` line per connected
   platform to its home channel, unless the platform's ``restart_notice`` is off.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from indus.foundation.blocking import run_blocking
from indus.gateway.core.admission import (
    GatewayIdentity,
    bind_identity,
    reset_identity,
)
from indus.gateway.core.events import InboundEvent, Source
from indus.gateway.daemon.lifecycle import INTERRUPTED_REASON
from indus.gateway.daemon.watchdogs import RestartLoopGuard
from indus.state import gateway_routes as routes
from indus.state import messages
from indus.state.gateway_routes import RouteEntry
from indus.state.gateway_runtime import boots_path
from indus.state.registry import StoreHandle

if TYPE_CHECKING:
    from indus.gateway.daemon.process import GatewayDaemon

__all__ = [
    "BACK_ONLINE",
    "HEADS_UP",
    "RECOVERED_LABEL",
    "RESUME_FRESH",
    "RESUME_NOTE",
    "UNKNOWN_NOTICE",
    "UNKNOWN_REASON",
    "BootRecovery",
    "RecoveryReport",
    "resolve_unknown",
]

log = logging.getLogger(__name__)

UNKNOWN_REASON = "unknown_turn"
_TOLD = UNKNOWN_REASON + ":told"
RESUME_FRESH = 3600.0

HEADS_UP = "I was restarted while working on your last message. Picking it up again now."
RESUME_NOTE = (
    "[The gateway restarted while you were working on the previous request. Continue from where "
    "you stopped. Check the current state of anything you were changing before repeating a step.]"
)
UNKNOWN_NOTICE = (
    "The gateway stopped unexpectedly while I was working on your last message, so I can't tell "
    "how far it got. To be safe this conversation is paused. Send /reset to start a fresh one."
)
RECOVERED_LABEL = "(Recovered after a restart; this reply was written before the gateway stopped.)"
BACK_ONLINE = "Indus Agent is back online."

type CrashReplySink = Callable[[RouteEntry, Source, str], Awaitable[None]]


@dataclass
class RecoveryReport:
    recovered_replies: list[str] = field(default_factory=list[str])
    paused: list[str] = field(default_factory=list[str])
    resumed: list[str] = field(default_factory=list[str])
    skipped: dict[str, str] = field(default_factory=dict[str, str])
    notices: list[str] = field(default_factory=list[str])
    guard_tripped: bool = False


def _paused(entry: RouteEntry | None) -> bool:
    return bool(
        entry is not None
        and entry.resume_pending
        and (entry.resume_reason or "").startswith(UNKNOWN_REASON)
    )


def resolve_unknown(store: StoreHandle, session_key: str, token: str) -> bool:
    """Operator resolution of an unknown turn: only while the pause still has ``token``."""
    entry = routes.get(store, session_key)
    if not _paused(entry) or entry is None or entry.active_turn_token != token:
        return False
    routes.end_turn(store, session_key, token)
    routes.clear_resume_pending(store, session_key)
    return True


def _source_of(entry: RouteEntry) -> Source | None:
    try:
        return Source.model_validate(entry.origin)
    except Exception:
        return None


def _final_after(store: StoreHandle, session_id: str, started: float) -> str | None:
    """The session's last message when it is a final assistant reply written after ``started``."""
    with store.read() as conn:
        row = conn.execute(
            "SELECT role, content, tool_calls, timestamp FROM messages "
            "WHERE session_id = ? AND active = 1 ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    if row is None:
        return None
    role, content, tool_calls, stamp = row[0], row[1], row[2], row[3]
    if role != "assistant" or tool_calls or float(stamp or 0.0) < started:
        return None
    text = messages.decode_content(content)
    return text.strip() if isinstance(text, str) and text.strip() else None


class BootRecovery:
    """One boot's recovery for ``daemon`` (see the module notes)."""

    def __init__(self, daemon: GatewayDaemon) -> None:
        self.daemon = daemon
        self.crash_reply_sink: CrashReplySink = self._send_recovered
        self._told: set[str] = set()

    # -- the pause gate (an intake interceptor) ----------------------------------------------------

    async def intercept(self, event: InboundEvent, key: str) -> str | None:
        """Hold back messages for a paused conversation; commands pass (``/reset`` ends it)."""
        if event.is_command:
            return None
        try:
            entry = await run_blocking(routes.get, self.daemon.store, key, label="gateway route")
        except Exception:
            return None
        if not _paused(entry):
            return None
        assert entry is not None
        if entry.resume_reason == _TOLD or key in self._told:
            return ""  # told once already: drop quietly
        self._told.add(key)
        try:
            await run_blocking(
                routes.mark_resume_pending, self.daemon.store, key, _TOLD, label="resume flag"
            )
        except Exception:
            log.warning("could not record the pause notice for %s", key, exc_info=True)
        return UNKNOWN_NOTICE

    # -- boot --------------------------------------------------------------------------------------

    async def run(self) -> RecoveryReport:
        report = RecoveryReport()
        try:
            entries = await run_blocking(routes.list_routes, self.daemon.store, label="routes")
        except Exception:
            log.exception("boot recovery could not read the routes")
            return report
        for entry in entries:
            if entry.active_turn_token:
                await self._crash_left(entry, report)
        await self._resume(report)
        await self._notices(report)
        return report

    async def _crash_left(self, entry: RouteEntry, report: RecoveryReport) -> None:
        store = self.daemon.store
        key, token = entry.session_key, entry.active_turn_token or ""
        started = entry.active_turn_started_at or 0.0
        try:
            text = await run_blocking(
                _final_after, store, entry.session_id, started, label="transcript"
            )
        except Exception:
            log.warning("could not read the transcript of %s", entry.session_id, exc_info=True)
            text = None
        source = _source_of(entry)
        if text is not None and source is not None:
            # At most once: the marker goes before the send.
            await run_blocking(routes.end_turn, store, key, token, label="active-turn marker")
            if entry.resume_reason == INTERRUPTED_REASON:
                await run_blocking(routes.clear_resume_pending, store, key, label="resume flag")
            try:
                await self.crash_reply_sink(entry, source, text)
            except Exception:
                log.warning("could not deliver the recovered reply for %s", key, exc_info=True)
            report.recovered_replies.append(key)
            return
        await run_blocking(
            routes.mark_resume_pending, store, key, UNKNOWN_REASON, label="resume flag"
        )
        report.paused.append(key)
        log.warning(
            "the previous gateway died during a turn for %s; that conversation is paused until "
            "/reset (or resolve_unknown with token %s)",
            key,
            token,
        )

    async def _send_recovered(self, entry: RouteEntry, source: Source, text: str) -> None:
        await self.daemon.pipeline.reply(source, f"{RECOVERED_LABEL}\n\n{text}", kind="final")

    async def _resume(self, report: RecoveryReport) -> None:
        daemon = self.daemon
        try:
            entries = await run_blocking(routes.list_routes, daemon.store, label="routes")
        except Exception:
            return
        candidates = [
            e
            for e in entries
            if e.resume_pending
            and e.resume_reason == INTERRUPTED_REASON
            and not e.active_turn_token
        ]
        if not candidates:
            return
        guard = RestartLoopGuard(boots_path(daemon.scope.home))
        if guard.check_and_record(daemon.clock.now().timestamp()):
            report.guard_tripped = True
            for entry in candidates:
                report.skipped[entry.session_key] = "restart_loop"
            return
        now = daemon.clock.now().timestamp()
        for entry in candidates:
            why = await self._resume_one(entry, now)
            if why is None:
                report.resumed.append(entry.session_key)
            else:
                report.skipped[entry.session_key] = why

    async def _resume_one(self, entry: RouteEntry, now: float) -> str | None:
        daemon = self.daemon
        key = entry.session_key
        if now - entry.updated_at > daemon.options.resume_fresh:
            return "stale"
        source = _source_of(entry)
        if source is None:
            return "no_origin"
        platform = str(source.platform).lower()
        if platform not in daemon.adapters:
            return "platform_offline"
        try:
            if not daemon.authorizer.check(source).granted:
                return "not_authorized"
        except Exception:
            return "not_authorized"
        # Cleared before the resume starts: a crash during it never resumes it again.
        await run_blocking(routes.clear_resume_pending, daemon.store, key, label="resume flag")
        await daemon.pipeline.reply(source, HEADS_UP, kind="notice")
        event = InboundEvent(source=source, text=RESUME_NOTE, internal=True, allow_control=False)
        reset_identity()
        bind_identity(
            GatewayIdentity(
                platform=platform,
                chat_id=str(source.chat_id),
                user_id=str(source.user_id or ""),
                thread_id=str(source.thread_id or ""),
                session_key=key,
            )
        )
        try:
            await daemon.pipeline.run_admitted(event, key, background=True)
        finally:
            reset_identity()
        log.info("resumed the restart-interrupted conversation %s", key)
        return None

    async def _notices(self, report: RecoveryReport) -> None:
        daemon = self.daemon
        if not daemon.boot.previous_run:
            return
        for name in sorted(daemon.adapters):
            settings = daemon.settings.get(name)
            if settings is None or not settings.restart_notice or settings.home_channel is None:
                continue
            home = settings.home_channel
            source = Source(
                platform=name,
                chat_id=home.chat_id,
                chat_type="dm",
                thread_id=home.thread_id,
            )
            await daemon.pipeline.reply(source, BACK_ONLINE, kind="notice")
            report.notices.append(name)
