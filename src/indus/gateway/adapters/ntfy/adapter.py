"""The ntfy adapter (P16-26): one streaming subscription in, POST publish out.

Inbound: ``GET <server>/<topic>[,<topic>…]/json`` streams one JSON object per line (``open``,
``keepalive``, ``message`` …). Each ``message`` becomes a DM-typed `InboundEvent` whose chat id
**and** user id are its topic: ntfy has no authenticated senders (the title is whatever the
publisher wrote), so nothing inside a message decides who sent it. A topic is a broadcast surface
anyone who knows its name can read, so ``dm_is_private`` is False and pairing codes or login links
are never sent there (R06.24). Our own replies carry ``ECHO_TAG`` and are dropped when they come
back; ids seen in the last 300 s (at most 1,000) are dropped as repeats.

The stream is reopened after it ends, fails or stays silent for 90 s (ntfy sends a keepalive every
45 s), after 2 s doubling to 60 s (back to 2 s once a stream lived 60 s), with ``since=<last id>``
so messages published during the gap still arrive. A refused credential or an unknown topic is
fatal: at connect it fails the connect, later it reports the platform fatal and stops.

Outbound: ``POST <server>/<topic>`` with the text as the body; title, tags and priority from the
send metadata go as ntfy's query parameters. Text over 4,096 bytes is refused as too long (ntfy
would turn it into an attachment); the sender splits before.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, Final
from urllib.parse import urlsplit

import httpx

from indus.foundation.clock import Clock
from indus.gateway.adapters._http import SeenIds, failure_from_exception, failure_from_status
from indus.gateway.adapters.base import Adapter, AdapterConnectError, Capabilities, ChatInfo
from indus.gateway.core.events import InboundEvent, Source
from indus.gateway.core.send import SendResult
from indus.gateway.http.serving import is_loopback

if TYPE_CHECKING:
    from indus.gateway.config import PlatformSettings

__all__ = [
    "API_TARGET",
    "DEFAULT_SERVER",
    "ECHO_TAG",
    "MAX_BYTES",
    "STREAM_TIMEOUT_S",
    "NtfyAdapter",
    "NtfyApi",
    "NtfyError",
    "NtfyMarkdownAdapter",
    "auth_header",
    "utf8_bytes",
    "valid_topic",
]

log = logging.getLogger(__name__)

API_TARGET: Final = "indus.gateway.adapters.ntfy.adapter:NtfyApi"
DEFAULT_SERVER: Final = "https://ntfy.sh"
MAX_BYTES: Final = 4096
STREAM_TIMEOUT_S: Final = 90.0
BACKOFF_START_S: Final = 2.0
BACKOFF_MAX_S: Final = 60.0
STABLE_S: Final = 60.0
DEDUPE_TTL_S: Final = 300.0
DEDUPE_MAX: Final = 1000
ECHO_TAG: Final = "indus-reply"
_TOPIC: Final = re.compile(r"^[-_A-Za-z0-9]{1,64}$")
_PRIORITIES: Final = frozenset({"min", "low", "default", "high", "max", "urgent"})


def utf8_bytes(text: str) -> int:
    return len(text.encode("utf-8", "surrogatepass"))


def valid_topic(topic: str) -> bool:
    """ntfy topic names: 1–64 of ``A–Z a–z 0–9 - _`` (no path tricks)."""
    return bool(_TOPIC.match(topic))


def auth_header(credential: str | None) -> dict[str, str]:
    """``Authorization`` for an ntfy credential: ``user:pass`` → Basic, else a Bearer token."""
    value = (credential or "").strip()
    if not value:
        return {}
    if ":" in value:
        return {"Authorization": "Basic " + base64.b64encode(value.encode()).decode()}
    return {"Authorization": f"Bearer {value}"}


class NtfyError(Exception):
    """The ntfy server answered with an error status."""

    def __init__(self, status: int, text: str, headers: Mapping[str, str]) -> None:
        super().__init__(f"ntfy answered {status}")
        self.status = status
        self.text = text
        self.headers = dict(headers)

    @property
    def detail(self) -> str:
        try:
            data = json.loads(self.text)
        except ValueError:
            return self.text
        return str(data.get("error") or self.text) if isinstance(data, dict) else self.text


class NtfyApi:
    """Publish and subscribe calls against one ntfy server."""

    def __init__(
        self,
        server: str,
        *,
        credential: str | None,
        client_factory: Callable[[], httpx.AsyncClient],
    ) -> None:
        self.server = server.rstrip("/")
        self._auth = auth_header(credential)
        self._factory = client_factory
        self._client: httpx.AsyncClient | None = None

    def __repr__(self) -> str:
        return f"<NtfyApi {self.server}>"

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = self._factory()
        return self._client

    async def publish(
        self, topic: str, text: str, params: Mapping[str, str] | None = None
    ) -> dict[str, Any]:
        """``POST /<topic>``: the stored message as ntfy returns it."""
        response = await self.client.post(
            f"{self.server}/{topic}",
            content=text.encode("utf-8"),
            params=dict(params or {}),
            headers={**self._auth, "Content-Type": "text/plain; charset=utf-8"},
        )
        if response.status_code >= 400:
            raise NtfyError(response.status_code, response.text, response.headers)
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    async def subscribe(self, topics: Sequence[str], since: str | None) -> httpx.Response:
        """Open ``GET /<topics>/json`` as a stream; the caller closes the response."""
        params = {"since": since} if since else {}
        request = self.client.build_request(
            "GET", f"{self.server}/{','.join(topics)}/json", params=params, headers=self._auth
        )
        response = await self.client.send(request, stream=True)
        if response.status_code >= 400:
            await response.aread()
            await response.aclose()
            raise NtfyError(response.status_code, response.text, response.headers)
        return response

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()


async def within[T](clock: Clock, work: Awaitable[T], seconds: float) -> T:
    """``work``'s result, or ``TimeoutError`` once ``clock`` moved ``seconds`` on first."""
    task = asyncio.ensure_future(work)
    timer = asyncio.ensure_future(clock.sleep(seconds))
    try:
        await asyncio.wait({task, timer}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        timer.cancel()
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
    if task.cancelled():
        raise TimeoutError
    return task.result()


class _Fatal(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fatal_of(exc: NtfyError, topics: Sequence[str]) -> _Fatal | None:
    if exc.status in (401, 403):
        return _Fatal("ntfy_auth", f"ntfy refused the credential ({exc.status}); check NTFY_TOKEN")
    if exc.status == 404:
        return _Fatal("ntfy_topic", f"ntfy does not know the topic {','.join(topics)}")
    return None


class NtfyAdapter(Adapter):
    """ntfy topics as a broadcast chat surface (see the module notes)."""

    platform: ClassVar[str] = "ntfy"
    capabilities: ClassVar[Capabilities] = Capabilities(
        max_len=MAX_BYTES, len_fn=utf8_bytes, markup="plain", dm_is_private=False
    )
    vendor_names: ClassVar[Mapping[str, tuple[str, ...]]] = {
        API_TARGET: ("publish", "subscribe", "aclose"),
    }
    markdown: ClassVar[bool] = False

    def __init__(
        self,
        api: NtfyApi,
        *,
        topics: Sequence[str],
        clock: Clock,
        settings: PlatformSettings | None = None,
        secrets: Sequence[str] = (),
        reply_topic: str | None = None,
        send_only: bool = False,
        stream_timeout: float = STREAM_TIMEOUT_S,
    ) -> None:
        super().__init__(clock=clock, settings=settings, secrets=secrets)
        names = tuple(dict.fromkeys(t.strip() for t in topics if t.strip()))
        bad = [t for t in (*names, *([reply_topic] if reply_topic else [])) if not valid_topic(t)]
        if not names or bad:
            raise ValueError(f"ntfy topic names must match {_TOPIC.pattern}: {bad or 'none set'}")
        self.api = api
        self.topics = names
        self.reply_topic = reply_topic
        self.send_only = send_only
        self.stream_timeout = stream_timeout
        self._seen = SeenIds(clock, DEDUPE_TTL_S, DEDUPE_MAX)
        self._since: str | None = None
        self._reader: asyncio.Task[None] | None = None
        self._response: httpx.Response | None = None
        # Times the stream was (re)opened after a loss, for status and tests.
        self.reopened = 0

    # -- lifecycle ------------------------------------------------------------------------------

    async def _open(self, is_reconnect: bool) -> None:
        if self.send_only:
            return  # ``indus send``: publish only, never consume the topic
        try:
            response = await self.api.subscribe(self.topics, self._since)
        except NtfyError as exc:
            fatal = _fatal_of(exc, self.topics)
            if fatal is not None:
                raise AdapterConnectError(str(fatal), retryable=False, code=fatal.code) from None
            raise AdapterConnectError(f"ntfy answered {exc.status} to the subscription") from None
        except httpx.HTTPError as exc:
            raise AdapterConnectError(f"cannot reach ntfy: {type(exc).__name__}") from None
        self._response = response
        self._reader = asyncio.ensure_future(self._run(response))

    async def _close(self) -> None:
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader
        await self._drop_response()
        await self.api.aclose()

    async def _drop_response(self) -> None:
        response, self._response = self._response, None
        if response is not None:
            with contextlib.suppress(Exception):
                await response.aclose()

    # -- the subscription -----------------------------------------------------------------------

    async def _run(self, response: httpx.Response) -> None:
        backoff = BACKOFF_START_S
        while True:
            started = self.clock.monotonic()
            try:
                await self._consume(response)
                reason = "ended"
            except TimeoutError:
                reason = f"was silent for {self.stream_timeout:g} s"
            except httpx.HTTPError as exc:
                reason = f"failed ({type(exc).__name__})"
            await self._drop_response()
            if self.clock.monotonic() - started >= STABLE_S:
                backoff = BACKOFF_START_S
            log.info("ntfy: the subscription %s; reopening it", reason)
            while True:
                await self.clock.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_S)
                try:
                    response = await self.api.subscribe(self.topics, self._since)
                except NtfyError as exc:
                    fatal = _fatal_of(exc, self.topics)
                    if fatal is not None:
                        self.report_fatal(fatal.code, str(fatal))
                        return
                    log.info("ntfy: reopening the subscription got %s", exc.status)
                    continue
                except httpx.HTTPError as exc:
                    log.info("ntfy: reopening the subscription failed: %s", type(exc).__name__)
                    continue
                self._response = response
                self.reopened += 1
                break

    async def _consume(self, response: httpx.Response) -> None:
        lines = response.aiter_lines()
        while True:
            try:
                line = await within(self.clock, anext(lines), self.stream_timeout)
            except StopAsyncIteration:
                return
            line = line.strip()
            if line:
                await self._line(line)

    async def _line(self, line: str) -> None:
        try:
            data = json.loads(line)
        except ValueError:
            return
        if not isinstance(data, dict) or data.get("event") != "message":
            return  # open, keepalive, poll_request: they only show the stream is alive
        message_id = str(data.get("id") or "")
        if not message_id:
            return
        self._since = message_id
        if not self._seen.first_time(message_id):
            return
        tags = data.get("tags")
        if isinstance(tags, list) and ECHO_TAG in tags:
            return
        text = str(data.get("message") or "").strip()
        topic = str(data.get("topic") or (self.topics[0] if len(self.topics) == 1 else ""))
        if not text or topic not in self.topics:
            return
        metadata: dict[str, Any] = {}
        if data.get("title"):
            metadata["title"] = str(data["title"])
        stamp = data.get("time")
        timestamp = None
        if isinstance(stamp, int) and not isinstance(stamp, bool) and stamp > 0:
            with contextlib.suppress(OverflowError, OSError, ValueError):
                timestamp = datetime.fromtimestamp(stamp, tz=UTC)
        source = Source(
            platform=self.platform,
            chat_id=topic,
            chat_type="dm",
            chat_name=topic,
            user_id=topic,
            user_name=topic,
        )
        await self.emit(
            InboundEvent(
                source=source,
                text=text,
                message_id=message_id,
                metadata=metadata,
                timestamp=timestamp,
            )
        )

    # -- outbound -------------------------------------------------------------------------------

    def _params(self, meta: Mapping[str, Any]) -> dict[str, str]:
        params: dict[str, str] = {}
        tags = meta.get("tags")
        extra_tags = [str(t) for t in tags] if isinstance(tags, list | tuple) else []
        params["tags"] = ",".join([*extra_tags, ECHO_TAG])
        title = meta.get("title")
        if isinstance(title, str) and title.strip():
            params["title"] = title.strip()[:256]
        priority = meta.get("priority")
        if isinstance(priority, int) and not isinstance(priority, bool) and 1 <= priority <= 5:
            params["priority"] = str(priority)
        elif isinstance(priority, str) and priority in _PRIORITIES:
            params["priority"] = priority
        if self.markdown:
            params["markdown"] = "yes"
        return params

    async def _send(
        self, chat_id: str, text: str, reply_to: str | None, meta: dict[str, Any]
    ) -> SendResult:
        topic = self.reply_topic if self.reply_topic and chat_id in self.topics else chat_id
        if not valid_topic(topic):
            return SendResult.failure(f"chat not found: {topic[:70]!r} is not an ntfy topic name")
        if utf8_bytes(text) > MAX_BYTES:
            return SendResult.failure(f"message is too long for ntfy ({MAX_BYTES} bytes)")
        try:
            stored = await self.api.publish(topic, text, self._params(meta))
        except NtfyError as exc:
            return failure_from_status(exc.status, exc.detail, exc.headers, now=self.clock.now())
        except httpx.HTTPError as exc:
            return failure_from_exception(exc)
        return SendResult.sent(str(stored.get("id") or f"ntfy-{self.clock.monotonic():.6f}"))

    async def _chat_info(self, chat_id: str) -> ChatInfo:
        return ChatInfo(name=chat_id, type="dm")


class NtfyMarkdownAdapter(NtfyAdapter):
    """ntfy with ``markdown: true``: standard Markdown, rendered by ntfy's apps."""

    capabilities: ClassVar[Capabilities] = Capabilities(
        max_len=MAX_BYTES, len_fn=utf8_bytes, markup="markdown", dm_is_private=False
    )
    markdown: ClassVar[bool] = True


def server_url(value: str) -> str:
    """A usable ntfy server URL (``ValueError`` otherwise)."""
    url = value.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ValueError(f"the ntfy server must be an http(s) URL, not {value!r}")
    return url


def plain_http_allowed(url: str) -> bool:
    """Credentials go over plain http only to a loopback server."""
    parts = urlsplit(url)
    return parts.scheme == "https" or is_loopback(parts.hostname or "")
