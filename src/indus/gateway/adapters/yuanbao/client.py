"""Yuanbao's HTTP side and the socket opener (P16-21).

Sign token: ``POST <api>/api/v5/robotLogic/sign-token`` with ``{app_key, nonce, signature,
timestamp}``. The signature is HMAC-SHA256 (the stdlib ``hmac`` module) keyed by the App Secret
over nonce + timestamp + App ID + App Secret, as hex; the timestamp is Beijing time (UTC+8) in ISO
form without fractions. The answer's ``data`` holds the token, the bot's account id, a source
tag and the lifetime in seconds. Code 10099 asks for another try (three more, a second apart);
any other non-zero code, or HTTP 401/403, is a refusal (`TokenError`, not retryable). The token
is cached until a minute before it runs out; one caller refreshes at a time.

Business REST (token headers ``X-ID`` / ``X-Token`` / ``X-Source``): ``POST
/api/resource/genUploadInfo`` (temporary COS credentials for one upload) and ``GET
/api/resource/v1/download?resourceId=`` (a direct URL for an inbound media resource). Uploads go
to Tencent COS with a version-1 ``sha1`` signature (`cos_authorization`, HMAC-SHA1 through
``hmac``) and the temporary security token. Inbound media is downloaded only from https hosts on
the media allowlist, without credentials, redirects or more than the cap.

Nothing secret (the App Secret, the signature, the token, the COS keys) is ever put in a URL, a log
line, an error or a repr.

The socket: ``httpx_ws.aconnect_ws`` (the ``yuanbao`` extra), imported only when a socket opens;
`SocketClosed` carries the server's close code.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import urllib.parse
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Final, Protocol, cast

import httpx

from indus.foundation.clock import Clock
from indus.foundation.digits import is_digits
from indus.gateway.http.serving import is_loopback

__all__ = [
    "DEFAULT_API",
    "DEFAULT_WS_URL",
    "INSTALL_HINT",
    "ROUTES",
    "ApiError",
    "MediaTooLarge",
    "SignToken",
    "SocketClosed",
    "SocketOpener",
    "TokenError",
    "YuanbaoApi",
    "YuanbaoSocket",
    "beijing_timestamp",
    "check_api_base",
    "check_ws_url",
    "cos_authorization",
    "host_allowed",
    "httpx_ws_opener",
    "sign",
]

log = logging.getLogger(__name__)

DEFAULT_WS_URL: Final = "wss://bot-wss.yuanbao.tencent.com/wss/connection"
DEFAULT_API: Final = "https://bot.yuanbao.tencent.com"
INSTALL_HINT: Final = "pip install 'indus-agent[yuanbao]'"
SIGN_PATH: Final = "/api/v5/robotLogic/sign-token"
UPLOAD_INFO_PATH: Final = "/api/resource/genUploadInfo"
RESOURCE_PATH: Final = "/api/resource/v1/download"
RETRY_CODE: Final = 10099
SIGN_RETRIES: Final = 3
SIGN_RETRY_WAIT_S: Final = 1.0
REFRESH_EARLY_S: Final = 60.0
DEFAULT_LIFETIME_S: Final = 3600.0
HTTP_TIMEOUT_S: Final = 15.0
UPLOAD_TIMEOUT_S: Final = 120.0
BEIJING: Final = timezone(timedelta(hours=8))

ROUTES: Mapping[str, str] = {
    "sign_token": f"POST {SIGN_PATH}",
    "upload_info": f"POST {UPLOAD_INFO_PATH}",
    "resource_url": f"GET {RESOURCE_PATH}",
    "cos_put": "PUT https://<bucket>.cos.accelerate.myqcloud.com/<key>",
}

_BUCKET = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SNIPPET = 200


def sign(app_id: str, secret: str, nonce: str, timestamp: str) -> str:
    """The sign-token signature (see the module notes)."""
    message = (nonce + timestamp + app_id + secret).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def beijing_timestamp(now: datetime) -> str:
    """``now`` in Beijing time, ``2026-01-01T17:00:00+08:00``."""
    return now.astimezone(BEIJING).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _https_or_local(url: str, secure: str, plain: str, what: str) -> str:
    text = url.strip().rstrip("/")
    parts = urllib.parse.urlsplit(text)
    host = parts.hostname or ""
    if parts.username or parts.password or not host:
        raise ValueError(f"{what} must be a plain {secure}:// URL without credentials")
    if parts.scheme == secure or (parts.scheme == plain and is_loopback(host)):
        return text
    raise ValueError(f"{what} must use {secure}:// ({plain}:// only to this machine)")


def check_ws_url(url: str) -> str:
    return _https_or_local(url, "wss", "ws", "the Yuanbao WebSocket URL")


def check_api_base(url: str) -> str:
    return _https_or_local(url, "https", "http", "the Yuanbao API domain")


def host_allowed(url: str, hosts: tuple[str, ...]) -> bool:
    """https, no credentials, and the host (or a parent domain of it) on ``hosts``."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "https" or not host or parts.username or parts.password:
        return False
    return any(host == h or host.endswith("." + h) for h in hosts)


