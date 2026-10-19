# pyright: strict
"""Reading the ``plugins`` config section for a profile.

Plugin ids may contain ``/`` (``image_gen/<name>``) and ``.``, so per-plugin entries are read as
one mapping and indexed by id, never through a dotted path that would split the id.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, cast

from indus.foundation.scope import Scope

__all__ = [
    "bounded_seconds",
    "plugin_entry",
    "plugins_setting",
    "string_set",
]

log = logging.getLogger(__name__)

_warned: set[tuple[str, str]] = set()


def plugins_setting(scope: Scope | None, key: str, default: Any = None) -> Any:
    """``plugins.<key>`` from the scope's config; ``default`` when unset or unreadable."""
    if scope is None or scope.config is None:
        return default
    try:
        value = scope.config.get(f"plugins.{key}")
    except Exception:
        log.debug("plugins.%s could not be read", key, exc_info=True)
        return default
    return default if value is None else value


def plugin_entry(scope: Scope | None, plugin_id: str) -> Mapping[str, Any]:
    """``plugins.entries.<plugin_id>`` (empty when missing or not a mapping)."""
    entries = plugins_setting(scope, "entries", {})
    if not isinstance(entries, Mapping):
        return {}
    entry = cast(Mapping[str, Any], entries).get(plugin_id)
    return cast(Mapping[str, Any], entry) if isinstance(entry, Mapping) else {}


def string_set(value: object) -> frozenset[str]:
    """A list of names as a set; anything else is empty."""
    if not isinstance(value, list | tuple | set | frozenset):
        return frozenset()
    return frozenset(str(v) for v in cast(list[object], value) if isinstance(v, str) and v)


def bounded_seconds(raw: object, *, name: str, default: float, maximum: float) -> float:
    """A seconds setting: 0 means off, negative or non-numbers fall back, above ``maximum`` clamps.

    Each distinct bad value warns once per process.
    """
    if raw is None:
        return default
    try:
        if isinstance(raw, bool):
            raise TypeError
        value = float(cast(Any, raw))
    except (TypeError, ValueError):
        _warn_once(name, repr(raw), f"{name} is not a number; using {default:g} s")
        return default
    if value != value or value < 0:  # NaN or negative
        _warn_once(name, repr(raw), f"{name} is negative; using {default:g} s")
        return default
    if value > maximum:
        _warn_once(name, repr(raw), f"{name}={value:g} is above {maximum:g} s; clamped")
        return maximum
    return value


def _warn_once(name: str, raw: str, message: str) -> None:
    if (name, raw) in _warned:
        return
    _warned.add((name, raw))
    log.warning("%s", message)
