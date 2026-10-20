"""A small Feishu / Lark Open Platform client over httpx, and the long-connection opener (P16-14).

Only the routes the adapter and the document tools use (``ROUTES``). Two hosts, one per domain:
Feishu (China) ``https://open.feishu.cn`` and Lark (international) ``https://open.larksuite.com``.

Credentials: the app id and secret go only into the tenant-token request body and the
long-connection endpoint body; the tenant token only into the ``Authorization`` header. The token
is cached until five minutes before it expires; a call answered with an invalid-token code fetches
a new token and is sent once more (the API refused it before doing anything). Nothing here logs a
request body, a token or the socket URL.

Every failed call raises `FeishuError` whose text starts with a reading the send classifier
(P15-04) recognises, chosen from the API's ``code`` (and the HTTP status): ``rate limited``,
``forbidden``, ``chat not found``, ``message not found``, ``message is too long``, ``can't parse``,
``unauthorized`` (refused credentials: final, never a dead chat), or a connection-level reading
for 502/503/504.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any, Protocol
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from indus.foundation.clock import Clock

__all__ = [
    "AUTH_CODES",
    "DOMAINS",
    "INSTALL_HINT",
    "ROUTES",
    "Endpoint",
    "FeishuClient",
    "FeishuError",
    "MediaTooLarge",
    "SocketLike",
    "SocketOpener",
    "base_url_for",
    "httpx_ws_opener",
]

DOMAINS: Mapping[str, str] = {
    "feishu": "https://open.feishu.cn",
    "lark": "https://open.larksuite.com",
}
INSTALL_HINT = "pip install 'indus-agent[feishu]'"
DEFAULT_TIMEOUT_S = 30.0
TOKEN_MARGIN_S = 300.0

ROUTES: Mapping[str, str] = {
    "tenant_token": "POST /open-apis/auth/v3/tenant_access_token/internal",
    "bot_info": "GET /open-apis/bot/v3/info",
    "ws_endpoint": "POST /callback/ws/endpoint",
    "create_message": "POST /open-apis/im/v1/messages",
    "reply_message": "POST /open-apis/im/v1/messages/{message_id}/reply",
    "update_message": "PUT /open-apis/im/v1/messages/{message_id}",
    "patch_message": "PATCH /open-apis/im/v1/messages/{message_id}",
    "delete_message": "DELETE /open-apis/im/v1/messages/{message_id}",
    "get_message": "GET /open-apis/im/v1/messages/{message_id}",
    "get_chat": "GET /open-apis/im/v1/chats/{chat_id}",
    "upload_image": "POST /open-apis/im/v1/images",
    "upload_file": "POST /open-apis/im/v1/files",
    "get_resource": "GET /open-apis/im/v1/messages/{message_id}/resources/{file_key}",
    "add_reaction": "POST /open-apis/im/v1/messages/{message_id}/reactions",
    "delete_reaction": "DELETE /open-apis/im/v1/messages/{message_id}/reactions/{reaction_id}",
    "get_user": "GET /open-apis/contact/v3/users/{user_id}",
    "doc_raw_content": "GET /open-apis/docx/v1/documents/{document_id}/raw_content",
    "list_comments": "GET /open-apis/drive/v1/files/{file_token}/comments",
    "get_comment": "GET /open-apis/drive/v1/files/{file_token}/comments/{comment_id}",
    "list_replies": "GET /open-apis/drive/v1/files/{file_token}/comments/{comment_id}/replies",
    "reply_comment": "POST /open-apis/drive/v1/files/{file_token}/comments/{comment_id}/replies",
    "add_comment": "POST /open-apis/drive/v1/files/{file_token}/comments",
}

# Refused app credentials (token route).
AUTH_CODES = frozenset({10003, 10014})
# A tenant token the API no longer takes: refresh once and resend.
TOKEN_INVALID_CODES = frozenset({99991661, 99991663})
_RATE_CODES = frozenset({99991400, 230020})
_FORBIDDEN_CODES = frozenset({230002, 230006, 230013, 230027, 99991672})
_GONE_MESSAGE_CODES = frozenset({230011, 231003})
_TOO_LONG_CODES = frozenset({230025})
_PARSE_CODES = frozenset({230099})
_DETAIL_CHARS = 300


class FeishuError(Exception):
    def __init__(
        self,
        reading: str,
        *,
        code: int | None = None,
        status: int = 0,
        detail: str = "",
        retry_after: float | None = None,
    ) -> None:
        self.reading = reading
        self.code = code
        self.status = status
        self.detail = detail
        self.retry_after = retry_after
        where = ", ".join(
            p
            for p in (
                f"code {code}" if code is not None else "",
                f"HTTP {status}" if status else "",
            )
            if p
        )
        text = f"{reading} ({where})" if where else reading
        super().__init__(f"{text}: {detail}" if detail else text)

    @property
    def is_auth(self) -> bool:
        return self.reading.startswith("unauthorized")


class MediaTooLarge(Exception):
    def __init__(self, size: int, cap: int) -> None:
        super().__init__(f"the file is too large ({size} bytes, the cap is {cap})")


def _reading(status: int, code: int | None, msg: str) -> str:
    low = msg.lower()
    if code in AUTH_CODES or code in TOKEN_INVALID_CODES or status == 401:
        return "unauthorized: the app credentials or token were refused"
    if code in _RATE_CODES or status == 429:
        return "rate limited"
    if code in _FORBIDDEN_CODES or status == 403:
        return "forbidden"
    if code in _GONE_MESSAGE_CODES:
        return "message not found"
    if code in _TOO_LONG_CODES or "too long" in low:
        return "message is too long"
    if "content format of the post type is incorrect" in low or code in _PARSE_CODES:
        return "can't parse the message content"
    if code == 230001:
        if "receive_id" in low or "chat_id" in low or "chat" in low:
            return "chat not found"
        return "can't parse the request"
    if status == 404:
        return "message not found"
    if status in (502, 503, 504):
        return "server unavailable, connect error"
    return "server error"


def _retry_after(response: httpx.Response) -> float | None:
    for header in ("x-ogw-ratelimit-reset", "Retry-After"):
        raw = response.headers.get(header, "").strip()
        try:
            if raw:
                return max(0.0, float(raw))
        except ValueError:
            continue
    return None


def _body(response: httpx.Response) -> dict[str, Any]:
    try:
        loaded = response.json()
    except (ValueError, UnicodeDecodeError):
        return {}
    return dict(loaded) if isinstance(loaded, dict) else {}  # pyright: ignore[reportUnknownArgumentType]


def error_of(response: httpx.Response, body: Mapping[str, Any] | None = None) -> FeishuError:
    data = body if body is not None else _body(response)
    raw_code = data.get("code")
    code = raw_code if isinstance(raw_code, int) and not isinstance(raw_code, bool) else None
    msg = str(data.get("msg") or data.get("message") or "")[:_DETAIL_CHARS]
    reading = _reading(response.status_code, code, msg)
    retry = _retry_after(response) if reading == "rate limited" else None
    return FeishuError(
        reading, code=code, status=response.status_code, detail=msg, retry_after=retry
    )


def base_url_for(domain: str) -> str:
    """The Open Platform host of ``domain`` (``feishu`` or ``lark``); ValueError otherwise."""
    found = DOMAINS.get(domain.strip().lower())
    if found is None:
        raise ValueError(f"unknown Feishu domain {domain!r}: use feishu or lark")
    return found


def _q(value: str) -> str:
    return quote(value, safe="")


class Endpoint:
    """The long connection's URL and the server's client settings."""

    def __init__(self, url: str, config: Mapping[str, Any]) -> None:
        self.url = url
        self.config = dict(config)

    def __repr__(self) -> str:  # the URL carries a connection ticket
        return "<Endpoint ****>"

    def number(self, key: str, default: float) -> float:
        value = self.config.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
            return float(value)
        return default

    @property
    def service_id(self) -> int:
        query = urlsplit(self.url).query
        for part in query.split("&"):
            name, _, value = part.partition("=")
            if name == "service_id" and value.isdigit():
                return int(value)
        return 0


class FeishuClient:
    """One app's session with the Open Platform (see the module notes). Close with ``aclose``."""

    def __init__(
        self,
        base_url: str,
        app_id: str,
        app_secret: str,
        *,
        clock: Clock,
        transport: httpx.AsyncBaseTransport | None = None,
        proxy: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        on_secret: Callable[[str], None] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._app_id = app_id
        self._app_secret = app_secret
        self._clock = clock
        # Told about every new tenant token and socket URL, so the owner can mask them.
        self._on_secret = on_secret or (lambda _value: None)
        self._token = ""
        self._token_expires = 0.0
        self._token_lock = asyncio.Lock()
        self.http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"User-Agent": "IndusAgent"},
            timeout=httpx.Timeout(timeout),
            transport=transport,
            proxy=None if transport is not None else proxy,
            trust_env=False,
            follow_redirects=False,
        )

    def __repr__(self) -> str:
        return f"<FeishuClient {self.base_url}>"

    async def aclose(self) -> None:
        await self.http.aclose()

    # -- token ---------------------------------------------------------------------------------

    def forget_token(self) -> None:
        self._token = ""
        self._token_expires = 0.0

    async def tenant_token(self, *, fresh: bool = False) -> str:
        """The cached tenant token, fetched when missing, near expiry or ``fresh``."""
        async with self._token_lock:
            now = self._clock.monotonic()
            if not fresh and self._token and now < self._token_expires - TOKEN_MARGIN_S:
                return self._token
            response = await self.http.post(
                "/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self._app_id, "app_secret": self._app_secret},
            )
            body = _body(response)
            token = body.get("tenant_access_token")
            if response.status_code >= 400 or body.get("code") != 0 or not isinstance(token, str):
                error = error_of(response, body)
                if response.status_code in (400, 401, 403) and error.code is None:
                    error = FeishuError(
                        "unauthorized: the app credentials were refused",
                        status=response.status_code,
                        detail=error.detail,
                    )
                raise error
            expire = body.get("expire")
            lifetime = float(expire) if isinstance(expire, int | float) else 7200.0
            self._on_secret(token)
            self._token = token
            self._token_expires = now + max(lifetime, 0.0)
            return token

    # -- calls ---------------------------------------------------------------------------------

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None,
        body: Any,
        files: Any,
        data: Any,
        fresh: bool,
    ) -> httpx.Response:
        token = await self.tenant_token(fresh=fresh)
        return await self.http.request(
            method,
            path,
            params=params,
            json=body,
            files=files,
            data=data,
            headers={"Authorization": f"Bearer {token}"},
        )

    async def call(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Any = None,
        files: Any = None,
        data: Any = None,
    ) -> dict[str, Any]:
        """One API call; the ``data`` object of a ``code: 0`` answer, `FeishuError` otherwise."""
        response = await self._send(
            method, path, params=params, body=body, files=files, data=data, fresh=False
        )
        parsed = _body(response)
        if parsed.get("code") in TOKEN_INVALID_CODES:
            response = await self._send(
                method, path, params=params, body=body, files=files, data=data, fresh=True
            )
            parsed = _body(response)
        if response.status_code >= 400 or parsed.get("code") != 0:
            if not parsed and response.status_code < 400:
                raise FeishuError("unexpected answer from the server", status=response.status_code)
            raise error_of(response, parsed)
        found = parsed.get("data")
        return dict(found) if isinstance(found, dict) else {}  # pyright: ignore[reportUnknownArgumentType]

    # -- identity and connection -----------------------------------------------------------------

    async def bot_info(self) -> dict[str, Any]:
        response = await self._send(
            "GET", "/open-apis/bot/v3/info", params=None, body=None, files=None, data=None,
            fresh=False,
        )  # fmt: skip
        parsed = _body(response)
        if response.status_code >= 400 or parsed.get("code") != 0:
            raise error_of(response, parsed)
        bot = parsed.get("bot")
        return dict(bot) if isinstance(bot, dict) else {}  # pyright: ignore[reportUnknownArgumentType]

    async def ws_endpoint(self) -> Endpoint:
        """The long connection's URL (it carries a one-time ticket: never log it)."""
        response = await self.http.post(
            "/callback/ws/endpoint",
            json={"AppID": self._app_id, "AppSecret": self._app_secret},
            headers={"locale": "en"},
        )
        parsed = _body(response)
        data = parsed.get("data")
        if response.status_code >= 400 or parsed.get("code") != 0 or not isinstance(data, dict):
            raise error_of(response, parsed)
        found: dict[str, Any] = dict(data)  # pyright: ignore[reportUnknownArgumentType]
        url = found.get("URL") or found.get("url")
        if not isinstance(url, str) or not url.startswith(("wss://", "ws://")):
            raise FeishuError("unexpected answer from the server: no connection URL")
        self._on_secret(url)
        config = found.get("ClientConfig")
        return Endpoint(url, dict(config) if isinstance(config, dict) else {})  # pyright: ignore[reportUnknownArgumentType]

    # -- messages --------------------------------------------------------------------------------

    async def create_message(
        self, receive_id_type: str, receive_id: str, msg_type: str, content: str
    ) -> dict[str, Any]:
        return await self.call(
            "POST",
            "/open-apis/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            body={
                "receive_id": receive_id,
                "msg_type": msg_type,
                "content": content,
                "uuid": str(uuid.uuid4()),
            },
        )

    async def reply_message(
        self, message_id: str, msg_type: str, content: str, *, in_thread: bool = False
    ) -> dict[str, Any]:
        return await self.call(
            "POST",
            f"/open-apis/im/v1/messages/{_q(message_id)}/reply",
            body={
                "msg_type": msg_type,
                "content": content,
                "reply_in_thread": in_thread,
                "uuid": str(uuid.uuid4()),
            },
        )

    async def update_message(self, message_id: str, msg_type: str, content: str) -> None:
        await self.call(
            "PUT",
            f"/open-apis/im/v1/messages/{_q(message_id)}",
            body={"msg_type": msg_type, "content": content},
        )

    async def patch_card(self, message_id: str, card: str) -> None:
        await self.call(
            "PATCH", f"/open-apis/im/v1/messages/{_q(message_id)}", body={"content": card}
        )

    async def delete_message(self, message_id: str) -> None:
        await self.call("DELETE", f"/open-apis/im/v1/messages/{_q(message_id)}")

    async def get_message(self, message_id: str) -> dict[str, Any]:
        data = await self.call("GET", f"/open-apis/im/v1/messages/{_q(message_id)}")
        items = data.get("items")
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return dict(items[0])  # pyright: ignore[reportUnknownArgumentType]
        return {}

    async def get_chat(self, chat_id: str) -> dict[str, Any]:
        return await self.call("GET", f"/open-apis/im/v1/chats/{_q(chat_id)}")

    async def get_user(self, open_id: str) -> dict[str, Any]:
        data = await self.call(
            "GET",
            f"/open-apis/contact/v3/users/{_q(open_id)}",
            params={"user_id_type": "open_id"},
        )
        user = data.get("user")
        return dict(user) if isinstance(user, dict) else {}  # pyright: ignore[reportUnknownArgumentType]

    async def add_reaction(self, message_id: str, emoji: str) -> str:
        data = await self.call(
            "POST",
            f"/open-apis/im/v1/messages/{_q(message_id)}/reactions",
            body={"reaction_type": {"emoji_type": emoji}},
        )
        return str(data.get("reaction_id") or "")

    async def delete_reaction(self, message_id: str, reaction_id: str) -> None:
        await self.call(
            "DELETE",
            f"/open-apis/im/v1/messages/{_q(message_id)}/reactions/{_q(reaction_id)}",
        )

    # -- files -----------------------------------------------------------------------------------

    async def upload_image(self, data: bytes, *, name: str) -> str:
        found = await self.call(
            "POST",
            "/open-apis/im/v1/images",
            data={"image_type": "message"},
            files={"image": (name, data, "application/octet-stream")},
        )
        key = str(found.get("image_key") or "")
        if not key:
            raise FeishuError("unexpected answer from the server: no image key")
        return key

    async def upload_file(
        self, data: bytes, *, name: str, file_type: str, duration_ms: int = 0
    ) -> str:
        fields: dict[str, str] = {"file_type": file_type, "file_name": name}
        if duration_ms > 0:
            fields["duration"] = str(duration_ms)
        found = await self.call(
            "POST",
            "/open-apis/im/v1/files",
            data=fields,
            files={"file": (name, data, "application/octet-stream")},
        )
        key = str(found.get("file_key") or "")
        if not key:
            raise FeishuError("unexpected answer from the server: no file key")
        return key

    async def download(
        self, message_id: str, file_key: str, resource_type: str, *, cap: int
    ) -> AsyncIterator[bytes]:
        """Stream one message resource; `MediaTooLarge` past ``cap`` bytes (declared or read)."""
        token = await self.tenant_token()
        path = f"/open-apis/im/v1/messages/{_q(message_id)}/resources/{_q(file_key)}"
        async with self.http.stream(
            "GET",
            path,
            params={"type": resource_type},
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(120.0),
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                raise error_of(response)
            declared = response.headers.get("Content-Length", "")
            if declared.isdigit() and int(declared) > cap:
                raise MediaTooLarge(int(declared), cap)
            seen = 0
            async for chunk in response.aiter_bytes():
                seen += len(chunk)
                if seen > cap:
                    raise MediaTooLarge(seen, cap)
                yield chunk

    # -- documents and comments --------------------------------------------------------------------

    async def doc_raw_content(self, document_id: str) -> str:
        data = await self.call("GET", f"/open-apis/docx/v1/documents/{_q(document_id)}/raw_content")
        return str(data.get("content") or "")

    async def list_comments(
        self,
        file_token: str,
        file_type: str,
        *,
        is_whole: bool | None = None,
        page_size: int = 100,
        page_token: str = "",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "file_type": file_type,
            "user_id_type": "open_id",
            "page_size": page_size,
        }
        if is_whole is not None:
            params["is_whole"] = "true" if is_whole else "false"
        if page_token:
            params["page_token"] = page_token
        return await self.call(
            "GET", f"/open-apis/drive/v1/files/{_q(file_token)}/comments", params=params
        )

    async def get_comment(self, file_token: str, file_type: str, comment_id: str) -> dict[str, Any]:
        return await self.call(
            "GET",
            f"/open-apis/drive/v1/files/{_q(file_token)}/comments/{_q(comment_id)}",
            params={"file_type": file_type, "user_id_type": "open_id"},
        )

    async def list_replies(
        self,
        file_token: str,
        file_type: str,
        comment_id: str,
        *,
        page_size: int = 100,
        page_token: str = "",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "file_type": file_type,
            "user_id_type": "open_id",
            "page_size": page_size,
        }
        if page_token:
            params["page_token"] = page_token
        return await self.call(
            "GET",
            f"/open-apis/drive/v1/files/{_q(file_token)}/comments/{_q(comment_id)}/replies",
            params=params,
        )

    async def reply_comment(
        self, file_token: str, file_type: str, comment_id: str, text: str
    ) -> dict[str, Any]:
        return await self.call(
            "POST",
            f"/open-apis/drive/v1/files/{_q(file_token)}/comments/{_q(comment_id)}/replies",
            params={"file_type": file_type},
            body={"content": {"elements": [{"type": "text_run", "text_run": {"text": text}}]}},
        )

    async def add_comment(self, file_token: str, file_type: str, text: str) -> dict[str, Any]:
        element = {"type": "text_run", "text_run": {"text": text}}
        return await self.call(
            "POST",
            f"/open-apis/drive/v1/files/{_q(file_token)}/comments",
            params={"file_type": file_type},
            body={"reply_list": {"replies": [{"content": {"elements": [element]}}]}},
        )


def json_content(value: Mapping[str, Any]) -> str:
    """A message ``content`` string (JSON text, non-ASCII kept)."""
    return json.dumps(value, ensure_ascii=False)


# -- the long connection ---------------------------------------------------------------------


class SocketLike(Protocol):
    """What the listener needs from a WebSocket session (httpx-ws's ``AsyncWebSocketSession``)."""

    async def receive_bytes(self) -> bytes: ...

    async def send_bytes(self, data: bytes) -> None: ...


type SocketOpener = Callable[[str], contextlib.AbstractAsyncContextManager[SocketLike]]


class _HiddenQuery(httpx.AsyncBaseTransport):
    """Adds the connection URL's query (its ticket) inside the transport, so httpx's request log
    line, ``response.request`` and error texts name the bare path only."""

    def __init__(self, inner: httpx.AsyncBaseTransport, query: str) -> None:
        self._inner = inner
        self._query = query

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._query:
            request = httpx.Request(
                request.method,
                request.url.copy_with(query=self._query.encode("ascii", "ignore")),
                headers=request.headers,
                stream=request.stream,
                extensions=request.extensions,
            )
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def _http_scheme(url: str) -> tuple[str, str]:
    """``wss://h/p?q`` → (``https://h/p``, ``q``)."""
    parts = urlsplit(url)
    scheme = {"wss": "https", "ws": "http"}.get(parts.scheme, parts.scheme)
    bare = urlunsplit((scheme, parts.netloc, parts.path, "", ""))
    return bare, parts.query


def httpx_ws_opener(
    proxy: str | None = None, *, transport: httpx.AsyncBaseTransport | None = None
) -> SocketOpener:
    """The production opener: ``httpx_ws.aconnect_ws`` on a client of its own (the ``feishu``
    extra; imported only when a socket opens). ``transport`` replaces the network (tests)."""

    @contextlib.asynccontextmanager
    async def open_socket(url: str) -> AsyncIterator[SocketLike]:
        try:
            import httpx_ws  # pyright: ignore[reportMissingImports]
        except ModuleNotFoundError as exc:
            raise ModuleNotFoundError(
                f"the Feishu long connection needs httpx-ws: {INSTALL_HINT}", name=exc.name
            ) from None
        bare, query = _http_scheme(url)
        inner = transport if transport is not None else httpx.AsyncHTTPTransport(proxy=proxy)
        async with (
            httpx.AsyncClient(
                transport=_HiddenQuery(inner, query),
                timeout=httpx.Timeout(DEFAULT_TIMEOUT_S),
                trust_env=False,
                follow_redirects=False,
            ) as client,
            httpx_ws.aconnect_ws(  # pyright: ignore[reportUnknownMemberType]
                bare, client, max_message_size_bytes=8 * 1024 * 1024
            ) as socket,
        ):
            yield socket  # pyright: ignore[reportUnknownArgumentType]

    return open_socket
