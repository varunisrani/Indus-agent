"""Our small client for the LINE Messaging API v2 (P16-23), over httpx.

Only the calls the adapter makes: the bot's own profile (credential check and self id), reply and
push (at most five message objects each; both answer with the ids of the messages LINE stored),
the loading animation in a user chat, and the download of an inbound message's content. The
channel access token travels only in the ``Authorization`` header, never in a URL, so httpx's
request log lines and error texts cannot show it.

A status of 400 or more raises `LineError` (status, body text, headers); transport failures
propagate as httpx exceptions, which the adapter classifies (P16-01 kit).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import httpx

from indus.foundation.digits import is_digits

__all__ = [
    "API_BASE",
    "DATA_BASE",
    "LineApi",
    "LineError",
    "Stored",
]

API_BASE: Final = "https://api.line.me"
DATA_BASE: Final = "https://api-data.line.me"
# Inbound content above this is refused while downloading.
MAX_CONTENT_BYTES: Final = 50 * 1024 * 1024


class LineError(Exception):
    """LINE answered with an error status."""

    def __init__(self, status: int, text: str, headers: Mapping[str, str]) -> None:
        super().__init__(f"LINE answered {status}")
        self.status = status
        self.text = text
        self.headers = dict(headers)

    @property
    def detail(self) -> str:
        """The ``message`` LINE put in its error body (with any ``details``), else the text."""
        try:
            data = json.loads(self.text)
        except ValueError:
            return self.text
        if not isinstance(data, dict):
            return self.text
        parts = [str(data.get("message") or "")]
        for item in data.get("details") or ():
            if isinstance(item, dict):
                parts.append(str(item.get("message") or ""))
        return "; ".join(p for p in parts if p) or self.text


@dataclass(frozen=True)
class Stored:
    """What LINE kept from one reply or push: one id per message object, in order."""

    ids: tuple[str, ...]
    quote_tokens: tuple[str, ...] = ()


def _stored(response: httpx.Response, count: int) -> Stored:
    ids: list[str] = []
    quotes: list[str] = []
    try:
        data = response.json() if response.content else {}
    except ValueError:
        data = {}
    sent = data.get("sentMessages") if isinstance(data, dict) else None
    for item in sent if isinstance(sent, list) else ():
        if isinstance(item, dict) and item.get("id"):
            ids.append(str(item["id"]))
            quotes.append(str(item.get("quoteToken") or ""))
    if len(ids) != count:
        # An older answer without ids: name the messages after LINE's request id.
        request_id = response.headers.get("x-line-request-id") or "line"
        ids = [f"{request_id}.{n}" for n in range(count)]
        quotes = []
    return Stored(tuple(ids), tuple(quotes))


class LineApi:
    """The Messaging API calls of one channel (see the module notes)."""

    def __init__(
        self,
        token: str,
        *,
        client_factory: Callable[[], httpx.AsyncClient],
        api_base: str = API_BASE,
        data_base: str = DATA_BASE,
    ) -> None:
        self._token = token
        self._factory = client_factory
        self._client: httpx.AsyncClient | None = None
        self.api_base = api_base.rstrip("/")
        self.data_base = data_base.rstrip("/")

    def __repr__(self) -> str:
        return f"<LineApi {self.api_base}>"

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = self._factory()
        return self._client

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def _post(self, path: str, body: Mapping[str, Any]) -> httpx.Response:
        response = await self.client.post(
            f"{self.api_base}{path}", json=dict(body), headers=self._headers()
        )
        if response.status_code >= 400:
            raise LineError(response.status_code, response.text, response.headers)
        return response

    async def bot_info(self) -> dict[str, Any]:
        """``GET /v2/bot/info``: the bot's ``userId``, ``displayName`` and ``basicId``."""
        response = await self.client.get(f"{self.api_base}/v2/bot/info", headers=self._headers())
        if response.status_code >= 400:
            raise LineError(response.status_code, response.text, response.headers)
        data = response.json()
        return data if isinstance(data, dict) else {}

    async def reply(self, reply_token: str, messages: Sequence[Mapping[str, Any]]) -> Stored:
        """``POST /v2/bot/message/reply`` with a single-use reply token."""
        body = {"replyToken": reply_token, "messages": [dict(m) for m in messages]}
        return _stored(await self._post("/v2/bot/message/reply", body), len(messages))

    async def push(self, to: str, messages: Sequence[Mapping[str, Any]]) -> Stored:
        """``POST /v2/bot/message/push`` to a user, group or room id."""
        body = {"to": to, "messages": [dict(m) for m in messages]}
        return _stored(await self._post("/v2/bot/message/push", body), len(messages))

    async def start_loading(self, chat_id: str, seconds: int) -> None:
        """``POST /v2/bot/chat/loading/start``: the loading animation in a user chat."""
        await self._post(
            "/v2/bot/chat/loading/start", {"chatId": chat_id, "loadingSeconds": seconds}
        )

    async def content(
        self, message_id: str, *, max_bytes: int = MAX_CONTENT_BYTES
    ) -> AsyncIterator[bytes]:
        """The content of an inbound image, video, audio or file message, as chunks."""
        url = f"{self.data_base}/v2/bot/message/{message_id}/content"
        async with self.client.stream("GET", url, headers=self._headers()) as response:
            if response.status_code >= 400:
                await response.aread()
                raise LineError(response.status_code, response.text, response.headers)
            declared = response.headers.get("content-length", "")
            if is_digits(declared) and int(declared) > max_bytes:
                raise LineError(413, "the content is too large to download", {})
            seen = 0
            async for chunk in response.aiter_bytes():
                seen += len(chunk)
                if seen > max_bytes:
                    raise LineError(413, "the content is too large to download", {})
                yield chunk

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()
