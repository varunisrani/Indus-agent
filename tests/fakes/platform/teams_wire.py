"""A fake Bot Framework for the Teams adapter (P16-11), on the P16-01 callback wire base.

What it serves, over one ``httpx`` mock transport (no socket):

- the identity platform's token endpoint ``POST /<tenant>/oauth2/v2.0/token`` (client-credentials
  form; the right secret gets a fresh opaque access token and connects the fake platform, a wrong
  one ``401 invalid_client``);
- the Bot Framework OpenID metadata and key set (``/v1/.well-known/openidconfiguration``,
  ``/v1/.well-known/keys``), with the public half of the signer's RSA key (rotatable);
- the Bot Connector: ``POST /<region>/v3/conversations/<id>/activities`` (``message`` with text or
  an Adaptive Card, ``typing``) and ``PUT …/activities/<activity>`` (update); the bearer must be a
  token this fake issued; ``;messageid=<n>`` addresses a channel thread; errors in the Bot
  Framework shape (``{"error": {"code", "message"}}``);
- attachment downloads (``/<region>/v3/attachments/<id>/views/original`` needs the bearer;
  SharePoint-style ``/sites/files/<id>`` does not).

Inbound, ``render`` turns each platform-neutral `FakeInbound` into a Bot Framework activity POSTed
to ``/api/messages`` with a freshly signed RS256 token (``BotSigner``); ``deliver`` sends hand-made
ones (forged tokens, other service URLs). Closing the adapter's HTTP client is the platform's
disconnect. The RSA key comes from ``cryptography`` (installed with the base dependencies) and is
made once per process.
"""

from __future__ import annotations

import base64
import itertools
import json
import urllib.parse
from collections.abc import Mapping
from typing import Any

import httpx

from tests.fakes.platform.core import (
    Button,
    FailureKind,
    FakeChatPlatform,
    FakeInbound,
    FakeMedia,
    FakePlatformError,
)
from tests.fakes.platform.http_wire import ApiCall, Callback, HttpCallbackWire, api, json_body

APP_ID = "4d6f6e6b-0000-4000-8000-00000000a11d"
TENANT_ID = "7e6e616e-0000-4000-8000-0000000071d0"
ISSUER = "https://api.botframework.com"
SERVICE_URL = "https://smba.trafficmanager.net/teams/"
METADATA_URL = "https://login.botframework.com/v1/.well-known/openidconfiguration"
KEYS_URL = "https://login.botframework.com/v1/.well-known/keys"
TOKEN_URL = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
FILES_HOST = "https://contoso.sharepoint.com"
BOT_NAME = "IndusBot"
MAX_LEN = 28_000
CARD_TYPE = "application/vnd.microsoft.card.adaptive"

_KINDS = {"dm": "personal", "group": "groupChat", "channel": "channel", "thread": "channel"}
_MEDIA = {
    "voice": ("audio/ogg", "voice.ogg"),
    "audio": ("audio/mpeg", "song.mp3"),
    "photo": ("image/png", "photo.png"),
    "video": ("video/mp4", "clip.mp4"),
    "sticker": ("image/png", "sticker.png"),
}
_ERRORS: dict[FailureKind, tuple[int, str]] = {
    FailureKind.RATE_LIMIT: (429, "TooManyRequests"),
    FailureKind.CHAT_NOT_FOUND: (404, "ConversationNotFound"),
    FailureKind.THREAD_NOT_FOUND: (404, "NotFound"),
    FailureKind.FORBIDDEN: (403, "BotNotInConversationRoster"),
    FailureKind.PARSE_ERROR: (400, "BadSyntax"),
    FailureKind.TOO_LONG: (413, "MessageSizeTooBig"),
}


