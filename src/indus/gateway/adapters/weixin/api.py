"""A small iLink Bot client over httpx (P16-19).

Every iLink call is a JSON ``POST <base>/ilink/bot/<name>`` carrying ``base_info.channel_version``
and the bot token as ``Authorization: Bearer`` (with ``AuthorizationType: ilink_bot_token``, a
random ``X-WECHAT-UIN`` per request and the app id / client version headers the vendor's own
client sends). The token travels only in that header and only to the iLink base URL: CDN uploads
and downloads carry no credential. Redirects are not followed.

Errors come back two ways and both become an `ILinkError` whose text starts with a reading the
send classifier (P15-04) knows: an HTTP status (429 rate limited with ``Retry-After``, 401
unauthorized, 403 forbidden, 404 chat/message not found, 400 can't parse, 413 too long, 502–504
connection-level), or ``ret`` / ``errcode`` in a 200 answer (``-14`` session expired, ``-2``
frequency limit — or, with ``errmsg`` "unknown error" / "prepare failed", a session that is not
ready). The poll's client-side read timeout is an empty answer, not an error.
"""

from __future__ import annotations

import base64
import json
import secrets
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

import httpx

__all__ = [
    "CDN_BASE",
    "CHANNEL_VERSION",
    "FREQUENCY_LIMIT",
    "ILINK_BASE",
    "ROUTES",
    "SESSION_EXPIRED",
    "ILinkClient",
    "ILinkError",
    "MediaTooLarge",
]

ILINK_BASE = "https://ilinkai.weixin.qq.com"
CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"
CHANNEL_VERSION = "2.2.0"
APP_ID = "bot"
CLIENT_VERSION = str((2 << 16) | (2 << 8) | 0)
POLL_TIMEOUT_MS = 35_000
POLL_GRACE_S = 10.0
API_TIMEOUT_S = 15.0
CONFIG_TIMEOUT_S = 10.0
CDN_TIMEOUT_S = 120.0
SESSION_EXPIRED = -14
FREQUENCY_LIMIT = -2
_NOT_READY = frozenset({"unknown error", "prepare failed"})

# Method → the vendor route it calls (the wire fake serves every one).
ROUTES: Mapping[str, str] = {
    "get_updates": "POST /ilink/bot/getupdates",
    "send_message": "POST /ilink/bot/sendmessage",
    "send_typing": "POST /ilink/bot/sendtyping",
    "get_config": "POST /ilink/bot/getconfig",
    "get_upload_url": "POST /ilink/bot/getuploadurl",
    "cdn_upload": "POST <cdn>/upload",
    "cdn_download": "GET <cdn>/download",
}


class ILinkError(Exception):
    """A refused or failed iLink call (see the module notes for the readings)."""

    def __init__(
        self,
        reading: str,
        *,
        status: int = 0,
        code: int | None = None,
        detail: str = "",
        retry_after: float | None = None,
    ) -> None:
        self.reading = reading
        self.status = status
        self.code = code
        self.detail = detail
        self.retry_after = retry_after
        where = ", ".join(p for p in (f"HTTP {status}" if status else "", _code(code)) if p)
        text = f"{reading} ({where})" if where else reading
        super().__init__(f"{text}: {detail}" if detail else text)

    @property
    def is_auth(self) -> bool:
        return self.status == 401

    @property
    def session_expired(self) -> bool:
        return self.code == SESSION_EXPIRED

    @property
    def not_ready(self) -> bool:
        return self.reading.startswith("session not ready")

    @property
    def frequency_limited(self) -> bool:
        return self.code == FREQUENCY_LIMIT and not self.not_ready


class MediaTooLarge(Exception):
    def __init__(self, size: int, cap: int) -> None:
        super().__init__(f"the file is too large ({size} bytes, the cap is {cap})")


def _code(code: int | None) -> str:
    return f"ret {code}" if code is not None else ""


def _status_reading(status: int, detail: str) -> str:
    low = detail.lower()
    if status == 429:
        return "rate limited"
    if status == 401:
        return "unauthorized: the bot token was refused"
    if status == 403:
        return "forbidden"
    if status == 404:
        return "message not found" if ("message" in low or "thread" in low) else "chat not found"
    if status == 413:
        return "message is too long"
    if status == 400:
        return "can't parse the request"
    if status in (502, 503, 504):
        return "iLink unavailable, connect error"
    return "iLink error"


def answer_error(answer: Mapping[str, Any]) -> ILinkError | None:
    """The error a 200 answer reports in ``ret`` / ``errcode``, or None."""
    codes = [c for c in (answer.get("ret"), answer.get("errcode")) if isinstance(c, int) and c]
    if not codes:
        return None
    code = codes[0]
    message = str(answer.get("errmsg") or answer.get("msg") or "")[:200]
    if SESSION_EXPIRED in codes:
        return ILinkError("session expired", code=SESSION_EXPIRED, detail=message)
    if FREQUENCY_LIMIT in codes:
        if message.strip().lower() in _NOT_READY:
            return ILinkError(
                "session not ready: the person has to message the bot first",
                code=FREQUENCY_LIMIT,
                detail=message,
            )
        return ILinkError(
            "rate limited: iLink frequency limit", code=FREQUENCY_LIMIT, detail=message
        )
    return ILinkError("iLink refused the request", code=code, detail=message)


def _uin() -> str:
    """``X-WECHAT-UIN``: base64 of a random unsigned 32-bit number written in decimal."""
    return base64.b64encode(str(secrets.randbits(32)).encode("ascii")).decode("ascii")


