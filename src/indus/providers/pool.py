"""Credential pool runtime (P08-12): which credential a request uses, and what a failure does.

P07 keeps the pool rows (``auth.json``, secrets in the SecretStore, env-seeded rows for the
profile's own key variables). Here a ``PoolRuntime`` is one provider's live pool for a session:

- ``lease(route, model)`` gives the credential the next request goes out with. The session keeps
  one credential until it fails; a new pick follows the provider's strategy (``fill_first``,
  ``round_robin``, ``least_used``, ``random``) and is counted with ``note_request``. After a quota
  rotation the session returns to the credential it was moved off as soon as that one's cooldown
  has passed. When nothing is usable the bound key (or the credential that returns first) is still
  used: the pool never stops a request, it only decides what a failure does.
- ``after_failure(lease, verdict, model, route)`` applies the policy table (``policy_for``) to the
  final verdict of P03's retries and says whether to try again on the same or another credential.
  ``adjuster`` makes the per-attempt decision inside those retries: a generic 429 is retried once
  on the same key, the second one in a row, a plan/usage limit or a billing answer stops the
  retries at once when another credential is available.
- ``call(...)`` is that loop around one model call; ``RetryFailed`` leaves it when the pool has
  nothing left, for the fallback chain (P08-13).
- ``peek`` and ``would_fail`` are status reads: they never count, rotate, refresh or bench.

Benches are written to the store (``edit_pool``) so ``indus auth list`` shows them and other
sessions skip the key; a pool inherited from the root profile is benched in memory only. A
``PoolSet`` holds one runtime per provider for a session; subagents get the parent's set.
``pool_key`` lets route resolution use a pool-only key (``indus auth add``) when no key variable
resolves.
"""

from __future__ import annotations

import logging
import random
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from indus.foundation.blocking import run_blocking
from indus.foundation.clock import Clock
from indus.providers.auth.pool_model import (
    STRATEGIES,
    Pool,
    PoolEntry,
    SecretReader,
    edit_pool,
    env_entries,
    load_pool,
    note_request,
    secret_name,
    strategy_for,
)
from indus.providers.auth.store import AuthStore
from indus.providers.classify import BILLING, RATE_LIMIT, USAGE_LIMIT, ClassifiedError
from indus.providers.profile import ProviderProfile
from indus.providers.retry import RETRY_AFTER_CAP, RetryFailed
from indus.providers.route import RuntimeRoute, Secret, is_placeholder, key_allowed

__all__ = [
    "BENCHES_FIELD",
    "ENTITLEMENT_PHRASES",
    "PER_MODEL_RATE_LIMITS",
    "Lease",
    "Policy",
    "PoolRuntime",
    "PoolSet",
    "is_entitlement",
    "is_plan_limit",
    "policy_for",
    "pool_key",
]

log = logging.getLogger(__name__)

COOLDOWN_RATE_LIMIT_S = 3600.0
COOLDOWN_BILLING_S = 3600.0
COOLDOWN_AUTH_S = 300.0
MAX_REFRESHES = 2
"""Refreshes of one entry per turn while its 401s persist; then it is rotated away."""
GATE_WAIT_S = RETRY_AFTER_CAP
"""A pool "would fail" only when every credential is out for longer than the longest retry wait."""

PER_MODEL_RATE_LIMITS = frozenset({"anthropic"})
"""Vendors whose rate limits are per model: a generic 429 benches only that model."""

ENTITLEMENT_PHRASES = (
    "not supported when using",
    "not entitled",
    "is not available on your plan",
    "is not available for your plan",
    "not included in your plan",
    "does not have access to model",
    "does not have access to the model",
    "do not have access to model",
    "do not have access to the model",
    "upgrade your plan to use",
    "requires a paid plan",
)
"""Wording of a 400 that rejects a model for this account (a plan property, not a window)."""

BENCHES_FIELD = "model_benches"
"""Row field ``{model: until (ISO time), or "" = until reset}``; ``indus auth reset`` clears it."""

QUOTA_CLASSES = frozenset({"rate_limit", "usage_limit", "billing"})

