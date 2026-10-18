"""Fallback chain with turn-scoped restore (P08-13, R01.5, R01.10, R01.11)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import timedelta
from typing import Any

import httpx
import pytest
import respx

from indus.core.agent import Agent
from indus.core.exit_reasons import ExitReason
from indus.core.fallback import (
    WALKED_GATE_S,
    FallbackChain,
    is_trigger,
    read_chain,
)
from indus.core.route import RouteBinding
from indus.foundation.scope import Scope
from indus.protocol.events import EventBase, Notice
from indus.providers.auth.pool_model import Pool, PoolEntry, edit_pool, load_pool, secret_name
from indus.providers.auth.store import AuthStore
from indus.providers.classify import ClassifiedError
from indus.providers.model_list import ModelInfo
from indus.providers.profile import ProviderProfile
from indus.providers.route import RuntimeRoute, Secret
from tests.fakes.agent import DictConfig, fake_options, run_driven
from tests.fakes.clock import FakeClock
from tests.fakes.model import Fail, FakeModelServer, Finish, Turn, fake_route, script_of, text_turn
from tests.fakes.model.server import CHAT_BASE_URL, FAKE_HOST, FAKE_KEY

PRIMARY = "https://primary.invalid/v1"
BACKUP_HOST = "backup.invalid"
BACKUP = f"https://{BACKUP_HOST}/v1"
WINDOWS = {"main-model": 200_000, "backup-model": 128_000, "third-model": 100_000}


def route(provider: str, model: str, base_url: str = PRIMARY) -> RuntimeRoute:
    return RuntimeRoute(
        provider=provider,
        model=model,
        api_mode="chat_completions",
        base_url=base_url,
        api_key=Secret(FAKE_KEY),
        source="test",
    )


async def lister(_route: RuntimeRoute) -> list[ModelInfo]:
    return [ModelInfo(name, tokens) for name, tokens in WINDOWS.items()]


def clients(_route: RuntimeRoute) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(404)))


class Resolver:
    """Stands in for P03 resolution: known (provider, model) pairs, else the error P03 raises."""

    def __init__(self, routes: Mapping[tuple[str, str], RuntimeRoute]) -> None:
        self.routes = dict(routes)
        self.asked: list[dict[str, Any]] = []

    def __call__(self, _scope: Scope, **values: Any) -> RuntimeRoute:
        self.asked.append(values)
        found = self.routes.get((values["provider"], values["model"]))
        if found is None:
            raise LookupError(f"no key for {values['provider']}")
        return found


class Pools:
    """A ``PoolSet`` stand-in: providers whose pool would fail."""

    def __init__(self, failing: set[str] | None = None) -> None:
        self.failing = failing or set()
        self.turns = 0

    def for_route(self, target: RuntimeRoute) -> Any:
        failing = target.provider in self.failing

        class _Pool:
            def would_fail(self, _model: str | None = None) -> bool:
                return failing

        return _Pool()

    def new_turn(self) -> None:
        self.turns += 1


def chain_config(*entries: Any, min_switch: float = 0.0) -> DictConfig:
    return DictConfig(
        {"fallback_providers": list(entries), "fallback": {"min_switch_reset_s": min_switch}}
    )


async def make_chain(
    scope: Scope,
    clock: FakeClock,
    *entries: Any,
    resolver: Resolver | None = None,
    pools: Pools | None = None,
    min_switch: float = 0.0,
    offline: bool = False,
) -> FallbackChain:
    scoped = scope.with_config(chain_config(*entries, min_switch=min_switch))
    binding = await RouteBinding.bind(
        scoped, route("main", "main-model"), client_factory=clients, list_models=lister
    )
    resolver = resolver or Resolver(
        {
            ("backup", "backup-model"): route("backup", "backup-model", BACKUP),
            ("third", "third-model"): route("third", "third-model", "https://third.invalid/v1"),
        }
    )
    return FallbackChain(
        scoped,
        binding,
        clock=clock,
        pools=pools,  # type: ignore[arg-type]
        resolve=resolver,
        offline=offline,
    )


def verdict(reason: str, status: int | None = None, **values: Any) -> ClassifiedError:
    from indus.providers.classify import _HINTS  # pyright: ignore[reportPrivateUsage]

    hints = {"retryable": True, **_HINTS[reason], **values}
    return ClassifiedError(reason, status=status, message=reason, **hints)  # type: ignore[arg-type]


BACKUP_ENTRY = {"provider": "backup", "model": "backup-model"}
THIRD_ENTRY = {"provider": "third", "model": "third-model"}


# -- configuration and triggers -------------------------------------------------------------------


def test_chain_entries_need_provider_and_model() -> None:
    entries = read_chain(
        DictConfig(
            {
                "fallback_providers": [
                    {"provider": "backup"},
                    {"model": "x"},
                    "nonsense",
                    {"provider": "Custom", "model": "m", "base_url": "https://h.invalid/v1/"},
                    BACKUP_ENTRY,
                ]
            }
        )
    )
    assert [(e.provider, e.model, e.base_url) for e in entries] == [
        ("custom", "m", "https://h.invalid/v1"),
        ("backup", "backup-model", ""),
    ]
    assert read_chain(DictConfig({"fallback_providers": {"provider": "x"}})) == []


@pytest.mark.parametrize(
    ("reason", "status", "expected"),
    [
        ("auth", 401, True),
        ("auth_permanent", 403, True),
        ("model_not_found", 404, True),
        ("unknown", 404, True),
        ("billing", 402, True),
        ("rate_limit", 429, True),
        ("upstream_rate_limit", 429, True),
        ("content_policy", 400, True),
        ("overloaded", 503, True),
        ("server_error", 500, True),
        ("timeout", None, True),
        ("connection", None, True),
        ("incomplete_response", None, True),
        ("context_overflow", 400, False),
        ("payload_too_large", 413, False),
        ("stream_unsupported", 400, False),
    ],
)
def test_triggers(reason: str, status: int | None, expected: bool) -> None:
    assert is_trigger(verdict(reason, status)) is expected


# -- activation -----------------------------------------------------------------------------------


def test_activation_rebinds_every_field_and_restore_undoes_all_of_it(
    scope: Scope, fake_clock: FakeClock
) -> None:
    async def main() -> None:
        chain = await make_chain(scope, fake_clock, BACKUP_ENTRY)
        primary = chain.binding.state
        generation = chain.binding.generation
        decision = await chain.activate(verdict("auth", 401), "auth")
        assert decision.action == "switched"
        assert "main-model via main" in decision.notice
        assert "backup-model via backup" in decision.notice
        after = chain.binding.state
        assert chain.binding.generation == generation + 1  # an old payload is stale (R01.5)
        assert (after.provider, after.model, after.base_url) == ("backup", "backup-model", BACKUP)
        assert after.client is not primary.client and after.context_length == 128_000
        assert chain.active and chain.primary is primary
        notice = await chain.restore()
        assert notice is not None and notice.startswith("Main model restored: main-model via main")
        restored = chain.binding.state
        assert restored is primary  # route, client, window, reasoning, extra body, cache: all
        assert restored.context_length == 200_000 and restored.base_url == PRIMARY
        assert not chain.active and chain.index == 0

    asyncio.run(main())


def test_skip_rules(scope: Scope, fake_clock: FakeClock) -> None:
    async def main() -> None:
        resolver = Resolver(
            {
                ("backup", "backup-model"): route("backup", "backup-model", BACKUP),
                ("cooling", "c-model"): route("cooling", "c-model", "https://c.invalid/v1"),
                ("third", "third-model"): route("third", "third-model", "https://t.invalid/v1"),
            }
        )
        chain = await make_chain(
            scope,
            fake_clock,
            {"provider": "main", "model": "main-model"},  # the route that just failed
            {"provider": "embedder", "model": "text-embedding-3-large"},  # not a chat model
            {"provider": "nokey", "model": "m"},  # cannot be resolved
            {"provider": "cooling", "model": "c-model"},  # every credential cooling down
            THIRD_ENTRY,
            resolver=resolver,
            pools=Pools({"cooling"}),
        )
        decision = await chain.activate(verdict("server_error", 500), "server_error")
        assert decision.action == "switched"
        assert chain.binding.state.provider == "third"
        assert ("nokey", "m", "") in chain.unavailable
        assert [a["provider"] for a in resolver.asked] == ["nokey", "cooling", "third"]

    asyncio.run(main())


def test_offline_skips_networked_entries(scope: Scope, fake_clock: FakeClock) -> None:
    async def main() -> None:
        local = route("local", "local-model", "http://127.0.0.1:8080/v1")
        resolver = Resolver({("local", "local-model"): local})
        chain = await make_chain(
            scope,
            fake_clock,
            BACKUP_ENTRY,
            {"provider": "local", "model": "local-model", "base_url": "http://127.0.0.1:8080/v1"},
            resolver=resolver,
            offline=True,
        )
        await chain.activate(verdict("connection"), "connection")
        assert chain.binding.state.provider == "local"
        assert resolver.asked[0]["api_mode"] == "chat_completions"  # read from its own URL

    asyncio.run(main())


def test_api_mode_is_detected_from_the_entry_url_by_exact_host(
    scope: Scope, fake_clock: FakeClock
) -> None:
    async def main() -> None:
        lookalike = "https://api.anthropic.com.evil.invalid/v1"
        resolver = Resolver({("custom", "m"): route("custom", "m", lookalike)})
        chain = await make_chain(
            scope,
            fake_clock,
            {"provider": "custom", "model": "m", "base_url": lookalike},
            resolver=resolver,
        )
        await chain.activate(verdict("auth", 401), "auth")
        assert resolver.asked[0]["api_mode"] == "chat_completions"  # not the Anthropic wire

    asyncio.run(main())


def test_a_bind_failure_marks_the_entry_and_walks_on(scope: Scope, fake_clock: FakeClock) -> None:
    async def main() -> None:
        resolver = Resolver(
            {
                ("backup", "backup-model"): route("backup", "backup-model", ""),  # no URL: refused
                ("third", "third-model"): route("third", "third-model", "https://t.invalid/v1"),
            }
        )
        chain = await make_chain(scope, fake_clock, BACKUP_ENTRY, THIRD_ENTRY, resolver=resolver)
        decision = await chain.activate(verdict("auth", 401), "auth")
        assert decision.action == "switched" and chain.binding.state.provider == "third"
        assert ("backup", "backup-model", "") in chain.unavailable

    asyncio.run(main())


def test_a_failed_activation_still_resets_the_index(scope: Scope, fake_clock: FakeClock) -> None:
    async def main() -> None:
        resolver = Resolver({})
        chain = await make_chain(scope, fake_clock, BACKUP_ENTRY, resolver=resolver)
        decision = await chain.activate(verdict("server_error", 500), "server_error")
        assert decision.action == "none" and chain.index == 1 and not chain.active
        assert await chain.restore() is None  # nothing fell back: no "restored" notice
        assert chain.index == 0  # R01.10: the next turn can fall back again
        chain.unavailable.clear()
        resolver.routes[("backup", "backup-model")] = route("backup", "backup-model", BACKUP)
        assert (await chain.activate(verdict("server_error", 500), "x")).action == "switched"

    asyncio.run(main())


def test_no_restored_notice_when_nothing_fell_back(scope: Scope, fake_clock: FakeClock) -> None:
    async def main() -> None:
        pools = Pools()
        chain = await make_chain(scope, fake_clock, BACKUP_ENTRY, pools=pools)
        state = chain.binding.state
        assert await chain.restore() is None
        assert chain.binding.state is state and pools.turns == 1

    asyncio.run(main())


def test_empty_chain_does_nothing(scope: Scope, fake_clock: FakeClock) -> None:
    async def main() -> None:
        chain = await make_chain(scope, fake_clock)
        assert (await chain.activate(verdict("auth", 401), "auth")).action == "none"
        assert not chain.has_candidate()

    asyncio.run(main())


# -- the primary cooldown -------------------------------------------------------------------------


def test_rate_limited_primary_cools_down_on_a_doubling_ladder(
    scope: Scope, fake_clock: FakeClock
) -> None:
    async def main() -> None:
        chain = await make_chain(scope, fake_clock, BACKUP_ENTRY)
        decision = await chain.activate(verdict("rate_limit", 429), "rate_limit")
        assert "about 60 s" in decision.notice
        assert await chain.restore() is None and chain.active  # still cooling down
        fake_clock.advance(61)
        assert await chain.restore() is not None
        await chain.activate(verdict("rate_limit", 429), "rate_limit")
        assert chain.cooldown_until - fake_clock.monotonic() == pytest.approx(120)
        fake_clock.advance(121)
        assert await chain.restore() is not None
        reset = fake_clock.now() + timedelta(minutes=30)
        await chain.activate(verdict("rate_limit", 429, reset_at=reset), "rate_limit")
        assert chain.cooldown_until - fake_clock.monotonic() == pytest.approx(1800)

    asyncio.run(main())


def test_ladder_is_capped_at_four_hours(scope: Scope, fake_clock: FakeClock) -> None:
    async def main() -> None:
        chain = await make_chain(scope, fake_clock, BACKUP_ENTRY)
        chain.level = 12
        await chain.activate(verdict("billing", 402), "billing")
        assert chain.cooldown_until - fake_clock.monotonic() == pytest.approx(14_400)

    asyncio.run(main())


def test_walk_after_a_non_rate_limit_failure_arms_a_short_gate(
    scope: Scope, fake_clock: FakeClock
) -> None:
    async def main() -> None:
        chain = await make_chain(scope, fake_clock, BACKUP_ENTRY)
        await chain.activate(verdict("auth", 401), "auth")
        assert chain.cooldown_until == 0.0
        assert (await chain.activate(verdict("server_error", 500), "x")).action == "none"
        assert chain.cooldown_until == pytest.approx(fake_clock.monotonic() + WALKED_GATE_S)
        assert await chain.restore() is None
        fake_clock.advance(WALKED_GATE_S + 1)
        assert await chain.restore() is not None

    asyncio.run(main())


def test_stay_on_primary_when_its_reset_is_sooner_than_the_threshold(
    scope: Scope, fake_clock: FakeClock
) -> None:
    async def main() -> None:
        chain = await make_chain(scope, fake_clock, BACKUP_ENTRY, min_switch=120)
        soon = verdict("rate_limit", 429, reset_at=fake_clock.now() + timedelta(seconds=30))
        decision = await chain.activate(soon, "rate_limit")
        assert decision.action == "wait" and decision.seconds == pytest.approx(30)
        assert chain.binding.state.provider == "main"
        assert (await chain.activate(soon, "rate_limit")).action == "switched"  # once a turn
        late = await make_chain(scope, fake_clock, BACKUP_ENTRY, min_switch=120)
        far = verdict("rate_limit", 429, reset_at=fake_clock.now() + timedelta(seconds=600))
        assert (await late.activate(far, "rate_limit")).action == "switched"
        off = await make_chain(scope, fake_clock, BACKUP_ENTRY)
        assert (await off.activate(soon, "rate_limit")).action == "switched"

    asyncio.run(main())


# -- restore gates --------------------------------------------------------------------------------


def test_an_unentitled_primary_is_not_restored(scope: Scope, fake_clock: FakeClock) -> None:
    async def main() -> None:
        chain = await make_chain(scope, fake_clock, BACKUP_ENTRY)
        refused = ClassifiedError(
            "format_error",
            status=400,
            message="The 'main-model' model is not available on your plan.",
            retryable=False,
            should_fallback=True,
        )
        assert (await chain.activate(refused, "format_error")).action == "switched"
        assert await chain.restore() is None
        assert chain.binding.state.provider == "backup"  # no false "restored"

    asyncio.run(main())


def test_a_primary_whose_pool_would_fail_is_not_restored(
    scope: Scope, fake_clock: FakeClock
) -> None:
    async def main() -> None:
        pools = Pools({"main"})
        chain = await make_chain(scope, fake_clock, BACKUP_ENTRY, pools=pools)
        await chain.activate(verdict("auth", 401), "auth")
        assert await chain.restore() is None and chain.binding.state.provider == "backup"
        pools.failing.clear()
        assert await chain.restore() is not None

    asyncio.run(main())


def test_a_manual_switch_cancels_the_restore(scope: Scope, fake_clock: FakeClock) -> None:
    async def main() -> None:
        chain = await make_chain(scope, fake_clock, BACKUP_ENTRY, THIRD_ENTRY)
        await chain.activate(verdict("auth", 401), "auth")
        picked = await chain.binding.apply(route("third", "third-model", "https://t.invalid/v1"))
        assert await chain.restore() is None
        assert chain.binding.state is picked and not chain.active and chain.index == 0

    asyncio.run(main())


def test_eager_rule_needs_a_usable_entry(scope: Scope, fake_clock: FakeClock) -> None:
    async def main() -> None:
        upstream = verdict("upstream_rate_limit", 429)
        assert (await make_chain(scope, fake_clock, BACKUP_ENTRY)).wants_eager(upstream)
        assert not (await make_chain(scope, fake_clock)).wants_eager(upstream)
        chain = await make_chain(scope, fake_clock, BACKUP_ENTRY)
        assert not chain.wants_eager(verdict("rate_limit", 429))

    asyncio.run(main())


# -- through the agent and the loop ---------------------------------------------------------------


class Backup:
    """A fake model server behind a respx router on its own host."""

    def __init__(self, clock: FakeClock, *turns: Turn) -> None:
        self.server = FakeModelServer(script_of(*turns), clock=clock, model="backup-model")
        self.router = respx.MockRouter(
            base_url=BACKUP, assert_all_called=False, assert_all_mocked=True
        )
        self.chat = self.router.post("/chat/completions").mock(side_effect=self.server._handle)  # pyright: ignore[reportPrivateUsage]
        self.router.get("/models").respond(200, json={"data": []})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.router.async_handler))


def agent_scope(scope: Scope) -> Scope:
    entry = {"provider": "custom", "model": "backup-model", "base_url": BACKUP}
    env = scope.env.with_overlay({"OPENAI_API_KEY": None, "OPENAI_BASE_URL": None})
    return scope.with_config(chain_config(entry)).with_env(env)


def build(scope: Scope, primary: FakeModelServer, backup: Backup, events: list[EventBase]) -> Any:
    def factory(target: RuntimeRoute) -> httpx.AsyncClient:
        return backup.client() if target.host == BACKUP_HOST else primary.client()

    options = replace(fake_options(primary, max_retries=1), client_factory=factory)
    return Agent.start(agent_scope(scope), fake_route(primary), [], options, events.append)


def notices(events: list[EventBase]) -> list[str]:
    return [e.text for e in events if isinstance(e, Notice) and e.key == "fallback"]


def test_second_request_after_fallback_goes_to_the_new_host_and_primary_returns(
    scope: Scope, fake_clock: FakeClock
) -> None:
    primary = FakeModelServer(
        script_of(Turn((Fail(401, '{"error": {"message": "invalid api key"}}'),)), text_turn("p")),
        clock=fake_clock,
    )
    backup = Backup(fake_clock, text_turn("from the backup"))
    events: list[EventBase] = []

    async def main() -> list[Any]:
        agent = await build(scope, primary, backup, events)
        first = await agent.run_turn("hello")
        second = await agent.run_turn("again")
        await agent.close()
        return [first, second, agent]

    first, second, agent = asyncio.run(run_driven(main(), fake_clock))
    assert first.exit_reason == ExitReason.COMPLETED and first.final_text == "from the backup"
    assert backup.chat.call_count == 1
    sent = backup.chat.calls.last.request
    assert sent.url.host == BACKUP_HOST
    assert sent.headers.get("authorization") is None  # the primary's key stays on its host
    assert json.loads(sent.content)["model"] == "backup-model"  # rebuilt, not replayed (R01.5)
    assert second.final_text == "p"  # the next turn is back on the primary
    assert len(primary.model_requests) == 2
    texts = notices(events)
    assert len(texts) == 2 and texts[0].startswith("Model fallback:")
    assert texts[1].startswith("Main model restored")
    assert agent.binding.state.model == primary.model


def test_content_filter_refusal_falls_back(scope: Scope, fake_clock: FakeClock) -> None:
    primary = FakeModelServer(script_of(Turn((Finish("content_filter"),))), clock=fake_clock)
    backup = Backup(fake_clock, text_turn("answered elsewhere"))
    events: list[EventBase] = []

    async def main() -> Any:
        agent = await build(scope, primary, backup, events)
        result = await agent.run_turn("hello")
        await agent.close()
        return result

    result = asyncio.run(run_driven(main(), fake_clock))
    assert result.final_text == "answered elsewhere" and backup.chat.call_count == 1
    assert "content filter" in notices(events)[0]


def test_transient_errors_fall_back_after_retries(scope: Scope, fake_clock: FakeClock) -> None:
    down = Turn((Fail(503, '{"error": {"message": "service unavailable"}}'),))
    primary = FakeModelServer(script_of(down, down), clock=fake_clock)
    backup = Backup(fake_clock, text_turn("still here"))
    events: list[EventBase] = []

    async def main() -> Any:
        agent = await build(scope, primary, backup, events)
        result = await agent.run_turn("hello")
        await agent.close()
        return result

    result = asyncio.run(run_driven(main(), fake_clock))
    assert len(primary.model_requests) == 2  # P03's retries ran first (max_retries=1)
    assert result.final_text == "still here"


def test_without_a_chain_the_turn_fails_as_before(scope: Scope, fake_clock: FakeClock) -> None:
    primary = FakeModelServer(
        script_of(Turn((Fail(401, '{"error": {"message": "invalid api key"}}'),))),
        clock=fake_clock,
    )
    events: list[EventBase] = []

    async def main() -> Any:
        agent = await Agent.start(
            scope, fake_route(primary), [], fake_options(primary), events.append
        )
        result = await agent.run_turn("hello")
        await agent.close()
        return result

    result = asyncio.run(run_driven(main(), fake_clock))
    assert result.exit_reason == ExitReason.PROVIDER_FAILED and notices(events) == []


def test_pool_rotates_keys_before_the_chain_falls_back_and_gates_the_restore(
    scope: Scope, fake_clock: FakeClock
) -> None:
    profile = ProviderProfile(
        name="fakevendor",
        base_url=CHAT_BASE_URL,
        hostname=FAKE_HOST,
        env_vars=("FAKEVENDOR_API_KEY",),
    )
    keys = {"ida": "test-key-a-0000", "idb": "test-key-b-0000"}
    assert scope.secrets is not None
    for ident, value in keys.items():
        scope.secrets.set(secret_name("fakevendor", ident), value)  # type: ignore[attr-defined]

    def seed(pool: Pool, _doc: dict[str, Any]) -> None:
        for ident in keys:
            pool.add(PoolEntry(id=ident, label=ident))

    store = AuthStore(scope.home, env=scope.env)
    edit_pool(store, "fakevendor", seed)
    spent = Turn((Fail(402, '{"error": {"message": "insufficient credits"}}'),))
    primary = FakeModelServer(script_of(spent, spent), clock=fake_clock)
    backup = Backup(fake_clock, text_turn("backup one"), text_turn("backup two"))
    events: list[EventBase] = []

    def factory(target: RuntimeRoute) -> httpx.AsyncClient:
        return backup.client() if target.host == BACKUP_HOST else primary.client()

    options = replace(fake_options(primary, max_retries=1), client_factory=factory)
    vendor_route = replace(fake_route(primary), provider="fakevendor", profile=profile)

    async def main() -> list[Any]:
        agent = await Agent.start(agent_scope(scope), vendor_route, [], options, events.append)
        first = await agent.run_turn("hello")
        second = await agent.run_turn("again")
        await agent.close()
        return [first, second]

    first, second = asyncio.run(run_driven(main(), fake_clock))
    sent = [r.headers.get("authorization") for r in primary.model_requests]
    assert sent == ["Bearer test-key-a-0000", "Bearer test-key-b-0000"]  # pool first
    assert first.final_text == "backup one"  # then the chain
    assert second.final_text == "backup two"  # every key cools down: no restore yet
    rows, _ = load_pool(store, "fakevendor")
    assert {e.status for e in rows.entries} == {"exhausted"}
    assert len(notices(events)) == 1
