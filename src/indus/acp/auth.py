"""How an editor signs in: the ACP auth methods we advertise and accept (P19-02).

ACP has no auth store of its own here: the profile's provider route decides. ``detect_provider``
resolves the route the way a turn would and names its provider when a credential is usable — a
key, or a provider that needs none (a local endpoint, an identity-based login). It never raises:
a broken or empty configuration simply means "no credentials yet".

Advertised methods: the provider's name as an agent-managed method (only when credentials
resolve), and always the terminal method ``indus-setup`` (``indus acp --setup``) so a fresh editor
install can configure Indus Agent first.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from indus.foundation.scope import Scope

__all__ = ["SETUP_ARGS", "SETUP_METHOD_ID", "accepts", "auth_methods", "detect_provider"]

log = logging.getLogger(__name__)

SETUP_METHOD_ID: Final = "indus-setup"
SETUP_ARGS: Final = ("--setup",)


def detect_provider(scope: Scope) -> str | None:
    """The active provider's name when its credentials resolve, else None (never raises)."""
    try:
        from indus.providers.route import resolve

        route = resolve(scope)
    except Exception:
        log.debug("no provider resolves for the editor's sign-in", exc_info=True)
        return None
    name = route.provider.strip().lower()
    if not name:
        return None
    if route.api_key:
        return name
    profile = route.profile
    if profile is not None and not profile.needs_key:
        return name  # an identity login or a keyless endpoint: the credential is not a key
    if route.is_local:
        return name
    return None


def auth_methods(scope: Scope) -> list[dict[str, Any]]:
    """``authMethods`` for ``initialize`` (wire shape)."""
    methods: list[dict[str, Any]] = []
    provider = detect_provider(scope)
    if provider is not None:
        methods.append(
            {
                "id": provider,
                "name": f"{provider} (configured in Indus Agent)",
                "description": f"Use the {provider} credentials this Indus Agent profile has.",
            }
        )
    methods.append(
        {
            "type": "terminal",
            "id": SETUP_METHOD_ID,
            "name": "Set up Indus Agent",
            "description": "Choose a provider and model for Indus Agent in a terminal (first run).",
            "args": list(SETUP_ARGS),
        }
    )
    return methods


def accepts(scope: Scope, method_id: str) -> bool:
    """Does ``authenticate(method_id)`` succeed now? Setup only once credentials resolve; any
    other id must name the resolved provider."""
    wanted = method_id.strip().lower()
    provider = detect_provider(scope)
    if provider is None:
        return False
    if wanted == SETUP_METHOD_ID:
        return True
    return wanted == provider
