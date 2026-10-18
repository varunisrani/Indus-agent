"""Reading the provider-related settings of a scope's config (one place for the shapes).

``model.*`` names the chosen route; ``providers.<name>`` holds named custom endpoints and
per-provider settings (timeouts, per-model context lengths). Values are read defensively: a wrong
type is treated as unset, never as an error, because the config file is user-edited.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, cast

from indus.foundation import config as config_mod
from indus.foundation.scope import ConfigSource, Scope


def config_of(scope: Scope) -> ConfigSource:
    """The scope's attached config view, or the loaded config of its home."""
    if scope.config is not None:
        return scope.config
    return config_mod.load(scope)


def as_mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, Any], value)
    return {}


def text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def positive_number(value: object) -> float | None:
    """A finite number > 0 (numeric strings accepted), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) and number > 0 else None


def positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
        return number if number > 0 else None
    return None


def model_section(config: ConfigSource) -> Mapping[str, Any]:
    raw = config.get("model")
    if isinstance(raw, str):
        return {"default": raw}
    return as_mapping(raw)


def providers_section(config: ConfigSource) -> Mapping[str, Any]:
    return as_mapping(config.get("providers"))


def provider_entry(config: ConfigSource, name: str) -> Mapping[str, Any]:
    """``providers.<name>`` (matched case-insensitively), or an empty mapping."""
    section = providers_section(config)
    if name in section:
        return as_mapping(section[name])
    lowered = name.lower()
    for key, value in section.items():
        if key.lower() == lowered:
            return as_mapping(value)
    return {}


def model_entry(provider: Mapping[str, Any], model: str) -> Mapping[str, Any]:
    """``models.<id>`` of a provider entry (a list of ids gives empty settings)."""
    if not model:
        return {}
    return as_mapping(as_mapping(provider.get("models")).get(model))
