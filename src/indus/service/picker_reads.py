"""The TUI's picker reads, one definition for every transport (W47-c).

The slash menu (``complete.slash``), the model picker (``model.options``) and the sessions picker
(``session.list``) are answered by the in-process bridge (``--local``) and by the daemon's
WebSocket (an attached TUI, the default). Both call these functions, so an attached TUI sees what
``--local`` shows for the same profile:

- ``slash_completions(scope, text, surface=, session_id=, quick_exec=)``: the shared registry's
  names for the surface, plus the profile's quick commands (``exec`` ones only where they run) and
  the runnable skills and bundles;
- ``provider_options(scope, provider=, model=, route=, client=)``: the bundled providers with
  whether a key is stored (the secret store or the environment), and for a custom or local
  endpoint the model ids its ``/models`` lists (a failure leaves the list empty);
  ``session_choice(service, session_id)`` is a session's current provider, model, route and client;
  ``profile_choice(scope)`` the profile's resolved route (empty when none resolves);
- ``stored_sessions(handle, params)``: the store's picker listing (blocking; run it off the loop);
  each row carries the title, source and the model the session last ran on (W48-b);
- ``command_catalog(scope, surface=, skills=, refused=, quick_exec=)``: ``commands.catalog``, the
  surface's built-in commands (who runs each: ``SERVER_COMMANDS`` / ``CLIENT_COMMANDS``), then the
  quick commands and the runnable skills and bundles (blocking; W48-b). A host that does not run
  some of them names them: ``refused`` built-ins and, with ``quick_exec=False``, quick ``exec``
  commands are listed ``unavailable`` (the daemon: ``DAEMON_REFUSED``, W48-h; it runs quick
  ``exec`` commands since W50-b).

Who may name which session is the transport's rule, not this module's: the daemon checks that the
connection attached it before it calls in here.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping
from typing import TYPE_CHECKING, Any, Final, cast

import httpx

from indus.foundation.config import loader, schema
from indus.foundation.scope import Scope
from indus.protocol.bridge import (
    CatalogEntry,
    CommandKind,
    CommandRunner,
    CommandsCatalogResult,
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
from indus.service.commands.registry import REGISTRY, CommandDef, Surface
from indus.state import listing
from indus.state.registry import StoreHandle

if TYPE_CHECKING:
    from indus.learning.skills.invoke import CommandTable
    from indus.service.session_service import SessionService

__all__ = [
    "CLIENT_COMMANDS",
    "DAEMON_REFUSED",
    "MAX_SLASH_ITEMS",
    "QUICK_KEY",
    "SERVER_COMMANDS",
    "SKILL_PLATFORM",
    "command_catalog",
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
SERVER_COMMANDS = frozenset(
    {
        "title",
        "undo",
        "retry",
        "branch",
        "compress",
        "stop",
        "steer",
        "queue",
        "status",
        "usage",
        "history",
        "help",
        "version",
        "yolo",
        "reasoning",
        "fast",
        "tools",
    }
)
"""Built-in commands the agent side runs (``slash.exec``; the bridge's ``HANDLERS``)."""
CLIENT_COMMANDS = frozenset(
    {"new", "clear", "resume", "sessions", "model", "config", "quit", "save"}
)
"""Built-in commands the terminal UI runs itself."""
DAEMON_REFUSED: Final[Mapping[str, str]] = {
    "queue": (
        "not run by the daemon: it keeps no queue per viewer (a message sent while a reply runs "
        "waits in the session's own queue)"
    ),
}
"""Server commands a shared daemon does not run, with the reason it answers (W48-h; the session
switches ``yolo``, ``reasoning``, ``fast`` run there since W50-a, quick ``exec`` commands since
W50-b)."""

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
    scope: Scope,
    text: str,
    *,
    surface: Surface,
    session_id: str | None = None,
    quick_exec: bool = True,
) -> CompleteResult:
    """``complete.slash``: the candidates for the slash text typed so far (quick ``exec``
    commands only when ``quick_exec``: a host that does not run them leaves them out)."""
    extra: dict[str, str] = {}
    for name, spec in quick_command_table(scope).items():
        if not quick_exec and str(spec.get("type", "exec")).lower() == "exec":
            continue
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


