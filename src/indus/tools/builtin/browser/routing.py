"""Hybrid routing of private URLs (P24-41).

With a cloud browser provider serving a task and ``browser.route_private_local: true``, an address
the P05 URL policy refuses as private (private, loopback, link-local, CGNAT, unique-local; decided
after DNS resolution) is opened in a local Chromium *sidecar* session of the same task instead,
so the provider never sees it. Only the sidecar's checks are relaxed (``relaxed_scope``: private
addresses allowed, the metadata floor still refused); the cloud route keeps the normal policy.

The cloud session's request guard records the private addresses it refused
(``BrowserSession.private_hits``); a navigation on the cloud route that failed or landed on a
refused address while such a hit was recorded is retried on the sidecar, again only when routing
is on. The task's later page tools follow the route its last navigation took
(``SessionManager.routes``). Navigate results name the engine that served the page.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from indus.foundation.config.schema import default_for
from indus.foundation.scope import ConfigSource, Scope
from indus.safety.url_policy import Blocked

__all__ = [
    "ALLOW_PRIVATE",
    "MAIN",
    "SIDECAR",
    "SWITCH",
    "RelaxedConfig",
    "Route",
    "local_route_for",
    "relaxed_scope",
    "routing_on",
    "sidecar_key",
]

log = logging.getLogger(__name__)

type Route = Literal["main", "sidecar"]
MAIN: Route = "main"
SIDECAR: Route = "sidecar"
SWITCH = "browser.route_private_local"
ALLOW_PRIVATE = "security.allow_private_urls"
SIDECAR_SUFFIX = "#local"


def sidecar_key(task_key: str) -> str:
    return f"{task_key}{SIDECAR_SUFFIX}"


@dataclass(frozen=True)
class RelaxedConfig:
    """The scope's config with private addresses allowed (and nothing else changed)."""

    inner: ConfigSource | None
    overrides: Mapping[str, object]

    def get(self, path: str, /) -> object:
        if path in self.overrides:
            return self.overrides[path]
        if self.inner is None:
            return None
        return self.inner.get(path)


def relaxed_scope(scope: Scope) -> Scope:
    """``scope`` whose URL checks allow private addresses; metadata stays refused (P05 floor)."""
    return scope.with_config(RelaxedConfig(scope.config, {ALLOW_PRIVATE: True}))


def _flag(scope: Scope, key: str) -> bool:
    value: object = None
    if scope.config is not None:
        with contextlib.suppress(Exception):
            value = scope.config.get(key)
    return (default_for(key) if value is None else value) is True


def routing_on(scope: Scope, cloud_serving: bool) -> bool:
    """True when private addresses go to the sidecar for this scope."""
    return cloud_serving and _flag(scope, SWITCH)


def local_route_for(blocked: Blocked, scope: Scope, cloud_serving: bool) -> bool:
    """Whether a refusal may be served by the sidecar instead (only the ``private`` class)."""
    return blocked.kind == "private" and routing_on(scope, cloud_serving)
