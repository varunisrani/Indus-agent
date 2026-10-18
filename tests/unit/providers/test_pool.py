"""Credential pool runtime (P08-12): selection, the error policy table, return to the original
key, the refresh cap, peeks, strategies, and pool-only keys making a provider usable."""

from __future__ import annotations

import asyncio
import random
from collections import Counter
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.providers import bundled
from indus.providers.auth import pool_model
from indus.providers.auth.device_code import TokenSet
from indus.providers.auth.pool_model import (
    Pool,
    PoolEntry,
    edit_pool,
    load_pool,
    pending_requests,
    secret_name,
)
from indus.providers.auth.store import AuthStore
from indus.providers.classify import ClassifiedError
from indus.providers.pool import (
    BENCHES_FIELD,
    COOLDOWN_AUTH_S,
    MAX_REFRESHES,
    PoolRuntime,
    PoolSet,
    is_plan_limit,
    policy_for,
)
from indus.providers.profile import ProviderProfile
from indus.providers.retry import RetryFailed
from indus.providers.route import RuntimeRoute, resolve
from tests.fakes.clock import FakeClock
from tests.fakes.oauth_server import fake_token
from tests.fakes.secret_store import MemorySecretStore
from tests.unit.providers.conftest import ScopeFactory

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
OPENROUTER = bundled.by_name()["openrouter"]
ANTHROPIC = bundled.by_name()["anthropic"]
HOUR = timedelta(hours=1)


@pytest.fixture(autouse=True)
def no_pending() -> Iterator[None]:
    pool_model.reset_pending_requests()
    yield
    pool_model.reset_pending_requests()


def key(tag: str) -> str:
    return f"test-key-{tag}-0000"


def route_for(profile: ProviderProfile = OPENROUTER, model: str = "m1") -> RuntimeRoute:
    return RuntimeRoute(
        provider=profile.name,
        model=model,
        api_mode=profile.api_mode,
        base_url=profile.base_url,
        profile=profile,
    )


class Fixture:
    """A store with ``labels`` manual API-key entries (secrets in memory) and a fake clock."""

    def __init__(
        self,
        tmp_path: Path,
        *labels: str,
        profile: ProviderProfile = OPENROUTER,
        strategy: str = "fill_first",
        rng: random.Random | None = None,
        oauth: tuple[str, ...] = (),
    ) -> None:
        self.profile = profile
        self.clock = FakeClock(NOW)
        self.store = AuthStore(tmp_path / "home")
        self.secrets = MemorySecretStore()
        self.strategy = strategy
        self.rng = rng
        self.refreshed: list[str] = []
        self.refresh_ok = True
        entries: list[PoolEntry] = []
        for label in labels:
            entry = PoolEntry(id=f"id{label}", label=label)
            self.secrets.set(secret_name(profile.name, entry.id), key(label))
            entries.append(entry)
        for label in oauth:
            entry = PoolEntry(id=f"id{label}", label=label, auth_type="oauth", source="pkce")
            tokens = TokenSet(fake_token("access"), NOW, refresh_token=fake_token("refresh"))
            self.secrets.set(secret_name(profile.name, entry.id), tokens.to_secret())
            entries.append(entry)

        def seed(pool: Pool, _doc: dict[str, Any]) -> None:
            for entry in entries:
                pool.add(entry)

        edit_pool(self.store, profile.name, seed)

    async def refresher(self, provider: str, entry: PoolEntry, stored: str) -> str | None:
        self.refreshed.append(entry.label)
        return fake_token("access", len(self.refreshed) + 1) if self.refresh_ok else None

    def runtime(self) -> PoolRuntime:
        return PoolRuntime(
            self.profile,
            store=self.store,
            secrets=self.secrets,
            env={},
            clock=self.clock,
            strategy=self.strategy,
            rng=self.rng,
            refresher=self.refresher,
        )

    def disk(self) -> dict[str, PoolEntry]:
        pool, _ = load_pool(self.store, self.profile.name)
        return {e.label: e for e in pool.entries}


def rate_limit(message: str = "Rate limit exceeded", **values: Any) -> ClassifiedError:
    return ClassifiedError(
        "rate_limit",
        status=values.pop("status", 429),
        message=message,
        should_rotate_credential=True,
        should_fallback=True,
        **values,
    )


def failure(reason: str, status: int, message: str = "") -> ClassifiedError:
    return ClassifiedError(reason, status=status, message=message, retryable=False)  # type: ignore[arg-type]


def label_of(runtime: PoolRuntime, model: str = "m1") -> str | None:
    lease = runtime.lease(route_for(runtime.profile, model), model)
    return lease.label if lease is not None else None


