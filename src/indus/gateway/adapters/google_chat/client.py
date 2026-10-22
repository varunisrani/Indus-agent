"""Small Google Chat and Cloud Pub/Sub REST clients over httpx (P16-13).

Only the calls the adapter makes (``CHAT_ROUTES``, ``PUBSUB_ROUTES``). Every request carries the
service account's bearer token in the ``Authorization`` header (never in a URL); a 401 drops the
cached token and the call is tried once more with a fresh one. Resource names are checked against
Chat's own character set before they go into a URL path, so a crafted id cannot leave its path.

A non-2xx answer raises `GoogleApiError` (status, Google's error message, the response headers);
the adapter turns it into a classified `SendResult` with the listener kit's ``failure_from_status``.
httpx's own errors (connect, read timeout) pass through for ``failure_from_exception``.
"""

from __future__ import annotations

import base64
import re
from collections.abc import AsyncIterator, Mapping
from typing import Any, cast
from urllib.parse import quote

import httpx

from indus.foundation.digits import is_digits
from indus.gateway.adapters.google_chat.auth import ServiceAccountTokens

__all__ = [
    "CHAT_BASE",
    "CHAT_ROUTES",
    "PUBSUB_BASE",
    "PUBSUB_ROUTES",
    "ChatClient",
    "GoogleApiError",
    "MediaTooLarge",
    "PubSubClient",
    "ReceivedMessage",
    "is_message_name",
    "is_space_name",
    "is_subscription_name",
    "is_thread_name",
    "is_user_name",
]

CHAT_BASE = "https://chat.googleapis.com"
PUBSUB_BASE = "https://pubsub.googleapis.com"

CHAT_ROUTES: Mapping[str, str] = {
    "create_message": "POST /v1/{space}/messages",
    "patch_message": "PATCH /v1/{message}",
    "delete_message": "DELETE /v1/{message}",
    "get_space": "GET /v1/{space}",
    "find_direct_message": "GET /v1/spaces:findDirectMessage?name={user}",
    "download_media": "GET /v1/media/{resourceName}?alt=media",
}
PUBSUB_ROUTES: Mapping[str, str] = {
    "get_subscription": "GET /v1/{subscription}",
    "pull": "POST /v1/{subscription}:pull",
    "acknowledge": "POST /v1/{subscription}:acknowledge",
}

_SPACE = re.compile(r"^spaces/[A-Za-z0-9_-]{1,128}$")
_MESSAGE = re.compile(r"^spaces/[A-Za-z0-9_-]{1,128}/messages/[A-Za-z0-9_.-]{1,256}$")
_THREAD = re.compile(r"^spaces/[A-Za-z0-9_-]{1,128}/threads/[A-Za-z0-9_.-]{1,256}$")
_USER = re.compile(r"^users/[A-Za-z0-9_.+@-]{1,254}$")
_SUBSCRIPTION = re.compile(
    r"^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/subscriptions/[A-Za-z][A-Za-z0-9._~+%-]{2,254}$"
)
_MEDIA_NAME = re.compile(r"^[A-Za-z0-9_.~+=/-]{1,1024}$")
_SNIPPET = 300


def is_space_name(value: str) -> bool:
    return bool(_SPACE.match(value))


def is_message_name(value: str) -> bool:
    return bool(_MESSAGE.match(value))


def is_thread_name(value: str) -> bool:
    return bool(_THREAD.match(value))


def is_user_name(value: str) -> bool:
    return bool(_USER.match(value))


def is_subscription_name(value: str) -> bool:
    return bool(_SUBSCRIPTION.match(value))


class GoogleApiError(Exception):
    """A non-2xx answer from a Google API (``detail`` is Google's message, cut to 300 chars)."""

    def __init__(self, status: int, detail: str = "", headers: Mapping[str, str] | None = None):
        self.status = status
        self.detail = " ".join(detail.split())[:_SNIPPET]
        self.headers: dict[str, str] = dict(headers or {})
        text = f"Google API HTTP {status}"
        super().__init__(f"{text}: {self.detail}" if self.detail else text)


class MediaTooLarge(Exception):
    def __init__(self, size: int, cap: int) -> None:
        super().__init__(f"the attachment is too large ({size} bytes, the cap is {cap})")


def _error_of(response: httpx.Response) -> GoogleApiError:
    detail = ""
    try:
        body = response.json()
    except (ValueError, UnicodeDecodeError):
        body = None
    if isinstance(body, dict):
        error = cast(dict[str, Any], body).get("error")
        if isinstance(error, dict):
            detail = str(cast(dict[str, Any], error).get("message") or "")
        elif isinstance(error, str):
            detail = error
    return GoogleApiError(response.status_code, detail, response.headers)


