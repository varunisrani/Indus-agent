"""Loopback PKCE sign-in and the browser/remote heuristics (P07-10)."""

from __future__ import annotations

import asyncio
import json
import re
import socket
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
import respx

from indus.providers.auth import browser, pkce
from indus.providers.auth.device_code import DeviceFlowSpec
from indus.providers.auth.flows import FLOWS, flows_for
from indus.providers.auth.pkce import (
    CallbackPortBusy,
    CallbackTimeout,
    LoopbackServer,
    PkceDenied,
    PkceExchangeFailed,
    PkceFlowSpec,
    PkceOutcome,
    challenge_for,
    make_verifier,
)
from tests.fakes import oauth_server as fake
from tests.fakes.clock import FakeClock

START = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
DESKTOP = {"DISPLAY": ":0"}


def _openrouter() -> PkceFlowSpec:
    spec = FLOWS["openrouter"].pkce
    assert spec is not None
    return spec


OPENROUTER = _openrouter()
TOKEN_SPEC = PkceFlowSpec(
    provider="example",
    authorize_url=f"{fake.ISSUER}/authorize",
    exchange_url=fake.TOKEN_URL,
    exchange="token",
    client_id=fake.CLIENT_ID,
    scope="models",
)


async def _drive[T](clock: FakeClock, work: Awaitable[T], step: float = 0.1) -> T:
    task = asyncio.ensure_future(work)
    for _ in range(100_000):
        await clock.run_until_idle()
        if task.done():
            return task.result()
        clock.advance(step)
    raise AssertionError("never finished")


def _get(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=5) as reply:  # noqa: S310 (loopback only)
            return int(reply.status)
    except urllib.error.HTTPError as err:
        return int(err.code)


def _callback_of(url: str) -> tuple[str, dict[str, str]]:
    params = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    target = params.get("callback_url") or params["redirect_uri"]
    return target, params


class Browser:
    """A fake authorize step: answers the callback the way the provider's page would."""

    def __init__(self, reply: Callable[[str, dict[str, str]], list[str]]) -> None:
        self.reply = reply
        self.urls: list[str] = []
        self.statuses: list[int] = []

    def __call__(self, url: str) -> bool:
        self.urls.append(url)
        target, params = _callback_of(url)
        for query in self.reply(target, params):
            self.statuses.append(_get(f"{target}{query}"))
        return True


def approve(target: str, params: dict[str, str]) -> list[str]:
    return ["?" + urlencode({"code": "auth-code-1", "state": params["state"]})]


def _run(
    spec: PkceFlowSpec,
    opener: Callable[[str], bool] | None,
    *,
    env: dict[str, str] | None = None,
    timeout: float = 30,
    routes: Callable[[respx.MockRouter], None] | None = None,
) -> tuple[PkceOutcome | BaseException, list[str], list[httpx.Request]]:
    clock = FakeClock(START)
    lines: list[str] = []
    sent: list[httpx.Request] = []

    async def main() -> PkceOutcome | BaseException:
        with respx.mock(assert_all_called=False) as router:
            if routes is not None:
                routes(router)
            async with httpx.AsyncClient() as client:
                try:
                    return await _drive(
                        clock,
                        pkce.run_pkce_login(
                            spec,
                            clock=clock,
                            say=lines.append,
                            env=env if env is not None else DESKTOP,
                            client=client,
                            open_url=opener,
                            timeout=timeout,
                        ),
                    )
                except BaseException as exc:
                    return exc
                finally:
                    sent.extend(call.request for call in router.calls)

    result = asyncio.run(main())
    return result, lines, sent


def _key_route(key: str) -> Callable[[respx.MockRouter], None]:
    def add(router: respx.MockRouter) -> None:
        router.post(OPENROUTER.exchange_url).mock(
            return_value=httpx.Response(200, json={"key": key})
        )

    return add


def _port_is_free(port: int) -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


