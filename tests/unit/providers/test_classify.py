"""Error classifier (P03-15): the case table plus hook, host-hint and redaction rules."""

from __future__ import annotations

import builtins
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from indus.foundation import errors as product_errors
from indus.foundation import yamlio
from indus.foundation.errors import ProviderHTTPError, ProviderStreamError
from indus.providers.classify import REASONS, ClassifiedError, ErrorContext, Verdict, classify
from tests.fakes.clock import FakeClock

CASES_FILE = Path(__file__).resolve().parents[2] / "fixtures" / "errors" / "classify_cases.yaml"
CASES: list[dict[str, Any]] = yamlio.load_safe(CASES_FILE)["cases"]

# Built at runtime so no credential-shaped literal sits in the source.
FAKE_KEY = "test" + "key" + "-" + "q7Lm2Vx9" * 3


@dataclass(frozen=True)
class Route:
    base_url: str
    api_key: str = FAKE_KEY
    provider: str = "custom"


@dataclass(frozen=True)
class Profile:
    hostname: str
    hook: Any = None

    def classify_error(self, ctx: ErrorContext) -> Any:
        return self.hook(ctx) if self.hook else None


def build_error(case: dict[str, Any]) -> BaseException:
    body = case.get("body")
    if "status" in case:
        return ProviderHTTPError(
            f"HTTP {case['status']}",
            status=int(case["status"]),
            body=body,
            headers={str(k): str(v) for k, v in (case.get("headers") or {}).items()},
        )
    if "stream" in case:
        message = ""
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            message = str(body["error"].get("message", ""))
        return ProviderStreamError(message, body=body if isinstance(body, dict) else {})
    name = case["exception"]
    message = case.get("message", "")
    if hasattr(httpx, name):
        return getattr(httpx, name)(message)
    if hasattr(product_errors, name):
        return getattr(product_errors, name)(message)
    return getattr(builtins, name)(message)


def test_table_is_big_enough_and_ids_unique() -> None:
    ids = [c["id"] for c in CASES]
    assert len(ids) >= 60
    assert len(set(ids)) == len(ids)


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_cases(case: dict[str, Any]) -> None:
    result = classify(build_error(case))
    expect = dict(case["expect"])
    contains = expect.pop("message_contains", None)
    assert result.reason in REASONS
    for name, value in expect.items():
        assert getattr(result, name) == value, f"{case['id']}: {name}"
    if contains:
        assert contains in result.message


def test_every_reason_in_the_closed_set_is_reachable_from_the_table() -> None:
    seen = {classify(build_error(c)).reason for c in CASES}
    missing = REASONS - seen - {"auth_permanent"}  # only a profile hook or a code produces it
    assert not missing


def test_status_is_reported() -> None:
    err = ProviderHTTPError("x", status=503, body="Service Unavailable")
    assert classify(err).status == 503


def test_profile_hook_wins() -> None:
    err = ProviderHTTPError("x", status=403, body={"error": {"message": "quota_exhausted"}})
    assert classify(err).reason == "auth"
    as_string = Profile("api.example.com", hook=lambda ctx: "billing")
    assert classify(err, profile=as_string).reason == "billing"
    as_mapping = Profile(
        "api.example.com", hook=lambda ctx: {"reason": "rate_limit", "should_fallback": False}
    )
    result = classify(err, profile=as_mapping)
    assert result.reason == "rate_limit" and result.should_fallback is False
    as_verdict = Profile("api.example.com", hook=lambda ctx: Verdict("overloaded"))
    assert classify(err, profile=as_verdict).reason == "overloaded"
    whole = ClassifiedError(reason="auth_permanent", retryable=False, message="gone")
    assert classify(err, profile=Profile("h", hook=lambda ctx: whole)).reason == "auth_permanent"


def test_profile_hook_sees_the_context() -> None:
    seen: list[ErrorContext] = []

    def hook(ctx: ErrorContext) -> None:
        seen.append(ctx)

    err = ProviderHTTPError(
        "x", status=429, body={"error": {"code": "slow", "message": "Hold On"}}, headers={}
    )
    classify(err, profile=Profile("h", hook=hook))
    (ctx,) = seen
    assert ctx.status == 429 and ctx.code == "slow" and "hold on" in ctx.text


@pytest.mark.parametrize("result", [RuntimeError("boom"), "not-a-reason", {"reason": "nope"}, 42])
def test_bad_or_failing_hook_falls_back_to_the_rules(result: Any) -> None:
    def hook(ctx: ErrorContext) -> Any:
        if isinstance(result, Exception):
            raise result
        return result

    err = ProviderHTTPError("x", status=401, body="Unauthorized")
    assert classify(err, profile=Profile("h", hook=hook)).reason == "auth"


def test_host_hint_only_for_non_default_host() -> None:
    err = ProviderHTTPError("x", status=401, body={"error": {"message": "Invalid API key"}})
    own = classify(
        err, route=Route("https://api.example.com/v1"), profile=Profile("api.example.com")
    )
    assert own.host_hint is None and "sent to" not in own.message
    other = classify(
        err, route=Route("https://my.proxy.example/v1"), profile=Profile("api.example.com")
    )
    assert other.host_hint == "my.proxy.example"
    assert other.message.endswith("(sent to my.proxy.example)")
    no_profile = classify(err, route=Route("https://my.proxy.example/v1"))
    assert no_profile.host_hint == "my.proxy.example"
    not_auth = classify(
        ProviderHTTPError("x", status=500, body="Internal Server Error"),
        route=Route("https://my.proxy.example/v1"),
        profile=Profile("api.example.com"),
    )
    assert not_auth.host_hint is None


def test_no_message_contains_the_key() -> None:
    route = Route("https://my.proxy.example/v1")
    echoed = f"Incorrect API key provided: {FAKE_KEY}. Authorization: Bearer {FAKE_KEY}"
    for case in CASES:
        err = build_error(case)
        assert FAKE_KEY not in classify(err, route=route).message
    for status in (400, 401, 403, 429, 500):
        err = ProviderHTTPError("x", status=status, body={"error": {"message": echoed}})
        assert FAKE_KEY not in classify(err, route=route).message
    stream = ProviderStreamError(echoed, body={"error": {"message": echoed}})
    assert FAKE_KEY not in classify(stream, route=route).message


def test_reset_at_with_clock() -> None:
    clock = FakeClock(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
    err = ProviderHTTPError(
        "x", status=402, body={"error": {"message": "Usage limit reached, resets in 1h 5m"}}
    )
    result = classify(err, clock=clock)
    assert result.reason == "rate_limit"
    assert result.reset_at == clock.now() + timedelta(hours=1, minutes=5)
    assert result.retry_after == 600.0  # waits are capped, the reset time is not
    assert classify(err).reset_at is None


def test_http_date_retry_after_uses_the_clock() -> None:
    clock = FakeClock(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
    err = ProviderHTTPError(
        "x",
        status=503,
        body="busy",
        headers={"Retry-After": "Thu, 01 Jan 2026 09:00:30 GMT"},
    )
    assert classify(err, clock=clock).retry_after == 30.0


def test_http_status_error_from_httpx_is_read() -> None:
    request = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    response = httpx.Response(
        429, json={"error": {"message": "Rate limit reached"}}, request=request
    )
    err = httpx.HTTPStatusError("429", request=request, response=response)
    result = classify(err)
    assert result.reason == "rate_limit" and result.status == 429
