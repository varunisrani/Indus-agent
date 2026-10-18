"""Route binding: every route-dependent value of a session, changed in one step (P04-23).

A session talks to one route at a time, and much hangs off it: provider, model, base URL, wire
format, credential, the HTTP client, the reasoning setting, provider-specific extra request
fields, the context window, the prompt-cache policy and the system prompt's ``Model:`` /
``Provider:`` lines. ``/model`` (P06) and fallback (P08) change the route mid-session; a switch
that lands half-way (a new model with the old client, the old window with a new model) breaks the
next request. So all of it lives in one frozen ``RouteState`` and ``RouteBinding`` replaces that
value whole:

``apply(route)`` first derives the complete next state (the route must name a model and a base
URL; the window is resolved again through P03, which refuses anything below 64,000 tokens; a
client comes from the factory; reasoning, extra body and cache policy are derived for the new
route, never carried from the old one), then lets each registered *dependent* follow the change
(the prompt-identity rewrite here; P08 adds the compressor). If anything raises, the dependents
that already followed are walked back in reverse, the state stays as it was and the error
propagates. Only then is the new state swapped in, with one assignment, and ``generation`` bumped
so a caller holding an older state can tell (a retry must never replay a payload built for the
previous route, R01.5).

``rollback()`` returns to the state before the last ``apply`` through the same dependent path;
``rollback(to=snapshot)`` restores any earlier ``state`` value (fallback's "back to the primary").
"""

from __future__ import annotations

import asyncio
import copy
import logging
import types
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final

import httpx

from indus.core.prompt.lifecycle import PromptLifecycle
from indus.foundation.errors import RouteRefused
from indus.foundation.scope import Scope
from indus.providers import context_length, http, model_list
from indus.providers.context_length import ContextWindow, ListModels
from indus.providers.route import RuntimeRoute, Secret
from indus.providers.settings import as_mapping, config_of, model_entry, provider_entry
from indus.providers.types import normalize_effort

__all__ = [
    "KEEP",
    "MIN_CONTEXT",
    "CachePolicy",
    "ClientFactory",
    "Dependent",
    "RouteBinding",
    "RouteState",
    "cache_policy_for",
    "derive_state",
    "extra_body_for",
    "pooled_client",
    "prompt_identity",
    "reasoning_for",
]

log = logging.getLogger(__name__)

MIN_CONTEXT: Final = context_length.MIN_CONTEXT
NATIVE_CACHE_WIRES: Final = frozenset({"anthropic_messages"})

ClientFactory = Callable[[RuntimeRoute], httpx.AsyncClient]
# A dependent brings its own state in line with ``after``; called with the pair reversed it walks
# the change back, so it must be safe to call in either direction.
Dependent = Callable[["RouteState", "RouteState"], Awaitable[object]]


class _Keep:
    """Marker for "keep the session's current requested effort"."""


KEEP: Final = _Keep()


@dataclass(frozen=True)
class CachePolicy:
    """Whether requests carry prompt-cache markers, and what decided it."""

    markers: bool
    reason: str


@dataclass(frozen=True)
class RouteState:
    """Everything that follows from the bound route. Never mutated: a switch makes a new one."""

    route: RuntimeRoute
    client: httpx.AsyncClient
    window: ContextWindow
    reasoning_effort: str | None
    requested_effort: str | None
    extra_body: Mapping[str, Any]
    cache: CachePolicy

    @property
    def provider(self) -> str:
        return self.route.provider

    @property
    def model(self) -> str:
        return self.route.model

    @property
    def base_url(self) -> str:
        return self.route.base_url

    @property
    def api_mode(self) -> str:
        return self.route.api_mode

    @property
    def credential(self) -> Secret | None:
        return self.route.api_key

    @property
    def context_length(self) -> int:
        return self.window.tokens

    @property
    def identity(self) -> tuple[str, str]:
        """The ``Model:`` and ``Provider:`` values the system prompt should carry."""
        return (self.route.model.strip(), self.route.provider.strip())


# -- deriving the route-dependent values ----------------------------------------------------------


