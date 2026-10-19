"""The terminal step's backend list and the questions each cloud backend needs (P21-13).

``DESCRIPTIONS`` gives every built-in backend a one-line description for the picker. After the
pick, ``configure`` asks only what that backend needs: the Modal token pair, the Daytona API key,
or the Vercel token with its project and team ids. Answers go to the profile's secret store (the
OS keychain or its owner-only file), never to ``.env``, and are never printed; a field that is
already set is kept unless the user asks to replace it.

Without a terminal, ``apply_non_interactive`` takes the same names from the environment
(``indus setup --non-interactive --terminal-backend NAME``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

import click

from indus.cli.prompts import ask_secret, ask_text, ask_yes_no

if TYPE_CHECKING:
    from indus.cli.setup.sections import SetupContext
    from indus.foundation.scope import Scope
    from indus.foundation.secrets import SecretStore

__all__ = [
    "DESCRIPTIONS",
    "NEEDS",
    "Need",
    "apply_non_interactive",
    "configure",
    "label",
]

DESCRIPTIONS: Mapping[str, str] = {
    "local": "on this machine, in your own shell",
    "docker": "in a container on this machine (Docker or Podman)",
    "ssh": "on another machine you reach over SSH",
    "singularity": "in an Apptainer/SingularityCE instance (HPC clusters, no root daemon)",
    "modal": "in a Modal cloud sandbox, billed to your own Modal account",
    "daytona": "in a Daytona cloud sandbox that keeps its files when stopped",
    "vercel_sandbox": "in a Vercel cloud microVM; files kept through snapshots",
}


@dataclass(frozen=True, slots=True)
class Need:
    """One value a backend needs: its variable name, the question, whether it is a secret."""

    name: str
    question: str
    secret: bool = True


# The first sign-in set of each backend (``vendor_env.REQUIRED_SETS``), with our questions. The
# Vercel OIDC token is short-lived, so setup never stores it.
NEEDS: Mapping[str, tuple[Need, ...]] = {
    "modal": (
        Need("MODAL_TOKEN_ID", "Modal token id"),
        Need("MODAL_TOKEN_SECRET", "Modal token secret"),
    ),
    "daytona": (Need("DAYTONA_API_KEY", "Daytona API key"),),
    "vercel_sandbox": (
        Need("VERCEL_TOKEN", "Vercel access token"),
        Need("VERCEL_PROJECT_ID", "Vercel project id", secret=False),
        Need("VERCEL_TEAM_ID", "Vercel team id", secret=False),
    ),
}


def label(name: str) -> str:
    """The picker row for backend ``name``."""
    text = DESCRIPTIONS.get(name)
    return f"{name} — {text}" if text else name


def _store(scope: Scope) -> SecretStore:
    from indus.cli.commands._support import secret_store

    return secret_store(scope)


def _where(store: SecretStore) -> str:
    return "the OS keychain" if store.backend_name == "keychain" else "the profile's secret file"


def _stored(store: SecretStore, name: str) -> bool:
    try:
        return bool(store.get(name))
    except Exception:
        return False


def _extra_line(backend: str) -> str | None:
    from indus.backends.sdk_entries import extra_missing

    problem = extra_missing(backend)
    if problem is None:
        return None
    return f"{problem}; the backend stays unavailable until it is installed."


def configure(ctx: SetupContext, backend: str) -> None:
    """Ask what ``backend`` needs and store it (nothing to ask for local, docker and ssh)."""
    say = ctx.io.say
    if backend == "singularity":
        from indus.backends.singularity import NOT_FOUND_REASON, find_cli

        found = find_cli(ctx.scope)
        say(f"Container CLI: {found}" if found else f"{NOT_FOUND_REASON} on PATH yet.")
        return
    needs = NEEDS.get(backend)
    if not needs:
        return
    missing = _extra_line(backend)
    if missing is not None:
        say(missing)
    store = _store(ctx.scope)
    say(f"Credentials are kept in {_where(store)}; they are never written to .env or shown.")
    for need in needs:
        if _stored(store, need.name):
            if not ask_yes_no(ctx.io, f"{need.name} is already stored. Replace it?", False):
                continue
        elif ctx.scope.env.get(need.name):
            say(f"{need.name} is set in the environment; storing a value here takes precedence.")
        value = (
            ask_secret(ctx.io, need.question) if need.secret else ask_text(ctx.io, need.question)
        )
        value = value.strip()
        if not value:
            say(f"{need.name}: skipped.")
            continue
        store.set(need.name, value)
        say(f"{need.name}: saved.")


def apply_non_interactive(scope: Scope, backend: str, env: Mapping[str, str]) -> int:
    """Select ``backend`` with credentials from ``env``; returns the exit code (see the notes)."""
    from indus.backends.registry import default_entries
    from indus.foundation import config

    names = [entry.name for entry in default_entries()]
    chosen = backend.strip().lower()
    if chosen not in names:
        click.echo(
            f"indus setup: there is no terminal backend called {backend!r} "
            f"(available: {', '.join(names)}).",
            err=True,
        )
        return 2
    needs = NEEDS.get(chosen, ())
    if needs:
        from indus.backends.vendor_env import vendor_credentials

        store = _store(scope)
        for need in needs:
            value = (env.get(need.name) or "").strip()
            if value:
                store.set(need.name, value)
                click.echo(f"Stored {need.name} from the environment in {_where(store)}.")
        found = vendor_credentials(scope.with_secrets(store).with_env(scope.env), chosen)
        if found.missing:
            click.echo(
                f"indus setup: the {chosen} backend still needs {', '.join(found.missing)}; "
                "set them in the environment and run this again. terminal.backend is unchanged.",
                err=True,
            )
            return 1
        missing = _extra_line(chosen)
        if missing is not None:
            click.echo(missing)
    config.save(scope, {"terminal": {"backend": chosen}})
    click.echo(f"Commands run on: {chosen}. Check it with `indus doctor`.")
    return 0
