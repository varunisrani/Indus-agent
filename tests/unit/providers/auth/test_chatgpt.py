"""ChatGPT sign-in (D-16.1282): claims, the device-code flow, the browser flow's redirect, the row.

Everything runs against ``tests.fakes.chatgpt_auth`` (MockTransport, a fake host, run-time tokens);
nothing reaches OpenAI.
"""

from __future__ import annotations

import asyncio
import json
import socket
import urllib.error
import urllib.request
from collections.abc import Awaitable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from indus.providers.auth import chatgpt, chatgpt_facts, device_signin, pkce
from indus.providers.auth.chatgpt import ChatGptDeviceSpec
from indus.providers.auth.device_code import (
    DeviceAuthorization,
    DeviceCodeExpired,
    DeviceFlowError,
    DeviceFlowSpec,
    SignInServiceDown,
    TokenSet,
)
from indus.providers.auth.flows import flows_for
from tests.fakes import chatgpt_auth as fake
from tests.fakes.clock import FakeClock

START = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)


async def _drive[T](clock: FakeClock, work: Awaitable[T], step: float = 0.5) -> T:
    task = asyncio.ensure_future(work)
    for _ in range(200_000):
        await clock.run_until_idle()
        if task.done():
            return task.result()
        clock.advance(step)
    raise AssertionError("never finished")


def _login(server: fake.FakeSignIn) -> tuple[TokenSet | BaseException, list[str]]:
    clock = server.clock
    assert clock is not None
    lines: list[str] = []

    async def main() -> TokenSet | BaseException:
        async with server.client() as client:
            try:
                return await _drive(
                    clock,
                    device_signin.run_login(
                        fake.DEVICE_SPEC,
                        clock=clock,
                        say=lines.append,
                        open_url=None,
                        client=client,
                    ),
                )
            except BaseException as exc:
                return exc

    return asyncio.run(main()), lines


def _gaps(times: list[float]) -> list[float]:
    return [round(b - a, 3) for a, b in zip(times, times[1:], strict=False)]


def _poll(status: int) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": "deviceauth_pending"}})


# -- claims ----------------------------------------------------------------------------------------


def test_account_headers_and_plan_come_from_the_token_claims() -> None:
    token = fake.fake_jwt(account="acct-42", plan="pro")
    assert chatgpt.account_id(token) == "acct-42"
    assert chatgpt.plan(token) == "pro"
    assert chatgpt.account_headers(token) == {"ChatGPT-Account-ID": "acct-42"}


def test_residency_claim_adds_its_header() -> None:
    claims = {
        chatgpt_facts.AUTH_CLAIM: {"chatgpt_account_id": "a1", "chatgpt_data_residency": "eu"}
    }
    body = fake._b64(claims)  # pyright: ignore[reportPrivateUsage]
    token = f"{fake._b64({'alg': 'none'})}.{body}.sig"  # pyright: ignore[reportPrivateUsage]
    assert chatgpt.account_headers(token) == {
        "ChatGPT-Account-ID": "a1",
        "x-openai-internal-codex-residency": "eu",
    }


@pytest.mark.parametrize("token", ["", "not-a-jwt", "a.b", "a.%%%.c", "a.bm90LWpzb24.c"])
def test_anything_else_gives_no_headers(token: str) -> None:
    assert chatgpt.claims(token) == {}
    assert chatgpt.account_headers(token) == {}
    assert chatgpt.plan(token) is None


def test_a_claim_with_a_line_break_never_becomes_a_header() -> None:
    token = fake.fake_jwt(account="acct\r\nX-Evil: 1")
    assert chatgpt.account_headers(token) == {}


# -- the flows row ---------------------------------------------------------------------------------


