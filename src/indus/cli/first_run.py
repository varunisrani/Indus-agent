"""Two checks before the classic chat starts: "is anything set up?" and "is this route costly?".

``first_run_guard``: when no provider is usable, print a short "not set up yet" block. Without a
terminal add the non-interactive guidance and exit 1 (R04.19); on a terminal offer to run
``indus setup`` (no, Ctrl+C or EOF → exit 1). Usable means any of:

- the configured route (``model.provider`` / ``model.base_url``) resolves;
- a provider key in the secret store, the profile's ``.env`` or the environment;
- a usable credential in a provider's pool (``indus auth add``, P08-12);
- a local base URL (``model.base_url`` on a loopback host, or a profile's base-URL variable such
  as ``OPENAI_BASE_URL``);
- a provider recorded as signed in by the auth store (``providers.auth.store``; metadata only).

Placeholder-shaped values (a bare ``sk-``, "your-key", ...) never count.

``route_guard``: only when ``-m`` or ``--provider`` was given, every registered guard may warn
about the route (``cost``: a paid tier; ``data_policy``: a tier that trains on prompts). On a
terminal the person confirms; without one the run is refused on stderr, except that data-policy
warnings alone pass when ``security.allow_data_training_tiers_noninteractive`` is true. ``--yolo``
does not skip it: it approves commands, not spending.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import click

from indus.cli.prompts import Cancelled, Interactive, NonInteractive, ask_yes_no

if TYPE_CHECKING:
    from indus.cli.args import CliState, GlobalArgs
    from indus.foundation.scope import Scope

__all__ = [
    "AUTH_FILE",
    "FLAGGED_ROUTES",
    "GUARDS",
    "FlaggedRoute",
    "SelectionWarning",
    "first_run_guard",
    "register_guard",
    "route_guard",
    "selection_warnings",
    "usable_provider",
]

log = logging.getLogger(__name__)

AUTH_FILE = "auth.json"
ALLOW_DATA_POLICY_KEY = "security.allow_data_training_tiers_noninteractive"

NOT_SET_UP = (
    "",
    "Indus AGI Agent is not set up yet: no model provider or API key was found.",
    "",
    "  Run:  indus setup",
    "",
)


# -- usable provider ---------------------------------------------------------------------------


def _fresh_scope(scope: Scope) -> Scope:
    """``scope`` with the config as it is on disk now (setup may have just written it)."""
    from indus.foundation import config

    return scope.with_config(config.load(scope))


def _env_file_values(scope: Scope) -> dict[str, str]:
    from indus.foundation.envfile import read_env_file
    from indus.foundation.envload import env_file_path

    try:
        return read_env_file(env_file_path(scope.home))
    except (OSError, UnicodeError) as exc:
        log.debug("could not read the profile .env: %s", exc)
        return {}


def _configured_route(scope: Scope) -> str | None:
    from indus.providers.route import resolve
    from indus.providers.settings import config_of, model_section, text

    saved = model_section(config_of(scope))
    if not (text(saved.get("provider")) or text(saved.get("base_url"))):
        return None
    try:
        route = resolve(scope)
    except Exception as exc:
        log.debug("the configured route does not resolve: %s", exc)
        return None
    return f"configured provider {route.provider}"


def _provider_key(scope: Scope, file_values: Mapping[str, str]) -> str | None:
    from indus.providers.registry import Registry
    from indus.providers.route import find_key, is_placeholder, pool_key_for

    try:
        profiles = Registry(scope).list_providers()
    except Exception as exc:
        log.debug("provider registry unavailable: %s", exc)
        return None
    for profile in profiles:
        if profile.auth_type == "none" or not profile.env_vars:
            continue
        found = find_key(scope, profile.env_vars)
        if found is not None:
            return f"{found[0]} for {profile.name}"
        for name in profile.env_vars:
            if not is_placeholder(file_values.get(name)):
                return f"{name} in .env for {profile.name}"
    for profile in profiles:
        if profile.auth_type != "none" and pool_key_for(scope, profile) is not None:
            return f"credential pool for {profile.name}"
    return None


def _local_url(scope: Scope, file_values: Mapping[str, str]) -> str | None:
    from indus.providers.hosts import is_loopback
    from indus.providers.registry import Registry
    from indus.providers.settings import config_of, model_section, text

    saved_url = text(model_section(config_of(scope)).get("base_url"))
    if saved_url:
        try:
            if is_loopback(saved_url):
                return f"local endpoint {saved_url}"
        except Exception:
            pass
    try:
        names = {p.base_url_env_var for p in Registry(scope).list_providers()} - {""}
    except Exception:
        names = {"OPENAI_BASE_URL"}
    for name in sorted(names):
        value = (scope.env.get(name) or file_values.get(name) or "").strip()
        if value:
            return f"{name} is set"
    return None


def signed_in_providers(home: Path) -> list[str]:
    """Providers the auth store records as signed in; a store that cannot be read gives ``[]``
    (the auth commands report that problem, the guard does not)."""
    from indus.foundation.errors import AuthError
    from indus.providers.auth.store import AuthStore

    try:
        return AuthStore(home).signed_in()
    except AuthError as exc:
        log.debug("auth store not consulted: %s", exc)
        return []


def usable_provider(scope: Scope) -> str | None:
    """A short description of the first sign that a provider is usable, or None."""
    fresh = _fresh_scope(scope)
    file_values = _env_file_values(fresh)
    for check in (
        lambda: _configured_route(fresh),
        lambda: _provider_key(fresh, file_values),
        lambda: _local_url(fresh, file_values),
    ):
        found = check()
        if found is not None:
            return found
    signed_in = signed_in_providers(fresh.home)
    return f"signed in to {signed_in[0]}" if signed_in else None


# -- first-run guard ---------------------------------------------------------------------------


def _terminal_io(state: CliState, guidance: str) -> Interactive:
    return Interactive.for_terminal(
        stdin_isatty=state.stdin_isatty,
        stdout_isatty=state.stdout_isatty,
        env=state.scope.env,
        guidance=guidance,
    )


def _default_setup(state: CliState) -> int:
    from indus.cli.setup.wizard import SetupOptions, run_setup

    return run_setup(state, SetupOptions())


def first_run_guard(
    state: CliState,
    *,
    io: Interactive | None = None,
    run_setup: Callable[[CliState], int] | None = None,
) -> int | None:
    """None when chat may start; otherwise the exit code (see the module notes)."""
    if usable_provider(state.scope) is not None:
        return None
    from indus.cli.setup.wizard import non_interactive_guidance

    guidance = non_interactive_guidance()
    io = io if io is not None else _terminal_io(state, guidance)
    if not io.tty:
        for line in (*NOT_SET_UP, *guidance.splitlines()):
            click.echo(line, err=True)
        return 1
    for line in NOT_SET_UP:
        io.say(line)
    try:
        wanted = ask_yes_no(io, "Run setup now?", default=True)
    except (Cancelled, NonInteractive):
        wanted = False
    if not wanted:
        io.say("You can run `indus setup` whenever you are ready.")
        return 1
    code = (run_setup or _default_setup)(state)
    if code == 0 and usable_provider(state.scope) is not None:
        return None
    return code or 1


# -- route guard -------------------------------------------------------------------------------


@dataclass(frozen=True)
class SelectionWarning:
    """``kind`` is ``cost`` or ``data_policy`` (later guards may add kinds)."""

    kind: str
    message: str


@dataclass(frozen=True)
class FlaggedRoute:
    """A static rule: ``provider`` ("" = any) and a model-id substring (case-insensitive)."""

    kind: str
    provider: str
    model_contains: str
    message: str


FLAGGED_ROUTES: tuple[FlaggedRoute, ...] = ()
"""Built-in rules. Empty until provider profiles carry pricing and data-policy metadata (P08);
the confirm/refuse logic below is complete and is fed by ``GUARDS``."""

type Guard = Callable[[Scope, str, str], SelectionWarning | None]


def _rule_guard(rules: Iterable[FlaggedRoute]) -> Guard:
    frozen = tuple(rules)

    def guard(scope: Scope, provider: str, model: str) -> SelectionWarning | None:
        lowered = model.lower()
        for rule in frozen:
            if rule.provider and rule.provider != provider:
                continue
            if rule.model_contains.lower() in lowered:
                return SelectionWarning(rule.kind, rule.message)
        return None

    return guard


GUARDS: list[Guard] = [_rule_guard(FLAGGED_ROUTES)]


def register_guard(guard: Guard) -> None:
    """Add a guard (evaluated in registration order)."""
    GUARDS.append(guard)


def selection_warnings(scope: Scope, provider: str, model: str) -> list[SelectionWarning]:
    """Warnings for the route; a guard that raises is skipped (never blocks a chat)."""
    out: list[SelectionWarning] = []
    for guard in list(GUARDS):
        try:
            found = guard(scope, provider, model)
        except Exception as exc:
            log.warning("a model route guard failed and was skipped: %s", exc)
            continue
        if found is not None:
            out.append(found)
    return out


def _err(*lines: str) -> None:
    for line in lines:
        click.echo(line, err=True)


def route_guard(state: CliState, args: GlobalArgs, *, io: Interactive | None = None) -> int | None:
    """None when the run may go on; 1 when the route was refused (see the module notes)."""
    explicit_model = (args.model or "").strip()
    explicit_provider = (args.provider or "").strip()
    if not explicit_model and not explicit_provider:
        return None
    from indus.foundation import config
    from indus.providers.settings import model_section, text

    view = config.load(state.scope)
    saved = model_section(view)
    model = explicit_model or text(saved.get("default"))
    if not model:
        return None
    provider = (explicit_provider or text(saved.get("provider"))).lower()
    warnings = selection_warnings(state.scope, provider, model)
    if not warnings:
        return None
    io = io if io is not None else _terminal_io(state, "")
    if not io.tty:
        if view.get(ALLOW_DATA_POLICY_KEY) is True:
            allowed = [w for w in warnings if w.kind == "data_policy"]
            if allowed:
                _err(*(w.message for w in allowed))
                _err(f"Going ahead without a terminal because {ALLOW_DATA_POLICY_KEY} is true.")
                warnings = [w for w in warnings if w.kind != "data_policy"]
                if not warnings:
                    return None
        _err(*(w.message for w in warnings))
        if any(w.kind == "data_policy" for w in warnings):
            _err(
                f"To allow data-training tiers in unattended runs, set {ALLOW_DATA_POLICY_KEY} "
                "to true."
            )
        _err(
            "Refused: this model route needs a confirmation and there is no terminal to ask on. "
            "Run it in a terminal to confirm it."
        )
        return 1
    _err(*(w.message for w in warnings))
    try:
        confirmed = ask_yes_no(io, "Use it for this run?", default=False)
    except (Cancelled, NonInteractive):
        confirmed = False
    if confirmed:
        return None
    _err("Not started: the model route was not confirmed.")
    return 1
