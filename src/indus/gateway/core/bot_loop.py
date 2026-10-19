"""The bot-loop guard and the concurrent-session cap (P15-10).

Bot-loop guard (R06.10): when a platform admits bot senders, two agents in one chat can answer each
other forever. The guard keeps one sliding window per conversation ``(profile, platform, chat_id)``
— never per sender pair, so N bots share one budget. Defaults: on, 20 bot-authored messages per
300 s; the message that would be the 21st trips a 600 s cooldown and clears the window.

``admit()`` counts one bot-authored message and answers ``ok`` / ``tripped`` / ``cooldown`` /
``disabled``; admission calls it once per inbound message (a repeated message id is not counted
again). ``blocked()`` only reads, so every authorization pass may ask it. Idle windows are swept at
most once per window length. Settings are read on every call (an edit applies without a restart);
a missing or unusable value falls back to its default.

Concurrency cap: ``gateway.max_concurrent_sessions`` (a positive integer, unset = no cap) refuses a
new session with a short message; a session already running is never counted twice.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Hashable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from indus.foundation.config.schema import BotLoopGuardSection
from indus.gateway.core._shapes import SourceLike, text_of

__all__ = [
    "CONCURRENCY_REFUSAL",
    "AdmitState",
    "BotLoopGuard",
    "BotLoopSettings",
    "ConcurrencyCap",
    "cap_from_value",
    "conversation_of",
    "settings_reader",
]

log = logging.getLogger(__name__)

type AdmitState = Literal["ok", "tripped", "cooldown", "disabled"]
type Conversation = tuple[str, str, str]

CONCURRENCY_REFUSAL = (
    "I'm already handling as many conversations as I'm allowed to at once. "
    "Please try again in a little while."
)
_RECENT_IDS = 64
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def _bool(raw: object, default: bool) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower() if raw is not None else ""
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return default


def _positive(raw: object, default: float) -> float:
    if isinstance(raw, bool) or raw is None:
        return default
    try:
        value = float(raw)  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) and value > 0 else default


def _positive_int(raw: object, default: int) -> int:
    value = _positive(raw, 0.0)
    return int(value) if value >= 1 and value == int(value) else default


# Defaults come from the one config defaults table (R06.25), never from this module.
_SCHEMA = BotLoopGuardSection()


@dataclass(frozen=True)
class BotLoopSettings:
    enabled: bool = _SCHEMA.enabled
    max_messages: int = _SCHEMA.max_messages
    window_seconds: float = _SCHEMA.window_seconds
    cooldown_seconds: float = _SCHEMA.cooldown_seconds

    @classmethod
    def from_mapping(cls, raw: object) -> BotLoopSettings:
        """Settings from the ``gateway.bot_loop_guard`` block; unusable values keep defaults."""
        if not isinstance(raw, Mapping):
            return cls()
        block: Mapping[str, Any] = raw  # pyright: ignore[reportUnknownVariableType]
        base = cls()
        return cls(
            enabled=_bool(block.get("enabled"), base.enabled),
            max_messages=_positive_int(block.get("max_messages"), base.max_messages),
            window_seconds=_positive(block.get("window_seconds"), base.window_seconds),
            cooldown_seconds=_positive(block.get("cooldown_seconds"), base.cooldown_seconds),
        )


def settings_reader(config_get: Callable[[str], object]) -> Callable[[], BotLoopSettings]:
    """A settings callable that reads ``gateway.bot_loop_guard`` afresh on every call.

    ``config_get`` is a dotted-key lookup such as ``ConfigView.get``; any failure gives defaults.
    """

    def read() -> BotLoopSettings:
        try:
            return BotLoopSettings.from_mapping(config_get("gateway.bot_loop_guard"))
        except Exception:
            return BotLoopSettings()

    return read


def conversation_of(source: SourceLike, profile: str = "") -> Conversation:
    """The guard's budget key: one per chat, whoever the bots are."""
    return (profile, text_of(source.platform).lower(), text_of(source.chat_id))


