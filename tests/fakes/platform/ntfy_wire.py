"""A fake ntfy server over the shared fake platform (P16-26), no socket.

- ``GET /<topic>[,<topic>…]/json`` checks the credential (401 without it when ``token`` is set),
  connects the platform and answers with an endless newline-delimited JSON stream: an ``open``
  event, then every message published to those topics from then on (``since=<id>`` first replays
  the cached messages after that id). Closing the stream disconnects the platform.
- ``POST /<topic>`` posts the body on the platform (armed failures answer as ntfy would, with
  ``{"code", "http", "error"}``), stores the message (``id``, ``time``, ``topic``, ``message``,
  ``title``, ``tags``, ``priority`` from the query) and streams it to open subscribers of that topic
  — our own replies come back, as on a real server.
- ``pump()`` publishes the platform's inbound items as messages. ntfy has no groups, threads, bot
  authors, edits or (in this adapter) attachments: those items publish nothing.
- ``keepalive()`` sends a keepalive line on every open stream; ``drop()`` ends them all (the
  server went away). A stream that gets nothing just stays silent, which is how a stall looks.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from tests.fakes.platform.core import FakeChatPlatform, FakeInbound, FakePlatformError
from tests.fakes.platform.http_wire import ERROR_STATUS, ApiCall, HttpCallbackWire, api


class FakeStream(httpx.AsyncByteStream):
    """One open subscription: lines go in through ``put``; ``None`` ends it."""

    def __init__(self, wire: NtfyWire, topics: tuple[str, ...]) -> None:
        self.wire = wire
        self.topics = topics
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.closed = False

    def put(self, event: dict[str, Any] | None) -> None:
        if not self.closed:
            self.queue.put_nowait(None if event is None else (json.dumps(event) + "\n").encode())

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            line = await self.queue.get()
            if line is None:
                return
            yield line

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self in self.wire.streams:
            self.wire.streams.remove(self)
        if not self.wire.streams:
            await self.wire.platform.disconnect()


class NtfyWire(HttpCallbackWire):
    base_url = "https://ntfy.fake.test"

    def __init__(self, platform: FakeChatPlatform, *, token: str | None = None) -> None:
        super().__init__(platform)
        self.token = token
        self.streams: list[FakeStream] = []
        self.cache: list[dict[str, Any]] = []
        self.subscriptions: list[ApiCall] = []
        self._ids = itertools.count(1)
        # When set, a subscribe call answers with this status instead.
        self.refuse_subscribe: int | None = None

    def _authorized(self, call: ApiCall) -> bool:
        if self.token is None:
            return True
        header = call.headers.get("authorization", "")
        return header == f"Bearer {self.token}"

    def _error(self, status: int, text: str) -> httpx.Response:
        return httpx.Response(
            status, json={"code": status * 100 + 1, "http": status, "error": text}
        )

    def error_response(self, exc: FakePlatformError) -> httpx.Response:
        status = ERROR_STATUS.get(exc.kind, 500)
        response = self._error(status, exc.text)
        if exc.retry_after is not None and status == 429:
            response.headers["Retry-After"] = str(int(exc.retry_after))
        return response

    def _event(self, topic: str, message: str, **fields: Any) -> dict[str, Any]:
        event = {
            "id": fields.pop("id", None) or f"m{next(self._ids):010d}",
            "time": int(self.platform.clock.now().timestamp()),
            "event": "message",
            "topic": topic,
            "message": message,
            **{k: v for k, v in fields.items() if v not in (None, "", [])},
        }
        self.cache.append(event)
        for stream in list(self.streams):
            if topic in stream.topics:
                stream.put(event)
        return event

    @api("GET", "/{topics}/json")
    async def subscribe(self, call: ApiCall, topics: str) -> httpx.Response:
        self.subscriptions.append(call)
        if not self._authorized(call):
            return self._error(401, "unauthorized")
        if self.refuse_subscribe is not None:
            return self._error(self.refuse_subscribe, "refused")
        await self.platform.connect()
        names = tuple(topics.split(","))
        stream = FakeStream(self, names)
        self.streams.append(stream)
        stream.put({"id": "open", "time": 0, "event": "open", "topic": topics})
        since = call.params.get("since")
        if since:
            ids = [e["id"] for e in self.cache]
            after = self.cache[ids.index(since) + 1 :] if since in ids else []
            for event in after:
                if event["topic"] in names:
                    stream.put(event)
        return httpx.Response(200, stream=stream, headers={"Content-Type": "application/x-ndjson"})

    @api("POST", "/{topic}")
    async def publish(self, call: ApiCall, topic: str) -> httpx.Response:
        if not self._authorized(call):
            return self._error(401, "unauthorized")
        text = call.body.decode("utf-8")
        sent = await self.platform.post(topic, text)
        tags = [t for t in call.params.get("tags", "").split(",") if t]
        event = self._event(
            topic,
            text,
            id=sent.message_id,
            title=call.params.get("title"),
            tags=tags,
            priority=call.params.get("priority"),
        )
        return httpx.Response(200, json=event)

    def publish_inbound(self, item: FakeInbound) -> dict[str, Any] | None:
        if item.chat_kind != "dm" or item.thread_id or item.is_bot or item.media:
            return None
        if item.event != "message":
            return None
        return self._event(item.chat_id, item.text, id=item.message_id)

    async def pump(self) -> list[httpx.Response]:
        for item in self.platform.drain_inbound():
            self.publish_inbound(item)
        return []

    def keepalive(self) -> None:
        for stream in list(self.streams):
            stream.put(
                {"id": "ka", "time": 0, "event": "keepalive", "topic": ",".join(stream.topics)}
            )

    def drop(self) -> None:
        for stream in list(self.streams):
            stream.put(None)

    def send_raw(self, line: str) -> None:
        """A raw line on every open stream (not JSON, odd events)."""
        for stream in list(self.streams):
            if not stream.closed:
                stream.queue.put_nowait((line + "\n").encode())


__all__ = ["FakeStream", "NtfyWire"]