def test_the_row_uses_the_public_client_and_its_registered_redirect() -> None:
    flows = flows_for("openai-codex")
    assert flows.oauth_default and flows.pkce is not None
    spec = flows.pkce
    assert spec.client_id == chatgpt_facts.CLIENT_ID and spec.exchange == "token"
    assert spec.authorize_url == "https://auth.openai.com/oauth/authorize"
    assert spec.exchange_url == "https://auth.openai.com/oauth/token"
    assert (spec.fixed_port, spec.redirect_host, spec.callback_path) == (
        1455,
        "localhost",
        "/auth/callback",
    )
    assert isinstance(spec.fallback_device, ChatGptDeviceSpec)
    assert isinstance(flows.device, ChatGptDeviceSpec)
    refresh = device_signin.refresh_spec("openai-codex")
    assert refresh is not None and refresh.token_url == chatgpt_facts.TOKEN_URL
    assert refresh.client_id == chatgpt_facts.CLIENT_ID and refresh.scope == ""
    assert device_signin.refresh_spec("anthropic") is None


def test_the_listener_binds_loopback_while_the_redirect_says_localhost() -> None:
    with pkce.LoopbackServer(path="/auth/callback", redirect_host="localhost") as server:
        assert server.redirect_uri == f"http://localhost:{server.port}/auth/callback"
        assert server._server.server_address[0] == "127.0.0.1"  # pyright: ignore[reportPrivateUsage]
    with pkce.LoopbackServer(redirect_host="evil.example") as other:
        assert other.redirect_uri.startswith("http://127.0.0.1:")


# -- device code -----------------------------------------------------------------------------------


def test_device_sign_in_polls_at_the_server_interval_then_exchanges() -> None:
    clock = FakeClock(START)
    server = fake.FakeSignIn(
        clock, polls=[_poll(403), _poll(404), _poll(403), fake.FakeSignIn().polls[0]]
    )
    tokens, lines = _login(server)
    assert isinstance(tokens, TokenSet), tokens
    assert tokens.access_token == fake.fake_jwt(1)
    assert tokens.refresh_token == fake.fake_refresh(1)
    assert _gaps(server.poll_times) == [5, 5, 5]
    first = server.requests[0]
    assert json.loads(first.content) == {"client_id": "client-under-test"}
    poll = server.requests[1]
    assert json.loads(poll.content) == {"device_auth_id": "dev-auth-1", "user_code": "ABCD-1234"}
    assert server.token_forms == [
        {
            "grant_type": "authorization_code",
            "code": "auth-code-1",
            "redirect_uri": fake.DEVICE_SPEC.redirect_uri,
            "client_id": "client-under-test",
            "code_verifier": "verifier-1" + "v" * 40,
        }
    ]
    text = "\n".join(lines)
    assert "ABCD-1234" in text and fake.DEVICE_SPEC.verify_page in text
    assert "dev-auth-1" not in text  # the device id is never shown


def test_interval_is_never_under_three_seconds() -> None:
    clock = FakeClock(START)
    usercode = httpx.Response(200, json={"device_auth_id": "d", "user_code": "C", "interval": 1})
    server = fake.FakeSignIn(
        clock, usercode=[usercode], polls=[_poll(404), _poll(404), fake.FakeSignIn().polls[0]]
    )
    tokens, _ = _login(server)
    assert isinstance(tokens, TokenSet)
    assert _gaps(server.poll_times) == [3, 3]


def test_usercode_429_waits_retry_after_then_succeeds() -> None:
    clock = FakeClock(START)
    limited = httpx.Response(429, headers={"Retry-After": "7"}, json={"error": "slow"})
    server = fake.FakeSignIn(clock, usercode=[limited, fake.FakeSignIn().usercode[0]])
    tokens, _ = _login(server)
    assert isinstance(tokens, TokenSet)
    assert sum(1 for r in server.requests if r.url.path.endswith("usercode")) == 2


def test_usercode_429_every_time_is_a_service_problem() -> None:
    clock = FakeClock(START)
    server = fake.FakeSignIn(clock, usercode=[httpx.Response(429, json={})])
    result, _ = _login(server)
    assert isinstance(result, SignInServiceDown) and result.last_status == 429
    assert sum(1 for r in server.requests if r.url.path.endswith("usercode")) == 4


