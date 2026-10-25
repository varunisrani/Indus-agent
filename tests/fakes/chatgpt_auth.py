"""A fake ChatGPT sign-in service and a fake Codex backend, as ``httpx.MockTransport`` handlers.

Nothing here reaches auth.openai.com or chatgpt.com: the sign-in endpoints live on
``auth.chatgpt-fake.test`` (pass ``DEVICE_SPEC`` / ``BROWSER_SPEC`` to the flows), and the backend
handler answers whatever host the request names without any network. Tokens are built at run time:
``fake_jwt`` makes an unsigned JWT-shaped access token carrying the account claims the backend
headers come from.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import httpx

from indus.providers.auth.chatgpt import ChatGptDeviceSpec
from indus.providers.auth.pkce import PkceFlowSpec
from tests.fakes.clock import FakeClock

ISSUER = "https://auth.chatgpt-fake.test"
DEVICE_SPEC = ChatGptDeviceSpec(
    usercode_url=f"{ISSUER}/api/accounts/deviceauth/usercode",
    poll_url=f"{ISSUER}/api/accounts/deviceauth/token",
    verify_page=f"{ISSUER}/codex/device",
    token_url=f"{ISSUER}/oauth/token",
    redirect_uri=f"{ISSUER}/deviceauth/callback",
    client_id="client-under-test",
)
BROWSER_SPEC = PkceFlowSpec(
    provider="openai-codex",
    authorize_url=f"{ISSUER}/oauth/authorize",
    exchange_url=f"{ISSUER}/oauth/token",
    exchange="token",
    client_id="client-under-test",
    scope="openid profile email offline_access",
    extra_params={"id_token_add_organizations": "true"},
    fallback_device=DEVICE_SPEC,
    redirect_host="localhost",
    callback_path="/auth/callback",
)
ACCOUNT = "acct-under-test-1"


def _b64(data: dict[str, Any]) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def fake_jwt(n: int = 1, *, account: str = ACCOUNT, plan: str = "plus", exp: int = 0) -> str:
    """An unsigned three-part token with the ChatGPT claim set (never a real credential)."""
    claims = {
        "https://api.openai.com/auth": {"chatgpt_account_id": account, "chatgpt_plan_type": plan},
        "exp": exp,
        "n": n,
    }
    return ".".join((_b64({"alg": "none", "typ": "JWT"}), _b64(claims), "sig" + "ab" * 6 + str(n)))


def fake_refresh(n: int = 1) -> str:
    return "-".join(("fake", "refresh", str(n), "0" * 10))


def tokens_answer(n: int = 1, *, expires_in: int = 3600, refresh: bool = True) -> httpx.Response:
    body: dict[str, Any] = {
        "access_token": fake_jwt(n),
        "id_token": fake_jwt(100 + n),
        "token_type": "Bearer",
        "expires_in": expires_in,
    }
    if refresh:
        body["refresh_token"] = fake_refresh(n)
    return httpx.Response(200, json=body)


@dataclass
class FakeSignIn:
    """The sign-in service. ``usercode`` and ``polls`` are scripts (the last answer repeats);
    every request is recorded with the fake clock's monotonic time."""

    clock: FakeClock | None = None
    usercode: list[httpx.Response] = field(
        default_factory=lambda: [
            httpx.Response(
                200,
                json={"device_auth_id": "dev-auth-1", "user_code": "ABCD-1234", "interval": "5"},
            )
        ]
    )
    polls: list[httpx.Response] = field(
        default_factory=lambda: [
            httpx.Response(
                200,
                json={
                    "authorization_code": "auth-code-1",
                    "code_verifier": "verifier-1" + "v" * 40,
                },
            )
        ]
    )
    token: list[httpx.Response] = field(default_factory=lambda: [tokens_answer(1)])
    poll_times: list[float] = field(default_factory=list[float])
    requests: list[httpx.Request] = field(default_factory=list[httpx.Request])
    token_forms: list[dict[str, str]] = field(default_factory=list[dict[str, str]])

    @staticmethod
    def _next(script: list[httpx.Response]) -> httpx.Response:
        return script.pop(0) if len(script) > 1 else script[0]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/deviceauth/usercode"):
            return self._next(self.usercode)
        if path.endswith("/deviceauth/token"):
            if self.clock is not None:
                self.poll_times.append(self.clock.monotonic())
            return self._next(self.polls)
        if path.endswith("/oauth/token"):
            self.token_forms.append(
                {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            )
            return self._next(self.token)
        return httpx.Response(404, json={"error": "not_found"})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def factory(self) -> Callable[[], httpx.AsyncClient]:
        return self.client


def sse(*events: dict[str, Any]) -> bytes:
    return b"".join(f"data: {json.dumps(e)}\n\n".encode() for e in events)


def completed_reply(text: str = "hello", model: str = "gpt-5.5") -> bytes:
    """A streamed Responses answer: one text delta, the finished item, ``response.completed``."""
    item = {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }
    return sse(
        {"type": "response.created", "response": {"id": "resp_1", "model": model}},
        {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "output_index": 0,
            "delta": text,
        },
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "model": model,
                "status": "completed",
                "output": [item],
                "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
            },
        },
    )


@dataclass
class FakeBackend:
    """The Codex backend: ``/responses`` streams ``reply``; ``/models`` answers ``catalogue``."""

    reply: bytes = field(default_factory=completed_reply)
    catalogue: dict[str, Any] | None = None
    status: int = 200
    requests: list[httpx.Request] = field(default_factory=list[httpx.Request])

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "refused"})
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=self.catalogue or {"models": []})
        body = json.loads(request.content or b"{}")
        if not body.get("stream"):
            return httpx.Response(400, json={"detail": "Stream must be set to true"})
        return httpx.Response(
            200, content=self.reply, headers={"content-type": "text/event-stream"}
        )

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
