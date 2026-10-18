"""``indus auth``: credential pools and provider sign-in.

``add`` stores an API key (asked for with a hidden prompt when not given) or runs the provider's
documented sign-in flow; ``list``, ``remove``, ``reset``, ``priority``, ``refresh``, ``status`` and
``logout`` read and edit the pools in ``auth.json``. Secrets go to the profile's SecretStore,
never to ``auth.json`` or the screen. Bare ``indus auth`` opens a small menu in a terminal and
prints the pools otherwise. There is no ``indus login`` (D-16.80): the unknown-command answer
points here.
"""

from __future__ import annotations

import asyncio
import difflib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, NoReturn, cast

import click

from indus.cli import prompts
from indus.cli.args import CliState
from indus.cli.lazy import LazyGroup
from indus.cli.prompts import Cancelled, Interactive, NonInteractive
from indus.foundation import config as config_mod
from indus.foundation.config import keys as config_keys
from indus.foundation.errors import AuthError, ConfigError, IndusError, SecretStoreError
from indus.foundation.scope import Scope
from indus.foundation.secrets import open_secret_store
from indus.providers.auth import browser, device_code, pkce
from indus.providers.auth.flows import SignInFlows, flows_for
from indus.providers.auth.pool_model import (
    STRATEGIES,
    STRATEGY_KEY,
    Pool,
    PoolEntry,
    PoolTargetError,
    SecretWriter,
    edit_pool,
    env_entries,
    load_pool,
    new_entry_id,
    secret_name,
    strategy_for,
    suppress_env,
    unsuppress,
)
from indus.providers.auth.store import AuthStore
from indus.providers.profile import ProviderProfile
from indus.providers.registry import Registry
from indus.providers.route import is_placeholder

__all__ = ["auth_group"]

STRATEGY_TEXT = {
    "fill_first": "use the first healthy credential until it runs out (priority order)",
    "round_robin": "take turns through the healthy credentials",
    "least_used": "pick the credential used least so far",
    "random": "pick any healthy credential at random",
}


AUTH_GUIDANCE = (
    "There is no terminal to ask on. Use `indus auth add PROVIDER --api-key KEY`, "
    "`indus auth list`, `indus auth remove PROVIDER TARGET` (see `indus auth --help`)."
)


