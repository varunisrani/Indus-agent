"""The runner's intake: every adapter event, in a fixed order of gates, to a turn (P15-11).

``AdmissionPipeline.accept`` is the sink every adapter emits into. Control commands (``/stop``,
``/new``, ``/queue``, ``/status``, ``/approve``, ``/deny``) are handled inline, in the adapter's
own await, never as a background task (R06.1). A key that is already replying goes to the busy
guard (P15-12, guard 1). Plain text may first wait in a split-paste batch. Everything else starts
its own handler task, which runs ``handle``:

1. the per-chat identity context is reset, then bound to this message (R06.21, R01.16): a task
   copies its parent's context, so without the reset a subprocess could carry another chat's
   identity; ``identity_env()`` is what a child process gets;
2. identity: a known platform and a chat id;
3. startup-restore queue: while boot recovery runs, events wait and are replayed in order;
4. the ``pre_gateway_dispatch`` plugin hook (skip drops, rewrite replaces the text; fail-open);
5. authorization (P15-08); an unknown sender gets the pair / ignore / decline treatment; a bot
   sender is counted once by the bot-loop guard (P15-10); in groups only messages that mention us
   (or reply to us, or are commands) are answered unless the channel is free-response;
6. the emergency-stop gate: a paused gateway answers new turns with the paused notice;
   recognised commands and messages for a session that is replying or asking still pass;
7. pending prompt replies (interceptors: numbered confirmations here, clarify answers in P15-22);
8. the busy guard (guard 2) when the key holds a claim or its session runs a turn;
9. slash dispatch (P15-13), else the session claim;
10. the session is found or created, ``post_gateway_admission`` (once per admitted message,
    with its session id; fail-open), media enrichment (P15-23 plugs in; the default adds the
    reply quote), then the turn through ``SessionService``.

Front doors (P15-22, P15-23) see every non-internal event first, in the adapter's own await, ahead
of control routing, busy guards and mention gating: a button press or a typed answer to a pending
prompt, an album still being collected. A door that returns True has taken the event (it runs the
authorization chain itself); a failing door is skipped.

Claim before the first await (R06.2): ``ClaimTable.claim`` installs the key's sentinel
synchronously, so of two events with no await between them only one starts a turn; the other is
a busy message. Every claim carries a run generation and ``release`` frees the key only while
that claim still owns it, so a turn displaced by ``/new`` or ``/stop`` cannot clear its successor.
Leaked claims (their task finished without releasing) and stale ones (older than
``agent.gateway_timeout`` with nothing running) are evicted before the busy check.

Replies: the final text of every completed turn of a routed session — ours, the service's steer
follow-ups, P10 injected turns — leaves through one ``on_turn_end`` listener to the session's
origin, through the outbound sender (P15-17). A failed turn gets a short notice; an interrupted
one says nothing. Every exit path of a turn restores a one-turn model, clears the durable
active-turn marker, releases the claim (generation-guarded) and starts the key's next queued
message. A turn's final carries its ledger key (session key, ``<session id>:<turn id>``) so the
outbound sender records it in the delivery ledger (P15-19) before sending; the active-turn
marker is cleared only after that final has been through the sender (R06.5): ``_finish`` waits
for the turn-end listener that delivered it (bounded by ``FINAL_SETTLE_S``).

Mirror notes (P15-20): a message delivered into a routed chat from elsewhere (cron, ``indus
send``) while its session was not running here waits in the session's metadata; it is staged as
a must-deliver note just before that session's next turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import itertools
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, Protocol

from indus.core.session_scheduler import TurnEnd
from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
from indus.foundation.config import loader as config_loader
from indus.foundation.config import writer as config_writer
from indus.foundation.scope import Scope
from indus.gateway.core.authz import (
    Authorizer,
    MentionPolicy,
    UnauthorizedHandler,
    should_answer,
)
from indus.gateway.core.batching import Batcher, TextBatchPolicy, merge_events
from indus.gateway.core.bot_loop import (
    CONCURRENCY_REFUSAL,
    BotLoopGuard,
    ConcurrencyCap,
    conversation_of,
)
from indus.gateway.core.events import InboundEvent, MessageKind, Source, model_text
from indus.gateway.core.sent_index import SentIndex
from indus.gateway.core.session_keys import build_session_key, origin_of
from indus.protocol.base import ProtocolModel
from indus.protocol.errors import SESSION_BUSY, SESSION_NOT_FOUND, TEXT_EMPTY
from indus.protocol.events import ExitReason
from indus.protocol.methods import ProtocolError, SessionCreateParams
from indus.service.commands.registry import REGISTRY
from indus.service.session_service import BUSY_COPY, SessionService
from indus.state import gateway_routes as routes
from indus.state.registry import StoreHandle

if TYPE_CHECKING:
    from indus.gateway.adapters.base import Adapter
    from indus.gateway.core.busy import BusyGuard
    from indus.gateway.core.commands import ChatCommands
    from indus.gateway.daemon.reconnect import ReconnectWatcher
    from indus.service.automation import SessionAutomation

__all__ = [
    "ANSWERABLE_KINDS",
    "AdmissionPipeline",
    "FrontDoor",
    "Claim",
    "ClaimTable",
    "GatewayHooks",
    "GatewayIdentity",
    "IDENTITY_ENV",
    "IntakeSettings",
    "Interceptor",
    "ModelChoice",
    "ModelSwitcher",
    "ReplyKind",
    "ReplySender",
    "RouteSwitcher",
    "bind_identity",
    "current_identity",
    "identity_env",
    "reset_identity",
]

log = logging.getLogger(__name__)

ANSWERABLE_KINDS = ("approval", "clarify")
"""Server requests a chat can answer; sudo and secret prompts need a local terminal."""

_INTERRUPTED = frozenset({ExitReason.INTERRUPTED_BY_USER, ExitReason.INTERRUPTED_BY_SYSTEM})
_SECRET_KINDS = frozenset({"sudo", "secret"})
_ORIGINS_KEPT = 4096
_ANCHORS_KEPT = 1024
FINAL_SETTLE_S = 300.0
"""Longest wait for a turn's final to pass through the sender before the marker is cleared."""

