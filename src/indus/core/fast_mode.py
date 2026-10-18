# pyright: strict
"""Fast mode: ask the provider for its priority tier, always or for a window per turn (P08-15).

``/fast normal|fast|auto|status [--global]`` picks the session's mode; ``agent.fast_mode`` is the
profile default (``normal``):

- ``normal`` — requests go out as they are;
- ``fast`` — every request asks for the priority tier;
- ``auto`` — each user turn opens a window of ``agent.fast_window_s`` (60 s) on the scope clock;
  requests sent inside the window ask for the tier, later requests of the same turn do not.

What "the priority tier" means is the provider profile's declaration (``fast_body`` request
fields, ``fast_headers``, ``fast_models``): it is applied only when the profile declares it, the
model is one of ``fast_models`` (exact ids, any case; none listed: every model) and the request goes
to the profile's own host, so a proxy or custom endpoint never receives the fields. Otherwise the
request is sent unchanged and one notice per route is logged (``/fast`` prints its own).

``FastMode.layer`` is applied by the agent's caller next to ``finish_request``, on the route
state read once for that request; a field the request already sets is never overwritten.
``served`` then makes sure usage carries the tier the call was asked for when the answer itself
does not say (``fast_requested`` marks it), so pricing never applies standard rates to a fast
request.

The session's choice lives in ``FAST_MODES`` keyed by (profile home, session id), like the
approval switches, so ``/fast`` on any surface changes the running agent at once.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal, cast

from indus.foundation.scope import Scope
from indus.providers.route import RuntimeRoute
from indus.providers.streaming import CallOutcome, CallResult
from indus.providers.types import ChatRequest

__all__ = [
    "DEFAULT_WINDOW_S",
    "FAST_MODES",
    "MODES",
    "FastMode",
    "FastTier",
    "Mode",
    "ModeTable",
    "parse_mode",
    "served",
    "session_key",
    "tier_for",
    "unsupported_reason",
    "window_seconds",
]

log = logging.getLogger(__name__)

type Mode = Literal["normal", "fast", "auto"]
type SessionKey = tuple[str, str]

MODES: tuple[Mode, ...] = ("normal", "fast", "auto")
DEFAULT_WINDOW_S = 60.0
_WORDS: Mapping[str, Mode] = {
    "normal": "normal",
    "off": "normal",
    "default": "normal",
    "fast": "fast",
    "on": "fast",
    "priority": "fast",
    "auto": "auto",
}


def parse_mode(word: object) -> Mode | None:
    """The mode a user or config word names; None for an unknown word."""
    if isinstance(word, bool):
        return "fast" if word else "normal"
    if not isinstance(word, str):
        return None
    return _WORDS.get(word.strip().lower())


def window_seconds(raw: object) -> float:
    """``agent.fast_window_s`` as seconds; missing, unreadable or not positive → 60."""
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        return DEFAULT_WINDOW_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_WINDOW_S
    return value if 0 < value < float("inf") else DEFAULT_WINDOW_S


# -- what a route supports ------------------------------------------------------------------------


@dataclass(frozen=True)
class FastTier:
    """The request fields and headers a route takes for its priority tier."""

    body: Mapping[str, Any]
    headers: Mapping[str, str]


def unsupported_reason(route: RuntimeRoute) -> str | None:
    """Why ``route`` cannot take fast mode, or None when it can."""
    profile = route.profile
    if profile is None or not profile.fast_body:
        return f"{route.provider} does not offer a priority tier"
    if profile.family != "vendor" or not profile.hostname or route.host != profile.hostname:
        return f"the priority tier is only sent to {profile.hostname or route.provider} itself"
    models = profile.fast_models
    if models and route.model.lower() not in {m.lower() for m in models}:
        return f"{route.model} has no priority tier on {route.provider}"
    return None


def tier_for(route: RuntimeRoute) -> FastTier | None:
    """The route's priority tier, or None when it has none (see ``unsupported_reason``)."""
    if unsupported_reason(route) is not None:
        return None
    profile = route.profile
    assert profile is not None
    return FastTier(dict(profile.fast_body), dict(profile.fast_headers))


def _merged_headers(route: RuntimeRoute, extra: Mapping[str, str]) -> dict[str, str]:
    """Route headers plus ``extra``; a list-valued header (``anthropic-beta``) gains the new
    entries instead of losing its old ones."""
    headers = dict(route.headers)
    lowered = {k.lower(): k for k in headers}
    for key, value in extra.items():
        existing = lowered.get(key.lower())
        if existing is None:
            headers[key] = value
            continue
        items = [v.strip() for v in headers[existing].split(",") if v.strip()]
        for part in (p.strip() for p in value.split(",")):
            if part and part not in items:
                items.append(part)
        headers[existing] = ",".join(items)
    return headers