class TokenError(Exception):
    """The sign-token call failed: ``retryable`` false means the credentials were refused."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class ApiError(Exception):
    """A Yuanbao REST call answered an error (HTTP status and/or a business code)."""

    def __init__(self, status: int, code: int | None = None, detail: str = "") -> None:
        self.status = status
        self.code = code
        self.detail = " ".join(detail.split())[:_SNIPPET]
        where = f"HTTP {status}" + (f", code {code}" if code is not None else "")
        super().__init__(f"Yuanbao API {where}: {self.detail}" if self.detail else where)


class MediaTooLarge(Exception):
    def __init__(self, size: int, cap: int) -> None:
        super().__init__(f"the attachment is too large ({size} bytes, the cap is {cap})")


@dataclass(frozen=True)
class SignToken:
    token: str = field(repr=False)
    bot_id: str
    source: str
    expires_at: float


def _number(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and is_digits(value.strip(), signed=True):
        return int(value.strip())
    return None


def _object(value: object) -> dict[str, Any]:
    return dict(cast(Mapping[str, Any], value)) if isinstance(value, Mapping) else {}


class YuanbaoApi:
    """Sign tokens and the business REST calls (see the module notes)."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        app_id: str,
        secret: str,
        base: str,
        clock: Clock,
        route_env: str = "",
        version: str = "",
        instance_id: str = "",
        os_name: str = "",
    ) -> None:
        self.http = http
        self.app_id = app_id
        self._secret = secret
        self.base = base.rstrip("/")
        self.clock = clock
        self.route_env = route_env
        self.version = version
        self.instance_id = instance_id
        self.os_name = os_name
        self._token: SignToken | None = None
        self._lock = asyncio.Lock()
        self.fetches = 0

    def __repr__(self) -> str:
        return f"<YuanbaoApi {self.base}>"

    @property
    def current(self) -> SignToken | None:
        return self._token

    def invalidate(self) -> None:
        self._token = None

    def _fresh(self) -> bool:
        token = self._token
        return token is not None and self.clock.monotonic() < token.expires_at - REFRESH_EARLY_S

    async def token(self, *, force: bool = False) -> SignToken:
        if force:
            self.invalidate()
        if self._fresh():
            assert self._token is not None
            return self._token
        async with self._lock:
            if not self._fresh():
                self._token = await self._sign()
            assert self._token is not None
            return self._token

    def _client_headers(self) -> dict[str, str]:
        headers = {
            "X-AppVersion": self.version,
            "X-OperationSystem": self.os_name,
            "X-Instance-Id": self.instance_id,
            "X-Bot-Version": self.version,
        }
        if self.route_env:
            headers["X-Route-Env"] = self.route_env
        return headers

    async def _sign(self) -> SignToken:
        url = f"{self.base}{SIGN_PATH}"
        for attempt in range(SIGN_RETRIES + 1):
            self.fetches += 1
            nonce = secrets.token_hex(16)
            stamp = beijing_timestamp(self.clock.now())
            body = {
                "app_key": self.app_id,
                "nonce": nonce,
                "signature": sign(self.app_id, self._secret, nonce, stamp),
                "timestamp": stamp,
            }
            try:
                answer = await self.http.post(
                    url, json=body, headers=self._client_headers(), timeout=HTTP_TIMEOUT_S
                )
            except httpx.HTTPError as exc:
                raise TokenError(
                    f"the Yuanbao sign-token endpoint could not be reached: {type(exc).__name__}",
                    retryable=True,
                ) from None
            if answer.status_code in (401, 403):
                raise TokenError(
                    f"Yuanbao refused the App ID / App Secret (HTTP {answer.status_code})",
                    retryable=False,
                )
            if answer.status_code != 200:
                raise TokenError(
                    f"the Yuanbao sign-token endpoint answered HTTP {answer.status_code}",
                    retryable=True,
                )
            try:
                fields = _object(answer.json())
            except (ValueError, UnicodeDecodeError):
                raise TokenError("the sign-token answer is not JSON", retryable=True) from None
            code = _number(fields.get("code"))
            if code == 0:
                data = _object(fields.get("data"))
                token = str(data.get("token") or "")
                if not token:
                    raise TokenError("the sign-token answer has no token", retryable=True)
                lifetime = _number(data.get("duration")) or 0
                seconds = float(lifetime) if lifetime > 0 else DEFAULT_LIFETIME_S
                return SignToken(
                    token=token,
                    bot_id=str(data.get("bot_id") or ""),
                    source=str(data.get("source") or ""),
                    expires_at=self.clock.monotonic() + max(seconds, REFRESH_EARLY_S + 1),
                )
            if code != RETRY_CODE or attempt >= SIGN_RETRIES:
                message = " ".join(str(fields.get("msg") or fields.get("message") or "").split())
                raise TokenError(
                    f"Yuanbao refused the sign-token request (code {code})"
                    + (f": {message[:_SNIPPET]}" if message else ""),
                    retryable=code == RETRY_CODE,
                )
            log.info("yuanbao: sign-token asked for another try (%d of %d)", attempt + 1, 3)
            await self.clock.sleep(SIGN_RETRY_WAIT_S)
        raise AssertionError("unreachable")  # pragma: no cover

    # -- business REST -----------------------------------------------------------------------------

    async def _headers(self, source: str = "") -> dict[str, str]:
        token = await self.token()
        return {
            "X-ID": token.bot_id or self.app_id,
            "X-Token": token.token,
            "X-Source": source or token.source or "web",
            **({"X-Route-Env": self.route_env} if self.route_env else {}),
        }

    async def _call(self, method: str, path: str, *, source: str = "", **kw: Any) -> dict[str, Any]:
        for attempt in (0, 1):
            response = await self.http.request(
                method, f"{self.base}{path}", headers=await self._headers(source), **kw
            )
            if response.status_code == 401 and attempt == 0:
                self.invalidate()  # the token expired early: sign again once
                continue
            fields: dict[str, Any] = {}
            try:
                fields = _object(response.json())
            except (ValueError, UnicodeDecodeError):
                fields = {}
            code = _number(fields.get("code"))
            if response.status_code >= 400 or code not in (None, 0):
                detail = str(fields.get("msg") or fields.get("message") or "")
                raise ApiError(response.status_code, code, detail)
            data = fields.get("data")
            return _object(data) if isinstance(data, Mapping) else fields
        raise AssertionError("unreachable")  # pragma: no cover

    async def upload_info(self, file_name: str) -> dict[str, Any]:
        """Temporary COS credentials for one upload (``bucketName``, ``region``, ``location``,
        ``encryptTmpSecretId``, ``encryptTmpSecretKey``, ``encryptToken``, ``startTime``,
        ``expiredTime``, ``resourceUrl``)."""
        body = {
            "fileName": file_name,
            "fileId": secrets.token_hex(16),
            "docFrom": "localDoc",
            "docOpenId": "",
        }
        # Upload slots are asked for as the "web" source whatever the token's source is.
        data = await self._call(
            "POST", UPLOAD_INFO_PATH, source="web", json=body, timeout=HTTP_TIMEOUT_S
        )
        missing = [k for k in ("bucketName", "location") if not data.get(k)]
        if missing:
            raise ApiError(200, None, "the upload answer lacks " + ", ".join(missing))
        return data

    async def resource_url(self, resource_id: str) -> str:
        """The direct URL of an inbound media resource."""
        data = await self._call(
            "GET", RESOURCE_PATH, params={"resourceId": resource_id}, timeout=HTTP_TIMEOUT_S
        )
        url = str(data.get("url") or data.get("realUrl") or "").strip()
        if not url:
            raise ApiError(200, None, "the resource answer has no URL")
        return url

    async def put_object(self, url: str, data: bytes, headers: Mapping[str, str]) -> None:
        response = await self.http.put(
            url, content=data, headers=dict(headers), timeout=UPLOAD_TIMEOUT_S
        )
        if response.status_code >= 300:
            raise ApiError(response.status_code, None, "the COS upload was refused")

    async def download(self, url: str, *, cap: int) -> AsyncIterator[bytes]:
        """Stream ``url`` (already checked against the media allowlist) without credentials;
        `MediaTooLarge` past ``cap``."""
        async with self.http.stream("GET", url, timeout=UPLOAD_TIMEOUT_S) as response:
            if response.status_code >= 400:
                raise ApiError(response.status_code, None, "the media download was refused")
            declared = response.headers.get("Content-Length", "")
            if is_digits(declared) and int(declared) > cap:
                raise MediaTooLarge(int(declared), cap)
            seen = 0
            async for chunk in response.aiter_bytes():
                seen += len(chunk)
                if seen > cap:
                    raise MediaTooLarge(seen, cap)
                yield chunk