def _settings(scope: Scope, route: RuntimeRoute) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """``(providers.<name>.models.<id>, providers.<name>)`` of the scope's config."""
    entry = provider_entry(config_of(scope), route.provider)
    return model_entry(entry, route.model), entry


def reasoning_for(scope: Scope, route: RuntimeRoute, requested: str | None) -> str | None:
    """The effort to send: the per-model setting, else the provider's, else what the session
    asked for. ``false`` or an off word switches reasoning off."""
    for source in _settings(scope, route):
        if "reasoning_effort" not in source:
            continue
        raw = source["reasoning_effort"]
        if raw is False:
            return None
        if isinstance(raw, str):
            return normalize_effort(raw)
        log.warning("reasoning_effort for %s is not text; ignoring it", route.provider)
    return normalize_effort(requested)


def extra_body_for(scope: Scope, route: RuntimeRoute) -> Mapping[str, Any]:
    """Extra request fields for this provider and model only (model over provider, shallow).

    Built from the new route alone, so one provider's fields can never ride along to another.
    """
    per_model, entry = _settings(scope, route)
    merged = {**as_mapping(entry.get("extra_body")), **as_mapping(per_model.get("extra_body"))}
    return types.MappingProxyType(copy.deepcopy(merged))


def cache_policy_for(scope: Scope, route: RuntimeRoute) -> CachePolicy:
    """Cache markers for wires with a native cache layout, unless the provider entry says
    ``prompt_caching: false``. P08 refines this when prompt caching lands."""
    _, entry = _settings(scope, route)
    if entry.get("prompt_caching") is False:
        return CachePolicy(False, "config")
    return CachePolicy(route.api_mode in NATIVE_CACHE_WIRES, "wire")


def pooled_client(route: RuntimeRoute) -> httpx.AsyncClient:
    """The default client factory: P03's shared pool (clients are shared, never closed here)."""
    return http.client_for(route, timeouts=http.Timeouts())


def _check(route: RuntimeRoute) -> None:
    parts = (("model", route.model), ("base URL", route.base_url))
    missing = [name for name, value in parts if not value.strip()]
    if missing:
        raise RouteRefused(
            f"cannot switch to provider {route.provider or '?'}: the route has no "
            f"{' and no '.join(missing)}",
            hint="pick a model, and for a custom provider set its base URL",
        )


async def derive_state(
    scope: Scope,
    route: RuntimeRoute,
    *,
    requested_effort: str | None = None,
    client_factory: ClientFactory = pooled_client,
    list_models: ListModels | None = None,
) -> RouteState:
    """The complete state for ``route``. Raises ``RouteRefused`` for an incomplete route and
    ``ContextTooSmall`` for a window under ``MIN_CONTEXT``; nothing is changed either way."""
    _check(route)
    client = client_factory(route)

    async def ask_endpoint(target: RuntimeRoute) -> list[model_list.ModelInfo]:
        return await model_list.list_models(target, client=client, scope=scope)

    lister = list_models if list_models is not None else ask_endpoint
    window = await context_length.resolve(route, scope=scope, list_models=lister)
    return RouteState(
        route=route,
        client=client,
        window=window,
        reasoning_effort=reasoning_for(scope, route, requested_effort),
        requested_effort=normalize_effort(requested_effort),
        extra_body=extra_body_for(scope, route),
        cache=cache_policy_for(scope, route),
    )


# -- dependents -----------------------------------------------------------------------------------


def prompt_identity(lifecycle: PromptLifecycle) -> Dependent:
    """Keep the prompt's ``Model:``/``Provider:`` lines in line with the bound route.

    Only those lines change (P04-07 ``rebind_identity``); the rest of the prompt keeps its bytes
    so the provider's prefix cache survives as far as it can.
    """

    async def follow(before: RouteState, after: RouteState) -> None:
        if before.identity != after.identity:
            await lifecycle.rebind_identity(model=after.model, provider=after.provider)

    return follow


# -- the binding ----------------------------------------------------------------------------------


