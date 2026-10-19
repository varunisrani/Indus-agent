"""Shared helpers for the slice-A learning tests (memory, pending writes, session search)."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from indus.foundation.config.loader import ConfigView
from indus.foundation.config.schema import defaults_tree
from indus.foundation.scope import Scope


def _merge(base: dict[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    for key, value in extra.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), dict):
            _merge(base[key], value)  # pyright: ignore[reportUnknownArgumentType]
        else:
            base[key] = copy.deepcopy(value)
    return base


def with_config(scope: Scope, settings: Mapping[str, Any]) -> Scope:
    """``scope`` reading the stock defaults with ``settings`` laid over them."""
    merged = _merge(defaults_tree(), settings)
    view = ConfigView(scope.home / "config.yaml", merged, dict(settings), set())
    return scope.with_config(view)