def fail(runtime: PoolRuntime, verdict: ClassifiedError, model: str = "m1") -> bool:
    route = route_for(runtime.profile, model)
    lease = runtime.lease(route, model)
    return asyncio.run(runtime.after_failure(lease, verdict, model, route))


# -- selection ------------------------------------------------------------------------------------


def test_lease_is_sticky_counted_once_and_carries_the_key(tmp_path: Path) -> None:
    fx = Fixture(tmp_path, "a", "b")
    runtime = fx.runtime()
    route = route_for()
    first = runtime.lease(route)
    again = runtime.lease(route)
    assert first is not None and again is not None and first.entry_id == again.entry_id == "ida"
    assert first.apply(route).api_key is not None
    assert first.apply(route).api_key.reveal() == key("a")  # type: ignore[union-attr]
    assert pending_requests(fx.store, "openrouter") == {"ida": 1}


def test_key_is_never_leased_to_another_host(tmp_path: Path) -> None:
    fx = Fixture(tmp_path, "a")
    foreign = RuntimeRoute(
        provider="openrouter",
        model="m1",
        api_mode="chat_completions",
        base_url="https://openrouter.ai.example.net/api/v1",
        profile=OPENROUTER,
    )
    assert fx.runtime().lease(foreign) is None


# -- the policy table -----------------------------------------------------------------------------


def test_generic_429_retries_the_same_key_once_then_rotates_for_an_hour(tmp_path: Path) -> None:
    fx = Fixture(tmp_path, "a", "b")
    runtime = fx.runtime()
    route = route_for()
    lease = runtime.lease(route)
    adjust = runtime.adjuster(lease, "m1", route)
    assert adjust(rate_limit()).retryable  # first 429: same key once more
    assert not adjust(rate_limit()).retryable  # second in a row: stop, rotate
    assert asyncio.run(runtime.after_failure(lease, rate_limit(), "m1", route))
    benched = fx.disk()["a"]
    assert benched.status == "exhausted" and benched.last_error_class == "rate_limit"
    assert benched.cooldown_end() == NOW + HOUR
    assert label_of(runtime) == "b"


def test_the_retried_flag_clears_on_every_success(tmp_path: Path) -> None:
    fx = Fixture(tmp_path, "a", "b")
    runtime = fx.runtime()
    route = route_for()
    lease = runtime.lease(route)
    adjust = runtime.adjuster(lease, "m1", route)
    assert adjust(rate_limit()).retryable
    runtime.after_success(lease)
    assert adjust(rate_limit()).retryable  # a single transient 429 never rotates


def test_plan_limit_rotates_at_once_until_the_provider_reset(tmp_path: Path) -> None:
    fx = Fixture(tmp_path, "a", "b")
    runtime = fx.runtime()
    route = route_for()
    lease = runtime.lease(route)
    verdict = rate_limit("Usage limit reached; try again in 2 hours", reset_at=NOW + 2 * HOUR)
    assert is_plan_limit(verdict)
    assert not runtime.adjuster(lease, "m1", route)(verdict).retryable
    assert asyncio.run(runtime.after_failure(lease, verdict, "m1", route))
    assert fx.disk()["a"].cooldown_end() == NOW + 2 * HOUR
    assert label_of(runtime) == "b"


def test_billing_rotates_for_an_hour(tmp_path: Path) -> None:
    fx = Fixture(tmp_path, "a", "b")
    runtime = fx.runtime()
    assert fail(runtime, failure("billing", 402, "insufficient credits"))
    assert fx.disk()["a"].last_error_class == "billing"
    assert fx.disk()["a"].cooldown_end() == NOW + HOUR
    assert label_of(runtime) == "b"


def test_401_on_an_api_key_rotates_for_five_minutes(tmp_path: Path) -> None:
    fx = Fixture(tmp_path, "a", "b")
    runtime = fx.runtime()
    assert fail(runtime, failure("auth", 401, "invalid api key"))
    assert fx.refreshed == []
    assert fx.disk()["a"].cooldown_end() == NOW + timedelta(seconds=COOLDOWN_AUTH_S)
    assert label_of(runtime) == "b"


def test_401_on_a_sign_in_refreshes_first_and_is_capped(tmp_path: Path) -> None:
    fx = Fixture(tmp_path, "b", oauth=("s",))
    runtime = fx.runtime()
    runtime.pool.set_priority("ids", 0)  # the sign-in first
    assert label_of(runtime) == "s"
    for _ in range(MAX_REFRESHES):
        assert fail(runtime, failure("auth", 401, "token expired"))
        assert label_of(runtime) == "s"  # refreshed: same entry again
    assert fail(runtime, failure("auth", 401, "token expired"))  # cap reached: rotate
    assert fx.refreshed == ["s"] * MAX_REFRESHES
    assert label_of(runtime) == "b"
    runtime.new_turn()  # the counter is per turn
    assert runtime.refreshes == {}


