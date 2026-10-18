"""RFC 8628 device-code polling and the refresh grant (P07-09), on a fake clock and fake server."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from indus.providers.auth import device_code
from indus.providers.auth.device_code import (
    AccessDenied,
    DeviceCodeExpired,
    DeviceFlowError,
    DeviceFlowSpec,
    ReloginRequired,
    SignInServiceDown,
    TokenSet,
    needs_refresh,
)
from tests.fakes import oauth_server as fake
from tests.fakes.clock import FakeClock

START = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
SPEC = DeviceFlowSpec(
    provider="example",
    device_authorization_url=fake.DEVICE_URL,
    token_url=fake.TOKEN_URL,
    client_id=fake.CLIENT_ID,
    scope="models",
)


async def _drive[T](clock: FakeClock, work: Awaitable[T], step: float = 0.5) -> T:
    task = asyncio.ensure_future(work)
    for _ in range(100_000):
        await clock.run_until_idle()
        if task.done():
            return task.result()
        clock.advance(step)
    raise AssertionError("never finished")


def _login(
    server: fake.FakeOAuthServer, spec: DeviceFlowSpec = SPEC
) -> tuple[TokenSet | BaseException, list[str]]:
    lines: list[str] = []
    opened: list[str] = []

    async def go() -> TokenSet:
        async with httpx.AsyncClient() as client:
            return await device_code.run_device_login(
                spec,
                clock=server.clock,
                say=lines.append,
                open_url=lambda url: opened.append(url) or True,
                client=client,
            )

    async def main() -> TokenSet | BaseException:
        with server.running():
            try:
                return await _drive(server.clock, go())
            except BaseException as exc:  # returned so tests can inspect it
                return exc

    result = asyncio.run(main())
    return result, lines + [f"opened {u}" for u in opened]


def _gaps(times: list[float]) -> list[float]:
    return [round(b - a, 3) for a, b in zip(times, times[1:], strict=False)]


def test_pending_three_times_then_success() -> None:
    clock = FakeClock(START)
    server = fake.FakeOAuthServer(
        clock, script=[fake.pending(), fake.pending(), fake.pending(), fake.success()]
    )
    tokens, lines = _login(server)
    assert isinstance(tokens, TokenSet)
    assert tokens.access_token == fake.fake_token("access")
    assert tokens.refresh_token == fake.fake_token("refresh")
    assert tokens.obtained_at == START + timedelta(seconds=15)
    assert tokens.expires_at == tokens.obtained_at + timedelta(seconds=3600)
    assert _gaps(server.poll_times) == [5, 5, 5]
    assert server.token_forms[0] == {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": "dev-code-1",
        "client_id": fake.CLIENT_ID,
    }
    text = "\n".join(lines)
    assert "WDJB-MJHT" in text and f"opened {fake.ISSUER}/device?user_code=WDJB-MJHT" in text


def test_missing_interval_means_five_seconds() -> None:
    clock = FakeClock(START)
    server = fake.FakeOAuthServer(clock, interval=None, script=[fake.pending(), fake.success()])
    _login(server)
    assert _gaps(server.poll_times) == [5]


def test_slow_down_adds_five_seconds_and_caps_at_thirty() -> None:
    clock = FakeClock(START)
    script = [fake.slow_down()] * 7 + [fake.pending(), fake.success()]
    server = fake.FakeOAuthServer(clock, expires_in=3600, script=list(script))
    tokens, _ = _login(server)
    assert isinstance(tokens, TokenSet)
    # 5 → 10, 15, 20, 25, 30, then capped; the grown interval also paces the pending poll.
    assert _gaps(server.poll_times) == [10, 15, 20, 25, 30, 30, 30, 30]


def test_documented_slow_down_step_is_honoured() -> None:
    clock = FakeClock(START)
    server = fake.FakeOAuthServer(
        clock, script=[fake.slow_down(), fake.slow_down(), fake.success()]
    )
    spec = DeviceFlowSpec(**{**SPEC.__dict__, "slow_down_step": 2})
    _login(server, spec)
    assert _gaps(server.poll_times) == [7, 9]


def test_503_with_retry_after_is_honoured() -> None:
    clock = FakeClock(START)
    server = fake.FakeOAuthServer(
        clock,
        script=[fake.unavailable(503, retry_after=7), fake.unavailable(429, 2), fake.success()],
    )
    tokens, _ = _login(server)
    assert isinstance(tokens, TokenSet)
    # Retry-After 7 is used; Retry-After 2 is raised to the 5 s interval.
    assert _gaps(server.poll_times) == [7, 5]


def test_failures_without_retry_after_back_off_exponentially_to_sixty() -> None:
    clock = FakeClock(START)
    script = [fake.unavailable(502)] * 5 + [fake.unavailable(403), fake.success()]
    server = fake.FakeOAuthServer(clock, expires_in=3600, script=list(script))
    tokens, _ = _login(server)
    assert isinstance(tokens, TokenSet)
    assert _gaps(server.poll_times) == [10, 20, 40, 60, 60, 60]


def test_outage_at_the_deadline_is_reported_as_an_outage() -> None:
    clock = FakeClock(START)
    server = fake.FakeOAuthServer(clock, expires_in=60, script=[fake.unavailable(503)])
    error, _ = _login(server)
    assert isinstance(error, SignInServiceDown)
    assert error.code == "auth.service_down" and error.last_status == 503
    assert "not about your approval" in str(error)
    assert server.poll_times[-1] <= 60  # never waits past the deadline


def test_plain_expiry_is_not_called_an_outage() -> None:
    clock = FakeClock(START)
    server = fake.FakeOAuthServer(clock, expires_in=20, script=[fake.pending()])
    error, _ = _login(server)
    assert type(error) is DeviceCodeExpired
    assert "expired before it was approved" in str(error)
    assert len(server.poll_times) == 4  # 0, 5, 10, 15


def test_recovery_clears_the_outage_flag() -> None:
    clock = FakeClock(START)
    server = fake.FakeOAuthServer(
        clock, expires_in=30, script=[fake.unavailable(503), fake.pending()]
    )
    error, _ = _login(server)
    assert type(error) is DeviceCodeExpired


def test_transport_errors_count_as_an_outage() -> None:
    clock = FakeClock(START)
    server = fake.FakeOAuthServer(clock, expires_in=30)

    async def main() -> BaseException | None:
        with server.running() as router:
            router.post(fake.TOKEN_URL).mock(side_effect=httpx.ConnectTimeout("slow"))
            async with httpx.AsyncClient() as client:
                grant = await device_code.request_device_authorization(client, SPEC)
                try:
                    await _drive(
                        clock, device_code.poll_for_token(client, SPEC, grant, clock=clock)
                    )
                except BaseException as exc:
                    return exc
        return None

    error = asyncio.run(main())
    assert isinstance(error, SignInServiceDown) and "no connection" in str(error)


@pytest.mark.parametrize(
    ("answer", "kind"),
    [
        (fake.denied(), AccessDenied),
        (fake.expired(), DeviceCodeExpired),
        (fake.oauth_error("invalid_client", "unknown client"), DeviceFlowError),
        (httpx.Response(200, json={"token_type": "Bearer"}), DeviceFlowError),
        (httpx.Response(400, text="nope"), DeviceFlowError),
    ],
)
def test_terminal_answers_raise_typed_errors(answer: httpx.Response, kind: type[Exception]) -> None:
    clock = FakeClock(START)
    server = fake.FakeOAuthServer(clock, script=[fake.pending(), answer])
    error, _ = _login(server)
    assert type(error) is kind


def test_device_authorization_needs_the_required_fields() -> None:
    async def main() -> None:
        with fake.FakeOAuthServer(FakeClock(START)).running() as router:
            router.post(fake.DEVICE_URL).mock(
                return_value=httpx.Response(200, json={"device_code": "d", "expires_in": 10})
            )
            async with httpx.AsyncClient() as client:
                await device_code.request_device_authorization(client, SPEC)

    with pytest.raises(DeviceFlowError, match="user_code, verification_uri"):
        asyncio.run(main())


def _refresh(server: fake.FakeOAuthServer, tokens: TokenSet) -> TokenSet:
    async def main() -> TokenSet:
        with server.running():
            async with httpx.AsyncClient() as client:
                return await device_code.fresh_tokens(client, SPEC, tokens, clock=server.clock)

    return asyncio.run(main())


def _tokens(expires_in: float | None = 600, refresh: str | None = "old") -> TokenSet:
    return TokenSet(
        access_token=fake.fake_token("access", 9),
        obtained_at=START,
        refresh_token=fake.fake_token("refresh", 9) if refresh else None,
        expires_at=START + timedelta(seconds=expires_in) if expires_in is not None else None,
    )


def test_refresh_skew_is_120_seconds() -> None:
    tokens = _tokens(600)
    assert not needs_refresh(tokens, START + timedelta(seconds=479))
    assert needs_refresh(tokens, START + timedelta(seconds=480))
    assert not needs_refresh(_tokens(None), START + timedelta(days=400))


def test_fresh_tokens_refresh_only_inside_the_skew() -> None:
    clock = FakeClock(START)
    server = fake.FakeOAuthServer(clock)
    tokens = _tokens(600)
    assert _refresh(server, tokens) is tokens
    clock.advance(500)
    renewed = _refresh(server, tokens)
    assert renewed.access_token == fake.fake_token("access", 2)
    assert renewed.refresh_token == fake.fake_token("refresh", 2)
    assert server.token_forms[-1]["grant_type"] == "refresh_token"
    assert server.token_forms[-1]["refresh_token"] == fake.fake_token("refresh", 9)


def test_refresh_keeps_the_old_refresh_token_when_none_is_returned() -> None:
    clock = FakeClock(START + timedelta(hours=1))
    server = fake.FakeOAuthServer(clock, refresh_script=[fake.success(3, refresh=False)])
    renewed = _refresh(server, _tokens(600))
    assert renewed.refresh_token == fake.fake_token("refresh", 9)


@pytest.mark.parametrize(
    "answer",
    [
        fake.oauth_error("invalid_grant"),
        fake.oauth_error("invalid_token"),
        fake.oauth_error("invalid_request", "refresh token was reused"),
    ],
)
def test_dead_grant_asks_to_sign_in_again(answer: httpx.Response) -> None:
    clock = FakeClock(START + timedelta(hours=1))
    server = fake.FakeOAuthServer(clock, refresh_script=[answer])
    with pytest.raises(ReloginRequired, match="sign in again"):
        _refresh(server, _tokens(600))


def test_no_refresh_token_means_sign_in_again() -> None:
    clock = FakeClock(START + timedelta(hours=1))
    with pytest.raises(ReloginRequired):
        _refresh(fake.FakeOAuthServer(clock), _tokens(600, refresh=None))


def test_server_error_on_refresh_is_not_terminal() -> None:
    clock = FakeClock(START + timedelta(hours=1))
    server = fake.FakeOAuthServer(clock, refresh_script=[fake.unavailable(500)])
    with pytest.raises(DeviceFlowError) as caught:
        _refresh(server, _tokens(600))
    assert type(caught.value) is DeviceFlowError


def test_token_set_secret_round_trip_and_repr_hides_values() -> None:
    tokens = _tokens(600)
    again = TokenSet.from_secret(tokens.to_secret())
    assert again == tokens
    assert fake.fake_token("access", 9) not in repr(tokens)
    with pytest.raises(ValueError):
        TokenSet.from_secret('{"token_type": "Bearer"}')


def test_tls_choices(tmp_path: Path) -> None:
    warnings: list[str] = []
    assert device_code.tls_verify(insecure=True, warn=warnings.append) is False
    assert "OFF" in warnings[0]
    assert device_code.tls_verify() is True
    with pytest.raises(DeviceFlowError, match="does not exist"):
        device_code.tls_verify(str(tmp_path / "missing.pem"))
