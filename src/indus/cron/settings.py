"""The ``cron.*`` settings the ticker and the runner read, with their env overrides.

Every reader takes the scope and never raises: a value of the wrong type warns once per key and
gives the default. Env overrides (``INDUS_CRON_*``) come from the scope's env view and win over the
config file.
"""

from __future__ import annotations

import logging
import math
import threading
from pathlib import Path
from typing import Any

from indus.foundation.config.schema import default_for
from indus.foundation.scope import Scope

log = logging.getLogger(__name__)

MAX_PARALLEL_ENV = "INDUS_CRON_MAX_PARALLEL"
SCRIPT_TIMEOUT_ENV = "INDUS_CRON_SCRIPT_TIMEOUT"
INACTIVITY_ENV = "INDUS_CRON_INACTIVITY_TIMEOUT"

_warned: set[tuple[str, str]] = set()
_warned_lock = threading.Lock()


def _warn_once(scope: Scope, key: str, value: object) -> None:
    marker = (str(scope.home), key)
    with _warned_lock:
        if marker in _warned:
            return
        _warned.add(marker)
    log.warning("%s: %r is not usable here; using the default", key, value)


def config_value(scope: Scope, key: str) -> Any:
    """``key`` from the scope's config view (its default when no view or unreadable)."""
    config = scope.config
    if config is None:
        return default_for(key)
    try:
        value = config.get(key)
    except Exception:
        log.debug("%s unreadable", key, exc_info=True)
        return default_for(key)
    return default_for(key) if value is None else value


def _number(raw: object) -> float | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        value = float(raw)
    elif isinstance(raw, str) and raw.strip():
        try:
            value = float(raw.strip())
        except ValueError:
            return None
    else:
        return None
    return value if math.isfinite(value) else None


def _env_then_config(scope: Scope, env_name: str, key: str) -> float | None:
    raw_env = scope.env.get(env_name, "").strip()
    if raw_env:
        value = _number(raw_env)
        if value is not None:
            return value
        _warn_once(scope, env_name, raw_env)
    raw = config_value(scope, key)
    value = _number(raw)
    if value is None:
        _warn_once(scope, key, raw)
    return value


def cron_dir(home: Path) -> Path:
    return home / "cron"


def scripts_dir(home: Path) -> Path:
    return home / "scripts"


def output_dir(home: Path) -> Path:
    return cron_dir(home) / "output"


def max_parallel(scope: Scope) -> int | None:
    """Jobs one ticker runs at once; None = no limit (unset or 0)."""
    value = _env_then_config(scope, MAX_PARALLEL_ENV, "cron.max_parallel_jobs")
    if value is None or value <= 0:
        return None
    return max(1, int(value))


def script_timeout(scope: Scope) -> float:
    value = _env_then_config(scope, SCRIPT_TIMEOUT_ENV, "cron.script_timeout_seconds")
    return value if value is not None and value > 0 else 3600.0


def inactivity_timeout(scope: Scope) -> float | None:
    """Seconds an agent run may show no progress; None = no limit."""
    value = _env_then_config(scope, INACTIVITY_ENV, "cron.inactivity_timeout_seconds")
    if value is None:
        return 600.0
    return value if value > 0 else None


def output_retention(scope: Scope) -> int:
    """Documents kept per job; 0 or less keeps all."""
    value = _number(config_value(scope, "cron.output_retention"))
    return 50 if value is None else int(value)


def cleanup_timeout(scope: Scope) -> float:
    value = _number(config_value(scope, "cron.cleanup_timeout_seconds"))
    return 10.0 if value is None or value < 0 else value


def preflight_enabled(scope: Scope) -> bool:
    return config_value(scope, "cron.preflight") is not False


def allow_agent_scheduling(scope: Scope) -> bool:
    return config_value(scope, "cron.allow_agent_scheduling") is True


def text_setting(scope: Scope, key: str) -> str:
    value = config_value(scope, key)
    return value.strip() if isinstance(value, str) else ""
