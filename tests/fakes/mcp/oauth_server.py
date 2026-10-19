"""A fake OAuth authorization server and protected MCP resource for P13-09 tests.

``FakeAuthServer.handle`` is an ``httpx2.MockTransport`` handler (and ``handle_v1`` the same for
``httpx``): protected-resource metadata, authorization-server metadata (optionally with a device
endpoint), dynamic registration, the token endpoint (authorization code with PKCE, refresh, device
code) and the protected resource itself, which answers 401 unless the bearer token is current.
``authorize(url)`` plays the browser's half: it checks the request and returns the redirect URL
with a fresh code. Every token is built at runtime.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import httpx2

RESOURCE = "https://mcp.example.test/mcp"
ISSUER = "https://auth.example.test"
PRM_URL = "https://mcp.example.test/.well-known/oauth-protected-resource/mcp"


def make_token(kind: str) -> str:
    return f"{kind}-" + secrets.token_hex(12)


def s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


@dataclass
class FakeAuthServer:
    device: bool = False
    iss_parameter: bool = True
    expires_in: int = 3600
    refuse_refresh: bool = False
    device_script: list[str] = field(default_factory=list[str])
    refresh_gate: asyncio.Event | None = None
    on_device_poll: Callable[[], None] | None = None
    registrations: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    token_requests: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    authorize_requests: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    resource_requests: list[str | None] = field(default_factory=list[str | None])
    pkce_checked: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    valid_access: set[str] = field(default_factory=set[str])
    refresh_tokens: set[str] = field(default_factory=set[str])
    codes: dict[str, dict[str, str]] = field(default_factory=dict[str, dict[str, str]])

    # -- counters -----------------------------------------------------------------------------

    def grants(self, kind: str) -> int:
        return sum(1 for r in self.token_requests if r.get("grant_type") == kind)

    # -- the browser's half -------------------------------------------------------------------

    def authorize(self, url: str) -> str:
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        self.authorize_requests.append(query)
        assert query["response_type"] == "code"
        assert query["code_challenge_method"] == "S256"
        code = make_token("code")
        self.codes[code] = query
        back: dict[str, str] = {"code": code, "state": query["state"]}
        if self.iss_parameter:
            back["iss"] = ISSUER
        return f"{query['redirect_uri']}?{urlencode(back)}"

    # -- endpoints ----------------------------------------------------------------------------

    def _issue(self) -> dict[str, Any]:
        access = make_token("access")
        refresh = make_token("refresh")
        self.valid_access.add(access)
        self.refresh_tokens.add(refresh)
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": self.expires_in,
            "refresh_token": refresh,
        }

    def _metadata(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "issuer": ISSUER,
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token",
            "registration_endpoint": f"{ISSUER}/register",
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
            "authorization_response_iss_parameter_supported": self.iss_parameter,
        }
        if self.device:
            data["device_authorization_endpoint"] = f"{ISSUER}/device"
        return data

    def _token(self, form: dict[str, str]) -> tuple[int, dict[str, Any]]:
        self.token_requests.append(form)
        grant = form.get("grant_type")
        if grant == "authorization_code":
            asked = self.codes.pop(form.get("code", ""), None)
            if asked is None:
                return 400, {"error": "invalid_grant"}
            verifier = form.get("code_verifier", "")
            self.pkce_checked.append((verifier, asked["code_challenge"]))
            if s256(verifier) != asked["code_challenge"]:
                return 400, {"error": "invalid_grant", "error_description": "pkce"}
            if form.get("redirect_uri") != asked["redirect_uri"]:
                return 400, {"error": "invalid_grant", "error_description": "redirect"}
            return 200, self._issue()
        if grant == "refresh_token":
            token = form.get("refresh_token", "")
            if self.refuse_refresh or token not in self.refresh_tokens:
                return 400, {"error": "invalid_grant"}
            self.refresh_tokens.discard(token)
            return 200, self._issue()
        if grant == "urn:ietf:params:oauth:grant-type:device_code":
            if self.on_device_poll is not None:
                self.on_device_poll()
            if self.device_script:
                return 400, {"error": self.device_script.pop(0)}
            return 200, self._issue()
        return 400, {"error": "unsupported_grant_type"}

    def route(self, method: str, url: str, headers: dict[str, str], body: bytes) -> tuple[int, Any]:
        parts = urlsplit(url)
        where = f"{parts.scheme}://{parts.netloc}{parts.path}"
        if (
            where == PRM_URL
            or where == "https://mcp.example.test/.well-known/oauth-protected-resource"
        ):
            return 200, {"resource": RESOURCE, "authorization_servers": [ISSUER]}
        if where.startswith(f"{ISSUER}/.well-known/oauth-authorization-server"):
            return 200, self._metadata()
        if where.startswith(f"{ISSUER}/.well-known/"):
            return 404, {}
        if where == f"{ISSUER}/register" and method == "POST":
            data = json.loads(body or b"{}")
            self.registrations.append(data)
            return 201, {**data, "client_id": f"client-{len(self.registrations)}"}
        if where == f"{ISSUER}/token" and method == "POST":
            form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            return self._token(form)
        if where == f"{ISSUER}/device" and method == "POST":
            return 200, {
                "device_code": make_token("device"),
                "user_code": "WDJB-MJHT",
                "verification_uri": f"{ISSUER}/activate",
                "expires_in": 600,
                "interval": 5,
            }
        if where == RESOURCE:
            sent = headers.get("authorization")
            self.resource_requests.append(sent)
            token = sent.removeprefix("Bearer ") if sent else ""
            if token in self.valid_access:
                return 200, {"ok": True}
            return 401, None
        return 404, {}

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        body = await request.aread()
        headers = {k.lower(): v for k, v in request.headers.items()}
        if (
            self.refresh_gate is not None
            and b"grant_type=refresh_token" in body
            and str(request.url).startswith(f"{ISSUER}/token")
        ):
            await self.refresh_gate.wait()
        status, payload = self.route(request.method, str(request.url), headers, body)
        if status == 401:
            challenge = f'Bearer resource_metadata="{PRM_URL}"'
            return httpx2.Response(401, headers={"WWW-Authenticate": challenge})
        return httpx2.Response(status, json=payload)

    def handle_v1(self, request: httpx.Request) -> httpx.Response:
        headers = {k.lower(): v for k, v in request.headers.items()}
        status, payload = self.route(request.method, str(request.url), headers, request.content)
        if status == 401:
            return httpx.Response(401)
        return httpx.Response(status, json=payload)


class MemorySecrets:
    """A writable in-memory secret store (``SecretStore``'s shape)."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values: dict[str, str] = dict(values or {})
        self.generation = 0

    def get(self, name: str, /) -> str | None:
        return self.values.get(name)

    def set(self, name: str, value: str, /) -> None:
        self.values[name] = value
        self.generation += 1

    def delete(self, name: str, /) -> bool:
        self.generation += 1
        return self.values.pop(name, None) is not None

    def known_values(self) -> list[str]:
        return list(self.values.values())
