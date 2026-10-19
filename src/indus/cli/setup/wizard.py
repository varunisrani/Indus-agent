"""The setup wizard: guards, the mode choice, the sections, the summary.

Order of the guards: a managed install refuses; the home is created; the settings file is copied
to a ``pre-setup`` backup **before** ``--reset`` replaces it; without a terminal (or with
``--non-interactive``) the guidance is printed and the run ends with 0.

Then one of: a single section (``indus setup tools``); ``--quick`` on a set-up install (only the
missing parts); the full wizard on a set-up install (every question defaults to the current
value); or a first install, which starts by choosing Full or Blank slate. Ctrl+C ends the run
with "Setup cancelled. Sections not reached were left unchanged." (each section saves its own
part when it ends).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import click

from indus.cli.model_picker import PickerDeps
from indus.cli.prompts import Cancelled, Interactive, ask_choice, run_steps
from indus.cli.setup.sections import SECTIONS, Mode, PlatformToolsModel, SetupContext, ToolsModel

if TYPE_CHECKING:
    from indus.cli.args import CliState
    from indus.foundation.scope import Scope

__all__ = [
    "CANCELLED",
    "SetupDeps",
    "SetupOptions",
    "non_interactive_guidance",
    "run_setup",
]

CANCELLED = "Setup cancelled. Sections not reached were left unchanged."
CANCEL_EXIT = 1
FIRST_MODES: tuple[tuple[Mode, str], ...] = (
    ("full", "Full setup: go through every part, with the usual capabilities on"),
    ("blank", "Blank slate: only the minimum on; switch on each capability yourself"),
)


def non_interactive_guidance() -> str:
    """What to do when nobody can answer questions (also printed by the first-run guard)."""
    return "\n".join(
        (
            "Setup asks questions, so it needs a terminal. To set things up from a script:",
            "  indus config set model.provider custom",
            "  indus config set model.base_url http://127.0.0.1:8000/v1",
            "  indus config set model.default <model id>",
            "or keep a hosted provider's key in the keychain and pick its model:",
            "  indus auth add openrouter",
            "  indus config set model.provider openrouter",
            "  indus config set model.default <model id>",
            "Check the result with `indus doctor`; run `indus setup` in a terminal for the "
            "guided setup.",
        )
    )


@dataclass(frozen=True)
class SetupOptions:
    section: str | None = None
    non_interactive: bool = False
    reset: bool = False
    quick: bool = False
    terminal_backend: str | None = None  # select it from env credentials, never asking (P21-13)


@dataclass
class SetupDeps:
    """Seams for tests: the terminal, the picker's HTTP client, the tools model."""

    io: Interactive | None = None
    picker: PickerDeps = field(default_factory=PickerDeps)
    tools: ToolsModel = field(default_factory=PlatformToolsModel)


def _err(line: str) -> None:
    click.echo(line, err=True)


def _reset(scope: Scope) -> None:
    """Replace the settings file with a fresh one (the commented example, no values)."""
    from indus.foundation import config

    path = config.config_path(scope)
    path.unlink(missing_ok=True)
    config.save(scope, {})


def _is_set_up(scope: Scope) -> bool:
    from indus.cli.first_run import usable_provider
    from indus.foundation import config

    if config.load(scope).get("onboarding.setup_completed") is True:
        return True
    return usable_provider(scope) is not None


def _model_missing(scope: Scope) -> bool:
    from indus.cli.first_run import usable_provider
    from indus.foundation import config

    if not str(config.load(scope).get("model.default") or "").strip():
        return True
    return usable_provider(scope) is None


def _record_completed(scope: Scope) -> None:
    from indus.foundation import config

    config.save(scope, {"onboarding": {"setup_completed": True}})


def _steps(ctx: SetupContext, names: list[str]) -> list[tuple[str, Callable[[], None]]]:
    out: list[tuple[str, Callable[[], None]]] = []
    for name in names:
        title, run = SECTIONS[name]
        out.append((title, (lambda run=run: run(ctx))))  # bind each section now
    return out