class BotLoopGuard:
    """Per-conversation sliding window of bot-authored messages, with a cooldown. Thread-safe."""

    def __init__(
        self,
        settings: Callable[[], BotLoopSettings] = BotLoopSettings,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._settings = settings
        self._monotonic = monotonic if monotonic is not None else time.monotonic
        self._lock = threading.Lock()
        self._events: dict[Hashable, deque[float]] = {}
        self._cooldown_until: dict[Hashable, float] = {}
        self._recent: dict[Hashable, OrderedDict[str, None]] = {}
        self._last_sweep: float | None = None

    def _read_settings(self) -> BotLoopSettings:
        try:
            return self._settings()
        except Exception:
            return BotLoopSettings()

    @property
    def tracked(self) -> int:
        with self._lock:
            return len(self._events)

    def blocked(self, conversation: Hashable) -> bool:
        """True while ``conversation`` cools down. Read-only: never counts a message."""
        if not self._read_settings().enabled:
            return False
        with self._lock:
            return self._cooldown_until.get(conversation, float("-inf")) > self._monotonic()

    def admit(self, conversation: Hashable, message_id: str | None = None) -> AdmitState:
        """Count one bot-authored message for ``conversation``.

        ``tripped`` means this message started the cooldown; ``ok`` and ``disabled`` let it in.
        """
        settings = self._read_settings()
        if not settings.enabled:
            return "disabled"
        now = self._monotonic()
        with self._lock:
            self._sweep(now, settings)
            if self._cooldown_until.get(conversation, float("-inf")) > now:
                return "cooldown"
            if message_id:
                seen = self._recent.setdefault(conversation, OrderedDict())
                if message_id in seen:
                    return "ok"
                seen[message_id] = None
                while len(seen) > _RECENT_IDS:
                    seen.popitem(last=False)
            window = self._events.setdefault(conversation, deque())
            cutoff = now - settings.window_seconds
            while window and window[0] <= cutoff:
                window.popleft()
            if len(window) >= settings.max_messages:
                self._cooldown_until[conversation] = now + settings.cooldown_seconds
                window.clear()
                log.warning(
                    "bot-loop guard: too many bot messages in %s; ignoring bots there for %g s "
                    "(gateway.bot_loop_guard)",
                    conversation,
                    settings.cooldown_seconds,
                )
                return "tripped"
            window.append(now)
            return "ok"

    def _sweep(self, now: float, settings: BotLoopSettings) -> None:
        """Forget idle conversations, at most once per window, so memory stays bounded."""
        if self._last_sweep is not None and now - self._last_sweep < settings.window_seconds:
            return
        self._last_sweep = now
        idle = now - settings.window_seconds
        for key in [k for k, w in self._events.items() if not w or w[-1] <= idle]:
            del self._events[key]
            self._recent.pop(key, None)
        for key in [k for k, until in self._cooldown_until.items() if until <= now]:
            del self._cooldown_until[key]


def cap_from_value(raw: object) -> int | None:
    """``gateway.max_concurrent_sessions`` as a positive int, else None (no cap)."""
    value = _positive_int(raw, 0)
    return value or None


class ConcurrencyCap:
    """At most ``limit()`` sessions run at once; ``limit`` is read on every claim."""

    def __init__(self, limit: Callable[[], int | None]) -> None:
        self._limit = limit
        self._active: set[str] = set()
        self._lock = threading.Lock()

    @property
    def active(self) -> int:
        with self._lock:
            return len(self._active)

    def try_claim(self, session_key: str) -> bool:
        """Claim a slot for ``session_key`` (true when it already runs)."""
        try:
            limit = cap_from_value(self._limit())
        except Exception:
            limit = None
        with self._lock:
            if session_key in self._active:
                return True
            if limit is not None and len(self._active) >= limit:
                return False
            self._active.add(session_key)
            return True

    def release(self, session_key: str) -> None:
        with self._lock:
            self._active.discard(session_key)
