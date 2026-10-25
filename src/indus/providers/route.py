"""Route resolution: which provider, model, endpoint, wire format and key a call uses.

Precedence for every part of the route: explicit arguments, then the saved config (``model.*``),
then the environment (``INDUS_INFERENCE_PROVIDER``, ``INDUS_INFERENCE_MODEL`` and a profile's own
base-URL variable), then the profile's defaults. Saved config beats stale shell exports
(R01.11, R04.9).

A key is attached only to its own host: a vendor's key goes to that vendor's exact hostname and
nowhere else; the generic ``OPENAI_API_KEY`` of custom endpoints is never sent to another bundled
vendor's host. Keys are looked up in the secret store first, then in the scope's env view, and
placeholder-shaped values count as missing. When no key variable resolves, the provider's
credential pool (P08-12; keys added with ``indus auth add``) may supply one.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from indus.foundation.errors import KeyHostMismatch, NoUsableProvider, ProviderError
from indus.foundation.scope import Scope
from indus.providers import bundled
from indus.providers.hosts import api_mode_from_url, is_loopback, parse_base_url
from indus.providers.profile import ACTIVE_API_MODES, API_MODES, USER_AGENT, ProviderProfile
from indus.providers.registry import Registry
from indus.providers.settings import config_of, model_section, text

log = logging.getLogger(__name__)

PROVIDER_ENV = "INDUS_INFERENCE_PROVIDER"
MODEL_ENV = "INDUS_INFERENCE_MODEL"
GENERIC_KEY_ENV = "OPENAI_API_KEY"
AUTO = "auto"

SOURCE_EXPLICIT = "explicit"
SOURCE_CONFIG = "config"
SOURCE_ENV = "env"
SOURCE_AUTO = "auto"

MIN_KEY_LENGTH = 8
# Values that are a key prefix with nothing after it, or the usual "fill me in" words.
_BARE_PREFIXES = frozenset(
    {"sk-", "sk-proj-", "sk-ant-", "sk-or-", "sk-or-v1-", "ghp_", "hf_", "xai-", "gsk_", "nvapi-"}
)
_PLACEHOLDER_WORDS = (
    "your-key",
    "your_key",
    "your-api-key",
    "your_api_key",
    "yourapikey",
    "api-key-here",
    "api_key_here",
    "key-here",
    "placeholder",
    "changeme",
    "change-me",
    "replace-me",
    "replace_me",
)
_ANGLE = re.compile(r"^<.*>$")


def is_placeholder(value: str | None) -> bool:
    """True for missing, too short or obviously not-yet-filled-in key values."""
    if value is None:
        return True
    text_value = value.strip()
    lowered = text_value.lower()
    if len(text_value) < MIN_KEY_LENGTH or lowered in _BARE_PREFIXES:
        return True
    if any(ch.isspace() for ch in text_value):
        return True
    if _ANGLE.match(text_value) or text_value.startswith("${"):
        return True
    if "xxx" in lowered or any(word in lowered for word in _PLACEHOLDER_WORDS):
        return True
    return len(set(text_value)) == 1


class Secret:
    """A key value that never shows itself in ``repr`` or ``str``."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def fingerprint(self) -> str:
        """A short stable digest, for cache keys and logs (never the value)."""
        return hashlib.sha256(self._value.encode("utf-8")).hexdigest()[:16]

    def __repr__(self) -> str:
        return "Secret('[redacted]')"

    def __str__(self) -> str:
        return "[redacted]"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(("secret", self.fingerprint()))

    def __bool__(self) -> bool:
        return bool(self._value)


@dataclass(frozen=True)
class RuntimeRoute:
    provider: str
    model: str
    api_mode: str
    base_url: str
    api_key: Secret | None = None
    source: str = SOURCE_CONFIG
    headers: Mapping[str, str] = field(default_factory=dict, hash=False)
    key_env: str = ""
    profile: ProviderProfile | None = field(default=None, compare=False, repr=False)
    warnings: tuple[str, ...] = ()

    @property
    def host(self) -> str:
        return parse_base_url(self.base_url).host

    @property
    def is_local(self) -> bool:
        return is_loopback(self.base_url)


# API modes whose wire carries images inside a tool result (P10-12, P10-15): Anthropic's
# ``tool_result`` blocks take image parts; the other transports send a tool result as text only.
TOOL_IMAGE_API_MODES = frozenset({"anthropic_messages"})


def sees_tool_images(route: object) -> bool:
    """Does the main model see an image a tool returns? The route's provider declares vision and
    its wire carries images in tool results."""
    if not isinstance(route, RuntimeRoute) or route.api_mode not in TOOL_IMAGE_API_MODES:
        return False
    return route.profile is not None and route.profile.supports_vision


