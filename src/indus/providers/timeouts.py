"""How long a model call may stay silent, and the socket read timeout that goes with it.

``TimeoutPolicy`` is a frozen value built once (``from_scope`` reads config and env); its
``for_call`` method is a pure function of the route, the estimated prompt size and the call
shape. The stream driver (P03-14) uses the result for stale detection and its breaker; the HTTP
client factory (P03-08) uses ``read``.

Implicit patience grows with the prompt (big prompts think longer before the first token), has a
floor for reasoning models, is long but finite for local servers, and is capped by a run budget.
An explicit operator value is returned unchanged: it may shorten or lengthen patience (R01.5).
"""

from __future__ import annotations

import ipaddress
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from indus.providers.hosts import host_of, is_private_network

if TYPE_CHECKING:
    from indus.foundation.scope import Scope

log = logging.getLogger(__name__)

# Implicit stale timeouts, seconds.
CLOUD_STREAM_STALE = 180.0
CLOUD_STREAM_TIERS: tuple[tuple[int, float], ...] = ((100_000, 300.0), (50_000, 240.0))
REASONING_STREAM_FLOOR = 300.0
LOCAL_STREAM_STALE = 900.0
CLOUD_CALL_STALE = 90.0
CLOUD_CALL_TIERS: tuple[tuple[int, float], ...] = ((100_000, 240.0), (50_000, 150.0))
BASE_READ_TIMEOUT = 120.0
BREAKER_THRESHOLD = 5
STREAM_RETRIES = 2
RUN_BUDGET_FLOOR = 60.0
RUN_BUDGET_SHARE = 0.5

ENV_STREAM_STALE = "INDUS_STREAM_STALE_TIMEOUT"
ENV_CALL_STALE = "INDUS_API_CALL_STALE_TIMEOUT"
ENV_LOCAL_STREAM_STALE = "INDUS_LOCAL_STREAM_STALE_TIMEOUT"
ENV_BREAKER = "INDUS_STREAM_STALE_GIVEUP"
ENV_STREAM_RETRIES = "INDUS_STREAM_RETRIES"
CONFIG_STREAM_STALE = "agent.stream_stale_timeout"
CONFIG_CALL_STALE = "agent.api_call_stale_timeout"
CONFIG_LOCAL_STREAM_STALE = "agent.local_stream_stale_timeout"

_LOCAL_SUFFIXES = (".local", ".internal", ".lan", ".home.arpa")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


class _HasBaseUrl(Protocol):
    @property
    def base_url(self) -> str: ...


@dataclass(frozen=True)
class CallTimeouts:
    """The numbers for one call. ``stale`` None means stale detection is off."""

    stale: float | None
    read: float
    explicit: bool
    local: bool
    breaker_threshold: int
    stream_retries: int


