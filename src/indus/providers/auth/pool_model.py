"""Credential pools: several keys or sign-ins for one provider, in a fixed order with a status.

A pool is the list ``auth.json`` keeps under ``pool.<provider>``. Each row is a ``PoolEntry``
(metadata only); its secret lives in the SecretStore under ``secret_name(provider, id)``. Keys the
person keeps under the provider profile's own env var names (keychain or environment) appear as
``source: env`` rows; they are written to the store on the next pool write so their status and
place persist, and they drop out when the variable no longer resolves.

Operations shared by ``indus auth`` and the runtime (P08): ``add`` (at a priority, else last),
``remove``, ``reset`` (clear exhaustion), ``set_priority`` (others renumbered) and ``peek`` (what
would be used next; never changes anything). Targets are a 1-based index, an entry id or an exact
label. Request counts are kept in memory (``note_request``) and folded into the rows on the next
pool write, so counting never costs a disk write of its own.
"""

from __future__ import annotations

import logging
import re
import secrets
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from typing import Any, Literal, Protocol, cast

from indus.foundation.errors import AuthError
from indus.providers.auth.store import AuthStore
from indus.providers.profile import ProviderProfile
from indus.providers.route import is_placeholder

log = logging.getLogger(__name__)

AuthType = Literal["api_key", "oauth"]
Source = Literal["manual", "env", "device_code", "pkce"]
Status = Literal["ok", "exhausted"]

AUTH_TYPES: tuple[str, ...] = ("api_key", "oauth")
SOURCES: tuple[str, ...] = ("manual", "env", "device_code", "pkce")
STATUSES: tuple[str, ...] = ("ok", "exhausted")
STRATEGIES: tuple[str, ...] = ("fill_first", "round_robin", "least_used", "random")
DEFAULT_STRATEGY = "fill_first"
STRATEGY_KEY = "credential_pool_strategies"
ENV_ID_PREFIX = "env-"

_SLUG = re.compile(r"[^A-Za-z0-9]+")


class PoolTargetError(AuthError):
    """No entry (or more than one) matches an index, id or label."""

    default_code = "auth.unknown_target"


class SecretReader(Protocol):
    def get(self, name: str, /) -> str | None: ...


class SecretWriter(SecretReader, Protocol):
    def set(self, name: str, value: str, /) -> None: ...

    def delete(self, name: str, /) -> bool: ...


class NowClock(Protocol):
    def now(self) -> datetime: ...


def secret_name(provider: str, entry_id: str) -> str:
    """The SecretStore name of an entry's secret: ``AUTH_<PROVIDER>_<ID>`` (env-name shaped)."""
    slug = _SLUG.sub("_", provider).strip("_").upper() or "PROVIDER"
    return f"AUTH_{slug}_{_SLUG.sub('_', entry_id).upper()}"


_GENERATED_SECRET = re.compile(r"AUTH_[A-Z0-9_]+_[0-9A-F]{8}")


def pool_secret_names(store: AuthStore) -> set[str]:
    """The SecretStore names this profile's pools use: every stored row's ``secret_name`` (a store
    that cannot be read gives an empty set)."""
    try:
        doc = store.read()
    except AuthError:
        return set()
    names: set[str] = set()
    pools = doc.get("pool")
    if isinstance(pools, dict):
        for provider, rows in cast(dict[str, Any], pools).items():
            if isinstance(rows, list):
                for row in cast(list[object], rows):
                    if isinstance(row, dict) and cast(dict[str, Any], row).get("id"):
                        names.add(secret_name(str(provider), str(cast(dict[str, Any], row)["id"])))
    return names


def is_pool_secret(name: str, known: set[str] | frozenset[str] = frozenset()) -> bool:
    """``name`` holds a pool credential: listed in ``known`` or shaped like a generated one."""
    return name in known or _GENERATED_SECRET.fullmatch(name) is not None


def new_entry_id(taken: Iterable[str] = ()) -> str:
    used = set(taken)
    while True:
        candidate = secrets.token_hex(4)
        if candidate not in used:
            return candidate


