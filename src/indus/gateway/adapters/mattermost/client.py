"""A small Mattermost REST API v4 client over httpx, and the WebSocket opener (P16-08).

Only the requests the adapter makes (``ROUTES``). The token travels in the ``Authorization``
header (REST and the WebSocket upgrade alike), never in a URL or a message body.

Every non-2xx answer becomes a `MattermostError` whose text starts with a reading the send
classifier (P15-04) recognises, chosen from the server's error id (``api.post.create_post.
root_id.app_error`` and friends) and status: ``rate limited`` (with ``Retry-After`` /
``X-Ratelimit-Reset`` seconds), ``forbidden``, ``channel not found``, ``thread not found``,
``message not found``, ``message is too long``, ``can't parse``, a connection-level reading for
502/503/504, and ``unauthorized`` for a refused token (final, never a dead chat).

The WebSocket goes through ``httpx-ws`` (the ``mattermost`` extra), imported only when the
listener opens a socket.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any, Protocol
from urllib.parse import quote

import httpx

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "ROUTES",
    "MattermostClient",
    "MattermostError",
    "SocketOpener",
    "WebSocketLike",
    "httpx_ws_opener",
]

DEFAULT_TIMEOUT_S = 30.0
INSTALL_HINT = "pip install 'indus-agent[mattermost]'"

ROUTES: Mapping[str, str] = {
    "me": "GET /api/v4/users/me",
    "create_post": "POST /api/v4/posts",
    "patch_post": "PUT /api/v4/posts/{postId}/patch",
    "delete_post": "DELETE /api/v4/posts/{postId}",
    "get_post": "GET /api/v4/posts/{postId}",
    "typing": "POST /api/v4/users/{userId}/typing",
    "channel": "GET /api/v4/channels/{channelId}",
    "upload": "POST /api/v4/files",
    "download": "GET /api/v4/files/{fileId}",
    "teams": "GET /api/v4/users/me/teams",
    "team_channels": "GET /api/v4/users/me/teams/{teamId}/channels",
    "add_reaction": "POST /api/v4/reactions",
    "remove_reaction": "DELETE /api/v4/users/{userId}/posts/{postId}/reactions/{emojiName}",
    "websocket": "GET /api/v4/websocket",
}


class MattermostError(Exception):
    def __init__(
        self,
        reading: str,
        *,
        status: int = 0,
        error_id: str = "",
        detail: str = "",
        retry_after: float | None = None,
    ) -> None:
        self.reading = reading
        self.status = status
        self.error_id = error_id
        self.detail = detail
        self.retry_after = retry_after
        where = ", ".join(p for p in (f"HTTP {status}" if status else "", error_id) if p)
        text = f"{reading} ({where})" if where else reading
        super().__init__(f"{text}: {detail}" if detail else text)

    @property
    def is_auth(self) -> bool:
        return self.status == 401


class MediaTooLarge(Exception):
    def __init__(self, size: int, cap: int) -> None:
        super().__init__(f"the file is too large ({size} bytes, the cap is {cap})")


def _reading(status: int, error_id: str, detail: str) -> str:
    low = f"{error_id} {detail}".lower()
    if status == 429:
        return "rate limited"
    if status == 401:
        return "unauthorized: the token was refused"
    if status == 403:
        return "forbidden"
    if "root_id" in low or "rootid" in low:
        return "thread not found"
    if status == 413 or "too long" in low or "msg.app_error" in low or "message_length" in low:
        return "message is too long"
    if status == 404:
        return "channel not found" if "channel" in low else "message not found"
    if status == 400 and ("invalid_body" in low or "json" in low or "body_param" in low):
        return "can't parse the request body"
    if status in (502, 503, 504):
        return "server unavailable, connect error"
    return "server error"


def _retry_after(response: httpx.Response) -> float | None:
    for header in ("Retry-After", "X-Ratelimit-Reset"):
        raw = response.headers.get(header, "").strip()
        try:
            if raw:
                return max(0.0, float(raw))
        except ValueError:
            continue
    return None


def error_of(response: httpx.Response) -> MattermostError:
    body: dict[str, Any] = {}
    try:
        loaded = response.json()
        if isinstance(loaded, dict):
            body = dict(loaded)  # pyright: ignore[reportUnknownArgumentType]
    except (ValueError, UnicodeDecodeError):
        body = {}
    error_id = str(body.get("id") or "")
    detail = str(body.get("message") or "")[:300]
    status = response.status_code
    reading = _reading(status, error_id, detail)
    retry = _retry_after(response) if reading == "rate limited" else None
    return MattermostError(
        reading, status=status, error_id=error_id, detail=detail, retry_after=retry
    )


def _q(value: str) -> str:
    return quote(value, safe="")


class WebSocketLike(Protocol):
    """What the listener needs from a WebSocket session (httpx-ws's ``AsyncWebSocketSession``)."""

    async def receive_text(self) -> str: ...

    async def send_text(self, data: str) -> None: ...


type SocketOpener = Callable[
    [httpx.AsyncClient, str], contextlib.AbstractAsyncContextManager[WebSocketLike]
]


def httpx_ws_opener(client: httpx.AsyncClient, url: str) -> Any:
    """The production opener: ``httpx_ws.aconnect_ws`` on the REST client (headers shared)."""
    try:
        import httpx_ws  # pyright: ignore[reportMissingImports]
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            f"the Mattermost WebSocket needs httpx-ws: {INSTALL_HINT}", name=exc.name
        ) from None
    return httpx_ws.aconnect_ws(  # pyright: ignore[reportUnknownMemberType]
        url, client, max_message_size_bytes=4 * 1024 * 1024
    )


