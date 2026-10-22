"""P24-07: peer gateways, the receiving bot routes, local delivery and the ``self-chat`` target."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from indus.cron import delivery as cron_delivery
from indus.cron.delivery import DeliveryRequest
from indus.cron.targets import DeliveryTarget, is_known_platform, parse_targets
from indus.foundation.scope import Scope
from indus.foundation.secrets import SecretStore
from indus.gateway.delivery.router import DeliveryRouter
from indus.gateway.http.serving import BadRequest, BearerGate
from indus.gateway.rooms.peers import (
    CHAT_PATH,
    DM_PATH,
    SELF_CHAT,
    LocalBotTransport,
    OneShotResult,
    bots_router,
    deliver_self_chat,
)
from indus.service.peers import (
    PEERS_FILE,
    RELAY_CAP_S,
    Peer,
    PeerBook,
    PeerClient,
    PeerError,
    PeerTarget,
    key_name,
    parse_target,
    peer_key,
)
from indus.state import messages, registry
from indus.state.bot_chat import CANONICAL_TITLE, find_canonical
from indus.tools.builtin.message_agent import BotDelivery, BotTarget
from tests import self_chat_guard
from tests.contract.gateway._http_kit import build_api, within
from tests.fakes.clock import FakeClock
from tests.fakes.model import text_turn

PUBLIC = "93.184.216.34"


def _fake_key() -> str:
    return "peer-" + "k" * 30  # built at runtime: never a credential-shaped literal


class Recorder(httpx.AsyncBaseTransport):
    """A transport that records requests and answers from ``reply``."""

    def __init__(self, reply: Callable[[httpx.Request], httpx.Response]) -> None:
        self.reply = reply
        self.seen: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.seen.append(request)
        return self.reply(request)


def _json(status: int, body: object) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _req: httpx.Response(status, json=body)


def _resolver(host: str, port: int | None) -> list[str]:
    return [PUBLIC]


def _add(
    scope: Scope, name: str = "spark", url: str = "http://spark.example:8642", **kw: Any
) -> None:
    PeerBook(scope).put(Peer(name, url, **kw))


def _store_key(scope: Scope, name: str = "spark") -> str:
    key = _fake_key()
    assert isinstance(scope.secrets, SecretStore)
    scope.secrets.set(key_name(name), key)
    return key


# -- the registry -------------------------------------------------------------------------------


def test_registry_file_is_owner_only_and_holds_no_key(scope: Scope) -> None:
    _add(scope, note="the lab box")
    key = _store_key(scope)
    path = scope.home / PEERS_FILE
    assert path.stat().st_mode & 0o077 == 0
    text = path.read_text(encoding="utf-8")
    assert key not in text and "spark.example" in text
    assert PeerBook(scope).get("spark") == Peer(
        "spark", "http://spark.example:8642", False, "the lab box"
    )
    assert PeerBook(scope).remove("spark") and not PeerBook(scope).remove("spark")


@pytest.mark.parametrize(
    "url",
    ["ftp://host", "http://user:pw@host:1", "http://host/?token=1", "not a url", "http:///x"],
)
def test_bad_peer_urls_are_refused(scope: Scope, url: str) -> None:
    with pytest.raises(PeerError):
        _add(scope, url=url)


def test_key_comes_from_the_store_or_the_env_name(scope: Scope) -> None:
    assert key_name("my-lab") == "INDUS_PEER_MY_LAB_KEY"
    assert peer_key(scope, "spark") is None
    env_scope = scope.with_env(scope.env.with_overlay({key_name("spark"): "from-env-value"}))
    assert peer_key(env_scope, "spark") == "from-env-value"
    key = _store_key(scope)
    assert peer_key(scope, "spark") == key


def test_target_forms() -> None:
    assert parse_target("spark") == PeerTarget("spark")
    assert parse_target("Spark/researcher") == PeerTarget("spark", "researcher")
    with pytest.raises(PeerError):
        parse_target("spark/bad name")


# -- the client ---------------------------------------------------------------------------------


def test_peer_without_a_key_is_refused_before_any_request(scope: Scope) -> None:
    _add(scope)
    transport = Recorder(_json(200, {"session_id": "s", "reply": "x"}))
    client = PeerClient(scope, transport=transport, resolver=_resolver)
    with pytest.raises(PeerError) as caught:
        asyncio.run(client.dm(PeerTarget("spark"), "hello"))
    assert caught.value.reason == "missing_config"
    assert "--key-stdin" in caught.value.message
    assert transport.seen == []


def test_unknown_peer_is_missing_config(scope: Scope) -> None:
    client = PeerClient(scope, transport=Recorder(_json(200, {})), resolver=_resolver)
    with pytest.raises(PeerError) as caught:
        asyncio.run(client.dm(PeerTarget("ghost"), "hello"))
    assert caught.value.reason == "missing_config"


def test_dm_sends_the_key_and_reads_the_reply(scope: Scope) -> None:
    _add(scope)
    key = _store_key(scope)
    transport = Recorder(_json(200, {"session_id": "s1", "reply": "pong"}))
    client = PeerClient(scope, transport=transport, resolver=_resolver)
    got = asyncio.run(client.dm(PeerTarget("spark"), "ping", author="work"))
    assert (got.session_id, got.reply) == ("s1", "pong")
    (request,) = transport.seen
    assert request.headers["authorization"] == f"Bearer {key}"
    assert request.url.path == DM_PATH
    assert json.loads(request.content) == {"message": "ping", "author": "work"}
    assert key not in str(request.url)


def test_profile_target_uses_the_profile_prefix(scope: Scope) -> None:
    _add(scope)
    _store_key(scope)
    transport = Recorder(_json(200, {"session_id": "s1", "reply": "ok"}))
    client = PeerClient(scope, transport=transport, resolver=_resolver)
    asyncio.run(client.dm(PeerTarget("spark", "researcher"), "hi"))
    assert transport.seen[0].url.path == "/p/researcher" + DM_PATH


def test_a_private_address_needs_the_lan_flag(scope: Scope) -> None:
    _add(scope, url="http://10.0.0.5:8642")
    _store_key(scope)
    transport = Recorder(_json(200, {"session_id": "s", "reply": "ok"}))
    client = PeerClient(scope, transport=transport, resolver=_resolver)
    with pytest.raises(PeerError) as caught:
        asyncio.run(client.dm(PeerTarget("spark"), "hi"))
    assert caught.value.reason == "missing_config" and "--lan" in caught.value.message
    assert transport.seen == []
    _add(scope, url="http://10.0.0.5:8642", lan=True)
    assert asyncio.run(client.dm(PeerTarget("spark"), "hi")).reply == "ok"
    assert len(transport.seen) == 1


def test_a_redirect_is_not_followed(scope: Scope) -> None:
    _add(scope)
    key = _store_key(scope)
    transport = Recorder(
        lambda _req: httpx.Response(302, headers={"location": "http://elsewhere.example/steal"})
    )
    client = PeerClient(scope, transport=transport, resolver=_resolver)
    with pytest.raises(PeerError) as caught:
        asyncio.run(client.dm(PeerTarget("spark"), "hi"))
    assert "redirect" in caught.value.message
    assert len(transport.seen) == 1 and key not in caught.value.message


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (401, {"error": {"message": "nope"}}, "provider_auth"),
        (403, {}, "provider_auth"),
        (404, {}, "missing_config"),
        (409, {}, "target_busy"),
        (429, {}, "provider_rate_limit"),
        (503, {}, "provider_server"),
        (504, {"error": {"reason": "delivery_timeout", "accepted": True}}, "delivery_timeout"),
        (502, {"error": {"reason": "context_overflow", "message": "too long"}}, "context_overflow"),
    ],
)
def test_status_maps_to_a_typed_reason_without_the_key(
    scope: Scope, status: int, body: object, reason: str
) -> None:
    _add(scope)
    key = _store_key(scope)
    client = PeerClient(scope, transport=Recorder(_json(status, body)), resolver=_resolver)
    with pytest.raises(PeerError) as caught:
        asyncio.run(client.dm(PeerTarget("spark"), "hi"))
    assert caught.value.reason == reason
    assert key not in caught.value.message and key not in repr(caught.value)


def test_a_read_timeout_says_do_not_resend(scope: Scope) -> None:
    _add(scope)
    _store_key(scope)

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client = PeerClient(scope, transport=Recorder(slow), resolver=_resolver)
    with pytest.raises(PeerError) as caught:
        asyncio.run(client.dm(PeerTarget("spark"), "hi"))
    assert caught.value.reason == "delivery_timeout" and caught.value.accepted
    assert "do not resend" in caught.value.message


def test_a_connect_failure_is_runtime_offline(scope: Scope) -> None:
    _add(scope)
    _store_key(scope)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = PeerClient(scope, transport=Recorder(down), resolver=_resolver)
    with pytest.raises(PeerError) as caught:
        asyncio.run(client.dm(PeerTarget("spark"), "hi"))
    assert caught.value.reason == "runtime_offline"


def test_run_status_and_stop_use_the_canonical_chat(scope: Scope) -> None:
    _add(scope)
    _store_key(scope)

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == CHAT_PATH:
            return httpx.Response(200, json={"session_id": "bot-chat-1"})
        if request.url.path == "/v1/runs":
            body = json.loads(request.content)
            assert body == {"input": "long job", "session_id": "bot-chat-1"}
            return httpx.Response(202, json={"run_id": "run_1", "status": "started"})
        if request.url.path.endswith("/stop"):
            return httpx.Response(200, json={"status": "stopping"})
        return httpx.Response(200, json={"status": "completed", "output": "done"})

    transport = Recorder(answer)
    client = PeerClient(scope, transport=transport, resolver=_resolver)
    started = asyncio.run(client.run(PeerTarget("spark"), "long job"))
    assert started == {"run_id": "run_1", "session_id": "bot-chat-1", "status": "started"}
    assert asyncio.run(client.status(PeerTarget("spark"), "run_1"))["output"] == "done"
    assert asyncio.run(client.stop(PeerTarget("spark"), "run_1"))["status"] == "stopping"


# -- the receiving routes, end to end over the real API server pieces ---------------------------


def _bots_app(kit: Any) -> Any:
    app = FastAPI()

    async def bad(_request: Request, exc: Exception) -> Response:
        assert isinstance(exc, BadRequest)
        return JSONResponse(exc.body(), status_code=exc.status)

    app.add_exception_handler(BadRequest, bad)
    app.include_router(bots_router(kit.ctx))
    return BearerGate(app, kit.ctx.keys.verify)


def test_peer_dm_reaches_the_canonical_chat_of_the_peer(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_api(scope, fake_clock, tmp_path, text_turn("pong"), text_turn("NO_REPLY"))
        try:
            app = _bots_app(kit)
            _add(scope)
            assert isinstance(scope.secrets, SecretStore)
            scope.secrets.set(key_name("spark"), kit.key)
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))  # pyright: ignore[reportArgumentType]
            client = PeerClient(scope, transport=transport, resolver=_resolver)
            target = PeerTarget("spark")
            first = await within(client.ensure_chat(target))
            assert first == await within(client.ensure_chat(target))  # one chat, found again
            reply = await within(client.dm(target, "Message from bot @work:\n\nping"))
            assert (reply.session_id, reply.reply) == (first, "pong")
            silent = await within(client.dm(target, "anything to add?"))
            assert silent.reply == ""  # a silence token is a decision, not a message
            assert find_canonical(kit.store) == first
            texts = [m["content"] for m in messages.get(kit.store, first) if m["role"] == "user"]
            assert texts[0].endswith("ping")
        finally:
            await kit.close()

    asyncio.run(main())


def test_the_bot_routes_need_the_key(scope: Scope, fake_clock: FakeClock, tmp_path: Path) -> None:
    async def main() -> None:
        kit = await build_api(scope, fake_clock, tmp_path)
        try:
            transport = httpx.ASGITransport(app=_bots_app(kit), client=("127.0.0.1", 50000))  # pyright: ignore[reportArgumentType]
            async with httpx.AsyncClient(transport=transport, base_url="http://x") as plain:
                assert (await plain.post(CHAT_PATH, json={})).status_code == 401
                headers = {"Authorization": f"Bearer {kit.key}"}
                bad = await plain.post(DM_PATH, json={"message": "  "}, headers=headers)
                assert bad.status_code == 400
                extra = await plain.post(DM_PATH, json={"message": "x", "x": 1}, headers=headers)
                assert extra.status_code == 400
        finally:
            await kit.close()

    asyncio.run(main())


# -- local delivery -----------------------------------------------------------------------------


@pytest.fixture
def bob(scope: Scope) -> Iterator[Path]:
    home = scope.root / "profiles" / "bob"
    home.mkdir(parents=True)
    (home / "profile.yaml").touch()
    yield home
    registry.close_all_under(home)


class Runner:
    def __init__(self, result: OneShotResult) -> None:
        self.result = result
        self.calls: list[tuple[list[str], dict[str, str], float]] = []

    async def __call__(
        self, argv: list[str], env: dict[str, str], report: Path, cap: float
    ) -> OneShotResult:
        self.calls.append((argv, env, cap))
        return self.result


def _delivery(name: str, profile: str | None = None) -> BotDelivery:
    return BotDelivery("work", BotTarget(name, profile), "Message from bot @work:\n\nhi")


def test_local_delivery_runs_one_turn_in_the_targets_canonical_chat(
    scope: Scope, bob: Path
) -> None:
    runner = Runner(OneShotResult(0, "hello back\n"))
    transport = LocalBotTransport(scope, runner=runner, python="python3")
    outcome = asyncio.run(transport.deliver(_delivery("bob")))
    assert (outcome.ok, outcome.reply) == (True, "hello back")
    ((argv, env, cap),) = runner.calls
    with registry.shared(transport._target_scope("bob")) as store:  # pyright: ignore[reportPrivateUsage]
        chat = find_canonical(store)
    assert chat is not None and outcome.session_id == chat
    assert argv[:7] == ["python3", "-m", "indus", "-p", "bob", "-z", _delivery("bob").message]
    assert argv[argv.index("--resume") + 1] == chat
    assert env["INDUS_HOME"] == str(bob) and cap == RELAY_CAP_S
    assert not any(k.startswith("INDUS_PEER_") for k in env)
    again = LocalBotTransport(scope, runner=Runner(OneShotResult(0, "x")), python="python3")
    asyncio.run(again.deliver(_delivery("bob")))
    with registry.shared(transport._target_scope("bob")) as store:  # pyright: ignore[reportPrivateUsage]
        assert find_canonical(store) == chat  # the same chat, never a second one
        with store.read() as conn:
            titled = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE title = ?", (CANONICAL_TITLE,)
            ).fetchone()[0]
    assert titled == 1


@pytest.mark.parametrize(
    ("result", "ok", "reason"),
    [
        (OneShotResult(None, ""), False, "delivery_timeout"),
        (OneShotResult(1, "", {"completed": True}), True, None),
        (OneShotResult(1, ""), False, "runtime_offline"),
        (OneShotResult(2, "", {"failure": "429 rate limit reached"}), False, "provider_rate_limit"),
        (OneShotResult(2, "", {"failure": "401 invalid api key"}), False, "provider_auth"),
        (OneShotResult(3, ""), False, "target_busy"),
        (OneShotResult(0, "[SILENT]"), True, None),
    ],
)
def test_local_exit_codes_map_to_outcomes(
    scope: Scope, bob: Path, result: OneShotResult, ok: bool, reason: str | None
) -> None:
    transport = LocalBotTransport(scope, runner=Runner(result), python="python3")
    outcome = asyncio.run(transport.deliver(_delivery("bob")))
    assert (outcome.ok, outcome.reason) == (ok, reason)
    if ok:
        assert outcome.reply == ""


def test_unknown_names_fall_back_to_a_peer_then_refuse(scope: Scope) -> None:
    runner = Runner(OneShotResult(0, "never"))
    _add(scope)
    _store_key(scope)
    peers = PeerClient(
        scope, transport=Recorder(_json(200, {"session_id": "r", "reply": "from spark"})),
        resolver=_resolver,
    )  # fmt: skip
    transport = LocalBotTransport(scope, peers=peers, runner=runner, python="python3")
    assert asyncio.run(transport.deliver(_delivery("spark"))).reply == "from spark"
    missing = asyncio.run(transport.deliver(_delivery("nobody")))
    assert (missing.ok, missing.reason) == (False, "missing_config")
    itself = asyncio.run(transport.deliver(_delivery("work")))
    assert itself.reason == "missing_config"
    no_key = asyncio.run(transport.deliver(_delivery("other", "bob")))
    assert no_key.reason == "missing_config"
    assert runner.calls == []


def test_peer_without_a_key_fails_as_missing_config(scope: Scope) -> None:
    _add(scope)
    transport = LocalBotTransport(scope, runner=Runner(OneShotResult(0, "")), python="python3")
    outcome = asyncio.run(transport.deliver(_delivery("spark", "researcher")))
    assert (outcome.ok, outcome.reason) == (False, "missing_config")


def test_the_one_shot_runner_stops_a_turn_past_the_cap(scope: Scope, tmp_path: Path) -> None:
    from indus.gateway.rooms.peers import run_oneshot

    async def instant(_seconds: float) -> None:
        return None

    report = tmp_path / "r.json"
    result = asyncio.run(
        run_oneshot(["sleep", "30"], {"PATH": "/usr/bin:/bin"}, report, 0.0, sleep=instant)
    )
    assert result.code is None


# -- the self-chat cron target ------------------------------------------------------------------


def _request(scope: Scope, chat: str | None = None) -> DeliveryRequest:
    return DeliveryRequest(
        scope=scope,
        job_id="j1",
        job_name="nightly",
        target=DeliveryTarget(SELF_CHAT, chat),
        text="[nightly] all green",
        plain_text="all green",
    )


def test_self_chat_cron_target_lands_as_an_incoming_message(scope: Scope) -> None:
    receipt = asyncio.run(deliver_self_chat(_request(scope)))
    assert receipt.ok and receipt.message_id
    with registry.shared(scope) as store:
        chat = find_canonical(store)
        assert chat is not None
        rows = messages.get(store, chat)
    assert [(r["role"], r["content"]) for r in rows] == [("user", "[nightly] all green")]
    other = asyncio.run(deliver_self_chat(_request(scope, "someone-else")))
    assert not other.ok and "own chat" in (other.error or "")


def _holders() -> int:
    from indus.service import self_chat

    return self_chat._users  # pyright: ignore[reportPrivateUsage]


def test_the_router_registers_self_chat_while_it_lives(scope: Scope) -> None:
    assert is_known_platform(SELF_CHAT)
    parsed = parse_targets("self-chat")
    assert parsed.ok and parsed.targets[0].platform == SELF_CHAT
    before = _holders()  # routers other tests left open still hold it
    router = DeliveryRouter(scope, homes=lambda _p: None)
    try:
        assert cron_delivery.deliverer_for(SELF_CHAT) is not None
        assert _holders() == before + 1
    finally:
        router.close()
    assert _holders() == before
    assert (cron_delivery.deliverer_for(SELF_CHAT) is None) == (before == 0)


def test_two_routers_share_the_self_chat_deliverer(scope: Scope) -> None:
    before = _holders()
    first = DeliveryRouter(scope, homes=lambda _p: None)
    second = DeliveryRouter(scope, homes=lambda _p: None)
    first.close()
    assert cron_delivery.deliverer_for(SELF_CHAT) is not None  # the second still needs it
    first.close()  # a second close changes nothing
    assert _holders() == before + 1
    second.close()
    assert _holders() == before


def test_a_router_left_open_is_found_and_released(scope: Scope) -> None:
    """Wave 42: a router never closed kept ``self-chat`` registered for the rest of the run, so
    ``test_a_cli_pass_can_deliver_to_the_own_bot_chat`` failed after the gateway tests. The root
    conftest finds such a leak after each test and gives the holds back."""
    before = self_chat_guard.holders()
    DeliveryRouter(scope, homes=lambda _p: None)  # never closed
    assert self_chat_guard.holders() == before + 1
    assert cron_delivery.deliverer_for(SELF_CHAT) is not None
    assert self_chat_guard.release_leaked(before) == 1
    assert self_chat_guard.holders() == before
    assert (cron_delivery.deliverer_for(SELF_CHAT) is None) == (before == 0)
    assert self_chat_guard.release_leaked(before) == 0


def test_the_api_server_itself_serves_the_bot_routes(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    """Wave 42: ``build_api_app`` (the daemon's API server) mounts ``bots_router`` behind its own
    bearer gate, so a peer reaches ``/v1/bots/*`` on a real gateway."""

    async def main() -> None:
        kit = await build_api(scope, fake_clock, tmp_path, text_turn("pong"))
        try:
            async with kit.client(key=None) as plain:
                assert (await plain.post(CHAT_PATH, json={})).status_code == 401
            async with kit.client() as client:
                chat = await within(client.post(CHAT_PATH, json={}))
                assert chat.status_code == 200
                session = chat.json()["session_id"]
                dm = await within(client.post(DM_PATH, json={"message": "ping", "author": "w"}))
                assert dm.status_code == 200
                assert (dm.json()["session_id"], dm.json()["reply"]) == (session, "pong")
        finally:
            await kit.close()

    asyncio.run(main())
