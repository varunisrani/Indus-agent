"""The environment a profile sees: inherited env, then the profile's ``.env``, then config bridges.

:func:`build_env` is pure: it reads files and returns an ``EnvView``; it never edits ``os.environ``.
The CLI entry in single-profile mode publishes one view with :func:`publish_to_process`; hosts that
serve several profiles keep one view per scope.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping, MutableMapping
from pathlib import Path
from typing import Any, cast

from indus.foundation.envfile import NAME_RE, read_env_file, read_written_names
from indus.foundation.scope import EnvView

log = logging.getLogger(__name__)

ENV_FILE_NAME = ".env"
REDACT_SWITCH = "INDUS_REDACT_SECRETS"
TERMINAL_PREFIX = "INDUS_TERMINAL_"

# The home and profile are chosen before any .env is read; a line in the file cannot move them.
LOCATION_NAMES: frozenset[str] = frozenset({"INDUS_HOME", "INDUS_PROFILE"})

_FALSE_WORDS = frozenset({"0", "false", "no", "off"})

_managed_registry: set[str] = set()


def register_managed_keys(*names: str) -> None:
    """Add names (provider keys, product ``INDUS_*`` settings) that are cleared from a view when
    the profile's ``.env`` exists but no longer defines them. Location names are never added."""
    for name in names:
        if NAME_RE.match(name) and name not in LOCATION_NAMES:
            _managed_registry.add(name)


def managed_key_names() -> frozenset[str]:
    return frozenset(_managed_registry)


def env_file_path(home: Path) -> Path:
    return home / ENV_FILE_NAME


def _section(config: Mapping[str, Any] | None, name: str) -> Mapping[str, Any]:
    if not isinstance(config, Mapping):
        return {}
    value = config.get(name)
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}


def _env_text(value: object) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float | str):
        return str(value)
    return None


def terminal_bridge(config: Mapping[str, Any] | None) -> dict[str, str]:
    """``terminal.<key>`` scalars from config as ``INDUS_TERMINAL_<KEY>`` env entries."""
    bridged: dict[str, str] = {}
    for key, value in _section(config, "terminal").items():
        text = _env_text(value)
        name = f"{TERMINAL_PREFIX}{str(key).upper()}"
        if text is not None and NAME_RE.match(name):
            bridged[name] = text
    return bridged


def force_ipv4(config: Mapping[str, Any] | None) -> bool:
    """The ``network.force_ipv4`` switch, for the caller that configures HTTP clients."""
    return _section(config, "network").get("force_ipv4") is True


def redaction_flag(env: Mapping[str, str]) -> bool:
    """Whether redaction is on for an env view; on unless the switch says a false word."""
    raw = env.get(REDACT_SWITCH)
    return raw is None or raw.strip().lower() not in _FALSE_WORDS


def _make_view(inherited: Mapping[str, str], values: Mapping[str, str]) -> EnvView:
    """``inherited`` as the snapshot; what ``.env`` and config changed as the overlay."""
    overlay: dict[str, str | None] = {k: v for k, v in values.items() if inherited.get(k) != v}
    overlay.update({k: None for k in inherited if k not in values})
    return EnvView(inherited, overlay)


def build_env(
    home: Path,
    inherited: Mapping[str, str],
    *,
    config: Mapping[str, Any] | None = None,
    managed_keys: Iterable[str] = (),
) -> EnvView:
    """Build the env view for the profile at ``home``.

    Order: ``inherited``; ``<home>/.env`` over it (the file wins); managed names the file does not
    define are cleared (names we wrote always; registry names only when the file exists); the
    redaction switch bridged from config when still unset; ``terminal.*`` config re-asserted last.
    """
    path = env_file_path(home)
    values = dict(inherited)
    try:
        file_values = read_env_file(path)
        file_exists = path.exists()
    except OSError as exc:
        log.warning("could not read %s: %s", path, exc)
        file_values, file_exists = {}, False

    for name, value in file_values.items():
        if name in LOCATION_NAMES:
            log.warning("ignored %s in %s: the home is chosen before .env is read", name, path)
            continue
        values[name] = value

    clear = set(read_written_names(path))
    if file_exists:
        clear |= _managed_registry | set(managed_keys)
    for name in clear - LOCATION_NAMES:
        if name not in file_values:
            values.pop(name, None)

    if REDACT_SWITCH not in values:
        setting = _section(config, "security").get("redact_secrets")
        if isinstance(setting, bool):
            values[REDACT_SWITCH] = "true" if setting else "false"

    values.update(terminal_bridge(config))
    return _make_view(inherited, values)


def publish_to_process(
    view: Mapping[str, str], environ: MutableMapping[str, str] | None = None
) -> None:
    """Make ``environ`` (default ``os.environ``) hold exactly the view's names and values.

    Called once by the CLI entry in single-profile mode; never by library code.
    """
    target: MutableMapping[str, str] = os.environ if environ is None else environ
    for name in [n for n in target if n not in view]:
        del target[name]
    for name, value in view.items():
        if target.get(name) != value:
            target[name] = value
