"""The delivery ledger's rules: recording finals, retrying them, recovering them at boot (P15-19).

The outbound sender (P15-17) records each chat turn's final reply here before it sends anything,
marks it ``attempting`` just before the platform call, and ``delivered`` or ``failed`` after.
Every call is wrapped: a ledger error is logged and the send goes on (a broken database must
never cost a reply). The table itself is ``indus.state.delivery_ledger``.

What happens to a row that did not reach ``delivered``:

- boot (``recover``, called by boot recovery before adapters take turns), for rows whose owner
  process is gone (pid plus start time): ``pending`` was never sent, so it goes out plainly;
  ``attempting`` may be on screen already, so it goes out with ``DUPLICATE_PREFIX``; ``failed``
  goes out again (labelled by why it failed). Rows for a platform that is not connected are left
  alone so no attempt is spent; rows older than ``STALE_AFTER`` or out of attempts are abandoned;
- a failed send while running: a rate-limit refusal waits the platform's stated time (default
  60 s, at most 15 min, plus 2 s slack) and goes out with ``FLOOD_PREFIX``; a boot inside that
  wait adopts the row without spending an attempt. A send that failed because the adapter was not
  connected waits for ``platform_reconnected`` and goes out with ``RECONNECTED_PREFIX``. Any other
  failure is retried after 30 s, then 120 s; the last allowed attempt is kept for the next boot.
  A failure proving the whole chat gone is never retried (the row is abandoned at once);
- at most ``MAX_ATTEMPTS`` redeliveries per row.

A row that failed part way keeps the pieces that did not go out; its redelivery sends only those
(after the label), never the whole reply again (R06.4).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal

from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
from indus.foundation.scope import Scope
from indus.gateway.core.send import ErrorKind, SendResult
from indus.state import delivery_ledger as table
from indus.state.delivery_ledger import LedgerRow, Owner
from indus.state.gateway_runtime import alive, process_start_time
from indus.state.registry import StoreHandle

if TYPE_CHECKING:
    from indus.gateway.outbound.sender import OutboundSender

__all__ = [
    "BACKOFF_SECONDS",
    "DUPLICATE_PREFIX",
    "FLOOD_PREFIX",
    "MAX_ATTEMPTS",
    "RECONNECTED_PREFIX",
    "STALE_AFTER",
    "DeliveryLedger",
    "FailureKind",
    "RecoveryReport",
    "current_owner",
    "owner_alive",
]

log = logging.getLogger(__name__)

MAX_ATTEMPTS: Final = 3
STALE_AFTER: Final = 86400.0
BACKOFF_SECONDS: Final = (30.0, 120.0)
FLOOD_DEFAULT_WAIT: Final = 60.0
FLOOD_MAX_WAIT: Final = 15 * 60.0
FLOOD_SLACK: Final = 2.0

DUPLICATE_PREFIX: Final = (
    "(Resent after an interrupted delivery: you may already have this message.)"
)
RECONNECTED_PREFIX: Final = (
    "(Resent now that {platform} is reachable again: you may already have this message.)"
)
FLOOD_PREFIX: Final = (
    "(Resent after {platform} asked us to slow down: the start of it may already be above.)"
)

type FailureKind = Literal["flood", "reconnect", "uncertain", "refused"]

FAULT_HOOK: Callable[[str], None] | None = None
"""Test seam: called with ``before_pending``, ``pending``, ``attempting`` or ``delivered`` at that
point of a send, so a test process can be killed exactly there."""


def _fault(point: str) -> None:
    hook = FAULT_HOOK
    if hook is not None:
        hook(point)


def current_owner() -> Owner:
    pid = os.getpid()
    return (pid, process_start_time(pid))


def owner_alive(pid: int | None, start: int | None) -> bool:
    """True when the process that owns a row still runs (a reused pid is not it)."""
    return pid is not None and alive(pid, start)


def _label(row: LedgerRow) -> str:
    """The prefix a redelivery of ``row`` (as it was before the claim) carries."""
    platform = row.platform.capitalize()
    if row.state == "attempting":
        return DUPLICATE_PREFIX
    if row.state == "failed":
        match row.error_kind:
            case "flood":
                return FLOOD_PREFIX.format(platform=platform)
            case "reconnect":
                return RECONNECTED_PREFIX.format(platform=platform)
            case "uncertain":
                return DUPLICATE_PREFIX
            case _:
                return ""
    return "" if row.attempts == 0 else DUPLICATE_PREFIX


def failure_kind(result: SendResult, *, reconnect_only: bool) -> FailureKind:
    if reconnect_only:
        return "reconnect"
    if result.kind is ErrorKind.RATE_LIMITED or result.retry_after is not None:
        return "flood"
    if result.maybe_delivered or result.partial or result.kind in (None, ErrorKind.UNKNOWN):
        return "uncertain"
    return "refused"


@dataclass
class RecoveryReport:
    """What a sweep did: rows sent again (by label), adopted, abandoned, left for later."""

    plain: int = 0
    labelled: int = 0
    adopted: int = 0
    abandoned: int = 0
    skipped: int = 0
    delivered: int = 0
    session_keys: set[str] = field(default_factory=set[str])

    @property
    def redelivered(self) -> int:
        return self.plain + self.labelled


class DeliveryLedger:
    """The ledger for one profile's store (see the module notes)."""

    def __init__(
        self,
        store: StoreHandle,
        *,
        clock: Clock | None = None,
        owner: Owner | None = None,
        is_alive: Callable[[int | None, int | None], bool] = owner_alive,
    ) -> None:
        self.store = store
        self.clock: Clock = clock or store.scope.clock
        self.owner: Owner = owner if owner is not None else current_owner()
        self.is_alive = is_alive
        self._lookup: Callable[[str], OutboundSender | None] = lambda _platform: None
        self._timer: asyncio.Task[None] | None = None
        self._timer_at: float | None = None
        self._closed = False

    @classmethod
    def for_scope(cls, scope: Scope, store: StoreHandle) -> DeliveryLedger | None:
        """The profile's ledger, or None when ``gateway.delivery_ledger`` is false."""
        from indus.gateway.config import gateway_section

        if not gateway_section(scope).delivery_ledger:
            return None
        return cls(store, clock=scope.clock)

    def bind(self, lookup: Callable[[str], OutboundSender | None]) -> None:
        """Where redeliveries go: the outbound sender of a platform (None: not connected)."""
        self._lookup = lookup

    def _now(self) -> float:
        return self.clock.now().timestamp()

    async def _call[T](self, fn: Callable[[], T], what: str) -> T | None:
        try:
            return await run_blocking(fn, label="delivery ledger")
        except Exception as exc:
            log.warning("delivery ledger: %s failed (%s); delivery goes on", what, exc)
            return None

    # -- the sender's checkpoints ----------------------------------------------------------------

    async def open(
        self,
        *,
        session_key: str,
        message_ref: str,
        platform: str,
        chat_id: str,
        content: str,
        thread_id: str | None = None,
    ) -> str | None:
        """Record a final as owed (``pending``); its row id, or None when the ledger failed."""
        _fault("before_pending")
        row_id = table.obligation_id(session_key, message_ref, content)
        recorded = await self._call(
            functools.partial(
                table.record_pending,
                self.store,
                row_id=row_id,
                session_key=session_key,
                platform=platform,
                chat_id=chat_id,
                thread_id=thread_id,
                content=content,
                owner=self.owner,
            ),
            "recording a reply",
        )
        if recorded is None:
            return None
        _fault("pending")
        return row_id

    async def attempting(self, row_id: str) -> None:
        await self._call(
            functools.partial(table.mark_attempting, self.store, row_id), "marking an attempt"
        )
        _fault("attempting")

    async def delivered(self, row_id: str) -> None:
        await self._call(
            functools.partial(table.mark_delivered, self.store, row_id), "marking a delivery"
        )
        _fault("delivered")

    async def failed(
        self,
        row_id: str,
        result: SendResult,
        *,
        pieces: Sequence[str] = (),
        reconnect_only: bool = False,
    ) -> None:
        """Record a failed send and arm the retry its kind calls for."""
        error = result.error or "send failed"
        if result.chat_unreachable:
            await self._call(
                functools.partial(table.abandon, self.store, row_id, error), "abandoning a reply"
            )
            return
        row = await self._call(functools.partial(table.get, self.store, row_id), "reading a row")
        attempts = row.attempts if row is not None else 0
        kind = failure_kind(result, reconnect_only=reconnect_only)
        now = self._now()
        retry_at: float | None = None
        if kind == "flood":
            wait = result.retry_after if result.retry_after and result.retry_after > 0 else None
            retry_at = now + min(wait or FLOOD_DEFAULT_WAIT, FLOOD_MAX_WAIT) + FLOOD_SLACK
        elif kind != "reconnect" and attempts < MAX_ATTEMPTS - 1:
            retry_at = now + BACKOFF_SECONDS[min(attempts, len(BACKOFF_SECONDS) - 1)]
        await self._call(
            functools.partial(
                table.mark_failed,
                self.store,
                row_id,
                error=error,
                kind=kind,
                retry_at=retry_at,
                pieces=tuple(pieces),
            ),
            "marking a failure",
        )
        if retry_at is not None:
            self._arm(retry_at)

    # -- redelivery ------------------------------------------------------------------------------

    async def _redeliver(self, before: LedgerRow, prefix: str) -> bool:
        """Send a claimed row again through its platform's sender; True when it was delivered."""
        from indus.gateway.outbound.sender import FinalReply

        sender = self._lookup(before.platform)
        if sender is None:  # the platform went away between the claim and the send
            await self._call(
                functools.partial(
                    table.mark_failed,
                    self.store,
                    before.id,
                    error="the platform is not connected",
                    kind="reconnect",
                ),
                "releasing a claim",
            )
            return False
        if before.pieces:
            text = prefix
        else:
            text = f"{prefix}\n\n{before.content}" if prefix else before.content
        reply = FinalReply(
            text,
            thread_id=before.thread_id,
            allow_media=False,
            ledger_row=before.id,
            resume_pieces=before.pieces,
        )
        try:
            report = await sender.deliver(before.chat_id, reply)
        except Exception:
            log.warning("delivery ledger: redelivering %s failed", before.id, exc_info=True)
            return False
        return report.outcome == "sent"

    async def recover(self) -> RecoveryReport:
        """The boot sweep (see the module notes)."""
        report = RecoveryReport()
        now = self._now()
        abandoned = await self._call(
            functools.partial(table.abandon_older_than, self.store, now - STALE_AFTER),
            "abandoning stale rows",
        )
        report.abandoned += abandoned or 0
        rows = await self._call(functools.partial(table.list_open, self.store), "listing rows")
        for row in rows or []:
            if row.owner == self.owner or self.is_alive(row.owner_pid, row.owner_start):
                report.skipped += 1
                continue
            if row.attempts >= MAX_ATTEMPTS:
                await self._call(
                    functools.partial(table.abandon, self.store, row.id), "abandoning a row"
                )
                report.abandoned += 1
                continue
            if self._lookup(row.platform) is None:
                report.skipped += 1  # no adapter this boot: spending an attempt would be a no-op
                continue
            report.session_keys.add(row.session_key)
            waiting = row.retry_at is not None and now < row.retry_at
            if row.state == "failed" and row.error_kind == "flood" and waiting:
                adopted = await self._call(
                    functools.partial(
                        table.adopt, self.store, row.id, expect_owner=row.owner, owner=self.owner
                    ),
                    "adopting a row",
                )
                if adopted:
                    report.adopted += 1
                    assert row.retry_at is not None
                    self._arm(row.retry_at)
                continue
            before = await self._call(
                functools.partial(
                    table.claim, self.store, row.id, expect_owner=row.owner, owner=self.owner
                ),
                "claiming a row",
            )
            if before is None:
                continue
            prefix = _label(before)
            if prefix:
                report.labelled += 1
            else:
                report.plain += 1
            if await self._redeliver(before, prefix):
                report.delivered += 1
        return report

    async def _claim_own_failed(
        self, select: Callable[[LedgerRow], bool]
    ) -> list[tuple[LedgerRow, str]]:
        rows = await self._call(functools.partial(table.list_open, self.store), "listing rows")
        claimed: list[tuple[LedgerRow, str]] = []
        now = self._now()
        for row in rows or []:
            if row.state != "failed" or row.owner != self.owner or not select(row):
                continue
            if row.attempts >= MAX_ATTEMPTS or now - row.created_at > STALE_AFTER:
                await self._call(
                    functools.partial(table.abandon, self.store, row.id), "abandoning a row"
                )
                continue
            before = await self._call(
                functools.partial(
                    table.claim,
                    self.store,
                    row.id,
                    expect_owner=self.owner,
                    owner=self.owner,
                    states=("failed",),
                ),
                "claiming a row",
            )
            if before is not None:
                claimed.append((before, _label(before)))
        return claimed

    async def platform_reconnected(self, platform: str) -> int:
        """Send this process's reconnect-only rows for ``platform`` again; how many went out."""
        name = platform.strip().lower()
        claimed = await self._claim_own_failed(
            lambda row: row.platform == name and row.error_kind == "reconnect"
        )
        sent = 0
        for before, prefix in claimed:
            if await self._redeliver(before, prefix):
                sent += 1
        return sent

    async def retry_due(self) -> int:
        """Send this process's failed rows whose retry time has come; how many went out."""
        now = self._now()
        claimed = await self._claim_own_failed(
            lambda row: row.retry_at is not None and row.retry_at <= now
        )
        sent = 0
        for before, prefix in claimed:
            if await self._redeliver(before, prefix):
                sent += 1
        await self._rearm()
        return sent

    # -- timers ----------------------------------------------------------------------------------

    def _arm(self, at: float) -> None:
        if self._closed:
            return
        running = self._timer is not None and not self._timer.done()
        if running and self._timer_at is not None and self._timer_at <= at:
            return
        if self._timer is not None and running:
            self._timer.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._timer_at = at
        self._timer = loop.create_task(self._wake(at), name="delivery ledger retry")

    async def _wake(self, at: float) -> None:
        await self.clock.sleep(max(0.0, at - self._now()))
        if self._timer is asyncio.current_task():
            self._timer = None
            self._timer_at = None
        try:
            await self.retry_due()
        except Exception:
            log.warning("delivery ledger: a retry pass failed", exc_info=True)

    async def _rearm(self) -> None:
        rows = await self._call(functools.partial(table.list_open, self.store), "listing rows")
        due = [
            row.retry_at
            for row in rows or []
            if row.state == "failed" and row.owner == self.owner and row.retry_at is not None
        ]
        if due:
            self._arm(min(due))

    @property
    def next_retry_at(self) -> float | None:
        """When the armed retry timer fires (None: nothing armed)."""
        if self._timer is None or self._timer.done():
            return None
        return self._timer_at

    async def aclose(self) -> None:
        """Stop the retry timer (rows stay for the next boot)."""
        self._closed = True
        timer, self._timer = self._timer, None
        if timer is not None and not timer.done():
            timer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await timer