class _Authorized:
    """Requests with the service account's token; one retry with a fresh token after a 401."""

    def __init__(self, http: httpx.AsyncClient, tokens: ServiceAccountTokens) -> None:
        self.http = http
        self.tokens = tokens

    async def request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        params: Mapping[str, str] | None = None,
    ) -> Any:
        for attempt in (0, 1):
            token = await self.tokens.token()
            response = await self.http.request(
                method,
                url,
                json=json,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            if response.status_code == 401 and attempt == 0:
                self.tokens.invalidate()
                continue
            if response.status_code >= 400:
                raise _error_of(response)
            if not response.content:
                return {}
            try:
                return response.json()
            except (ValueError, UnicodeDecodeError):
                raise GoogleApiError(response.status_code, "unexpected answer") from None
        raise AssertionError("unreachable")  # pragma: no cover

    async def object(self, method: str, url: str, **kw: Any) -> dict[str, Any]:
        data = await self.request(method, url, **kw)
        if not isinstance(data, dict):
            raise GoogleApiError(200, "unexpected answer")
        return cast(dict[str, Any], data)


class ChatClient(_Authorized):
    """Google Chat API v1 calls (see ``CHAT_ROUTES``)."""

    def __init__(
        self, http: httpx.AsyncClient, tokens: ServiceAccountTokens, base: str = CHAT_BASE
    ) -> None:
        super().__init__(http, tokens)
        self.base = base.rstrip("/")

    async def create_message(
        self, space: str, body: Mapping[str, Any], *, in_thread: bool = False
    ) -> dict[str, Any]:
        if not is_space_name(space):
            raise ValueError("not a Google Chat space name")
        params = (
            {"messageReplyOption": "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"} if in_thread else None
        )
        return await self.object(
            "POST", f"{self.base}/v1/{space}/messages", json=dict(body), params=params
        )

    async def patch_message(
        self, name: str, body: Mapping[str, Any], *, update_mask: str
    ) -> dict[str, Any]:
        if not is_message_name(name):
            raise ValueError("not a Google Chat message name")
        return await self.object(
            "PATCH", f"{self.base}/v1/{name}", json=dict(body), params={"updateMask": update_mask}
        )

    async def delete_message(self, name: str) -> None:
        if not is_message_name(name):
            raise ValueError("not a Google Chat message name")
        await self.request("DELETE", f"{self.base}/v1/{name}")

    async def get_space(self, space: str) -> dict[str, Any]:
        if not is_space_name(space):
            raise ValueError("not a Google Chat space name")
        return await self.object("GET", f"{self.base}/v1/{space}")

    async def find_direct_message(self, user: str) -> dict[str, Any]:
        """The DM space between the app and ``users/<id>`` (404 when there is none)."""
        if not is_user_name(user):
            raise ValueError("not a Google Chat user name")
        return await self.object(
            "GET", f"{self.base}/v1/spaces:findDirectMessage", params={"name": user}
        )

    async def download_media(self, resource_name: str, *, cap: int) -> AsyncIterator[bytes]:
        """Stream an attachment's bytes (``MediaTooLarge`` past ``cap``)."""
        if not _MEDIA_NAME.match(resource_name) or ".." in resource_name.split("/"):
            raise ValueError("not an attachment resource name")
        url = f"{self.base}/v1/media/{quote(resource_name, safe='/')}"
        async for chunk in self.stream(url, cap=cap, params={"alt": "media"}):
            yield chunk

    async def stream(
        self, url: str, *, cap: int, params: Mapping[str, str] | None = None
    ) -> AsyncIterator[bytes]:
        token = await self.tokens.token()
        headers = {"Authorization": f"Bearer {token}"}
        async with self.http.stream("GET", url, params=params, headers=headers) as response:
            if response.status_code >= 400:
                await response.aread()
                raise _error_of(response)
            declared = response.headers.get("Content-Length", "")
            if is_digits(declared) and int(declared) > cap:
                raise MediaTooLarge(int(declared), cap)
            seen = 0
            async for chunk in response.aiter_bytes():
                seen += len(chunk)
                if seen > cap:
                    raise MediaTooLarge(seen, cap)
                yield chunk


class ReceivedMessage:
    """One pulled Pub/Sub message."""

    __slots__ = ("ack_id", "attributes", "data", "message_id")

    def __init__(self, ack_id: str, data: bytes, attributes: Mapping[str, str], message_id: str):
        self.ack_id = ack_id
        self.data = data
        self.attributes = dict(attributes)
        self.message_id = message_id


class PubSubClient(_Authorized):
    """Cloud Pub/Sub v1 REST calls for one pull subscription (see ``PUBSUB_ROUTES``)."""

    def __init__(
        self, http: httpx.AsyncClient, tokens: ServiceAccountTokens, base: str = PUBSUB_BASE
    ) -> None:
        super().__init__(http, tokens)
        self.base = base.rstrip("/")

    def _url(self, subscription: str, verb: str = "") -> str:
        if not is_subscription_name(subscription):
            raise ValueError("not a Pub/Sub subscription name")
        return f"{self.base}/v1/{subscription}{verb}"

    async def get_subscription(self, subscription: str) -> dict[str, Any]:
        return await self.object("GET", self._url(subscription))

    async def pull(self, subscription: str, max_messages: int) -> list[ReceivedMessage]:
        data = await self.object(
            "POST", self._url(subscription, ":pull"), json={"maxMessages": max_messages}
        )
        received = data.get("receivedMessages")
        out: list[ReceivedMessage] = []
        if not isinstance(received, list):
            return out
        for raw in cast(list[object], received):
            if not isinstance(raw, dict):
                continue
            item = cast(dict[str, Any], raw)
            ack_id = str(item.get("ackId") or "")
            message = item.get("message")
            if not ack_id or not isinstance(message, dict):
                continue
            body = cast(dict[str, Any], message)
            try:
                payload = base64.b64decode(str(body.get("data") or ""), validate=True)
            except ValueError:
                payload = b""
            attributes = body.get("attributes")
            attrs = (
                {str(k): str(v) for k, v in cast(dict[object, object], attributes).items()}
                if isinstance(attributes, dict)
                else {}
            )
            out.append(ReceivedMessage(ack_id, payload, attrs, str(body.get("messageId") or "")))
        return out

    async def acknowledge(self, subscription: str, ack_ids: list[str]) -> None:
        if ack_ids:
            await self.request(
                "POST", self._url(subscription, ":acknowledge"), json={"ackIds": ack_ids}
            )
