"""The TUI's picker reads, one definition for every transport (W47-c).

The slash menu (``complete.slash``), the model picker (``model.options``) and the sessions picker
(``session.list``) are answered by the in-process bridge (``--local``) and by the daemon's
WebSocket (an attached TUI, the default). Both call these functions, so an attached TUI sees what
``--local`` shows for the same profile:

- ``slash_completions(scope, text, surface=, session_id=)``: the shared registry's names for the
  surface, plus the profile's quick commands and the runnable skills and bundles;
- ``provider_options(scope, provider=, model=, route=, client=)``: the bundled providers with
  whether a key is stored (the secret store or the environment), and for a custom or local
  endpoint the model ids its ``/models`` lists (a failure leaves the list empty);
  ``session_choice(service, session_id)`` is a session's current provider, model, route and client;
  ``profile_choice(scope)`` the profile's resolved route (empty when none resolves);
- ``stored_sessions(handle, params)``: the store's picker listing (blocking; run it off the loop).

Who may name which session is the transport's rule, not this module's: the daemon checks that the
connection attached it before it calls in here.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

import httpx

from indus.foundation.config import loader, schema
from indus.foundation.scope import Scope
from indus.protocol.bridge import (
    CompleteResult,
    CompletionItem,
    ModelOptionsResult,
    ProviderOption,
    SessionListParams,
    SessionListResult,
    SessionRow,
)
from indus.providers import bundled
from indus.providers.model_list import list_models
from indus.providers.profile import ProviderProfile
from indus.providers.route import RuntimeRoute, resolve
from indus.service.commands.registry import REGISTRY, Surface
from indus.state import listing
from indus.state.registry import StoreHandle

if TYPE_CHECKING:
    from indus.learning.skills.invoke import CommandTable
    from indus.service.session_service import SessionService

__all__ = [
    "MAX_SLASH_ITEMS",
    "QUICK_KEY",
    "SKILL_PLATFORM",
    "profile_choice",
    "provider_option",
    "provider_options",
    "quick_command_table",
    "session_choice",
    "session_row",
    "skill_table",
    "slash_completions",
    "stored_sessions",
]

log = logging.getLogger(__name__)

QUICK_KEY = "quick_commands"
SKILL_PLATFORM = "tui"
"""The platform skills are listed for (the terminal UI's catalogue)."""
MAX_SLASH_ITEMS = 200

type Choice = tuple[str, str, RuntimeRoute | None, httpx.AsyncClient | None]
"""``(provider, model, route, client)``: what the picker marks current and may list live."""


# -- slash -----------------------------------------------------------------------------------------


def quick_command_table(scope: Scope) -> dict[str, Mapping[str, Any]]:
    """The profile's ``quick_commands`` table (read fresh), keyed by lowercase name."""
    raw: object = None
    try:
        raw = loader.load(scope).get(QUICK_KEY)
    except Exception:
        log.debug("settings unreadable", exc_info=True)
    if raw is None:
        raw = schema.default_for(QUICK_KEY)
    if not isinstance(raw, Mapping):
        return {}
    table: dict[str, Mapping[str, Any]] = {}
    for name, spec in cast(Mapping[object, object], raw).items():
        if isinstance(name, str) and name.strip() and isinstance(spec, Mapping):
            table[name.strip().lstrip("/").lower()] = cast(Mapping[str, Any], spec)
    return table


def skill_table(scope: Scope, session_id: str | None) -> CommandTable | None:
    """The skills and bundles a slash word can run (None when they cannot be listed)."""
    try:
        from indus.learning.skills.bundles import bundle_expanders
        from indus.learning.skills.invoke import CommandTable, skill_command_names
        from indus.learning.skills.resolver import load_catalog

        catalog = load_catalog(scope, platform=SKILL_PLATFORM)
        return CommandTable(
            skills=skill_command_names(catalog), bundles=bundle_expanders(scope, session_id)
        )
    except Exception:
        log.warning("skills could not be listed", exc_info=True)
        return None


def slash_completions(
    scope: Scope, text: str, *, surface: Surface, session_id: str | None = None
) -> CompleteResult:
    """``complete.slash``: the candidates for the slash text typed so far."""
    extra: dict[str, str] = {}
    for name, spec in quick_command_table(scope).items():
        extra[name] = str(spec.get("description") or "quick command")
    table = skill_table(scope, session_id)
    if table is not None:
        for name, note in table.names().items():
            extra.setdefault(name, note)
    found = REGISTRY.complete(text, REGISTRY.keys(surface), extra=extra)
    return CompleteResult(
        items=[
            CompletionItem(text=c.text, display=c.text, meta=c.meta)
            for c in found[:MAX_SLASH_ITEMS]
        ]
    )


# -- models ----------------------------------------------------------------------------------------


def _key_stored(scope: Scope, profile: ProviderProfile) -> bool:
    secrets = scope.secrets
    for name in profile.env_vars:
        try:
            if secrets is not None and secrets.get(name):
                return True
        except Exception:
            log.debug("secret store unreadable", exc_info=True)
        if scope.env.get(name, "").strip():
            return True
    return False


def provider_option(scope: Scope, profile: ProviderProfile, current: str) -> ProviderOption:
    """One picker row: the provider, whether it can be used now, whether it is current."""
    return ProviderOption(
        slug=profile.name,
        name=profile.display_name or profile.name,
        auth_type=profile.auth_type,
        # A provider whose key is optional (local servers, custom endpoints) works without one.
        authenticated=profile.auth_type in ("none", "api_key_optional")
        or _key_stored(scope, profile),
        current=profile.name == current,
    )


def _lists_live(route: RuntimeRoute | None) -> bool:
    """A custom or local endpoint's own list is the only one the picker can offer (RS4)."""
    if route is None:
        return False
    profile = route.profile or bundled.by_name().get(route.provider.strip().lower())
    return profile is not None and profile.is_custom and profile.supports_model_listing


async def _listed_ids(
    scope: Scope, route: RuntimeRoute, client: httpx.AsyncClient | None
) -> list[str]:
    try:
        if client is not None:
            found = await list_models(route, client=client, scope=scope)
        else:
            async with httpx.AsyncClient() as own:
                found = await list_models(route, client=own, scope=scope)
    except Exception:
        log.debug("could not list the models of %s", route.provider, exc_info=True)
        return []
    return list(dict.fromkeys(m.id for m in found if m.id))


def session_choice(service: SessionService, session_id: str) -> Choice:
    """A session's current provider, model, route and model client."""
    state = service.agent(session_id).binding.state
    return state.provider, state.model, state.route, state.client


def profile_choice(scope: Scope) -> Choice:
    """The profile's resolved route (empty when none resolves)."""
    try:
        route = resolve(scope)
    except Exception:
        log.debug("no route resolves for this profile", exc_info=True)
        return "", "", None, None
    return route.provider, route.model, route, None


async def provider_options(scope: Scope, choice: Choice) -> ModelOptionsResult:
    """``model.options`` for the current ``choice``."""
    provider, model, route, client = choice
    options = [provider_option(scope, p, provider) for p in bundled.PROFILES if not p.hidden]
    if route is not None and _lists_live(route):
        listed = await _listed_ids(scope, route, client)
        options = [
            o.model_copy(update={"models": listed}) if o.slug == route.provider else o
            for o in options
        ]
    return ModelOptionsResult(provider=provider, model=model, providers=options)


# -- sessions --------------------------------------------------------------------------------------


def session_row(summary: listing.SessionSummary) -> SessionRow:
    """One listing row on the wire."""
    return SessionRow(
        session_id=summary.id,
        title=summary.title,
        preview=summary.preview,
        source=summary.source,
        started_at=summary.started_at,
        last_active=summary.last_active,
        message_count=max(0, summary.message_count),
    )


def stored_sessions(handle: StoreHandle, params: SessionListParams) -> SessionListResult:
    """``session.list``: stored conversations, newest first (blocking)."""
    filters = listing.ListFilters(include_subagents=params.include_subagents, query=params.query)
    found = listing.list_sessions(handle, filters, params.limit)
    return SessionListResult(sessions=[session_row(s) for s in found])