class RouteBinding:
    """One session's bound route. Switches are serialized and all-or-nothing."""

    def __init__(
        self,
        scope: Scope,
        state: RouteState,
        *,
        client_factory: ClientFactory = pooled_client,
        list_models: ListModels | None = None,
        dependents: Sequence[Dependent] = (),
    ) -> None:
        self._scope = scope
        self._state = state
        self._previous: RouteState | None = None
        self._generation = 0
        self._client_factory = client_factory
        self._list_models = list_models
        self._dependents: list[Dependent] = list(dependents)
        self._lock = asyncio.Lock()

    @classmethod
    async def bind(
        cls,
        scope: Scope,
        route: RuntimeRoute,
        *,
        requested_effort: str | None = None,
        client_factory: ClientFactory = pooled_client,
        list_models: ListModels | None = None,
        dependents: Sequence[Dependent] = (),
    ) -> RouteBinding:
        """The first binding of a session (same checks as ``apply``; no dependent runs)."""
        state = await derive_state(
            scope,
            route,
            requested_effort=requested_effort,
            client_factory=client_factory,
            list_models=list_models,
        )
        return cls(
            scope,
            state,
            client_factory=client_factory,
            list_models=list_models,
            dependents=dependents,
        )

    @property
    def state(self) -> RouteState:
        """The current state. Read it once per request and keep that value for the request."""
        return self._state

    @property
    def previous(self) -> RouteState | None:
        """The state the last ``apply`` replaced; None after a rollback or before any switch."""
        return self._previous

    @property
    def generation(self) -> int:
        """Bumped by every switch and rollback; a request built at another generation is stale."""
        return self._generation

    def add_dependent(self, dependent: Dependent) -> None:
        self._dependents.append(dependent)

    async def apply(
        self, route: RuntimeRoute, *, requested_effort: str | None | _Keep = KEEP
    ) -> RouteState:
        """Switch to ``route`` completely, or not at all (the error is re-raised)."""
        async with self._lock:
            before = self._state
            if isinstance(requested_effort, _Keep):
                wanted = before.requested_effort
            else:
                wanted = requested_effort
            try:
                after = await derive_state(
                    self._scope,
                    route,
                    requested_effort=wanted,
                    client_factory=self._client_factory,
                    list_models=self._list_models,
                )
            except Exception as exc:
                log.warning(
                    "route switch to %s (%s) refused: %s; staying on %s (%s)",
                    route.model,
                    route.provider,
                    exc,
                    before.model,
                    before.provider,
                )
                raise
            await self._follow(before, after)
            self._commit(after, previous=before)
            log.info(
                "route switched: %s (%s) -> %s (%s), window %s tokens (%s)",
                before.model,
                before.provider,
                after.model,
                after.provider,
                f"{after.window.tokens:,}",
                after.window.source,
            )
            return after

    async def rollback(self, to: RouteState | None = None) -> RouteState | None:
        """Return to the state before the last ``apply`` (or to ``to``); None when there is
        nothing to return to. All-or-nothing, like ``apply``."""
        async with self._lock:
            target = to if to is not None else self._previous
            if target is None or target is self._state:
                return None
            target = self._with_live_client(target)
            before = self._state
            await self._follow(before, target)
            self._commit(target, previous=None)
            log.info(
                "route restored: %s (%s) -> %s (%s)",
                before.model,
                before.provider,
                target.model,
                target.provider,
            )
            return target

    # -- helpers ----------------------------------------------------------------------------------

    def _with_live_client(self, state: RouteState) -> RouteState:
        if not state.client.is_closed:
            return state
        return replace(state, client=self._client_factory(state.route))

    async def _follow(self, before: RouteState, after: RouteState) -> None:
        done: list[Dependent] = []
        try:
            for dependent in self._dependents:
                await dependent(before, after)
                done.append(dependent)
        except BaseException as exc:
            log.warning(
                "a route dependent failed (%s: %s); walking %d dependent(s) back",
                type(exc).__name__,
                exc,
                len(done),
            )
            for dependent in reversed(done):
                try:
                    await dependent(after, before)
                except Exception as undo_exc:
                    log.error(
                        "walking a route dependent back failed: %s: %s",
                        type(undo_exc).__name__,
                        undo_exc,
                    )
            raise

    def _commit(self, state: RouteState, *, previous: RouteState | None) -> None:
        self._state = state
        self._previous = previous
        self._generation += 1
