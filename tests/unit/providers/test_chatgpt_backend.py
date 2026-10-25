"""The ``openai-codex`` route (D-16.1282): profile, request shape, streaming, model list, renewal.

A fake Codex backend and a fake sign-in service (``tests.fakes.chatgpt_auth``) stand in for OpenAI;
no request leaves the process.
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from indus.foundation.clock import SystemClock
from indus.foundation.errors import KeyHostMismatch, NoUsableProvider
from indus.foundation.scope import Scope
from indus.providers import bundled, model_list
from indus.providers.auth import chatgpt, renew
from indus.providers.auth.device_code import (
    DeviceFlowSpec,
    RefreshThrottled,
    ReloginRequired,
    TokenSet,
)
from indus.providers.auth.pool_model import Pool, PoolEntry, edit_pool, load_pool, secret_name
from indus.providers.auth.store import AuthStore
from indus.providers.pool import Lease, PoolRuntime
from indus.providers.profile import USER_AGENT
from indus.providers.route import RuntimeRoute, Secret, resolve
from indus.providers.streaming import StreamFlags, stream_call
from indus.providers.timeouts import TimeoutPolicy
from indus.providers.transports import get_transport
from indus.providers.types import ChatRequest
from tests.fakes import chatgpt_auth as fake
from tests.fakes.clock import FakeClock
from tests.fakes.secret_store import MemorySecretStore
from tests.unit.providers.conftest import ScopeFactory

NOW = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
PROFILE = bundled.by_name()["openai-codex"]
BASE = PROFILE.base_url
REQ = ChatRequest(
    model="gpt-5.5",
    messages=[{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}],
    max_tokens=500,
    temperature=0.2,
    stream=False,
)
REFRESH_SPEC = DeviceFlowSpec(
    provider="openai-codex",
    device_authorization_url="",
    token_url=f"{fake.ISSUER}/oauth/token",
    client_id="client-under-test",
)


def _route(token: str | None = None) -> RuntimeRoute:
    return RuntimeRoute(
        provider=PROFILE.name,
        model="gpt-5.5",
        api_mode=PROFILE.api_mode,
        base_url=BASE,
        api_key=Secret(token or fake.fake_jwt()),
        headers={"User-Agent": USER_AGENT, **PROFILE.default_headers},
        profile=PROFILE,
    )


# -- the profile -----------------------------------------------------------------------------------


def test_profile_is_a_sign_in_only_vendor_row_on_chatgpt_com() -> None:
    assert PROFILE.signs_in and PROFILE.needs_key and not PROFILE.env_vars
    assert PROFILE.hostname == "chatgpt.com" and PROFILE.stream_only
    assert PROFILE.api_mode == "responses" and PROFILE.family == "vendor"
    assert PROFILE.default_models and "gpt-5.5" in PROFILE.default_models
    assert bundled.by_name()["openai-codex"] is PROFILE
    assert PROFILE.label == "OpenAI (ChatGPT sign-in)"


def test_resolve_without_a_sign_in_says_how_to_sign_in(make_scope: ScopeFactory) -> None:
    scope = make_scope(config={"model": {"provider": "openai-codex", "default": "gpt-5.5"}})
    with pytest.raises(NoUsableProvider) as caught:
        resolve(scope)
    assert "not signed in" in str(caught.value)
    assert caught.value.hint == "sign in with `indus auth add openai-codex`"


def _sign_in(scope: Scope, token: str, *, expires: datetime | None = None) -> PoolEntry:
    entry = PoolEntry(id="c0de0001", label="sign-in-1", auth_type="oauth", source="pkce")
    tokens = TokenSet(token, NOW, refresh_token=fake.fake_refresh(1), expires_at=expires)
    assert scope.secrets is not None
    scope.secrets.set(secret_name("openai-codex", entry.id), tokens.to_secret())  # pyright: ignore[reportAttributeAccessIssue]
    edit_pool(AuthStore(scope.home, env=scope.env), "openai-codex", lambda p, _d: p.add(entry))
    return entry


def test_resolve_with_a_sign_in_uses_its_access_token(make_scope: ScopeFactory) -> None:
    scope = make_scope(config={"model": {"provider": "chatgpt", "default": "gpt-5.5"}})
    token = fake.fake_jwt(7)
    _sign_in(scope, token)
    route = resolve(scope)
    assert route.provider == "openai-codex" and route.base_url == BASE
    assert route.api_key is not None and route.api_key.reveal() == token
    assert route.headers["originator"] == "indus-agent"
    assert route.headers["User-Agent"].startswith("indus/")
    assert token not in repr(route)


def test_a_token_that_happens_to_contain_xxx_is_still_used(make_scope: ScopeFactory) -> None:
    scope = make_scope(config={"model": {"provider": "openai-codex", "default": "gpt-5.5"}})
    token = fake.fake_jwt(9) + "XxX"
    _sign_in(scope, token)
    route = resolve(scope)
    assert route.api_key is not None and route.api_key.reveal() == token


def test_the_token_never_follows_a_saved_url_to_another_host(make_scope: ScopeFactory) -> None:
    scope = make_scope(
        config={
            "model": {
                "provider": "openai-codex",
                "default": "gpt-5.5",
                "base_url": "https://proxy.example.test/v1",
            }
        }
    )
    _sign_in(scope, fake.fake_jwt(8))
    with pytest.raises(KeyHostMismatch) as caught:
        resolve(scope)
    assert fake.fake_jwt(8) not in str(caught.value)


# -- the request -----------------------------------------------------------------------------------


def test_request_is_streamed_unstored_and_carries_the_account_header() -> None:
    token = fake.fake_jwt(account="acct-77")
    request = get_transport("responses").build_request(_route(token), REQ)
    assert str(request.url) == "https://chatgpt.com/backend-api/codex/responses"
    assert request.headers["authorization"] == f"Bearer {token}"
    assert request.headers["ChatGPT-Account-ID"] == "acct-77"
    assert request.headers["originator"] == "indus-agent"
    assert request.headers["accept"] == "text/event-stream"
    assert request.headers["User-Agent"] == USER_AGENT
    body = json.loads(request.content)
    assert body["stream"] is True and body["store"] is False
    assert body["instructions"] == "be brief"
    assert "max_output_tokens" not in body and "temperature" not in body
    assert body["input"][0]["role"] == "user"


def test_instructions_are_always_present() -> None:
    req = replace(REQ, messages=[{"role": "user", "content": "hi"}])
    body = json.loads(get_transport("responses").build_request(_route(), req).content)
    assert body["instructions"] == chatgpt.DEFAULT_INSTRUCTIONS


def test_effort_is_clamped_to_the_profile_ladder() -> None:
    req = replace(REQ, reasoning_effort="minimal")
    body = json.loads(get_transport("responses").build_request(_route(), req).content)
    assert body["reasoning"]["effort"] == "low"
    assert body["include"] == ["reasoning.encrypted_content"]


def test_other_responses_routes_keep_their_shape() -> None:
    plain = RuntimeRoute(
        provider="custom",
        model="m",
        api_mode="responses",
        base_url="https://llm.example.test/v1",
        api_key=Secret(fake.fake_jwt()),
    )
    request = get_transport("responses").build_request(plain, REQ)
    body = json.loads(request.content)
    assert body["stream"] is False and body["max_output_tokens"] == 500
    assert "ChatGPT-Account-ID" not in request.headers


def test_a_non_streaming_call_still_streams_to_the_backend() -> None:
    backend = fake.FakeBackend()
    flags = StreamFlags(streaming=False)

    async def go() -> Any:
        async with backend.client() as client:
            return await stream_call(
                client,
                get_transport("responses"),
                _route(),
                REQ,
                policy=TimeoutPolicy(),
                clock=SystemClock(),
                on_event=lambda _e: None,
                route_epoch=lambda: 0,
                flags=flags,
            )

    outcome = asyncio.run(go())
    assert outcome.response.content == "hello"
    assert outcome.streamed and flags.streaming
    assert len(backend.requests) == 1


def test_a_refusal_is_reported_without_the_token() -> None:
    backend = fake.FakeBackend(status=401)
    token = fake.fake_jwt(5)

    async def go() -> Any:
        async with backend.client() as client:
            return await stream_call(
                client,
                get_transport("responses"),
                _route(token),
                REQ,
                policy=TimeoutPolicy(),
                clock=SystemClock(),
                on_event=lambda _e: None,
                route_epoch=lambda: 0,
            )

    with pytest.raises(Exception) as caught:
        asyncio.run(go())
    assert token not in str(caught.value) and token not in repr(caught.value)


# -- the model list --------------------------------------------------------------------------------


def test_catalogue_shape_is_read_hidden_dropped_and_ranked() -> None:
    payload = {
        "models": [
            {"slug": "gpt-b", "priority": 2},
            {"slug": "gpt-hidden", "visibility": "hide", "priority": 0},
            {"slug": "gpt-a", "priority": 1, "visibility": "list"},
            {"slug": "gpt-c"},
        ]
    }
    assert [m.id for m in model_list.parse_models(payload)] == ["gpt-a", "gpt-b", "gpt-c"]


def test_list_models_asks_with_the_account_header_and_client_version() -> None:
    backend = fake.FakeBackend(catalogue={"models": [{"slug": "gpt-x", "priority": 1}]})
    token = fake.fake_jwt(account="acct-9")

    async def go() -> list[str]:
        async with backend.client() as client:
            return [m.id for m in await model_list.list_models(_route(token), client=client)]

    assert asyncio.run(go()) == ["gpt-x"]
    sent = backend.requests[0]
    assert sent.url.path == "/backend-api/codex/models"
    assert sent.url.params["client_version"]
    assert sent.headers["ChatGPT-Account-ID"] == "acct-9"


def test_list_models_falls_back_to_the_documented_list() -> None:
    for backend in (fake.FakeBackend(status=401), fake.FakeBackend(catalogue={"models": []})):

        async def go(b: fake.FakeBackend = backend) -> list[str]:
            async with b.client() as client:
                return [m.id for m in await model_list.list_models(_route(), client=client)]

        assert asyncio.run(go()) == list(PROFILE.default_models)


# -- renewal ---------------------------------------------------------------------------------------


class Stored:
    def __init__(self, tmp_path: Path, *, expires: datetime | None = None) -> None:
        self.home = tmp_path / "home"
        self.home.mkdir(exist_ok=True)
        self.secrets = MemorySecretStore()
        self.entry = PoolEntry(id="c0de0002", label="s", auth_type="oauth", source="device_code")
        self.tokens = TokenSet(
            fake.fake_jwt(1), NOW, refresh_token=fake.fake_refresh(1), expires_at=expires
        )
        self.secrets.set(secret_name("openai-codex", self.entry.id), self.tokens.to_secret())
        edit_pool(AuthStore(self.home), "openai-codex", lambda p, _d: p.add(self.entry))

    def renew(self, server: fake.FakeSignIn, seen: str | None) -> TokenSet | None:
        return renew.renew_stored(
            home=self.home,
            secrets=self.secrets,
            provider="openai-codex",
            entry_id=self.entry.id,
            spec=REFRESH_SPEC,
            clock=FakeClock(NOW),
            seen_access=seen,
            make_client=server.factory(),
        )

    def current(self) -> TokenSet:
        stored = self.secrets.get(secret_name("openai-codex", self.entry.id))
        assert stored is not None
        return TokenSet.from_secret(stored)


def test_renewal_stores_the_new_pair_and_the_row_expiry(tmp_path: Path) -> None:
    held = Stored(tmp_path)
    server = fake.FakeSignIn(token=[fake.tokens_answer(2, expires_in=900)])
    fresh = held.renew(server, held.tokens.access_token)
    assert fresh is not None and fresh.access_token == fake.fake_jwt(2)
    assert held.current().refresh_token == fake.fake_refresh(2)
    assert server.token_forms == [
        {
            "grant_type": "refresh_token",
            "refresh_token": fake.fake_refresh(1),
            "client_id": "client-under-test",
        }
    ]
    pool, _ = load_pool(AuthStore(held.home), "openai-codex")
    assert pool.entries[0].expires_at == (NOW + timedelta(seconds=900)).isoformat()
    assert fake.fake_jwt(2) not in (held.home / "auth.json").read_text()


def test_a_pair_renewed_elsewhere_is_adopted_without_a_request(tmp_path: Path) -> None:
    held = Stored(tmp_path)
    server = fake.FakeSignIn()
    got = held.renew(server, seen=fake.fake_jwt(99))  # the caller held an older token
    assert got is not None and got.access_token == held.tokens.access_token
    assert server.requests == []


def test_two_sessions_renewing_at_once_spend_the_refresh_token_once(tmp_path: Path) -> None:
    held = Stored(tmp_path)
    server = fake.FakeSignIn(token=[fake.tokens_answer(2)])
    seen = held.tokens.access_token
    results: list[TokenSet | None] = []
    gate = threading.Barrier(2)

    def worker() -> None:
        gate.wait()
        results.append(held.renew(server, seen))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert len(server.token_forms) == 1
    assert [r.access_token if r else None for r in results] == [fake.fake_jwt(2)] * 2


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(400, json={"error": "invalid_grant"}),
        httpx.Response(
            400, json={"error": {"code": "refresh_token_reused", "message": "already used"}}
        ),
        httpx.Response(401, json={"error": {"message": "expired"}}),
    ],
)
def test_a_dead_grant_asks_to_sign_in_again_and_keeps_nothing_new(
    tmp_path: Path, answer: httpx.Response
) -> None:
    held = Stored(tmp_path)
    with pytest.raises(ReloginRequired) as caught:
        held.renew(fake.FakeSignIn(token=[answer]), held.tokens.access_token)
    assert held.current() == held.tokens
    assert fake.fake_refresh(1) not in str(caught.value)


def test_a_throttled_renewal_is_not_a_dead_grant(tmp_path: Path) -> None:
    held = Stored(tmp_path)
    with pytest.raises(RefreshThrottled):
        held.renew(fake.FakeSignIn(token=[httpx.Response(429, json={})]), None)


def test_nothing_stored_means_nothing_to_renew(tmp_path: Path) -> None:
    held = Stored(tmp_path)
    held.secrets.delete(secret_name("openai-codex", held.entry.id))
    assert held.renew(fake.FakeSignIn(), None) is None


# -- renewal ahead of expiry in the pool ----------------------------------------------------------


def _runtime(held: Stored, calls: list[str], clock: FakeClock) -> PoolRuntime:
    async def refresher(provider: str, entry: PoolEntry, stored: str) -> str | None:
        calls.append(entry.id)
        return fake.fake_jwt(3)

    return PoolRuntime(
        PROFILE,
        store=AuthStore(held.home),
        secrets=held.secrets,
        env={},
        clock=clock,
        refresher=refresher,
    )


def test_a_token_about_to_expire_is_renewed_before_the_request(tmp_path: Path) -> None:
    held = Stored(tmp_path, expires=NOW + timedelta(seconds=60))
    calls: list[str] = []
    runtime = _runtime(held, calls, FakeClock(NOW))
    lease = runtime.lease(_route(), "gpt-5.5")
    assert lease is not None
    renewed = asyncio.run(runtime.renewed_ahead(lease))
    assert calls == [held.entry.id] and renewed.key.reveal() == fake.fake_jwt(3)


def test_a_token_with_time_left_is_used_as_it_is(tmp_path: Path) -> None:
    held = Stored(tmp_path, expires=NOW + timedelta(hours=1))
    calls: list[str] = []
    runtime = _runtime(held, calls, FakeClock(NOW))
    lease = runtime.lease(_route(), "gpt-5.5")
    assert lease is not None
    assert asyncio.run(runtime.renewed_ahead(lease)) is lease and calls == []


def test_the_call_loop_sends_the_renewed_token(tmp_path: Path) -> None:
    held = Stored(tmp_path, expires=NOW - timedelta(seconds=1))
    calls: list[str] = []
    runtime = _runtime(held, calls, FakeClock(NOW))
    sent: list[str] = []

    async def run(route: RuntimeRoute, _adjust: Any) -> str:
        assert route.api_key is not None
        sent.append(route.api_key.reveal())
        return "ok"

    assert asyncio.run(runtime.call(_route(), run)) == "ok"
    assert sent == [fake.fake_jwt(3)]


def test_api_key_leases_are_never_touched(tmp_path: Path) -> None:
    held = Stored(tmp_path)
    runtime = _runtime(held, [], FakeClock(NOW))
    lease = Lease("unknown", "x", Secret("k" * 20))
    assert asyncio.run(runtime.renewed_ahead(lease)) is lease


def test_signed_in_rows_show_in_the_store(tmp_path: Path) -> None:
    held = Stored(tmp_path)
    assert AuthStore(held.home).signed_in() == ["openai-codex"]

    def drop(pool: Pool, _doc: dict[str, Any]) -> None:
        pool.entries.clear()

    edit_pool(AuthStore(held.home), "openai-codex", drop)
    assert AuthStore(held.home).signed_in() == []