type PolicyKind = Literal["retry", "rotate", "refresh", "bench_model", "pass"]
type Adjust = Callable[[ClassifiedError], ClassifiedError]
type Refresher = Callable[[str, PoolEntry, str], Awaitable[str | None]]
"""``(provider, entry, stored secret) -> new access token`` (None: the refresh failed)."""


class SecretSink(SecretReader, Protocol):
    def set(self, name: str, value: str, /) -> None: ...


# -- the policy table -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Policy:
    """What a verdict means for the credential that produced it."""

    kind: PolicyKind
    cooldown_s: float | None = None  # None with ``bench_model``: until a manual reset
    per_model: bool = False
    error_class: str = ""


def is_entitlement(verdict: ClassifiedError) -> bool:
    """A 400 telling us the account's plan does not include this model."""
    if verdict.status != 400:
        return False
    text = verdict.message.lower()
    return any(phrase in text for phrase in ENTITLEMENT_PHRASES)


def is_plan_limit(verdict: ClassifiedError) -> bool:
    """A rate limit that is really a plan or usage cap (retrying the same key cannot help)."""
    if verdict.reason != "rate_limit":
        return False
    if verdict.status == 402:
        return True
    text = verdict.message.lower()
    quota = any(p in text for p in USAGE_LIMIT) or any(p in text for p in BILLING)
    return quota and not any(p in text for p in RATE_LIMIT)  # "rate limit exceeded" is generic


def _reset_seconds(verdict: ClassifiedError, now: datetime, default: float) -> float:
    if verdict.reset_at is not None:
        left = (verdict.reset_at - now).total_seconds()
        if left > 0:
            return left
    return default


def policy_for(
    verdict: ClassifiedError, *, provider: str, retried_429: bool, now: datetime
) -> Policy:
    """The pool's answer to a final verdict (see the module notes and the P08-12 table)."""
    if is_entitlement(verdict):
        return Policy("bench_model", None, per_model=True, error_class="entitlement")
    reason = verdict.reason
    if reason == "auth":
        return Policy("refresh", COOLDOWN_AUTH_S, error_class="auth")
    if reason == "auth_permanent":
        return Policy("rotate", COOLDOWN_AUTH_S, error_class="auth")
    if reason == "billing":
        return Policy(
            "rotate", _reset_seconds(verdict, now, COOLDOWN_BILLING_S), error_class="billing"
        )
    if reason == "rate_limit":
        cooldown = _reset_seconds(verdict, now, COOLDOWN_RATE_LIMIT_S)
        if is_plan_limit(verdict):
            return Policy("rotate", cooldown, error_class="usage_limit")
        if not retried_429:
            return Policy("retry", error_class="rate_limit")
        per_model = provider in PER_MODEL_RATE_LIMITS
        return Policy("rotate", cooldown, per_model=per_model, error_class="rate_limit")
    return Policy("pass")


# -- leases ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Lease:
    """The credential one request goes out with."""

    entry_id: str
    label: str
    key: Secret
    env_var: str = ""

    def apply(self, route: RuntimeRoute) -> RuntimeRoute:
        """``route`` carrying this credential (the client is unchanged: keys travel per request)."""
        return replace(route, api_key=self.key, key_env=self.env_var)


def _iso(when: datetime | None) -> str | None:
    return when.isoformat() if when is not None else None


def _benches(entry: PoolEntry) -> dict[str, str]:
    raw = entry.extra.get(BENCHES_FIELD)
    if not isinstance(raw, Mapping):
        return {}
    return {str(k): str(v or "") for k, v in cast(Mapping[object, object], raw).items()}


def _model_benched(entry: PoolEntry, model: str | None, now: datetime) -> bool:
    if not model:
        return False
    until = _benches(entry).get(model)
    if until is None:
        return False
    if until == "":
        return True
    try:
        return datetime.fromisoformat(until) > now
    except ValueError:
        return False


def _model_bench_end(entry: PoolEntry, model: str | None) -> datetime | None:
    if not model:
        return None
    until = _benches(entry).get(model)
    if not until:
        return None
    try:
        return datetime.fromisoformat(until)
    except ValueError:
        return None


