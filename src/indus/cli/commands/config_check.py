"""``indus config check`` and ``indus config migrate``.

``check`` reads the user's file as written (no defaults) and reports, changing nothing: the
version stamp (a file newer than this build is an error), values the schema rejects (errors),
unknown keys (warnings, with a did-you-mean), structural problems (``fallback_providers`` entries,
``providers`` entries, list or mapping slots holding quoted text) and deprecated environment names
(``DEPRECATED_ENV``, empty today). It returns 1 when any error was found. ``indus doctor`` reads
the same findings (``check_settings``).

``migrate`` runs P01's migration runner: malformed YAML is refused before any side effect, steps
run from the file's stamp, new default keys are listed but never written, and the file is backed
up (reason ``pre-migrate``) before it is saved. It is a dry run unless ``--apply`` is given.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

import click

from indus.cli.commands._support import err, state_of
from indus.cli.config_coerce import OPEN_ROOTS, check_key
from indus.foundation import yamlio
from indus.foundation.config import keys, loader, migrations, schema
from indus.foundation.envfile import read_env_file
from indus.foundation.envload import env_file_path
from indus.foundation.errors import IndusError
from indus.foundation.home import is_managed
from indus.foundation.scope import Scope

__all__ = [
    "DEPRECATED_ENV",
    "Finding",
    "Topic",
    "check_command",
    "check_settings",
    "migrate_command",
]

DEPRECATED_ENV: dict[str, str] = {}
"""Environment names that no longer do anything: ``name -> what to use instead``. Later phases add
rows when a name is retired."""

_SHOWN_DEFAULTS = 10
_OWN_CHECKS = frozenset({"providers", "fallback_providers"})

type Severity = Literal["error", "warning", "info"]


type Topic = Literal["read", "version", "value", "unknown", "env"]


@dataclass(frozen=True)
class Finding:
    severity: Severity
    message: str
    topic: Topic = "value"
    """What the finding is about (``indus doctor`` files them under its own ids)."""


def _read_raw(scope: Scope) -> tuple[dict[str, Any] | None, list[Finding]]:
    path = loader.config_path(scope)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None, [
            Finding("info", f"No settings file at {path}; built-in values apply.", "read")
        ]
    except OSError as exc:
        return None, [Finding("error", f"{path} cannot be read: {exc}", "read")]
    try:
        return loader.read_user_tree(data), []
    except yamlio.YamlError as exc:
        where = f" at line {exc.line}" if exc.line is not None else ""
        message = f"{path} has a formatting error{where}: {exc.reason}"
        return None, [Finding("error", message, "read")]


def _version_findings(raw: dict[str, Any]) -> list[Finding]:
    version = migrations.read_version(raw)
    current = migrations.CURRENT_VERSION
    if version < current:
        return [
            Finding(
                "warning",
                f"Config version {version} → {current} is available: run `indus config migrate`.",
                "version",
            )
        ]
    if version > current:
        return [
            Finding(
                "error",
                f"Config version {version} is newer than this build understands ({current}).",
                "version",
            )
        ]
    return [Finding("info", f"Config version {version} (current).", "version")]


def _unknown_keys(node: Any, path: tuple[str, ...] = ()) -> list[str]:
    """Dotted paths of the user's keys the schema does not know (the first unknown level only)."""
    found: list[str] = []
    for key, child in cast(Mapping[Any, Any], node).items():
        if not isinstance(key, str) or (not path and key.startswith("_")):
            continue
        dotted = keys.join_key([*path, key])
        if not check_key(dotted).known:
            found.append(dotted)
        elif isinstance(child, Mapping) and (path or key not in OPEN_ROOTS):
            found.extend(_unknown_keys(child, (*path, key)))
    return found


def _schema_findings(raw: dict[str, Any], scope: Scope) -> list[Finding]:
    expanded = loader.expand(loader.canonicalise(raw), scope.env, {}, set())
    merged = loader.deep_merge(schema.defaults_tree(), cast(dict[str, Any], expanded))
    found: list[Finding] = []
    for line in schema.validate(merged):
        where = line.split(":", 1)[0]
        if line.endswith("unknown setting (kept)") or where.split(".")[0] in _OWN_CHECKS:
            continue  # reported below with better wording
        found.append(Finding("error", line))
    for dotted in _unknown_keys(loader.canonicalise(raw)):
        check = check_key(dotted)
        hint = f" Did you mean {check.suggestion}?" if check.suggestion else ""
        message = f"{dotted} is not a setting this version reads.{hint}"
        found.append(Finding("warning", message, "unknown"))
    return found


