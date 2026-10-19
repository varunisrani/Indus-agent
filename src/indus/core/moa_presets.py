"""Mixture-of-agents presets: the ``moa`` settings, read tolerantly and checked strictly (P10-22).

Shape (``config.yaml``)::

    moa:
      default_preset: review
      reference_timeout: 120        # seconds per reference call; empty = no limit
      privacy_filter: ""            # "" | display | full (see core.moa)
      presets:
        review:
          reference_models:
            - {provider: openai, model: some-model}
            - {provider: anthropic, model: other-model, enabled: false, reasoning_effort: high}
          aggregator: {provider: openai, model: some-model}   # optional, shown by `indus moa list`
          reference_timeout: 90     # optional, overrides the section's
          fanout: per_iteration     # per_iteration (default) | user_turn | every_n:N

Reading (``read_settings``) never raises: a preset with problems is left out of ``presets`` and
its problems are kept in ``problems`` for ``indus moa list``. Writing (``indus moa configure``)
calls ``validate_preset`` / ``validate_section`` first and refuses to save anything with a problem;
every problem is listed at once.

A slot may not be a mixture itself: the provider ``moa`` and a provider that is the name of a
preset are refused (presets do not nest).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal, cast

from indus.foundation.scope import Scope

__all__ = [
    "FANOUTS",
    "MOA_PROVIDER",
    "PRIVACY_MODES",
    "MoaSettings",
    "Preset",
    "PrivacyMode",
    "Slot",
    "fanout_runs",
    "known_provider_check",
    "parse_fanout",
    "parse_slot_text",
    "preset_names",
    "preset_to_config",
    "privacy_mode",
    "read_settings",
    "settings_for",
    "validate_preset",
    "validate_section",
]

type PrivacyMode = Literal["", "display", "full"]

MOA_PROVIDER: Final = "moa"
PRIVACY_MODES: Final[tuple[PrivacyMode, ...]] = ("", "display", "full")
FANOUTS: Final = ("per_iteration", "user_turn")
_EVERY_N = re.compile(r"^every_n:(\d+)$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


@dataclass(frozen=True)
class Slot:
    """One model a preset asks (or aggregates with)."""

    provider: str
    model: str
    enabled: bool = True
    reasoning_effort: str = ""

    @property
    def label(self) -> str:
        base = f"{self.provider}:{self.model}"
        return f"{base} (reasoning {self.reasoning_effort})" if self.reasoning_effort else base

    def to_config(self, *, with_enabled: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {"provider": self.provider, "model": self.model}
        if with_enabled:
            out["enabled"] = self.enabled
        if self.reasoning_effort:
            out["reasoning_effort"] = self.reasoning_effort
        return out


@dataclass(frozen=True)
class Preset:
    name: str
    references: tuple[Slot, ...]
    aggregator: Slot | None = None
    reference_timeout: float | None = None
    fanout: str = "per_iteration"

    @property
    def enabled_references(self) -> tuple[Slot, ...]:
        return tuple(slot for slot in self.references if slot.enabled)


@dataclass(frozen=True)
class MoaSettings:
    presets: Mapping[str, Preset] = field(default_factory=dict[str, Preset])
    default_preset: str = ""
    reference_timeout: float | None = None
    privacy_filter: PrivacyMode = ""
    problems: tuple[str, ...] = ()

    def preset(self, name: str | None = None) -> Preset | None:
        """``name``'s preset, or the default one (the only one when no default is set)."""
        if name:
            return self.presets.get(name)
        if self.default_preset:
            return self.presets.get(self.default_preset)
        if len(self.presets) == 1:
            return next(iter(self.presets.values()))
        return None

    def timeout_for(self, preset: Preset) -> float | None:
        return preset.reference_timeout if preset.reference_timeout else self.reference_timeout


# -- small parsers ---------------------------------------------------------------------------------


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _mapping(value: object) -> Mapping[str, Any] | None:
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else None


