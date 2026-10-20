"""A fake Twilio Programmable Messaging API (REST + signed webhooks) over `FakeChatPlatform`
(P16-10).

REST (HTTP Basic auth with the account SID and auth token; anything else answers Twilio error
20003 with HTTP 401):

- ``GET /2010-04-01/Accounts/<sid>.json`` → the account (the platform connects);
- ``POST /2010-04-01/Accounts/<sid>/Messages.json`` (form ``To``, ``From``, ``Body``) → 201 with
  the message ``sid`` (the platform's message id); ``From`` must be the account's number; a
  ``To`` in ``unsubscribed`` answers 21610, as Twilio does after the person texted STOP;
- ``GET /2010-04-01/Accounts/<sid>/Messages/<sid>/Media/<name>`` → the MMS bytes, by redirect to
  a CDN host when ``redirect_media`` is set (as Twilio does).

Closing the adapter's client disconnects the platform. Armed platform failures answer in
Twilio's error shape ``{code, message, more_info, status}`` (``error_response``).

Webhooks: ``render`` turns a platform-neutral `FakeInbound` into Twilio's form-encoded incoming
message, signed with ``X-Twilio-Signature`` over ``public_url`` (+ ``query``). SMS is 1:1 text
without bots or edits, so group, thread, bot and edit items render as None; media become MMS
fields. ``callback`` signs any hand-made field list for unit tests.
"""

from __future__ import annotations

import base64
import urllib.parse
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping
from typing import Any

import httpx

from indus.gateway.adapters._http import hmac_sha1_url_signature
from indus.gateway.adapters.sms.adapter import API_VERSION, DEFAULT_PATH, MAX_LEN
from tests.fakes.platform.core import FailureKind, FakeChatPlatform, FakeInbound, FakePlatformError
from tests.fakes.platform.http_wire import ApiCall, Callback, HttpCallbackWire, api

__all__ = [
    "ACCOUNT_SID",
    "AUTH_TOKEN",
    "MAX_LEN",
    "PHONE_NUMBER",
    "PUBLIC_URL",
    "TwilioWire",
]

# Built at runtime: never a credential-shaped literal in the source.
ACCOUNT_SID = "".join(["AC", "0f1e2d3c" * 4])
AUTH_TOKEN = "".join(["twl", "-auth-", "9a8b" * 6])
PHONE_NUMBER = "+15550009999"
PUBLIC_URL = "https://sms.indus.test" + DEFAULT_PATH

# Twilio error code, HTTP status and title per armed failure kind.
TWILIO_ERRORS: Mapping[FailureKind, tuple[int, int, str]] = {
    FailureKind.RATE_LIMIT: (20429, 429, "Too Many Requests"),
    FailureKind.CHAT_NOT_FOUND: (21211, 400, "Invalid 'To' Phone Number"),
    FailureKind.THREAD_NOT_FOUND: (20404, 404, "The requested resource was not found"),
    FailureKind.FORBIDDEN: (21610, 400, "Attempt to send to unsubscribed recipient"),
    FailureKind.PARSE_ERROR: (21602, 400, "Message body is required"),
    FailureKind.TOO_LONG: (21617, 400, "The concatenated message body exceeds the limit"),
}


def twilio_error(code: int, message: str, status: int = 400) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "code": code,
            "message": message,
            "more_info": f"https://www.twilio.com/docs/errors/{code}",
            "status": status,
        },
    )


class _ClosingTransport(httpx.MockTransport):
    """A mock transport whose close is the platform's disconnect."""

    def __init__(
        self,
        handler: Callable[[httpx.Request], Coroutine[Any, Any, httpx.Response]],
        on_close: Callable[[], Awaitable[None]],
    ) -> None:
        super().__init__(handler)
        self._on_close = on_close

    async def aclose(self) -> None:
        await self._on_close()


