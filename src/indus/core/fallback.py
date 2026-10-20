"""The fallback chain, turn-scoped (P08-13, R01.5, R01.10, R01.11).

``fallback_providers`` lists ``{provider, model}`` routes (a custom endpoint may add
``base_url``); an entry without both provider and model is ignored. ``FallbackChain`` belongs to
one session (subagents inherit the provider, never the chain):

``activate(verdict, reason)`` runs after P03's retries and the credential pool (P08-12) gave up,
when ``is_trigger`` says another route could help. It may first decide to *wait* on the primary
(a rate limit that reopens sooner than ``fallback.min_switch_reset_s``; once per turn). Leaving the
primary on a rate-limit or billing failure arms a primary cooldown (the provider's reset, else
``min(60 × 2^k, 14,400)`` s). Then it walks the chain from the current index, skipping entries that
are known unavailable, rejected for the account, non-chat, the backend that just failed, networked
while offline, or whose pool would fail; resolves the next one with the P03 rules (its key only to
its own exact host, the API mode read from its own base URL) and applies it through
``RouteBinding.apply`` — client, API mode, reasoning, extra body, window (the compressor follows
through the agent's route dependent), cache policy and the prompt's identity lines, all or
nothing. The binding's generation moves, so a retry built for the old route is never sent (R01.5).

``restore()`` runs at every turn start (the prologue): the pools reread their rows, the chain is
read again from the config, and — when a fallback is active — the primary snapshot is put back
unless it is cooling down, rejected/non-chat, or its pool would fail. The chain index returns to 0
whenever no fallback is active at turn start, also after an activation that found nothing (a
stranded index must never disable fallback for good, R01.10), and after a restore. "Primary
restored" is announced only when a fallback really ran; a ``/model`` switch made meanwhile cancels
the pending restore.

``fall_back`` / ``restore_primary`` are the loop's and the prologue's entry points; they emit the
notices.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from indus.protocol.events import Notice, NoticeLevel
from indus.providers.classify import ClassifiedError
from indus.providers.hosts import api_mode_from_url, is_loopback
from indus.providers.pool import PoolSet, is_entitlement
from indus.providers.route import RuntimeRoute
from indus.providers.route import resolve as resolve_route
from indus.providers.settings import as_mapping, config_of, text

if TYPE_CHECKING:
    from indus.core.route import RouteBinding, RouteState
    from indus.core.turn_state import TurnState
    from indus.foundation.clock import Clock
    from indus.foundation.scope import Scope

__all__ = [
    "CHAIN_KEY",
    "MIN_SWITCH_KEY",
    "ChainEntry",
    "Decision",
    "FallbackChain",
    "fall_back",
    "is_non_chat",
    "is_trigger",
    "read_chain",
    "restore_primary",
]

log = logging.getLogger(__name__)

CHAIN_KEY = "fallback_providers"
MIN_SWITCH_KEY = "min_switch_reset_s"
COOLDOWN_BASE_S = 60.0
COOLDOWN_MAX_S = 14_400.0
WALKED_GATE_S = 5.0
"""After a chain walk on a non-rate-limit failure the primary waits this long before a restore."""

RATE_LIMIT_REASONS = frozenset({"rate_limit", "billing", "upstream_rate_limit"})
TRANSIENT_REASONS = frozenset(
    {"overloaded", "server_error", "timeout", "connection", "incomplete_response", "unknown"}
)
NEVER_REASONS = frozenset({"context_overflow", "payload_too_large", "stream_unsupported"})
"""Compression (P08-05/06) and the stream switch (P03) own these."""
ANSWER_REASONS = frozenset({"content_filter", "empty_response"})
"""Triggers read from an answer rather than from an error."""

NON_CHAT_WORDS = (
    "embedding",
    "embed-",
    "whisper",
    "tts-",
    "text-to-speech",
    "dall-e",
    "moderation",
    "rerank",
    "transcribe",
)

type Resolver = Callable[..., RuntimeRoute]


def is_trigger(verdict: ClassifiedError) -> bool:
    """Could another route help with this final verdict?"""
    if verdict.reason in NEVER_REASONS:
        return False
    return verdict.should_fallback or verdict.reason in TRANSIENT_REASONS


def is_non_chat(model: str) -> bool:
    lowered = model.lower()
    return any(word in lowered for word in NON_CHAT_WORDS)


@dataclass(frozen=True)
class ChainEntry:
    provider: str
    model: str
    base_url: str = ""

    def key(self) -> tuple[str, str, str]:
        return (self.provider, self.model, self.base_url)

    def label(self) -> str:
        return f"{self.model} via {self.provider}"


def read_chain(config: object) -> list[ChainEntry]:
    """The usable entries of ``fallback_providers`` (entries lacking provider or model are
    ignored with a debug line)."""
    raw: object = None
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            raw = getter(CHAIN_KEY)
        except Exception:
            raw = None
    if not isinstance(raw, list):
        return []
    out: list[ChainEntry] = []
    for item in cast(list[object], raw):
        entry = as_mapping(item)
        provider, model = text(entry.get("provider")).lower(), text(entry.get("model"))
        if not provider or not model:
            log.debug("fallback entry %r ignored: it needs both provider and model", item)
            continue
        out.append(ChainEntry(provider, model, text(entry.get("base_url")).rstrip("/")))
    return out


@dataclass(frozen=True)
class Decision:
    """``switched``: the binding now holds a fallback route; ``wait``: wait ``seconds`` and retry
    the primary; ``none``: nothing could be done."""

    action: Literal["switched", "wait", "none"]
    seconds: float = 0.0
    notice: str = ""


NOTHING = Decision("none")


def _reason_text(reason: str) -> str:
    words = {
        "rate_limit": "rate limited",
        "upstream_rate_limit": "rate limited upstream",
        "billing": "billing or quota",
        "auth": "the key was refused",
        "auth_permanent": "the account was refused",
        "model_not_found": "model not found",
        "content_policy": "refused by its content filter",
        "content_filter": "refused by its content filter",
        "empty_response": "no usable answer",
        "incomplete_response": "incomplete answers",
        "overloaded": "overloaded",
        "server_error": "server error",
        "timeout": "timed out",
        "connection": "unreachable",
    }
    return words.get(reason, reason.replace("_", " "))


class FallbackChain:
    """One session's chain state (see the module notes)."""

    def __init__(
        self,
        scope: Scope,
        binding: RouteBinding,
        *,
        clock: Clock,
        pools: PoolSet | None = None,
        resolve: Resolver | None = None,
        offline: bool = False,
        on_switch: Callable[[RouteState], object] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.scope = scope
        self.binding = binding
        self.clock = clock
        self.pools = pools
        self._resolve = resolve or resolve_route
        self.offline = offline
        self.on_switch = on_switch
        self._monotonic = monotonic or clock.monotonic
        self.chain: list[ChainEntry] = read_chain(config_of(scope))
        self.index = 0
        self.active = False
        self.primary: RouteState | None = None
        self.current: RouteState | None = None
        self.cooldown_until = 0.0
        self.level = 0
        self.stayed = False
        self.unavailable: set[tuple[str, str, str]] = set()
        self.rejected: set[tuple[str, str]] = set()

    # -- config -----------------------------------------------------------------------------------

    def _min_switch(self) -> float:
        try:
            section = as_mapping(config_of(self.scope).get("fallback"))
        except Exception:
            return 0.0
        raw = section.get(MIN_SWITCH_KEY)
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            return 0.0
        return max(0.0, float(raw))

    # -- questions --------------------------------------------------------------------------------

    def wants_eager(self, verdict: ClassifiedError) -> bool:
        """Skip the same-route retries for this verdict (a usable entry is waiting)."""
        return (
            verdict.reason == "upstream_rate_limit"
            and verdict.should_fallback
            and self.has_candidate()
        )

    def has_candidate(self) -> bool:
        state = self.binding.state
        return any(self._skip(e, state) is None for e in self.chain[self.index :])

    def _skip(self, entry: ChainEntry, state: RouteState) -> str | None:
        if entry.key() in self.unavailable:
            return "unavailable earlier in this session"
        if (entry.provider, entry.model) in self.rejected:
            return "the account is not entitled to it"
        if is_non_chat(entry.model):
            return "not a chat model"
        same_url = not entry.base_url or entry.base_url == state.base_url.rstrip("/")
        if entry.provider == state.provider and entry.model == state.model and same_url:
            return "the route that just failed"
        if self.offline and not (entry.base_url and _local(entry.base_url)):
            return "needs the network while offline"
        return None

    def primary_blocked(self) -> str | None:
        """Why the primary may not come back now (None: it may)."""
        primary = self.primary
        if primary is None:
            return "no primary recorded"
        if (primary.provider, primary.model) in self.rejected:
            return "the account is not entitled to it"
        if is_non_chat(primary.model):
            return "not a chat model"
        if self.cooldown_until > self._monotonic():
            return "still cooling down"
        if self.pools is not None:
            pool = self.pools.for_route(primary.route)
            if pool is not None and pool.would_fail(primary.model):
                return "every credential is cooling down"
        return None

    # -- activation -------------------------------------------------------------------------------

    def _arm(self, verdict: ClassifiedError | None) -> float | None:
        if verdict is None or verdict.reason not in RATE_LIMIT_REASONS:
            return None
        if self.active:  # a fallback failed: the primary was not the failing source
            return None
        if verdict.reset_at is not None:
            # The provider named its reset: follow it, also when it has already passed (a slow
            # retry path must not turn a short reset into a ladder step).
            seconds = max((verdict.reset_at - self.clock.now()).total_seconds(), 0.0)
        else:
            ladder = COOLDOWN_BASE_S * float(1 << min(self.level, 32))
            seconds = min(ladder, COOLDOWN_MAX_S)
        self.level += 1
        self.cooldown_until = max(self.cooldown_until, self._monotonic() + seconds)
        log.info("primary route cooling down for %.0f s (level %d)", seconds, self.level)
        return seconds

    def _stay(self, verdict: ClassifiedError | None) -> Decision | None:
        if verdict is None or verdict.reason not in RATE_LIMIT_REASONS:
            return None
        if self.active or self.stayed or verdict.reset_at is None:
            return None
        threshold = self._min_switch()
        if threshold <= 0:
            return None
        left = (verdict.reset_at - self.clock.now()).total_seconds()
        if not 0 < left < threshold:
            return None
        self.stayed = True
        state = self.binding.state
        return Decision(
            "wait",
            seconds=left,
            notice=f"{state.model} via {state.provider} is rate limited for {left:.0f} s more; "
            "waiting instead of switching models.",
        )

    def _route_for(self, entry: ChainEntry) -> RuntimeRoute:
        mode = api_mode_from_url(entry.base_url) if entry.base_url else None
        return self._resolve(
            self.scope,
            provider=entry.provider,
            model=entry.model,
            base_url=entry.base_url or None,
            api_mode=mode,
        )

    async def activate(self, verdict: ClassifiedError | None, reason: str) -> Decision:
        """Move to the next usable entry (see the module notes)."""
        state = self.binding.state
        if verdict is not None and is_entitlement(verdict):
            self.rejected.add((state.provider, state.model))
        if not self.chain:
            return NOTHING
        stay = self._stay(verdict)
        if stay is not None:
            return stay
        cooldown = self._arm(verdict)
        while self.index < len(self.chain):
            entry = self.chain[self.index]
            self.index += 1
            why = self._skip(entry, state)
            if why is not None:
                log.info("fallback %s skipped: %s", entry.label(), why)
                continue
            try:
                route = self._route_for(entry)
            except Exception as exc:
                log.warning("fallback %s is not usable: %s", entry.label(), exc)
                self.unavailable.add(entry.key())
                continue
            if self.pools is not None:
                pool = self.pools.for_route(route)
                if pool is not None and pool.would_fail(route.model):
                    log.info("fallback %s skipped: every credential is cooling down", entry.label())
                    continue
            try:
                after = await self.binding.apply(route)
            except Exception as exc:
                log.warning("fallback %s could not be bound: %s", entry.label(), exc)
                self.unavailable.add(entry.key())
                continue
            if not self.active:
                self.primary = state
            self.active = True
            self.current = after
            self._switched(after)
            notice = (
                f"Model fallback: {state.model} via {state.provider} failed "
                f"({_reason_text(reason)}); using {after.model} via {after.provider} for the "
                "rest of this turn."
            )
            if cooldown is not None and cooldown >= 1:
                notice += f" The main model is retried after about {cooldown:.0f} s."
            elif cooldown is not None:
                notice += " The main model is retried at the next turn."
            log.warning("%s", notice)
            return Decision("switched", notice=notice)
        if verdict is None or verdict.reason not in RATE_LIMIT_REASONS:
            self.cooldown_until = max(self.cooldown_until, self._monotonic() + WALKED_GATE_S)
        log.warning("fallback chain exhausted (%s); no route left to try", _reason_text(reason))
        return NOTHING

    def _switched(self, state: RouteState) -> None:
        if self.on_switch is None:
            return
        try:
            self.on_switch(state)
        except Exception:
            log.debug("route switch listener failed", exc_info=True)

    # -- turn scope -------------------------------------------------------------------------------

    async def restore(self) -> str | None:
        """Turn start (see the module notes); the "primary restored" notice, or None."""
        if self.pools is not None:
            self.pools.new_turn()
        self.chain = read_chain(config_of(self.scope))
        self.stayed = False
        if not self.active:
            self.index = 0
            return None
        if self.current is not None and self.binding.state is not self.current:
            log.info("the route was changed by hand during a fallback; no restore")
            self._settle()
            return None
        blocked = self.primary_blocked()
        if blocked is not None:
            log.info("staying on the fallback route: the main route is %s", blocked)
            return None
        primary = self.primary
        assert primary is not None
        fallen = self.binding.state
        try:
            restored = await self.binding.rollback(to=primary)
        except Exception as exc:
            log.warning("could not restore the main route (%s); staying on the fallback", exc)
            return None
        self._settle()
        state = restored or self.binding.state
        self._switched(state)
        notice = (
            f"Main model restored: {state.model} via {state.provider}; the fallback "
            f"{fallen.model} via {fallen.provider} is no longer active."
        )
        log.info("%s", notice)
        return notice

    def _settle(self) -> None:
        self.active = False
        self.primary = None
        self.current = None
        self.index = 0


def _local(url: str) -> bool:
    try:
        return is_loopback(url)
    except Exception:
        return False


# -- loop and prologue entry points ---------------------------------------------------------------


def _chain(state: TurnState) -> FallbackChain | None:
    found: Any = getattr(state.runtime, "fallback", None)
    return found if isinstance(found, FallbackChain) else None


async def fall_back(
    state: TurnState, verdict: ClassifiedError | None, reason: str | None = None
) -> Decision:
    """The loop's entry point: try the chain for a failure (``verdict``) or a bad answer
    (``reason`` in ``content_filter`` / ``empty_response``)."""
    chain = _chain(state)
    if chain is None or state.control.interrupted or state.grace:
        return NOTHING
    if verdict is not None and not is_trigger(verdict):
        return NOTHING
    why = reason or (verdict.reason if verdict is not None else "")
    if verdict is None and why not in ANSWER_REASONS:
        return NOTHING
    decision = await chain.activate(verdict, why)
    if decision.notice:
        level = NoticeLevel.WARNING if decision.action == "switched" else NoticeLevel.INFO
        state.emit(Notice, level=level, text=decision.notice, key="fallback")
    return decision


async def restore_primary(state: TurnState) -> None:
    """The prologue's entry point: restore the main route when a fallback ran last turn."""
    chain = _chain(state)
    if chain is None:
        return
    notice = await chain.restore()
    if notice:
        state.emit(Notice, level=NoticeLevel.INFO, text=notice, key="fallback")