class MattermostClient:
    """One server session (see the module notes). Close it with ``aclose``."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        proxy: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {token}", "User-Agent": "IndusAgent"},
            timeout=httpx.Timeout(timeout),
            transport=transport,
            proxy=None if transport is not None else proxy,
            trust_env=False,
            follow_redirects=False,
        )

    def __repr__(self) -> str:
        return f"<MattermostClient {self.base_url}>"

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _call(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        timeout: float | None = None,
    ) -> Any:
        response = await self.http.request(
            method,
            path,
            json=body,
            timeout=httpx.Timeout(timeout) if timeout is not None else httpx.USE_CLIENT_DEFAULT,
        )
        if response.status_code >= 400:
            raise error_of(response)
        if not response.content:
            return {}
        try:
            return response.json()
        except (ValueError, UnicodeDecodeError):
            raise MattermostError(
                "unexpected answer from the server", status=response.status_code
            ) from None

    async def _object(self, method: str, path: str, *, body: Any = None) -> dict[str, Any]:
        data = await self._call(method, path, body=body)
        if not isinstance(data, dict):
            raise MattermostError("unexpected answer from the server")
        return dict(data)  # pyright: ignore[reportUnknownArgumentType]

    async def _list(self, path: str) -> list[dict[str, Any]]:
        data = await self._call("GET", path)
        if not isinstance(data, list):
            raise MattermostError("unexpected answer from the server")
        return [dict(item) for item in data if isinstance(item, dict)]  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]

    # -- users and posts ---------------------------------------------------------------------------

    async def me(self) -> dict[str, Any]:
        return await self._object("GET", "/api/v4/users/me")

    async def create_post(
        self,
        channel_id: str,
        message: str,
        *,
        root_id: str | None = None,
        file_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"channel_id": channel_id, "message": message}
        if root_id:
            body["root_id"] = root_id
        if file_ids:
            body["file_ids"] = file_ids
        return await self._object("POST", "/api/v4/posts", body=body)

    async def patch_post(self, post_id: str, message: str) -> dict[str, Any]:
        return await self._object(
            "PUT", f"/api/v4/posts/{_q(post_id)}/patch", body={"message": message}
        )

    async def delete_post(self, post_id: str) -> None:
        await self._call("DELETE", f"/api/v4/posts/{_q(post_id)}")

    async def get_post(self, post_id: str) -> dict[str, Any]:
        return await self._object("GET", f"/api/v4/posts/{_q(post_id)}")

    async def typing(self, user_id: str, channel_id: str, parent_id: str = "") -> None:
        body = {"channel_id": channel_id, "parent_id": parent_id}
        await self._call("POST", f"/api/v4/users/{_q(user_id)}/typing", body=body)

    async def channel(self, channel_id: str) -> dict[str, Any]:
        return await self._object("GET", f"/api/v4/channels/{_q(channel_id)}")

    async def teams(self) -> list[dict[str, Any]]:
        return await self._list("/api/v4/users/me/teams")

    async def team_channels(self, team_id: str) -> list[dict[str, Any]]:
        return await self._list(f"/api/v4/users/me/teams/{_q(team_id)}/channels")

    async def add_reaction(self, user_id: str, post_id: str, emoji: str) -> None:
        body = {"user_id": user_id, "post_id": post_id, "emoji_name": emoji}
        await self._call("POST", "/api/v4/reactions", body=body)

    async def remove_reaction(self, user_id: str, post_id: str, emoji: str) -> None:
        await self._call(
            "DELETE",
            f"/api/v4/users/{_q(user_id)}/posts/{_q(post_id)}/reactions/{_q(emoji)}",
        )

    # -- files -------------------------------------------------------------------------------------

    async def upload(self, channel_id: str, data: bytes, *, name: str, mime: str) -> str:
        response = await self.http.post(
            "/api/v4/files",
            data={"channel_id": channel_id},
            files={"files": (name, data, mime or "application/octet-stream")},
            timeout=httpx.Timeout(120.0),
        )
        if response.status_code >= 400:
            raise error_of(response)
        try:
            infos = response.json().get("file_infos") or []
            file_id = str(infos[0].get("id") or "")
        except (ValueError, AttributeError, IndexError, TypeError):
            file_id = ""
        if not file_id:
            raise MattermostError("unexpected answer from the server: no file id")
        return file_id

    async def download(self, file_id: str, *, cap: int) -> AsyncIterator[bytes]:
        """Stream a file; raise `MediaTooLarge` past ``cap`` bytes (declared or read)."""
        path = f"/api/v4/files/{_q(file_id)}"
        async with self.http.stream("GET", path, timeout=httpx.Timeout(120.0)) as response:
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

    def websocket_url(self) -> str:
        """The event socket (http(s) scheme: the upgrade is an HTTP request on this client)."""
        return f"{self.base_url}/api/v4/websocket"
