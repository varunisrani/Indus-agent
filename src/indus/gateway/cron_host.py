"""The cron scheduler hosted by the gateway daemon (P15-21).

``GatewayCronHost`` runs P14's ticker on the daemon's event loop: one pass at start, then one
every ``interval`` (60 s) — claims, the attempt ledger, the recursion guard, delivery and
bookkeeping are P14's (``service.cron_host.CronHost``) and unchanged. While it runs it holds the
profile's ticker lease (``cron.ticker.claim_ticker``, refreshed before every pass), so
``indus cron tick`` and ``indus cron daemon`` in another process stand down instead of racing it.
``stop`` stops starting passes, gives runs in flight ``drain_s`` (30 s) to finish, cancels the
rest and drops the lease.

Delivery: P14's ``cron.delivery`` hands platform targets to the deliverers the gateway registers
(P15-20); its status split (``delivery_failed`` with a reason, not counted in the failure streak;
R07.17) and its secret masking (always on, whatever the log-redaction setting) apply as they are.
One filter is added on the way: an output that only narrates silence ("(silent)", "*staying
quiet*", 🔇 …, at most ``NARRATION_MAX_CHARS`` characters) is settled as a silent run instead of
being delivered. Continuable deliveries are not part of P15.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import re
from collections.abc import Generator
from typing import Any

from indus.cron.classify import RunOutcome
from indus.cron.context import CronContext
from indus.cron.ports import CronPorts, JobSpec
from indus.cron.runner import RunEnv
from indus.cron.ticker import (
    DRAIN_S,
    TICK_INTERVAL_S,
    Ticker,
    TickerOwner,
    TickReport,
    claim_ticker,
    release_ticker,
)
from indus.foundation.clock import Clock
from indus.foundation.scope import Scope, use_scope
from indus.service.cron_host import CronHost

__all__ = [
    "NARRATION_MAX_CHARS",
    "GatewayCronHost",
    "narrates_silence",
    "screen_outcome",
]

log = logging.getLogger(__name__)

NARRATION_MAX_CHARS = 64

# Wrappers a model puts around a narration: emphasis, quotes, brackets, a muted-speaker emoji.
_WRAPPERS = re.compile(r"[\s*_~`\"'“”‘’()\[\]{}<>|🔇🤫🤐]+")
_TRAILING = re.compile(r"[.!…:;,-]+$")
_NARRATION = re.compile(
    r"^(?:i\s*(?:am|m|ll|will)\s+)?"
    r"(?:(?:stay|staying|remain|remaining|keep|keeping|stays)\s+)?"
    r"(?:silent|silence|quiet|no\s*reply|no\s*response|no\s*output|no\s*update[s]?|"
    r"nothing\s+(?:new|to\s+(?:say|report|add|share))|"
    r"(?:no|nothing)\s+(?:new\s+)?(?:news|changes?))"
    r"(?:\s+(?:here|for\s+now|this\s+time|today))?$"
)


def narrates_silence(text: str | None) -> bool:
    """True when ``text`` is only a short narration of staying silent (not a report)."""
    stripped = (text or "").strip()
    if not stripped or len(stripped) > NARRATION_MAX_CHARS:
        return False
    plain = _WRAPPERS.sub(" ", stripped).strip().lower()
    plain = _TRAILING.sub("", plain).strip()
    plain = " ".join(plain.split())
    if not plain:
        return True  # only wrappers: "...", "🔇", "*(…)*"
    return _NARRATION.match(plain) is not None


def screen_outcome(outcome: RunOutcome) -> RunOutcome:
    """A successful, delivering outcome that only narrates silence becomes a silent one."""
    if outcome.status != "ok" or not outcome.deliver or not narrates_silence(outcome.message):
        return outcome
    log.info("a cron output only narrated silence; not delivered")
    return dataclasses.replace(outcome, status="silent", deliver=False)


class GatewayCronHost:
    """The profile's ticker on the daemon loop (see the module notes). One per daemon."""

    def __init__(
        self,
        scope: Scope,
        *,
        backend: CronHost | None = None,
        clock: Clock | None = None,
        interval: float = TICK_INTERVAL_S,
        drain_s: float = DRAIN_S,
    ) -> None:
        self.scope = scope
        self.backend = backend if backend is not None else CronHost(scope)
        self.clock: Clock = clock or scope.clock
        self.interval = interval
        self.drain_s = drain_s
        self.owner: TickerOwner | None = None
        self.last_report: TickReport | None = None
        self._ctx: contextlib.ExitStack | None = None
        self._ticker: Ticker | None = None
        self._stop = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None

    # -- wiring -----------------------------------------------------------------------------------

    def env(self, ctx: CronContext) -> RunEnv:
        """P14's run environment with the narration filter in front of its finish step."""
        env = self.backend.env(ctx)
        ports: CronPorts = env.ports
        finish = ports.finish
        if finish is None:
            return env

        async def screened(job: JobSpec, outcome: RunOutcome) -> str | None:
            return await finish(job, screen_outcome(outcome))

        return dataclasses.replace(env, ports=dataclasses.replace(ports, finish=screened))

    @property
    def running(self) -> bool:
        return self._loop_task is not None and not self._loop_task.done()

    # -- lifecycle --------------------------------------------------------------------------------

    async def start(self) -> None:
        """Take the lease, open the cron store and start the pass loop (call on the daemon loop)."""
        if self._loop_task is not None:
            return
        self._stop = asyncio.Event()
        self._claim()
        stack = contextlib.ExitStack()
        ctx = stack.enter_context(self.backend.opened())
        self._ctx = stack
        self._ticker = Ticker(self.env(ctx), clock=self.clock, hosted=True)
        self._loop_task = asyncio.ensure_future(self._loop())
        self._loop_task.set_name("cron ticker")

    async def stop(self) -> None:
        """No new passes; drain the runs in flight (``drain_s``); drop the lease."""
        self._stop.set()
        task, self._loop_task = self._loop_task, None
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        ticker, self._ticker = self._ticker, None
        try:
            if ticker is not None:
                with self._scoped():
                    await ticker.drain(self.drain_s)
        finally:
            stack, self._ctx = self._ctx, None
            if stack is not None:
                with contextlib.suppress(Exception):
                    stack.close()
            with contextlib.suppress(OSError):
                release_ticker(self.scope.home)
            self.owner = None

    async def tick_now(self) -> TickReport:
        """One pass now (the loop keeps its own schedule); refreshes the lease."""
        ticker = self._ticker
        if ticker is None:
            raise RuntimeError("the cron host is not started")
        self._claim()
        with self._scoped():
            report = await ticker.tick(wait=False)
        self.last_report = report
        return report

    # -- the loop ---------------------------------------------------------------------------------

    @contextlib.contextmanager
    def _scoped(self) -> Generator[Any]:
        with use_scope(self.scope) as scope:
            yield scope

    def _claim(self) -> None:
        try:
            self.owner = claim_ticker(self.scope.home, self.clock.now())
        except OSError:
            log.warning("could not write the cron ticker lease", exc_info=True)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick_now()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("cron pass failed")
            await self._sleep_or_stop(self.interval)

    async def _sleep_or_stop(self, seconds: float) -> None:
        sleeper = asyncio.ensure_future(self.clock.sleep(seconds))
        stopper = asyncio.ensure_future(self._stop.wait())
        try:
            await asyncio.wait({sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in (sleeper, stopper):
                waiter.cancel()
            await asyncio.wait({sleeper, stopper})
