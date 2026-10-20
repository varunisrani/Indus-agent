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

On the shared host (P17-22) step 1 reads the runtime ledger, where every started admission of the
previous owner is now ``unknown``. The marker's token is its admission's request id:

- a stored final written after the turn began is delivered once (as above) and the session's
  unknown admission is discarded, so the conversation goes on;
- an admission that never started (still queued) or that settled is not a crash-left turn: the
  marker is cleared and nothing is paused (a queued one runs now and its reply goes out);
- otherwise the route is paused as above.

Then every other unknown admission (a turn the host started on its own, a marker already cleared)
is settled the same way per chat route of its session: a final the lost turn stored is owed once
(through ``crash_reply_sink``, which skips it when the delivery ledger shows it went out) and the
admission is discarded; otherwise the routes are paused. ``blocked`` is the admission service's
notice that queued input waits behind an unknown turn (handed on only after this recovery ran):
the route is paused and the chat told once. ``resolve_unknown`` also discards the session's
unknown admission and closes the lost turn in its transcript with a short assistant note
(``admissions.close_lost_turn``, in the discard's transaction), so a later resume does not
start from a dangling question.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from indus.foundation.blocking import run_blocking
from indus.gateway.core.admission import (
    GatewayIdentity,
    bind_identity,
    reset_identity,
)
from indus.gateway.core.events import InboundEvent, Source
from indus.gateway.core.shared_host import UNKNOWN_PAUSE_REASON
from indus.gateway.daemon.lifecycle import INTERRUPTED_REASON
from indus.gateway.daemon.watchdogs import RestartLoopGuard
from indus.service.admissions import close_lost_turn
from indus.state import gateway_routes as routes
from indus.state import messages
from indus.state import runtime_ledger as ledger
from indus.state.gateway_routes import RouteEntry
from indus.state.gateway_runtime import boots_path
from indus.state.registry import StoreHandle
from indus.state.runtime_ledger import AdmissionRow, LedgerRefusal

if TYPE_CHECKING:
    from indus.gateway.core.shared_host import MessagingHost
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

UNKNOWN_REASON = UNKNOWN_PAUSE_REASON
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
    cleared: list[str] = field(default_factory=list[str])
    """Markers whose admission never started or settled (not crash-left turns)."""
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
    """Operator resolution of an unknown turn: only while the pause still has ``token``. The
    session's unknown admission in the runtime ledger (if any) is discarded with it, so the
    session's queue moves on at its next input."""
    entry = routes.get(store, session_key)
    if not _paused(entry) or entry is None or entry.active_turn_token != token:
        return False
    _discard_unknown_rows(store, entry.session_id, token)
    routes.end_turn(store, session_key, token)
    routes.clear_resume_pending(store, session_key)
    return True


def _discard_unknown_rows(store: StoreHandle, session_id: str, token: str) -> None:
    epoch = ledger.current_epoch(store)
    if epoch is None:
        return
    with store.read() as conn:
        found = conn.execute(
            "SELECT session_id FROM runtime_admissions WHERE request_id = ? "
            "ORDER BY seq DESC LIMIT 1",
            (token,),
        ).fetchone()
    key = str(found[0]) if found is not None else session_id
    close = close_lost_turn(now=lambda: store.scope.clock.now().timestamp())
    for row in ledger.pending(store, key):
        if row.state != "unknown" or row.generation is None:
            continue
        try:
            ledger.resolve_unknown(
                store,
                epoch=epoch,
                admission_id=row.admission_id,
                generation=row.generation,
                terminal_write=close,
            )
        except LedgerRefusal as refusal:
            log.warning("could not discard the lost turn %s: %s", row.admission_id, refusal)


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

    @property
    def shared(self) -> MessagingHost | None:
        pipeline = getattr(self.daemon, "pipeline", None)
        return pipeline.shared if pipeline is not None else None

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
        await self._pause_unknown(report)
        await self._resume(report)
        await self._notices(report)
        return report

    async def _crash_left(self, entry: RouteEntry, report: RecoveryReport) -> None:
        store = self.daemon.store
        key, token = entry.session_key, entry.active_turn_token or ""
        started = entry.active_turn_started_at or 0.0
        owner: AdmissionRow | None = None
        unknown: AdmissionRow | None = None
        shared = self.shared
        if shared is not None:
            try:
                owner = await shared.by_request(token)
                ledger_key = owner.session_id if owner is not None else entry.session_id
                unknown = await shared.unknown_of(ledger_key)
            except Exception:
                log.warning("could not read the runtime ledger for %s", key, exc_info=True)
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
            if shared is not None and unknown is not None:
                await shared.discard(unknown)  # its reply is owed and sent: the FIFO goes on
            return
        if owner is not None and unknown is None and owner.state != "unknown":
            # Never started (it runs now) or settled before the crash: not a crash-left turn.
            await run_blocking(routes.end_turn, store, key, token, label="active-turn marker")
            report.cleared.append(key)
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

    async def _pause_unknown(self, report: RecoveryReport) -> None:
        """Every unknown admission pauses the chat routes of its session (R06.19)."""
        shared = self.shared
        if shared is None:
            return
        try:
            rows = await shared.unknown_rows()
        except Exception:
            log.warning("could not read the unknown turns", exc_info=True)
            return
        store = self.daemon.store
        for row in rows:
            entries = [e for e in await self._routes_of(row.session_id) if not _paused(e)]
            if not entries:
                continue
            try:
                text = await run_blocking(
                    _final_after, store, row.session_id, row.created_at, label="transcript"
                )
            except Exception:
                log.warning("could not read the transcript of %s", row.session_id, exc_info=True)
                text = None
            if text is not None:
                # The lost turn stored its reply: owed once (the ledger knows if it went out).
                for entry in entries:
                    source = _source_of(entry)
                    if source is None:
                        continue
                    owed = replace(
                        entry,
                        active_turn_token=row.request_id,
                        active_turn_started_at=row.created_at,
                    )
                    try:
                        await self.crash_reply_sink(owed, source, text)
                    except Exception:
                        log.warning("could not deliver the owed reply", exc_info=True)
                    report.recovered_replies.append(entry.session_key)
                await shared.discard(row)
                continue
            for entry in entries:
                await run_blocking(
                    routes.mark_resume_pending,
                    self.daemon.store,
                    entry.session_key,
                    UNKNOWN_REASON,
                    label="resume flag",
                )
                report.paused.append(entry.session_key)
                log.warning(
                    "%s waits behind a turn whose outcome was lost in a restart; paused until "
                    "/reset",
                    entry.session_key,
                )

    async def _routes_of(self, session_id: str) -> list[RouteEntry]:
        try:
            return await run_blocking(
                routes.routes_for_session, self.daemon.store, session_id, label="gateway route"
            )
        except Exception:
            log.warning("could not read the routes of %s", session_id, exc_info=True)
            return []

    async def blocked(self, session_id: str, row: AdmissionRow) -> None:
        """Queued input waits behind the unknown admission ``row``: pause the session's chat
        routes and tell each chat once (later messages are dropped quietly)."""
        del row
        store = self.daemon.store
        for entry in await self._routes_of(session_id):
            key = entry.session_key
            if entry.resume_reason == _TOLD or key in self._told:
                continue
            source = _source_of(entry)
            self._told.add(key)
            await run_blocking(routes.mark_resume_pending, store, key, _TOLD, label="resume flag")
            if source is not None:
                await self.daemon.pipeline.reply(source, UNKNOWN_NOTICE, kind="notice")

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
