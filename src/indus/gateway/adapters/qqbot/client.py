"""A small QQ Bot API v2 client over httpx, the access-token source and the socket opener (P16-20).

Token: ``POST https://bots.qq.com/app/getAppAccessToken`` with the app id and client secret gives an
access token (``expires_in`` seconds, sent as a string); it is cached until a minute before it
expires and refreshed by one caller at a time. REST calls go to ``https://api.sgroup.qq.com`` (the
sandbox host when configured) with ``Authorization: QQBot <token>`` and ``X-Union-Appid``; a 401
drops the token and the call is tried once more. The secret and the token never appear in a URL,
an error, a log line or a repr.

Errors: a non-2xx answer raises `QQApiError` with the HTTP status, QQ's business ``code`` and its
message; the adapter classifies it with the listener kit's ``failure_from_status`` (by status, not
by text). httpx's own errors pass through for ``failure_from_exception`` (a read timeout is never
retried).

Paths carry ids QQ issued; they are checked against a conservative character set first, so a
crafted id cannot leave its path segment. Media downloads go only to https QQ hosts.

The gateway WebSocket goes through ``httpx-ws`` (the ``qqbot`` extra), imported only when the
listener opens a socket; `SocketClosed` carries the server's close code.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

import httpx

from indus.foundation.clock import Clock
from indus.foundation.digits import is_digits

__all__ = [
    "API_BASE",
    "INSTALL_HINT",
    "ROUTES",
    "SANDBOX_BASE",
    "TOKEN_URL",
    "MediaTooLarge",
    "QQApiError",
    "QQClient",
    "QQSocket",
    "QQTokens",
    "SocketClosed",
    "SocketOpener",
    "TokenError",
    "httpx_ws_opener",
    "is_qq_media_url",
    "safe_id",
]

API_BASE = "https://api.sgroup.qq.com"
SANDBOX_BASE = "https://sandbox.api.sgroup.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
INSTALL_HINT = "pip install 'indus-agent[qqbot]'"
REFRESH_EARLY_S = 60.0
DEFAULT_TIMEOUT_S = 30.0
UPLOAD_TIMEOUT_S = 120.0
PART_TIMEOUT_S = 300.0
MEDIA_HOSTS: tuple[str, ...] = ("qq.com", "qq.com.cn", "qpic.cn", "gtimg.cn")

ROUTES: Mapping[str, str] = {
    "token": "POST https://bots.qq.com/app/getAppAccessToken",
    "gateway": "GET /gateway",
    "send_c2c": "POST /v2/users/{openid}/messages",
    "send_group": "POST /v2/groups/{group_openid}/messages",
    "send_channel": "POST /channels/{channel_id}/messages",
    "send_dm": "POST /dms/{guild_id}/messages",
    "upload_file": "POST /v2/{users|groups}/{id}/files",
    "upload_prepare": "POST /v2/{users|groups}/{id}/upload_prepare",
    "upload_part_finish": "POST /v2/{users|groups}/{id}/upload_part_finish",
    "ack_interaction": "PUT /interactions/{id}",
}

_ID = re.compile(r"^[A-Za-z0-9_.=-]{1,128}$")
_SNIPPET = 300


def safe_id(value: str) -> str:
    """``value`` when it is a plausible QQ id (``ValueError`` otherwise)."""
    if not _ID.match(value or ""):
        raise ValueError("not a QQ id")
    return value


def is_qq_media_url(url: str) -> bool:
    """https on a QQ media host (or a subdomain), no credentials in the URL."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "https" or not host or parts.username or parts.password:
        return False
    return any(host == h or host.endswith("." + h) for h in MEDIA_HOSTS)


