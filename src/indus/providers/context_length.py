"""How many tokens the model behind a route can hold.

Order (first answer wins): a config override; the profile's own hook or hint; the persistent cache
of provider-confirmed values; the live ``/models`` list of a custom or local endpoint; our family
table; 256,000 with one warning per (model, base URL). Windows below 64,000 tokens are refused
(``ContextTooSmall``): sessions and model switches need that much working room (A35).
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from indus.foundation.errors import ContextTooSmall
from indus.foundation.fsio import atomic_write_json
from indus.foundation.scope import Scope
from indus.providers.model_list import ModelInfo
from indus.providers.route import RuntimeRoute
from indus.providers.settings import (
    as_mapping,
    config_of,
    model_entry,
    model_section,
    positive_int,
    provider_entry,
    text,
)

log = logging.getLogger(__name__)

MIN_CONTEXT = 64_000
FALLBACK_CONTEXT = 256_000
SANE_RANGE = (1_024, 10_000_000)
CACHE_DIR = "cache"
CACHE_FILE = "context_lengths.json"
CACHE_VERSION = 1
# Only values a provider told us are persisted; table guesses and the fallback never are.
CONFIRMED_SOURCES = frozenset({"endpoint", "provider-error"})

SOURCE_CONFIG = "config"
SOURCE_PROFILE = "profile"
SOURCE_CACHE = "cache"
SOURCE_ENDPOINT = "endpoint"
SOURCE_FAMILY = "family"
SOURCE_FALLBACK = "fallback"

# Context windows by model family, matched by the longest key contained in the lowercased model
# id. Values come from the vendors' model pages (checked 2026-10-18):
#   Anthropic  https://docs.anthropic.com/en/docs/about-claude/models/overview
#   OpenAI     https://platform.openai.com/docs/models
#   DeepSeek   https://api-docs.deepseek.com/quick_start/pricing
#   xAI        https://docs.x.ai/docs/models
#   Google     https://ai.google.dev/gemini-api/docs/models
#   Mistral    https://docs.mistral.ai/getting-started/models/
#   Qwen       https://www.alibabacloud.com/help/en/model-studio/models
#   Moonshot   https://platform.moonshot.ai/docs/introduction
#   Z.ai       https://docs.z.ai/guides/overview/pricing
#   MiniMax    https://platform.minimax.io/docs/guides/models-intro
#   Meta       https://www.llama.com/docs/model-cards-and-prompt-formats/
FAMILY_TABLE: dict[str, int] = {
    "claude": 200_000,
    "gpt-4o": 128_000,
    "gpt-4.1": 1_047_576,
    "gpt-5": 400_000,
    "gpt-oss": 131_072,
    "o1": 200_000,
    "o3": 200_000,
    "o4-mini": 200_000,
    "deepseek": 128_000,
    "grok-3": 131_072,
    "grok-4": 256_000,
    "gemini": 1_048_576,
    "mistral-large": 128_000,
    "mistral-medium": 128_000,
    "codestral": 256_000,
    "qwen3": 131_072,
    "qwen3-coder": 262_144,
    "kimi-k2": 262_144,
    "glm-4.5": 131_072,
    "glm-4.6": 200_000,
    "minimax-m2": 204_800,
    "llama-3.1": 131_072,
    "llama-3.3": 131_072,
}

ListModels = Callable[[RuntimeRoute], Awaitable[list[ModelInfo]]]


@dataclass(frozen=True)
class ContextWindow:
    tokens: int
    source: str


_warn_lock = threading.Lock()
_warned: set[tuple[str, str]] = set()


def reset_warnings() -> None:
    """Forget which fallbacks were warned about (tests)."""
    with _warn_lock:
        _warned.clear()


def _warn_fallback(model: str, base_url: str) -> None:
    key = (model, base_url)
    with _warn_lock:
        if key in _warned:
            return
        _warned.add(key)
    log.warning(
        "the context window of %s at %s is unknown; assuming %s tokens "
        "(set model.context_length to override)",
        model,
        base_url,
        f"{FALLBACK_CONTEXT:,}",
    )


def _sane(value: object) -> int | None:
    number = positive_int(value)
    if number is None or not (SANE_RANGE[0] <= number <= SANE_RANGE[1]):
        return None
    return number


# -- cache --------------------------------------------------------------------------------------


def cache_path(scope: Scope) -> Path:
    return scope.home / CACHE_DIR / CACHE_FILE


def _cache_key(base_url: str, model: str) -> str:
    return f"{base_url.rstrip('/')}|{model}"


def _read_entries(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.warning("ignored the context length cache %s: %s", path, exc)
        return {}
    document = as_mapping(data)
    if document.get("version") != CACHE_VERSION:
        return {}
    return dict(as_mapping(document.get("entries")))


def cached_length(scope: Scope, base_url: str, model: str) -> int | None:
    """A provider-confirmed window from the cache, checked for sanity; None otherwise."""
    entry = as_mapping(_read_entries(cache_path(scope)).get(_cache_key(base_url, model)))
    if entry.get("source") not in CONFIRMED_SOURCES:
        return None
    return _sane(entry.get("tokens"))


def remember(
    scope: Scope, base_url: str, model: str, tokens: int, *, source: str = "provider-error"
) -> bool:
    """Persist a window the provider confirmed (its ``/models`` or an overflow error).

    Returns False (and writes nothing) for an unconfirmed source or an implausible value.
    """
    if source not in CONFIRMED_SOURCES or _sane(tokens) is None or not model.strip():
        return False
    path = cache_path(scope)
    entries = _read_entries(path)
    entries[_cache_key(base_url, model)] = {
        "tokens": tokens,
        "source": source,
        "at": scope.clock.now().isoformat(),
    }
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_write_json(path, {"version": CACHE_VERSION, "entries": entries}, mode=0o600)
    except OSError as exc:
        log.warning("could not save the context length cache %s: %s", path, exc)
        return False
    return True


# -- resolution ---------------------------------------------------------------------------------


def family_length(model: str) -> int | None:
    """The family-table window for ``model`` (longest matching key), or None."""
    lowered = model.lower()
    best = ""
    for key in FAMILY_TABLE:
        if key in lowered and len(key) > len(best):
            best = key
    return FAMILY_TABLE[best] if best else None


def _config_length(route: RuntimeRoute, scope: Scope) -> int | None:
    config = config_of(scope)
    saved = model_section(config)
    saved_model = text(saved.get("default"))
    if not saved_model or saved_model == route.model:
        value = positive_int(saved.get("context_length"))
        if value is not None:
            return value
    per_model = model_entry(provider_entry(config, route.provider), route.model)
    return positive_int(per_model.get("context_length"))


def _profile_length(route: RuntimeRoute) -> int | None:
    profile = route.profile
    if profile is None:
        return None
    hooked = profile.context_length(route.model)
    if isinstance(hooked, int) and not isinstance(hooked, bool) and hooked > 0:
        return hooked
    return profile.context_length_hint


async def _endpoint_length(
    route: RuntimeRoute, scope: Scope, list_models: ListModels | None
) -> int | None:
    is_custom = route.profile is None or route.profile.is_custom
    if list_models is None or not is_custom:
        return None
    models = await list_models(route)
    wanted = route.model.lower()
    for info in models:
        if info.id == route.model or info.id.lower() == wanted:
            if info.context_length is None:
                return None
            remember(scope, route.base_url, route.model, info.context_length, source="endpoint")
            return info.context_length
    return None


async def find_window(
    route: RuntimeRoute, *, scope: Scope, list_models: ListModels | None = None
) -> ContextWindow:
    """The window by the module's order, without the minimum check."""
    configured = _config_length(route, scope)
    if configured is not None:
        return ContextWindow(configured, SOURCE_CONFIG)
    declared = _profile_length(route)
    if declared is not None:
        return ContextWindow(declared, SOURCE_PROFILE)
    if not route.model.strip():
        return ContextWindow(FALLBACK_CONTEXT, SOURCE_FALLBACK)
    cached = cached_length(scope, route.base_url, route.model)
    if cached is not None:
        return ContextWindow(cached, SOURCE_CACHE)
    live = await _endpoint_length(route, scope, list_models)
    if live is not None:
        return ContextWindow(live, SOURCE_ENDPOINT)
    family = family_length(route.model)
    if family is not None:
        return ContextWindow(family, SOURCE_FAMILY)
    _warn_fallback(route.model, route.base_url)
    return ContextWindow(FALLBACK_CONTEXT, SOURCE_FALLBACK)