class _Stop(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class _Env:
    """What every subcommand needs, built once per invocation."""

    state: CliState
    scope: Scope
    store: AuthStore
    registry: Registry
    _secrets: SecretWriter | None = None
    _io: Interactive | None = None

    @property
    def tty(self) -> bool:
        return self.io.tty

    @property
    def io(self) -> Interactive:
        if self._io is None:
            self._io = prompts.io_for(self.state, AUTH_GUIDANCE)
        return self._io

    @property
    def secrets(self) -> SecretWriter:
        if self._secrets is None:
            current = self.scope.secrets
            if callable(getattr(current, "set", None)) and callable(
                getattr(current, "delete", None)
            ):
                self._secrets = cast(SecretWriter, current)
            else:
                mode = str(_config_get(self.scope, "security.secret_store") or "auto")
                self._secrets = open_secret_store(self.scope.home, self.scope.profile_id, mode=mode)
        return self._secrets

    def env_rows(self, profile: ProviderProfile) -> list[PoolEntry]:
        try:
            return env_entries(profile, self.secrets, self.scope.env)
        except SecretStoreError:
            return env_entries(profile, None, self.scope.env)


def _config_get(scope: Scope, key: str) -> Any:
    if scope.config is None:
        return None
    try:
        return scope.config.get(key)
    except Exception:
        return None


def _config_view(scope: Scope) -> Any:
    return config_mod.load(scope)


def _say(text: str = "") -> None:
    click.echo(text)


def _warn(text: str) -> None:
    click.echo(text, err=True)


def _fail(message: str, *hints: str) -> NoReturn:
    _warn(message)
    for hint in hints:
        _warn(hint)
    raise _Stop(1)


@contextmanager
def _guarded() -> Iterator[None]:
    """Turn the expected failures into a message and exit 1 (never a traceback)."""
    try:
        yield
    except _Stop as stop:
        raise click.exceptions.Exit(stop.code) from None
    except NonInteractive as exc:
        _warn(exc.guidance)
        raise click.exceptions.Exit(1) from None
    except Cancelled:
        _warn("Cancelled; nothing more was changed.")
        raise click.exceptions.Exit(1) from None
    except PoolTargetError as exc:
        _warn(str(exc))
        raise click.exceptions.Exit(1) from None
    except IndusError as exc:
        _warn(str(exc))
        if exc.hint:
            _warn(exc.hint)
        raise click.exceptions.Exit(1) from None


def _env_from(ctx: click.Context) -> _Env:
    state = ctx.find_object(CliState)
    if state is None:
        raise click.UsageError("the indus app was started without its process state")
    scope = state.scope
    return _Env(state=state, scope=scope, store=AuthStore(scope.home), registry=Registry(scope))


def _provider(env: _Env, name: str) -> ProviderProfile:
    profile = env.registry.lookup(name)
    if profile is not None:
        return profile
    known = [p.name for p in env.registry.list_providers()]
    close = difflib.get_close_matches(name.lower(), known, n=3, cutoff=0.5)
    hint = (
        f"Did you mean: {', '.join(close)}?" if close else f"Known providers: {', '.join(known)}."
    )
    _fail(
        f"'{name}' is not a provider Indus Agent knows.",
        hint,
        "Pick one interactively with `indus model`, or add a custom endpoint under `providers:`.",
    )


def _pool(env: _Env, profile: ProviderProfile) -> tuple[Pool, bool]:
    return load_pool(env.store, profile.name, env.env_rows(profile))


def _edit[T](env: _Env, profile: ProviderProfile, change: Callable[[Pool, dict[str, Any]], T]) -> T:
    return edit_pool(env.store, profile.name, change, env.env_rows(profile))


def _no_edit_on_inherited(env: _Env, profile: ProviderProfile) -> None:
    pool, inherited = _pool(env, profile)
    if inherited and pool.entries:
        _fail(
            f"{profile.name}'s credentials come from the default profile and are read-only here.",
            "Run the command without -p (or with -p default) to change them.",
        )


# -- formatting --------------------------------------------------------------------------------


def _left(seconds: float) -> str:
    total = max(0, int(seconds + 0.999))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    parts = [(days, "d"), (hours, "h"), (minutes, "m"), (secs, "s")]
    shown = [f"{v}{u}" for v, u in parts if v][:2]
    return " ".join(shown) or "0s"


def _status_text(entry: PoolEntry, now: datetime) -> str:
    if entry.status == "exhausted":
        why = entry.last_error_class or "exhausted"
        end = entry.cooldown_end()
        if end is None:
            return f"benched ({why})"
        remaining = (end - now).total_seconds()
        if remaining <= 0:
            return f"benched ({why}, ready to retry)"
        return f"benched ({why}, {_left(remaining)} left)"
    if entry.auth_type == "oauth" and entry.expires_at:
        try:
            ends = datetime.fromisoformat(entry.expires_at)
        except ValueError:
            return "ok"
        remaining = (ends - now).total_seconds()
        return (
            "ok (token expired; refresh)"
            if remaining <= 0
            else f"ok (token {_left(remaining)} left)"
        )
    return "ok"


def _print_pool(pool: Pool, strategy: str, now: datetime, *, inherited: bool) -> None:
    where = ", from the default profile" if inherited else ""
    count = len(pool)
    _say(f"{pool.provider} ({count} credential{'s' if count != 1 else ''}, {strategy}{where}):")
    nxt = pool.peek(strategy, now)
    for i, entry in enumerate(pool.entries, start=1):
        marker = "  <- next" if nxt is not None and entry.id == nxt.id else ""
        _say(
            f"  #{i:<2} {entry.label:<22} {entry.auth_type:<7} {entry.source:<11} "
            f"id={entry.id}  {_status_text(entry, now)}{marker}"
        )


def _strategy(env: _Env, provider: str) -> str:
    return strategy_for(env.scope.config, provider)


# -- secrets -----------------------------------------------------------------------------------


def _ask_secret(env: _Env, question: str) -> str:
    return prompts.ask_secret(env.io, question)


def _ask_text(env: _Env, question: str, default: str = "") -> str:
    return prompts.ask_text(env.io, question, default or None)


def _ask_choice(env: _Env, question: str, options: list[str]) -> int:
    return prompts.ask_choice(env.io, question, options)


def _store_secret_then_row(
    env: _Env,
    profile: ProviderProfile,
    secret: str,
    entry: PoolEntry,
    priority: int | None,
) -> tuple[int, PoolEntry]:
    name = secret_name(profile.name, entry.id)
    env.secrets.set(name, secret)

    def change(pool: Pool, doc: dict[str, Any]) -> tuple[int, PoolEntry]:
        unsuppress(doc, profile.name)
        pool.add(entry, priority)
        return pool.resolve(entry.id)

    try:
        return _edit(env, profile, change)
    except BaseException:
        env.secrets.delete(name)
        raise


# -- sign-in flows -----------------------------------------------------------------------------


@dataclass(frozen=True)
class _FlowOptions:
    no_browser: bool
    timeout: float
    ca_bundle: str | None
    insecure: bool


async def _sign_in(env: _Env, flows: SignInFlows, options: _FlowOptions) -> pkce.PkceOutcome:
    verify = device_code.tls_verify(options.ca_bundle, options.insecure, warn=_warn)
    async with device_code.make_client(verify) as client:
        if flows.pkce is not None:
            return await pkce.run_pkce_login(
                flows.pkce,
                clock=env.scope.clock,
                say=_say,
                env=env.scope.env,
                client=client,
                no_browser=options.no_browser,
                timeout=options.timeout,
            )
        assert flows.device is not None
        opener = (
            browser.open_url
            if browser.should_open(env.scope.env, no_browser=options.no_browser)
            else None
        )
        tokens = await device_code.run_device_login(
            flows.device, clock=env.scope.clock, say=_say, open_url=opener, client=client
        )
        return pkce.PkceOutcome(tokens=tokens, via_device=True)


def run_sign_in(env: _Env, flows: SignInFlows, options: _FlowOptions) -> pkce.PkceOutcome:
    """Seam for tests: run the provider's flow to completion."""
    return asyncio.run(_sign_in(env, flows, options))


def _refresh_spec(flows: SignInFlows) -> device_code.DeviceFlowSpec | None:
    if flows.device is not None:
        return flows.device
    if flows.pkce is not None and flows.pkce.exchange == "token":
        return device_code.DeviceFlowSpec(
            provider=flows.provider,
            device_authorization_url="",
            token_url=flows.pkce.exchange_url,
            client_id=flows.pkce.client_id,
            scope=flows.pkce.scope,
        )
    return None


def run_refresh(
    env: _Env, spec: device_code.DeviceFlowSpec, tokens: device_code.TokenSet
) -> device_code.TokenSet:
    """Seam for tests: renew ``tokens`` through the provider's token endpoint."""

    async def go() -> device_code.TokenSet:
        async with device_code.make_client() as client:
            return await device_code.refresh_tokens(client, spec, tokens, clock=env.scope.clock)

    return asyncio.run(go())


# -- the group ---------------------------------------------------------------------------------


@click.group(
    "auth",
    cls=LazyGroup,
    invoke_without_command=True,
    help="Manage provider credentials: keys, sign-ins and credential pools.",
)
@click.pass_context
def auth_group(ctx: click.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    env = _env_from(ctx)
    with _guarded():
        if not env.tty:
            _list(env, None)
            _say("")
            _say(
                "Manage them with `indus auth add|list|remove|reset|priority|refresh|status|logout`"
            )
            _say("(see `indus auth --help`).")
            return
        _menu(env)


def _menu(env: _Env) -> None:
    _list(env, None)
    _say("")
    _say("What would you like to do?")
    choice = _ask_choice(
        env,
        "Choice",
        [
            "Add a credential",
            "Remove a credential",
            "Reset benched credentials",
            "Choose how a pool picks credentials",
            "Quit",
        ],
    )
    if choice == 4:
        return
    profile = _provider(env, _ask_text(env, "Provider"))
    if choice == 0:
        _add_api_key(env, profile, key=None, label=None, priority=None)
    elif choice == 1:
        _list(env, profile.name)
        _remove(env, profile, _ask_text(env, "Which one (index, id or label)"))
    elif choice == 2:
        _reset(env, profile, None)
    else:
        current = _strategy(env, profile.name)
        _say(f"{profile.name} uses {current} now.")
        picked = STRATEGIES[
            _ask_choice(env, "Strategy", [f"{s} — {STRATEGY_TEXT[s]}" for s in STRATEGIES])
        ]
        config_mod.save(env.scope, {STRATEGY_KEY: {profile.name: picked}})
        _say(f"{profile.name} now uses {picked}.")


# -- add ---------------------------------------------------------------------------------------


def _add_api_key(
    env: _Env, profile: ProviderProfile, *, key: str | None, label: str | None, priority: int | None
) -> None:
    value = (key or "").strip()
    if not value:
        if not env.tty:
            _fail(
                f"No key given for {profile.name}, and there is no terminal to ask for one.",
                f"Pass it with `indus auth add {profile.name} --api-key KEY`, or run the command "
                "in a terminal to type it hidden.",
            )
        value = _ask_secret(env, f"{profile.label} API key (hidden)").strip()
        if not value:
            _fail("No key entered; nothing was saved.")
    if is_placeholder(value):
        _fail("That does not look like a real key (too short or a placeholder); nothing was saved.")
    pool, _ = _pool(env, profile)
    entry_id = new_entry_id(pool.ids())
    if not label and env.tty and key is None:
        label = _ask_text(env, "Label", default=f"api-key-{len(pool) + 1}")
    entry = PoolEntry(
        id=entry_id,
        label=(label or "").strip() or f"api-key-{len(pool) + 1}",
        auth_type="api_key",
        source="manual",
    )
    index, saved = _store_secret_then_row(env, profile, value, entry, priority)
    _say(
        f'Added {profile.name} credential #{index} ("{saved.label}", key stored in {_where(env)}).'
    )


def _where(env: _Env) -> str:
    backend = getattr(env.secrets, "backend_name", "")
    return "the OS keychain" if backend == "keychain" else "the profile's secret store"


def _add_signed_in(
    env: _Env,
    profile: ProviderProfile,
    flows: SignInFlows,
    options: _FlowOptions,
    label: str | None,
    priority: int | None,
) -> None:
    outcome = run_sign_in(env, flows, options)
    pool, _ = _pool(env, profile)
    entry_id = new_entry_id(pool.ids())
    if outcome.api_key:
        entry = PoolEntry(
            id=entry_id,
            label=(label or "").strip() or f"signed-in-key-{len(pool) + 1}",
            auth_type="api_key",
            source="pkce",
        )
        secret = outcome.api_key
    else:
        tokens = outcome.tokens
        assert tokens is not None
        entry = PoolEntry(
            id=entry_id,
            label=(label or "").strip() or f"sign-in-{len(pool) + 1}",
            auth_type="oauth",
            source="device_code" if outcome.via_device else "pkce",
            expires_at=tokens.expires_at.isoformat() if tokens.expires_at else None,
        )
        secret = tokens.to_secret()
    index, saved = _store_secret_then_row(env, profile, secret, entry, priority)
    now = env.scope.clock.now().isoformat()
    with env.store.transaction() as doc:
        providers = cast(dict[str, Any], doc.setdefault("providers", {}))
        providers[profile.name] = {"signed_in_at": now, "flow": saved.source}
        if not doc.get("active_provider"):
            doc["active_provider"] = profile.name
    _say(f'Signed in. Added {profile.name} credential #{index} ("{saved.label}").')


@auth_group.command("add")
@click.argument("provider")
@click.option(
    "--type",
    "kind",
    type=click.Choice(["api-key", "api_key", "oauth"], case_sensitive=False),
    default=None,
    help="api-key (default) or oauth (the provider's documented browser or device sign-in).",
)
@click.option("--label", default=None, help="A name to tell this credential apart.")
@click.option(
    "--priority", type=int, default=None, help="Place it at this position (0 = tried first)."
)
@click.option("--api-key", "api_key", default=None, help="The key (asked for, hidden, if omitted).")
@click.option("--no-browser", is_flag=True, help="Never open a browser; print the address.")
@click.option(
    "--timeout", type=float, default=300.0, show_default=True, help="Seconds to wait for sign-in."
)
@click.option("--ca-bundle", default=None, metavar="PEM", help="Trust this CA bundle for sign-in.")
@click.option("--insecure", is_flag=True, help="Skip TLS certificate checks (unsafe).")
@click.pass_context
def add_command(
    ctx: click.Context,
    provider: str,
    kind: str | None,
    label: str | None,
    priority: int | None,
    api_key: str | None,
    no_browser: bool,
    timeout: float,
    ca_bundle: str | None,
    insecure: bool,
) -> None:
    """Add an API key or sign in, as a new credential in PROVIDER's pool."""
    env = _env_from(ctx)
    with _guarded():
        profile = _provider(env, provider)
        _no_edit_on_inherited(env, profile)
        flows = flows_for(profile.name)
        wanted = (kind or "").lower().replace("-", "_") or (
            "oauth" if flows.oauth_default else "api_key"
        )
        if wanted == "oauth" and api_key:
            _fail("--api-key goes with --type api-key, not oauth.")
        if wanted == "api_key":
            _add_api_key(env, profile, key=api_key, label=label, priority=priority)
            return
        if not flows.any:
            _fail(
                f"{profile.name} publishes no sign-in flow Indus Agent can use; add an API key "
                "instead.",
                f"indus auth add {profile.name} --api-key KEY",
            )
        options = _FlowOptions(no_browser, timeout, ca_bundle, insecure)
        try:
            _add_signed_in(env, profile, flows, options, label, priority)
        except AuthError as exc:
            _fail(f"Sign-in failed: {exc}")


# -- list / remove / reset / priority ----------------------------------------------------------


def _list(env: _Env, provider: str | None) -> None:
    now = env.scope.clock.now()
    if provider is not None:
        profiles = [_provider(env, provider)]
    else:
        stored = set(env.store.read().get("pool", {})) | set(
            env.store.read_fallback().get("pool", {})
        )
        profiles = list(env.registry.list_providers())
        listed = {p.name for p in profiles}
        for name in sorted(stored - listed):
            found = env.registry.lookup(name)
            if found is not None and found.name not in listed:
                profiles.append(found)
                listed.add(found.name)
    shown = 0
    for profile in profiles:
        pool, inherited = _pool(env, profile)
        if not pool.entries:
            continue
        if shown:
            _say("")
        _print_pool(pool, _strategy(env, profile.name), now, inherited=inherited)
        shown += 1
    if not shown:
        what = f"for {profiles[0].name} " if provider is not None else ""
        _say(f"No credentials {what}yet. Add one with `indus auth add PROVIDER`.")


@auth_group.command("list")
@click.argument("provider", required=False)
@click.pass_context
def list_command(ctx: click.Context, provider: str | None) -> None:
    """Show each credential pool (or just PROVIDER's)."""
    env = _env_from(ctx)
    with _guarded():
        _list(env, provider)


def _remove(env: _Env, profile: ProviderProfile, target: str) -> None:
    _no_edit_on_inherited(env, profile)

    def change(pool: Pool, doc: dict[str, Any]) -> tuple[int, PoolEntry]:
        index, entry = pool.remove(target)
        if entry.source == "env" and entry.env_var:
            suppress_env(doc, profile.name, entry.env_var)
        return index, entry

    index, removed = _edit(env, profile, change)
    if removed.stores_secret:
        env.secrets.delete(secret_name(profile.name, removed.id))
    _say(f'Removed {profile.name} credential #{index} ("{removed.label}").')
    if removed.source == "env":
        _say(
            f"It comes from {removed.env_var}, which is still set, so it is hidden from the pool "
            "until you add another credential here."
        )
        _say(f"To delete the key itself: indus config unset {removed.env_var}")


@auth_group.command("remove")
@click.argument("provider")
@click.argument("target")
@click.pass_context
def remove_command(ctx: click.Context, provider: str, target: str) -> None:
    """Remove a credential by index, id or label (its stored secret too)."""
    env = _env_from(ctx)
    with _guarded():
        _remove(env, _provider(env, provider), target)


def _reset(env: _Env, profile: ProviderProfile, target: str | None) -> None:
    _no_edit_on_inherited(env, profile)
    cleared = _edit(env, profile, lambda pool, _doc: pool.reset(target))
    if target is None:
        _say(f"Cleared the status of {len(cleared)} {profile.name} credential(s).")
    else:
        _say(f'Cleared the status of {profile.name} credential "{cleared[0].label}".')


@auth_group.command("reset")
@click.argument("provider")
@click.argument("target", required=False)
@click.pass_context
def reset_command(ctx: click.Context, provider: str, target: str | None) -> None:
    """Put benched credentials back in rotation (all of PROVIDER's, or one)."""
    env = _env_from(ctx)
    with _guarded():
        _reset(env, _provider(env, provider), target)


@auth_group.command("priority")
@click.argument("provider")
@click.argument("target")
@click.argument("priority", type=int)
@click.pass_context
def priority_command(ctx: click.Context, provider: str, target: str, priority: int) -> None:
    """Move a credential to PRIORITY (0 = tried first); the others shift."""
    env = _env_from(ctx)
    with _guarded():
        profile = _provider(env, provider)
        _no_edit_on_inherited(env, profile)
        moved = _edit(env, profile, lambda pool, _doc: pool.set_priority(target, priority))
        _say(f'Moved {profile.name} credential "{moved.label}" to priority {moved.priority}.')
        if moved.priority != priority:
            _say(f"(Asked for {priority}; the pool has no position beyond {moved.priority}.)")
        strategy = _strategy(env, profile.name)
        if strategy != "fill_first":
            _say(f"Note: {profile.name} uses {strategy}, which does not follow priority order.")


# -- refresh / status / logout -----------------------------------------------------------------


@auth_group.command("refresh")
@click.argument("provider")
@click.argument("target", required=False)
@click.pass_context
def refresh_command(ctx: click.Context, provider: str, target: str | None) -> None:
    """Renew a signed-in credential's tokens and put it back in rotation."""
    env = _env_from(ctx)
    with _guarded():
        profile = _provider(env, provider)
        _no_edit_on_inherited(env, profile)
        pool, _ = _pool(env, profile)
        if target is None:
            if len(pool) > 1:
                _fail(
                    f"{profile.name} has {len(pool)} credentials; say which one to refresh.",
                    f"indus auth refresh {profile.name} <index|id|label>",
                )
            if not pool.entries:
                _fail(f"{profile.name} has no credentials to refresh.")
            target = pool.entries[0].id
        index, entry = pool.resolve(target)
        if entry.auth_type != "oauth":
            _fail(
                f'{profile.name} credential #{index} ("{entry.label}") is an API key; it '
                "has nothing to refresh."
            )
        spec = _refresh_spec(flows_for(profile.name))
        stored = env.secrets.get(secret_name(profile.name, entry.id))
        if spec is None or not stored:
            _fail(
                f'{profile.name} credential #{index} ("{entry.label}") cannot be refreshed here; '
                "sign in again.",
                f"indus auth add {profile.name} --type oauth",
            )
        try:
            tokens = device_code.TokenSet.from_secret(stored)
            fresh = run_refresh(env, spec, tokens)
        except ValueError:
            _fail(
                "The stored sign-in is damaged; sign in again.",
                f"indus auth add {profile.name} --type oauth",
            )
        except device_code.ReloginRequired as exc:
            _fail(f"{exc}.", f"indus auth add {profile.name} --type oauth")
        except AuthError as exc:
            _fail(f"Refresh failed: {exc}")
        env.secrets.set(secret_name(profile.name, entry.id), fresh.to_secret())
        expires = fresh.expires_at.isoformat() if fresh.expires_at else None

        def change(pool: Pool, _doc: dict[str, Any]) -> None:
            _, row = pool.resolve(entry.id)
            row.expires_at = expires
            pool.reset(entry.id)

        _edit(env, profile, change)
        _say(
            f'Refreshed {profile.name} credential #{index} ("{entry.label}"); it is back in '
            "rotation."
        )


@auth_group.command("status")
@click.argument("provider")
@click.pass_context
def status_command(ctx: click.Context, provider: str) -> None:
    """Summarise PROVIDER: credentials, strategy, sign-in and what would be used next."""
    env = _env_from(ctx)
    with _guarded():
        profile = _provider(env, provider)
        now = env.scope.clock.now()
        pool, inherited = _pool(env, profile)
        strategy = _strategy(env, profile.name)
        state, state_inherited = env.store.provider_state(profile.name)
        active = env.store.read().get("active_provider")
        flows = flows_for(profile.name)
        _say(f"{profile.label} ({profile.name})")
        count = len(pool)
        origin = " (from the default profile)" if inherited else ""
        _say(f"  credentials: {count}{origin}, strategy {strategy}")
        nxt = pool.peek(strategy, now)
        if nxt is not None:
            _say(f'  next to use: "{nxt.label}" ({nxt.auth_type}, {nxt.source})')
        elif count:
            _say("  next to use: none (every credential is benched)")
        if state is not None:
            since = state.get("signed_in_at")
            where = " (default profile)" if state_inherited else ""
            _say(f"  signed in{where}: {since or 'yes'}")
        _say(f"  active sign-in provider: {'yes' if active == profile.name else 'no'}")
        kinds = [k for k, v in (("browser (PKCE)", flows.pkce), ("device code", flows.device)) if v]
        _say(f"  sign-in flows: {', '.join(kinds) if kinds else 'none (API keys only)'}")
        configured = str(_config_get(env.scope, "model.provider") or "")
        if configured:
            _say(f"  model.provider: {configured}")


@auth_group.command("logout")
@click.argument("provider")
@click.pass_context
def logout_command(ctx: click.Context, provider: str) -> None:
    """Forget everything stored for PROVIDER (credentials and sign-in state)."""
    env = _env_from(ctx)
    with _guarded():
        profile = _provider(env, provider)
        removed: list[PoolEntry] = []

        def change(pool: Pool, doc: dict[str, Any]) -> None:
            keep = [e for e in pool.entries if e.source == "env"]
            removed.extend(e for e in pool.entries if e.source != "env")
            pool.entries[:] = keep
            providers = doc.get("providers")
            if isinstance(providers, dict):
                cast(dict[str, Any], providers).pop(profile.name, None)
            if doc.get("active_provider") == profile.name:
                doc.pop("active_provider", None)

        _edit(env, profile, change)
        for entry in removed:
            env.secrets.delete(secret_name(profile.name, entry.id))
        if removed:
            _say(f"Signed out of {profile.name}: removed {len(removed)} stored credential(s).")
        else:
            _say(f"Nothing was stored for {profile.name}.")
        _reset_model_provider(env, profile)
        for entry in env.env_rows(profile):
            _say(
                f"{entry.env_var} still holds a key for {profile.name}; remove it with "
                f"`indus config unset {entry.env_var}` if you meant to."
            )


def _reset_model_provider(env: _Env, profile: ProviderProfile) -> None:
    view = _config_view(env.scope)
    configured = str(view.get("model.provider") or "")
    if not configured:
        return
    named = env.registry.lookup(configured)
    if (named.name if named is not None else configured.lower()) != profile.name:
        return
    raw = view.user_raw()
    if not config_keys.unset_path(raw, "model.provider"):
        return
    try:
        config_mod.save(env.scope, raw, replace=True)
    except ConfigError as exc:
        _fail(f"model.provider still names {profile.name}: {exc}")
    _say(f"model.provider was {configured}; it is now unset (Indus Agent picks from your keys).")