def _bool(value: object, default: bool = True) -> bool | None:
    """A boolean, or None when the value cannot mean one."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        word = value.strip().lower()
        if word in ("true", "yes", "on", "1"):
            return True
        if word in ("false", "no", "off", "0"):
            return False
    return None


def _timeout(value: object) -> tuple[float | None, str | None]:
    """``(seconds or None, problem)``: empty means no limit; anything else must be > 0."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, None
    if isinstance(value, bool):
        return None, "must be a number of seconds"
    try:
        number = float(cast(Any, value))
    except (TypeError, ValueError):
        return None, "must be a number of seconds"
    if not math.isfinite(number) or number <= 0:
        return None, "must be more than 0 seconds"
    return number, None


def parse_fanout(value: object) -> tuple[str, str | None]:
    """``(normal form, problem)``; empty means ``per_iteration``."""
    word = _text(value).lower() if value is not None else ""
    if not word:
        return "per_iteration", None
    if word in FANOUTS:
        return word, None
    found = _EVERY_N.match(word)
    if found and int(found.group(1)) >= 1:
        n = int(found.group(1))
        return ("per_iteration" if n == 1 else f"every_n:{n}"), None
    return "per_iteration", "must be per_iteration, user_turn or every_n:N (N at least 1)"


def fanout_runs(fanout: str, iteration: int) -> bool:
    """Whether references are asked before ``iteration`` (1-based) of a turn."""
    if iteration <= 1 or fanout == "per_iteration":
        return True
    if fanout == "user_turn":
        return False
    found = _EVERY_N.match(fanout)
    if found:
        return (iteration - 1) % int(found.group(1)) == 0
    return True


def privacy_mode(value: object) -> PrivacyMode:
    word = _text(value).lower() if isinstance(value, str) else ""
    if value is True:
        return "full"
    for mode in PRIVACY_MODES:
        if word == mode:
            return mode
    return ""


def parse_slot_text(text: str) -> Slot | None:
    """``provider:model`` (the model may itself hold colons) → a slot; None when malformed."""
    provider, sep, model = text.strip().partition(":")
    if not sep or not provider.strip() or not model.strip():
        return None
    return Slot(provider.strip().lower(), model.strip())


# -- validation ------------------------------------------------------------------------------------


def _slot_problems(
    raw: object,
    where: str,
    *,
    known_provider: Callable[[str], bool] | None,
    preset_names: Sequence[str],
) -> tuple[Slot | None, list[str]]:
    entry = _mapping(raw)
    if entry is None:
        return None, [f"{where}: must be a mapping with provider and model"]
    provider, model = _text(entry.get("provider")).lower(), _text(entry.get("model"))
    problems: list[str] = []
    if not provider:
        problems.append(f"{where}: provider is missing")
    if not model:
        problems.append(f"{where}: model is missing")
    if provider == MOA_PROVIDER or (provider and provider in preset_names):
        problems.append(
            f"{where}: {provider!r} is a mixture of agents itself; presets cannot be nested"
        )
    elif provider and known_provider is not None and not known_provider(provider):
        problems.append(f"{where}: no provider is called {provider!r}")
    enabled = _bool(entry.get("enabled"))
    if enabled is None:
        problems.append(f"{where}: enabled must be true or false")
    effort = entry.get("reasoning_effort")
    if effort is not None and not isinstance(effort, str):
        problems.append(f"{where}: reasoning_effort must be a word such as low or high")
    if problems:
        return None, problems
    return Slot(provider, model, bool(enabled), _text(effort)), []