# -- COS -------------------------------------------------------------------------------------------


def cos_url(bucket: str, key: str) -> tuple[str, str, str]:
    """(URL, host, signed path) of ``key`` in ``bucket`` on the COS accelerate endpoint."""
    if not _BUCKET.match(bucket):
        raise ValueError("the upload answer names an invalid bucket")
    path = "/" + urllib.parse.quote(key.lstrip("/"), safe="/")
    host = f"{bucket}.cos.accelerate.myqcloud.com"
    return f"https://{host}{path}", host, path


def _hmac_sha1(key: str, message: str) -> str:
    return hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha1).hexdigest()


def _pairs(values: Mapping[str, str]) -> list[tuple[str, str]]:
    return sorted((k.lower(), urllib.parse.quote(str(v), safe="-_.~")) for k, v in values.items())


def cos_authorization(
    *,
    method: str,
    path: str,
    headers: Mapping[str, str],
    params: Mapping[str, str] | None = None,
    secret_id: str,
    secret_key: str,
    start: int,
    end: int,
) -> str:
    """A COS request signature, ``q-sign-algorithm=sha1`` (Tencent COS signature docs).

    KeyTime = ``start;end``; SignKey = HMAC-SHA1(secret key, KeyTime); the request string is the
    lower-case method, the path, then the sorted, encoded parameters and headers (``k=v`` joined
    by ``&``) on their own lines; the string to sign is ``sha1``, KeyTime and the SHA-1 of the
    request string, each followed by a newline; the signature is HMAC-SHA1(SignKey, that)."""
    key_time = f"{start};{end}"
    query = _pairs(params or {})
    header_pairs = _pairs(headers)

    def joined(pairs: list[tuple[str, str]]) -> str:
        return "&".join(f"{k}={v}" for k, v in pairs)

    request = f"{method.lower()}\n{path}\n{joined(query)}\n{joined(header_pairs)}\n"
    digest = hashlib.sha1(request.encode("utf-8")).hexdigest()
    to_sign = f"sha1\n{key_time}\n{digest}\n"
    signature = _hmac_sha1(_hmac_sha1(secret_key, key_time), to_sign)
    return (
        f"q-sign-algorithm=sha1&q-ak={secret_id}&q-sign-time={key_time}&q-key-time={key_time}"
        f"&q-header-list={';'.join(k for k, _ in header_pairs)}"
        f"&q-url-param-list={';'.join(k for k, _ in query)}&q-signature={signature}"
    )


