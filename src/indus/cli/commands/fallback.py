"""``indus fallback``: the routes tried, in order, when the main model fails.

The chain lives in ``fallback_providers`` as ``{provider, model}`` entries (a custom endpoint
also keeps its ``base_url``). ``list`` (default, alias ``ls``) shows the main route and the chain;
an entry missing its provider or model is shown as ignored. ``add`` runs the ``indus model``
picker without touching the main route, or takes ``PROVIDER MODEL``; a pick equal to the main
route or already in the chain changes nothing. ``remove`` (alias ``rm``) picks an entry or takes
its number. ``clear`` asks first unless ``-y``. Without a terminal, ``add`` / ``remove`` need
their arguments and ``clear`` needs ``-y``; otherwise they print what to type and exit 1.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

import click

from indus.cli.prompts import Cancelled, Interactive, ask_choice, ask_yes_no, run_steps

if TYPE_CHECKING:
    from indus.cli.args import CliState
    from indus.foundation.config.loader import ConfigView
    from indus.foundation.scope import Scope

__all__ = ["KEY", "fallback_command", "read_chain"]

KEY = "fallback_providers"
_ALIASES = {"ls": "list", "rm": "remove"}
_SETTINGS = {"help_option_names": ["-h", "--help"], "max_content_width": 100}

NEEDS_TERMINAL = {
    "add": "Without a terminal, name the route: indus fallback add PROVIDER MODEL",
    "remove": "Without a terminal, give the entry's number: indus fallback remove INDEX "
    "(see indus fallback list)",
    "clear": "Without a terminal, confirm with -y: indus fallback clear -y",
}


class _FallbackGroup(click.Group):
    """``ls``/``rm`` aliases and the shared unknown-command answer."""

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        return super().get_command(ctx, _ALIASES.get(cmd_name, cmd_name))

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        from indus.cli.unknown import unknown_command_lines

        word = args[0]
        if (
            self.get_command(ctx, word) is None
            and not ctx.resilient_parsing
            and not word.startswith("-")
        ):
            known = [*self.list_commands(ctx), *_ALIASES]
            for line in unknown_command_lines(ctx.command_path, word, known):
                click.echo(line, err=True)
            ctx.exit(2)
        return super().resolve_command(ctx, args)


def _state(ctx: click.Context) -> CliState:
    from indus.cli.args import CliState

    state = ctx.find_object(CliState)
    if state is None:
        raise click.UsageError("fallback was started without its process state")
    return state


def make_io(state: CliState, guidance: str) -> Interactive:
    """The terminal for questions (tests replace this)."""
    return Interactive.for_terminal(
        stdin_isatty=state.stdin_isatty,
        stdout_isatty=state.stdout_isatty,
        env=state.scope.env,
        guidance=guidance,
    )


io_factory: Callable[[CliState, str], Interactive] = make_io


def _view(scope: Scope) -> ConfigView:
    from indus.foundation import config

    return config.load(scope)


def _problem(entry: object) -> str | None:
    if not isinstance(entry, dict):
        return "not a {provider, model} entry"
    item = cast(dict[str, Any], entry)
    missing = [k for k in ("provider", "model") if not str(item.get(k) or "").strip()]
    return f"missing {' and '.join(missing)}" if missing else None


def read_chain(view: ConfigView) -> tuple[list[Any], str | None]:
    """The raw entries (malformed ones included) and a note when the value is not a list."""
    raw = view.get(KEY)
    if raw is None:
        return [], None
    if not isinstance(raw, list):
        return [], f"{KEY} is not a list, so it is ignored."
    return list(cast(list[Any], raw)), None


def _describe(entry: object) -> str:
    problem = _problem(entry)
    if problem is not None:
        return f"{entry!r}  (ignored: {problem})"
    item = cast(dict[str, Any], entry)
    text = f"{item['model']}  via {item['provider']}"
    return f"{text}  [{item['base_url']}]" if item.get("base_url") else text


def _count(n: int) -> str:
    return f"{n} entry" if n == 1 else f"{n} entries"


def _primary(view: ConfigView) -> dict[str, str] | None:
    from indus.providers.settings import model_section, text

    saved = model_section(view)
    provider, model = text(saved.get("provider")), text(saved.get("default"))
    if not model:
        return None
    return {"provider": provider.lower(), "model": model}


def _same(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return (
        str(a.get("provider", "")).lower() == str(b.get("provider", "")).lower()
        and str(a.get("model", "")) == str(b.get("model", ""))
        and str(a.get("base_url", "")).rstrip("/") == str(b.get("base_url", "")).rstrip("/")
    )


def _save(scope: Scope, chain: list[Any]) -> None:
    from indus.foundation import config

    config.save(scope, {KEY: chain})


def _show(scope: Scope) -> int:
    view = _view(scope)
    chain, note = read_chain(view)
    if note:
        click.echo(note)
    primary = _primary(view)
    if primary is not None:
        click.echo(f"Main route: {_describe(primary)}")
    if not chain:
        click.echo("No fallback routes are set. Add one with: indus fallback add")
        return 0
    click.echo(f"Fallback chain ({_count(len(chain))}), tried in order when the main route fails:")
    for i, entry in enumerate(chain, 1):
        click.echo(f"  {i}. {_describe(entry)}")
    ignored = sum(1 for entry in chain if _problem(entry) is not None)
    if ignored:
        click.echo(f"{_count(ignored)} cannot be used; fix or remove them (indus fallback remove).")
    return 0


@click.group(
    "fallback",
    cls=_FallbackGroup,
    invoke_without_command=True,
    help="Routes tried in order when the main model fails (list, add, remove, clear).",
    context_settings=_SETTINGS,
)
@click.pass_context
def fallback_command(ctx: click.Context) -> int:
    if ctx.invoked_subcommand is None:
        return _show(_state(ctx).scope)
    return 0


@fallback_command.command("list", help="Show the chain (the default; alias ls).")
@click.pass_context
def list_command(ctx: click.Context) -> int:
    return _show(_state(ctx).scope)


def _append(scope: Scope, entry: dict[str, Any]) -> int:
    view = _view(scope)
    primary = _primary(view)
    if primary is not None and _same(primary, entry):
        click.echo(f"{_describe(entry)} is the main route; it cannot be its own fallback.")
        click.echo("No change.")
        return 0
    chain, note = read_chain(view)
    if note:
        click.echo(f"{note} A new list replaces it.")
    if any(_problem(old) is None and _same(cast(dict[str, Any], old), entry) for old in chain):
        click.echo(f"{_describe(entry)} is already in the chain. No change.")
        return 0
    chain.append(entry)
    _save(scope, chain)
    click.echo(f"Added fallback: {_describe(entry)}")
    click.echo(f"The chain has {_count(len(chain))}.")
    return 0


@fallback_command.command(
    "add", help="Add a route: pick it like `indus model`, or name PROVIDER MODEL."
)
@click.argument("provider", required=False)
@click.argument("model", required=False)
@click.pass_context
def add_command(ctx: click.Context, provider: str | None, model: str | None) -> int:
    from indus.cli.model_picker import Selection, pick, store_key
    from indus.foundation.errors import IndusError
    from indus.providers.registry import Registry

    state = _state(ctx)
    scope = state.scope
    if provider is not None and model is None:
        raise click.UsageError("give both PROVIDER and MODEL, or neither to pick them")
    if provider is not None and model is not None:
        found = Registry(scope).lookup(provider)
        if found is None:
            click.echo(f"indus fallback: no provider is called {provider!r}.", err=True)
            click.echo("Run `indus model` on a terminal to see the providers.", err=True)
            return 1
        return _append(scope, {"provider": found.name, "model": model.strip()})
    io = io_factory(state, NEEDS_TERMINAL["add"])
    if not io.tty:
        click.echo(NEEDS_TERMINAL["add"], err=True)
        return 1
    chosen: list[Selection | None] = []

    def step() -> None:
        chosen[:] = [pick(scope, io, for_fallback=True)]

    try:
        run_steps(io, [("the provider list", step)])
    except Cancelled:
        chosen[:] = [None]
    selection = chosen[0] if chosen else None
    if selection is None:
        io.say("No fallback added.")
        return 0
    try:
        stored = store_key(scope, selection)
    except IndusError as exc:
        click.echo(f"indus fallback: {exc.message}", err=True)
        return 1
    if stored:
        io.say(stored)
    return _append(scope, dict(selection.entry()))


@fallback_command.command("remove", help="Remove an entry: pick it, or give its number (alias rm).")
@click.argument("index", required=False)
@click.pass_context
def remove_command(ctx: click.Context, index: str | None) -> int:
    state = _state(ctx)
    scope = state.scope
    chain, _note = read_chain(_view(scope))
    if not chain:
        click.echo("No fallback routes are set; nothing to remove.")
        return 0
    if index is None:
        io = io_factory(state, NEEDS_TERMINAL["remove"])
        if not io.tty:
            click.echo(NEEDS_TERMINAL["remove"], err=True)
            return 1
        rows = [_describe(entry) for entry in chain] + ["Cancel"]
        try:
            at = ask_choice(io, "Remove which entry?", rows, 0)
        except Cancelled:
            at = len(chain)
        if at == len(chain):
            click.echo("No change.")
            return 0
    else:
        if not index.strip().isdigit() or not 1 <= int(index) <= len(chain):
            click.echo(
                f"indus fallback: {index!r} is not an entry number (1 to {len(chain)}).", err=True
            )
            return 1
        at = int(index) - 1
    removed = chain.pop(at)
    _save(scope, chain)
    click.echo(f"Removed fallback: {_describe(removed)}")
    click.echo(f"The chain has {_count(len(chain))}." if chain else "The chain is now empty.")
    return 0


@fallback_command.command("clear", help="Remove every entry (asks first unless -y).")
@click.option("-y", "--yes", is_flag=True, help="Do not ask for confirmation.")
@click.pass_context
def clear_command(ctx: click.Context, yes: bool) -> int:
    state = _state(ctx)
    scope = state.scope
    chain, _note = read_chain(_view(scope))
    if not chain:
        click.echo("No fallback routes are set; nothing to clear.")
        return 0
    if not yes:
        io = io_factory(state, NEEDS_TERMINAL["clear"])
        if not io.tty:
            click.echo(NEEDS_TERMINAL["clear"], err=True)
            return 1
        for i, entry in enumerate(chain, 1):
            io.say(f"  {i}. {_describe(entry)}")
        try:
            confirmed = ask_yes_no(io, f"Remove all {_count(len(chain))}?", default=False)
        except Cancelled:
            confirmed = False
        if not confirmed:
            click.echo("No change.")
            return 0
    _save(scope, [])
    click.echo("The fallback chain is now empty.")
    return 0