@dataclass
class PoolEntry:
    id: str
    label: str
    auth_type: str = "api_key"
    source: str = "manual"
    priority: int = 0
    status: str = "ok"
    cooldown_until: str | None = None
    last_error_class: str | None = None
    request_count: int = 0
    env_var: str | None = None
    expires_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict[str, Any])

    def __post_init__(self) -> None:
        if self.auth_type not in AUTH_TYPES:
            raise ValueError(f"auth_type must be one of {AUTH_TYPES}, not {self.auth_type!r}")
        if self.source not in SOURCES:
            raise ValueError(f"source must be one of {SOURCES}, not {self.source!r}")
        if self.status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}, not {self.status!r}")

    @property
    def stores_secret(self) -> bool:
        """Env entries read their value from the variable; every other entry owns a secret."""
        return self.source != "env"

    def cooldown_end(self) -> datetime | None:
        if not self.cooldown_until:
            return None
        try:
            return datetime.fromisoformat(self.cooldown_until)
        except ValueError:
            return None

    def available(self, now: datetime) -> bool:
        """Usable now: status ok, or exhausted with a cooldown that has passed."""
        if self.status == "ok":
            return True
        end = self.cooldown_end()
        return end is not None and end <= now

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        extra = cast(dict[str, Any], row.pop("extra"))
        out = {k: v for k, v in row.items() if v is not None}
        for key, value in extra.items():
            out.setdefault(key, value)
        return out

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> PoolEntry:
        names = {f for f in cls.__dataclass_fields__ if f != "extra"}
        known = {k: row[k] for k in names if k in row}
        extra = {k: v for k, v in row.items() if k not in names}
        known.setdefault("id", new_entry_id())
        known.setdefault("label", str(known["id"]))
        try:
            known["priority"] = int(known.get("priority", 0))
            known["request_count"] = int(known.get("request_count", 0))
        except (TypeError, ValueError):
            known["priority"], known["request_count"] = 0, 0
        return cls(**known, extra=extra)


# -- in-memory request counts ------------------------------------------------------------------

_pending_lock = threading.Lock()
_pending: dict[tuple[str, str, str], int] = {}


def note_request(store: AuthStore, provider: str, entry_id: str) -> None:
    """Count one selection of ``entry_id``; written on the next pool write for ``provider``."""
    key = (str(store.path), provider, entry_id)
    with _pending_lock:
        _pending[key] = _pending.get(key, 0) + 1


def pending_requests(store: AuthStore, provider: str) -> dict[str, int]:
    with _pending_lock:
        return {
            k[2]: v for k, v in _pending.items() if k[0] == str(store.path) and k[1] == provider
        }


def _take_pending(store: AuthStore, provider: str) -> dict[str, int]:
    with _pending_lock:
        keys = [k for k in _pending if k[0] == str(store.path) and k[1] == provider]
        return {k[2]: _pending.pop(k) for k in keys}


def _restore_pending(store: AuthStore, provider: str, counts: Mapping[str, int]) -> None:
    with _pending_lock:
        for entry_id, count in counts.items():
            key = (str(store.path), provider, entry_id)
            _pending[key] = _pending.get(key, 0) + count


def reset_pending_requests() -> None:
    """Tests only."""
    with _pending_lock:
        _pending.clear()


# -- the pool ----------------------------------------------------------------------------------