def test_usercode_answer_without_its_fields_is_refused() -> None:
    clock = FakeClock(START)
    server = fake.FakeSignIn(clock, usercode=[httpx.Response(200, json={"user_code": "X"})])
    result, _ = _login(server)
    assert isinstance(result, DeviceFlowError) and "lacks its code" in str(result)


def test_server_errors_back_off_and_six_in_a_row_end_the_flow() -> None:
    clock = FakeClock(START)
    server = fake.FakeSignIn(clock, polls=[httpx.Response(503, text="busy")])
    result, _ = _login(server)
    assert isinstance(result, SignInServiceDown) and result.last_status == 503
    assert len(server.poll_times) == 6
    assert _gaps(server.poll_times) == [10, 20, 40, 60, 60]


def test_a_blip_then_approval_still_signs_in() -> None:
    clock = FakeClock(START)
    server = fake.FakeSignIn(
        clock,
        polls=[httpx.Response(502, text="x"), _poll(403), fake.FakeSignIn().polls[0]],
    )
    tokens, _ = _login(server)
    assert isinstance(tokens, TokenSet)
    assert _gaps(server.poll_times) == [10, 5]


def test_transport_errors_count_as_failures() -> None:
    clock = FakeClock(START)
    server = fake.FakeSignIn(clock)

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/deviceauth/token"):
            server.poll_times.append(clock.monotonic())
            raise httpx.ConnectError("refused", request=request)
        return server.handle(request)

    async def main() -> BaseException | TokenSet:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            try:
                grant = await _drive(
                    clock, device_signin.start(client, fake.DEVICE_SPEC, clock=clock)
                )
                return await _drive(
                    clock, device_signin.finish(client, fake.DEVICE_SPEC, grant, clock=clock)
                )
            except BaseException as exc:
                return exc

    result = asyncio.run(main())
    assert isinstance(result, SignInServiceDown) and "no connection" in str(result)


def test_unexpected_status_stops_at_once() -> None:
    clock = FakeClock(START)
    server = fake.FakeSignIn(clock, polls=[httpx.Response(400, json={"error": "bad"})])
    result, _ = _login(server)
    assert isinstance(result, DeviceFlowError) and "HTTP 400" in str(result)
    assert len(server.poll_times) == 1


def test_the_code_expires_after_the_deadline() -> None:
    clock = FakeClock(START)
    spec = replace(fake.DEVICE_SPEC, deadline_seconds=30)
    server = fake.FakeSignIn(clock, polls=[_poll(403)])

    async def main() -> BaseException | TokenSet:
        async with server.client() as client:
            try:
                return await _drive(
                    clock,
                    device_signin.run_login(
                        spec, clock=clock, say=lambda _l: None, open_url=None, client=client
                    ),
                )
            except BaseException as exc:
                return exc

    result = asyncio.run(main())
    assert isinstance(result, DeviceCodeExpired)
    assert clock.monotonic() <= 31


def test_a_refused_exchange_carries_no_secret() -> None:
    clock = FakeClock(START)
    leaky = httpx.Response(400, json={"error": "invalid_grant", "code": "auth-code-1"})
    server = fake.FakeSignIn(clock, token=[leaky])
    result, lines = _login(server)
    assert isinstance(result, DeviceFlowError) and "HTTP 400" in str(result)
    for secret in ("auth-code-1", "verifier-1", "dev-auth-1"):
        assert secret not in str(result) and secret not in "\n".join(lines)


def test_approval_without_a_code_is_refused() -> None:
    clock = FakeClock(START)
    server = fake.FakeSignIn(clock, polls=[httpx.Response(200, json={"authorization_code": "x"})])
    result, _ = _login(server)
    assert isinstance(result, DeviceFlowError) and "no authorization code" in str(result)


def test_rfc_device_specs_still_go_the_rfc_way() -> None:
    spec = DeviceFlowSpec(
        "example", "https://idp.example.test/d", "https://idp.example.test/t", "c"
    )
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "device_code": "dc",
                "user_code": "U",
                "verification_uri": "https://idp.example.test/v",
                "expires_in": 60,
            },
        )

    async def main() -> DeviceAuthorization:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await device_signin.start(client, spec, clock=FakeClock(START))

    grant = asyncio.run(main())
    assert grant.user_code == "U" and seen == ["/d"]


