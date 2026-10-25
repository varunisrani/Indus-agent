"""The daemon's main route (docs/notes/P25/windows-gateway.md, follow-up): a first run without a
model provider starts the daemon, and a provider set up later (the desktop's onboarding writes the
config through ``indus serve``) is picked up on the next agent start, without a restart."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from indus.foundation import config as config_mod
from indus.foundation.config import loader
from indus.foundation.errors import NoUsableProvider
from indus.foundation.scope import Scope
from indus.gateway.daemon.process import MainRoute
from indus.providers import bundled
from indus.providers.route import MODEL_ENV, PROVIDER_ENV, RuntimeRoute
from indus.service.session_service import SessionService

ENDPOINT = "http://127.0.0.1:18431/v1"


@pytest.fixture(autouse=True)
def fresh_config_cache() -> Iterator[None]:
    loader.reset()
    yield
    loader.reset()


@pytest.fixture
def bare(scope: Scope) -> Scope:
    """The shared scope without any provider key, base URL or provider choice in its env."""
    cleared: dict[str, str | None] = {name: None for name in bundled.env_var_names()}
    cleared.update({PROVIDER_ENV: None, MODEL_ENV: None})
    return scope.with_env(scope.env.with_overlay(cleared))


def choose_endpoint(scope: Scope) -> None:
    """What the desktop's onboarding saves for a model running on this computer."""
    change: dict[str, Any] = {
        "model": {"provider": "custom", "default": "fake-model", "base_url": ENDPOINT}
    }
    config_mod.save(scope, change)
    loader.invalidate()


def test_no_provider_is_pending_not_fatal(bare: Scope) -> None:
    fields: dict[str, Any] = {"cwd": "/work"}
    route = MainRoute(bare, fields)
    with pytest.raises(NoUsableProvider):
        route.refresh()
    assert route.known is None
    with pytest.raises(NoUsableProvider):
        route()  # reading a pending route never resolves on the caller's thread
    assert "model" not in fields


def test_a_provider_set_up_later_is_picked_up_and_kept(bare: Scope) -> None:
    fields: dict[str, Any] = {"cwd": "/work"}
    route = MainRoute(bare, fields)
    with pytest.raises(NoUsableProvider):
        route.refresh()
    choose_endpoint(bare)
    found = route.refresh()
    assert (found.provider, found.model, found.base_url) == ("custom", "fake-model", ENDPOINT)
    assert route() is found and route.known is found
    assert fields["model"] == "fake-model"  # new sessions record the model
    # once resolved it stays: a later config change needs a restart, as before
    config_mod.save(bare, {"model": {"default": "other-model"}})
    loader.invalidate()
    assert route.refresh() is found


def test_a_configured_profile_resolves_at_once(bare: Scope) -> None:
    choose_endpoint(bare)
    fields: dict[str, Any] = {}
    route = MainRoute(bare, fields)
    assert route.refresh().model == "fake-model"
    assert fields["model"] == "fake-model"


def test_the_service_asks_a_callable_route_each_time(bare: Scope) -> None:
    route = MainRoute(bare, {})
    service = SessionService(bare, route)
    with pytest.raises(NoUsableProvider):
        _ = service.route
    choose_endpoint(bare)
    route.refresh()
    assert service.route.model == "fake-model"


def test_a_plain_route_is_kept_as_given(bare: Scope) -> None:
    plain = RuntimeRoute(
        provider="custom", model="m", api_mode="chat_completions", base_url=ENDPOINT
    )
    assert SessionService(bare, plain).route is plain