class Pool:
    """One provider's entries, ordered by priority (0 = tried first)."""

    def __init__(self, provider: str, entries: Iterable[PoolEntry] = ()) -> None:
        self.provider = provider
        self.entries: list[PoolEntry] = sorted(entries, key=lambda e: e.priority)
        self._renumber()

    def __len__(self) -> int:
        return len(self.entries)

    def _renumber(self) -> None:
        for i, entry in enumerate(self.entries):
            entry.priority = i

    def ids(self) -> list[str]:
        return [e.id for e in self.entries]

    def resolve(self, target: str | int) -> tuple[int, PoolEntry]:
        """``(1-based index, entry)`` for an index, an id or an exact label."""
        text = str(target).strip()
        if not text:
            raise PoolTargetError(f"say which {self.provider} credential (index, id or label)")
        if text.isdigit():
            index = int(text)
            if 1 <= index <= len(self.entries):
                return index, self.entries[index - 1]
        for i, entry in enumerate(self.entries, start=1):
            if entry.id == text:
                return i, entry
        labelled = [(i, e) for i, e in enumerate(self.entries, start=1) if e.label == text]
        if len(labelled) == 1:
            return labelled[0]
        if len(labelled) > 1:
            numbers = ", ".join(f"#{i}" for i, _ in labelled)
            raise PoolTargetError(
                f"more than one {self.provider} credential is labelled {text!r} ({numbers}); "
                "use the index or the id instead"
            )
        if text.isdigit():
            raise PoolTargetError(
                f"{self.provider} has {len(self.entries)} credential(s); there is no #{text}"
            )
        raise PoolTargetError(f"no {self.provider} credential has the index, id or label {text!r}")

    def add(self, entry: PoolEntry, priority: int | None = None) -> PoolEntry:
        if entry.id in self.ids():
            raise ValueError(f"entry id {entry.id!r} is already in the {self.provider} pool")
        at = len(self.entries) if priority is None else max(0, min(priority, len(self.entries)))
        self.entries.insert(at, entry)
        self._renumber()
        return entry

    def remove(self, target: str | int) -> tuple[int, PoolEntry]:
        index, entry = self.resolve(target)
        self.entries.pop(index - 1)
        self._renumber()
        return index, entry

    def reset(self, target: str | int | None = None) -> list[PoolEntry]:
        chosen = self.entries if target is None else [self.resolve(target)[1]]
        for entry in chosen:
            entry.status = "ok"
            entry.cooldown_until = None
            entry.last_error_class = None
        return list(chosen)

    def set_priority(self, target: str | int, priority: int) -> PoolEntry:
        index, entry = self.resolve(target)
        self.entries.pop(index - 1)
        at = max(0, min(priority, len(self.entries)))
        self.entries.insert(at, entry)
        self._renumber()
        return entry

    def mark_exhausted(self, target: str | int, error_class: str, until: datetime | None) -> None:
        """Bench an entry (used by the runtime, P08)."""
        _, entry = self.resolve(target)
        entry.status = "exhausted"
        entry.last_error_class = error_class
        entry.cooldown_until = until.isoformat() if until is not None else None

    def peek(self, strategy: str, now: datetime) -> PoolEntry | None:
        """The entry the strategy would use next. Changes nothing (no reset, no count, no turn)."""
        available = [e for e in self.entries if e.available(now)]
        if not available:
            return None
        if strategy == "least_used":
            return min(available, key=lambda e: (e.request_count, e.priority))
        return available[0]

    def rows(self) -> list[dict[str, Any]]:
        return [e.to_row() for e in self.entries]


# -- strategies --------------------------------------------------------------------------------

_warned_strategies: set[tuple[str, str]] = set()


def check_strategy(name: str) -> str:
    """``name`` normalised, or ValueError naming the allowed ones."""
    value = name.strip().lower().replace("-", "_")
    if value not in STRATEGIES:
        raise ValueError(f"unknown pool strategy {name!r}; use one of {', '.join(STRATEGIES)}")
    return value


def strategy_for(config: object, provider: str) -> str:
    """``credential_pool_strategies.<provider>`` from a config view or mapping (else fill_first)."""
    getter = getattr(config, "get", None)
    section: object = getter(STRATEGY_KEY) if callable(getter) else None
    if not isinstance(section, Mapping):
        return DEFAULT_STRATEGY
    raw = cast(Mapping[str, object], section).get(provider)
    if raw is None or raw == "":
        return DEFAULT_STRATEGY
    try:
        return check_strategy(str(raw))
    except ValueError:
        key = (provider, str(raw))
        if key not in _warned_strategies:
            _warned_strategies.add(key)
            log.warning(
                "%s.%s is %r, which is not a pool strategy; using %s",
                STRATEGY_KEY,
                provider,
                raw,
                DEFAULT_STRATEGY,
            )
        return DEFAULT_STRATEGY


# -- env-seeded entries ------------------------------------------------------------------------


def env_entry_id(var: str) -> str:
    return ENV_ID_PREFIX + var.lower()


def env_entries(
    profile: ProviderProfile, secrets_view: SecretReader | None, env: Mapping[str, str]
) -> list[PoolEntry]:
    """An entry for each of the profile's env var names that holds a usable value."""
    found: list[PoolEntry] = []
    for var in profile.env_vars:
        value = secrets_view.get(var) if secrets_view is not None else None
        if is_placeholder(value):
            value = env.get(var)
        if is_placeholder(value):
            continue
        found.append(
            PoolEntry(
                id=env_entry_id(var),
                label=var,
                auth_type="api_key",
                source="env",
                env_var=var,
            )
        )
    return found