# -- keys ---------------------------------------------------------------------------------------


def _secret_value(scope: Scope, name: str) -> str | None:
    """Store first, then the scope's env view; a placeholder in either place is skipped."""
    if scope.secrets is not None:
        stored = scope.secrets.get(name)
        if not is_placeholder(stored):
            return stored
    from_env = scope.env.get(name)
    return None if is_placeholder(from_env) else from_env


def find_key(scope: Scope, names: tuple[str, ...]) -> tuple[str, str] | None:
    """The first usable ``(env name, value)`` in priority order."""
    for name in names:
        value = _secret_value(scope, name)
        if value is not None:
            return name, value.strip()
    return None


def pool_key_for(scope: Scope, profile: ProviderProfile) -> tuple[str, str] | None:
    """``(env name or "", key)`` from ``profile``'s credential pool (a peek), or None."""
    from indus.providers.pool import pool_key

    return pool_key(scope, profile)


def _vendor_owning(url: str) -> ProviderProfile | None:
    """The bundled vendor whose exact host ``url`` points at, if any."""
    host = parse_base_url(url).host
    for profile in bundled.PROFILES:
        if profile.family == "vendor" and profile.hostname == host:
            return profile
    return None


def key_allowed(profile: ProviderProfile, url: str, key_env: str) -> bool:
    """May the key read from ``key_env`` for ``profile`` be sent to ``url``?"""
    parsed = parse_base_url(url)
    if profile.family == "vendor":
        if not profile.hostname or parsed.host != profile.hostname:
            return False
        own = parse_base_url(profile.base_url) if profile.base_url else None
        downgraded = own is not None and own.scheme == "https" and parsed.scheme != "https"
        return not (downgraded and not is_loopback(url))
    owner = _vendor_owning(url)
    return owner is None or key_env in owner.env_vars


# -- resolution ---------------------------------------------------------------------------------


