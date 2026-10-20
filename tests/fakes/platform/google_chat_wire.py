"""A fake Google for the Google Chat adapter (P16-13): Chat API, Pub/Sub, OAuth and JWK sets.

Everything runs in-process over one ``httpx.MockTransport`` (any host) on a `FakeChatPlatform`:

- ``POST /token``: the OAuth JWT-bearer grant. The assertion's RS256 signature is checked with the
  fake service account's public key, and its ``iss`` / ``aud`` / ``scope`` claims too; the answer
  is a runtime-built access token (``expires_in`` 3600). The platform "connects" here.
- ``GET /oauth2/v3/certs`` and ``GET /service_accounts/v1/jwk/<email>``: Google's JWK sets (the
  fake Google key, kid ``gk1``).
- Chat API v1: ``POST /v1/spaces/<s>/messages`` (text, ``thread.name``, cards v2 buttons become
  platform buttons), ``PATCH`` / ``DELETE`` ``/v1/spaces/<s>/messages/<m>``, ``GET /v1/spaces/<s>``,
  ``GET /v1/spaces:findDirectMessage?name=users/<u>``, ``GET /v1/media/<resource>``. Every call
  needs ``Authorization: Bearer <issued token>`` (401 otherwise). Errors use Google's envelope
  ``{"error": {"code", "message", "status"}}``.
- Pub/Sub v1: ``GET`` the subscription, ``:pull`` (renders queued platform inbound as base64 event
  JSON; waits for traffic when there is none) and ``:acknowledge`` (ack ids kept in ``acked``).
- HTTP callbacks: ``render(item)`` makes the classic Chat event JSON, signed with a Google ID token
  (``id_token``) for the configured audience flavour; ``deliver`` / ``pump`` post it into the
  adapter's listener app.

Platform chats map to spaces: a chat id that is already ``spaces/<s>`` is that space; a DM chat
(the user id, ``users/<u>``) is the space ``spaces/DM<u>``. Message ids are the platform's ids.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import itertools
import json
import re
from collections.abc import Mapping
from typing import Any, Literal
from urllib.parse import parse_qs

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from indus.gateway.adapters.google_chat.auth import (
    CHAT_ISSUER,
    b64url_decode,
    b64url_encode,
)
from tests.fakes.platform.core import (
    Button,
    FailureKind,
    FakeChatPlatform,
    FakeInbound,
    FakePlatformError,
)
from tests.fakes.platform.http_wire import ERROR_STATUS, ApiCall, Callback, HttpCallbackWire, api

SA_EMAIL = "indus-bot@indus-test.iam.gserviceaccount.com"
PROJECT_NUMBER = "123456789012"
ENDPOINT_URL = "https://bot.example.test/google-chat"
SUBSCRIPTION = "projects/indus-test/subscriptions/chat-events"
CALLBACK_PATH = "/google-chat"
TOKEN_URI = "https://oauth2.googleapis.com/token"
GOOGLE_KID = "gk1"
APP_USER = "users/app-bot"
MAX_LEN = 4096
_STATUS = {
    401: "UNAUTHENTICATED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    429: "RESOURCE_EXHAUSTED",
}


@functools.cache
def rsa_key(label: str) -> rsa.RSAPrivateKey:
    """One generated 2048-bit key per label per process (no key material in the source)."""
    del label
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def private_pem(key: rsa.RSAPrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


def service_account_json(
    key: rsa.RSAPrivateKey | None = None,
    *,
    email: str = SA_EMAIL,
    token_uri: str = TOKEN_URI,
    **extra: Any,
) -> str:
    """A service-account key file's JSON, built at runtime."""
    data: dict[str, Any] = {
        "type": "service_account",
        "project_id": "indus-test",
        "private_key_id": "sa-key-1",
        "private_key": private_pem(key or rsa_key("service-account")),
        "client_email": email,
        "client_id": "1000",
        "token_uri": token_uri,
    }
    data.update(extra)
    return json.dumps(data)


