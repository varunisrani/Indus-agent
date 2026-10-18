"""The seam between the turn loop and P03's provider call machinery.

The loop asks a ``ModelCaller`` for one answer. ``RouteCaller`` is the real one: P03's
``stream_call`` (streaming, stale detection, reconnects, partial-delivery stubs, cancellation)
inside ``retry_call`` (classified retries with waits on the injected clock). It returns P03's
``CallResult`` or ``RouteChanged`` and raises ``RetryFailed`` when every attempt failed. The loop
owns iteration flow only (R01.5, R01.6).

Cancelling the task that awaits ``call`` closes the HTTP stream (P03) — that is how an interrupt
or a redirect stops a call, including during a retry wait.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from indus.providers.retry import DEFAULT_MAX_RETRIES, RetryWait, retry_call
from indus.providers.streaming import (
    CallOutcome,
    RouteChanged,
    StaleStreak,
    StreamFlags,
    stream_call,
)
from indus.providers.timeouts import TimeoutPolicy
from indus.providers.transports import get_transport
from indus.providers.types import ChatRequest

if TYPE_CHECKING:
    import httpx

    from indus.foundation.clock import Clock
    from indus.providers.classify import ClassifiedError

__all__ = ["ModelCaller", "OnEvent", "OnWait", "RouteCaller"]

type OnEvent = Callable[[Any], object | Awaitable[object]]
type OnWait = Callable[[RetryWait], object]


class ModelCaller(Protocol):
    @property
    def model(self) -> str: ...

    async def call(
        self, request: ChatRequest, *, on_event: OnEvent, on_wait: OnWait | None = None
    ) -> CallOutcome: ...


@dataclass
class RouteCaller:
    """One route's caller. ``flags`` and ``streak`` are kept per route for the session."""

    client: httpx.AsyncClient
    route: Any
    clock: Clock
    policy: TimeoutPolicy = field(default_factory=TimeoutPolicy)
    max_retries: int = DEFAULT_MAX_RETRIES
    flags: StreamFlags = field(default_factory=StreamFlags)
    streak: StaleStreak = field(default_factory=StaleStreak)
    epoch: Callable[[], object] = lambda: 0
    # False once the route this caller was built for is no longer the bound one: the next attempt
    # (a retry included) is not sent and the call ends ``RouteChanged`` (R01.5).
    still_current: Callable[[], bool] = lambda: True
    # The verdict for a failed attempt (P03 ``classify`` when None); the agent passes one that
    # knows the route, its profile and the clock, plus the credential pool's per-attempt rule.
    classify: Callable[[BaseException], ClassifiedError] | None = None

    @property
    def model(self) -> str:
        return str(getattr(self.route, "model", ""))

    async def call(
        self, request: ChatRequest, *, on_event: OnEvent, on_wait: OnWait | None = None
    ) -> CallOutcome:
        transport = get_transport(str(getattr(self.route, "api_mode", "chat_completions")))

        async def once() -> CallOutcome:
            if not self.still_current():
                return RouteChanged(None, 0, 0)
            return await stream_call(
                self.client,
                transport,
                self.route,
                request,
                policy=self.policy,
                clock=self.clock,
                on_event=on_event,
                route_epoch=self.epoch,
                flags=self.flags,
                streak=self.streak,
            )

        return await retry_call(
            once,
            max_retries=self.max_retries,
            clock=self.clock,
            on_wait=on_wait,
            classify=self.classify,
        )