def _ordered(entries: list[PoolEntry], strategy: str) -> list[PoolEntry]:
    """Entries in the order a status read reports them (``least_used`` by count)."""
    if strategy == "least_used":
        return sorted(entries, key=lambda e: (e.request_count, e.priority))
    return list(entries)


# -- one provider's live pool ---------------------------------------------------------------------


class PoolRuntime:
    """One provider's credentials for a session (see the module notes). Thread-safe."""

    def __init__(
        self,
        profile: ProviderProfile,
        *,
        store: AuthStore | None,
        secrets: SecretReader | None,
        env: Mapping[str, str],
        clock: Clock,
        strategy: str = "fill_first",
        rng: random.Random | None = None,
        refresher: Refresher | None = None,
    ) -> None:
        self.profile = profile
        self.provider = profile.name
        self.store = store
        self.secrets = secrets
        self.env = env
        self.clock = clock
        self.strategy = strategy if strategy in STRATEGIES else "fill_first"
        self.rng = rng if rng is not None else random.Random()
        self.refresher = refresher
        self.current_id: str | None = None
        self.revert_id: str | None = None
        self.retried_429 = False
        self.refreshes: dict[str, int] = {}
        self._lock = threading.RLock()
        self._unsaved: dict[str, PoolEntry] = {}
        self.pool = Pool(self.provider)
        self.inherited = False
        self.reload()

    # -- loading ----------------------------------------------------------------------------------

    def _env_rows(self) -> list[PoolEntry]:
        try:
            return env_entries(self.profile, self.secrets, self.env)
        except Exception:
            log.debug("secret store unreadable; env-seeded pool rows from the environment only")
            return env_entries(self.profile, None, self.env)

    def reload(self) -> None:
        """Read the pool again (turn start): ``indus auth add/reset`` from another process show up;
        benches that could not be written stay."""
        with self._lock:
            rows = self._env_rows()
            if self.store is None:
                pool, inherited = Pool(self.provider, rows), False
            else:
                try:
                    pool, inherited = load_pool(self.store, self.provider, rows)
                except Exception as exc:
                    log.warning("the %s credential pool could not be read: %s", self.provider, exc)
                    return
            for entry in pool.entries:
                kept = self._unsaved.get(entry.id)
                if kept is not None:
                    entry.status = kept.status
                    entry.cooldown_until = kept.cooldown_until
                    entry.last_error_class = kept.last_error_class
                    entry.extra = dict(kept.extra)
            self.pool, self.inherited = pool, inherited

    def new_turn(self) -> None:
        """Turn start: reread the rows and clear the per-turn refresh counter."""
        with self._lock:
            self.refreshes.clear()
        self.reload()

    def __len__(self) -> int:
        return len(self.pool)

    def entry(self, entry_id: str) -> PoolEntry | None:
        for entry in self.pool.entries:
            if entry.id == entry_id:
                return entry
        return None

    # -- secrets ----------------------------------------------------------------------------------

    def _stored(self, entry: PoolEntry) -> str | None:
        name = entry.env_var if entry.source == "env" and entry.env_var else None
        try:
            if name is not None:
                value = self.secrets.get(name) if self.secrets is not None else None
                if is_placeholder(value):
                    value = self.env.get(name)
                return value
            if self.secrets is None:
                return None
            return self.secrets.get(secret_name(self.provider, entry.id))
        except Exception as exc:
            log.debug("secret of %s pool entry %s unreadable: %s", self.provider, entry.id, exc)
            return None

    def key_of(self, entry: PoolEntry) -> str | None:
        """The value sent as the key: the API key, or a sign-in's access token."""
        stored = self._stored(entry)
        if stored is None:
            return None
        if entry.auth_type == "oauth":
            from indus.providers.auth.device_code import TokenSet

            try:
                token = TokenSet.from_secret(stored).access_token.strip()
            except (ValueError, KeyError, TypeError):
                return None
            # A token a sign-in issued is never a typed placeholder; the "looks like a
            # placeholder" rules (e.g. "xxx" inside) would drop real tokens by chance.
            return token or None
        return None if is_placeholder(stored) else stored.strip()

    def _host_ok(self, entry: PoolEntry, route: RuntimeRoute | None) -> bool:
        if route is None:
            return True
        try:
            return key_allowed(self.profile, route.base_url, entry.env_var or "")
        except Exception:
            return False

    # -- status (peeks) ---------------------------------------------------------------------------

    def _ready(self, entry: PoolEntry, model: str | None, now: datetime) -> bool:
        return entry.available(now) and not _model_benched(entry, model, now)

    def _usable(
        self, entry: PoolEntry, model: str | None, now: datetime, route: RuntimeRoute | None
    ) -> bool:
        return (
            self._ready(entry, model, now)
            and self._host_ok(entry, route)
            and self.key_of(entry) is not None
        )

    def peek(self, model: str | None = None) -> PoolEntry | None:
        """The credential the session would use next. Changes nothing."""
        with self._lock:
            now = self.clock.now()
            current = self.entry(self.current_id) if self.current_id else None
            if current is not None and self._usable(current, model, now, None):
                return current
            ready = [e for e in self.pool.entries if self._usable(e, model, now, None)]
            ordered = _ordered(ready, self.strategy)
            return ordered[0] if ordered else None

    def would_fail(self, model: str | None = None) -> bool:
        """True when the pool has credentials and all of them stay out for longer than the
        longest retry wait (the fallback walk and the primary restore ask this). A peek."""
        with self._lock:
            now = self.clock.now()
            keyed = [e for e in self.pool.entries if self.key_of(e) is not None]
            if not keyed:
                return False
            if any(self._ready(e, model, now) for e in keyed):
                return False
            back = [self._back_at(e, model) for e in keyed]
            known = [b for b in back if b is not None]
            if len(known) < len(back):
                return True  # benched until a manual reset
            return min(known) - now > timedelta(seconds=GATE_WAIT_S)

    def _back_at(self, entry: PoolEntry, model: str | None) -> datetime | None:
        ends: list[datetime] = []
        if entry.status != "ok":
            end = entry.cooldown_end()
            if end is None:
                return None
            ends.append(end)
        if model and model in _benches(entry):
            end = _model_bench_end(entry, model)
            if end is None:
                return None
            ends.append(end)
        return max(ends) if ends else self.clock.now()

    # -- selection --------------------------------------------------------------------------------

    def _pick(self, candidates: list[PoolEntry]) -> PoolEntry:
        if self.strategy == "random":
            return self.rng.choice(candidates)
        if self.strategy == "least_used":
            return min(candidates, key=lambda e: (e.request_count, e.priority))
        return candidates[0]

    def _lease(self, entry: PoolEntry) -> Lease | None:
        key = self.key_of(entry)
        if key is None:
            return None
        return Lease(entry.id, entry.label, Secret(key), entry.env_var or "")

    def lease(self, route: RuntimeRoute, model: str | None = None) -> Lease | None:
        """The credential for the next request (see the module notes); None: use the route's own
        key (no credential in the pool can be sent to this route)."""
        model = model if model is not None else route.model
        moved: tuple[str, int] | None = None
        with self._lock:
            now = self.clock.now()
            usable = [e for e in self.pool.entries if self._usable(e, model, now, route)]
            ids = {e.id for e in usable}
            if self.revert_id is not None and self.revert_id in ids:
                log.info(
                    "%s credential %s is back from its cooldown; using it again",
                    self.provider,
                    self.revert_id,
                )
                self.current_id, self.revert_id = self.revert_id, None
            if self.current_id in ids:
                current = self.entry(self.current_id or "")
                assert current is not None
                return self._lease(current)
            if usable:
                chosen = self._pick(usable)
                self.current_id = chosen.id
                chosen.request_count += 1
                if self.store is not None:
                    note_request(self.store, self.provider, chosen.id)
                if self.strategy == "round_robin" and len(usable) > 1:
                    self.pool.set_priority(chosen.id, len(self.pool))
                    moved = (chosen.id, len(self.pool))
                lease = self._lease(chosen)
            else:
                lease = self._stand_in(model, route)
        if moved is not None:
            self._write_order(*moved)
        return lease

    def _stand_in(self, model: str | None, route: RuntimeRoute) -> Lease | None:
        """Nothing is usable: keep sending with the current credential, else the one back first."""
        keyed = [
            e for e in self.pool.entries if self._host_ok(e, route) and self.key_of(e) is not None
        ]
        if not keyed:
            return None
        current = next((e for e in keyed if e.id == self.current_id), None)
        if current is None:
            far = self.clock.now() + timedelta(days=36500)
            current = min(keyed, key=lambda e: self._back_at(e, model) or far)
        return self._lease(current)

    def _has_other(self, lease: Lease | None, model: str | None, route: RuntimeRoute) -> bool:
        now = self.clock.now()
        skip = lease.entry_id if lease is not None else None
        return any(e.id != skip and self._usable(e, model, now, route) for e in self.pool.entries)

    # -- outcomes ---------------------------------------------------------------------------------

    def after_success(self, lease: Lease | None) -> None:
        """An answered request: the "already retried a 429" flag clears."""
        with self._lock:
            self.retried_429 = False

    def adjuster(self, lease: Lease | None, model: str, route: RuntimeRoute) -> Adjust:
        """The per-attempt rule inside P03's retries (see the module notes)."""

        def adjust(verdict: ClassifiedError) -> ClassifiedError:
            if lease is None or not verdict.retryable:
                return verdict
            if verdict.reason != "rate_limit":
                return verdict
            with self._lock:
                if not is_plan_limit(verdict) and not self.retried_429:
                    self.retried_429 = True
                    return verdict
                if self._has_other(lease, model, route):
                    return replace(verdict, retryable=False)
            return verdict

        return adjust

    async def after_failure(
        self,
        lease: Lease | None,
        verdict: ClassifiedError,
        model: str,
        route: RuntimeRoute,
    ) -> bool:
        """Apply the policy to the credential that failed; True: try the call again (same key
        after a refresh or a first 429, or the next credential)."""
        if lease is None:
            return False
        with self._lock:
            now = self.clock.now()
            policy = policy_for(
                verdict, provider=self.provider, retried_429=self.retried_429, now=now
            )
            if policy.kind == "pass":
                return False
            if policy.kind == "retry":
                self.retried_429 = True
                return True
            entry = self.entry(lease.entry_id)
            if entry is None:
                return False
        if policy.kind == "refresh" and await self._refresh(entry):
            return True
        until = (
            now + timedelta(seconds=policy.cooldown_s) if policy.cooldown_s is not None else None
        )
        bench_model = model if policy.per_model else None
        await self._bench(entry.id, until, policy.error_class, bench_model)
        with self._lock:
            if policy.error_class in QUOTA_CLASSES and self.revert_id is None:
                self.revert_id = entry.id
            if self.current_id == entry.id:
                self.current_id = None
            if policy.error_class == "rate_limit":
                self.retried_429 = False
            other = self._has_other(None, model, route)
        log.info(
            "%s credential %r benched (%s%s, %s); %s",
            self.provider,
            entry.label,
            policy.error_class,
            f" for {bench_model}" if bench_model else "",
            f"until {until.isoformat()}" if until is not None else "until reset",
            "rotating" if other else "no other credential is usable",
        )
        return other

    async def renewed_ahead(self, lease: Lease) -> Lease:
        """``lease`` with a renewed access token when its sign-in expires within the refresh
        margin (``device_code.REFRESH_SKEW_SECONDS``); unchanged otherwise or when renewing fails
        (the request then goes out and a 401 takes the refresh path)."""
        from indus.providers.auth.device_code import TokenSet, needs_refresh

        entry = self.entry(lease.entry_id)
        if entry is None or entry.auth_type != "oauth" or self.refresher is None:
            return lease
        stored = self._stored(entry)
        if not stored:
            return lease
        try:
            tokens = TokenSet.from_secret(stored)
        except (ValueError, KeyError, TypeError):
            return lease
        if not needs_refresh(tokens, self.clock.now()):
            return lease
        try:
            fresh = await self.refresher(self.provider, entry, stored)
        except Exception as exc:
            log.warning(
                "renewing %s sign-in %r ahead of expiry failed: %s", self.provider, entry.label, exc
            )
            return lease
        if not fresh:
            return lease
        log.info("%s sign-in %r renewed ahead of expiry", self.provider, entry.label)
        return replace(lease, key=Secret(fresh))

    async def _refresh(self, entry: PoolEntry) -> bool:
        if entry.auth_type != "oauth" or self.refresher is None:
            return False
        with self._lock:
            count = self.refreshes.get(entry.id, 0)
            if count >= MAX_REFRESHES:
                log.warning(
                    "%s sign-in %r still refused after %d refreshes; rotating",
                    self.provider,
                    entry.label,
                    count,
                )
                return False
            self.refreshes[entry.id] = count + 1
        stored = self._stored(entry)
        if not stored:
            return False
        try:
            fresh = await self.refresher(self.provider, entry, stored)
        except Exception as exc:
            log.warning("refreshing %s sign-in %r failed: %s", self.provider, entry.label, exc)
            return False
        if not fresh:
            return False
        log.info("%s sign-in %r refreshed after a 401", self.provider, entry.label)
        return True

    async def _bench(
        self, entry_id: str, until: datetime | None, error_class: str, model: str | None
    ) -> None:
        with self._lock:
            entry = self.entry(entry_id)
            if entry is None:
                return
            _mark(entry, until, error_class, model)
            snapshot = replace(entry, extra=dict(entry.extra))
            store, inherited = self.store, self.inherited
            if store is None or inherited:
                self._unsaved[entry_id] = snapshot
                return

        def change(pool: Pool, _doc: dict[str, Any]) -> None:
            for row in pool.entries:
                if row.id == entry_id:
                    _mark(row, until, error_class, model)

        def write() -> None:
            edit_pool(store, self.provider, change, self._env_rows())

        try:
            await run_blocking(write, label="credential pool")
        except Exception as exc:
            log.warning("could not record the %s credential bench: %s", self.provider, exc)
            with self._lock:
                self._unsaved[entry_id] = snapshot
            return
        with self._lock:
            self._unsaved.pop(entry_id, None)

    def _write_order(self, entry_id: str, priority: int) -> None:
        store = self.store
        if store is None or self.inherited:
            return

        def change(pool: Pool, _doc: dict[str, Any]) -> None:
            if entry_id in pool.ids():
                pool.set_priority(entry_id, priority)

        try:
            edit_pool(store, self.provider, change, self._env_rows())
        except Exception as exc:
            log.debug("could not record the %s round-robin order: %s", self.provider, exc)

    # -- the call loop ----------------------------------------------------------------------------

    async def call[T](
        self,
        route: RuntimeRoute,
        run: Callable[[RuntimeRoute, Adjust], Awaitable[T]],
        *,
        still_current: Callable[[], bool] = lambda: True,
        answered: Callable[[T], bool] = lambda _: True,
    ) -> T:
        """``run(route with the leased key, adjuster)`` until it answers or the pool has nothing
        left (``RetryFailed`` is re-raised for the fallback chain)."""
        model = route.model
        budget = 2 * max(1, len(self.pool)) + MAX_REFRESHES + 1
        while True:
            lease = self.lease(route, model)
            if lease is not None:
                lease = await self.renewed_ahead(lease)
            target = lease.apply(route) if lease is not None else route
            adjust = self.adjuster(lease, model, route)
            try:
                outcome = await run(target, adjust)
            except RetryFailed as exc:
                budget -= 1
                again = await self.after_failure(lease, exc.classified, model, route)
                if again and budget > 0 and still_current():
                    continue
                raise
            if answered(outcome):
                self.after_success(lease)
            return outcome