def test_s256_matches_the_rfc_7636_example() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert challenge_for(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    made = make_verifier()
    assert 43 <= len(made) <= 128 and re.fullmatch(r"[A-Za-z0-9._~-]+", made)
    assert make_verifier() != made


def test_round_trip_mints_a_key() -> None:
    key = fake.fake_token("minted")
    opener = Browser(approve)
    outcome, lines, sent = _run(OPENROUTER, opener, routes=_key_route(key))
    assert isinstance(outcome, PkceOutcome) and outcome.api_key == key
    assert key not in "\n".join(lines) and key not in repr(outcome)
    assert opener.statuses == [200]
    url = opener.urls[0]
    assert url.startswith("https://openrouter.ai/auth?")
    target, params = _callback_of(url)
    port = urlsplit(target).port
    assert target == f"http://127.0.0.1:{port}/callback" and port
    assert params["code_challenge_method"] == "S256" and params["key_label"] == "Indus Agent"
    body = json.loads(sent[-1].content)
    assert body["code"] == "auth-code-1" and body["code_challenge_method"] == "S256"
    assert challenge_for(body["code_verifier"]) == params["code_challenge"]
    assert _port_is_free(port)


def test_authorization_code_grant_returns_tokens() -> None:
    def routes(router: respx.MockRouter) -> None:
        router.post(fake.TOKEN_URL).mock(return_value=fake.success())

    opener = Browser(approve)
    outcome, _, sent = _run(TOKEN_SPEC, opener, routes=routes)
    assert isinstance(outcome, PkceOutcome) and outcome.tokens is not None
    assert outcome.tokens.access_token == fake.fake_token("access")
    form = {k: v[0] for k, v in parse_qs(sent[-1].content.decode()).items()}
    target, params = _callback_of(opener.urls[0])
    assert form["grant_type"] == "authorization_code" and form["redirect_uri"] == target
    assert form["client_id"] == fake.CLIENT_ID and params["response_type"] == "code"
    assert challenge_for(form["code_verifier"]) == params["code_challenge"]


def test_wrong_state_is_refused_and_never_exchanged() -> None:
    def forged(target: str, params: dict[str, str]) -> list[str]:
        return [
            "?" + urlencode({"code": "stolen", "state": "not-the-state"}),
            "?" + urlencode({"code": "stolen"}),
        ]

    opener = Browser(forged)
    outcome, _, sent = _run(OPENROUTER, opener, timeout=6, routes=_key_route("x" * 20))
    assert opener.statuses == [400, 400]
    assert isinstance(outcome, CallbackTimeout)
    assert "2 callback(s) with the wrong sign-in state" in str(outcome)
    assert not sent


def test_other_paths_get_404_and_change_nothing() -> None:
    def wander(target: str, params: dict[str, str]) -> list[str]:
        base = target.removesuffix("/callback")
        assert _get(base + "/elsewhere?code=x&state=" + params["state"]) == 404
        return approve(target, params)

    outcome, _, _ = _run(OPENROUTER, Browser(wander), routes=_key_route(fake.fake_token("k")))
    assert isinstance(outcome, PkceOutcome)


def test_declined_sign_in_raises() -> None:
    def decline(target: str, params: dict[str, str]) -> list[str]:
        return ["?" + urlencode({"error": "access_denied", "error_description": "user said no"})]

    outcome, _, sent = _run(OPENROUTER, Browser(decline))
    assert isinstance(outcome, PkceDenied) and "user said no" in str(outcome)
    assert not sent


def test_rejected_code_on_exchange() -> None:
    def routes(router: respx.MockRouter) -> None:
        router.post(OPENROUTER.exchange_url).mock(return_value=httpx.Response(403, json={}))

    outcome, _, _ = _run(OPENROUTER, Browser(approve), routes=routes)
    assert isinstance(outcome, PkceExchangeFailed) and "sign in again" in str(outcome)


def test_server_closed_on_timeout() -> None:
    seen: list[int] = []

    def remember(url: str) -> bool:
        seen.append(urlsplit(_callback_of(url)[0]).port or 0)
        return False

    outcome, lines, _ = _run(OPENROUTER, remember, timeout=1)
    assert isinstance(outcome, CallbackTimeout)
    assert "No browser could be opened" in "\n".join(lines)
    assert seen and _port_is_free(seen[0])


def test_minimum_wait_is_five_seconds() -> None:
    clock = FakeClock(START)

    async def main() -> float:
        with LoopbackServer() as server:
            try:
                await _drive(clock, server.wait(0.5, clock))
            except CallbackTimeout:
                return clock.monotonic()
        return -1

    assert asyncio.run(main()) >= 5


def test_busy_fixed_port_falls_back_to_the_device_flow() -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        port = taken.getsockname()[1]
        device = DeviceFlowSpec(
            provider="example",
            device_authorization_url=fake.DEVICE_URL,
            token_url=fake.TOKEN_URL,
            client_id=fake.CLIENT_ID,
        )
        with_fallback = PkceFlowSpec(
            **{**TOKEN_SPEC.__dict__, "fixed_port": port, "fallback_device": device}
        )
        clock = FakeClock(START)
        server = fake.FakeOAuthServer(clock, script=[fake.pending(), fake.success()])
        lines: list[str] = []

        async def main() -> PkceOutcome:
            with server.running():
                async with httpx.AsyncClient() as client:
                    return await _drive(
                        clock,
                        pkce.run_pkce_login(
                            with_fallback,
                            clock=clock,
                            say=lines.append,
                            env={},
                            client=client,
                            open_url=None,
                        ),
                        step=0.5,
                    )

        outcome = asyncio.run(main())
        assert outcome.via_device and outcome.tokens is not None
        assert f"Port {port} is busy" in lines[0]

        without = PkceFlowSpec(**{**TOKEN_SPEC.__dict__, "fixed_port": port})
        result, _, _ = _run(without, Browser(approve))
        assert isinstance(result, CallbackPortBusy) and result.code == "auth.port_busy"


def test_remote_session_prints_the_tunnel_hint_and_opens_nothing() -> None:
    opened: list[str] = []

    def opener(url: str) -> bool:
        opened.append(url)
        return True

    env = {"SSH_CONNECTION": "x", "SSH_CLIENT": "10.0.0.2 5555 22", "USER": "asha", "DISPLAY": ":0"}
    outcome, lines, _ = _run(OPENROUTER, opener, env=env, timeout=1)
    assert isinstance(outcome, CallbackTimeout) and opened == []
    hint = next(line for line in lines if "ssh -L" in line)
    port = re.search(r"-L (\d+):127\.0\.0\.1:(\d+) asha@", hint)
    assert port is not None and port.group(1) == port.group(2)


@pytest.mark.parametrize(
    ("env", "platform", "graphical", "remote"),
    [
        ({}, "linux", False, False),
        ({"DISPLAY": ":0"}, "linux", True, False),
        ({"WAYLAND_DISPLAY": "wayland-0"}, "linux", True, False),
        ({"BROWSER": "firefox"}, "linux", True, False),
        ({"BROWSER": "lynx"}, "linux", False, False),
        ({"BROWSER": "/usr/bin/w3m %s", "DISPLAY": ":0"}, "linux", False, False),
        ({"BROWSER": "elinks:firefox", "DISPLAY": ":0"}, "linux", False, False),
        ({}, "darwin", True, False),
        ({}, "win32", True, False),
        ({"BROWSER": "links"}, "darwin", False, False),
        ({"SSH_TTY": "/dev/pts/1", "DISPLAY": ":0"}, "linux", True, True),
        ({"SSH_CLIENT": "1.2.3.4 1 22"}, "darwin", True, True),
        ({"CODESPACES": "true"}, "linux", False, True),
        ({"CODESPACE_NAME": "x"}, "linux", False, True),
        ({"CLOUD_SHELL": "true", "DISPLAY": ":0"}, "linux", True, True),
        ({"GITPOD_WORKSPACE_ID": "w"}, "linux", False, True),
        ({"REPL_ID": "r"}, "linux", False, True),
        ({"STACKBLITZ": "1"}, "darwin", True, True),
        ({"SSH_TTY": ""}, "darwin", True, False),
    ],
)
def test_browser_heuristics_table(
    env: dict[str, str], platform: str, graphical: bool, remote: bool
) -> None:
    assert browser.graphical_browser_likely(env, platform=platform) is graphical
    assert browser.is_remote_session(env) is remote
    assert browser.should_open(env, platform=platform) is (graphical and not remote)
    assert browser.should_open(env, no_browser=True, platform=platform) is False


def test_tunnel_hint_text() -> None:
    assert browser.tunnel_hint(4242, {"USER": "ravi"}, host="box") == (
        "ssh -L 4242:127.0.0.1:4242 ravi@box"
    )
    assert browser.tunnel_hint(1, {}, host="h").endswith(" you@h")


def test_only_documented_flows_are_registered() -> None:
    """R01.12: one flow, OpenRouter's key mint; it needs no client id and is not the default."""
    assert set(FLOWS) == {"openrouter"}
    flows = flows_for("openrouter")
    assert flows.pkce is not None and flows.pkce.client_id == "" and flows.device is None
    assert not flows.oauth_default
    assert flows.pkce.authorize_url == "https://openrouter.ai/auth"
    assert flows.pkce.exchange_url == "https://openrouter.ai/api/v1/auth/keys"
    assert not flows_for("anthropic").any