@dataclass(frozen=True)
class TimeoutPolicy:
    """Operator settings; ``None`` means "not configured" (the implicit rules apply)."""

    explicit_stream_stale: float | None = None
    explicit_call_stale: float | None = None
    local_stream_stale: float = LOCAL_STREAM_STALE
    run_budget_remaining: float | None = None
    breaker_threshold: int = BREAKER_THRESHOLD
    read_timeout: float = BASE_READ_TIMEOUT
    stream_retries: int = STREAM_RETRIES

    @classmethod
    def from_scope(
        cls, scope: Scope, *, run_budget_remaining: float | None = None
    ) -> TimeoutPolicy:
        """Read the explicit values from the scope's env view first, then its config."""
        env: Mapping[str, str] = scope.env
        config = scope.config

        def config_value(path: str) -> object:
            return None if config is None else config.get(path)

        stream = _seconds(env.get(ENV_STREAM_STALE), ENV_STREAM_STALE)
        if stream is None:
            stream = _seconds(config_value(CONFIG_STREAM_STALE), CONFIG_STREAM_STALE)
        call = _seconds(env.get(ENV_CALL_STALE), ENV_CALL_STALE)
        if call is None:
            call = _seconds(config_value(CONFIG_CALL_STALE), CONFIG_CALL_STALE)
        local = _seconds(env.get(ENV_LOCAL_STREAM_STALE), ENV_LOCAL_STREAM_STALE)
        if local is None:
            local = _seconds(config_value(CONFIG_LOCAL_STREAM_STALE), CONFIG_LOCAL_STREAM_STALE)
        breaker = _count(env.get(ENV_BREAKER), ENV_BREAKER)
        retries = _count(env.get(ENV_STREAM_RETRIES), ENV_STREAM_RETRIES)
        return cls(
            explicit_stream_stale=stream,
            explicit_call_stale=call,
            local_stream_stale=LOCAL_STREAM_STALE if local is None else local,
            run_budget_remaining=run_budget_remaining,
            breaker_threshold=BREAKER_THRESHOLD if breaker is None else breaker,
            stream_retries=STREAM_RETRIES if retries is None else retries,
        )

    def for_call(
        self,
        route: _HasBaseUrl,
        est_prompt_tokens: int,
        *,
        streaming: bool,
        reasoning: bool = False,
    ) -> CallTimeouts:
        local = is_local_endpoint(route.base_url)
        explicit_value = self.explicit_stream_stale if streaming else self.explicit_call_stale
        if explicit_value is not None:
            stale: float | None = explicit_value if explicit_value > 0 else None
            return self._done(stale, explicit=True, local=local)
        if streaming:
            if local:
                stale = self.local_stream_stale
            else:
                stale = _tiered(CLOUD_STREAM_STALE, CLOUD_STREAM_TIERS, est_prompt_tokens)
                if reasoning:
                    stale = max(stale, REASONING_STREAM_FLOOR)
        elif local:
            stale = None
        else:
            stale = _tiered(CLOUD_CALL_STALE, CLOUD_CALL_TIERS, est_prompt_tokens)
        if stale is not None and self.run_budget_remaining is not None:
            cap = max(RUN_BUDGET_FLOOR, self.run_budget_remaining * RUN_BUDGET_SHARE)
            stale = min(stale, cap)
        return self._done(stale, explicit=False, local=local)

    def _done(self, stale: float | None, *, explicit: bool, local: bool) -> CallTimeouts:
        read = self.read_timeout if stale is None else max(self.read_timeout, stale)
        return CallTimeouts(
            stale=stale,
            read=read,
            explicit=explicit,
            local=local,
            breaker_threshold=max(0, self.breaker_threshold),
            stream_retries=max(0, self.stream_retries),
        )


def _tiered(base: float, tiers: tuple[tuple[int, float], ...], tokens: int) -> float:
    for above, floor in tiers:
        if tokens > above:
            return max(base, floor)
    return base


def _seconds(raw: object, name: str) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, str):
        if not raw.strip():
            return None
        try:
            value = float(raw.strip())
        except ValueError:
            log.warning("ignoring %s=%r: not a number of seconds", name, raw)
            return None
    elif isinstance(raw, int | float):
        value = float(raw)
    else:
        log.warning("ignoring %s: expected seconds, got %s", name, type(raw).__name__)
        return None
    if value < 0 or math.isnan(value):
        log.warning("ignoring %s=%r: must be >= 0", name, raw)
        return None
    return value


def _count(raw: str | None, name: str) -> int | None:
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw.strip())
    except ValueError:
        log.warning("ignoring %s=%r: not a whole number", name, raw)
        return None
    if value < 0:
        log.warning("ignoring %s=%r: must be >= 0", name, raw)
        return None
    return value


def is_local_endpoint(base_url: str) -> bool:
    """A server on the user's machine or network: loopback and private addresses (the shared
    ``providers.hosts.is_private_network``), plus the unspecified address, CGNAT / tailnet
    addresses and local-only host names."""
    if not base_url:
        return False
    if is_private_network(base_url):
        return True
    host = host_of(base_url)
    if not host:
        return False
    if host.endswith(_LOCAL_SUFFIXES):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_unspecified:
        return True
    return isinstance(address, ipaddress.IPv4Address) and address in _CGNAT


__all__ = [
    "BREAKER_THRESHOLD",
    "CallTimeouts",
    "TimeoutPolicy",
    "is_local_endpoint",
]