def _mark(entry: PoolEntry, until: datetime | None, error_class: str, model: str | None) -> None:
    if model:
        benches = _benches(entry)
        benches[model] = _iso(until) or ""
        entry.extra[BENCHES_FIELD] = benches
        return
    entry.status = "exhausted"
    entry.last_error_class = error_class
    entry.cooldown_until = _iso(until)


# -- a session's pools ----------------------------------------------------------------------------


@dataclass
class PoolSet:
    """One ``PoolRuntime`` per provider for a session (subagents share the parent's set)."""

    store: AuthStore | None
    secrets: SecretReader | None
    env: Mapping[str, str]
    clock: Clock
    config: object = None
    rng: random.Random | None = None
    refresher: Refresher | None = None
    _pools: dict[str, PoolRuntime] = field(default_factory=dict[str, PoolRuntime])

    @classmethod
    def from_scope(cls, scope: Any, *, rng: random.Random | None = None) -> PoolSet:
        """The scope's pools; without a readable auth store, env-seeded rows only."""
        store: AuthStore | None
        try:
            store = AuthStore(scope.home, env=scope.env)
        except Exception as exc:
            log.debug("credential pools without an auth store: %s", exc)
            store = None
        secrets = cast(SecretReader | None, getattr(scope, "secrets", None))
        return cls(
            store=store,
            secrets=secrets,
            env=scope.env,
            clock=scope.clock,
            config=scope.config,
            rng=rng,
            refresher=sign_in_refresher(secrets, scope.clock, getattr(scope, "home", None)),
        )

    def runtime(self, profile: ProviderProfile) -> PoolRuntime:
        found = self._pools.get(profile.name)
        if found is None:
            found = PoolRuntime(
                profile,
                store=self.store,
                secrets=self.secrets,
                env=self.env,
                clock=self.clock,
                strategy=strategy_for(self.config, profile.name),
                rng=self.rng,
                refresher=self.refresher,
            )
            self._pools[profile.name] = found
        return found

    def for_route(self, route: RuntimeRoute) -> PoolRuntime | None:
        """The pool for ``route``'s provider, or None (no profile, keyless, empty pool)."""
        profile = route.profile
        if profile is None or profile.auth_type == "none":
            return None
        try:
            found = self.runtime(profile)
        except Exception as exc:
            log.warning("the %s credential pool is unavailable: %s", profile.name, exc)
            return None
        return found if len(found) else None

    def new_turn(self) -> None:
        for runtime in list(self._pools.values()):
            runtime.new_turn()


