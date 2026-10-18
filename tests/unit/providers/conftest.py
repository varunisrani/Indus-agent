"""Helpers for provider tests: a scope with a written config, a clean env view and secrets.

The shared ``scope`` fixture snapshots the process env, which may hold real provider keys; every
scope built here removes all provider key and base-URL names first, so no test sees them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from typing import Any, cast

import pytest

from indus.foundation import config as config_mod
from indus.foundation import envload
from indus.foundation.config import loader
from indus.foundation.scope import Scope
from indus.foundation.secrets import SecretStore
from indus.providers import bundled, context_length
from indus.providers.route import MODEL_ENV, PROVIDER_ENV

ScopeFactory = Callable[..., Scope]

CLEARED = (*bundled.env_var_names(), PROVIDER_ENV, MODEL_ENV, "INDUS_API_TIMEOUT")


@pytest.fixture(autouse=True)
def fresh_config_cache() -> Iterator[None]:
    """Fresh config cache and fallback warnings; the .env managed-name registry restored after."""
    saved = set(envload._managed_registry)  # pyright: ignore[reportPrivateUsage]
    loader.reset()
    context_length.reset_warnings()
    yield
    loader.reset()
    envload._managed_registry.clear()  # pyright: ignore[reportPrivateUsage]
    envload._managed_registry.update(saved)  # pyright: ignore[reportPrivateUsage]


@pytest.fixture
def make_scope(scope: Scope) -> ScopeFactory:
    """``make_scope(config=..., env=..., secrets=...)`` → the shared scope with those applied."""

    def build(
        config: Mapping[str, Any] | None = None,
        env: Mapping[str, str] | None = None,
        secrets: Mapping[str, str] | None = None,
    ) -> Scope:
        if config is not None:
            config_mod.save(scope, dict(config), replace=True)
            loader.invalidate()
        overlay: dict[str, str | None] = {name: None for name in CLEARED}
        overlay.update(env or {})
        built = scope.with_env(scope.env.with_overlay(overlay))
        store = cast(SecretStore, built.secrets)
        for name, value in (secrets or {}).items():
            store.set(name, value)
        return built

    return build


def fake_key(tag: str) -> str:
    """An obviously fake key value (not credential-shaped)."""
    return f"test-key-{tag}-0000"