async def resolve(
    route: RuntimeRoute, *, scope: Scope, list_models: ListModels | None = None
) -> ContextWindow:
    """The route's context window; raises ``ContextTooSmall`` below ``MIN_CONTEXT`` tokens."""
    window = await find_window(route, scope=scope, list_models=list_models)
    if window.tokens < MIN_CONTEXT:
        raise ContextTooSmall(
            f"{route.model or 'this model'} has a {window.tokens:,}-token context window "
            f"({window.source}); at least {MIN_CONTEXT:,} tokens are needed",
            tokens=window.tokens,
            source=window.source,
            hint="pick a model with a larger window, or raise the server's context size "
            "(for local servers) and set model.context_length",
        )
    return window


# -- overflow messages --------------------------------------------------------------------------

_NUMBER = r"(\d[\d,]*\d|\d)"
_OVERFLOW_PATTERNS = tuple(
    re.compile(p)
    for p in (
        # "... too large for model with 131072 maximum context length"
        _NUMBER + r"\s*(?:tokens?\s*)?maximum context length",
        # "maximum context length is 128000 tokens", "maximum number of tokens allowed (1048576)"
        r"maximum (?:context length|context window|number of tokens allowed|input length)"
        r"[^0-9]{0,24}?" + _NUMBER,
        # "prompt is too long: 215000 tokens > 200000 maximum"
        r">\s*" + _NUMBER + r"\s*(?:tokens?\s*)?maximum",
        # "available context size (32768 tokens)", "context length of only 4096 tokens",
        # "context window of 200000", "context_length: 8192"
        r"context[ _](?:size|length|window)\s*(?:\(|of|is|=|:)?\s*(?:only\s*)?" + _NUMBER,
        # "max_model_len=32768", "num_ctx: 4096", "n_ctx (8192)"
        r"\b(?:max_model_len|num_ctx|n_ctx)\b\s*(?:is\s*)?[=:(]?\s*" + _NUMBER,
        # "exceeds the limit of 32768", "limit: 8192 tokens"
        r"\blimit\s*(?:of|is|:|=)?\s*" + _NUMBER + r"\b",
        # "supports up to 32768 tokens"
        r"supports? (?:only )?up to\s*" + _NUMBER,
    )
)
_OUTPUT_WORDS = ("output token", "max_tokens", "max_completion_tokens", "completion tokens")
_INPUT_WORDS = ("context", "prompt", "input")


def parse_overflow_limit(message: str) -> int | None:
    """The context window an overflow error states, or None.

    Messages about the output cap only (``max_tokens`` too large) are not context windows and
    give None, so callers shrink ``max_tokens`` instead of the window.
    """
    lowered = message.lower()
    if any(w in lowered for w in _OUTPUT_WORDS) and not any(w in lowered for w in _INPUT_WORDS):
        return None
    for pattern in _OVERFLOW_PATTERNS:
        match = pattern.search(lowered)
        if match is None:
            continue
        value = _sane(match.group(1).replace(",", ""))
        if value is not None:
            return value
    return None
