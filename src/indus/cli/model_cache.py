"""The model picker's cache of provider model lists: ``<home>/cache/provider_models.json``.

One row per provider and base URL: ``{fp, at, models, fallback}``. ``fp`` is a short digest of
the key the list was fetched with, so a new key never sees another key's list.

- A live row is fresh for ``LIVE_TTL_S`` (one hour). After that it is still served at once while
  a refresh runs in the background (stale-while-revalidate), up to ``STALE_MAX_S``; older than
  that, the caller waits for a live fetch.
- A fetch that fails stores a fallback row (the curated list, possibly empty) that lives
  ``FALLBACK_TTL_S`` and is never served stale. An empty fallback row is the negative probe: a
  dead endpoint is not asked again for a minute.
- A failed fetch never replaces a live row for the same key; the old list is served instead.

Reading or writing the file never raises: a broken file reads as empty, a failed write is
logged.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from indus.foundation.fsio import atomic_write_json
from indus.foundation.scope import Scope, thread_with_scope

__all__ = [
    "CACHE_FILE",
    "FALLBACK_TTL_S",
    "LIVE_TTL_S",
    "NEGATIVE_TTL_S",
    "STALE_MAX_S",
    "Lookup",
    "cache_path",
    "clear",
    "fingerprint",
    "get_models",
    "lookup",
    "row_key",
]

log = logging.getLogger(__name__)

CACHE_DIR = "cache"
CACHE_FILE = "provider_models.json"
VERSION = 1
LIVE_TTL_S = 3600.0
STALE_MAX_S = 7 * 24 * 3600.0
FALLBACK_TTL_S = 60.0
NEGATIVE_TTL_S = 60.0

State = Literal["fresh", "stale", "fallback", "miss"]
Revalidate = Callable[[Callable[[], None]], None]


@dataclass(frozen=True)
class Lookup:
    state: State
    models: tuple[str, ...] = ()


def cache_path(scope: Scope) -> Path:
    return scope.home / CACHE_DIR / CACHE_FILE


def row_key(provider: str, base_url: str) -> str:
    return f"{provider}|{base_url.rstrip('/')}"


def fingerprint(key: str | None) -> str:
    """A short digest of ``key`` ("" when there is none); never the key itself."""
    if not key:
        return ""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _now(scope: Scope) -> float:
    return scope.clock.now().timestamp()


def _read(path: Path) -> dict[str, dict[str, Any]]:
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, ValueError) as exc:
        log.warning("ignored the model list cache %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    doc = cast(dict[str, Any], data)
    rows = doc.get("rows")
    if doc.get("version") != VERSION or not isinstance(rows, dict):
        return {}
    return {
        str(k): cast(dict[str, Any], v)
        for k, v in cast(dict[str, Any], rows).items()
        if isinstance(v, dict)
    }


def _write(path: Path, rows: dict[str, dict[str, Any]]) -> None:
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_write_json(path, {"version": VERSION, "rows": rows}, mode=0o600)
    except OSError as exc:
        log.warning("could not save the model list cache %s: %s", path, exc)


def _models(row: dict[str, Any]) -> tuple[str, ...]:
    raw = row.get("models")
    if not isinstance(raw, list):
        return ()
    return tuple(str(m) for m in cast(list[Any], raw) if isinstance(m, str) and m)


def _age(row: dict[str, Any], now: float) -> float | None:
    at = row.get("at")
    if isinstance(at, bool) or not isinstance(at, int | float):
        return None
    age = now - float(at)
    return age if age >= 0 else None


def lookup(scope: Scope, key: str, fp: str) -> Lookup:
    """What the cache holds for ``key`` under credential ``fp`` right now."""
    row = _read(cache_path(scope)).get(key)
    if row is None or row.get("fp") != fp:
        return Lookup("miss")
    age = _age(row, _now(scope))
    if age is None:
        return Lookup("miss")
    if row.get("fallback"):
        return Lookup("fallback", _models(row)) if age < FALLBACK_TTL_S else Lookup("miss")
    if age < LIVE_TTL_S:
        return Lookup("fresh", _models(row))
    if age < STALE_MAX_S:
        return Lookup("stale", _models(row))
    return Lookup("miss")


def _store(scope: Scope, key: str, row: dict[str, Any]) -> None:
    path = cache_path(scope)
    rows = _read(path)
    rows[key] = row
    _write(path, rows)


def clear(scope: Scope) -> bool:
    """Remove the cache file (``indus model --refresh``); True when there was one."""
    try:
        cache_path(scope).unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("could not remove the model list cache: %s", exc)
        return False
    return True


def _background(job: Callable[[], None]) -> None:
    thread_with_scope(job, name="indus-model-list-refresh", daemon=True).start()


def _live_row(scope: Scope, key: str, fp: str) -> dict[str, Any] | None:
    row = _read(cache_path(scope)).get(key)
    if row is None or row.get("fp") != fp or row.get("fallback"):
        return None
    return row


def get_models(
    scope: Scope,
    key: str,
    fp: str,
    fetch: Callable[[], Sequence[str]],
    *,
    curated: Sequence[str] = (),
    refresh: bool = False,
    revalidate: Revalidate | None = None,
) -> list[str]:
    """Model ids for ``key``: cached when allowed (see the module notes), else fetched."""
    if not refresh:
        hit = lookup(scope, key, fp)
        if hit.state in ("fresh", "fallback"):
            return list(hit.models)
        if hit.state == "stale":

            def again() -> None:
                fresh = [m for m in fetch() if m]
                if fresh:
                    _store(scope, key, _row(fp, scope, fresh, fallback=False))

            (revalidate or _background)(again)
            return list(hit.models)
    live = [m for m in fetch() if m]
    if live:
        _store(scope, key, _row(fp, scope, live, fallback=False))
        return live
    previous = _live_row(scope, key, fp)
    if previous is not None:
        return list(_models(previous))
    _store(scope, key, _row(fp, scope, list(curated), fallback=True))
    return list(curated)


def _row(fp: str, scope: Scope, models: Sequence[str], *, fallback: bool) -> dict[str, Any]:
    return {"fp": fp, "at": _now(scope), "models": list(models), "fallback": fallback}
