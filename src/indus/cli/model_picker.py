"""``indus model``: choose the provider and model the chat uses.

``pick(scope, io)`` asks the questions and returns a ``Selection`` (or None for "no change")
without writing anything; ``apply(scope, selection)`` stores the key and saves the config. The
setup wizard and ``indus fallback add`` use the same two steps.

Rows: the hosted providers of the registry in catalogue order, one row for the local servers
(LM Studio, Ollama, vLLM, llama.cpp), the named custom providers from ``providers:``, a custom
endpoint, the reasoning effort alone, and "leave unchanged". The active provider is resolved as:
a named custom provider whose URL equals ``model.base_url`` → the configured provider (config,
then ``INDUS_INFERENCE_PROVIDER``) → detection from the keys; an unknown name warns and falls
back to detection.

Saving writes ``model.default`` and ``model.provider``; ``model.base_url`` / ``model.api_mode``
only when the route needs them, and an old provider's values are dropped together with a stale
``OPENAI_BASE_URL`` in ``.env`` when the new provider is not the bare custom endpoint (R04.9).
Keys go to the secret store (keychain by default, P4); a key typed during a cancelled pick is
never stored.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, cast

import click

from indus.cli import model_cache
from indus.cli.prompts import (
    Cancelled,
    Interactive,
    ask_choice,
    ask_secret,
    ask_text,
    run_steps,
)

if TYPE_CHECKING:
    import httpx

    from indus.foundation.scope import Scope
    from indus.providers.auth.pkce import PkceFlowSpec
    from indus.providers.profile import ProviderProfile
    from indus.providers.registry import Registry
    from indus.providers.route import RuntimeRoute

__all__ = [
    "MODEL_GUIDANCE",
    "SIGN_IN_FLOWS",
    "PickerDeps",
    "Selection",
    "active_provider",
    "apply",
    "model_command",
    "pick",
    "run_model",
    "sign_in_for",
    "store_key",
]

log = logging.getLogger(__name__)

CUSTOM = "custom"
OPENAI_BASE_URL = "OPENAI_BASE_URL"
CUSTOM_KEY_ENV = "OPENAI_API_KEY"

MODEL_GUIDANCE = "\n".join(
    (
        "`indus model` asks questions, so it needs a terminal.",
        "To choose the model from a script instead:",
        "  indus config set model.provider openrouter",
        "  indus config set model.default <model id>",
        "and store the provider's key with:  indus auth add <provider>",
    )
)

type SignIn = Callable[[Scope, Interactive], str | None]


def _key_mint(spec: PkceFlowSpec) -> SignIn:
    """A browser sign-in that mints an ordinary API key (``providers.auth.flows``)."""

    def sign_in(scope: Scope, io: Interactive) -> str | None:
        from indus.foundation.errors import AuthError
        from indus.providers.auth import device_code, pkce

        async def go() -> pkce.PkceOutcome:
            async with device_code.make_client() as client:
                return await pkce.run_pkce_login(
                    spec, clock=scope.clock, say=io.say, env=scope.env, client=client
                )

        try:
            outcome = asyncio.run(go())
        except AuthError as exc:
            io.say(f"Sign-in failed: {exc}")
            return None
        return outcome.api_key

    return sign_in


def _documented_sign_ins() -> dict[str, SignIn]:
    from indus.providers.auth.flows import FLOWS

    return {
        name: _key_mint(flows.pkce)
        for name, flows in FLOWS.items()
        if flows.pkce is not None and flows.pkce.exchange == "key_mint"
    }


SIGN_IN_FLOWS: dict[str, SignIn] = {}
"""Provider name → a sign-in flow that returns an API key, on top of the documented key-mint rows
of ``providers.auth.flows`` (OpenRouter today), which are looked up only when a key is asked for
so ``--help`` stays light. Other providers take a pasted key."""


def sign_in_for(provider: str) -> SignIn | None:
    """The sign-in flow offered for ``provider`` (a ``SIGN_IN_FLOWS`` row wins), or None."""
    return SIGN_IN_FLOWS.get(provider) or _documented_sign_ins().get(provider)


_ROW_CUSTOM = "__custom__"
_ROW_LOCAL = "__local__"
_ROW_REASONING = "__reasoning__"
_ROW_CANCEL = "__cancel__"
_TYPE_ID = "Type a model id"


@dataclass(frozen=True)
class Selection:
    """A finished pick. ``key_value`` is never shown by ``repr``."""

    provider: str
    model: str
    base_url: str = ""
    api_mode: str = ""
    key_name: str = ""
    key_value: str = field(default="", repr=False)
    reasoning: str | None = None
    reasoning_only: bool = False

    def entry(self) -> dict[str, str]:
        """The ``{provider, model[, base_url, api_mode]}`` form (fallback chain entries)."""
        out = {"provider": self.provider, "model": self.model}
        if self.base_url:
            out["base_url"] = self.base_url
        if self.api_mode:
            out["api_mode"] = self.api_mode
        return out


@dataclass
class PickerDeps:
    """Seams: the HTTP client for ``/models``, how stale lists are refreshed, ``--refresh``."""

    client_factory: Callable[[], httpx.AsyncClient] | None = None
    revalidate: model_cache.Revalidate | None = None
    refresh: bool = False


# -- reading the current state ----------------------------------------------------------------


def _fresh(scope: Scope) -> Scope:
    from indus.foundation import config

    return scope.with_config(config.load(scope))


def _saved_model(scope: Scope) -> tuple[str, str, str]:
    from indus.providers.settings import config_of, model_section, text

    saved = model_section(config_of(scope))
    return (
        text(saved.get("default")),
        text(saved.get("provider")).lower(),
        text(saved.get("base_url")).rstrip("/"),
    )


def active_provider(scope: Scope, registry: Registry) -> tuple[str | None, list[str]]:
    """The provider in effect now and any warnings (see the module notes)."""
    from indus.providers.registry import SOURCE_CONFIG
    from indus.providers.route import PROVIDER_ENV, resolve

    _model, provider, url = _saved_model(scope)
    notes: list[str] = []
    if url and provider in ("", CUSTOM):
        for profile in registry.list_providers():
            same = profile.base_url.rstrip("/") == url
            if same and registry.source_of(profile.name) == SOURCE_CONFIG:
                return profile.name, notes
    requested = provider or scope.env.get(PROVIDER_ENV, "").strip().lower()
    if requested and requested != "auto":
        found = registry.lookup(requested)
        if found is not None:
            return found.name, notes
        notes.append(
            f"The provider '{requested}' in your settings is not known; showing the one "
            "detected from your keys instead."
        )
    try:
        return resolve(scope, provider="auto", registry=registry).provider, notes
    except Exception:
        return None, notes


# -- models -----------------------------------------------------------------------------------


def _fetch(route: RuntimeRoute, deps: PickerDeps) -> list[str]:
    import httpx

    from indus.providers.model_list import list_models

    async def run() -> list[str]:
        client = deps.client_factory() if deps.client_factory else httpx.AsyncClient()
        async with client:
            found = await list_models(route, client=client)
        return [m.id for m in found]

    try:
        return asyncio.run(run())
    except Exception as exc:
        log.warning("could not list models at %s: %s", route.base_url, exc)
        return []


def _route_for(
    scope: Scope, provider: str, *, base_url: str = "", key: tuple[str, str] | None = None
) -> RuntimeRoute | None:
    from indus.providers.route import resolve

    target = scope
    if key is not None:
        target = scope.with_secrets(None).with_env(scope.env.with_overlay({key[0]: key[1]}))
    try:
        return resolve(target, provider=provider, base_url=base_url or None)
    except Exception as exc:
        log.debug("no route for %s: %s", provider, exc)
        return None


def _model_ids(
    scope: Scope, profile: ProviderProfile, route: RuntimeRoute | None, deps: PickerDeps
) -> list[str]:
    curated = list(profile.model_aliases)
    if route is None or not profile.supports_model_listing:
        return curated
    secret = route.api_key.reveal() if route.api_key is not None else None
    key = model_cache.row_key(profile.name, route.base_url)
    return model_cache.get_models(
        scope,
        key,
        model_cache.fingerprint(secret),
        lambda: _fetch(route, deps),
        curated=curated,
        refresh=deps.refresh,
        revalidate=deps.revalidate,
    )


def _choose_model(io: Interactive, models: Sequence[str], current: str) -> str:
    if not models:
        return ask_text(io, "Model id", current or None, validate=_needs_text)
    rows = [*models, _TYPE_ID]
    default = list(models).index(current) if current in models else 0
    picked = ask_choice(io, "Model:", rows, default)
    if picked == len(models):
        return ask_text(io, "Model id", current or None, validate=_needs_text)
    return models[picked]


def _needs_text(value: str) -> str | None:
    return None if value.strip() else "Please enter a model id."


# -- keys -------------------------------------------------------------------------------------


class _Stop(Exception):
    """The pick ends with no change (a required key was not given)."""


def _key_for(scope: Scope, io: Interactive, profile: ProviderProfile) -> tuple[str, str] | None:
    """``(name, value)`` for a new key, None to keep the saved one or go without; ``_Stop``
    when a required key was not entered."""
    from indus.providers.route import find_key

    if profile.auth_type == "none" or not profile.env_vars:
        return None
    name = profile.env_vars[0]
    existing = find_key(scope, profile.env_vars)
    flow = sign_in_for(profile.name)
    if existing is not None:
        keep = ask_choice(
            io,
            f"A key for {profile.label} is saved ({existing[0]}).",
            ["Keep it", "Enter a new key"],
            0,
        )
        if keep == 0:
            return None
    elif flow is not None:
        how = ask_choice(
            io, f"How do you want to connect {profile.label}?", ["Paste an API key", "Sign in"], 0
        )
        if how == 1:
            minted = flow(scope, io)
            if not minted:
                raise _Stop()
            return name, minted
    optional = not profile.needs_key
    hint = "Enter for none" if optional else "Enter to cancel"
    value = ask_secret(io, f"{name} ({hint})")
    if value:
        return name, value
    if optional or existing is not None:
        return None
    io.say("No key was entered.")
    raise _Stop()


# -- flows ------------------------------------------------------------------------------------


def _provider_flow(
    scope: Scope, io: Interactive, profile: ProviderProfile, deps: PickerDeps
) -> Selection | None:
    current, saved_provider, _url = _saved_model(scope)
    try:
        new_key = _key_for(scope, io, profile)
    except _Stop:
        return None
    route = _route_for(scope, profile.name, key=new_key)
    models = _model_ids(scope, profile, route, deps)
    model = _choose_model(io, models, current if saved_provider == profile.name else "")
    return Selection(
        provider=profile.name,
        model=profile.resolve_model(model),
        key_name=new_key[0] if new_key else "",
        key_value=new_key[1] if new_key else "",
    )


def _url_problem(value: str) -> str | None:
    from indus.foundation.errors import InvalidBaseURL
    from indus.providers.hosts import parse_base_url

    try:
        parse_base_url(value.rstrip("/"))
    except InvalidBaseURL as exc:
        return f"That URL cannot be used: {exc.message}"
    return None


def _custom_flow(
    scope: Scope, io: Interactive, registry: Registry, deps: PickerDeps
) -> Selection | None:
    current, saved_provider, saved_url = _saved_model(scope)
    is_custom = saved_provider == CUSTOM
    url = ask_text(
        io,
        "Endpoint URL (OpenAI-compatible, e.g. http://127.0.0.1:8000/v1)",
        saved_url if is_custom and saved_url else None,
        validate=_url_problem,
    ).rstrip("/")
    secret = ask_secret(io, f"API key for this endpoint ({CUSTOM_KEY_ENV}; Enter for none)")
    new_key = (CUSTOM_KEY_ENV, secret) if secret else None
    route = _route_for(scope, CUSTOM, base_url=url, key=new_key)
    profile = registry.lookup(CUSTOM)
    models = _model_ids(scope, profile, route, deps) if profile is not None else []
    if len(models) == 1:
        io.say(f"The endpoint offers one model: {models[0]}")
        model = models[0]
    else:
        model = _choose_model(io, models, current if is_custom else "")
    return Selection(
        provider=CUSTOM,
        model=model,
        base_url=url,
        key_name=new_key[0] if new_key else "",
        key_value=new_key[1] if new_key else "",
    )


def _local_flow(
    scope: Scope, io: Interactive, profile: ProviderProfile, deps: PickerDeps
) -> Selection:
    current, saved_provider, saved_url = _saved_model(scope)
    mine = saved_provider == profile.name
    default_url = (saved_url if mine and saved_url else profile.base_url).rstrip("/")
    url = ask_text(io, f"{profile.label} URL", default_url, validate=_url_problem).rstrip("/")
    route = _route_for(scope, profile.name, base_url=url)
    models = _model_ids(scope, profile, route, deps)
    model = _choose_model(io, models, current if mine else "")
    keep_url = url if url != profile.base_url.rstrip("/") else ""
    return Selection(provider=profile.name, model=model, base_url=keep_url)


def _reasoning(scope: Scope, io: Interactive, profile: ProviderProfile | None) -> str | None:
    from indus.providers.profile import REASONING_LEVELS
    from indus.providers.settings import config_of

    levels = list(profile.reasoning_efforts or REASONING_LEVELS) if profile else []
    if not levels:
        levels = list(REASONING_LEVELS)
    current = str(config_of(scope).get("agent.reasoning_effort") or "") or "provider default"
    rows = [f"Keep the current setting ({current})", *levels]
    picked = ask_choice(io, "Reasoning effort:", rows, 0)
    return None if picked == 0 else levels[picked - 1]


def _rows(
    registry: Registry, active: str | None, *, with_reasoning: bool
) -> tuple[list[tuple[str, str]], int]:
    from indus.providers.registry import LOCAL_SERVER_NAMES, SOURCE_CONFIG

    rows: list[tuple[str, str]] = []
    default = 0
    profiles = registry.list_providers()

    def add(key: str, label: str) -> None:
        nonlocal default
        if active is not None and key == active:
            label += "  (active)"
            default = len(rows)
        rows.append((key, label))

    for profile in profiles:
        if profile.is_custom:
            continue
        add(profile.name, f"{profile.label} — {profile.description}")
    locals_ = [p for p in profiles if p.name in LOCAL_SERVER_NAMES]
    if locals_:
        label = "Local server ▸ (" + ", ".join(p.label for p in locals_) + ")"
        rows.append((_ROW_LOCAL, label))
        if active in LOCAL_SERVER_NAMES:
            rows[-1] = (_ROW_LOCAL, label + "  (active)")
            default = len(rows) - 1
    for profile in profiles:
        if registry.source_of(profile.name) == SOURCE_CONFIG and profile.name != CUSTOM:
            add(profile.name, f"{profile.label} ({profile.base_url})")
    add(CUSTOM, "Custom endpoint (type a URL)")
    if with_reasoning:
        rows.append((_ROW_REASONING, "Reasoning effort for the current model"))
    rows.append((_ROW_CANCEL, "Leave unchanged"))
    return rows, default


def pick(
    scope: Scope,
    io: Interactive,
    *,
    deps: PickerDeps | None = None,
    for_fallback: bool = False,
) -> Selection | None:
    """Ask for a provider and model; None means "no change". Nothing is written here."""
    from indus.providers.registry import LOCAL_SERVER_NAMES, Registry

    deps = deps or PickerDeps()
    scope = _fresh(scope)
    registry = Registry(scope)
    current, _provider, _url = _saved_model(scope)
    active, notes = active_provider(scope, registry)
    for note in notes:
        io.say(note)
    active_profile = registry.lookup(active) if active else None
    io.say(f"Current model:    {current or '(not set)'}")
    io.say(
        f"Active provider:  {active_profile.label if active_profile else 'none yet'}"
        + ("" if active_profile else " (pick one below)")
    )
    rows, default = _rows(registry, active, with_reasoning=not for_fallback)
    question = "Fallback provider:" if for_fallback else "Provider:"
    key = rows[ask_choice(io, question, [label for _, label in rows], default)][0]
    if key == _ROW_CANCEL:
        return None
    if key == _ROW_REASONING:
        effort = _reasoning(scope, io, active_profile)
        if effort is None:
            return None
        return Selection(
            provider=active or "", model=current, reasoning=effort, reasoning_only=True
        )
    if key == _ROW_LOCAL:
        servers = [p for p in registry.list_providers() if p.name in LOCAL_SERVER_NAMES]
        at = next((i for i, p in enumerate(servers) if p.name == active), 0)
        chosen = servers[ask_choice(io, "Local server:", [p.label for p in servers], at)]
        selection: Selection | None = _local_flow(scope, io, chosen, deps)
        profile: ProviderProfile | None = chosen
    elif key == CUSTOM:
        selection = _custom_flow(scope, io, registry, deps)
        profile = registry.lookup(CUSTOM)
    else:
        profile = registry.lookup(key)
        if profile is None:
            io.say(f"The provider {key} is no longer available.")
            return None
        selection = _provider_flow(scope, io, profile, deps)
    if selection is None or for_fallback:
        return selection
    return replace(selection, reasoning=_reasoning(scope, io, profile))


# -- saving -----------------------------------------------------------------------------------


def store_key(scope: Scope, selection: Selection) -> str | None:
    """Store the key typed during a pick (if any); returns the line to show."""
    if selection.key_name and selection.key_value:
        return _store_key(scope, selection.key_name, selection.key_value)
    return None


def _store_key(scope: Scope, name: str, value: str) -> str:
    from indus.foundation.envfile import set_value
    from indus.foundation.envload import env_file_path
    from indus.foundation.secrets import SecretStore

    store = scope.secrets
    if isinstance(store, SecretStore):
        store.set(name, value)
        where = "the OS keychain" if store.backend_name == "keychain" else "the profile's key file"
        return f"Saved {name} in {where}."
    set_value(env_file_path(scope.home), name, value)
    return f"Saved {name} in {env_file_path(scope.home)}."


def _shown(url: str) -> str:
    return url if len(url) <= 48 else url[:45] + "..."


def apply(scope: Scope, selection: Selection) -> list[str]:
    """Store the key and save the choice; returns lines to show."""
    from indus.foundation import config
    from indus.foundation.envfile import read_env_file, set_value
    from indus.foundation.envload import env_file_path

    notes: list[str] = []
    stored = store_key(scope, selection)
    if stored is not None:
        notes.append(stored)
    raw = config.load(scope).user_raw()
    if not selection.reasoning_only:
        section = raw.get("model")
        model: dict[str, Any] = (
            dict(cast(dict[str, Any], section)) if isinstance(section, dict) else {}
        )
        model["default"] = selection.model
        model["provider"] = selection.provider
        for name, value in (("base_url", selection.base_url), ("api_mode", selection.api_mode)):
            if value:
                model[name] = value
            elif model.pop(name, None):
                notes.append(f"Dropped model.{name}: it belonged to the previous provider.")
        raw["model"] = model
    if selection.reasoning:
        agent = raw.get("agent")
        agent_map: dict[str, Any] = (
            dict(cast(dict[str, Any], agent)) if isinstance(agent, dict) else {}
        )
        agent_map["reasoning_effort"] = selection.reasoning
        raw["agent"] = agent_map
    config.save(scope, raw, replace=True)
    if not selection.reasoning_only:
        notes.append(f"Model set to {selection.model} on {selection.provider}.")
        if selection.provider != CUSTOM:
            env_path = env_file_path(scope.home)
            stale = read_env_file(env_path).get(OPENAI_BASE_URL)
            if stale:
                set_value(env_path, OPENAI_BASE_URL, None)
                notes.append(f"Removed a stale {OPENAI_BASE_URL} from .env (was {_shown(stale)}).")
    if selection.reasoning:
        notes.append(f"Reasoning effort set to {selection.reasoning}.")
    return notes


# -- command ----------------------------------------------------------------------------------


def run_model(scope: Scope, io: Interactive, *, deps: PickerDeps | None = None) -> int:
    """The whole ``indus model`` run; returns the exit code."""
    from indus.foundation.errors import IndusError

    if not io.tty:
        click.echo(MODEL_GUIDANCE, err=True)
        return 1
    deps = deps or PickerDeps()
    if deps.refresh and model_cache.clear(scope):
        io.say("Cleared the saved model lists.")
    chosen: list[Selection | None] = []

    def step() -> None:
        chosen[:] = [pick(scope, io, deps=deps)]

    try:
        run_steps(io, [("the provider list", step)])
    except Cancelled:
        io.say("No change.")
        return 0
    selection = chosen[0] if chosen else None
    if selection is None:
        io.say("No change.")
        return 0
    try:
        notes = apply(scope, selection)
    except IndusError as exc:
        click.echo(f"indus model: {exc.message}", err=True)
        if exc.hint:
            click.echo(exc.hint, err=True)
        return 1
    for note in notes:
        io.say(note)
    return 0


@click.command(
    "model",
    help="Choose the provider and model for chats (asks questions; needs a terminal).",
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
)
@click.option("--refresh", is_flag=True, help="Fetch model lists again instead of the saved ones.")
@click.pass_context
def model_command(ctx: click.Context, refresh: bool) -> int:
    from indus.cli.args import CliState

    state = ctx.find_object(CliState)
    if state is None:
        raise click.UsageError("model was started without its process state")
    io = Interactive.for_terminal(
        stdin_isatty=state.stdin_isatty,
        stdout_isatty=state.stdout_isatty,
        env=state.scope.env,
        guidance=MODEL_GUIDANCE,
    )
    return run_model(state.scope, io, deps=PickerDeps(refresh=refresh))