def suppressed(doc: Mapping[str, Any], provider: str) -> set[str]:
    raw = doc.get("suppressed_sources", {})
    if not isinstance(raw, Mapping):
        return set()
    names = cast(Mapping[str, object], raw).get(provider, [])
    return {str(n) for n in cast(list[object], names)} if isinstance(names, list) else set()


def _merge(
    rows: list[dict[str, Any]], env_rows: Iterable[PoolEntry], hidden: set[str]
) -> list[PoolEntry]:
    stored = [PoolEntry.from_row(r) for r in rows]
    live_env = {e.id: e for e in env_rows if e.env_var not in hidden}
    kept = [e for e in stored if e.source != "env" or e.id in live_env]
    have = {e.id for e in kept}
    nxt = max((e.priority for e in kept), default=-1) + 1
    for entry in live_env.values():
        if entry.id not in have:
            kept.append(replace(entry, priority=nxt))
            nxt += 1
    return kept


def load_pool(
    store: AuthStore, provider: str, env_rows: Iterable[PoolEntry] = ()
) -> tuple[Pool, bool]:
    """``(pool, inherited)``: stored rows plus live env rows (read only; nothing is written).

    ``inherited`` is True when the rows came from the root profile's store (read-only)."""
    rows, inherited = store.pool_rows(provider)
    doc = store.read()
    pool = Pool(provider, _merge(rows, env_rows, suppressed(doc, provider)))
    for entry_id, count in pending_requests(store, provider).items():
        for entry in pool.entries:
            if entry.id == entry_id:
                entry.request_count += count
    return pool, inherited


def edit_pool[T](
    store: AuthStore,
    provider: str,
    change: Callable[[Pool, dict[str, Any]], T],
    env_rows: Iterable[PoolEntry] = (),
) -> T:
    """Run ``change(pool, document)`` inside one store transaction and write the pool back.

    The document is the whole store, for changes beyond the pool (suppressed sources, provider
    state). Pending request counts are folded in on this write."""
    with store.transaction() as doc:
        pool_section = cast(dict[str, Any], doc.setdefault("pool", {}))
        raw = pool_section.get(provider)
        rows = (
            [r for r in cast(list[object], raw) if isinstance(r, dict)]
            if isinstance(raw, list)
            else []
        )
        pool = Pool(
            provider, _merge(cast(list[dict[str, Any]], rows), env_rows, suppressed(doc, provider))
        )
        taken = _take_pending(store, provider)
        for entry_id, count in taken.items():
            for entry in pool.entries:
                if entry.id == entry_id:
                    entry.request_count += count
        try:
            result = change(pool, doc)
        except BaseException:
            _restore_pending(store, provider, taken)
            raise
        if pool.entries:
            pool_section[provider] = pool.rows()
        else:
            pool_section.pop(provider, None)
        return result


def prune_env_rows(store: AuthStore, var: str) -> int:
    """Drop the stored env-seeded rows of ``var`` from every pool (after the key was removed);
    returns how many went. A row comes back on the next read while ``var`` is still set."""
    dropped = 0
    with store.transaction() as doc:
        section = doc.get("pool")
        if not isinstance(section, dict):
            return 0
        pools = cast(dict[str, Any], section)
        for provider in list(pools):
            rows = pools[provider]
            if not isinstance(rows, list):
                continue
            kept = [
                r
                for r in cast(list[object], rows)
                if not (
                    isinstance(r, dict)
                    and cast(dict[str, Any], r).get("source") == "env"
                    and cast(dict[str, Any], r).get("env_var") == var
                )
            ]
            dropped += len(cast(list[object], rows)) - len(kept)
            if kept:
                pools[provider] = kept
            else:
                pools.pop(provider)
    return dropped


def suppress_env(doc: dict[str, Any], provider: str, var: str) -> None:
    section = doc.setdefault("suppressed_sources", {})
    if not isinstance(section, dict):
        section = doc["suppressed_sources"] = {}
    names = set(suppressed(doc, provider)) | {var}
    cast(dict[str, Any], section)[provider] = sorted(names)


def unsuppress(doc: dict[str, Any], provider: str) -> None:
    section = doc.get("suppressed_sources")
    if isinstance(section, dict):
        cast(dict[str, Any], section).pop(provider, None)
        if not section:
            doc.pop("suppressed_sources", None)
