"""``indus plugins``: install, enable and inspect plugins (P13-23).

- ``list [--json]``: every plugin discovery sees, with state, kind, source (pin) and updates found
  by the last check;
- ``install <id> [--ref SHA] [--enable/--no-enable] [--force]``: fetch, scan and publish (P13-21),
  then the P13-22 questions in order: missing env values, "enable now?" (default no; a memory
  provider asks to become ``memory.provider`` instead), Python dependencies, capabilities;
- ``update [name|--all] [--force] [--trust-update-url]``, ``remove <name> [-y]``;
- ``enable`` / ``disable <name>``; ``info <name>``;
  ``capabilities <name> [--grant X] [--revoke X]``;
- ``check-updates``; ``validate <dir>`` (manifest + scan for authors; exit 1 when dangerous);
- ``pack install|show|export`` (``plugin_pack``, P24-44).

Without a terminal nothing is asked: an install stays disabled unless ``--enable`` and nothing
needs a question, a caution scan needs ``--force``, ``remove`` needs ``-y``, and no capability is
ever granted. ``plugin_lines`` and ``toggle_plugin`` are what a ``/plugins`` chat command calls.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

import click

from indus.cli import prompts
from indus.cli.args import CliState
from indus.cli.cmdkit import cli_state, err, group
from indus.cli.commands._support import scope_with_config, secret_store
from indus.foundation import config
from indus.foundation.errors import IndusError
from indus.foundation.scope import Scope
from indus.plugins import consent
from indus.plugins.discovery import Discovery, PluginRecord, discover, set_enabled
from indus.plugins.install import InstallBlocked, Installer, InstallError, forget_plugin
from indus.plugins.manifest import PluginManifest, read_manifest
from indus.safety.content_scan import ScanReport, render_report, scan_tree

__all__ = ["ask_env", "dependencies_ok", "plugin_lines", "plugins_group", "toggle_plugin"]

NOT_A_SANDBOX = (
    "Capabilities record what you allowed; they are not a sandbox. A plugin runs inside Indus "
    "with your rights."
)


# -- shared bits ----------------------------------------------------------------------------------


def _scope() -> Scope:
    return scope_with_config(cli_state().scope)


def _raw(scope: Scope) -> dict[str, Any]:
    return config.load(scope).user_raw()


def _save(scope: Scope, raw: Mapping[str, Any]) -> None:
    try:
        config.save(scope, raw, replace=True)
    except IndusError as exc:
        err(f"Not saved: {exc}")
        raise click.exceptions.Exit(1) from None


def _discover(scope: Scope) -> Discovery:
    return discover(scope_with_config(scope), project_root=Path.cwd())


def _find(scope: Scope, name: str) -> PluginRecord | None:
    found = _discover(scope)
    record = found.get(name)
    if record is not None:
        return record
    return next((r for r in found.records.values() if r.name == name), None)


def _need(scope: Scope, name: str) -> PluginRecord:
    record = _find(scope, name)
    if record is None:
        err(f"No plugin named {name!r}; see `indus plugins list`.")
        raise click.exceptions.Exit(1)
    return record


def _source_label(record: PluginRecord, installed: Mapping[str, Mapping[str, Any]]) -> str:
    meta = installed.get(record.name) if record.source == "user" else None
    if meta is None:
        return record.source
    if meta.get("kind") == "local":
        return "local"
    revision = str(meta.get("revision") or "")[:8]
    return f"git pinned@{revision}" if meta.get("pinned") is True else f"git@{revision}"


def _io(state: CliState) -> prompts.Interactive:
    return prompts.io_for(state)


def _asker(io: prompts.Interactive) -> Callable[[str], bool] | None:
    if not io.tty:
        return None

    def ask(text: str) -> bool:
        click.echo(text)
        return prompts.ask_yes_no(io, "Allow these?", default=False)

    return ask


def _confirm_scan(io: prompts.Interactive) -> Callable[[ScanReport], bool] | None:
    if not io.tty:
        return None

    def confirm(report: ScanReport) -> bool:
        click.echo(render_report(report))
        return prompts.ask_yes_no(
            io, "The scan asks for caution. Install anyway? Only if you trust the source.", False
        )

    return confirm


def plugin_lines(scope: Scope) -> list[str]:
    """One line per plugin: name, state, kind, source and a waiting update (``/plugins``)."""
    found = _discover(scope)
    tool = Installer(scope)
    try:
        installed = tool.records()
    except InstallError:
        installed = {}
    waiting = tool.last_check().get("available")
    available = cast(Mapping[str, Any], waiting) if isinstance(waiting, Mapping) else {}
    lines: list[str] = []
    for key, record in sorted(found.records.items()):
        update = "  update available" if record.name in available else ""
        lines.append(
            f"{key:<24} {record.state:<12} {record.kind:<10} "
            f"{_source_label(record, installed)}{update}"
        )
    return lines


def toggle_plugin(scope: Scope, name: str, on: bool) -> str:
    """Enable or disable ``name`` for this profile; the line to show."""
    record = _find(scope, name)
    if record is None:
        return f"No plugin named {name!r}."
    raw = _raw(scope)
    set_enabled(raw, record.key, on)
    if not on:
        memory = raw.get("memory")
        if isinstance(memory, dict) and cast(dict[str, Any], memory).get("provider") == record.name:
            cast(dict[str, Any], memory)["provider"] = ""
    config.save(scope, raw, replace=True)
    word = "enabled" if on else "disabled"
    return f"Plugin {record.key} {word}; the change applies the next time indus starts."


# -- the group ------------------------------------------------------------------------------------


@group("plugins", "Install, enable and inspect plugins.")
def plugins_group() -> None:
    pass


@plugins_group.command("list")
@click.option("--json", "as_json", is_flag=True, help="Print the list as JSON.")
def list_command(as_json: bool) -> None:
    """List every plugin with its state and source."""
    scope = _scope()
    found = _discover(scope)
    if as_json:
        installed = Installer(scope).records()
        rows = [
            {
                "key": key,
                "name": r.name,
                "state": r.state,
                "kind": r.kind,
                "source": _source_label(r, installed),
                "reason": r.reason,
            }
            for key, r in sorted(found.records.items())
        ]
        click.echo(json.dumps(rows, indent=2))
        return
    for warning in found.warnings:
        err(f"warning: {warning}")
    if not found.records:
        click.echo("No plugins found. Install one with `indus plugins install <owner/repo>`.")
        return
    click.echo(f"{'PLUGIN':<24} {'STATE':<12} {'KIND':<10} SOURCE")
    for line in plugin_lines(scope):
        click.echo(line)


def _ask_env(scope: Scope, io: prompts.Interactive, manifest: PluginManifest) -> None:
    missing = consent.missing_env(scope, manifest)
    if not missing:
        return
    if not io.tty:
        names = ", ".join(m.name for m in missing)
        click.echo(f"Needs settings you have not set yet: {names} (`indus auth` or the .env file).")
        return
    store = secret_store(scope)
    click.echo(f"{manifest.name} needs these values (kept in the profile's secret store):")
    for need in missing:
        question = need.prompt or need.name
        value = prompts.ask_secret(io, question) if need.secret else prompts.ask_text(io, question)
        if value.strip():
            store.set(need.name, value.strip())
            click.echo(f"  {need.name}: saved")
        else:
            click.echo(f"  {need.name}: skipped")


def _deps_ok(scope: Scope, io: prompts.Interactive, manifest: PluginManifest) -> bool:
    """Python dependencies consented and installed (True when there are none)."""
    if not manifest.python_dependencies:
        return True
    try:
        wanted = consent.pinned_requirements(manifest)
    except consent.ConsentError as exc:
        err(f"{exc}")
        return False
    click.echo(f"{manifest.name} needs Python packages: {', '.join(wanted)}")
    if not io.tty:
        click.echo("No terminal to ask: the packages were not installed and the plugin stays off.")
        return False
    if not prompts.ask_yes_no(io, "Install them into the Indus environment now?", default=False):
        click.echo("Not installed; the plugin stays off.")
        return False
    ok, output = consent.install_dependencies(scope, wanted)
    if not ok:
        err(f"Installing the packages failed: {output.strip()[-500:]}")
    return ok


ask_env = _ask_env
"""The ``requires_env`` questions of one installed plugin (also used by plugin packs)."""
dependencies_ok = _deps_ok
"""Python dependencies consented and installed (also used by plugin packs)."""


def _capabilities(raw: dict[str, Any], io: prompts.Interactive, manifest: PluginManifest) -> None:
    result = consent.consent_capabilities(raw, manifest, ask=_asker(io))
    if result.waiting:
        click.echo(
            f"Capabilities not granted (no terminal to ask): {', '.join(result.waiting)}; "
            f"grant them with `indus plugins capabilities {manifest.plugin_id} --grant NAME`."
        )
    elif result.refused:
        click.echo(f"Not granted: {', '.join(result.refused)}; the plugin runs without them.")


@plugins_group.command("install")
@click.argument("identifier")
@click.option("--ref", default=None, help="Pin to this full 40-character commit SHA.")
@click.option("--enable/--no-enable", default=None, help="Enable it now (default: ask, or no).")
@click.option("--force", is_flag=True, help="Reinstall, and accept a caution scan.")
def install_command(identifier: str, ref: str | None, enable: bool | None, force: bool) -> None:
    """Install a plugin from owner/repo, a git URL or a local folder."""
    state = cli_state()
    scope = _scope()
    io = _io(state)
    tool = Installer(scope)
    try:
        result = tool.install(identifier, ref=ref, force=force, confirm=_confirm_scan(io))
    except InstallBlocked as exc:
        if exc.report is not None:
            err(render_report(exc.report))
        err(f"Blocked: {exc}")
        raise click.exceptions.Exit(1) from None
    except InstallError as exc:
        err(f"Error: {exc}" + (f" ({exc.hint})" if exc.hint else ""))
        raise click.exceptions.Exit(1) from None
    kind = str(result.record.get("kind"))
    if kind == "git":
        click.echo("Note: a custom, unreviewed source.")
    pinned = " (pinned)" if result.record.get("pinned") else ""
    revision = str(result.record.get("revision") or "")[:12]
    click.echo(f"Installed {result.name}{' at ' + revision if revision else ''}{pinned}.")
    manifest = result.manifest
    if manifest is None:
        click.echo("It has no plugin.yaml or plugin.json, so it cannot be enabled yet.")
        return
    _ask_env(scope, io, manifest)
    raw = _raw(scope)
    if consent.is_memory_provider(manifest):
        use = (
            enable
            if enable is not None
            else (
                io.tty and prompts.ask_yes_no(io, f"Make {result.name} the memory provider?", False)
            )
        )
        if use:
            raw.setdefault("memory", {})["provider"] = result.name
            click.echo(f"{result.name} is now memory.provider; new sessions use it.")
        else:
            click.echo(f"Installed but not active; `indus memory setup {result.name}` uses it.")
        _save(scope, raw)
        return
    wanted = (
        enable
        if enable is not None
        else (io.tty and prompts.ask_yes_no(io, f"Enable {result.name} now?", default=False))
    )
    if wanted and not _deps_ok(scope, io, manifest):
        wanted = False
    if wanted:
        set_enabled(raw, manifest.plugin_id, True)
        click.echo(f"Enabled {result.name}; new sessions load it.")
    else:
        click.echo(f"Not enabled. Run `indus plugins enable {result.name}` to turn it on.")
    _capabilities(raw, io, manifest)
    _save(scope, raw)
    after = result.target / "after-install.md"
    if after.is_file():
        click.echo(after.read_text(encoding="utf-8-sig").strip())


@plugins_group.command("update")
@click.argument("name", required=False)
@click.option("--all", "every", is_flag=True, help="Update every installed plugin.")
@click.option("--force", is_flag=True, help="Accept a caution scan.")
@click.option("--trust-update-url", is_flag=True, help="Accept a changed update_url.")
def update_command(name: str | None, every: bool, force: bool, trust_update_url: bool) -> None:
    """Pull the latest version of a plugin (scanned again before it is used)."""
    if not name and not every:
        raise click.UsageError("name a plugin or pass --all")
    state = cli_state()
    scope = _scope()
    io = _io(state)
    tool = Installer(scope)
    names = sorted(tool.records()) if every else [cast(str, name)]
    failed = False
    for plugin in names:
        try:
            result = tool.update(
                plugin, force=force, confirm=_confirm_scan(io), trust_update_url=trust_update_url
            )
        except InstallError as exc:
            err(f"{plugin}: {exc}")
            failed = True
            continue
        if result.blocked:
            failed = True
            err(f"{plugin}: {result.blocked}")
            if result.disable:
                raw = _raw(scope)
                set_enabled(raw, plugin, False)
                _save(scope, raw)
                err(f"{plugin}: disabled; it stays at {result.old[:12]}")
        elif result.changed:
            click.echo(f"{plugin}: updated {result.old[:12]} -> {result.new[:12]}")
        else:
            click.echo(f"{plugin}: already up to date")
        if result.note:
            click.echo(f"{plugin}: {result.note}")
    if failed:
        raise click.exceptions.Exit(1)


@plugins_group.command("remove")
@click.argument("name")
@click.option("-y", "--yes", is_flag=True, help="Do not ask for confirmation.")
def remove_command(name: str, yes: bool) -> None:
    """Delete an installed plugin and its settings."""
    state = cli_state()
    scope = _scope()
    io = prompts.io_for(
        state, f"Removing {name} needs confirmation: run `indus plugins remove {name} -y`."
    )
    answer = prompts.confirm(io, f"Remove plugin {name} and its settings?", assume_yes=yes)
    if answer is None:
        raise click.exceptions.Exit(1)
    if not answer:
        click.echo("Nothing removed.")
        return
    try:
        removed = Installer(scope).remove(name)
    except InstallError as exc:
        err(f"Error: {exc}")
        raise click.exceptions.Exit(1) from None
    raw = _raw(scope)
    changed = forget_plugin(raw, name)
    if changed:
        _save(scope, raw)
    if not removed and not changed:
        err(f"No installed plugin named {name!r}.")
        raise click.exceptions.Exit(1)
    click.echo(f"Removed {name}" + (f" (cleared {', '.join(changed)})" if changed else "") + ".")


@plugins_group.command("enable")
@click.argument("name")
def enable_command(name: str) -> None:
    """Allow a plugin to load in new sessions."""
    state = cli_state()
    scope = _scope()
    record = _need(scope, name)
    if record.manifest is None:
        err(f"{record.key} cannot be enabled: {record.error or 'its manifest is broken'}")
        raise click.exceptions.Exit(1)
    if record.handler == "memory":
        err(
            f"{record.key} is a memory provider: choose it with `indus memory setup {record.name}`."
        )
        raise click.exceptions.Exit(1)
    io = _io(state)
    if not _deps_ok(scope, io, record.manifest):
        raise click.exceptions.Exit(1)
    raw = _raw(scope)
    set_enabled(raw, record.key, True)
    _capabilities(raw, io, record.manifest)
    _save(scope, raw)
    click.echo(f"Plugin {record.key} enabled; new sessions load it.")


@plugins_group.command("disable")
@click.argument("name")
def disable_command(name: str) -> None:
    """Stop a plugin from loading."""
    scope = _scope()
    _need(scope, name)
    click.echo(toggle_plugin(scope, name, False))


@plugins_group.command("info")
@click.argument("name")
def info_command(name: str) -> None:
    """Show a plugin's manifest, capabilities, grants and where it came from."""
    scope = _scope()
    record = _need(scope, name)
    click.echo(f"Plugin {record.key} ({record.source}, {record.state})")
    if record.reason:
        click.echo(f"  {record.reason}")
    manifest = record.manifest
    if manifest is None:
        click.echo(f"  Broken: {record.error}")
        return
    for label, value in (
        ("Version", manifest.version),
        ("Description", manifest.description),
        ("Author", manifest.author),
        ("License", manifest.license),
        ("Homepage", manifest.homepage),
        ("Kind", manifest.kind),
        ("Folder", manifest.path),
        ("Tools", ", ".join(manifest.provides_tools)),
        ("Hooks", ", ".join(manifest.provides_hooks)),
        ("Python packages", ", ".join(manifest.python_dependencies)),
    ):
        if value:
            click.echo(f"  {label}: {value}")
    missing = {m.name for m in consent.missing_env(scope, manifest)}
    for env_name in manifest.env_names():
        click.echo(f"  Needs {env_name}: {'missing' if env_name in missing else 'set'}")
    raw = _raw(scope)
    granted = set(consent.granted_capabilities(raw, manifest.plugin_id))
    declared = consent.declared_capabilities(manifest)
    if declared:
        click.echo("  Capabilities:")
        for cap in declared:
            mark = "granted" if cap in granted else "not granted"
            click.echo(f"    {cap} ({mark}): {consent.CAPABILITIES[cap]}")
        click.echo(f"  {NOT_A_SANDBOX}")
    installed = Installer(scope).record(record.name) if record.source == "user" else None
    if installed is not None:
        pin = " (pinned)" if installed.get("pinned") else ""
        click.echo(
            f"  Installed from {installed.get('source')} at {installed.get('revision') or '-'}"
            f"{pin} on {installed.get('installed_at')}"
        )
        scan = installed.get("scan")
        if isinstance(scan, Mapping):
            found = cast(Mapping[str, Any], scan)
            click.echo(f"  Scan: {found.get('verdict')} ({found.get('scanner')})")


