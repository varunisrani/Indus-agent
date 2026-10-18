"""``Registry``: find a provider profile by name or alias for one profile home.

Layers, in order: the bundled catalogue, the local-server profiles, named custom providers from the
config's ``providers:`` section, and profiles registered at run time by plugins (P13). Lookups are
case-insensitive and return ``None`` for unknown names. The config layer is rebuilt whenever the
``providers:`` section changes.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from indus.foundation.errors import InvalidBaseURL, InvalidProviderProfile, ProviderConflict
from indus.foundation.scope import Scope
from indus.providers import bundled
from indus.providers.hosts import api_mode_from_url
from indus.providers.profile import ACTIVE_API_MODES, ENV_NAME_RE, NAME_RE, ProviderProfile
from indus.providers.settings import (
    as_mapping,
    config_of,
    positive_int,
    providers_section,
    text,
)

log = logging.getLogger(__name__)

SOURCE_BUNDLED = "bundled"
SOURCE_LOCAL = "local"
SOURCE_CONFIG = "config"

# Local inference servers on their documented default ports (LM Studio is in the catalogue).
# Ollama: https://docs.ollama.com/api/openai-compatibility; vLLM: `vllm serve` default port 8000;
# llama.cpp: llama-server default port 8080 (tools/server README).
LOCAL_SERVERS: tuple[ProviderProfile, ...] = (
    ProviderProfile(
        name="ollama",
        display_name="Ollama (local)",
        description="Ollama on this machine",
        base_url="http://127.0.0.1:11434/v1",
        auth_type="api_key_optional",
        family="custom",
        auto_detect=False,
    ),
    ProviderProfile(
        name="vllm",
        display_name="vLLM (local)",
        description="vLLM server on this machine",
        base_url="http://127.0.0.1:8000/v1",
        auth_type="api_key_optional",
        family="custom",
        auto_detect=False,
    ),
    ProviderProfile(
        name="llamacpp",
        aliases=("llama.cpp", "llama-cpp"),
        display_name="llama.cpp (local)",
        description="llama.cpp server on this machine",
        base_url="http://127.0.0.1:8080/v1",
        auth_type="api_key_optional",
        family="custom",
        auto_detect=False,
    ),
)
LOCAL_SERVER_NAMES = frozenset({"lmstudio", *(p.name for p in LOCAL_SERVERS)})

# Keys a ``providers.<name>`` entry may carry (others are kept but warned about once).
CUSTOM_KEYS = frozenset(
    {
        "base_url",
        "api_mode",
        "key_env",
        "models",
        "context_length",
        "aliases",
        "display_name",
        "description",
        "override",
        "request_timeout_seconds",
        "stale_timeout_seconds",
        "max_tokens_param",
        "api_key",
    }
)


@dataclass(frozen=True)
class _Entry:
    profile: ProviderProfile
    source: str


def _context_hook(
    provider_level: int | None, per_model: Mapping[str, int]
) -> Callable[[str], int | None]:
    def context_length(model: str) -> int | None:
        return per_model.get(model, provider_level)

    return context_length


def _per_model_context(models: object) -> dict[str, int]:
    out: dict[str, int] = {}
    for model_id, settings in as_mapping(models).items():
        value = positive_int(as_mapping(settings).get("context_length"))
        if value is not None:
            out[str(model_id)] = value
    return out


def _detected_mode(name: str, base_url: str) -> str:
    mode = api_mode_from_url(base_url)
    if mode not in ACTIVE_API_MODES:
        log.info(
            "provider %s: %s suggests the %s API, which arrives later; using chat completions",
            name,
            base_url,
            mode,
        )
        return "chat_completions"
    return mode


def custom_profile_from_config(name: str, raw: object) -> ProviderProfile | None:
    """A ``custom``-family profile for ``providers.<name>``, or None when it is only settings.

    Raises ``InvalidProviderProfile`` / ``InvalidBaseURL`` for an entry that names a ``base_url``
    but cannot be turned into a profile.
    """
    entry = as_mapping(raw)
    base_url = text(entry.get("base_url")).rstrip("/")
    if not base_url:
        return None
    if not NAME_RE.match(name):
        raise InvalidProviderProfile(
            f"providers.{name}: the name must be lowercase letters, digits, '.', '_' or '-'"
        )
    unknown = sorted(set(entry) - CUSTOM_KEYS)
    if unknown:
        log.warning("providers.%s: unknown settings kept but unused: %s", name, ", ".join(unknown))
    if "api_key" in entry:
        log.warning(
            "providers.%s.api_key is ignored: keep keys in the secret store or .env and name the "
            "variable with key_env",
            name,
        )
    key_env = text(entry.get("key_env"))
    if key_env and not ENV_NAME_RE.match(key_env):
        raise InvalidProviderProfile(f"providers.{name}.key_env {key_env!r} is not an env name")
    api_mode = text(entry.get("api_mode")) or _detected_mode(name, base_url)
    aliases_raw = entry.get("aliases")
    aliases = tuple(
        str(a).strip().lower() for a in (aliases_raw if isinstance(aliases_raw, list) else [])
    )
    models = entry.get("models")
    per_model = _per_model_context(models)
    provider_level = positive_int(entry.get("context_length"))
    return ProviderProfile(
        name=name,
        aliases=aliases,
        display_name=text(entry.get("display_name")) or name,
        description=text(entry.get("description")) or "custom endpoint from config",
        base_url=base_url,
        api_mode=api_mode,
        auth_type="api_key_optional",
        env_vars=(key_env,) if key_env else ("OPENAI_API_KEY",),
        family="custom",
        auto_detect=False,
        max_tokens_param=text(entry.get("max_tokens_param")) or "max_tokens",
        context_length_hint=provider_level,
        context_length=_context_hook(provider_level, per_model),
    )


class Registry:
    """Provider profiles visible to one scope."""

    def __init__(self, scope: Scope) -> None:
        self._scope = scope
        self._lock = threading.RLock()
        self._plugins: list[tuple[_Entry, bool]] = []
        self._signature: str | None = None
        self._entries: dict[str, _Entry] = {}
        self._aliases: dict[str, str] = {}
        self._order: list[str] = []
        bundled.register_managed_env_names()
        self._rebuild(self._config_providers())

    # -- building -------------------------------------------------------------------------------

    def _config_providers(self) -> Mapping[str, Any]:
        return providers_section(config_of(self._scope))

    @staticmethod
    def _signature_of(section: Mapping[str, Any]) -> str:
        return json.dumps(section, sort_keys=True, default=str)

    def _add(self, entry: _Entry, *, replace: bool = False) -> None:
        profile = entry.profile
        names = profile.all_names()
        if not replace:
            taken = [n for n in names if n in self._entries or n in self._aliases]
            if taken:
                raise ProviderConflict(
                    f"provider {profile.name!r} ({entry.source}): name or alias already taken: "
                    + ", ".join(taken)
                )
        else:
            self._remove(profile.name)
            for alias in profile.aliases:
                owner = self._aliases.get(alias) or (alias if alias in self._entries else None)
                if owner is not None and owner != profile.name:
                    raise ProviderConflict(
                        f"provider {profile.name!r}: alias {alias!r} belongs to {owner!r}"
                    )
        self._entries[profile.name] = entry
        for alias in profile.aliases:
            self._aliases[alias] = profile.name
        if profile.name not in self._order:
            self._order.append(profile.name)

    def _remove(self, name: str) -> None:
        old = self._entries.pop(name, None)
        if old is None:
            return
        for alias in old.profile.aliases:
            if self._aliases.get(alias) == name:
                del self._aliases[alias]

    def _rebuild(self, section: Mapping[str, Any]) -> None:
        self._entries, self._aliases, self._order = {}, {}, []
        for profile in bundled.PROFILES:
            self._add(_Entry(profile, SOURCE_BUNDLED))
        for profile in LOCAL_SERVERS:
            self._add(_Entry(profile, SOURCE_LOCAL))
        for raw_name in sorted(section, key=lambda n: str(n).lower()):
            name = str(raw_name).strip().lower()
            raw = section[raw_name]
            try:
                profile = custom_profile_from_config(name, raw)
            except (InvalidProviderProfile, InvalidBaseURL) as exc:
                log.warning("skipped providers.%s: %s", raw_name, exc)
                continue
            if profile is None:
                if self._canonical(name) is None:
                    log.warning("providers.%s has no base_url and names no known provider", name)
                continue
            existing = self._canonical(name)
            override = as_mapping(raw).get("override") is True
            if existing is not None:
                current = self._entries[existing]
                if current.source == SOURCE_BUNDLED and existing not in LOCAL_SERVER_NAMES:
                    if not override:
                        log.warning(
                            "providers.%s would replace the built-in provider %r; add "
                            "'override: true' to that entry if that is what you want",
                            name,
                            existing,
                        )
                        continue
                    log.warning("providers.%s replaces the built-in provider %r", name, existing)
                if existing != name:
                    log.warning("providers.%s is an alias of %r; skipped", name, existing)
                    continue
            try:
                self._add(_Entry(profile, SOURCE_CONFIG), replace=existing is not None)
            except ProviderConflict as exc:
                log.warning("skipped providers.%s: %s", name, exc)
                continue
        for entry, override in self._plugins:
            try:
                self._add(entry, replace=override)
            except ProviderConflict as exc:
                log.warning("plugin provider skipped after a config change: %s", exc)
        self._signature = self._signature_of(section)

    def refresh(self) -> bool:
        """Rebuild when the config's ``providers:`` section changed; True when it did."""
        section = self._config_providers()
        signature = self._signature_of(section)
        with self._lock:
            if signature == self._signature:
                return False
            self._rebuild(section)
            return True

    # -- queries --------------------------------------------------------------------------------

    def _canonical(self, name: str) -> str | None:
        key = name.strip().lower()
        if key in self._entries:
            return key
        return self._aliases.get(key)

    def lookup(self, name: str) -> ProviderProfile | None:
        """The profile called ``name`` (or with that alias), case-insensitive; None if unknown."""
        if not name or not name.strip():
            return None
        self.refresh()
        with self._lock:
            canonical = self._canonical(name)
            return self._entries[canonical].profile if canonical else None

    get = lookup

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.lookup(name) is not None

    def source_of(self, name: str) -> str | None:
        self.refresh()
        with self._lock:
            canonical = self._canonical(name)
            return self._entries[canonical].source if canonical else None

    def list_providers(self, *, include_hidden: bool = False) -> list[ProviderProfile]:
        """Catalogue order (a replaced built-in keeps its place), then local servers, then config
        custom providers by name, then plugin profiles in registration order."""
        self.refresh()
        builtin = [p.name for p in bundled.PROFILES] + [p.name for p in LOCAL_SERVERS]
        builtin_set = set(builtin)
        with self._lock:
            names = [n for n in builtin if n in self._entries]
            names += sorted(
                n
                for n, e in self._entries.items()
                if e.source == SOURCE_CONFIG and n not in builtin_set
            )
            names += [
                n
                for n in self._order
                if n in self._entries
                and n not in builtin_set
                and self._entries[n].source not in (SOURCE_BUNDLED, SOURCE_LOCAL, SOURCE_CONFIG)
            ]
            profiles = [self._entries[n].profile for n in names]
        return [p for p in profiles if include_hidden or not p.hidden]

    def auto_detect_candidates(self) -> list[ProviderProfile]:
        """Profiles ``auto`` may pick, in catalogue order."""
        return [p for p in self.list_providers() if p.auto_detect]

    # -- plugins --------------------------------------------------------------------------------

    def register(self, profile: ProviderProfile, source: str, *, override: bool = False) -> None:
        """Add a plugin profile. Taking an existing name or alias needs ``override=True``."""
        if not source or source in (SOURCE_BUNDLED, SOURCE_LOCAL, SOURCE_CONFIG):
            raise ProviderConflict(f"plugin source {source!r} is reserved or empty")
        entry = _Entry(profile, source)
        self.refresh()
        with self._lock:
            self._add(entry, replace=override)
            if override:
                log.warning(
                    "provider %r from %s replaces an existing profile", profile.name, source
                )
            self._plugins.append((entry, override))