class ILinkClient:
    """One bot session against iLink and its CDN. Close it with ``aclose``."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        cdn_base_url: str = CDN_BASE,
        transport: httpx.AsyncBaseTransport | None = None,
        proxy: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.cdn_base_url = cdn_base_url.rstrip("/")
        self._token = token
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(API_TIMEOUT_S),
            transport=transport,
            proxy=None if transport is not None else proxy,
            trust_env=False,
            follow_redirects=False,
            headers={"User-Agent": "IndusAgent"},
        )

    def __repr__(self) -> str:
        return f"<ILinkClient {self.base_url}>"

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- plumbing ----------------------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "Authorization": f"Bearer {self._token}",
            "X-WECHAT-UIN": _uin(),
            "iLink-App-Id": APP_ID,
            "iLink-App-ClientVersion": CLIENT_VERSION,
        }

    async def call(
        self, name: str, payload: Mapping[str, Any], *, timeout: float = API_TIMEOUT_S
    ) -> dict[str, Any]:
        """POST ``ilink/bot/<name>``; the JSON answer (HTTP errors raised, ``ret`` not checked)."""
        body = json.dumps(
            {**payload, "base_info": {"channel_version": CHANNEL_VERSION}},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        response = await self._http.post(
            f"{self.base_url}/ilink/bot/{name}",
            content=body,
            headers=self._headers(),
            timeout=httpx.Timeout(timeout),
        )
        if response.status_code >= 400:
            raise _http_error(response)
        try:
            data = response.json()
        except (ValueError, UnicodeDecodeError):
            raise ILinkError("unexpected answer from iLink", status=response.status_code) from None
        if not isinstance(data, dict):
            raise ILinkError("unexpected answer from iLink", status=response.status_code)
        return dict(data)  # pyright: ignore[reportUnknownArgumentType]

    async def checked(
        self, name: str, payload: Mapping[str, Any], *, timeout: float = API_TIMEOUT_S
    ) -> dict[str, Any]:
        answer = await self.call(name, payload, timeout=timeout)
        error = answer_error(answer)
        if error is not None:
            raise error
        return answer

    # -- the bot API -------------------------------------------------------------------------------

    async def get_updates(
        self, cursor: str, *, timeout_ms: int = POLL_TIMEOUT_MS
    ) -> dict[str, Any]:
        """One long poll; a client-side read timeout is an empty answer for the same cursor."""
        try:
            return await self.call(
                "getupdates",
                {"get_updates_buf": cursor},
                timeout=timeout_ms / 1000.0 + POLL_GRACE_S,
            )
        except httpx.ReadTimeout:
            return {"ret": 0, "msgs": [], "get_updates_buf": cursor}

    async def send_message(
        self, to: str, items: Sequence[Mapping[str, Any]], *, context_token: str, client_id: str
    ) -> dict[str, Any]:
        message: dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": to,
            "client_id": client_id,
            "message_type": 2,
            "message_state": 2,
            "item_list": [dict(i) for i in items],
            "context_token": context_token,
        }
        return await self.checked("sendmessage", {"msg": message})

    async def get_config(self, user_id: str, *, context_token: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"ilink_user_id": user_id}
        if context_token:
            payload["context_token"] = context_token
        return await self.call("getconfig", payload, timeout=CONFIG_TIMEOUT_S)

    async def send_typing(self, user_id: str, ticket: str, *, on: bool) -> None:
        await self.checked(
            "sendtyping",
            {"ilink_user_id": user_id, "typing_ticket": ticket, "status": 1 if on else 2},
            timeout=CONFIG_TIMEOUT_S,
        )

    async def get_upload_url(
        self,
        *,
        filekey: str,
        media_type: int,
        to: str,
        raw_size: int,
        raw_md5: str,
        size: int,
        key_hex: str,
    ) -> dict[str, Any]:
        return await self.checked(
            "getuploadurl",
            {
                "filekey": filekey,
                "media_type": media_type,
                "to_user_id": to,
                "rawsize": raw_size,
                "rawfilemd5": raw_md5,
                "filesize": size,
                "no_need_thumb": True,
                "aeskey": key_hex,
            },
        )

    # -- the CDN (no credential) -------------------------------------------------------------------

    async def cdn_upload(self, url: str, data: bytes) -> str:
        """POST the ciphertext; the CDN's ``x-encrypted-param`` (the download parameter)."""
        response = await self._http.post(
            url,
            content=data,
            headers={"Content-Type": "application/octet-stream"},
            timeout=httpx.Timeout(CDN_TIMEOUT_S),
        )
        if response.status_code >= 400:
            raise _http_error(response)
        param = response.headers.get("x-encrypted-param", "")
        if not param:
            raise ILinkError("the CDN accepted the upload but gave no download parameter")
        return param

    async def cdn_download(self, url: str, *, cap: int) -> AsyncIterator[bytes]:
        async with self._http.stream("GET", url, timeout=httpx.Timeout(CDN_TIMEOUT_S)) as response:
            if response.status_code >= 400:
                await response.aread()
                raise _http_error(response)
            declared = response.headers.get("Content-Length", "")
            if declared.isdigit() and int(declared) > cap:
                raise MediaTooLarge(int(declared), cap)
            seen = 0
            async for chunk in response.aiter_bytes():
                seen += len(chunk)
                if seen > cap:
                    raise MediaTooLarge(seen, cap)
                yield chunk


def _http_error(response: httpx.Response) -> ILinkError:
    detail = ""
    try:
        loaded = response.json()
        if isinstance(loaded, dict):
            detail = str(loaded.get("errmsg") or loaded.get("msg") or "")  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    except (ValueError, UnicodeDecodeError):
        detail = ""
    status = response.status_code
    retry: float | None = None
    if status == 429:
        header = response.headers.get("Retry-After", "").strip()
        try:
            retry = float(header) if header else None
        except ValueError:
            retry = None
    return ILinkError(
        _status_reading(status, detail), status=status, detail=detail[:200], retry_after=retry
    )
