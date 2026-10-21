"""``indus plugins pack``: install, show and export plugin packs (P24-44).

- ``show <pack>``: the review screen only (a dry run);
- ``install <pack> [--force]``: the review screen, one question, then each plugin through the
  normal install (pinned at its ref), its env questions, enable and its own capability consent.
  There is no ``--yes``: without a terminal the install is refused after the screen (exit 1).
  ``--force`` only lets a ``caution`` scan through (never ``dangerous``). Exit 1 when any plugin
  failed; the others stay installed;
- ``export [--name N] [--enabled-only] [-o FILE]``: a pack of the installed plugins that came
  from git (local folders are left out with a warning).

``<pack>`` is a local file or an ``https://`` URL.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import click

from indus.cli import prompts
from indus.cli.cmdkit import cli_state, err
from indus.cli.commands._support import scope_with_config
from indus.foundation import config
from indus.foundation.errors import IndusError
from indus.plugins import consent
from indus.plugins import packs as pack_mod
from indus.plugins.discovery import set_enabled
from indus.plugins.install import Installer, InstallResult
from indus.safety.content_scan import ScanReport, render_report

__all__ = ["pack_group"]


def _load(where: str) -> tuple[pack_mod.Pack, list[str], list[pack_mod.Resolved]]:
    scope = scope_with_config(cli_state().scope)
    try:
        pack, warnings = pack_mod.load_pack(where, scope=scope)
    except pack_mod.PackError as exc:
        err(f"Error: {exc}")
        raise click.exceptions.Exit(1) from None
    return pack, warnings, pack_mod.resolve(pack)


def _show(pack: pack_mod.Pack, warnings: list[str], resolved: list[pack_mod.Resolved]) -> None:
    for line in pack_mod.review_lines(pack, resolved, warnings):
        click.echo(line)


@click.group("pack")
def pack_group() -> None:
    """Install, show and export plugin packs."""


@pack_group.command("show")
@click.argument("where", metavar="PACK")
def show_command(where: str) -> None:
    """Show what a pack would install (nothing is installed)."""
    pack, warnings, resolved = _load(where)
    _show(pack, warnings, resolved)
    broken = [r for r in resolved if r.identifier is None]
    if broken:
        click.echo(f"{len(broken)} entr{'y' if len(broken) == 1 else 'ies'} cannot be installed.")
    click.echo("Dry run: `indus plugins pack install` installs it after one question.")


@pack_group.command("install")
@click.argument("where", metavar="PACK")
@click.option("--force", is_flag=True, help="Accept a caution scan (never a dangerous one).")
def install_command(where: str, force: bool) -> None:
    """Review a pack, then install every plugin in it at its pinned commit."""
    state = cli_state()
    pack, warnings, resolved = _load(where)
    _show(pack, warnings, resolved)
    io = prompts.io_for(state)
    if not io.tty:
        err(
            "A pack runs third-party code: installing one needs a terminal to review and confirm "
            "it (there is no --yes)."
        )
        raise click.exceptions.Exit(1)
    count = len(resolved)
    if not prompts.ask_yes_no(io, f"Install {count} plugin(s) from pack {pack.name!r}?", False):
        click.echo("Nothing installed.")
        raise click.exceptions.Exit(1)
    scope = scope_with_config(state.scope)

    def confirm_scan(report: ScanReport) -> bool:
        click.echo(render_report(report))
        return prompts.ask_yes_no(io, "The scan asks for caution. Install anyway?", False)

    def ask(text: str) -> bool:
        click.echo(text)
        return prompts.ask_yes_no(io, "Allow these?", default=False)

    def finish(result: InstallResult, seed: Mapping[str, Any] | None) -> str | None:
        manifest = result.manifest
        if manifest is None:
            return "it has no plugin.yaml or plugin.json, so it cannot be enabled"
        from indus.cli.commands.plugins import ask_env, dependencies_ok

        ask_env(scope, io, manifest)
        if not dependencies_ok(scope, io, manifest):
            return "its Python packages were not installed; it stays off"
        raw = config.load(scope).user_raw()
        set_enabled(raw, manifest.plugin_id, True)
        outcome = consent.consent_capabilities(raw, manifest, ask=ask)
        if outcome.refused:
            click.echo(f"  not granted: {', '.join(outcome.refused)}")
        if seed:
            for line in pack_mod.apply_seed(raw, manifest.plugin_id, seed):
                click.echo(f"  {line}")
        try:
            config.save(scope, raw, replace=True)
        except IndusError as exc:
            return f"settings not saved: {exc}"
        return None

    results = pack_mod.install_pack(
        pack,
        resolved,
        Installer(scope),
        finish=finish,
        force=force,
        confirm_scan=confirm_scan,
        say=click.echo,
    )
    ok = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    click.echo(f"Pack {pack.name!r}: {len(ok)} installed, {len(failed)} failed.")
    for row in failed:
        err(f"  ✗ {row.display}: {row.error}")
    if ok:
        click.echo("New sessions load them (a running gateway needs a restart).")
    if failed:
        raise click.exceptions.Exit(1)


@pack_group.command("export")
@click.option("--name", default="my-indus-pack", show_default=True, help="The pack's name.")
@click.option("--enabled-only", is_flag=True, help="Only plugins that are enabled.")
@click.option("--output", "-o", default="-", help="Write to this file (default: stdout).")
def export_command(name: str, enabled_only: bool, output: str) -> None:
    """Write a pack of the plugins installed from git."""
    scope = scope_with_config(cli_state().scope)
    raw = config.load(scope).user_raw()
    only: list[str] | None = None
    if enabled_only:
        plugins = raw.get("plugins")
        enabled = (
            cast(Mapping[str, Any], plugins).get("enabled")
            if isinstance(plugins, Mapping)
            else None
        )
        keys = [str(e) for e in cast(list[Any], enabled)] if isinstance(enabled, list) else []
        only = [*keys, *(k.rsplit("/", 1)[-1] for k in keys)]
    text, warnings = pack_mod.export_pack(scope, raw, name=name, only=only)
    for warning in warnings:
        err(f"warning: {warning}")
    if "repo:" not in text:
        err("Nothing to export: no plugin here was installed from git.")
        raise click.exceptions.Exit(1)
    if output == "-":
        click.echo(text, nl=False)
        return
    Path(output).expanduser().write_text(text, encoding="utf-8")
    click.echo(f"Pack written to {output}.")