def cos_window(credentials: Mapping[str, Any], now: float | None = None) -> tuple[int, int]:
    """The signature's validity window from the upload answer (one hour when it gives none)."""
    current = int(now if now is not None else time.time())
    start = _number(credentials.get("startTime")) or current
    end = _number(credentials.get("expiredTime")) or 0
    return start, end if end > start else start + 3600


# -- the socket ------------------------------------------------------------------------------------


class SocketClosed(Exception):
    """The bot socket closed; ``code`` is the server's close code (None when unknown)."""

    def __init__(self, code: int | None = None, reason: str = "") -> None:
        self.code = code
        self.reason = reason
        super().__init__(f"the Yuanbao socket closed (code {code}) {reason}".strip())


class YuanbaoSocket(Protocol):
    async def receive_bytes(self) -> bytes: ...

    async def send_bytes(self, data: bytes) -> None: ...


type SocketOpener = Callable[
    [httpx.AsyncClient, str], contextlib.AbstractAsyncContextManager[YuanbaoSocket]
]


class _WsSession:
    """httpx-ws's session behind `YuanbaoSocket`; a close becomes `SocketClosed`."""

    def __init__(self, session: Any, disconnect: type[BaseException]) -> None:
        self._session = session
        self._disconnect = disconnect

    async def receive_bytes(self) -> bytes:
        try:
            return bytes(await self._session.receive_bytes())
        except self._disconnect as exc:
            raise SocketClosed(
                getattr(exc, "code", None), str(getattr(exc, "reason", "") or "")
            ) from None

    async def send_bytes(self, data: bytes) -> None:
        try:
            await self._session.send_bytes(data)
        except self._disconnect as exc:
            raise SocketClosed(getattr(exc, "code", None)) from None


@contextlib.asynccontextmanager
async def httpx_ws_opener(client: httpx.AsyncClient, url: str) -> AsyncIterator[YuanbaoSocket]:
    """The production opener: ``httpx_ws.aconnect_ws`` over the adapter's HTTP client."""
    try:
        import httpx_ws  # pyright: ignore[reportMissingImports]
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            f"the Yuanbao socket needs httpx-ws: {INSTALL_HINT}", name=exc.name
        ) from None
    target = re.sub(r"^ws", "http", url, count=1)  # wss → https, ws → http
    async with httpx_ws.aconnect_ws(  # pyright: ignore[reportUnknownMemberType]
        target, client, max_message_size_bytes=4 * 1024 * 1024
    ) as session:
        yield _WsSession(session, httpx_ws.WebSocketDisconnect)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]


def json_data(text: str) -> dict[str, Any] | None:
    """``text`` as a JSON object, or None."""
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return None
    return _object(value) if isinstance(value, Mapping) else None


def utc_now_ms(clock: Clock) -> int:
    return int(clock.now().astimezone(UTC).timestamp() * 1000)