def secret() -> str:
    """The app secret, built at runtime (never a credential-shaped literal)."""
    return "".join(["teams", "~app~", "e5f6" * 4])


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _int_b64(value: int) -> str:
    return b64url(value.to_bytes((value.bit_length() + 7) // 8, "big"))


_KEYS: dict[int, Any] = {}


def rsa_private_key(bits: int = 2048, slot: int = 0) -> Any:
    """An RSA private key, made once per process per (bits, slot)."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = _KEYS.get(bits * 10 + slot)
    if key is None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=bits)
        _KEYS[bits * 10 + slot] = key
    return key


class BotSigner:
    """Signs Bot Connector tokens with one RSA key (``kid``), publishes its JWK."""

    def __init__(self, kid: str = "bf-key-1", *, bits: int = 2048, slot: int = 0) -> None:
        self.kid = kid
        self.key = rsa_private_key(bits, slot)

    def jwk(self, endorsements: tuple[str, ...] | None = ("msteams",)) -> dict[str, Any]:
        numbers = self.key.public_key().public_numbers()
        entry: dict[str, Any] = {
            "kty": "RSA",
            "use": "sig",
            "kid": self.kid,
            "n": _int_b64(numbers.n),
            "e": _int_b64(numbers.e),
        }
        if endorsements is not None:
            entry["endorsements"] = list(endorsements)
        return entry

    def sign_raw(self, data: bytes) -> bytes:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        return self.key.sign(data, padding.PKCS1v15(), hashes.SHA256())

    def token(self, claims: Mapping[str, Any], **header: Any) -> str:
        head = {"alg": "RS256", "typ": "JWT", "kid": self.kid, **header}
        signing_input = (
            b64url(json.dumps(head).encode()) + "." + b64url(json.dumps(dict(claims)).encode())
        )
        return signing_input + "." + b64url(self.sign_raw(signing_input.encode("ascii")))


def claims_for(now: float, **overrides: Any) -> dict[str, Any]:
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": APP_ID,
        "exp": int(now) + 3600,
        "nbf": int(now) - 60,
        "serviceUrl": SERVICE_URL,
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


class _ClosingTransport(httpx.MockTransport):
    """Closing the adapter's HTTP client is the platform's disconnect."""

    def __init__(self, wire: TeamsWire) -> None:
        super().__init__(wire._handle)  # pyright: ignore[reportPrivateUsage]
        self._wire = wire

    async def aclose(self) -> None:
        await self._wire.platform.disconnect()


class TeamsWire(HttpCallbackWire):
    """The fake Bot Framework (see the module notes)."""

    base_url = SERVICE_URL

    def __init__(self, platform: FakeChatPlatform, *, app_secret: str | None = None) -> None:
        super().__init__(platform)
        self.secret = app_secret or secret()
        self.signer = BotSigner()
        self.published: list[dict[str, Any]] = [self.signer.jwk()]
        self.service_url = SERVICE_URL
        self.issued: list[str] = []
        self.token_requests = 0
        self.key_requests = 0
        self.regions: list[str] = []
        self.bodies: list[tuple[str, dict[str, Any]]] = []
        self.downloads: list[ApiCall] = []
        self.files: dict[str, bytes] = {}
        self.refuse_cards = False
        self._tokens = itertools.count(1)

    def transport(self) -> httpx.MockTransport:
        return _ClosingTransport(self)

    # -- identity platform -----------------------------------------------------------------------

    @api("POST", "/{tenant}/oauth2/v2.0/token")
    async def token(self, call: ApiCall, tenant: str) -> httpx.Response:
        self.token_requests += 1
        form = dict(urllib.parse.parse_qsl(call.body.decode()))
        good = (
            tenant == TENANT_ID
            and form.get("grant_type") == "client_credentials"
            and form.get("client_id") == APP_ID
            and form.get("client_secret") == self.secret
            and form.get("scope") == "https://api.botframework.com/.default"
        )
        if not good:
            return httpx.Response(401, json={"error": "invalid_client", "error_description": "x"})
        await self.platform.connect()
        issued = "".join(["at", "-", str(next(self._tokens)), "-", "f00d" * 6])
        self.issued.append(issued)
        return httpx.Response(
            200, json={"token_type": "Bearer", "expires_in": 3599, "access_token": issued}
        )

    @api("GET", "/v1/.well-known/openidconfiguration")
    async def metadata(self, call: ApiCall) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "issuer": ISSUER,
                "jwks_uri": KEYS_URL,
                "id_token_signing_alg_values_supported": ["RS256"],
            },
        )

    @api("GET", "/v1/.well-known/keys")
    async def keys(self, call: ApiCall) -> httpx.Response:
        self.key_requests += 1
        return httpx.Response(200, json={"keys": self.published})

    def rotate(self, kid: str = "bf-key-2") -> BotSigner:
        """Sign with a new key from now on and publish it alongside the old one."""
        self.signer = BotSigner(kid, slot=1)
        self.published = [*self.published, self.signer.jwk()]
        return self.signer

    # -- Bot Connector ---------------------------------------------------------------------------

    def _authorised(self, call: ApiCall) -> bool:
        header = call.headers.get("authorization", "")
        return bool(self.issued) and header == f"Bearer {self.issued[-1]}"

    @staticmethod
    def _split(conversation: str) -> tuple[str, str | None]:
        base, mark, thread = urllib.parse.unquote(conversation).partition(";messageid=")
        return base, (thread if mark else None)

    @api("POST", "/{region}/v3/conversations/{conversation}/activities")
    async def post_activity(self, call: ApiCall, region: str, conversation: str) -> httpx.Response:
        if not self._authorised(call):
            return httpx.Response(401, json={"error": {"code": "Unauthorized", "message": "no"}})
        self.regions.append(region)
        chat, thread = self._split(conversation)
        body = call.json()
        self.bodies.append((chat, body))
        if body.get("type") == "typing":
            await self.platform.typing(chat, thread_id=thread)
            return httpx.Response(200, json={})
        cards = [a for a in body.get("attachments") or () if a.get("contentType") == CARD_TYPE]
        images = [
            a
            for a in body.get("attachments") or ()
            if str(a.get("contentUrl") or "").startswith("data:image/")
        ]
        if images:
            media = FakeMedia("photo", images[0].get("name") or "", caption=body.get("text") or "")
            sent = await self.platform.upload(chat, media, thread_id=thread)
        elif cards:
            if self.refuse_cards:
                return httpx.Response(
                    400, json={"error": {"code": "BadArgument", "message": "card rejected"}}
                )
            content = cards[0]["content"]
            text = "\n".join(b.get("text", "") for b in content.get("body") or ())
            buttons = tuple(
                _button(a["title"], a.get("data") or {}) for a in content.get("actions") or ()
            )
            sent = await self.platform.post(chat, text, thread_id=thread, buttons=buttons)
        else:
            sent = await self.platform.post(
                chat, body.get("text") or "", reply_to=body.get("replyToId"), thread_id=thread
            )
        return httpx.Response(201, json={"id": sent.message_id})

    @api("PUT", "/{region}/v3/conversations/{conversation}/activities/{activity}")
    async def update_activity(
        self, call: ApiCall, region: str, conversation: str, activity: str
    ) -> httpx.Response:
        if not self._authorised(call):
            return httpx.Response(401, json={"error": {"code": "Unauthorized", "message": "no"}})
        chat, _ = self._split(conversation)
        body = call.json()
        await self.platform.edit(chat, activity, body.get("text") or "")
        return httpx.Response(200, json={"id": activity})

    @api("GET", "/{region}/v3/attachments/{attachment}/views/original")
    async def bot_download(self, call: ApiCall, region: str, attachment: str) -> httpx.Response:
        self.downloads.append(call)
        if not self._authorised(call):
            return httpx.Response(401)
        return httpx.Response(200, content=self.files.get(attachment, b"\x89PNG fake"))

    @api("GET", "/sites/files/{name}")
    async def file_download(self, call: ApiCall, name: str) -> httpx.Response:
        self.downloads.append(call)
        return httpx.Response(200, content=self.files.get(name, b"%PDF-1.7 fake"))

    def error_response(self, exc: FakePlatformError) -> httpx.Response:
        status, code = _ERRORS.get(exc.kind, (500, "InternalServerError"))
        headers: dict[str, str] = {}
        if exc.kind is FailureKind.RATE_LIMIT and exc.retry_after is not None:
            headers["Retry-After"] = str(int(exc.retry_after))
        return httpx.Response(
            status, json={"error": {"code": code, "message": exc.text}}, headers=headers
        )

    # -- inbound ---------------------------------------------------------------------------------

    def now(self) -> float:
        return self.platform.clock.now().timestamp()

    def bearer(self, **claims: Any) -> str:
        return "Bearer " + self.signer.token(claims_for(self.now(), **claims))

    def activity(self, item: FakeInbound) -> dict[str, Any]:
        thread = item.thread_id
        conversation = f"{item.chat_id};messageid={thread}" if thread else item.chat_id
        kind = "channel" if thread else _KINDS.get(item.chat_kind, "personal")
        if item.is_bot:
            sender: dict[str, Any] = {
                "id": f"28:{item.user_id}",
                "name": item.user_name,
                "role": "bot",
            }
        else:
            sender = {
                "id": f"29:{item.user_id}",
                "name": item.user_name,
                "aadObjectId": item.user_id,
                "role": "user",
            }
        text = item.text
        entities: list[dict[str, Any]] = []
        if item.mentions_bot:
            tag = f"<at>{BOT_NAME}</at>"
            text = f"{tag} {text}"
            entities.append(
                {
                    "type": "mention",
                    "text": tag,
                    "mentioned": {"id": f"28:{APP_ID}", "name": BOT_NAME},
                }
            )
        activity: dict[str, Any] = {
            "type": "message",
            "id": item.message_id or f"act{item.update_id}",
            "timestamp": item.at.isoformat().replace("+00:00", "Z"),
            "channelId": "msteams",
            "serviceUrl": self.service_url,
            "from": sender,
            "conversation": {
                "id": conversation,
                "conversationType": kind,
                "tenantId": TENANT_ID,
                **({"name": item.chat_name} if item.chat_name else {}),
            },
            "recipient": {"id": f"28:{APP_ID}", "name": BOT_NAME},
            "text": text,
            "textFormat": "plain",
            "entities": entities,
            "attachments": [self._attachment(m.kind, m.ref) for m in item.media],
            "channelData": {"tenant": {"id": TENANT_ID}},
        }
        if item.reply_to:
            activity["replyToId"] = item.reply_to
        if item.event == "edited":
            activity["type"] = "messageUpdate"
            activity["channelData"]["eventType"] = "editMessage"
        if item.event == "button":
            activity["type"] = "invoke"
            activity["name"] = "adaptiveCard/action"
            activity["id"] = f"inv{item.update_id}"
            activity["value"] = {
                "action": {
                    "type": "Action.Execute",
                    "verb": "indus.approval",
                    "data": {"indus_callback": item.button_data},
                }
            }
            activity.pop("text")
        return activity

    def _attachment(self, kind: str, ref: str) -> dict[str, Any]:
        if kind == "document":
            return {
                "contentType": "application/vnd.microsoft.teams.file.download.info",
                "name": "report.pdf",
                "content": {"downloadUrl": f"{FILES_HOST}/sites/files/{ref}", "fileType": "pdf"},
            }
        mime, name = _MEDIA.get(kind, ("application/octet-stream", "file.bin"))
        return {
            "contentType": mime,
            "name": name,
            "contentUrl": f"{SERVICE_URL}v3/attachments/{ref}/views/original",
        }

    def signed(self, activity: Mapping[str, Any], *, authorization: str | None = None) -> Callback:
        headers = {
            "Content-Type": "application/json",
            "Authorization": authorization if authorization is not None else self.bearer(),
        }
        return Callback("/api/messages", json_body(dict(activity)), headers)

    def render(self, item: FakeInbound) -> Callback | None:
        return self.signed(self.activity(item))


def _button(title: str, data: Mapping[str, Any]) -> Button:
    return Button(title, str(data.get("indus_callback") or ""))


__all__ = [
    "APP_ID",
    "BOT_NAME",
    "FILES_HOST",
    "KEYS_URL",
    "MAX_LEN",
    "METADATA_URL",
    "SERVICE_URL",
    "TENANT_ID",
    "TOKEN_URL",
    "BotSigner",
    "TeamsWire",
    "b64url",
    "claims_for",
    "rsa_private_key",
    "secret",
]
