"""Config version stamp and the ordered migration registry.

Our stamp starts at 1 and there are no migrations from the reference's formats (D-16.3), so the
registry starts empty and an unstamped file is only stamped. A later schema change adds
``STEPS[n] = step`` turning version ``n`` into ``n + 1``. New default keys are listed, never
written: the file only ever holds what the user chose.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

from indus.foundation import yamlio
from indus.foundation.config import backups, loader, schema, writer
from indus.foundation.config.errors import ConfigUnreadable
from indus.foundation.config.loader import ConfigScope

__all__ = ["CURRENT_VERSION", "STEPS", "MigrationReport", "migrate", "read_version"]

CURRENT_VERSION = schema.CURRENT_VERSION

# A step edits the raw user mapping in place and returns one line per change it made.
Step = Callable[[dict[str, Any]], list[str]]
STEPS: dict[int, Step] = {}


@dataclass(frozen=True)
class MigrationReport:
    from_version: int
    to_version: int
    steps: list[str] = field(default_factory=list[str])
    new_defaults: list[str] = field(default_factory=list[str])
    wrote: bool = False
    message: str = ""


def read_version(raw: dict[str, Any]) -> int:
    """The file's stamp; missing, boolean or non-integer stamps read as 0."""
    value = raw.get(schema.VERSION_KEY)
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _missing_defaults(raw: dict[str, Any]) -> list[str]:
    """Dotted paths that have a default but are absent from the user's file."""
    found: list[str] = []

    def walk(default: dict[str, Any], have: Any, prefix: str) -> None:
        for key, child in default.items():
            if key == schema.VERSION_KEY:
                continue
            name = f"{prefix}{key}"
            if not isinstance(have, dict) or key not in have:
                found.extend(_leaves(child, name))
            elif isinstance(child, dict) and child:
                walk(cast(dict[str, Any], child), cast(dict[str, Any], have)[key], f"{name}.")

    walk(schema.defaults_tree(), loader.canonicalise(raw), "")
    return sorted(found)


def _leaves(tree: Any, prefix: str) -> list[str]:
    if isinstance(tree, dict) and tree:
        out: list[str] = []
        for key, child in cast(dict[str, Any], tree).items():
            out.extend(_leaves(child, f"{prefix}.{key}"))
        return out
    return [prefix]


def migrate(scope: ConfigScope, *, dry_run: bool = False) -> MigrationReport:
    """Bring the scope's ``config.yaml`` up to `CURRENT_VERSION`."""
    path = loader.config_path(scope)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return MigrationReport(
            CURRENT_VERSION, CURRENT_VERSION, message="No config file yet; nothing to migrate."
        )
    except OSError as exc:
        raise ConfigUnreadable(
            f"{path} cannot be read; nothing was migrated", detail=str(exc)
        ) from exc
    try:
        raw = loader.read_user_tree(data)
    except yamlio.YamlError as exc:
        where = f" at line {exc.line}" if exc.line is not None else ""
        raise ConfigUnreadable(
            f"{path} has a formatting error{where}; nothing was migrated",
            detail=exc.reason,
            hint="fix the file first",
        ) from exc

    version = read_version(raw)
    new_defaults = _missing_defaults(raw)
    if version > CURRENT_VERSION:
        return MigrationReport(
            version,
            version,
            new_defaults=new_defaults,
            message=(
                f"Config version {version} is newer than this build understands "
                f"({CURRENT_VERSION}); left unchanged."
            ),
        )
    if version == CURRENT_VERSION:
        return MigrationReport(
            version,
            version,
            new_defaults=new_defaults,
            message=f"Config version {version} is current.",
        )

    migrated = copy.deepcopy(raw)
    applied: list[str] = []
    for step_from in range(version, CURRENT_VERSION):
        step = STEPS.get(step_from)
        if step is not None:
            applied.extend(step(migrated))
    migrated[schema.VERSION_KEY] = CURRENT_VERSION
    message = f"Config version {version} → {CURRENT_VERSION}"
    if dry_run:
        return MigrationReport(
            version, CURRENT_VERSION, applied, new_defaults, wrote=False, message=message
        )
    backups.backup_config(path, "pre-migrate", clock=scope.clock)
    writer.save(scope, migrated, replace=True)
    return MigrationReport(
        version, CURRENT_VERSION, applied, new_defaults, wrote=True, message=message
    )
