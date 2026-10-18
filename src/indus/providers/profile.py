"""``ProviderProfile``: a declarative description of one model provider.

A profile says how to reach a provider (endpoint, wire format, key names, headers) and which
request quirks it has. It owns no clients, connection pools or streams; ``http``, the transports
and the streaming driver read it. Hooks are plain callables with no-op defaults so a vendor can
adjust a request body, classify its own errors or report a context window without subclassing.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from indus import __version__
from indus.foundation.errors import InvalidBaseURL, InvalidProviderProfile
from indus.providers.hosts import normalise_host, parse_base_url

USER_AGENT = f"indus/{__version__}"

# Wire formats: P03 implements the first two, P08 the Responses, Bedrock and Gemini ones.
ACTIVE_API_MODES = frozenset(
    {"chat_completions", "anthropic_messages", "responses", "bedrock", "gemini"}
)
API_MODES = ACTIVE_API_MODES
AUTH_TYPES = frozenset({"api_key", "api_key_optional", "none"})
MAX_TOKENS_PARAMS = frozenset({"max_tokens", "max_completion_tokens"})
FAMILIES = frozenset({"vendor", "custom"})
REASONING_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
# A product token in the RFC 9110 sense: ``Name/1.2``. Any such token that is not ours means the
# header claims to be another program (R01.12).
_PRODUCT_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9._-])([A-Za-z][A-Za-z0-9._-]*)/\d")

BodyHook = Callable[[dict[str, Any], str], dict[str, Any]]
ErrorHook = Callable[[object], object | None]
ContextHook = Callable[[str], int | None]


def keep_body(body: dict[str, Any], model: str) -> dict[str, Any]:
    """Default ``prepare_body``: the body unchanged."""
    return body


def no_verdict(ctx: object) -> object | None:
    """Default ``classify_error``: no opinion, the generic classifier decides."""
    return None


def no_context_length(model: str) -> int | None:
    """Default ``context_length``: unknown."""
    return None


def _frozen_map(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType(dict(value or {}))


def _bad(name: str, why: str) -> InvalidProviderProfile:
    return InvalidProviderProfile(f"provider profile {name!r}: {why}")


def foreign_product_tokens(value: str) -> list[str]:
    """Product tokens (``Name/1.0``) in a header value that do not name this product."""
    return [m.group(1) for m in _PRODUCT_TOKEN_RE.finditer(value) if m.group(1).lower() != "indus"]


@dataclass(frozen=True)
class ProviderProfile:
    name: str
    base_url: str = ""
    api_mode: str = "chat_completions"
    auth_type: str = "api_key"
    env_vars: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    display_name: str = ""
    description: str = ""
    base_url_env_var: str = ""
    models_url: str = ""
    hostname: str = ""
    default_headers: Mapping[str, str] = field(default_factory=dict, hash=False)
    supports_streaming: bool = True
    supports_vision: bool = False
    supports_model_listing: bool = True
    max_tokens_param: str = "max_tokens"
    default_max_tokens: int | None = None
    fixed_temperature: float | None = None
    context_length_hint: int | None = None
    model_aliases: Mapping[str, str] = field(default_factory=dict, hash=False)
    hidden: bool = False
    # ``vendor``: a named service with its own host. ``custom``: an endpoint the user chose
    # (bare ``custom``, named custom providers, local servers).
    family: str = "vendor"
    # False keeps the profile out of ``auto`` provider detection (local servers, bare custom).
    auto_detect: bool = True
    # Ladder levels the provider accepts (None = not declared; the transport keeps its default).
    reasoning_efforts: tuple[str, ...] | None = None
    # Substrings of model ids that think before answering (stale-timeout floor, P03-13).
    reasoning_models: tuple[str, ...] = ()
    # Substrings of model ids that take an effort field instead of a thinking budget (P03-10).
    adaptive_thinking_models: tuple[str, ...] = ()
    # Anthropic-wire hosts that document ``Authorization: Bearer`` instead of ``x-api-key``.
    bearer_auth: bool = False
    # Fast mode (P08-15): request fields that ask for the provider's priority tier, headers that
    # must go with them, and the exact model ids that accept them (empty: every model). Sent only
    # to the profile's own host.
    fast_body: Mapping[str, Any] = field(default_factory=dict, hash=False)
    fast_headers: Mapping[str, str] = field(default_factory=dict, hash=False)
    fast_models: tuple[str, ...] = ()
    prepare_body: BodyHook = keep_body
    classify_error: ErrorHook = no_verdict
    context_length: ContextHook = no_context_length

    def __post_init__(self) -> None:
        name = self.name
        if not isinstance(name, str) or not NAME_RE.match(name):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise _bad(str(name), "name must be lowercase letters, digits, '.', '_' or '-'")
        for alias in self.aliases:
            if not NAME_RE.match(alias):
                raise _bad(
                    name, f"alias {alias!r} must be lowercase letters, digits, '.', '_', '-'"
                )
        if self.api_mode not in API_MODES:
            raise _bad(name, f"api_mode {self.api_mode!r} is not one of {sorted(API_MODES)}")
        if self.auth_type not in AUTH_TYPES:
            raise _bad(name, f"auth_type {self.auth_type!r} is not one of {sorted(AUTH_TYPES)}")
        if self.max_tokens_param not in MAX_TOKENS_PARAMS:
            raise _bad(name, f"max_tokens_param {self.max_tokens_param!r} is not supported")
        if self.family not in FAMILIES:
            raise _bad(name, f"family {self.family!r} is not one of {sorted(FAMILIES)}")
        for env in (*self.env_vars, *((self.base_url_env_var,) if self.base_url_env_var else ())):
            if not ENV_NAME_RE.match(env):
                raise _bad(name, f"env name {env!r} must match {ENV_NAME_RE.pattern}")
        if self.reasoning_efforts is not None:
            unknown = [e for e in self.reasoning_efforts if e not in REASONING_LEVELS]
            if unknown:
                raise _bad(name, f"unknown reasoning levels {unknown}")
        for number, what in (
            (self.default_max_tokens, "default_max_tokens"),
            (self.context_length_hint, "context_length_hint"),
        ):
            if number is not None and (not isinstance(number, int) or number <= 0):  # pyright: ignore[reportUnnecessaryIsInstance]
                raise _bad(name, f"{what} must be a positive integer")
        if self.auth_type == "api_key" and not self.base_url:
            raise _bad(name, "an api_key provider needs a base_url to send the key to")
        host = self._checked_hosts()
        object.__setattr__(self, "hostname", host)
        object.__setattr__(self, "aliases", tuple(self.aliases))
        object.__setattr__(self, "env_vars", tuple(self.env_vars))
        object.__setattr__(self, "default_headers", self._checked_headers())
        object.__setattr__(self, "model_aliases", _frozen_map(self.model_aliases))
        object.__setattr__(self, "fast_body", MappingProxyType(dict(self.fast_body)))
        object.__setattr__(self, "fast_headers", self._checked_fast_headers())
        object.__setattr__(self, "fast_models", tuple(self.fast_models))

    def _checked_hosts(self) -> str:
        derived = ""
        for url, what in ((self.base_url, "base_url"), (self.models_url, "models_url")):
            if not url:
                continue
            try:
                parsed = parse_base_url(url)
            except InvalidBaseURL as exc:
                raise _bad(self.name, f"{what}: {exc}") from exc
            if what == "base_url":
                derived = parsed.host
        if self.hostname:
            try:
                return normalise_host(self.hostname)
            except (UnicodeError, ValueError) as exc:
                raise _bad(self.name, f"hostname {self.hostname!r} is not a host name") from exc
        return derived

    def _checked_fast_headers(self) -> Mapping[str, str]:
        headers: dict[str, str] = {}
        for key, value in dict(self.fast_headers).items():
            if not isinstance(key, str) or not isinstance(value, str):  # pyright: ignore[reportUnnecessaryIsInstance]
                raise _bad(self.name, "fast-mode header names and values must be strings")
            if not _HEADER_NAME_RE.match(key) or "\r" in value or "\n" in value:
                raise _bad(self.name, f"fast-mode header {key!r} is not valid")
            headers[key] = value
        return MappingProxyType(headers)

    def _checked_headers(self) -> Mapping[str, str]:
        headers: dict[str, str] = {}
        for key, value in dict(self.default_headers).items():
            if not isinstance(key, str) or not isinstance(value, str):  # pyright: ignore[reportUnnecessaryIsInstance]
                raise _bad(self.name, "header names and values must be strings")
            if not _HEADER_NAME_RE.match(key):
                raise _bad(self.name, f"header name {key!r} is not valid")
            if "\r" in value or "\n" in value:
                raise _bad(self.name, f"header {key!r} contains a line break")
            if (
                key.lower() == "user-agent"
                and value != USER_AGENT
                and not value.startswith("indus/")
            ):
                raise _bad(self.name, f"User-Agent must be ours ({USER_AGENT}), not {value!r}")
            foreign = foreign_product_tokens(value)
            if foreign:
                raise _bad(
                    self.name,
                    f"header {key!r} names another product ({', '.join(foreign)}); "
                    "requests only ever identify as this product",
                )
            headers[key] = value
        return MappingProxyType(headers)

    @property
    def label(self) -> str:
        return self.display_name or self.name

    @property
    def needs_key(self) -> bool:
        return self.auth_type == "api_key"

    @property
    def is_custom(self) -> bool:
        return self.family == "custom"

    def all_names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)

    def resolve_model(self, model: str) -> str:
        """The provider's id for ``model`` when it is one of the profile's model aliases."""
        return self.model_aliases.get(model, model)

    def is_reasoning_model(self, model: str) -> bool:
        lowered = model.lower()
        return any(p.lower() in lowered for p in self.reasoning_models)