def validate_preset(
    name: str,
    raw: object,
    *,
    known_provider: Callable[[str], bool] | None = None,
    preset_names: Sequence[str] = (),
) -> tuple[Preset | None, list[str]]:
    """The preset and every problem with it (the preset is None when there is any problem)."""
    where = f"preset {name!r}"
    problems: list[str] = []
    if not _NAME.match(name or ""):
        problems.append(f"{where}: the name must be letters, digits, '-', '_' or '.'")
    entry = _mapping(raw)
    if entry is None:
        return None, [*problems, f"{where}: must be a mapping"]
    others = [n for n in preset_names if n != name]
    raw_refs = entry.get("reference_models")
    refs: list[Slot] = []
    if raw_refs is None or raw_refs == []:
        problems.append(f"{where}: no reference models (add at least one)")
    elif not isinstance(raw_refs, list):
        problems.append(f"{where}: reference_models must be a list")
    else:
        for i, item in enumerate(cast(list[object], raw_refs), 1):
            slot, found = _slot_problems(
                item,
                f"{where} reference {i}",
                known_provider=known_provider,
                preset_names=[*others, name],
            )
            problems.extend(found)
            if slot is not None:
                refs.append(slot)
        listed = len(cast(list[object], raw_refs))
        if refs and not any(slot.enabled for slot in refs) and len(refs) == listed:
            problems.append(f"{where}: every reference model is disabled")
    aggregator: Slot | None = None
    if entry.get("aggregator") is not None:
        aggregator, found = _slot_problems(
            entry.get("aggregator"),
            f"{where} aggregator",
            known_provider=known_provider,
            preset_names=[*others, name],
        )
        problems.extend(found)
    timeout, problem = _timeout(entry.get("reference_timeout"))
    if problem:
        problems.append(f"{where} reference_timeout: {problem}")
    fanout, problem = parse_fanout(entry.get("fanout"))
    if problem:
        problems.append(f"{where} fanout: {problem}")
    if problems:
        return None, problems
    return Preset(name, tuple(refs), aggregator, timeout, fanout), []


def validate_section(
    raw: object, *, known_provider: Callable[[str], bool] | None = None
) -> tuple[MoaSettings, list[str]]:
    """The settings from a raw ``moa`` section and every problem in it."""
    section = _mapping(raw) or {}
    problems: list[str] = []
    if raw is not None and _mapping(raw) is None:
        problems.append("moa: must be a mapping")
    raw_presets = section.get("presets")
    presets: dict[str, Preset] = {}
    table = _mapping(raw_presets)
    if raw_presets is not None and table is None:
        problems.append("moa.presets: must be a mapping of name → preset")
    names = [str(n) for n in (table or {})]
    for name in names:
        preset, found = validate_preset(
            name, (table or {})[name], known_provider=known_provider, preset_names=names
        )
        problems.extend(found)
        if preset is not None:
            presets[name] = preset
    default = _text(section.get("default_preset"))
    if default and default not in names:
        problems.append(f"moa.default_preset: no preset is called {default!r}")
    timeout, problem = _timeout(section.get("reference_timeout"))
    if problem:
        problems.append(f"moa.reference_timeout: {problem}")
    privacy = section.get("privacy_filter")
    mode = privacy_mode(privacy)
    if privacy not in (None, "", False) and mode == "" and privacy is not True:
        problems.append("moa.privacy_filter: must be empty, display or full")
    settings = MoaSettings(
        presets=presets,
        default_preset=default if default in presets else "",
        reference_timeout=timeout,
        privacy_filter=mode,
        problems=tuple(problems),
    )
    return settings, problems


def read_settings(raw: object) -> MoaSettings:
    """Tolerant read (no provider lookup): broken presets are left out, problems kept."""
    settings, _problems = validate_section(raw)
    return settings


def _config_value(scope: Scope, key: str) -> object:
    view = scope.config
    if view is None:
        from indus.foundation import config

        view = config.load(scope)
    try:
        return view.get(key)
    except Exception:
        return None


def settings_for(scope: Scope) -> MoaSettings:
    """The scope's ``moa`` settings (tolerant)."""
    return read_settings(_config_value(scope, "moa"))


def preset_names(scope: Scope) -> list[str]:
    """Names of the usable presets, sorted (for ``/moa`` completion)."""
    try:
        return sorted(settings_for(scope).presets)
    except Exception:
        return []


def known_provider_check(scope: Scope) -> Callable[[str], bool]:
    """``check(name)``: the provider registry knows ``name`` (bundled or configured)."""
    from indus.providers.registry import Registry

    registry = Registry(scope)
    return lambda name: registry.lookup(name) is not None


def preset_to_config(preset: Preset) -> dict[str, Any]:
    """The preset as it is written under ``moa.presets.<name>``."""
    out: dict[str, Any] = {"reference_models": [slot.to_config() for slot in preset.references]}
    if preset.aggregator is not None:
        out["aggregator"] = preset.aggregator.to_config(with_enabled=False)
    if preset.reference_timeout is not None:
        out["reference_timeout"] = preset.reference_timeout
    if preset.fanout != "per_iteration":
        out["fanout"] = preset.fanout
    return out