# -- the browser flow ------------------------------------------------------------------------------


def _get(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=5) as reply:  # noqa: S310 (loopback only)
            return int(reply.status)
    except urllib.error.HTTPError as err:
        return int(err.code)


def _browser_login(
    spec: pkce.PkceFlowSpec, answers: list[str], server: fake.FakeSignIn
) -> tuple[pkce.PkceOutcome | BaseException, list[str], list[int]]:
    clock = FakeClock(START)
    server.clock = clock
    opened: list[str] = []
    statuses: list[int] = []
    lines: list[str] = []

    def opener(url: str) -> bool:
        opened.append(url)
        params = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        if "redirect_uri" not in params:  # the device page of the fallback
            return True
        target = params["redirect_uri"].replace("localhost", "127.0.0.1")
        for answer in answers:
            query = answer.replace("{state}", params["state"])
            statuses.append(_get(f"{target}?{query}"))
        return True

    async def main() -> pkce.PkceOutcome | BaseException:
        async with server.client() as client:
            try:
                return await _drive(
                    clock,
                    pkce.run_pkce_login(
                        spec,
                        clock=clock,
                        say=lines.append,
                        env={"DISPLAY": ":0"},
                        client=client,
                        open_url=opener,
                        timeout=10,
                    ),
                    step=0.1,
                )
            except BaseException as exc:
                return exc

    result = asyncio.run(main())
    return result, opened + lines, statuses


def test_browser_sign_in_checks_state_and_exchanges_once() -> None:
    server = fake.FakeSignIn()
    answers = [
        urlencode({"code": "stolen", "state": "wrong"}),
        "code=auth-code-1&state={state}",
        "code=again&state={state}",
    ]
    result, lines, statuses = _browser_login(fake.BROWSER_SPEC, answers, server)
    assert isinstance(result, pkce.PkceOutcome) and result.tokens is not None, result
    assert result.tokens.access_token == fake.fake_jwt(1)
    assert statuses[:2] == [400, 200]
    assert len(server.token_forms) == 1
    form = server.token_forms[0]
    assert form["code"] == "auth-code-1" and form["client_id"] == "client-under-test"
    assert form["redirect_uri"].startswith("http://localhost:") and form["code_verifier"]
    url = lines[0]
    params = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert params["client_id"] == "client-under-test"
    assert params["scope"] == "openid profile email offline_access"
    assert params["id_token_add_organizations"] == "true"
    assert params["code_challenge_method"] == "S256" and params["response_type"] == "code"
    assert urlsplit(params["redirect_uri"]).path == "/auth/callback"
    assert fake.fake_jwt(1) not in "\n".join(lines)


def test_browser_flow_falls_back_to_the_device_code_when_the_port_is_busy() -> None:
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    try:
        spec = replace(fake.BROWSER_SPEC, fixed_port=int(holder.getsockname()[1]))
        server = fake.FakeSignIn()
        server.polls = [fake.FakeSignIn().polls[0]]
        result, lines, _ = _browser_login(spec, [], server)
    finally:
        holder.close()
    assert isinstance(result, pkce.PkceOutcome) and result.via_device, result
    assert any("busy" in line for line in lines)
    assert any("ABCD-1234" in line for line in lines)


def test_tokens_from_the_exchange_expire_as_announced() -> None:
    clock = FakeClock(START)
    server = fake.FakeSignIn(clock, token=[fake.tokens_answer(3, expires_in=600)])
    tokens, _ = _login(server)
    assert isinstance(tokens, TokenSet)
    assert tokens.expires_at == tokens.obtained_at + timedelta(seconds=600)
    assert "id_token" not in tokens.to_secret()  # the id token is never kept


def test_facts_cite_their_sources() -> None:
    doc = chatgpt_facts.__doc__ or ""
    for source in ("server.rs", "device_code_auth.rs", "auth.rs", "client.rs"):
        assert source in doc
    assert chatgpt_facts.ORIGINATOR == "indus-agent"
