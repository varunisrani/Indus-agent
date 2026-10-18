"""``indus status``: one screen about this profile (P07-21).

Default: model, the active provider and where that choice came from, the providers that have a
usable credential (a key, a sign-in recorded in the auth store, or your own endpoint), the
signed-in providers, terminal backend, session count and store
size, gateway and scheduled jobs (placeholders until those phases land). ``--full`` adds paths,
the provider key names that are set and the config keys you set — names only, never a value.
``--deep`` (implies ``--full``) adds the slow checks: the store's integrity check and the terminal
backend probe. The store is opened read-only and never created.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import click

from indus.cli.cmdkit import cli_state, config_of, human_size
from indus.foundation.errors import IndusError, StoreUnavailable
from indus.foundation.scope import Scope
from indus.providers import route as route_mod
from indus.providers.profile import ProviderProfile
from indus.providers.registry import SOURCE_CONFIG, Registry
from indus.state import browse, connection

__all__ = [
    "ProviderState",
    "credentialed_providers",
    "key_names",
    "provider_state",
    "signed_in",
    "status_command",
    "user_keys",
]

_SOURCE_WORDS = {
    route_mod.SOURCE_EXPLICIT: "chosen on the command line",
    route_mod.SOURCE_CONFIG: "from config.yaml",
    route_mod.SOURCE_ENV: f"from {route_mod.PROVIDER_ENV}",
    route_mod.SOURCE_AUTO: "picked from the keys found",
}


@dataclass(frozen=True)
class ProviderState:
    provider: str | None
    source: str
    model: str
    problem: str | None = None


def provider_state(scope: Scope) -> ProviderState:
    """The route a chat would use now, or why there is none."""
    config = config_of(scope)
    saved_model = str(config.get("model.default") or "")
    try:
        found = route_mod.resolve(scope)
    except IndusError as exc:
        return ProviderState(None, "", saved_model or "(not set)", exc.message)
    return ProviderState(
        found.provider,
        _SOURCE_WORDS.get(found.source, found.source),
        found.model or saved_model or "(not set)",
    )


def signed_in(scope: Scope) -> list[str]:
    """Providers the auth store records as signed in (``indus auth add --type oauth``); a store
    that cannot be read counts as none here (``indus doctor`` reports it)."""
    from indus.foundation.errors import AuthError
    from indus.providers.auth.store import AuthStore

    try:
        return AuthStore(scope.home).signed_in()
    except AuthError:
        return []


def credentialed_providers(scope: Scope) -> list[ProviderProfile]:
    """Providers with a usable key in the secret store or env view or a sign-in in the auth
    store, plus your own endpoints."""
    registry = Registry(scope)
    signed = set(signed_in(scope))
    out: list[ProviderProfile] = []
    for profile in registry.list_providers():
        own = registry.source_of(profile.name) == SOURCE_CONFIG
        has_key = bool(profile.env_vars) and route_mod.find_key(scope, profile.env_vars) is not None
        if has_key or own or profile.name in signed:
            out.append(profile)
    return out


def key_names(scope: Scope) -> list[tuple[str, bool]]:
    """Every provider key name the catalogue knows, with whether a usable value is set."""
    seen: dict[str, bool] = {}
    for profile in Registry(scope).list_providers():
        for name in profile.env_vars:
            if name not in seen:
                seen[name] = route_mod.find_key(scope, (name,)) is not None
    return list(seen.items())


def _leaves(tree: Mapping[str, Any], prefix: str = "") -> Iterator[str]:
    for key, value in tree.items():
        path = f"{prefix}{key}"
        if isinstance(value, Mapping) and value:
            yield from _leaves(value, path + ".")  # pyright: ignore[reportUnknownArgumentType]
        else:
            yield path


def user_keys(scope: Scope) -> list[str]:
    """Dotted names of the settings the user's own config file sets."""
    config = config_of(scope)
    raw = config.user_raw() if hasattr(config, "user_raw") else {}
    return sorted(k for k in _leaves(raw) if not k.startswith("_"))


def _store_line(scope: Scope) -> tuple[str, int | None]:
    path = connection.default_store_path(scope)
    if not path.is_file():
        return "0 sessions (nothing saved yet)", None
    size = path.stat().st_size
    try:
        with browse.open_readonly(scope) as store, store.read() as conn:
            count = int(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
    except (StoreUnavailable, sqlite3.Error) as exc:
        return f"cannot be read ({exc})", size
    return f"{count} session(s), {human_size(size)} store", size


def _row(label: str, value: object) -> None:
    click.echo(f"  {label:<14}{value}")


@click.command("status", short_help="Show model, provider and store.")
@click.option("--full", "--all", "full", is_flag=True, help="Show every section.")
@click.option("--deep", is_flag=True, help="Also run the slower checks (implies --full).")
def status_command(full: bool, deep: bool) -> int:
    """Show the model, provider, credentials, backend and session store at a glance."""
    scope = cli_state().scope
    full = full or deep
    config = config_of(scope)
    state = provider_state(scope)
    providers = credentialed_providers(scope)
    click.echo("Indus Agent status")
    _row("Profile:", f"{scope.profile_id} ({scope.home})")
    _row("Model:", state.model)
    if state.provider is None:
        _row("Provider:", f"none: {state.problem}. Run `indus setup`.")
    else:
        _row("Provider:", f"{state.provider} ({state.source})")
    _row("Credentials:", ", ".join(p.name for p in providers) or "none")
    _row("Signed in:", ", ".join(signed_in(scope)) or "none")
    _row("Terminal:", str(config.get("terminal.backend") or "local"))
    store_text, _size = _store_line(scope)
    _row("Sessions:", store_text)
    _row("Gateway:", "not running")
    _row("Scheduled:", "0 jobs")
    if not full:
        return 0
    click.echo("")
    click.echo("Paths")
    _row("Config:", scope.home / "config.yaml")
    _row(".env:", scope.home / ".env")
    _row("Store:", connection.default_store_path(scope))
    _row("Logs:", scope.home / "logs")
    click.echo("")
    click.echo("Provider keys (values never shown)")
    for name, is_set in key_names(scope):
        click.echo(f"  {name:<28} {'set' if is_set else 'not set'}")
    keys = user_keys(scope)
    click.echo("")
    click.echo(f"Settings you changed ({len(keys)})")
    for key in keys:
        click.echo(f"  {key}")
    if not deep:
        return 0
    click.echo("")
    click.echo("Deep checks")
    _row("Store check:", _integrity_line(scope))
    backend = str(config.get("terminal.backend") or "local")
    _row(
        "Backend:",
        "local commands run on this machine"
        if backend == "local"
        else f"{backend}: not available in this build yet",
    )
    return 0


def _integrity_line(scope: Scope) -> str:
    if not connection.default_store_path(scope).is_file():
        return "no store yet"
    try:
        report = browse.integrity(scope)
    except (StoreUnavailable, sqlite3.Error) as exc:
        return f"could not run ({exc})"
    if report.ok and report.fts is not False and not report.fts_stale:
        return "ok"
    problems = [line for line in report.structure if line != "ok"][:3]
    if report.fts is False or report.fts_stale:
        problems.append("search index needs a rebuild (`indus sessions repair`)")
    return "; ".join(problems) or "problems found"