@plugins_group.command("capabilities")
@click.argument("name")
@click.option("--grant", multiple=True, help="Grant a declared capability (repeatable).")
@click.option("--revoke", multiple=True, help="Take a capability back (repeatable).")
def capabilities_command(name: str, grant: tuple[str, ...], revoke: tuple[str, ...]) -> None:
    """Show, grant or revoke what a plugin may do beyond tools and hooks."""
    scope = _scope()
    record = _need(scope, name)
    if record.manifest is None:
        err(f"{record.key} has a broken manifest")
        raise click.exceptions.Exit(1)
    manifest = record.manifest
    declared = consent.declared_capabilities(manifest)
    unknown = [c for c in grant if c not in declared]
    if unknown:
        err(
            f"{record.key} does not declare: {', '.join(unknown)} "
            f"(declared: {', '.join(declared) or 'none'})"
        )
        raise click.exceptions.Exit(1)
    raw = _raw(scope)
    if grant or revoke:
        if revoke:
            consent.revoke_capabilities(raw, manifest.plugin_id, revoke)
        if grant:
            current = consent.granted_capabilities(raw, manifest.plugin_id)
            consent.set_grants(raw, manifest.plugin_id, [*current, *grant], declared)
        _save(scope, raw)
    granted = set(consent.granted_capabilities(raw, manifest.plugin_id))
    if not declared:
        click.echo(f"{record.key} declares no capabilities.")
        return
    for cap in declared:
        click.echo(
            f"{cap:<18} {'granted' if cap in granted else 'not granted':<12} "
            f"{consent.CAPABILITIES[cap]}"
        )
    click.echo(NOT_A_SANDBOX)