@dataclass
class _Draft:
    profile: ProviderProfile
    source: str
    base_url: str = ""
    warnings: list[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        log.warning("%s", message)
        self.warnings.append(message)


def _all_key_names(candidates: list[ProviderProfile]) -> tuple[str, ...]:
    names: dict[str, None] = {}
    for profile in candidates:
        for name in profile.env_vars:
            names.setdefault(name, None)
    return tuple(names)


def _auto_profile(scope: Scope, registry: Registry, saved_url: str) -> tuple[ProviderProfile, str]:
    """``auto``: a saved endpoint picks its owner (or ``custom``); else the first keyed profile."""
    if saved_url:
        owner = _vendor_owning(saved_url)
        if owner is not None:
            return owner, saved_url
        custom = registry.lookup("custom")
        assert custom is not None
        return custom, saved_url
    candidates = registry.auto_detect_candidates()
    for profile in candidates:
        if find_key(scope, profile.env_vars) is not None:
            return profile, ""
    for profile in candidates:
        if profile.auth_type != "none" and pool_key_for(scope, profile) is not None:
            return profile, ""
    names = _all_key_names(candidates)
    raise NoUsableProvider(
        "no model provider is set up: no API key was found",
        env_names=names,
        hint="set one of " + ", ".join(names) + " (or pick a provider and model in the config)",
    )


def _stale_check(draft: _Draft, url: str, where: str) -> str:
    """Drop ``url`` when it sits on another bundled vendor's host (left over from a switch)."""
    if draft.profile.family != "vendor":
        return url
    owner = _vendor_owning(url)
    if owner is not None and owner.name != draft.profile.name:
        draft.warn(
            f"ignored {where} {url}: it belongs to {owner.label}, not {draft.profile.label} "
            "(left over from an earlier provider?)"
        )
        return ""
    return url


def _choose_mode(draft: _Draft, explicit: str, saved: str) -> str:
    profile = draft.profile
    if explicit:
        mode = explicit
    elif saved:
        mode = saved
    elif profile.is_custom and draft.base_url != profile.base_url:
        mode = api_mode_from_url(draft.base_url)
    else:
        mode = profile.api_mode
    if mode not in API_MODES:
        raise ProviderError(f"api mode {mode!r} is not known (use one of {sorted(API_MODES)})")
    if mode not in ACTIVE_API_MODES:
        raise ProviderError(f"api mode {mode!r} is not available in this version")
    return mode


def resolve(
    scope: Scope,
    *,
    model: str | None = None,
    provider: str | None = None,
    base_url: str | None = None,
    api_mode: str | None = None,
    registry: Registry | None = None,
) -> RuntimeRoute:
    """Resolve the route for one call; see the module docstring for the rules."""
    registry = registry if registry is not None else Registry(scope)
    saved = model_section(config_of(scope))
    saved_provider = text(saved.get("provider")).lower()
    saved_url = text(saved.get("base_url")).rstrip("/")
    saved_mode = text(saved.get("api_mode"))
    explicit_url = (base_url or "").strip().rstrip("/")

    if provider and provider.strip():
        requested, source = provider.strip().lower(), SOURCE_EXPLICIT
    elif saved_provider:
        requested, source = saved_provider, SOURCE_CONFIG
    elif scope.env.get(PROVIDER_ENV, "").strip():
        requested, source = scope.env[PROVIDER_ENV].strip().lower(), SOURCE_ENV
    else:
        requested, source = AUTO, SOURCE_AUTO

    saved_is_ours = False
    if requested == AUTO:
        auto_url = explicit_url or (saved_url if saved_provider in ("", AUTO) else "")
        if auto_url:
            parse_base_url(auto_url)
        profile, picked_url = _auto_profile(scope, registry, auto_url)
        draft = _Draft(profile, SOURCE_AUTO if source == SOURCE_AUTO else source, picked_url)
        saved_is_ours = bool(picked_url) and picked_url == saved_url
    else:
        found = registry.lookup(requested)
        if found is None:
            raise NoUsableProvider(
                f"unknown provider {requested!r}",
                hint="pick one of the listed providers or add it under providers: in the config",
            )
        draft = _Draft(found, source)
        saved_owner = registry.lookup(saved_provider) if saved_provider else None
        saved_is_ours = saved_owner is not None and saved_owner.name == found.name
        if saved_url and not saved_is_ours and saved_provider not in ("", AUTO):
            log.debug("model.base_url belongs to %s, not %s; not used", saved_provider, found.name)
        if not saved_is_ours and saved_provider in ("", AUTO) and found.name == "custom":
            saved_is_ours = True

    profile = draft.profile
    if not draft.base_url:
        if explicit_url:
            draft.base_url = explicit_url
        elif saved_is_ours and saved_url:
            draft.base_url = _stale_check(draft, saved_url, "model.base_url")
        if not draft.base_url and profile.base_url_env_var:
            env_url = scope.env.get(profile.base_url_env_var, "").strip().rstrip("/")
            if env_url:
                draft.base_url = _stale_check(draft, env_url, profile.base_url_env_var)
        if not draft.base_url:
            draft.base_url = profile.base_url.rstrip("/")
    if not draft.base_url:
        names = (profile.base_url_env_var,) if profile.base_url_env_var else ()
        raise NoUsableProvider(
            f"provider {profile.name!r} has no endpoint URL",
            env_names=names,
            hint="set model.base_url (or providers.<name>.base_url) in the config",
        )
    parse_base_url(draft.base_url)

    mode = _choose_mode(draft, (api_mode or "").strip(), saved_mode if saved_is_ours else "")

    key: Secret | None = None
    key_env = ""
    if profile.auth_type != "none":
        found_key = find_key(scope, profile.env_vars) or pool_key_for(scope, profile)
        if found_key is not None:
            name, value = found_key
            if key_allowed(profile, draft.base_url, name):
                key, key_env = Secret(value), name
            elif profile.family == "vendor":
                raise KeyHostMismatch(
                    f"the {profile.label} key ({name or 'credential pool'}) is only sent to "
                    f"{profile.hostname}, "
                    f"not to {parse_base_url(draft.base_url).host}",
                    hint="for a proxy or another endpoint use provider 'custom' or a named "
                    "provider under providers: with its own key_env",
                )
            else:
                draft.warn(
                    f"{name} was not sent to {draft.base_url}: that host belongs to another "
                    "provider; select that provider instead"
                )
        if key is None and profile.signs_in:
            raise NoUsableProvider(
                f"not signed in to {profile.label}",
                hint=f"sign in with `indus auth add {profile.name}`",
            )
        if key is None and profile.needs_key:
            raise NoUsableProvider(
                f"no usable API key for {profile.label}",
                env_names=profile.env_vars,
                hint="set " + " or ".join(profile.env_vars),
            )

    if model and model.strip():
        chosen_model = model.strip()
    else:
        chosen_model = text(saved.get("default")) or scope.env.get(MODEL_ENV, "").strip()

    return RuntimeRoute(
        provider=profile.name,
        model=profile.resolve_model(chosen_model),
        api_mode=mode,
        base_url=draft.base_url,
        api_key=key,
        source=draft.source,
        headers={"User-Agent": USER_AGENT, **profile.default_headers},
        key_env=key_env,
        profile=profile,
        warnings=tuple(draft.warnings),
    )