def jwk_of(key: rsa.RSAPrivateKey, kid: str) -> dict[str, str]:
    numbers = key.public_key().public_numbers()

    def b64int(value: int) -> str:
        return b64url_encode(value.to_bytes((value.bit_length() + 7) // 8, "big"))

    return {
        "kty": "RSA",
        "alg": "RS256",
        "use": "sig",
        "kid": kid,
        "n": b64int(numbers.n),
        "e": b64int(numbers.e),
    }


def sign_jwt(key: rsa.RSAPrivateKey, header: Mapping[str, Any], claims: Mapping[str, Any]) -> str:
    signing_input = ".".join(
        b64url_encode(json.dumps(dict(part), separators=(",", ":")).encode())
        for part in (header, claims)
    )
    signature = key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{b64url_encode(signature)}"


def unsigned_jwt(
    header: Mapping[str, Any], claims: Mapping[str, Any], signature: bytes = b""
) -> str:
    return ".".join(
        [
            b64url_encode(json.dumps(dict(header)).encode()),
            b64url_encode(json.dumps(dict(claims)).encode()),
            b64url_encode(signature) if signature else "",
        ]
    )


def _error(status: int, message: str, headers: Mapping[str, str] | None = None) -> httpx.Response:
    body = {"error": {"code": status, "message": message, "status": _STATUS.get(status, "ERROR")}}
    return httpx.Response(status, json=body, headers=dict(headers or {}))


class _Transport(httpx.MockTransport):
    def __init__(self, wire: GoogleChatWire) -> None:
        super().__init__(wire._handle)  # pyright: ignore[reportPrivateUsage]
        self._wire = wire

    async def aclose(self) -> None:
        await self._wire.platform.disconnect()


class GoogleChatWire(HttpCallbackWire):
    """The fake Google (see the module notes)."""

    base_url = "https://chat.googleapis.com"

    def __init__(
        self,
        platform: FakeChatPlatform,
        *,
        audience: str = PROJECT_NUMBER,
        flavour: Literal["project_number", "url"] = "project_number",
    ) -> None:
        super().__init__(platform)
        self.audience = audience
        self.flavour = flavour
        self.sa_key = rsa_key("service-account")
        self.google_key = rsa_key("google")
        self.account_json = service_account_json(self.sa_key)
        self.access_token = "-".join(["ya29", "fake", "access", "token", "0001"])
        self.tokens_issued = 0
        self.assertion_claims: list[dict[str, Any]] = []
        self.jwks_fetches = 0
        self.hosts: list[str] = []
        self.subscription_status = 200
        self.pull_failures: list[int | BaseException] = []
        self.pull_calls = 0
        self.acked: list[str] = []
        self.media: dict[str, bytes] = {}
        self.extra_events: list[dict[str, Any]] = []
        self._chat_of: dict[str, str] = {}
        self._ids = itertools.count(1)
        self._ready = asyncio.Event()

    # -- wiring ---------------------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return _Transport(self)

    def space_for(self, chat_id: str) -> str:
        if chat_id.startswith("spaces/"):
            space = chat_id
        else:
            space = "spaces/DM" + re.sub(r"[^A-Za-z0-9]", "", chat_id)
        self._chat_of.setdefault(space, chat_id)
        return space

    def chat_for(self, space: str) -> str:
        return self._chat_of.get(space, space)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        self.hosts.append(request.url.host)
        return await super()._handle(request)

    def _authorized(self, call: ApiCall) -> bool:
        return call.headers.get("authorization") == f"Bearer {self.access_token}"

    def error_response(self, exc: FakePlatformError) -> httpx.Response:
        headers: dict[str, str] = {}
        if exc.kind is FailureKind.RATE_LIMIT and exc.retry_after is not None:
            headers["Retry-After"] = str(int(exc.retry_after))
        return _error(ERROR_STATUS.get(exc.kind, 500), exc.text, headers)

    # -- OAuth and keys ---------------------------------------------------------------------------

    @api("POST", "/token")
    async def token(self, call: ApiCall) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(call.body.decode()).items()}
        if form.get("grant_type") != "urn:ietf:params:oauth:grant-type:jwt-bearer":
            return httpx.Response(400, json={"error": "unsupported_grant_type"})
        parts = form.get("assertion", "").split(".")
        if len(parts) != 3:
            return httpx.Response(400, json={"error": "invalid_grant"})
        try:
            self.sa_key.public_key().verify(
                b64url_decode(parts[2]),
                f"{parts[0]}.{parts[1]}".encode(),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except Exception:
            return httpx.Response(400, json={"error": "invalid_grant"})
        claims = json.loads(b64url_decode(parts[1]))
        self.assertion_claims.append(claims)
        if claims.get("iss") != SA_EMAIL or claims.get("aud") != TOKEN_URI:
            return httpx.Response(400, json={"error": "invalid_grant"})
        if "https://www.googleapis.com/auth/chat.bot" not in str(claims.get("scope", "")).split():
            return httpx.Response(400, json={"error": "invalid_scope"})
        await self.platform.connect()
        self.tokens_issued += 1
        return httpx.Response(
            200,
            json={"access_token": self.access_token, "expires_in": 3600, "token_type": "Bearer"},
        )

    def _jwks(self) -> httpx.Response:
        self.jwks_fetches += 1
        return httpx.Response(200, json={"keys": [jwk_of(self.google_key, GOOGLE_KID)]})

    @api("GET", "/oauth2/v3/certs")
    async def oauth_certs(self, call: ApiCall) -> httpx.Response:
        return self._jwks()

    @api("GET", "/service_accounts/v1/jwk/{email}")
    async def account_jwks(self, call: ApiCall, email: str) -> httpx.Response:
        if email != CHAT_ISSUER:
            return _error(404, "unknown account")
        return self._jwks()

    async def fetch_keys(self, url: str) -> Mapping[str, Any]:
        """An injectable key fetcher (the same JWK set, no HTTP)."""
        self.jwks_fetches += 1
        return {"keys": [jwk_of(self.google_key, GOOGLE_KID)]}

    def id_token(self, **overrides: Any) -> str:
        """A token Google would put on a callback for the configured audience flavour."""
        now = int(self.platform.clock.now().timestamp())
        if self.flavour == "project_number":
            claims: dict[str, Any] = {"iss": CHAT_ISSUER, "aud": self.audience}
        else:
            claims = {
                "iss": "https://accounts.google.com",
                "aud": self.audience,
                "email": CHAT_ISSUER,
                "email_verified": True,
            }
        claims.update({"iat": now, "exp": now + 3600})
        header: dict[str, Any] = {"alg": "RS256", "typ": "JWT", "kid": GOOGLE_KID}
        key = overrides.pop("key", self.google_key)
        header.update(overrides.pop("header", {}))
        claims.update(overrides)
        return sign_jwt(key, header, {k: v for k, v in claims.items() if v is not None})

    # -- Chat API ---------------------------------------------------------------------------------

    @api("POST", "/v1/spaces/{space}/messages")
    async def create_message(self, call: ApiCall, space: str) -> httpx.Response:
        if not self._authorized(call):
            return _error(401, "Request had invalid authentication credentials.")
        name = f"spaces/{space}"
        body = call.json()
        buttons: list[Button] = []
        for card in body.get("cardsV2") or ():
            for section in card["card"]["sections"]:
                for widget in section["widgets"]:
                    for button in (widget.get("buttonList") or {}).get("buttons") or ():
                        action = button["onClick"]["action"]
                        data = {p["key"]: p["value"] for p in action["parameters"]}["data"]
                        buttons.append(Button(button["text"], data))
        thread = (body.get("thread") or {}).get("name")
        sent = await self.platform.post(
            self.chat_for(name),
            body.get("text") or "",
            thread_id=thread,
            buttons=tuple(buttons),
            extra={"params": dict(call.params)},
        )
        answer: dict[str, Any] = {"name": f"{name}/messages/{sent.message_id}"}
        answer["thread"] = {"name": thread or f"{name}/threads/t{sent.message_id}"}
        return httpx.Response(200, json=answer)

    @api("PATCH", "/v1/spaces/{space}/messages/{message}")
    async def patch_message(self, call: ApiCall, space: str, message: str) -> httpx.Response:
        if not self._authorized(call):
            return _error(401, "Request had invalid authentication credentials.")
        body = call.json()
        mask = call.params.get("updateMask", "").split(",")
        buttons: tuple[Button, ...] | None = () if "cardsV2" in mask else None
        await self.platform.edit(
            self.chat_for(f"spaces/{space}"), message, body.get("text") or "", buttons=buttons
        )
        return httpx.Response(200, json={"name": f"spaces/{space}/messages/{message}"})

    @api("DELETE", "/v1/spaces/{space}/messages/{message}")
    async def delete_message(self, call: ApiCall, space: str, message: str) -> httpx.Response:
        if not self._authorized(call):
            return _error(401, "Request had invalid authentication credentials.")
        await self.platform.delete(self.chat_for(f"spaces/{space}"), message)
        return httpx.Response(200, json={})

    @api("GET", "/v1/spaces/{space}")
    async def get_space(self, call: ApiCall, space: str) -> httpx.Response:
        if not self._authorized(call):
            return _error(401, "Request had invalid authentication credentials.")
        chat = await self.platform.chat(self.chat_for(f"spaces/{space}"))
        kind = "DIRECT_MESSAGE" if chat.kind == "dm" else "SPACE"
        return httpx.Response(
            200, json={"name": f"spaces/{space}", "displayName": chat.name, "spaceType": kind}
        )

    @api("GET", "/v1/spaces:findDirectMessage")
    async def find_dm(self, call: ApiCall) -> httpx.Response:
        if not self._authorized(call):
            return _error(401, "Request had invalid authentication credentials.")
        user = call.params.get("name", "")
        if not user.startswith("users/"):
            return _error(400, "invalid name")
        return httpx.Response(
            200, json={"name": self.space_for(user), "spaceType": "DIRECT_MESSAGE"}
        )

    @api("GET", "/v1/media/{resource}")
    async def download(self, call: ApiCall, resource: str) -> httpx.Response:
        if not self._authorized(call):
            return _error(401, "Request had invalid authentication credentials.")
        if call.params.get("alt") != "media" or resource not in self.media:
            return _error(404, "Requested entity was not found.")
        return httpx.Response(200, content=self.media[resource])

    # -- Pub/Sub ----------------------------------------------------------------------------------

    @api("GET", "/v1/projects/{project}/subscriptions/{sub}")
    async def get_subscription(self, call: ApiCall, project: str, sub: str) -> httpx.Response:
        if not self._authorized(call):
            return _error(401, "Request had invalid authentication credentials.")
        if self.subscription_status != 200:
            return _error(self.subscription_status, "subscription problem")
        return httpx.Response(200, json={"name": f"projects/{project}/subscriptions/{sub}"})

    @api("POST", "/v1/projects/{project}/subscriptions/{sub}:pull")
    async def pull(self, call: ApiCall, project: str, sub: str) -> httpx.Response:
        self.pull_calls += 1
        if not self._authorized(call):
            return _error(401, "Request had invalid authentication credentials.")
        if self.pull_failures:
            failure = self.pull_failures.pop(0)
            if isinstance(failure, BaseException):
                raise failure
            return _error(failure, "pull failed")
        limit = int((call.json() or {}).get("maxMessages") or 1)
        events = await self._next_events(limit)
        received = [
            {
                "ackId": f"ack{next(self._ids)}",
                "message": {
                    "data": base64.b64encode(json.dumps(event).encode()).decode(),
                    "messageId": str(next(self._ids)),
                    "attributes": {},
                },
            }
            for event in events
        ]
        return httpx.Response(200, json={"receivedMessages": received} if received else {})

    @api("POST", "/v1/projects/{project}/subscriptions/{sub}:acknowledge")
    async def acknowledge(self, call: ApiCall, project: str, sub: str) -> httpx.Response:
        if not self._authorized(call):
            return _error(401, "Request had invalid authentication credentials.")
        self.acked.extend(call.json().get("ackIds") or ())
        return httpx.Response(200, json={})

    def push_event(self, event: Mapping[str, Any]) -> None:
        """Queue a raw event for the next pull."""
        self.extra_events.append(dict(event))
        self._ready.set()

    async def _next_events(self, limit: int) -> list[dict[str, Any]]:
        while True:
            events: list[dict[str, Any]] = []
            while self.extra_events and len(events) < limit:
                events.append(self.extra_events.pop(0))
            while len(events) < limit and (item := self.platform.next_inbound()) is not None:
                rendered = self.event_for(item)
                if rendered is not None:
                    events.append(rendered)
            if events:
                return events
            self._ready.clear()
            getter = asyncio.ensure_future(self.platform.receive_inbound())
            ready = asyncio.ensure_future(self._ready.wait())
            try:
                done, _ = await asyncio.wait({getter, ready}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (getter, ready):
                    if not task.done():
                        task.cancel()
            if getter in done and not getter.cancelled():
                rendered = self.event_for(getter.result())
                if rendered is not None:
                    return [rendered]

    # -- callbacks --------------------------------------------------------------------------------

    def _sender(self, item: FakeInbound) -> dict[str, Any]:
        local = re.sub(r"[^A-Za-z0-9]", "", item.user_id) or "someone"
        return {
            "name": item.user_id,
            "displayName": item.user_name or item.user_id,
            "type": "BOT" if item.is_bot else "HUMAN",
            "email": f"{local}@example.test",
        }

    def event_for(self, item: FakeInbound) -> dict[str, Any] | None:
        """The classic Chat event JSON for ``item`` (None for edits: Chat sends none)."""
        if item.event == "edited":
            return None
        name = self.space_for(item.chat_id)
        space = {
            "name": name,
            "spaceType": "DIRECT_MESSAGE" if item.chat_kind == "dm" else "SPACE",
            "displayName": item.chat_name,
        }
        sender = self._sender(item)
        when = item.at.isoformat().replace("+00:00", "Z")
        if item.event == "button":
            return {
                "type": "CARD_CLICKED",
                "eventTime": when,
                "space": space,
                "user": sender,
                "message": {"name": f"{name}/messages/{item.message_id or 'card'}"},
                "action": {
                    "actionMethodName": "indus.button",
                    "parameters": [{"key": "data", "value": item.button_data}],
                },
                "common": {"invokedFunction": "indus.button"},
            }
        text = item.media[0].caption if item.media else item.text
        message: dict[str, Any] = {
            "name": f"{name}/messages/{item.message_id}",
            "sender": sender,
            "createTime": when,
            "text": text,
            "argumentText": text,
            "space": space,
        }
        if item.thread_id:
            message["thread"] = {"name": item.thread_id}
            message["threadReply"] = True
        else:
            message["thread"] = {"name": f"{name}/threads/t{item.message_id}"}
        if item.mentions_bot:
            message["text"] = f"@IndusBot {text}"
            message["argumentText"] = f" {text}"
            message["annotations"] = [
                {
                    "type": "USER_MENTION",
                    "startIndex": 0,
                    "length": 9,
                    "userMention": {"user": {"name": APP_USER, "type": "BOT"}, "type": "MENTION"},
                }
            ]
        attachments: list[dict[str, Any]] = []
        for media in item.media:
            resource = f"att{next(self._ids)}"
            self.media[resource] = f"bytes of {media.ref}".encode()
            attachments.append(
                {
                    "name": f"{message['name']}/attachments/{resource}",
                    "contentName": media.ref,
                    "contentType": media.mime,
                    "attachmentDataRef": {"resourceName": resource},
                    "source": "UPLOADED_CONTENT",
                }
            )
        if attachments:
            message["attachment"] = attachments
        return {
            "type": "MESSAGE",
            "eventTime": when,
            "space": space,
            "message": message,
            "user": sender,
        }

    def callback(self, event: Mapping[str, Any], *, token: str | None = None) -> Callback:
        body = json.dumps(dict(event)).encode()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token if token is not None else self.id_token()}",
        }
        return Callback(CALLBACK_PATH, body, headers)

    def render(self, item: FakeInbound) -> Callback | None:
        event = self.event_for(item)
        return None if event is None else self.callback(event)


__all__ = [
    "APP_USER",
    "CALLBACK_PATH",
    "ENDPOINT_URL",
    "GOOGLE_KID",
    "MAX_LEN",
    "PROJECT_NUMBER",
    "SA_EMAIL",
    "SUBSCRIPTION",
    "TOKEN_URI",
    "GoogleChatWire",
    "jwk_of",
    "private_pem",
    "rsa_key",
    "service_account_json",
    "sign_jwt",
    "unsigned_jwt",
]
