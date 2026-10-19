"""``indus moa``: mixture-of-agents presets (P10-22, I28).

``list`` (the default, alias ``ls``) shows every preset: its reference models, the aggregator
when one is named, the timeout and the fan-out, which one is the default, and any problems found
in the ``moa`` settings. ``configure [NAME]`` builds a preset — on a terminal with a picker over
the providers that can be used here (a stored key or a configured endpoint; never a route that
would only impersonate one), else from flags (``--reference PROVIDER:MODEL``, repeatable). Every
problem is listed before anything is written, and nothing with a problem is saved. ``delete NAME``
removes a preset, but never the default one. Writes go through the round-trip config writer, so
comments and the order of the file survive.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, cast

import click

from indus.cli.prompts import Cancelled, Interactive, ask_choice, ask_text, run_steps

if TYPE_CHECKING:
    from indus.cli.args import CliState
    from indus.core.moa_presets import Preset, Slot
    from indus.foundation.scope import Scope

__all__ = ["moa_group"]

_SETTINGS = {"help_option_names": ["-h", "--help"], "max_content_width": 100}
_ALIASES = {"ls": "list", "rm": "delete"}
DEFAULT_NAME = "default"
NEEDS_TERMINAL = (
    "Without a terminal, name the models: indus moa configure NAME "
    "--reference PROVIDER:MODEL [--reference PROVIDER:MODEL ...] [--aggregator PROVIDER:MODEL]"
)
_DONE_ROW = "Done"


class _MoaGroup(click.Group):
    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        return super().get_command(ctx, _ALIASES.get(cmd_name, cmd_name))


def _state(ctx: click.Context) -> CliState:
    from indus.cli.args import CliState

    state = ctx.find_object(CliState)
    if state is None:
        raise click.UsageError("moa was started without its process state")
    return state


def make_io(state: CliState, guidance: str) -> Interactive:
    return Interactive.for_terminal(
        stdin_isatty=state.stdin_isatty,
        stdout_isatty=state.stdout_isatty,
        env=state.scope.env,
        guidance=guidance,
    )


io_factory: Callable[[CliState, str], Interactive] = make_io


def _raw_moa(scope: Scope) -> tuple[Any, dict[str, Any]]:
    """The loaded view and the user's own ``moa`` section (a fresh dict)."""
    from indus.foundation import config

    view = config.load(scope)
    raw = view.user_raw()
    section = raw.get("moa")
    return view, dict(cast(Mapping[str, Any], section)) if isinstance(section, Mapping) else {}


def _save_moa(scope: Scope, section: Mapping[str, Any]) -> None:
    from indus.foundation import config

    raw = config.load(scope).user_raw()
    raw["moa"] = dict(section)
    config.save(scope, raw, replace=True)


def _describe_slot(slot: Slot) -> str:
    return slot.label + ("" if slot.enabled else "  (disabled)")


def _show(scope: Scope) -> int:
    from indus.core.moa_presets import known_provider_check, validate_section

    _view, section = _raw_moa(scope)
    settings, problems = validate_section(section, known_provider=known_provider_check(scope))
    if not settings.presets and not problems:
        click.echo("No mixture-of-agents presets yet. Make one with: indus moa configure NAME")
        return 0
    for name in sorted(settings.presets):
        preset = settings.presets[name]
        mark = "*" if name == settings.default_preset else " "
        click.echo(f"{mark} {name}")
        for i, slot in enumerate(preset.references, 1):
            click.echo(f"    reference {i}: {_describe_slot(slot)}")
        if preset.aggregator is not None:
            click.echo(f"    aggregator: {preset.aggregator.label}")
        timeout = settings.timeout_for(preset)
        click.echo(
            f"    timeout: {f'{timeout:g} s' if timeout else 'none'}, fan-out: {preset.fanout}"
        )
    if settings.default_preset:
        click.echo(f"Default preset (*): {settings.default_preset}")
    if settings.privacy_filter:
        click.echo(f"Privacy filter: {settings.privacy_filter}")
    if problems:
        click.echo("Problems in the moa settings (those presets are not used):")
        for problem in problems:
            click.echo(f"  - {problem}")
    return 0


@click.group(
    "moa",
    cls=_MoaGroup,
    invoke_without_command=True,
    help="Mixture-of-agents presets (list, configure, delete).",
    context_settings=_SETTINGS,
)
@click.pass_context
def moa_group(ctx: click.Context) -> int:
    if ctx.invoked_subcommand is None:
        return _show(_state(ctx).scope)
    return 0


@moa_group.command("list", help="Show the presets (the default; alias ls).")
@click.pass_context
def list_command(ctx: click.Context) -> int:
    return _show(_state(ctx).scope)


# -- configure -------------------------------------------------------------------------------------


def usable_providers(scope: Scope) -> list[str]:
    """Providers a reference can be sent to from here: ones whose route resolves (a key is
    stored, or the endpoint is configured)."""
    from indus.providers import route as routes
    from indus.providers.registry import Registry

    registry = Registry(scope)
    usable: list[str] = []
    for profile in registry.list_providers():
        try:
            routes.resolve(scope, provider=profile.name, model="probe", registry=registry)
        except Exception:
            continue
        usable.append(profile.name)
    return usable