def test_failed_refresh_rotates(tmp_path: Path) -> None:
    fx = Fixture(tmp_path, "b", oauth=("s",))
    fx.refresh_ok = False
    runtime = fx.runtime()
    runtime.pool.set_priority("ids", 0)
    assert fail(runtime, failure("auth", 401, "token expired"))
    assert fx.refreshed == ["s"] and label_of(runtime) == "b"


def test_entitlement_400_benches_that_model_only_until_a_reset(tmp_path: Path) -> None:
    fx = Fixture(tmp_path, "a", "b")
    runtime = fx.runtime()
    verdict = failure("format_error", 400, "The 'm1' model is not available on your plan.")
    assert fail(runtime, verdict, "m1")
    row = fx.disk()["a"]
    assert row.status == "ok" and row.extra[BENCHES_FIELD] == {"m1": ""}
    assert label_of(runtime, "m1") == "b"
    fresh = fx.runtime()
    assert label_of(fresh, "m2") == "a"  # other models keep the key
    fx.clock.advance(10 * 24 * 3600)
    assert label_of(fx.runtime(), "m1") == "b"  # no timer lifts it

    def reset(pool: Pool, _doc: dict[str, Any]) -> None:
        pool.reset()

    edit_pool(fx.store, "openrouter", reset)  # `indus auth reset openrouter`
    assert BENCHES_FIELD not in fx.disk()["a"].extra
    assert label_of(fx.runtime(), "m1") == "a"


def test_per_model_vendor_429_benches_only_that_model(tmp_path: Path) -> None:
    fx = Fixture(tmp_path, "a", "b", profile=ANTHROPIC)
    runtime = fx.runtime()
    route = route_for(ANTHROPIC, "claude-x")
    lease = runtime.lease(route)
    runtime.adjuster(lease, "claude-x", route)(rate_limit())
    assert asyncio.run(runtime.after_failure(lease, rate_limit(), "claude-x", route))
    row = fx.disk()["a"]
    assert row.status == "ok" and row.extra[BENCHES_FIELD] == {"claude-x": (NOW + HOUR).isoformat()}
    assert label_of(runtime, "claude-y") == "a"


def test_all_keys_exhausted_falls_through_but_never_stops_sending(tmp_path: Path) -> None:
    fx = Fixture(tmp_path, "a")
    runtime = fx.runtime()
    assert not fail(runtime, failure("billing", 402, "insufficient credits"))
    assert runtime.would_fail("m1")
    assert label_of(runtime) == "a"  # the bound key still goes out
    fx.clock.advance(HOUR.total_seconds() - 500)
    assert not runtime.would_fail("m1")  # back within the 600 s retry wait


def test_provider_reset_overrides_the_default_cooldown() -> None:
    verdict = rate_limit(reset_at=NOW + timedelta(minutes=90))
    policy = policy_for(verdict, provider="openrouter", retried_429=True, now=NOW)
    assert policy.kind == "rotate" and policy.cooldown_s == 90 * 60
    billing = replace(failure("billing", 402, "spend limit"), reset_at=NOW + 2 * HOUR)
    assert policy_for(billing, provider="x", retried_429=False, now=NOW).cooldown_s == 7200


def test_session_returns_to_its_original_key_after_the_cooldown(tmp_path: Path) -> None:
    fx = Fixture(tmp_path, "a", "b", strategy="least_used")
    runtime = fx.runtime()
    assert label_of(runtime) == "a"
    assert fail(runtime, failure("billing", 402, "insufficient credits"))
    assert label_of(runtime) == "b"
    assert label_of(runtime) == "b"
    fx.clock.advance(HOUR.total_seconds() + 1)
    runtime.new_turn()
    assert label_of(runtime) == "a"
    assert pending_requests(fx.store, "openrouter").get("ida", 0) <= 1  # going back is no pick


def test_a_bench_that_cannot_be_written_stays_in_memory(tmp_path: Path) -> None:
    secrets = MemorySecretStore({"OPENROUTER_API_KEY": key("env")})
    clock = FakeClock(NOW)
    runtime = PoolRuntime(OPENROUTER, store=None, secrets=secrets, env={}, clock=clock)
    assert label_of(runtime) == "OPENROUTER_API_KEY"
    assert not fail(runtime, failure("billing", 402, "insufficient credits"))
    runtime.new_turn()
    assert runtime.would_fail("m1")


# -- status reads ---------------------------------------------------------------------------------