def _signed_in(scope: Scope, profile: ProviderProfile) -> bool:
    """A sign-in-only provider counts as usable once ``indus auth add`` stored a sign-in."""
    from indus.providers.auth.store import AuthStore

    try:
        return profile.name in AuthStore(scope.home, env=scope.env).signed_in()
    except Exception:
        log.debug("auth store unreadable", exc_info=True)
        return False


def provider_option(scope: Scope, profile: ProviderProfile, current: str) -> ProviderOption:
    """One picker row: the provider, whether it can be used now, whether it is current."""
    return ProviderOption(
        slug=profile.name,
        name=profile.display_name or profile.name,
        auth_type=profile.auth_type,
        # A provider whose key is optional (local servers, custom endpoints) works without one.
        authenticated=profile.auth_type in ("none", "api_key_optional")
        or _key_stored(scope, profile)
        or (profile.signs_in and _signed_in(scope, profile)),
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
        model=summary.model or None,
        started_at=summary.started_at,
        last_active=summary.last_active,
        message_count=max(0, summary.message_count),
    )


def stored_sessions(handle: StoreHandle, params: SessionListParams) -> SessionListResult:
    """``session.list``: stored conversations, newest first (blocking)."""
    filters = listing.ListFilters(include_subagents=params.include_subagents, query=params.query)
    found = listing.list_sessions(handle, filters, params.limit)
    return SessionListResult(sessions=[session_row(s) for s in found])


# -- the command catalogue -------------------------------------------------------------------------


def _runner(cmd: CommandDef, refused: Collection[str]) -> CommandRunner:
    if cmd.name in refused:
        return CommandRunner.UNAVAILABLE
    if cmd.name in SERVER_COMMANDS:
        return CommandRunner.SERVER
    if cmd.name in CLIENT_COMMANDS:
        return CommandRunner.CLIENT
    return CommandRunner.LATER


def command_catalog(
    scope: Scope,
    *,
    surface: Surface,
    skills: CommandTable | None,
    refused: Collection[str] = (),
    quick_exec: bool = True,
) -> CommandsCatalogResult:
    """``commands.catalog``: built-ins, then quick commands, then the skills and bundles of
    ``skills`` (``skill_table``; None when they cannot be listed). ``refused`` built-ins and (when
    ``quick_exec`` is False) quick ``exec`` commands are ``unavailable`` here. Blocking."""
    entries = [
        CatalogEntry(
            name=cmd.name,
            aliases=list(cmd.aliases),
            description=cmd.description,
            category=cmd.category,
            args_hint=cmd.args_hint,
            kind=CommandKind.BUILTIN,
            runs=_runner(cmd, refused),
        )
        for cmd in REGISTRY.commands(surface)
    ]
    taken = {key for cmd in REGISTRY for key in cmd.keys}
    for name, spec in sorted(quick_command_table(scope).items()):
        if name in taken:
            continue
        kind = str(spec.get("type", "exec"))
        runs = CommandRunner.SERVER
        if not quick_exec and kind.lower() == "exec":
            runs = CommandRunner.UNAVAILABLE
        entries.append(
            CatalogEntry(
                name=name,
                description=str(spec.get("description") or f"quick command ({kind})"),
                category="custom",
                kind=CommandKind.QUICK,
                runs=runs,
            )
        )
        taken.add(name)
    if skills is not None:
        for name, note in sorted(skills.names().items()):
            if name in taken:
                continue
            entries.append(
                CatalogEntry(
                    name=name,
                    description=note,
                    category="custom",
                    kind=CommandKind.SKILL,
                    runs=CommandRunner.SERVER,
                )
            )
    return CommandsCatalogResult(commands=entries)