def _fallback_findings(raw: Mapping[str, Any]) -> list[Finding]:
    if "fallback_providers" not in raw:
        return []
    entries = raw["fallback_providers"]
    if not isinstance(entries, list):
        return [Finding("error", "fallback_providers must be a list of {provider, model} entries.")]
    found: list[Finding] = []
    for index, entry in enumerate(cast(list[Any], entries)):
        ok = isinstance(entry, Mapping) and all(
            isinstance(cast(Mapping[str, Any], entry).get(name), str)
            and str(cast(Mapping[str, Any], entry).get(name)).strip()
            for name in ("provider", "model")
        )
        if not ok:
            found.append(
                Finding(
                    "error",
                    f"fallback_providers[{index}] needs both 'provider' and 'model'; it is "
                    "ignored as it is.",
                )
            )
    return found


def _provider_findings(raw: Mapping[str, Any]) -> list[Finding]:
    if "providers" not in raw:
        return []
    section = raw["providers"]
    if not isinstance(section, Mapping):
        return [Finding("error", "providers must be a mapping of provider name to settings.")]
    found: list[Finding] = []
    for name, entry in cast(Mapping[Any, Any], section).items():
        if not isinstance(entry, Mapping):
            found.append(
                Finding("error", f"providers.{name} must be a mapping (base_url, key_env, ...).")
            )
    return found


def _quoted_container_findings(node: Any, path: tuple[str, ...] = ()) -> list[Finding]:
    found: list[Finding] = []
    if isinstance(node, Mapping):
        for key, child in cast(Mapping[Any, Any], node).items():
            if isinstance(key, str) and "." not in key:
                found.extend(_quoted_container_findings(child, (*path, key)))
        return found
    if isinstance(node, str) and path:
        expected = schema.schema_for(".".join(path))
        text = node.strip()
        if expected in (list, dict) and text[:1] in ("[", "{"):
            shape = "list" if expected is list else "mapping"
            found.append(
                Finding(
                    "warning",
                    f"{'.'.join(path)} holds text that looks like a {shape}; remove the quotes "
                    "around it so it is read as one.",
                )
            )
    return found


def _env_findings(scope: Scope) -> list[Finding]:
    if not DEPRECATED_ENV:
        return []
    in_file = read_env_file(env_file_path(scope.home))
    found: list[Finding] = []
    for name, instead in DEPRECATED_ENV.items():
        if name in in_file or name in scope.env:
            found.append(Finding("warning", f"{name} no longer does anything; {instead}.", "env"))
    return found


def check_settings(scope: Scope) -> list[Finding]:
    """Every finding for the profile's settings file (see the module notes)."""
    raw, found = _read_raw(scope)
    if raw is not None:
        found += _version_findings(raw)
        found += _schema_findings(raw, scope)
        found += _fallback_findings(raw)
        found += _provider_findings(raw)
        found += _quoted_container_findings(raw)
    found += _env_findings(scope)
    return found


@click.command("check", help="Report problems in the settings file without changing it.")
@click.pass_context
def check_command(ctx: click.Context) -> int:
    findings = check_settings(state_of(ctx).scope)
    errors = [f for f in findings if f.severity == "error"]
    warnings = [f for f in findings if f.severity == "warning"]
    for finding in findings:
        prefix = "" if finding.severity == "info" else f"{finding.severity}: "
        click.echo(f"{prefix}{finding.message}")
    if not errors and not warnings:
        click.echo("No problems found.")
    else:
        click.echo(f"{len(errors)} error(s), {len(warnings)} warning(s).")
    return 1 if errors else 0


@click.command("migrate", help="Bring the settings file up to this version (dry run by default).")
@click.option("--apply", "apply_", is_flag=True, help="Write the migrated file (after a backup).")
@click.pass_context
def migrate_command(ctx: click.Context, apply_: bool) -> int:
    scope = state_of(ctx).scope
    if apply_ and is_managed(scope.home, scope.env):
        err("This installation is managed, so its settings cannot be migrated here.")
        return 1
    try:
        report = migrations.migrate(scope, dry_run=not apply_)
    except IndusError as exc:
        err(exc.message + (f" ({exc.hint})" if exc.hint else ""))
        return 1
    click.echo(report.message)
    for line in report.steps:
        click.echo(f"  - {line}")
    if report.new_defaults:
        count = len(report.new_defaults)
        click.echo(
            f"{count} setting(s) are not in your file; built-in values apply and nothing is "
            "written for them:"
        )
        for name in report.new_defaults[:_SHOWN_DEFAULTS]:
            click.echo(f"  {name}")
        if count > _SHOWN_DEFAULTS:
            click.echo(f"  … and {count - _SHOWN_DEFAULTS} more")
    if report.from_version < report.to_version:
        if report.wrote:
            click.echo("Saved; the previous file was backed up first.")
        else:
            click.echo(
                "Dry run: nothing was written. Run `indus config migrate --apply` to save it."
            )
    return 0