def _summary(ctx: SetupContext, backup: Path | None) -> None:
    from indus.foundation import config
    from indus.foundation.envload import env_file_path
    from indus.foundation.secrets import SecretStore

    scope = ctx.scope
    view = config.load(scope)
    path = config.config_path(scope)
    store = scope.secrets
    if isinstance(store, SecretStore) and store.backend_name == "keychain":
        keys = "the OS keychain"
    else:
        keys = "an owner-only file in the profile home"
    tools = view.get("platform_toolsets.cli")
    say = ctx.io.say
    say("")
    say("Setup complete.")
    say(f"  Settings:   {path}")
    say(f"  Keys:       {keys} (also read: {env_file_path(scope.home)})")
    say(f"  Home:       {scope.home}")
    model = view.get("model.default") or "(not set)"
    provider = view.get("model.provider") or "detected from your keys"
    say(f"  Model:      {model} on {provider}")
    say(f"  Commands:   run on {view.get('terminal.backend') or 'local'}")
    tools_text = ", ".join(str(t) for t in tools) if isinstance(tools, list) else "the usual set"  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    say(f"  Tools:      {tools_text}")
    say(f"  Approvals:  {view.get('approvals.mode') or 'manual'}")
    say("  Gateway:    not set up (arrives in a later release)")
    if backup is not None and backup.exists():
        say(f"Your earlier settings were saved to {backup}.")
        say(f"To undo this setup:  cp {backup} {path}")
    say("Next: run `indus` to start chatting; `indus doctor` checks the setup.")


def run_setup(state: CliState, options: SetupOptions, *, deps: SetupDeps | None = None) -> int:
    """Run ``indus setup``; returns the exit code."""
    from indus.foundation import config
    from indus.foundation.config import backups
    from indus.foundation.errors import IndusError
    from indus.foundation.home import is_managed
    from indus.foundation.layout import ensure_home

    deps = deps or SetupDeps()
    scope = state.scope
    if is_managed(scope.home, scope.env):
        _err(
            "This installation is managed by a package manager or an administrator, so setup "
            "cannot change it. Change settings the way it is managed."
        )
        return 1
    if options.section is not None and options.section not in SECTIONS:
        _err(f"indus setup: there is no section called {options.section!r}.")
        _err("Sections: " + ", ".join(SECTIONS))
        return 2
    try:
        ensure_home(scope)
        backup = backups.backup_config(config.config_path(scope), "pre-setup", clock=scope.clock)
        if options.reset:
            _reset(scope)
            click.echo("Settings reset to the built-in values.")
            if backup is not None:
                click.echo(f"Your earlier settings were saved to {backup}.")
    except IndusError as exc:
        _err(f"indus setup: {exc.message}")
        return 1
    if options.terminal_backend is not None:
        from indus.cli.setup.remote_backends import apply_non_interactive

        return apply_non_interactive(scope, options.terminal_backend, scope.env)
    io = deps.io or Interactive.for_terminal(
        stdin_isatty=state.stdin_isatty,
        stdout_isatty=state.stdout_isatty,
        env=scope.env,
        guidance=non_interactive_guidance(),
    )
    if options.non_interactive or not io.tty:
        if not io.tty:
            click.echo("No terminal was found, so setup will not ask questions.")
        click.echo(non_interactive_guidance())
        return 0
    try:
        return _interactive(scope, io, options, deps, backup)
    except Cancelled:
        io.say("")
        io.say(CANCELLED)
        return CANCEL_EXIT
    except IndusError as exc:
        _err(f"indus setup: {exc.message}")
        if exc.hint:
            _err(exc.hint)
        return 1


def _interactive(
    scope: Scope, io: Interactive, options: SetupOptions, deps: SetupDeps, backup: Path | None
) -> int:
    ctx = SetupContext(scope, io, "reconfigure", deps.picker, deps.tools)
    if options.section is not None:
        title, _run = SECTIONS[options.section]
        io.say(f"Indus Agent setup: {title}")
        run_steps(io, _steps(ctx, [options.section]))
        io.say(f"{title}: done.")
        return 0
    io.say("Indus Agent setup. Press Ctrl+C at any time to stop; the left arrow goes back.")
    if _is_set_up(scope):
        if options.quick:
            if not _model_missing(scope):
                io.say("Nothing is missing. Run `indus setup` without --quick to change things.")
                _record_completed(scope)
                return 0
            run_steps(io, _steps(ctx, ["model"]))
            _record_completed(scope)
            io.say("The missing parts are set up.")
            return 0
        io.say("Indus Agent is already set up. Each question shows the current value;")
        io.say("press Enter to keep it.")
        run_steps(io, _steps(ctx, list(SECTIONS)))
    else:
        if options.quick:
            io.say("Nothing is set up yet, so the full first-time setup runs.")

        def choose_mode() -> None:
            labels = [label for _, label in FIRST_MODES]
            ctx.mode = FIRST_MODES[ask_choice(io, "How do you want to start?", labels, 0)][0]

        run_steps(io, [("the setup mode", choose_mode), *_steps(ctx, list(SECTIONS))])
    _record_completed(scope)
    _summary(ctx, backup)
    return 0