class TwilioWire(HttpCallbackWire):
    """Twilio's side of one SMS number (see the module notes)."""

    def __init__(
        self,
        platform: FakeChatPlatform,
        *,
        account_sid: str = ACCOUNT_SID,
        auth_token: str = AUTH_TOKEN,
        phone_number: str = PHONE_NUMBER,
        public_url: str = PUBLIC_URL,
    ) -> None:
        super().__init__(platform)
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.phone_number = phone_number
        self.public_url = public_url
        # The query string Twilio was told to call the webhook with (signed with the URL).
        self.query = ""
        self.unsubscribed: set[str] = set()
        self.media: dict[str, tuple[bytes, str]] = {}
        self.redirect_media = False
        self.closed = 0

    def transport(self) -> httpx.MockTransport:
        return _ClosingTransport(self._handle, self._closed)

    async def _closed(self) -> None:
        self.closed += 1
        await self.platform.disconnect()

    def _refused(self, call: ApiCall, account: str) -> httpx.Response | None:
        pair = f"{self.account_sid}:{self.auth_token}".encode()
        expected = "Basic " + base64.b64encode(pair).decode()
        if call.headers.get("authorization") != expected:
            return twilio_error(20003, "Authenticate", 401)
        if account != self.account_sid:
            return twilio_error(20404, "The requested resource was not found", 404)
        return None

    def media_url(self, message_sid: str, name: str) -> str:
        return (
            f"{self.base_url}/{API_VERSION}/Accounts/{self.account_sid}/Messages/{message_sid}"
            f"/Media/{name}"
        )

    # -- REST ----------------------------------------------------------------------------------

    @api("GET", "/2010-04-01/Accounts/{account}.json")
    async def account(self, call: ApiCall, account: str) -> httpx.Response:
        if (refused := self._refused(call, account)) is not None:
            return refused
        await self.platform.connect()
        return httpx.Response(200, json={"sid": account, "status": "active", "type": "Full"})

    @api("POST", "/2010-04-01/Accounts/{account}/Messages.json")
    async def messages(self, call: ApiCall, account: str) -> httpx.Response:
        if (refused := self._refused(call, account)) is not None:
            return refused
        form = dict(urllib.parse.parse_qsl(call.body.decode(), keep_blank_values=True))
        to, sender, body = form.get("To", ""), form.get("From", ""), form.get("Body", "")
        if sender != self.phone_number:
            return twilio_error(21606, "The 'From' phone number is not a valid number")
        if not body:
            return twilio_error(21602, "Message body is required")
        if to in self.unsubscribed:
            return twilio_error(
                21610, f"Attempt to send to unsubscribed recipient: The 'To' number {to}"
            )
        if len(body) > MAX_LEN:
            return twilio_error(21617, "The concatenated message body exceeds the 1600 limit")
        sent = await self.platform.post(to, body)
        return httpx.Response(
            201, json={"sid": sent.message_id, "status": "queued", "to": to, "from": sender}
        )

    @api("GET", "/2010-04-01/Accounts/{account}/Messages/{message}/Media/{name}")
    async def media_file(
        self, call: ApiCall, account: str, message: str, name: str
    ) -> httpx.Response:
        if (refused := self._refused(call, account)) is not None:
            return refused
        if self.redirect_media:
            return httpx.Response(307, headers={"location": f"https://cdn.media.test/{name}"})
        found = self.media.get(name)
        if found is None:
            return twilio_error(20404, "The requested resource was not found", 404)
        data, mime = found
        return httpx.Response(200, content=data, headers={"content-type": mime})

    @api("GET", "/{name}")
    async def cdn(self, call: ApiCall, name: str) -> httpx.Response:
        """The CDN a media redirect lands on (no credentials expected)."""
        found = self.media.get(name)
        if found is None:
            return httpx.Response(404)
        return httpx.Response(200, content=found[0], headers={"content-type": found[1]})

    def error_response(self, exc: FakePlatformError) -> httpx.Response:
        code, status, title = TWILIO_ERRORS.get(exc.kind, (20500, 500, "Internal Server Error"))
        response = twilio_error(code, f"{title}: {exc.text}", status)
        if exc.kind is FailureKind.RATE_LIMIT and exc.retry_after is not None:
            response.headers["Retry-After"] = str(int(exc.retry_after))
        return response

    # -- webhooks ------------------------------------------------------------------------------

    def fields_for(self, item: FakeInbound) -> list[tuple[str, str]] | None:
        """Twilio's incoming-message fields for ``item``, or None when SMS has no such event."""
        if item.event != "message" or item.chat_kind != "dm" or item.is_bot:
            return None
        sid = item.message_id or f"SM{item.update_id}"
        fields = [
            ("ToCountry", "US"),
            ("SmsMessageSid", sid),
            ("NumMedia", str(len(item.media))),
            ("SmsSid", sid),
            ("SmsStatus", "received"),
            ("Body", item.text),
            ("To", self.phone_number),
            ("NumSegments", "1"),
            ("MessageSid", sid),
            ("AccountSid", self.account_sid),
            ("From", item.user_id),
            ("ApiVersion", API_VERSION),
        ]
        for n, media in enumerate(item.media):
            self.media.setdefault(media.ref, (f"bytes of {media.ref}".encode(), media.mime))
            fields.append((f"MediaContentType{n}", media.mime))
            fields.append((f"MediaUrl{n}", self.media_url(sid, media.ref)))
        return fields

    def sign(self, fields: Iterable[tuple[str, str]], *, url: str | None = None) -> str:
        signed_url = url or (f"{self.public_url}?{self.query}" if self.query else self.public_url)
        return hmac_sha1_url_signature(self.auth_token, signed_url, list(fields))

    def callback(
        self,
        fields: Iterable[tuple[str, str]],
        *,
        signature: str | None = None,
        url: str | None = None,
    ) -> Callback:
        pairs = list(fields)
        body = urllib.parse.urlencode(pairs).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        headers["X-Twilio-Signature"] = (
            signature if signature is not None else self.sign(pairs, url=url)
        )
        return Callback(DEFAULT_PATH, body, headers, query=self.query)

    def render(self, item: FakeInbound) -> Callback | None:
        fields = self.fields_for(item)
        return None if fields is None else self.callback(fields)


def message_fields(wire: TwilioWire, sender: str, body: str, **extra: Any) -> list[tuple[str, str]]:
    """A plain incoming SMS from ``sender`` (for unit tests)."""
    fields = {
        "MessageSid": str(extra.pop("sid", "SM900")),
        "AccountSid": wire.account_sid,
        "From": sender,
        "To": str(extra.pop("to", wire.phone_number)),
        "Body": body,
        "NumMedia": "0",
    }
    fields.update((k, str(v)) for k, v in extra.items())
    return list(fields.items())
