"""respx routes for an OAuth authorization server: device authorization, token polling, refresh.

``FakeOAuthServer`` answers the device authorization endpoint with a fixed grant and the token
endpoint from a script of answers (``pending()``, ``slow_down()``, ``unavailable(503, 7)``,
``success()`` …), one per request; the last answer repeats. Each token request is recorded with
the fake clock's monotonic time so tests can assert the waits between polls.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import parse_qs

import httpx
import respx

from tests.fakes.clock import FakeClock

ISSUER = "https://auth.example.test"
DEVICE_URL = f"{ISSUER}/oauth/device/code"
TOKEN_URL = f"{ISSUER}/oauth/token"
CLIENT_ID = "indus-test-client"


def fake_token(kind: str, n: int = 1) -> str:
    """A token-looking string built at run time (never a literal credential shape)."""
    return "-".join(("fake", kind, "value", str(n), "0" * 12))


def pending() -> httpx.Response:
    return httpx.Response(400, json={"error": "authorization_pending"})


def slow_down() -> httpx.Response:
    return httpx.Response(400, json={"error": "slow_down"})


def denied() -> httpx.Response:
    return httpx.Response(400, json={"error": "access_denied"})


def expired() -> httpx.Response:
    return httpx.Response(400, json={"error": "expired_token"})


def oauth_error(code: str, description: str = "") -> httpx.Response:
    body = {"error": code}
    if description:
        body["error_description"] = description
    return httpx.Response(400, json=body)


def unavailable(status: int = 503, retry_after: float | None = None) -> httpx.Response:
    headers = {"Retry-After": str(int(retry_after))} if retry_after is not None else {}
    return httpx.Response(status, text="<html>busy</html>", headers=headers)


def success(n: int = 1, *, expires_in: int | None = 3600, refresh: bool = True) -> httpx.Response:
    body: dict[str, object] = {"access_token": fake_token("access", n), "token_type": "Bearer"}
    if expires_in is not None:
        body["expires_in"] = expires_in
    if refresh:
        body["refresh_token"] = fake_token("refresh", n)
    return httpx.Response(200, json=body)


@dataclass
class FakeOAuthServer:
    clock: FakeClock
    expires_in: int = 900
    interval: int | None = 5
    script: list[httpx.Response] = field(default_factory=lambda: [success()])
    refresh_script: list[httpx.Response] = field(default_factory=lambda: [success(2)])
    poll_times: list[float] = field(default_factory=list[float])
    token_forms: list[dict[str, str]] = field(default_factory=list[dict[str, str]])

    def _grant(self, request: httpx.Request) -> httpx.Response:
        body: dict[str, object] = {
            "device_code": "dev-code-1",
            "user_code": "WDJB-MJHT",
            "verification_uri": f"{ISSUER}/device",
            "verification_uri_complete": f"{ISSUER}/device?user_code=WDJB-MJHT",
            "expires_in": self.expires_in,
        }
        if self.interval is not None:
            body["interval"] = self.interval
        return httpx.Response(200, json=body)

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.token_forms.append(form)
        if form.get("grant_type") == "refresh_token":
            script = self.refresh_script
        else:
            self.poll_times.append(self.clock.monotonic())
            script = self.script
        return script.pop(0) if len(script) > 1 else script[0]

    @contextmanager
    def running(self) -> Iterator[respx.MockRouter]:
        with respx.mock(assert_all_called=False) as router:
            router.post(DEVICE_URL).mock(side_effect=self._grant)
            router.post(TOKEN_URL).mock(side_effect=self._token)
            yield router