# -- sign-in refresh ------------------------------------------------------------------------------


def sign_in_refresher(
    secrets: SecretReader | None, clock: Clock, home: Path | None = None
) -> Refresher | None:
    """Renew a stored sign-in through its provider's token endpoint and store the new tokens
    (``auth.renew``: locked per credential and across processes, and a pair another session
    already renewed is adopted instead of spending its refresh token again); None when the secret
    store cannot be written."""
    if secrets is None or not callable(getattr(secrets, "set", None)):
        return None
    sink = cast(SecretSink, secrets)

    async def refresh(provider: str, entry: PoolEntry, stored: str) -> str | None:
        from indus.providers.auth import device_code, device_signin, renew

        spec = device_signin.refresh_spec(provider)
        if spec is None:
            return None
        seen = device_code.TokenSet.from_secret(stored).access_token
        if home is None:
            tokens = device_code.TokenSet.from_secret(stored)
            async with device_code.make_client() as client:
                fresh = await device_code.refresh_tokens(client, spec, tokens, clock=clock)
            sink.set(secret_name(provider, entry.id), fresh.to_secret())
            return fresh.access_token

        where = home

        def work() -> device_code.TokenSet | None:
            return renew.renew_stored(
                home=where,
                secrets=sink,
                provider=provider,
                entry_id=entry.id,
                spec=spec,
                clock=clock,
                seen_access=seen,
            )

        fresh = await run_blocking(work, label="sign-in renewal")
        return fresh.access_token if fresh is not None else None

    return refresh


# -- route resolution with pool-only keys ---------------------------------------------------------


def pool_key(scope: Any, profile: ProviderProfile) -> tuple[str, str] | None:
    """``(env name or "", key)`` of the first usable pool credential of ``profile`` (a peek), for
    route resolution when no key variable resolves. None when there is none."""
    try:
        pools = PoolSet.from_scope(scope)
        if pools.store is None:
            return None
        runtime = pools.runtime(profile)
        entry = runtime.peek()
        if entry is None:
            return None
        key = runtime.key_of(entry)
    except Exception as exc:
        log.debug("credential pool of %s not consulted: %s", profile.name, exc)
        return None
    if key is None:
        return None
    return entry.env_var or "", key