def test_peek_and_would_fail_change_nothing(tmp_path: Path) -> None:
    fx = Fixture(tmp_path, "a", "b", strategy="round_robin")
    runtime = fx.runtime()
    before = (fx.store.path.read_bytes(), dict(pending_requests(fx.store, "openrouter")))
    for _ in range(3):
        peeked = runtime.peek("m1")
        assert peeked is not None and peeked.label == "a"
        assert not runtime.would_fail("m1")
    assert runtime.current_id is None and fx.refreshed == []
    assert (fx.store.path.read_bytes(), dict(pending_requests(fx.store, "openrouter"))) == before


# -- strategies -----------------------------------------------------------------------------------


def test_round_robin_cycles_across_sessions(tmp_path: Path) -> None:
    fx = Fixture(tmp_path, "a", "b", "c", strategy="round_robin")
    picks = [label_of(fx.runtime()) for _ in range(6)]
    assert picks == ["a", "b", "c", "a", "b", "c"]


def test_least_used_spreads_evenly(tmp_path: Path) -> None:
    fx = Fixture(tmp_path, "a", "b", "c", strategy="least_used")
    picks = Counter(label_of(fx.runtime()) for _ in range(9))
    assert picks == {"a": 3, "b": 3, "c": 3}


def test_random_distribution_with_a_seeded_rng(tmp_path: Path) -> None:
    fx = Fixture(tmp_path, "a", "b", "c", strategy="random", rng=random.Random(7))
    picks = Counter(label_of(fx.runtime()) for _ in range(300))
    assert set(picks) == {"a", "b", "c"}
    assert all(70 <= n <= 130 for n in picks.values()), picks


def test_fill_first_keeps_priority_order(tmp_path: Path) -> None:
    fx = Fixture(tmp_path, "a", "b")
    assert [label_of(fx.runtime()) for _ in range(3)] == ["a", "a", "a"]


# -- the call loop --------------------------------------------------------------------------------


def _failed(verdict: ClassifiedError) -> RetryFailed:
    return RetryFailed(verdict, error=RuntimeError(verdict.reason), attempts=1, waits=())


def test_call_rotates_then_raises_for_the_fallback_chain(tmp_path: Path) -> None:
    fx = Fixture(tmp_path, "a", "b")
    runtime = fx.runtime()
    sent: list[str] = []

    async def run(route: RuntimeRoute, _adjust: Any) -> str:
        assert route.api_key is not None
        sent.append(route.api_key.reveal())
        if route.api_key.reveal() == key("a"):
            raise _failed(failure("billing", 402, "insufficient credits"))
        return "answer"

    assert asyncio.run(runtime.call(route_for(), run)) == "answer"
    assert sent == [key("a"), key("b")]

    async def always(route: RuntimeRoute, _adjust: Any) -> str:
        raise _failed(failure("billing", 402, "insufficient credits"))

    with pytest.raises(RetryFailed):
        asyncio.run(fx.runtime().call(route_for(), always))


def test_call_stops_rotating_once_the_route_moved(tmp_path: Path) -> None:
    fx = Fixture(tmp_path, "a", "b")
    calls: list[str] = []

    async def run(route: RuntimeRoute, _adjust: Any) -> str:
        calls.append("x")
        raise _failed(failure("billing", 402, "insufficient credits"))

    with pytest.raises(RetryFailed):
        asyncio.run(fx.runtime().call(route_for(), run, still_current=lambda: False))
    assert calls == ["x"]


def test_pool_set_skips_routes_without_a_profile(tmp_path: Path) -> None:
    fx = Fixture(tmp_path, "a")
    pools = PoolSet(store=fx.store, secrets=fx.secrets, env={}, clock=fx.clock)
    assert pools.for_route(route_for()) is not None
    bare = RuntimeRoute("openrouter", "m1", "chat_completions", OPENROUTER.base_url)
    assert pools.for_route(bare) is None


# -- pool-only keys make a provider usable ------------------------------------------------------


def test_pool_only_key_resolves_the_route_and_passes_the_first_run_check(
    make_scope: ScopeFactory,
) -> None:
    from indus.cli.first_run import usable_provider

    scope: Scope = make_scope(config={})
    assert scope.secrets is not None
    store = AuthStore(scope.home, env=scope.env)
    entry = PoolEntry(id="idpool", label="added-key")
    scope.secrets.set(secret_name("openrouter", entry.id), key("pool"))  # type: ignore[attr-defined]

    def add(pool: Pool, _doc: dict[str, Any]) -> None:
        pool.add(entry)

    edit_pool(store, "openrouter", add)
    route = resolve(scope, provider="openrouter", model="some/model")
    assert route.api_key is not None and route.api_key.reveal() == key("pool")
    assert route.base_url.startswith("https://openrouter.ai")
    auto = resolve(scope)
    assert auto.provider == "openrouter"
    assert usable_provider(scope) == "credential pool for openrouter"
