# pyright: strict
"""The trust tier, the per-server circuit breaker and OAuth parking (P13-07).

Trust: a server with ``trust: untrusted`` (P13-01 parses any unrecognised value as untrusted; a
missing one is ``full``) gets a person's approval before every call to a tool whose discovered
``readOnlyHint`` is not exactly JSON ``true``. The hint is the wire value
(``LiveSession.raw_hints``: the SDK would read ``"yes"`` as true), captured at discovery and
frozen until the next listing; anything else, a missing hint included, counts as write-capable
(fail closed, R07.25). The ask is the P05 per-tool gate (``safety.approvals.tool_gate``).

Breaker, per (profile, server): 3 failures in a row open it for 60 s, and calls get a plain
"unreachable, retry later" result without touching the server; after the 60 s one probe call is
let through (half-open): success closes it, failure opens it for another 60 s.

OAuth (R07.24): discovery, reconcile and reload never log in interactively — the OAuth layer asks
``interactive_auth_allowed()`` before it opens a browser, and only code inside
``allow_interactive_auth()`` (``indus mcp login``) gets ``True``. A connect the server refuses with
401/403 parks the server (P13-04 treats it as permanent); ``AuthParking`` remembers the credential
stamp it failed with, so later discovery passes (new sessions) leave it alone until the stamp
changes; only the lifecycle's own timed probe retries the token meanwhile.
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx2

from indus.foundation.clock import Clock
from indus.foundation.scope import Scope
from indus.mcp.connect import McpConnectFailed, first_cause
from indus.mcp.oauth import McpAuthRequired
from indus.protocol.requests_port import ServerRequestPort
from indus.safety.approvals.decision import ApprovalState
from indus.safety.approvals.tool_gate import approve_tool_call

__all__ = [
    "BREAKER_COOLDOWN_S",
    "BREAKER_THRESHOLD",
    "AuthParking",
    "BreakerBook",
    "BreakerState",
    "CircuitBreaker",
    "CredentialStamp",
    "allow_interactive_auth",
    "background_auth",
    "gate_call",
    "interactive_auth_allowed",
    "is_auth_failure",
    "needs_approval",
    "pattern_key",
    "read_only",
]

log = logging.getLogger(__name__)

BREAKER_THRESHOLD = 3
BREAKER_COOLDOWN_S = 60.0


# -- the trust tier -----------------------------------------------------------------------------


def read_only(raw_hint: object) -> bool:
    """True only for a hint that was exactly JSON ``true`` on the wire."""
    return raw_hint is True


def needs_approval(trust: str, is_read_only: bool) -> bool:
    """Whether a call needs a person first: any tier but ``full`` gates write-capable tools."""
    return trust != "full" and not is_read_only


def pattern_key(server: str, tool: str) -> str:
    """The session-approval key of one server tool."""
    return f"mcp:{server}:{tool}"


async def gate_call(
    server: str,
    tool: str,
    *,
    trust: str,
    is_read_only: bool,
    scope: Scope | None,
    session_id: str | None = None,
    turn_id: str | None = None,
    broker: ServerRequestPort | None = None,
    run_context: object = None,
    judge: Any = None,
    state: ApprovalState | None = None,
) -> str | None:
    """None when the call may go to the server; otherwise the refusal text for the model."""
    if not needs_approval(trust, is_read_only):
        return None
    verdict = await approve_tool_call(
        f"MCP tool {tool} on the untrusted server {server}",
        f"the server {server} is marked untrusted and {tool} does not declare itself read-only, "
        "so it may change things outside this machine",
        pattern_key(server, tool),
        scope=scope,
        session_id=session_id,
        turn_id=turn_id,
        broker=broker,
        run_context=run_context,
        judge=judge,
        state=state,
    )
    if verdict.approved:
        return None
    log.info("MCP tool %s on untrusted server %s not run (%s)", tool, server, verdict.outcome)
    return verdict.message or (
        f"BLOCKED: {tool} on the untrusted MCP server {server} was not approved, so it did not run."
    )


# -- the breaker --------------------------------------------------------------------------------


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Consecutive failures of one server's calls (see the module notes).

    Use: ``refusal = breaker.check()``; when None, run the call and report it with
    ``record(True)`` / ``record(False, application=...)``, or ``record(None)`` when it never
    reached an outcome (cancelled), so a half-open probe slot is given back.
    """

    def __init__(
        self,
        name: str,
        clock: Clock,
        *,
        threshold: int = BREAKER_THRESHOLD,
        cooldown: float = BREAKER_COOLDOWN_S,
    ) -> None:
        self.name = name
        self.clock = clock
        self.threshold = threshold
        self.cooldown = cooldown
        self.failures = 0
        self._opened_at: float | None = None
        self._probing = False
        self._all_application = True

    @property
    def state(self) -> BreakerState:
        if self._opened_at is None:
            return BreakerState.CLOSED
        if self.clock.monotonic() - self._opened_at < self.cooldown:
            return BreakerState.OPEN
        return BreakerState.HALF_OPEN

    def retry_in(self) -> float:
        if self._opened_at is None:
            return 0.0
        return max(0.0, self.cooldown - (self.clock.monotonic() - self._opened_at))

    def check(self) -> str | None:
        """None when a call may go ahead (taking the probe slot when half-open)."""
        state = self.state
        if state is BreakerState.CLOSED:
            return None
        if state is BreakerState.HALF_OPEN and not self._probing:
            self._probing = True
            return None
        wait = max(1, round(self.retry_in())) if state is BreakerState.OPEN else 1
        if self._all_application:
            return (
                f"MCP server {self.name} rejected the last {self.failures} calls (it answers, but "
                f"each call came back as an error). Calls to it are paused for about {wait} s. "
                "Do not repeat the same call: change the arguments or take another approach."
            )
        return (
            f"MCP server {self.name} is unreachable after {self.failures} failed calls in a row; "
            f"retry later (in about {wait} s). Do not call its tools until then: use another "
            "approach or ask the user to check the server."
        )

    def record(self, ok: bool | None, *, application: bool = False) -> None:
        probing, self._probing = self._probing, False
        if ok is None:
            return
        if ok:
            if self.failures or self._opened_at is not None:
                log.info("MCP server %s: calls succeed again; breaker closed", self.name)
            self.failures = 0
            self._opened_at = None
            self._all_application = True
            return
        self.failures += 1
        self._all_application = application and (self.failures == 1 or self._all_application)
        if probing or self.failures >= self.threshold:
            if self._opened_at is None or probing:
                log.warning(
                    "MCP server %s: %d failed calls in a row; pausing its calls for %.0f s",
                    self.name,
                    self.failures,
                    self.cooldown,
                )
            self._opened_at = self.clock.monotonic()

    def reset(self) -> None:
        self.failures = 0
        self._opened_at = None
        self._probing = False
        self._all_application = True