FAILED_TURN = "Sorry, I couldn't finish that reply ({reason}). Please try again."
SECRET_PROMPT = (
    "This step needs a password or secret typed into a terminal, which isn't supported here. "
    "Run it from the terminal app instead."
)
APPROVAL_FALLBACK = (
    "I need your approval to run:\n{command}\nWhy: {why}\nReply /approve to run it once, "
    "/approve session or /approve always to keep allowing it, or /deny [reason]."
)


# -- per-chat identity (R06.21, R01.16) ------------------------------------------------------------

IDENTITY_ENV: Mapping[str, str] = {
    "platform": "INDUS_GATEWAY_PLATFORM",
    "chat_id": "INDUS_GATEWAY_CHAT_ID",
    "user_id": "INDUS_GATEWAY_USER_ID",
    "thread_id": "INDUS_GATEWAY_THREAD_ID",
    "session_key": "INDUS_GATEWAY_SESSION_KEY",
    "session_id": "INDUS_SESSION_ID",
}


@dataclass(frozen=True)
class GatewayIdentity:
    """Who and where the message being handled comes from."""

    platform: str
    chat_id: str
    user_id: str = ""
    thread_id: str = ""
    session_key: str = ""
    session_id: str = ""


_IDENTITY: contextvars.ContextVar[GatewayIdentity | None] = contextvars.ContextVar(
    "indus_gateway_identity", default=None
)


def current_identity() -> GatewayIdentity | None:
    return _IDENTITY.get()


def bind_identity(identity: GatewayIdentity | None) -> None:
    _IDENTITY.set(identity)


def reset_identity() -> None:
    """Forget any identity inherited from the task that started this one."""
    _IDENTITY.set(None)


def identity_env(identity: GatewayIdentity | None = None) -> dict[str, str]:
    """Environment variables naming the current chat (empty outside a chat handler)."""
    found = identity if identity is not None else _IDENTITY.get()
    if found is None:
        return {}
    env: dict[str, str] = {}
    for attr, name in IDENTITY_ENV.items():
        value = getattr(found, attr)
        if value:
            env[name] = str(value)
    return env


# -- claims (R06.2) --------------------------------------------------------------------------------


@dataclass
class Claim:
    """One key's running-turn sentinel."""

    key: str
    generation: int
    started_at: float
    session_id: str | None = None
    turn_generation: int | None = None
    task: asyncio.Task[Any] | None = None
    capped: bool = False


class ClaimTable:
    """Per-key claims; every method is synchronous (no await between check and install)."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._claims: dict[str, Claim] = {}
        self._generations = itertools.count(1)

    def __len__(self) -> int:
        return len(self._claims)

    def keys(self) -> list[str]:
        return list(self._claims)

    def current(self, key: str) -> Claim | None:
        return self._claims.get(key)

    def claim(self, key: str) -> Claim | None:
        """Install a sentinel for ``key``; None when the key is already claimed."""
        if key in self._claims:
            return None
        claim = Claim(key, next(self._generations), self._clock.monotonic())
        self._claims[key] = claim
        return claim

    def release(self, claim: Claim) -> bool:
        """Free ``claim.key`` only while ``claim`` still owns it."""
        if self._claims.get(claim.key) is claim:
            del self._claims[claim.key]
            return True
        return False

    def drop(self, key: str) -> Claim | None:
        """Take the key's claim away (``/new``, ``/stop``); its own release is then a no-op."""
        return self._claims.pop(key, None)

    def evict_stale(
        self, key: str, *, stale_after: float, running: Callable[[Claim], bool]
    ) -> Claim | None:
        """Evict a leaked claim (task done) or a stale one (old, nothing running)."""
        claim = self._claims.get(key)
        if claim is None:
            return None
        leaked = claim.task is not None and claim.task.done()
        aged = (
            stale_after > 0
            and self._clock.monotonic() - claim.started_at >= stale_after
            and not running(claim)
        )
        if not (leaked or aged):
            return None
        del self._claims[key]
        log.warning(
            "evicted a %s claim for %s (generation %d)",
            "leaked" if leaked else "stale",
            key,
            claim.generation,
        )
        return claim


# -- seams -----------------------------------------------------------------------------------------

type ReplyKind = Literal["final", "notice", "ack", "command"]


class ReplySender(Protocol):
    """The outbound sender (P15-17): formatting, chunking, redaction, threading, the ledger."""

    async def reply(
        self,
        source: Source,
        text: str,
        *,
        reply_to: str | None = None,
        kind: ReplyKind = "final",
        session_key: str | None = None,
        message_ref: str | None = None,
    ) -> object: ...


class GatewayHooks(Protocol):
    """The plugin hooks the intake fires (``indus.plugins.hooks.HookBus`` has this shape)."""

    async def pre_gateway_dispatch(self, **payload: Any) -> dict[str, Any] | None: ...

    async def observe(self, name: str, **payload: Any) -> None: ...


type Interceptor = Callable[[InboundEvent, str], Awaitable[str | None]]
"""A pending prompt's reply catcher: ``None`` = not mine, a string (maybe empty) = handled."""

type Enricher = Callable[[InboundEvent], Awaitable[str]]
type FrontDoor = Callable[[InboundEvent], Awaitable[bool]]
"""Sees an adapter event before anything else; True = taken (see the module notes)."""
type RequestHook = Callable[[Source, ProtocolModel], Awaitable[None]]


@dataclass(frozen=True)
class ModelChoice:
    """A model picked for a chat: ``source`` is ``once``, ``session`` or ``channel``."""

    model: str
    provider: str | None = None
    base_url: str | None = None
    source: str = "session"

    def as_override(self) -> dict[str, str]:
        found = {"model": self.model, "provider": self.provider, "base_url": self.base_url}
        return {k: v for k, v in found.items() if v}

    def label(self) -> str:
        return f"{self.provider}:{self.model}" if self.provider else self.model


class ModelSwitcher(Protocol):
    async def check(self, service: SessionService, choice: ModelChoice) -> str | None:
        """None when ``choice`` can be used, else why not."""
        ...

    async def apply(
        self, service: SessionService, session_id: str, choice: ModelChoice | None
    ) -> None:
        """Point the session at ``choice`` (None: the profile's default route)."""
        ...