@plugins_group.command("check-updates")
def check_updates_command() -> None:
    """Check every installed plugin's source for a newer version."""
    scope = _scope()
    tool = Installer(scope)
    statuses = tool.check_updates()
    if not statuses:
        click.echo("No plugins were installed with `indus plugins install`.")
        return
    for status in statuses:
        if status.available:
            click.echo(
                f"{status.name}: update available ({status.current[:12]} -> {status.latest[:12]})"
            )
        elif status.note:
            click.echo(f"{status.name}: {status.note}")
        else:
            click.echo(f"{status.name}: up to date")


@plugins_group.command("validate")
@click.argument("folder", type=click.Path(file_okay=False, path_type=Path))
def validate_command(folder: Path) -> None:
    """Check a plugin folder's manifest and scan it (for plugin authors)."""
    if not folder.is_dir():
        err(f"{folder} is not a folder")
        raise click.exceptions.Exit(1)
    read = read_manifest(folder, source="user")
    if read is None:
        err(f"{folder} has no plugin.yaml, plugin.yml or plugin.json")
        raise click.exceptions.Exit(1)
    for warning in read.warnings:
        click.echo(f"manifest warning: {warning}")
    if read.manifest is None:
        err(f"manifest error: {read.error}")
        raise click.exceptions.Exit(1)
    manifest = read.manifest
    click.echo(f"Manifest ok: {manifest.name} {manifest.version}".rstrip())
    report = scan_tree(folder, kind="plugin", allowed_env=manifest.env_names(), name=manifest.name)
    click.echo(render_report(report))
    if report.verdict == "dangerous":
        raise click.exceptions.Exit(1)


def _add_pack_group() -> None:
    from indus.cli.commands.plugin_pack import pack_group

    plugins_group.add_command(pack_group)


_add_pack_group()
