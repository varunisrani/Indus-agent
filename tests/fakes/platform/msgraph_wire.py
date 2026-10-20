"""A fake Microsoft Graph for the change-notification receiver (P16-12).

- ``transport()``: an ``httpx`` mock transport serving the identity platform's token endpoint
  (client credentials; a wrong secret gets ``401 invalid_client``) and Graph's
  ``POST /v1.0/subscriptions`` / ``PATCH /v1.0/subscriptions/{id}``. Creating a subscription first
  runs Graph's validation call against the attached listener app (``validationToken`` must come
  back as plain text), then answers 201 with an id and the requested expiry (with seven
  fractional digits, as Graph writes it). Every call is kept in ``calls`` with the fake time.
- ``notify(items)`` POSTs a notification batch to the listener; ``validate(token)`` makes the
  validation call by hand. Requests come from ``client`` (default ``52.159.23.5``).
"""

from __future__ import annotations

import itertools
import json
import secrets
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from indus.gateway.http.serving import Asgi
from tests.fakes.clock import FakeClock

TENANT = "5ec7e7a1-0000-4000-8000-00000000c0de"
APP = "a99c0000-0000-4000-8000-00000000beef"
PATH = "/msgraph/webhook"
PUBLIC_URL = "https://hooks.example.org/msgraph/webhook"


def client_state() -> str:
    return "".join(["state", "-", "9a8b" * 6])


def app_secret() -> str:
    return "".join(["graph", "~app~", "77aa" * 4])


@dataclass
class GraphCall:
    method: str
    path: str
    body: dict[str, Any]
    at: datetime
    headers: dict[str, str] = field(default_factory=dict[str, str])


class GraphWire:
    def __init__(self, clock: FakeClock, *, secret: str | None = None) -> None:
        self.clock = clock
        self.secret = secret or app_secret()
        self.calls: list[GraphCall] = []
        self.subscriptions: dict[str, dict[str, Any]] = {}
        self.validations: list[httpx.Response] = []
        self.app: Asgi | None = None
        self.client: tuple[str, int] = ("52.159.23.5", 443)
        self.fail_next: list[int] = []
        self._ids = itertools.count(1)
        self._tokens: list[str] = []

    def attach(self, app: Asgi) -> None:
        self.app = app

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def tokens(self) -> list[str]:
        return list(self._tokens)

    def of(self, method: str) -> list[GraphCall]:
        return [c for c in self.calls if c.method == method and c.path.startswith("/v1.0/")]

    # -- the fake services -----------------------------------------------------------------------

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        raw = await request.aread()
        path = request.url.path
        if request.url.host == "login.microsoftonline.com":
            form = dict(urllib.parse.parse_qsl(raw.decode()))
            self.calls.append(GraphCall(request.method, path, form, self.clock.now()))
            if form.get("client_secret") != self.secret or form.get("client_id") != APP:
                return httpx.Response(401, json={"error": "invalid_client"})
            token = "".join(["gt", "-", secrets.token_hex(12)])
            self._tokens.append(token)
            return httpx.Response(200, json={"access_token": token, "expires_in": 3599})
        if request.url.host != "graph.microsoft.com":
            return httpx.Response(404)
        body: dict[str, Any] = json.loads(raw) if raw else {}
        headers = {k.lower(): v for k, v in request.headers.items()}
        self.calls.append(GraphCall(request.method, path, body, self.clock.now(), headers))
        if headers.get("authorization") != f"Bearer {self._tokens[-1] if self._tokens else ''}":
            return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}})
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0), json={"error": {"code": "Busy"}})
        if request.method == "POST" and path == "/v1.0/subscriptions":
            token = "graph-check-" + secrets.token_hex(6)
            answer = await self.validate(token)
            if answer.status_code != 200 or answer.text != token:
                return httpx.Response(
                    400, json={"error": {"code": "ValidationError", "message": "no echo"}}
                )
            sid = f"{secrets.token_hex(4)}-aaaa-4bbb-8ccc-{next(self._ids):012d}"
            entry = {"id": sid, **body, "expirationDateTime": _seven(body["expirationDateTime"])}
            self.subscriptions[sid] = entry
            return httpx.Response(201, json=entry)
        if request.method == "PATCH" and path.startswith("/v1.0/subscriptions/"):
            sid = path.rsplit("/", 1)[-1]
            if sid not in self.subscriptions:
                return httpx.Response(404, json={"error": {"code": "ResourceNotFound"}})
            entry = self.subscriptions[sid]
            entry["expirationDateTime"] = _seven(body["expirationDateTime"])
            return httpx.Response(200, json=entry)
        return httpx.Response(404)

    # -- calls into the listener -----------------------------------------------------------------

    async def _send(
        self, method: str, query: str = "", body: bytes = b"", path: str = PATH
    ) -> httpx.Response:
        if self.app is None:
            raise RuntimeError("attach the receiver's listener app first")
        transport = httpx.ASGITransport(app=self.app, client=self.client)  # pyright: ignore[reportArgumentType]
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            url = path + (f"?{query}" if query else "")
            headers = {"Content-Type": "application/json"} if body else {}
            return await client.request(method, url, content=body, headers=headers)

    async def validate(self, token: str, method: str = "POST") -> httpx.Response:
        answer = await self._send(method, urllib.parse.urlencode({"validationToken": token}))
        self.validations.append(answer)
        return answer

    async def notify(self, items: list[Any]) -> httpx.Response:
        return await self._send("POST", body=json.dumps({"value": items}).encode())

    async def post(self, body: bytes, query: str = "") -> httpx.Response:
        return await self._send("POST", query, body)

    async def get(self, query: str = "") -> httpx.Response:
        return await self._send("GET", query)


def _seven(value: str) -> str:
    """Graph writes expiry times with seven fractional digits."""
    return value.replace("Z", ".0000000Z") if "." not in value else value


def notification(state: str, n: int = 1, **over: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": f"note-{n}",
        "subscriptionId": "sub-1",
        "subscriptionExpirationDateTime": "2026-03-01T10:00:00.0000000Z",
        "clientState": state,
        "changeType": "created",
        "resource": f"users/u1/messages/m{n}",
        "resourceData": {"@odata.type": "#Microsoft.Graph.Message", "id": f"m{n}"},
        "tenantId": TENANT,
    }
    item.update(over)
    return {k: v for k, v in item.items() if v is not None}


__all__ = [
    "APP",
    "PATH",
    "PUBLIC_URL",
    "TENANT",
    "GraphCall",
    "GraphWire",
    "app_secret",
    "client_state",
    "notification",
]
