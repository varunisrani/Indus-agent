"""Live model lists from a provider's ``/models`` endpoint, cached per base URL.

The list feeds the model picker (P07) and context-window discovery for custom and local endpoints
(P03-17). Failures never raise: they log a reason and return an empty list, so a picker can fall
back to typing a model id.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import httpx

from indus.foundation.fsio import atomic_write_json
from indus.foundation.scope import Scope
from indus.providers.route import RuntimeRoute
from indus.providers.settings import as_mapping, config_of, positive_int, positive_number

log = logging.getLogger(__name__)

CACHE_DIR = "cache"
CACHE_FILE = "model_lists.json"
CACHE_VERSION = 1
DEFAULT_TTL_SECONDS = 20 * 60.0
LIST_TIMEOUT_SECONDS = 10.0
ANTHROPIC_VERSION = "2023-06-01"
# Fields OpenAI-compatible servers use for a model's window (vLLM: max_model_len).
CONTEXT_FIELDS = ("context_length", "max_model_len", "context_window")
# ``visibility`` values of catalogue entries a picker must not offer (the ChatGPT Codex backend).
HIDDEN = frozenset({"hide", "hidden"})


@dataclass(frozen=True)
class ModelInfo:
    id: str
    context_length: int | None = None
    owned_by: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "context_length": self.context_length, "owned_by": self.owned_by}


def models_url(route: RuntimeRoute) -> str:
    """The profile's ``models_url`` when the route uses the profile's own endpoint, else
    ``{base_url}/models`` (``/v1/models`` for Anthropic-wire hosts without a ``/v1`` suffix)."""
    base = route.base_url.rstrip("/")
    profile = route.profile
    if profile is not None and profile.models_url and base == profile.base_url.rstrip("/"):
        return profile.models_url
    if route.api_mode == "anthropic_messages" and not base.endswith("/v1"):
        return base + "/v1/models"
    return base + "/models"


def request_headers(route: RuntimeRoute) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if route.api_key is None:
        return headers
    bearer = route.profile is not None and route.profile.bearer_auth
    if route.api_mode == "anthropic_messages" and not bearer:
        headers["x-api-key"] = route.api_key.reveal()
    else:
        headers["Authorization"] = f"Bearer {route.api_key.reveal()}"
    if route.profile is not None:
        headers.update(route.profile.key_headers(route.api_key.reveal()))
    if route.api_mode == "anthropic_messages":
        headers["anthropic-version"] = ANTHROPIC_VERSION
    return headers


def _context_of(item: dict[str, Any]) -> int | None:
    for name in CONTEXT_FIELDS:
        value = positive_int(item.get(name))
        if value is not None:
            return value
    return None


def parse_models(payload: object) -> list[ModelInfo]:
    """Models from ``{"data": [...]}``, ``{"models": [...]}`` or a bare list; an entry's id is
    ``id`` (or ``slug``); entries without one, and hidden ones, are skipped. A list whose entries
    carry ``priority`` is ordered by it."""
    raw: object = payload
    if isinstance(payload, dict):
        body = cast(dict[str, Any], payload)
        raw = body.get("data", body.get("models"))
    if not isinstance(raw, list):
        return []
    models: list[ModelInfo] = []
    seen: set[str] = set()
    ranks: dict[str, float] = {}
    for entry in cast(list[object], raw):
        if not isinstance(entry, dict):
            continue
        item = cast(dict[str, Any], entry)
        model_id = item.get("id", item.get("slug"))
        if not isinstance(model_id, str) or not model_id.strip() or model_id in seen:
            continue
        if str(item.get("visibility") or "").strip().lower() in HIDDEN:
            continue
        seen.add(model_id)
        priority = item.get("priority")
        if isinstance(priority, int | float) and not isinstance(priority, bool):
            ranks[model_id] = float(priority)
        owner = item.get("owned_by")
        models.append(
            ModelInfo(
                id=model_id,
                context_length=_context_of(item),
                owned_by=owner if isinstance(owner, str) else None,
            )
        )
    if ranks:
        models.sort(key=lambda m: (ranks.get(m.id, 1e9), m.id))
    return models


# -- cache --------------------------------------------------------------------------------------


def cache_path(scope: Scope) -> Path:
    return scope.home / CACHE_DIR / CACHE_FILE


def ttl_seconds(scope: Scope) -> float:
    minutes = positive_number(as_mapping(config_of(scope).get("model_catalog")).get("ttl_minutes"))
    return minutes * 60.0 if minutes is not None else DEFAULT_TTL_SECONDS


def _read_cache(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.warning("ignored the model list cache %s: %s", path, exc)
        return {}
    document = as_mapping(data)
    if document.get("version") != CACHE_VERSION:
        return {}
    return dict(as_mapping(document.get("lists")))


def _cached(scope: Scope, base_url: str, ttl: float) -> list[ModelInfo] | None:
    entry = as_mapping(_read_cache(cache_path(scope)).get(base_url))
    fetched = positive_number(entry.get("fetched_at"))
    if fetched is None:
        return None
    now = scope.clock.now().timestamp()
    if not (fetched <= now < fetched + ttl):
        return None
    return parse_models({"data": entry.get("models")})


def _store(scope: Scope, base_url: str, models: list[ModelInfo]) -> None:
    path = cache_path(scope)
    lists = _read_cache(path)
    lists[base_url] = {
        "fetched_at": scope.clock.now().timestamp(),
        "models": [m.to_json() for m in models],
    }
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_write_json(path, {"version": CACHE_VERSION, "lists": lists}, mode=0o600)
    except OSError as exc:
        log.warning("could not save the model list cache %s: %s", path, exc)


# -- fetch --------------------------------------------------------------------------------------


async def list_models(
    route: RuntimeRoute,
    *,
    client: httpx.AsyncClient,
    scope: Scope | None = None,
    cache_ttl: float | None = None,
    refresh: bool = False,
) -> list[ModelInfo]:
    """The models ``route``'s endpoint offers; on any failure (reason logged) the profile's
    documented ``default_models`` (not cached), else ``[]``.

    With a ``scope`` the list is cached in ``<home>/cache/model_lists.json`` per base URL for
    ``cache_ttl`` seconds (default ``model_catalog.ttl_minutes``) of the scope's clock time;
    ``refresh`` skips the cached copy.
    """
    if route.profile is not None and not route.profile.supports_model_listing:
        return []
    base = route.base_url.rstrip("/")
    ttl = cache_ttl if cache_ttl is not None else (ttl_seconds(scope) if scope else 0.0)
    if scope is not None and not refresh and ttl > 0:
        cached = _cached(scope, base, ttl)
        if cached is not None:
            return cached
    url = models_url(route)
    fallback = [ModelInfo(id=m) for m in (route.profile.default_models if route.profile else ())]
    try:
        response = await client.get(
            url, headers=request_headers(route), timeout=LIST_TIMEOUT_SECONDS
        )
    except httpx.HTTPError as exc:
        log.warning("could not list models at %s: %s", url, type(exc).__name__)
        return fallback
    if response.status_code >= 400:
        log.warning("could not list models at %s: HTTP %s", url, response.status_code)
        return fallback
    try:
        payload = response.json()
    except (ValueError, UnicodeDecodeError):
        log.warning("could not list models at %s: the answer is not JSON", url)
        return fallback
    models = parse_models(payload)
    if not models and fallback:
        return fallback
    if scope is not None and ttl > 0:
        _store(scope, base, models)
    return models
