"""The one config loader (D-16.32).

`load(scope)` merges the user's ``config.yaml`` over the built-in defaults, canonicalises the model
shorthand and expands ``${NAME}`` / ``${env:NAME}`` from the scope's env view. A file broken
mid-edit keeps serving the last good settings (R04.6); with no good parse ever seen, the defaults
are served as a `FailedRead` the writer refuses to save.
"""

from __future__ import annotations

import copy
import logging
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from indus.foundation import yamlio
from indus.foundation.config import backups, keys, schema

log = logging.getLogger(__name__)

CONFIG_FILE = "config.yaml"

Source = Literal["user", "default", "env-expanded"]
PathTuple = tuple[Any, ...]


class ConfigScope(Protocol):
    """What the config modules need from a `Scope` (P01-06)."""

    @property
    def home(self) -> Path: ...

    @property
    def env(self) -> Mapping[str, str]: ...

    @property
    def clock(self) -> backups.NowClock: ...


def config_path(scope: ConfigScope) -> Path:
    return scope.home / CONFIG_FILE


# -- tree helpers -------------------------------------------------------------------------------


def canonicalise(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Copy of ``raw`` with ``model: x`` as ``{default: x}`` and ``api_base`` as ``base_url``."""
    out = copy.deepcopy(dict(raw))
    model = out.get("model")
    if isinstance(model, str) and model:
        out["model"] = {"default": model}
    elif isinstance(model, dict):
        section = cast(dict[str, Any], model)
        if "api_base" in section:
            alias = section.pop("api_base")
            if not section.get("base_url"):
                section["base_url"] = alias
    return out


def deep_merge(base: Mapping[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    """``over`` on top of ``base``: mappings recurse, a null over a mapping is ignored."""
    result = dict(base)
    for key, value in over.items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = deep_merge(cast(dict[str, Any], current), cast(dict[str, Any], value))
        elif isinstance(current, dict) and value is None:
            continue
        else:
            result[key] = copy.deepcopy(value)
    return result


_REF = re.compile(r"\$\{([^}]*)\}")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_OTHER_SOURCE = re.compile(r"^[a-z][a-z0-9_-]*:")


def ref_name(inner: str) -> str | None:
    """The env var a ``${...}`` body reads, or None for another source or a malformed body."""
    body = inner.strip()
    if body.startswith("env:"):
        body = body[4:].strip()
    elif _OTHER_SOURCE.match(body):
        return None
    return body if _NAME.match(body) else None


def expand_string(text: str, env: Mapping[str, str], used: dict[str, str | None]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = ref_name(match.group(1))
        if name is None:
            if _OTHER_SOURCE.match(match.group(1).strip()):
                log.warning(
                    "config reference %s names a source other than the environment; "
                    "left as written (use ${env:NAME})",
                    match.group(0),
                )
            return match.group(0)
        value = env.get(name)
        used[name] = value
        return match.group(0) if value is None else value

    return _REF.sub(replace, text)


def expand(
    obj: Any,
    env: Mapping[str, str],
    used: dict[str, str | None],
    changed: set[PathTuple],
    path: PathTuple = (),
) -> Any:
    """Expand references in string values (keys untouched); record paths whose text changed."""
    if isinstance(obj, str):
        out = expand_string(obj, env, used)
        if out != obj:
            changed.add(path)
        return out
    if isinstance(obj, dict):
        mapping = cast(dict[Any, Any], obj)
        return {k: expand(v, env, used, changed, (*path, k)) for k, v in mapping.items()}
    if isinstance(obj, list):
        items = cast(list[Any], obj)
        return [expand(v, env, used, changed, (*path, i)) for i, v in enumerate(items)]
    return obj


def _merge_admin_overlay(merged: dict[str, Any]) -> dict[str, Any]:
    """Hook for the administrator overlay (``/etc/indus/config.yaml``, I20). Not built yet."""
    return merged


# -- views --------------------------------------------------------------------------------------


_NOT_FOUND: Any = object()


class FailedReadDict(dict[str, Any]):
    """`ConfigView.as_dict` of a failed read; the writer refuses to save it."""


class ConfigView:
    """Read-only effective config for one profile."""

    failed = False

    def __init__(
        self,
        path: Path,
        merged: dict[str, Any],
        raw: dict[str, Any],
        expanded: set[PathTuple],
        *,
        error: yamlio.YamlError | OSError | None = None,
    ) -> None:
        self.path = path
        self._merged = merged
        self._raw = raw
        self._expanded = frozenset(expanded)
        self.error = error

    def get(self, key: str, default: Any = None) -> Any:
        value = keys.get_path(self._merged, key, _NOT_FOUND)
        if value is _NOT_FOUND:
            return default
        return copy.deepcopy(value)

    def __getitem__(self, key: str) -> Any:
        return copy.deepcopy(keys.get_path(self._merged, key))

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and keys.has_path(self._merged, key)

    def is_set(self, key: str) -> bool:
        """Did the user's file contain this path (D-16.32 presence query)?"""
        return keys.has_path(self._raw, key)

    def source(self, key: str) -> Source | None:
        where = keys.resolve_path(self._raw, key)
        if where is not None:
            depth = len(where)
            if any(p[:depth] == where for p in self._expanded):
                return "env-expanded"
            return "user"
        if keys.has_path(schema.DEFAULTS, key):
            return "default"
        return None

    def as_dict(self) -> dict[str, Any]:
        """A fresh, mutable copy of the effective config (what writers start from)."""
        return copy.deepcopy(self._merged)

    def user_raw(self) -> dict[str, Any]:
        """The user's own settings, canonicalised and unexpanded."""
        return copy.deepcopy(self._raw)


class FailedRead(ConfigView):
    """Defaults served because ``config.yaml`` could not be read and nothing good was known."""

    failed = True

    def as_dict(self) -> dict[str, Any]:
        return FailedReadDict(copy.deepcopy(self._merged))


# -- cache --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Entry:
    signature: tuple[int, int] | None
    env_used: dict[str, str | None]
    view: ConfigView


_LOCK = threading.RLock()
_CACHE: dict[str, _Entry] = {}
_LAST_GOOD: dict[str, dict[str, Any]] = {}


def lock() -> threading.RLock:
    """The process-wide config lock (the writer takes it around read-modify-write)."""
    return _LOCK


def _signature(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return (st.st_mtime_ns, st.st_size)


def invalidate(path: Path | None = None) -> None:
    """Drop cached views (all, or one file's) so the next `load` re-reads."""
    with _LOCK:
        if path is None:
            _CACHE.clear()
        else:
            _CACHE.pop(str(path), None)


def reset(path: Path | None = None) -> None:
    """Forget cached views and in-memory last-known-good state (a fresh process, for tests)."""
    with _LOCK:
        if path is None:
            _CACHE.clear()
            _LAST_GOOD.clear()
        else:
            _CACHE.pop(str(path), None)
            _LAST_GOOD.pop(str(path), None)


def _hit(entry: _Entry | None, signature: tuple[int, int] | None, env: Mapping[str, str]) -> bool:
    if entry is None or entry.signature != signature:
        return False
    return all(env.get(name) == value for name, value in entry.env_used.items())


def read_user_tree(data: bytes) -> dict[str, Any]:
    """Parse raw file bytes into the user's mapping (empty document → ``{}``)."""
    loaded = yamlio.load_safe(data)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise yamlio.YamlError(f"top level must be a mapping, not {type(loaded).__name__}", line=1)
    return cast(dict[str, Any], loaded)


def _build(
    path: Path,
    raw: dict[str, Any],
    env: Mapping[str, str],
    *,
    view_type: type[ConfigView] = ConfigView,
    error: yamlio.YamlError | OSError | None = None,
) -> tuple[ConfigView, dict[str, str | None]]:
    used: dict[str, str | None] = {}
    changed: set[PathTuple] = set()
    canonical = canonicalise(raw)
    expanded_user = expand(canonical, env, used, changed)
    merged = _merge_admin_overlay(deep_merge(schema.defaults_tree(), expanded_user))
    return view_type(path, merged, canonical, changed, error=error), used


def _fallback(
    scope: ConfigScope, path: Path, error: yamlio.YamlError | OSError
) -> tuple[ConfigView, dict[str, str | None]]:
    line = getattr(error, "line", None)
    where = f" (line {line})" if line is not None else ""
    raw = _LAST_GOOD.get(str(path))
    served = "the last good settings from this run"
    if raw is None:
        data = backups.newest_good_bytes(path)
        if data is not None:
            try:
                raw = read_user_tree(data)
                served = "the newest good backup"
            except yamlio.YamlError:
                raw = None
    if raw is None:
        served = "built-in defaults (changes cannot be saved until the file is fixed)"
    log.error("cannot read %s%s: %s; using %s", path, where, error, served)
    backups.backup_config(path, "corrupt", clock=scope.clock)
    if raw is None:
        return _build(path, {}, scope.env, view_type=FailedRead, error=error)
    return _build(path, raw, scope.env, error=error)


def load(scope: ConfigScope) -> ConfigView:
    """The effective config for ``scope`` (cached until the file or a used env value changes)."""
    path = config_path(scope)
    key = str(path)
    entry = _CACHE.get(key)
    if _hit(entry, _signature(path), scope.env):
        assert entry is not None
        return entry.view
    with _LOCK:
        signature = _signature(path)
        entry = _CACHE.get(key)
        if _hit(entry, signature, scope.env):
            assert entry is not None
            return entry.view
        error: yamlio.YamlError | OSError | None = None
        raw: dict[str, Any] = {}
        if signature is not None:
            try:
                raw = read_user_tree(path.read_bytes())
            except (yamlio.YamlError, OSError) as exc:
                error = exc
        if error is not None:
            view, used = _fallback(scope, path, error)
        else:
            view, used = _build(path, raw, scope.env)
            if signature is not None:
                _LAST_GOOD[key] = copy.deepcopy(raw)
                backups.backup_config(path, "good", clock=scope.clock)
            for warning in schema.validate(view.as_dict()):
                log.warning("%s: %s", path, warning)
        _CACHE[key] = _Entry(signature, used, view)
        return view