# -- per-session choice ---------------------------------------------------------------------------


class ModeTable:
    """Per-session modes chosen with ``/fast`` (absent: the config default). Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._modes: dict[SessionKey, Mode] = {}

    def get(self, key: SessionKey) -> Mode | None:
        with self._lock:
            return self._modes.get(key)

    def set(self, key: SessionKey, mode: Mode) -> None:
        with self._lock:
            self._modes[key] = mode

    def clear(self, key: SessionKey) -> None:
        with self._lock:
            self._modes.pop(key, None)

    def move(self, old: SessionKey, new: SessionKey) -> None:
        with self._lock:
            mode = self._modes.pop(old, None)
            if mode is not None:
                self._modes[new] = mode


FAST_MODES = ModeTable()


def session_key(scope: Scope, session_id: str | None) -> SessionKey:
    return (str(scope.home), session_id or "default")


# -- the session's fast mode ----------------------------------------------------------------------


def _config(scope: Scope, key: str) -> object:
    config: Any = scope.config
    if config is None:
        return None
    try:
        return config.get(key)
    except Exception:
        log.debug("config %s unreadable", key, exc_info=True)
        return None


class FastMode:
    """One session's fast mode (see the module notes)."""

    def __init__(
        self,
        scope: Scope,
        session_id: Callable[[], str],
        *,
        table: ModeTable | None = None,
    ) -> None:
        self.scope = scope
        self._session_id = session_id
        self.table = table if table is not None else FAST_MODES
        self._until = 0.0
        self._noted: set[tuple[str, str, str]] = set()

    @property
    def key(self) -> SessionKey:
        return session_key(self.scope, self._session_id())

    @property
    def mode(self) -> Mode:
        chosen = self.table.get(self.key)
        if chosen is not None:
            return chosen
        return parse_mode(_config(self.scope, "agent.fast_mode")) or "normal"

    def set_mode(self, mode: Mode) -> None:
        self.table.set(self.key, mode)
        self._until = 0.0
        if mode != "normal":
            self._noted.clear()

    @property
    def window_s(self) -> float:
        return window_seconds(_config(self.scope, "agent.fast_window_s"))

    def begin_turn(self) -> None:
        """A user turn starts: ``auto`` opens a fresh window, other modes need none."""
        self._until = 0.0
        if self.mode == "auto":
            self._until = self.scope.clock.monotonic() + self.window_s

    def remaining(self) -> float:
        """Seconds left in the open window (``auto``), 0 when none is open."""
        return max(0.0, self._until - self.scope.clock.monotonic())

    def active(self) -> bool:
        mode = self.mode
        if mode == "fast":
            return True
        return mode == "auto" and self.scope.clock.monotonic() < self._until

    def layer(
        self, request: ChatRequest, route: RuntimeRoute
    ) -> tuple[ChatRequest, RuntimeRoute, FastTier | None]:
        """``request`` and ``route`` with the priority tier on them while fast mode is active and
        the route supports it; otherwise both unchanged and no tier."""
        if not self.active():
            return request, route, None
        tier = tier_for(route)
        if tier is None:
            note = (route.provider, route.base_url, route.model)
            if note not in self._noted:
                self._noted.add(note)
                log.info("fast mode: %s; requests go out unchanged", unsupported_reason(route))
            return request, route, None
        body = dict(request.extra_body)
        for name, value in tier.body.items():
            body.setdefault(name, value)
        request = replace(request, extra_body=body)
        if tier.headers:
            route = replace(route, headers=_merged_headers(route, tier.headers))
        return request, route, tier


def served(outcome: CallOutcome, tier: FastTier | None) -> CallOutcome:
    """``outcome`` with the asked-for tier in its provider data when the answer did not report
    one (``service_tier`` at the top, other fields inside the raw usage)."""
    if tier is None or not isinstance(outcome, CallResult):
        return outcome
    response = outcome.response
    data = dict(response.provider_data)
    raw_usage = data.get("usage")
    usage: dict[str, Any] = (
        dict(cast(Mapping[str, Any], raw_usage)) if isinstance(raw_usage, Mapping) else {}
    )
    added = False
    for name, value in tier.body.items():
        if name == "service_tier":
            if "service_tier" not in data:
                data["service_tier"] = value
                added = True
        elif name not in usage:
            usage[name] = value
            added = True
    if not added:
        return outcome
    if usage:
        data["usage"] = usage
    data["fast_requested"] = True
    return replace(outcome, response=replace(response, provider_data=data))