def _pick_slots(
    scope: Scope, io: Interactive
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    providers = usable_providers(scope)
    if not providers:
        raise click.ClickException("no provider can be used here yet; run `indus model` first")
    refs: list[dict[str, Any]] = []
    aggregator: list[dict[str, Any] | None] = [None]

    def references() -> None:
        refs.clear()
        while True:
            rows = [*providers, _DONE_ROW] if refs else list(providers)
            at = ask_choice(io, f"Reference model {len(refs) + 1}: which provider?", rows, 0)
            if at >= len(providers):
                return
            model = ask_text(io, f"Model id on {providers[at]}").strip()
            if model:
                refs.append({"provider": providers[at], "model": model, "enabled": True})

    def pick_aggregator() -> None:
        rows = ["None: the session's own model aggregates", *providers]
        at = ask_choice(io, "Aggregator (shown in the list only)?", rows, 0)
        if at == 0:
            aggregator[0] = None
            return
        model = ask_text(io, f"Model id on {providers[at - 1]}").strip()
        aggregator[0] = {"provider": providers[at - 1], "model": model} if model else None

    run_steps(io, [("the reference models", references), ("the aggregator", pick_aggregator)])
    return refs, aggregator[0]


def _slot_entry(text: str, what: str) -> dict[str, Any]:
    from indus.core.moa_presets import parse_slot_text

    slot = parse_slot_text(text)
    if slot is None:
        raise click.UsageError(f"{what} {text!r} is not PROVIDER:MODEL")
    return slot.to_config()


def _validated(scope: Scope, name: str, entry: Mapping[str, Any], others: list[str]) -> Preset:
    from indus.core.moa_presets import known_provider_check, validate_preset

    preset, problems = validate_preset(
        name, entry, known_provider=known_provider_check(scope), preset_names=[*others, name]
    )
    if problems or preset is None:
        click.echo(f"indus moa: preset {name!r} was not saved:", err=True)
        for problem in problems:
            click.echo(f"  - {problem}", err=True)
        raise click.exceptions.Exit(1)
    return preset


@moa_group.command("configure", help="Create or replace a preset (picker, or flags).")
@click.argument("name", required=False)
@click.option(
    "--reference",
    "references",
    multiple=True,
    metavar="PROVIDER:MODEL",
    help="A reference model (repeat for more).",
)
@click.option("--aggregator", metavar="PROVIDER:MODEL", help="Shown with the preset.")
@click.option("--timeout", type=float, help="Seconds each reference may take (default: none).")
@click.option("--fanout", help="per_iteration (default), user_turn or every_n:N.")
@click.option("--default", "make_default", is_flag=True, help="Make it the default preset.")
@click.pass_context
def configure_command(
    ctx: click.Context,
    name: str | None,
    references: tuple[str, ...],
    aggregator: str | None,
    timeout: float | None,
    fanout: str | None,
    make_default: bool,
) -> int:
    from indus.core.moa_presets import preset_to_config

    state = _state(ctx)
    scope = state.scope
    _view, section = _raw_moa(scope)
    raw_presets = section.get("presets")
    presets: dict[str, Any] = (
        dict(cast(Mapping[str, Any], raw_presets)) if isinstance(raw_presets, Mapping) else {}
    )
    name = (name or str(section.get("default_preset") or "") or DEFAULT_NAME).strip()
    entry: dict[str, Any] = {}
    if references:
        entry["reference_models"] = [_slot_entry(t, "--reference") for t in references]
        if aggregator:
            entry["aggregator"] = _slot_entry(aggregator, "--aggregator")
    else:
        io = io_factory(state, NEEDS_TERMINAL)
        if not io.tty:
            click.echo(NEEDS_TERMINAL, err=True)
            return 1
        try:
            refs, picked = _pick_slots(scope, io)
        except Cancelled:
            click.echo("No change.")
            return 0
        entry["reference_models"] = refs
        if picked is not None:
            entry["aggregator"] = picked
    if timeout is not None:
        entry["reference_timeout"] = timeout
    if fanout:
        entry["fanout"] = fanout
    preset = _validated(scope, name, entry, [n for n in presets if n != name])
    replaced = name in presets
    presets[name] = preset_to_config(preset)
    section["presets"] = presets
    if make_default or not section.get("default_preset"):
        section["default_preset"] = name
    _save_moa(scope, section)
    click.echo(f"{'Replaced' if replaced else 'Saved'} MoA preset {name}:")
    for i, slot in enumerate(preset.references, 1):
        click.echo(f"  reference {i}: {_describe_slot(slot)}")
    if section.get("default_preset") == name:
        click.echo("It is the default preset: /moa <prompt> uses it.")
    return 0


@moa_group.command("delete", help="Remove a preset (not the default one; alias rm).")
@click.argument("name")
@click.pass_context
def delete_command(ctx: click.Context, name: str) -> int:
    scope = _state(ctx).scope
    _view, section = _raw_moa(scope)
    raw_presets = section.get("presets")
    presets: dict[str, Any] = (
        dict(cast(Mapping[str, Any], raw_presets)) if isinstance(raw_presets, Mapping) else {}
    )
    if name not in presets:
        click.echo(f"indus moa: no preset is called {name!r} (see indus moa list).", err=True)
        return 1
    if section.get("default_preset") == name:
        click.echo(
            f"indus moa: {name!r} is the default preset, so it stays. Make another one the "
            "default first (indus moa configure OTHER --default).",
            err=True,
        )
        return 1
    del presets[name]
    section["presets"] = presets
    _save_moa(scope, section)
    click.echo(f"Deleted MoA preset {name}.")
    return 0