class TokenError(Exception):
    """The token endpoint refused (``retryable`` false: bad credentials) or failed."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class QQApiError(Exception):
    """A non-2xx QQ answer: HTTP ``status``, QQ's business ``code`` and message."""

    def __init__(
        self,
        status: int,
        detail: str = "",
        *,
        code: int | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.detail = " ".join(detail.split())[:_SNIPPET]
        self.headers: dict[str, str] = dict(headers or {})
        where = f"HTTP {status}" + (f", code {code}" if code is not None else "")
        super().__init__(f"QQ API {where}: {self.detail}" if self.detail else f"QQ API {where}")


class MediaTooLarge(Exception):
    def __init__(self, size: int, cap: int) -> None:
        super().__init__(f"the attachment is too large ({size} bytes, the cap is {cap})")


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and is_digits(value.strip(), signed=True):
        return int(value.strip())
    return None


def _error_of(response: httpx.Response) -> QQApiError:
    code: int | None = None
    detail = ""
    try:
        body = response.json()
    except (ValueError, UnicodeDecodeError):
        body = None
    if isinstance(body, dict):
        fields = cast(dict[str, Any], body)
        code = _int(fields.get("code"))
        if code is None:
            code = _int(fields.get("err_code"))
        detail = str(fields.get("message") or fields.get("msg") or "")
    return QQApiError(response.status_code, detail, code=code, headers=response.headers)


class QQTokens:
    """The bot's access token (see the module notes)."""

    def __init__(
        self,
        app_id: str,
        secret: str,
        http: httpx.AsyncClient,
        *,
        clock: Clock,
        url: str = TOKEN_URL,
    ) -> None:
        self.app_id = app_id
        self._secret = secret
        self._http = http
        self.clock = clock
        self.url = url
        self._token = ""
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        self.fetches = 0

    def __repr__(self) -> str:
        return f"<QQTokens app {self.app_id}>"

    def _fresh(self) -> bool:
        return bool(self._token) and self.clock.monotonic() < self._expires_at - REFRESH_EARLY_S

    def invalidate(self) -> None:
        self._token = ""
        self._expires_at = 0.0

    @property
    def current(self) -> str:
        """The cached token ('' when none): for the socket's Identify/Resume frames."""
        return self._token

    async def token(self) -> str:
        if self._fresh():
            return self._token
        async with self._lock:
            if not self._fresh():
                await self._fetch()
            return self._token

    async def _fetch(self) -> None:
        self.fetches += 1
        body = {"appId": self.app_id, "clientSecret": self._secret}
        try:
            answer = await self._http.post(self.url, json=body)
        except httpx.HTTPError as exc:
            raise TokenError(
                f"the QQ token endpoint could not be reached: {type(exc).__name__}",
                retryable=True,
            ) from None
        if answer.status_code >= 500 or answer.status_code == 429:
            raise TokenError(
                f"the QQ token endpoint answered HTTP {answer.status_code}", retryable=True
            )
        try:
            data = answer.json()
        except (ValueError, UnicodeDecodeError):
            data = None
        fields = cast(dict[str, Any], data) if isinstance(data, dict) else {}
        token = str(fields.get("access_token") or "")
        if answer.status_code >= 400 or not token:
            code = _int(fields.get("code"))
            if answer.status_code >= 400 or code is not None:
                raise TokenError(
                    f"QQ refused the app id / client secret (HTTP {answer.status_code}"
                    + (f", code {code})" if code is not None else ")"),
                    retryable=False,
                )
            raise TokenError("the QQ token endpoint answered without a token", retryable=True)
        lifetime = _int(fields.get("expires_in")) or 7200
        self._token = token
        self._expires_at = self.clock.monotonic() + max(float(lifetime), REFRESH_EARLY_S + 1)


class QQClient:
    """QQ Bot REST calls (see ``ROUTES``)."""

    def __init__(self, http: httpx.AsyncClient, tokens: QQTokens, *, base: str = API_BASE):
        self.http = http
        self.tokens = tokens
        self.base = base.rstrip("/")

    def __repr__(self) -> str:
        return f"<QQClient {self.base}>"

    async def _headers(self) -> dict[str, str]:
        token = await self.tokens.token()
        return {"Authorization": f"QQBot {token}", "X-Union-Appid": self.tokens.app_id}

    async def call(
        self, method: str, path: str, body: Any = None, *, timeout: float = DEFAULT_TIMEOUT_S
    ) -> dict[str, Any]:
        for attempt in (0, 1):
            response = await self.http.request(
                method,
                f"{self.base}{path}",
                json=body,
                headers=await self._headers(),
                timeout=httpx.Timeout(timeout),
            )
            if response.status_code == 401 and attempt == 0:
                self.tokens.invalidate()
                continue
            if response.status_code >= 400:
                raise _error_of(response)
            if not response.content:
                return {}
            try:
                data = response.json()
            except (ValueError, UnicodeDecodeError):
                raise QQApiError(response.status_code, "unexpected answer") from None
            if not isinstance(data, dict):
                return {}
            fields = cast(dict[str, Any], data)
            # Some failures come back as 200 with a business code and no id.
            code = _int(fields.get("code"))
            if code not in (None, 0) and "id" not in fields and "file_info" not in fields:
                raise QQApiError(response.status_code, str(fields.get("message") or ""), code=code)
            return fields
        raise AssertionError("unreachable")  # pragma: no cover

    async def gateway_url(self) -> str:
        data = await self.call("GET", "/gateway")
        url = str(data.get("url") or "")
        if not url.startswith(("wss://", "ws://")):
            raise QQApiError(200, "the gateway answer has no WebSocket URL")
        return url

    async def send_message(self, kind: str, target: str, body: Mapping[str, Any]) -> dict[str, Any]:
        """``kind``: ``c2c``, ``group``, ``channel`` or ``dm`` (a guild DM)."""
        target = safe_id(target)
        path = {
            "c2c": f"/v2/users/{target}/messages",
            "group": f"/v2/groups/{target}/messages",
            "channel": f"/channels/{target}/messages",
            "dm": f"/dms/{target}/messages",
        }[kind]
        return await self.call("POST", path, dict(body))

    def _files_path(self, kind: str, target: str, endpoint: str) -> str:
        if kind not in ("c2c", "group"):
            raise ValueError("media uploads exist for C2C and group chats only")
        folder = "users" if kind == "c2c" else "groups"
        return f"/v2/{folder}/{safe_id(target)}/{endpoint}"

    async def files_call(
        self, kind: str, target: str, endpoint: str, body: Mapping[str, Any]
    ) -> dict[str, Any]:
        """``files``, ``upload_prepare`` or ``upload_part_finish`` for a C2C user or a group."""
        path = self._files_path(kind, target, endpoint)
        return await self.call("POST", path, dict(body), timeout=UPLOAD_TIMEOUT_S)

    async def ack_interaction(self, interaction_id: str, code: int = 0) -> None:
        await self.call("PUT", f"/interactions/{safe_id(interaction_id)}", {"code": code})

    async def put_part(self, url: str, data: bytes) -> int:
        """PUT one upload part to its pre-signed URL (no QQ token: the URL carries its own
        signature); the HTTP status."""
        if urlsplit(url).scheme != "https":
            raise ValueError("upload part URLs must be https")
        response = await self.http.put(
            url,
            content=data,
            headers={"Content-Length": str(len(data))},
            timeout=httpx.Timeout(PART_TIMEOUT_S),
        )
        return response.status_code

    async def download(self, url: str, *, cap: int) -> AsyncIterator[bytes]:
        """Stream an inbound attachment from a QQ media host (``MediaTooLarge`` past ``cap``)."""
        if not is_qq_media_url(url):
            raise ValueError("refusing to fetch a QQ attachment from a non-QQ host")
        headers = await self._headers()
        async with self.http.stream("GET", url, headers=headers) as response:
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


# -- the gateway socket ----------------------------------------------------------------------


class SocketClosed(Exception):
    """The gateway socket closed; ``code`` is the server's close code (None when unknown)."""

    def __init__(self, code: int | None = None, reason: str = "") -> None:
        self.code = code
        self.reason = reason
        super().__init__(f"the QQ gateway closed the socket (code {code}) {reason}".strip())


class QQSocket(Protocol):
    async def receive_text(self) -> str: ...

    async def send_text(self, data: str) -> None: ...


type SocketOpener = Callable[[str], contextlib.AbstractAsyncContextManager[QQSocket]]


class _WsSession:
    """httpx-ws's session behind `QQSocket`, close codes turned into `SocketClosed`."""

    def __init__(self, session: Any, disconnect: type[BaseException]) -> None:
        self._session = session
        self._disconnect = disconnect

    async def receive_text(self) -> str:
        try:
            return str(await self._session.receive_text())
        except self._disconnect as exc:
            raise SocketClosed(
                getattr(exc, "code", None), str(getattr(exc, "reason", ""))
            ) from None

    async def send_text(self, data: str) -> None:
        await self._session.send_text(data)


def httpx_ws_opener(http: httpx.AsyncClient) -> SocketOpener:
    """The production opener: ``httpx_ws.aconnect_ws`` over the REST client's connection pool."""

    @contextlib.asynccontextmanager
    async def open_socket(url: str) -> AsyncIterator[QQSocket]:
        try:
            import httpx_ws  # pyright: ignore[reportMissingImports]
        except ModuleNotFoundError as exc:
            raise ModuleNotFoundError(
                f"the QQ gateway socket needs httpx-ws: {INSTALL_HINT}", name=exc.name
            ) from None
        target = re.sub(r"^ws", "http", url, count=1)  # wss → https, ws → http
        async with httpx_ws.aconnect_ws(  # pyright: ignore[reportUnknownMemberType]
            target, http, max_message_size_bytes=4 * 1024 * 1024
        ) as session:
            yield _WsSession(session, httpx_ws.WebSocketDisconnect)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]

    return open_socket