class BreakerBook:
    """One breaker per (profile, server)."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self._breakers: dict[tuple[str, str], CircuitBreaker] = {}

    def get(self, profile: str, server: str) -> CircuitBreaker:
        key = (profile, server)
        found = self._breakers.get(key)
        if found is None:
            found = self._breakers[key] = CircuitBreaker(server, self.clock)
        return found

    def drop(self, profile: str, server: str) -> None:
        self._breakers.pop((profile, server), None)


# -- OAuth in the background --------------------------------------------------------------------

_interactive: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "indus_mcp_interactive_auth", default=False
)


def interactive_auth_allowed() -> bool:
    """True only inside ``allow_interactive_auth()``: may the OAuth layer open a browser or
    prompt? Background work (discovery, reconcile, reload, cron) never may."""
    return _interactive.get()


@contextmanager
def allow_interactive_auth() -> Generator[None]:
    """Mark the code inside as a person's own login (``indus mcp login``)."""
    token = _interactive.set(True)
    try:
        yield
    finally:
        _interactive.reset(token)


@contextmanager
def background_auth() -> Generator[None]:
    """The code inside never logs in interactively, whatever its caller allowed. Server runner
    tasks are started inside it, so their later reconnects stay background work."""
    token = _interactive.set(False)
    try:
        yield
    finally:
        _interactive.reset(token)


def is_auth_failure(exc: BaseException) -> bool:
    """The server refused the credentials (HTTP 401 or 403), however the SDK wrapped it, or the
    background OAuth handler stopped because only a new sign-in can help (P13-09's
    ``McpAuthRequired``)."""
    root = first_cause(exc)
    seen = 0
    while seen < 8:
        if isinstance(root, McpAuthRequired):
            return True
        if isinstance(root, McpConnectFailed) and root.cause is not None:
            root = first_cause(root.cause)
        elif root.__cause__ is not None and isinstance(root.__cause__, McpAuthRequired):
            return True
        else:
            break
        seen += 1
    return isinstance(root, httpx2.HTTPStatusError) and root.response.status_code in (401, 403)


CredentialStamp = Callable[[str], object]
"""Server name → something that changes when its stored credentials change (P13-09 supplies it:
e.g. the token record's revision). ``None`` means no credentials are stored."""


def _no_stamp(server: str) -> object:
    return None


@dataclass
class _Park:
    stamp: object
    reason: str


class AuthParking:
    """Servers parked because their credentials were refused, and the stamp they failed with."""

    def __init__(self, stamp: CredentialStamp | None = None) -> None:
        self._stamp = stamp or _no_stamp
        self._parked: dict[str, _Park] = {}

    def note(self, server: str, exc: BaseException) -> bool:
        """Record ``exc`` when it is an auth refusal (one warning per park); True if recorded."""
        if not is_auth_failure(exc):
            return False
        stamp = self._safe_stamp(server)
        current = self._parked.get(server)
        if current is None or current.stamp != stamp:
            log.warning(
                "MCP server %s refused its credentials; parked until the user signs in again "
                "(indus mcp login %s). No browser is opened from background work.",
                server,
                server,
            )
        self._parked[server] = _Park(stamp, str(first_cause(exc)))
        return True

    def clear(self, server: str) -> None:
        self._parked.pop(server, None)

    def parked(self, server: str) -> bool:
        return server in self._parked

    def may_retry(self, server: str) -> bool:
        """False while the server is auth-parked and its credentials are unchanged."""
        park = self._parked.get(server)
        if park is None:
            return True
        return self._safe_stamp(server) != park.stamp

    def _safe_stamp(self, server: str) -> object:
        try:
            return self._stamp(server)
        except Exception as exc:
            log.debug("credential stamp for %s failed: %s", server, type(exc).__name__)
            return None