class RouteSwitcher:
    """Resolves a route for the choice (credentials re-resolved, never stored) and binds it."""

    def _route(self, service: SessionService, choice: ModelChoice | None) -> Any:
        if choice is None:
            return service.route
        from indus.providers.route import resolve

        return resolve(
            service.scope, model=choice.model, provider=choice.provider, base_url=choice.base_url
        )

    async def check(self, service: SessionService, choice: ModelChoice) -> str | None:
        try:
            await run_blocking(self._route, service, choice, label="model route")
        except Exception as exc:
            return str(exc) or type(exc).__name__
        return None

    async def apply(
        self, service: SessionService, session_id: str, choice: ModelChoice | None
    ) -> None:
        route = await run_blocking(self._route, service, choice, label="model route")
        await service.agent(session_id).binding.apply(route)


# -- settings --------------------------------------------------------------------------------------

BUSY_MODES = ("interrupt", "queue", "steer")


def _config_get(view: Any, key: str, default: Any) -> Any:
    try:
        value = view.get(key)
    except Exception:
        return default
    return default if value is None else value


def _as_bool(value: object, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _as_float(value: object, default: float, *, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    number = float(value)
    return number if low <= number <= high else default


@dataclass(frozen=True)
class IntakeSettings:
    """The settings the intake reads on every message (an edit applies without a restart)."""

    profile: str = ""
    busy_mode: str = "interrupt"
    busy_ack: bool = True
    busy_quiet: float = 0.35
    busy_cap: float = 1.0
    busy_tip_seen: bool = False
    group_sessions_per_user: bool = True
    thread_sessions_per_user: bool = False
    stale_after: float = 1800.0
    destructive_confirm: bool = False
    mcp_reload_confirm: bool = True

    @classmethod
    def from_scope(cls, scope: Scope) -> IntakeSettings:
        try:
            view: Any = scope.config if scope.config is not None else config_loader.load(scope)
        except Exception:
            log.warning("could not read the settings; using the defaults", exc_info=True)
            return cls(profile=scope.profile_id)
        mode = str(_config_get(view, "display.busy_input_mode", "interrupt")).strip().lower()
        timeout = _config_get(view, "agent.gateway_timeout", 1800)
        return cls(
            profile=scope.profile_id,
            busy_mode=mode if mode in BUSY_MODES else "interrupt",
            busy_ack=_as_bool(_config_get(view, "display.busy_ack", True), True),
            busy_quiet=_as_float(
                _config_get(view, "display.busy_text_quiet_s", 0.35), 0.35, low=0.0, high=10.0
            ),
            busy_cap=_as_float(
                _config_get(view, "display.busy_text_cap_s", 1.0), 1.0, low=0.0, high=30.0
            ),
            busy_tip_seen=_as_bool(_config_get(view, "onboarding.seen.busy_input", False), False),
            group_sessions_per_user=_as_bool(
                _config_get(view, "gateway.group_sessions_per_user", True), True
            ),
            thread_sessions_per_user=_as_bool(
                _config_get(view, "gateway.thread_sessions_per_user", False), False
            ),
            stale_after=_as_float(timeout, 1800.0, low=0.0, high=7 * 86400.0),
            destructive_confirm=_as_bool(
                _config_get(view, "approvals.destructive_slash_confirm", False), False
            ),
            mcp_reload_confirm=_as_bool(
                _config_get(view, "approvals.mcp_reload_confirm", True), True
            ),
        )


def default_toolsets(platform: str) -> list[str] | None:
    """``indus-<platform>`` when that bundle exists, else the service's own selection."""
    from indus.tools.toolsets import TOOLSETS

    name = f"indus-{platform}"
    return [name] if name in TOOLSETS else None


@dataclass
class _Origin:
    key: str
    source: Source


# -- the pipeline ----------------------------------------------------------------------------------


class AdmissionPipeline:
    """The gateway's intake for one profile (see the module notes)."""

    def __init__(
        self,
        *,
        scope: Scope,
        service: SessionService,
        sender: ReplySender,
        store: StoreHandle,
        authorizer: Authorizer,
        unauthorized: UnauthorizedHandler | None = None,
        bot_guard: BotLoopGuard | None = None,
        cap: ConcurrencyCap | None = None,
        hooks: GatewayHooks | None = None,
        paused: Callable[[], str | None] | None = None,
        interceptors: Sequence[Interceptor] = (),
        enrich: Enricher | None = None,
        sent_index: SentIndex | None = None,
        adapters: Mapping[str, Adapter] | None = None,
        settings: Callable[[], IntakeSettings] | None = None,
        extra_for: Callable[[str], Mapping[str, Any] | None] | None = None,
        channel_model: Callable[[str, str], ModelChoice | None] | None = None,
        home_for: Callable[[str], str | None] | None = None,
        switcher: ModelSwitcher | None = None,
        toolsets_for: Callable[[str], list[str] | None] = default_toolsets,
        batching: Callable[[str], TextBatchPolicy | None] | None = None,
        on_request: RequestHook | None = None,
        front_doors: Sequence[FrontDoor] = (),
        automation: SessionAutomation | None = None,
        commands: Callable[[AdmissionPipeline], ChatCommands] | None = None,
        clock: Clock | None = None,
    ) -> None:
        from indus.gateway.core.busy import BusyGuard, PendingQueue
        from indus.gateway.core.commands import ChatCommands

        self.scope = scope
        self.service = service
        self.sender = sender
        self.store = store
        self.authorizer = authorizer
        self.unauthorized = unauthorized
        self.bot_guard = bot_guard
        self.cap = cap
        self.hooks = hooks
        self.paused = paused
        self.enrich = enrich
        self.sent_index = sent_index
        self.adapters: dict[str, Adapter] = dict(adapters or {})
        self._settings = settings or (lambda: IntakeSettings.from_scope(scope))
        self.extra_for = extra_for or (lambda _platform: None)
        self.channel_model = channel_model or (lambda _platform, _chat: None)
        self.home_for = home_for or (lambda _platform: None)
        self.switcher: ModelSwitcher = switcher or RouteSwitcher()
        self.toolsets_for = toolsets_for
        self.batching = batching
        self.on_request = on_request
        self.front_doors: list[FrontDoor] = list(front_doors)
        self.automation = automation
        self.clock: Clock = clock or scope.clock
        self.claims = ClaimTable(self.clock)
        self.pending = PendingQueue()
        self.paused_platforms: set[str] = set()
        self.reconnect: ReconnectWatcher | None = None  # platforms being retried (P15-29)
        self.progress_modes: dict[str, str] = {}
        self.once_next: dict[str, ModelChoice] = {}
        self._once_turns: dict[int, tuple[str, str, Source]] = {}
        self._applied: dict[str, ModelChoice | None] = {}
        self._sessions: dict[str, str] = {}
        self._history: dict[str, list[str]] = {}
        self._origins: OrderedDict[str, _Origin] = OrderedDict()
        self._anchors: OrderedDict[tuple[str, int], str] = OrderedDict()
        self._finals: OrderedDict[tuple[str, int], asyncio.Event] = OrderedDict()
        self._watched: set[str] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._restoring = False
        self._restore_queue: list[InboundEvent] = []
        self._undo: list[Callable[[], None]] = []
        self._tip_shown = False
        self._closed = False
        self.interceptors: list[Interceptor] = list(interceptors)
        self.busy_guard: BusyGuard = BusyGuard(self)
        self.commands: ChatCommands = commands(self) if commands else ChatCommands(self)
        self.interceptors.insert(0, self.commands.confirms.intercept)
        self._paste: Batcher[InboundEvent] = Batcher(
            self.clock,
            flush=self._flush_paste,
            merge=merge_events,
            delay_for=self._paste_delay,
        )
        self._paste_policy: dict[str, TextBatchPolicy] = {}

    # -- lifecycle ---------------------------------------------------------------------------------

    def start(self) -> None:
        """Listen to the service (turn ends, moved sessions, queued input). Call on its loop."""
        self.service.set_answerable_kinds(ANSWERABLE_KINDS)
        self._undo.append(self.service.on_turn_end(self._on_turn_end))
        self._undo.append(self.service.on_session_moved(self._on_moved))
        self.service.set_input_probe(self._input_waiting)
        self._undo.append(lambda: self.service.set_input_probe(None))
        for adapter in self.adapters.values():
            adapter.set_sink(self.accept)

    async def aclose(self) -> None:
        self._closed = True
        for undo in reversed(self._undo):
            with contextlib.suppress(Exception):
                undo()
        self._undo.clear()
        await self._paste.aclose()
        await self.busy_guard.aclose()
        tasks = [t for t in self._tasks if not t.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def settings(self) -> IntakeSettings:
        try:
            return self._settings()
        except Exception:
            log.warning("could not read the intake settings", exc_info=True)
            return IntakeSettings(profile=self.scope.profile_id)

    def spawn(
        self, coro: Coroutine[Any, Any, Any], *, name: str = "gateway task"
    ) -> asyncio.Task[Any]:
        """Run ``coro`` as a tracked task (cancelled at ``aclose``; failures logged)."""
        task = asyncio.ensure_future(coro)
        task.set_name(name)
        self._tasks.add(task)

        def done(finished: asyncio.Task[Any]) -> None:
            self._tasks.discard(finished)
            if not finished.cancelled() and finished.exception() is not None:
                log.error("%s failed", name, exc_info=finished.exception())

        task.add_done_callback(done)
        return task

    async def idle(self) -> None:
        """Wait until no tracked task is running (tests and drain)."""
        while True:
            tasks = [t for t in self._tasks if not t.done()]
            if not tasks:
                return
            await asyncio.wait(tasks)

    # -- startup restore ---------------------------------------------------------------------------

    def begin_restore(self) -> None:
        """Hold new events while boot recovery runs (P15-26)."""
        self._restoring = True

    async def finish_restore(self) -> int:
        """Replay the held events in arrival order; how many there were."""
        self._restoring = False
        held, self._restore_queue = self._restore_queue, []
        for event in held:
            await self.accept(event)
        return len(held)

    def _held_for_restore(self, event: InboundEvent) -> bool:
        if self._restoring and not event.internal:
            self._restore_queue.append(event)
            return True
        return False

    # -- keys and state ----------------------------------------------------------------------------

    def session_key(self, source: Source) -> str:
        settings = self.settings()
        return build_session_key(
            source,
            profile=settings.profile,
            group_sessions_per_user=settings.group_sessions_per_user,
            thread_sessions_per_user=settings.thread_sessions_per_user,
        )

    def cached_session(self, key: str) -> str | None:
        sid = self._sessions.get(key)
        return self.service.current_id(sid) if sid else None

    async def session_id_for(self, key: str) -> str | None:
        """The key's session id (cached, else from the routing index); never creates one."""
        cached = self.cached_session(key)
        if cached:
            return cached
        entry = await run_blocking(routes.get, self.store, key, label="gateway route")
        return None if entry is None else self.service.current_id(entry.session_id)

    def history(self, key: str) -> list[str]:
        """Session ids this key used in this process, newest first."""
        return list(reversed(self._history.get(key, [])))

    def _remember(self, key: str, session_id: str) -> None:
        self._sessions[key] = session_id
        seen = self._history.setdefault(key, [])
        if session_id in seen:
            seen.remove(session_id)
        seen.append(session_id)
        del seen[:-50]

    def is_running(self, session_id: str | None) -> bool:
        """True while the service runs a turn for the session."""
        if not session_id:
            return False
        try:
            return self.service.agent(session_id).current is not None
        except ProtocolError:
            return False

    def busy(self, key: str) -> bool:
        """The key holds a claim or its session is replying."""
        return self.claims.current(key) is not None or self.is_running(self.cached_session(key))

    def _input_waiting(self, session_id: str) -> bool:
        origin = self._origins.get(self.service.current_id(session_id))
        return origin is not None and self.pending.depth(origin.key) > 0

    def drop_claim(self, key: str, *, cancel: bool) -> Claim | None:
        """Take the key's claim away (``/new``, ``/stop``); ``cancel`` also stops its handler
        when that is not the caller. The dropped turn's own release is then a no-op."""
        claim = self.claims.drop(key)
        if claim is None:
            return None
        if claim.capped and self.cap is not None:
            self.cap.release(key)
        task = claim.task
        if cancel and task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
        return claim

    def evict(self, key: str) -> None:
        evicted = self.claims.evict_stale(
            key,
            stale_after=self.settings().stale_after,
            running=lambda claim: self.is_running(claim.session_id),
        )
        if evicted is not None and evicted.capped and self.cap is not None:
            self.cap.release(key)

    # -- replies -----------------------------------------------------------------------------------

    async def reply(
        self,
        source: Source,
        text: str,
        *,
        reply_to: str | None = None,
        kind: ReplyKind = "command",
        session_key: str | None = None,
        message_ref: str | None = None,
    ) -> None:
        """Send through the outbound sender; a failure is logged, never raised."""
        if not text or not text.strip():
            return
        try:
            if session_key is not None:
                await self.sender.reply(
                    source,
                    text,
                    reply_to=reply_to,
                    kind=kind,
                    session_key=session_key,
                    message_ref=message_ref,
                )
            else:
                await self.sender.reply(source, text, reply_to=reply_to, kind=kind)
        except Exception:
            log.warning("could not send a %s reply on %s", kind, source.platform, exc_info=True)

    def _note_origin(self, session_id: str, key: str, source: Source) -> None:
        self._origins[session_id] = _Origin(key, source)
        self._origins.move_to_end(session_id)
        while len(self._origins) > _ORIGINS_KEPT:
            self._origins.popitem(last=False)

    async def origin_source(self, session_id: str) -> Source | None:
        """Where the session's turns come from (the chat and person of its last admitted
        message), or None when unknown."""
        origin = await self._origin_of(self.service.current_id(session_id))
        return origin.source if origin is not None else None

    async def _origin_of(self, session_id: str) -> _Origin | None:
        found = self._origins.get(session_id)
        if found is not None:
            return found
        try:
            entries = await run_blocking(
                routes.routes_for_session, self.store, session_id, label="gateway route"
            )
        except Exception:
            log.warning("could not look up the route of %s", session_id, exc_info=True)
            return None
        for entry in entries:
            try:
                source = Source.model_validate(entry.origin)
            except Exception:
                continue
            origin = _Origin(entry.session_key, source)
            self._note_origin(session_id, entry.session_key, source)
            return origin
        return None

    # -- the sink ----------------------------------------------------------------------------------

    async def accept(self, event: InboundEvent) -> None:
        """The adapter sink: inline control, guard 1, split-paste batching or a handler task."""
        if self._closed:
            return
        platform = str(event.source.platform).lower()
        if platform in self.paused_platforms and not event.internal:
            log.debug("dropped a message from paused platform %s", platform)
            return
        if self._held_for_restore(event):
            return
        if not event.internal and await self._front_door(event):
            return
        if self.busy_guard.is_control(event):
            await self.handle(event, inline=True)  # never the background path (R06.1)
            return
        key = self.session_key(event.source)
        self.evict(key)
        if not event.internal and self.busy(key):
            await self.busy_guard.adapter_level(event, key)
            return
        policy = self._batch_policy(event)
        if policy is not None:
            self._paste_policy[key] = policy
            self._paste.add(key, event)
            return
        self.spawn(self.handle(event), name=f"inbound {platform}")

    async def _front_door(self, event: InboundEvent) -> bool:
        for door in list(self.front_doors):
            try:
                if await door(event):
                    return True
            except Exception:
                log.warning("a front door failed; the message goes on as usual", exc_info=True)
        return False

    def _batch_policy(self, event: InboundEvent) -> TextBatchPolicy | None:
        if self.batching is None or event.internal or event.is_command:
            return None
        if event.kind is not MessageKind.TEXT or event.media:
            return None
        try:
            return self.batching(str(event.source.platform).lower())
        except Exception:
            log.warning("split-paste policy lookup failed", exc_info=True)
            return None

    def _paste_delay(self, last: InboundEvent) -> float:
        key = self.session_key(last.source)
        policy = self._paste_policy.get(key) or TextBatchPolicy()
        adapter = self.adapters.get(str(last.source.platform).lower())
        length = adapter.capabilities.len_fn if adapter is not None else len
        return policy.delay_for(length(last.text))

    async def _flush_paste(self, key: str, event: InboundEvent) -> None:
        self._paste_policy.pop(key, None)
        if self.busy(key):
            await self.busy_guard.adapter_level(event, key)
            return
        self.spawn(self.handle(event), name="inbound batch")

    # -- the gates ---------------------------------------------------------------------------------

    async def handle(self, event: InboundEvent, *, inline: bool = False) -> None:
        """Run one message through the gates (see the module notes).

        ``inline``: called in the adapter's own await (control commands); a turn the message
        turns into (``/queue`` while idle) then runs as a task so the adapter keeps reading."""
        reset_identity()
        if not self._identity_ok(event) or self._held_for_restore(event):
            return
        if not event.internal:
            rewritten = await self._pre_dispatch(event)
            if rewritten is None:
                return
            event = rewritten
        source = event.source
        key = self.session_key(source)
        bind_identity(
            GatewayIdentity(
                platform=str(source.platform),
                chat_id=str(source.chat_id),
                user_id=str(source.user_id or ""),
                thread_id=str(source.thread_id or ""),
                session_key=key,
            )
        )
        if not event.internal and not await self.authorize(event):
            return
        notice = self._pause_notice(event, key)
        if notice is not None:
            await self.reply(source, notice, reply_to=event.message_id, kind="notice")
            return
        if not event.internal and await self.intercept(event, key):
            return
        self.evict(key)
        if self.busy(key):
            await self.busy_guard.runner_level(event, key)
            return
        if event.is_command:
            from indus.gateway.core.commands import Rewrite

            outcome = await self.commands.dispatch_idle(event, key)
            if not isinstance(outcome, Rewrite):
                if outcome:
                    await self.reply(source, outcome, reply_to=event.message_id)
                return
            event = event.model_copy(update={"text": outcome.text, "allow_control": False})
        await self.run_admitted(event, key, background=inline)

    def _identity_ok(self, event: InboundEvent) -> bool:
        source = event.source
        if not str(source.platform).strip() or not str(source.chat_id).strip():
            log.debug("dropped an event without a platform or chat id")
            return False
        return True

    async def _pre_dispatch(self, event: InboundEvent) -> InboundEvent | None:
        hooks = self.hooks
        if hooks is None:
            return event
        source = event.source
        try:
            answer = await hooks.pre_gateway_dispatch(
                event=event,
                platform=source.platform,
                chat_id=source.chat_id,
                user_id=source.user_id,
                text=event.text,
            )
        except Exception:
            log.warning("pre_gateway_dispatch failed; message dispatched as is", exc_info=True)
            return event
        if not answer:
            return event
        action = answer.get("action")
        if action == "skip":
            log.info("a plugin skipped a %s message: %s", source.platform, answer.get("reason"))
            return None
        text = answer.get("text")
        if action == "rewrite" and isinstance(text, str):
            return event.model_copy(update={"text": text})
        return event

    async def authorize(self, event: InboundEvent) -> bool:
        """Authorization, unknown-sender handling, the bot-loop count and mention gating."""
        source = event.source
        decision = self.authorizer.check(source)
        if not decision.granted:
            log.info(
                "refused a %s message from %s (%s)",
                source.platform,
                source.user_id or "?",
                decision.step,
            )
            if decision.step != "bot_cooldown" and self.unauthorized is not None:
                await self._unauthorized(source, event)
            return False
        if source.is_bot and self.bot_guard is not None and not event.metadata.get("bot_counted"):
            state = self.bot_guard.admit(
                conversation_of(source, self.settings().profile), event.message_id
            )
            if state in ("tripped", "cooldown"):
                return False
        platform = str(source.platform).lower()
        if source.chat_type != "dm" and not event.is_command and not event.reply_to_is_own:
            mentioned = event.metadata.get("mentioned") is True
            policy = MentionPolicy.from_extra(self.extra_for(platform))
            if not should_answer(
                source,
                mentioned=mentioned,
                policy=policy,
                bot_mode=self.authorizer.bot_mode(platform),
            ):
                return False
        return True

    async def _unauthorized(self, source: Source, event: InboundEvent) -> None:
        handler = self.unauthorized
        if handler is None:
            return
        platform = str(source.platform).lower()
        home = self.home_for(platform)

        async def send_reply(text: str) -> None:
            await self.reply(source, text, reply_to=event.message_id, kind="notice")

        async def notify_home(text: str) -> None:
            if home:
                await self.reply(Source(platform=platform, chat_id=str(home)), text, kind="notice")

        try:
            await handler.respond(
                source, send_reply=send_reply, notify_home=notify_home, home_chat_id=home
            )
        except Exception:
            log.warning("the unknown-sender reply failed on %s", platform, exc_info=True)

    def _pause_notice(self, event: InboundEvent, key: str) -> str | None:
        probe = self.paused
        if probe is None or event.internal:
            return None
        try:
            notice = probe()
        except Exception:
            log.warning("the pause probe failed; treating the gateway as running", exc_info=True)
            return None
        if notice is None:
            return None
        name = event.command
        if name and REGISTRY.resolve(name) is not None:
            return None
        if self.busy(key) or self.commands.confirms.waiting(key):
            return None
        return notice

    async def intercept(self, event: InboundEvent, key: str) -> bool:
        """Offer the message to pending prompts; True when one took it."""
        if not event.allow_control:
            return False
        for interceptor in list(self.interceptors):
            try:
                answer = await interceptor(event, key)
            except Exception:
                log.warning("a prompt reply handler failed", exc_info=True)
                continue
            if answer is None:
                continue
            await self.reply(event.source, answer, reply_to=event.message_id)
            return True
        return False

    # -- sessions ----------------------------------------------------------------------------------

    async def _create_session(self, source: Source) -> str:
        platform = str(source.platform).lower()
        toolsets = self.toolsets_for(platform)
        created = await self.service.create(SessionCreateParams(toolsets=toolsets))
        return created.session_id

    async def session_for(self, key: str, source: Source) -> str:
        """The key's session, created and routed on first use, attached to the service."""
        entry = await run_blocking(routes.get, self.store, key, label="gateway route")
        fresh: str | None = None
        if entry is None:
            fresh = await self._create_session(source)
        made = fresh
        entry, created = await run_blocking(
            lambda: routes.get_or_create(
                self.store,
                key,
                new_session_id=lambda: made or "",
                platform=str(source.platform).lower(),
                chat_type=str(source.chat_type),
                origin=origin_of(source),
                display_name=source.chat_name or source.user_name,
            ),
            label="gateway route",
        )
        if fresh is not None and not created and entry.session_id != fresh:
            await self.service.close(fresh)
        sid = self.service.current_id(entry.session_id)
        if sid not in self.service.attached():
            try:
                await self.service.resume(sid)
            except ProtocolError as exc:
                if exc.error is not SESSION_NOT_FOUND:
                    raise
                sid = await self._create_session(source)
                await run_blocking(routes.rotate, self.store, key, sid, label="gateway route")
        self._remember(key, sid)
        self._note_origin(sid, key, source)
        self._watch(sid)
        return sid

    async def new_session(self, key: str, source: Source) -> str:
        """Point the key at a fresh session (``/new``); the old one is closed."""
        old = await self.session_id_for(key)
        if old:
            with contextlib.suppress(ProtocolError):
                self.service.interrupt(old, hard=True)
            await self.service.close(old)
            self._applied.pop(old, None)
        sid = await self._create_session(source)
        entry = await run_blocking(routes.rotate, self.store, key, sid, label="gateway route")
        if entry is None:
            await run_blocking(
                lambda: routes.get_or_create(
                    self.store,
                    key,
                    new_session_id=lambda: sid,
                    platform=str(source.platform).lower(),
                    chat_type=str(source.chat_type),
                    origin=origin_of(source),
                ),
                label="gateway route",
            )
        self._remember(key, sid)
        self._note_origin(sid, key, source)
        self._watch(sid)
        return sid

    async def switch_session(self, key: str, source: Source, session_id: str) -> str:
        """Point the key at an existing session (``/resume``) and attach it."""
        old = await self.session_id_for(key)
        if old and old != session_id:
            await self.service.close(old)
        if session_id not in self.service.attached():
            await self.service.resume(session_id)
        entry = await run_blocking(
            routes.rotate, self.store, key, session_id, label="gateway route"
        )
        if entry is None:
            await self.session_for(key, source)
            await run_blocking(routes.rotate, self.store, key, session_id, label="gateway route")
        self._remember(key, session_id)
        self._note_origin(session_id, key, source)
        self._watch(session_id)
        return session_id

    def _watch(self, session_id: str) -> None:
        """Listen for the session's server requests once."""
        if session_id in self._watched:
            return
        try:
            self._undo.append(
                self.service.on_request(session_id, lambda req: self._on_request(session_id, req))
            )
        except ProtocolError:
            return
        self._watched.add(session_id)

    def _on_request(self, session_id: str, request: ProtocolModel) -> None:
        self.spawn(self._deliver_request(session_id, request), name="server request")

    async def _deliver_request(self, session_id: str, request: ProtocolModel) -> None:
        origin = await self._origin_of(self.service.current_id(session_id))
        if origin is None:
            return
        kind = getattr(request, "kind", "")
        if kind in _SECRET_KINDS:
            await self.reply(origin.source, SECRET_PROMPT, kind="notice")
            return
        if self.on_request is not None:
            await self.on_request(origin.source, request)
            return
        if kind == "approval":
            text = APPROVAL_FALLBACK.format(
                command=getattr(request, "command", ""), why=getattr(request, "description", "")
            )
            await self.reply(origin.source, text, kind="notice")

    async def _on_moved(self, old: str, new: str) -> None:
        """A rotating compaction continued ``old`` in ``new``: keep the routes pointing at it."""
        try:
            entries = await run_blocking(
                routes.routes_for_session, self.store, old, label="gateway route"
            )
            for entry in entries:
                await run_blocking(
                    routes.rotate, self.store, entry.session_key, new, label="gateway route"
                )
                self._remember(entry.session_key, new)
        except Exception:
            log.warning("could not move the routes of %s to %s", old, new, exc_info=True)
        origin = self._origins.pop(old, None)
        if origin is not None:
            self._note_origin(new, origin.key, origin.source)
        self._watch(new)

    # -- models ------------------------------------------------------------------------------------

    async def effective_model(
        self, key: str, source: Source, *, include_once: bool = True
    ) -> ModelChoice | None:
        """Precedence: one-turn override → session override → channel override → default."""
        if include_once and key in self.once_next:
            return self.once_next[key]
        entry = await run_blocking(routes.get, self.store, key, label="gateway route")
        override = entry.model_override if entry is not None else None
        if override and override.get("model"):
            return ModelChoice(
                override["model"],
                override.get("provider") or None,
                override.get("base_url") or None,
                "session",
            )
        try:
            channel = self.channel_model(str(source.platform).lower(), str(source.chat_id))
        except Exception:
            channel = None
        return channel

    async def apply_model(self, session_id: str, choice: ModelChoice | None) -> None:
        """Bind ``choice`` to the session when it differs from what it has now."""
        current = self._applied.get(session_id)
        same = (current is None and choice is None) or (
            current is not None
            and choice is not None
            and current.as_override() == choice.as_override()
        )
        if same:
            return
        await self.switcher.apply(self.service, session_id, choice)
        self._applied[session_id] = choice

    # -- turns -------------------------------------------------------------------------------------

    async def run_admitted(
        self, event: InboundEvent, key: str, *, background: bool = False
    ) -> None:
        """Claim the key (synchronously) and run the turn; a lost race makes it a busy message."""
        claim = self.claims.claim(key)
        if claim is None:
            await self.busy_guard.while_busy(event, key)
            return
        if background:
            self.spawn(self.run_claimed(event, key, claim), name="turn")
            return
        await self.run_claimed(event, key, claim)

    async def run_claimed(self, event: InboundEvent, key: str, claim: Claim) -> None:
        cap = self.cap
        if cap is not None:
            if not cap.try_claim(key):
                self.claims.release(claim)
                await self.reply(event.source, CONCURRENCY_REFUSAL, reply_to=event.message_id)
                return
            claim.capped = True
        claim.task = asyncio.current_task()
        await self._turn(event, key, claim)

    async def _post_admission(self, event: InboundEvent, key: str, session_id: str) -> None:
        if self.hooks is None or event.internal:
            return
        try:
            await self.hooks.observe(
                "post_gateway_admission",
                event=event,
                session_key=key,
                session_id=session_id,
                platform=event.source.platform,
            )
        except Exception:
            log.warning("post_gateway_admission failed (ignored)", exc_info=True)

    async def _model_text(self, event: InboundEvent) -> str:
        if self.enrich is not None:
            return await self.enrich(event)
        if self.sent_index is not None:
            from indus.gateway.core.events import with_reply_context

            event = with_reply_context(event, self.sent_index)
        text = model_text(event)
        if event.media and not text.strip():
            count = len(event.media)
            text = f"[The person sent {count} attachment(s) this chat cannot read yet.]"
        return text

    async def _turn(self, event: InboundEvent, key: str, claim: Claim) -> None:
        source = event.source
        token = f"turn-{claim.generation}"
        marked = False
        ended = False
        sid: str | None = None
        try:
            sid = await self.session_for(key, source)
            claim.session_id = sid
            identity = current_identity()
            if identity is not None:
                bind_identity(replace(identity, session_id=sid))
            await self._post_admission(event, key, sid)
            choice = await self.effective_model(key, source)
            if key in self.once_next:
                del self.once_next[key]
                self._once_turns[claim.generation] = (key, sid, source)
            try:
                await self.apply_model(sid, choice)
            except Exception as exc:
                log.warning("could not switch the model of %s: %s", sid, exc)
            text = await self._model_text(event)
            if not text.strip():
                return
            await self._stage_mirror_notes(sid)
            marked = await run_blocking(
                routes.begin_turn, self.store, key, token, label="active-turn marker"
            )
            try:
                ticket = await self.service.submit(sid, text)
            except ProtocolError as exc:
                if exc.error is SESSION_BUSY and sid in self.service.attached():
                    self.pending.push_front(key, event)  # runs when the running turn ends
                    return
                raise
            claim.turn_generation = ticket.generation
            self._anchor(sid, ticket.generation, event.message_id)
            result = await self.service.wait(sid, ticket.turn_id)
            ended = True
            if result.exit_reason is not ExitReason.COMPLETED and result.exit_reason not in (
                _INTERRUPTED
            ):
                reason = result.failure_reason or result.exit_reason.value.replace("_", " ")
                await self.reply(
                    source,
                    FAILED_TURN.format(reason=reason),
                    reply_to=event.message_id,
                    kind="notice",
                )
        except ProtocolError as exc:
            if exc.error is TEXT_EMPTY:
                return
            if exc.error is SESSION_BUSY:
                text = f"{BUSY_COPY['what']} {BUSY_COPY['todo']}"
            else:
                text = FAILED_TURN.format(reason=exc.message)
            await self.reply(source, text, reply_to=event.message_id, kind="notice")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("the turn for %s failed", key)
            await self.reply(
                source,
                FAILED_TURN.format(reason=type(exc).__name__),
                reply_to=event.message_id,
                kind="notice",
            )
        finally:
            await self._finish(key, claim, token, marked, sid, wait_final=ended)

    def _final_gate(self, session_id: str, generation: int) -> asyncio.Event:
        """Set once the turn-end listener has handed this turn's final to the sender."""
        found = self._finals.get((session_id, generation))
        if found is None:
            found = asyncio.Event()
            self._finals[(session_id, generation)] = found
            while len(self._finals) > _ANCHORS_KEPT:
                self._finals.popitem(last=False)
        return found

    async def _await_final(self, sid: str, generation: int) -> None:
        gate = self._final_gate(sid, generation)
        try:
            await asyncio.wait_for(gate.wait(), FINAL_SETTLE_S)
        except TimeoutError:
            log.warning("the final of %s (turn %d) was not handed over in time", sid, generation)
        finally:
            self._finals.pop((sid, generation), None)

    async def _stage_mirror_notes(self, session_id: str) -> None:
        from indus.gateway.delivery.mirror import stage_pending

        try:
            await stage_pending(self.store, self.service, session_id)
        except Exception:
            log.warning("could not stage delivery notes for %s", session_id, exc_info=True)

    async def _finish(
        self,
        key: str,
        claim: Claim,
        token: str,
        marked: bool,
        sid: str | None,
        *,
        wait_final: bool = False,
    ) -> None:
        once = self._once_turns.pop(claim.generation, None)
        if once is not None:
            once_key, once_sid, once_source = once
            try:
                choice = await self.effective_model(once_key, once_source, include_once=False)
                await self.apply_model(self.service.current_id(once_sid), choice)
            except Exception:
                log.warning("could not restore the model after a one-turn switch", exc_info=True)
        if marked and wait_final and sid is not None and claim.turn_generation is not None:
            await self._await_final(sid, claim.turn_generation)  # R06.5: ledger before marker
        if marked:
            try:
                await run_blocking(
                    routes.end_turn, self.store, key, token, label="active-turn marker"
                )
            except Exception:
                log.warning("could not clear the active-turn marker of %s", key, exc_info=True)
        released = self.claims.release(claim)
        if released and claim.capped and self.cap is not None:
            self.cap.release(key)
        if released:
            self.drain(key)

    def _anchor(self, session_id: str, generation: int, message_id: str | None) -> None:
        if not message_id:
            return
        self._anchors[(session_id, generation)] = message_id
        while len(self._anchors) > _ANCHORS_KEPT:
            self._anchors.popitem(last=False)

    def drain(self, key: str) -> bool:
        """Start the key's next queued message when the key is free; True when one started."""
        if self._closed or self.claims.current(key) is not None:
            return False
        if self.is_running(self.cached_session(key)):
            return False
        event = self.pending.pop(key)
        if event is None:
            return False
        claim = self.claims.claim(key)
        assert claim is not None  # checked above, no await in between
        self.spawn(self.run_claimed(event, key, claim), name="queued message")
        return True

    async def _on_turn_end(self, ended: TurnEnd) -> None:
        """Deliver a completed turn's final text to its chat; then the next queued message."""
        try:
            session_id = self.service.current_id(ended.session_id)
            origin = await self._origin_of(session_id)
            if origin is None:
                return
            anchor = self._anchors.pop((ended.session_id, ended.generation), None)
            if ended.completed and ended.final_text.strip():
                await self.reply(
                    origin.source,
                    ended.final_text,
                    reply_to=anchor,
                    kind="final",
                    session_key=origin.key,
                    message_ref=f"{ended.session_id}:{ended.turn_id}",
                )
        finally:
            self._final_gate(ended.session_id, ended.generation).set()
        self.drain(origin.key)

    # -- busy acknowledgements ---------------------------------------------------------------------

    def take_tip(self, settings: IntakeSettings) -> bool:
        """True once per install: the first busy acknowledgement carries the tip."""
        if self._tip_shown or settings.busy_tip_seen:
            return False
        self._tip_shown = True
        self.spawn(self._save_tip_seen(), name="busy tip flag")
        return True

    async def _save_tip_seen(self) -> None:
        try:
            await run_blocking(
                config_writer.save,
                self.scope,
                {"onboarding": {"seen": {"busy_input": True}}},
                label="settings write",
            )
        except Exception:
            log.warning("could not record that the busy tip was shown", exc_info=True)


def admitted_copy(event: InboundEvent, **changes: Any) -> InboundEvent:
    """A copy marked as already counted by the bot-loop guard (queued follow-ups)."""
    metadata = {**event.metadata, "bot_counted": True}
    return event.model_copy(update={"metadata": metadata, **changes})
