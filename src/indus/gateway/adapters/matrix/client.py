"""A small Matrix Client-Server API client over httpx (P16-06).

Only the requests the adapter makes (``ROUTES``; spec v1.11). The access token travels in the
``Authorization`` header, never in a URL, so request lines and error texts can be logged.

Every non-2xx answer becomes a `MatrixError` whose text starts with a reading the send classifier
(P15-04) recognises: ``rate limited`` (with the server's wait), ``forbidden``, ``chat not found`` /
``message not found``, ``message is too long``, ``can't parse``, a connection-level reading for a
502/503/504, and ``unauthorized`` for a refused token (no classifier rule: it is neither a dead
chat nor worth a retry; the adapter treats it as final).
"""

from __future__ import annotations

import itertools
import json
import secrets
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from indus.foundation.digits import is_digits

__all__ = [
    "AUTH_ERRCODES",
    "DEFAULT_TIMEOUT_S",
    "ROUTES",
    "SYNC_GRACE_S",
    "MatrixClient",
    "MatrixError",
    "MediaTooLarge",
    "parse_mxc",
]

DEFAULT_TIMEOUT_S = 30.0
# The long poll's own wait plus this much before httpx gives up on a silent connection.
SYNC_GRACE_S = 15.0
AUTH_ERRCODES = frozenset({"M_UNKNOWN_TOKEN", "M_MISSING_TOKEN"})

_C = "/_matrix/client/v3"

# Method name → the vendor route it calls (the wire fake must serve every one).
ROUTES: Mapping[str, str] = {
    "whoami": "GET /_matrix/client/v3/account/whoami",
    "sync": "GET /_matrix/client/v3/sync",
    "send_event": "PUT /_matrix/client/v3/rooms/{roomId}/send/{eventType}/{txnId}",
    "redact": "PUT /_matrix/client/v3/rooms/{roomId}/redact/{eventId}/{txnId}",
    "set_typing": "PUT /_matrix/client/v3/rooms/{roomId}/typing/{userId}",
    "account_data": "GET /_matrix/client/v3/user/{userId}/account_data/{type}",
    "set_account_data": "PUT /_matrix/client/v3/user/{userId}/account_data/{type}",
    "resolve_alias": "GET /_matrix/client/v3/directory/room/{roomAlias}",
    "join": "POST /_matrix/client/v3/join/{roomIdOrAlias}",
    "room_state": "GET /_matrix/client/v3/rooms/{roomId}/state/{eventType}/{stateKey}",
    "joined_members": "GET /_matrix/client/v3/rooms/{roomId}/joined_members",
    "upload": "POST /_matrix/media/v3/upload",
    "download": "GET /_matrix/client/v1/media/download/{serverName}/{mediaId}",
    "download_legacy": "GET /_matrix/media/v3/download/{serverName}/{mediaId}",
}


class MatrixError(Exception):
    """A non-2xx answer (or an unreadable one) from the homeserver."""

    def __init__(
        self,
        reading: str,
        *,
        status: int = 0,
        errcode: str = "",
        detail: str = "",
        retry_after: float | None = None,
    ) -> None:
        self.reading = reading
        self.status = status
        self.errcode = errcode
        self.detail = detail
        self.retry_after = retry_after
        parts = [f"HTTP {status}" if status else "", errcode]
        where = ", ".join(p for p in parts if p)
        text = f"{reading} ({where})" if where else reading
        super().__init__(f"{text}: {detail}" if detail else text)

    @property
    def is_auth(self) -> bool:
        """The token was refused: retrying or reconnecting cannot help."""
        return self.errcode in AUTH_ERRCODES or self.status == 401


class MediaTooLarge(Exception):
    def __init__(self, size: int, cap: int) -> None:
        super().__init__(f"the file is too large ({size} bytes, the cap is {cap})")
        self.size = size
        self.cap = cap


def _reading(status: int, errcode: str, detail: str, *, on_event: bool) -> str:
    low = detail.lower()
    if status == 429 or errcode == "M_LIMIT_EXCEEDED":
        return "rate limited"
    if errcode in AUTH_ERRCODES or status == 401:
        return "unauthorized: the access token was refused"
    if errcode == "M_FORBIDDEN" or status == 403:
        return "forbidden"
    if errcode == "M_TOO_LARGE" or status == 413:
        return "message is too long"
    if errcode in ("M_BAD_JSON", "M_NOT_JSON"):
        return "can't parse the request"
    if errcode == "M_NOT_FOUND" or status == 404:
        # An event inside a room (an edit or reply target, a thread root) is a sub-chat reading.
        if on_event or "event" in low or "thread" in low:
            return "message not found"
        return "chat not found"
    if status in (502, 503, 504):
        return "homeserver unavailable, connect error"
    if errcode == "M_UNRECOGNIZED":
        return "endpoint not supported by the homeserver"
    return "homeserver error"


def _retry_after(response: httpx.Response, body: Mapping[str, Any]) -> float | None:
    raw = body.get("retry_after_ms")
    if isinstance(raw, int | float) and raw >= 0:
        return float(raw) / 1000.0
    header = response.headers.get("Retry-After", "").strip()
    try:
        return float(header) if header else None
    except ValueError:
        return None


def error_of(response: httpx.Response, *, on_event: bool = False) -> MatrixError:
    """The `MatrixError` for a failed ``response`` (the body read already)."""
    body: dict[str, Any] = {}
    try:
        loaded = response.json()
        if isinstance(loaded, dict):
            body = dict(loaded)  # pyright: ignore[reportUnknownArgumentType]
    except (ValueError, UnicodeDecodeError):
        body = {}
    errcode = str(body.get("errcode") or "")
    detail = str(body.get("error") or "")[:300]
    status = response.status_code
    reading = _reading(status, errcode, detail, on_event=on_event)
    retry = _retry_after(response, body) if reading == "rate limited" else None
    return MatrixError(reading, status=status, errcode=errcode, detail=detail, retry_after=retry)


def parse_mxc(uri: str) -> tuple[str, str] | None:
    """``mxc://server/media`` → (server, media id); None for anything else."""
    if not uri.startswith("mxc://"):
        return None
    server, _, media = uri[len("mxc://") :].partition("/")
    if not server or not media or "/" in media or ".." in server:
        return None
    return server, media


def _q(value: str) -> str:
    return quote(value, safe="")


@dataclass(frozen=True)
class WhoAmI:
    user_id: str
    device_id: str


class MatrixClient:
    """One homeserver session (see the module notes). Close it with ``aclose``."""

    def __init__(
        self,
        homeserver: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        proxy: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.homeserver = homeserver.rstrip("/")
        self._token = token
        self._http = httpx.AsyncClient(
            base_url=self.homeserver,
            headers={"Authorization": f"Bearer {token}", "User-Agent": "IndusAgent"},
            timeout=httpx.Timeout(timeout),
            transport=transport,
            proxy=None if transport is not None else proxy,
            trust_env=False,
            follow_redirects=False,
        )
        self._txn = itertools.count(1)
        self._txn_prefix = secrets.token_hex(6)

    def __repr__(self) -> str:
        return f"<MatrixClient {self.homeserver}>"

    def next_txn(self) -> str:
        """A transaction id unique to this client (the server dedupes repeats of one id)."""
        return f"indus.{self._txn_prefix}.{next(self._txn)}"

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- plumbing ----------------------------------------------------------------------------------

    async def _call(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
        timeout: float | None = None,
        on_event: bool = False,
    ) -> dict[str, Any]:
        response = await self._http.request(
            method,
            path,
            json=dict(body) if body is not None else None,
            params=dict(params) if params else None,
            timeout=httpx.Timeout(timeout) if timeout is not None else httpx.USE_CLIENT_DEFAULT,
        )
        if response.status_code >= 400:
            raise error_of(response, on_event=on_event)
        try:
            data = response.json()
        except (ValueError, UnicodeDecodeError):
            raise MatrixError(
                "unexpected answer from the homeserver", status=response.status_code
            ) from None
        if not isinstance(data, dict):
            raise MatrixError("unexpected answer from the homeserver", status=response.status_code)
        return dict(data)  # pyright: ignore[reportUnknownArgumentType]

    async def request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Any JSON call on this homeserver session (the end-to-end encryption layer, P16-07, sends
        its key and to-device requests through here, so they share this client's token handling).
        ``path`` is the full API path; it must start with ``/_matrix/``."""
        if not path.startswith("/_matrix/") or ".." in path:
            raise MatrixError("not a Matrix API path", detail=path[:80])
        return await self._call(method.upper(), path, body=body, params=params, timeout=timeout)

    # -- account -----------------------------------------------------------------------------------

    async def whoami(self) -> WhoAmI:
        data = await self._call("GET", f"{_C}/account/whoami")
        return WhoAmI(str(data.get("user_id") or ""), str(data.get("device_id") or ""))

    async def sync(
        self, *, since: str | None, timeout_ms: int, filter_json: str | None = None
    ) -> dict[str, Any]:
        params = {"timeout": str(max(0, timeout_ms))}
        if since:
            params["since"] = since
        if filter_json:
            params["filter"] = filter_json
        return await self._call(
            "GET", f"{_C}/sync", params=params, timeout=timeout_ms / 1000.0 + SYNC_GRACE_S
        )

    async def account_data(self, user_id: str, kind: str) -> dict[str, Any] | None:
        """The user's account data of ``kind``; None when the server has none."""
        try:
            return await self._call("GET", f"{_C}/user/{_q(user_id)}/account_data/{_q(kind)}")
        except MatrixError as exc:
            if exc.status == 404 or exc.errcode == "M_NOT_FOUND":
                return None
            raise

    async def set_account_data(self, user_id: str, kind: str, content: Mapping[str, Any]) -> None:
        await self._call("PUT", f"{_C}/user/{_q(user_id)}/account_data/{_q(kind)}", body=content)

    # -- rooms -------------------------------------------------------------------------------------

    async def send_event(
        self, room_id: str, event_type: str, content: Mapping[str, Any], *, txn_id: str = ""
    ) -> str:
        txn = txn_id or self.next_txn()
        relates = content.get("m.relates_to")
        on_event = isinstance(relates, Mapping) and bool(relates)
        data = await self._call(
            "PUT",
            f"{_C}/rooms/{_q(room_id)}/send/{_q(event_type)}/{_q(txn)}",
            body=content,
            on_event=on_event,
        )
        event_id = str(data.get("event_id") or "")
        if not event_id:
            raise MatrixError("unexpected answer from the homeserver: no event id")
        return event_id

    async def redact(self, room_id: str, event_id: str, *, reason: str = "") -> str:
        body = {"reason": reason} if reason else {}
        data = await self._call(
            "PUT",
            f"{_C}/rooms/{_q(room_id)}/redact/{_q(event_id)}/{_q(self.next_txn())}",
            body=body,
            on_event=True,
        )
        return str(data.get("event_id") or "")

    async def set_typing(
        self, room_id: str, user_id: str, *, typing: bool, timeout_ms: int = 30_000
    ) -> None:
        body: dict[str, Any] = {"typing": typing}
        if typing:
            body["timeout"] = timeout_ms
        await self._call("PUT", f"{_C}/rooms/{_q(room_id)}/typing/{_q(user_id)}", body=body)

    async def resolve_alias(self, alias: str) -> str:
        data = await self._call("GET", f"{_C}/directory/room/{_q(alias)}")
        room_id = str(data.get("room_id") or "")
        if not room_id:
            raise MatrixError("chat not found", detail=f"alias {alias} has no room")
        return room_id

    async def join(self, room: str) -> str:
        data = await self._call("POST", f"{_C}/join/{_q(room)}", body={})
        return str(data.get("room_id") or room)

    async def room_state(
        self, room_id: str, event_type: str, state_key: str = ""
    ) -> dict[str, Any]:
        return await self._call(
            "GET", f"{_C}/rooms/{_q(room_id)}/state/{_q(event_type)}/{_q(state_key)}"
        )

    async def joined_members(self, room_id: str) -> list[str]:
        """The user ids joined to ``room_id``."""
        data = await self._call("GET", f"{_C}/rooms/{_q(room_id)}/joined_members")
        joined = data.get("joined")
        if not isinstance(joined, Mapping):
            return []
        return [str(user) for user in joined]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]

    # -- media -------------------------------------------------------------------------------------

    async def upload(self, data: bytes, *, mime: str, filename: str) -> str:
        response = await self._http.post(
            "/_matrix/media/v3/upload",
            params={"filename": filename},
            content=data,
            headers={"Content-Type": mime or "application/octet-stream"},
            timeout=httpx.Timeout(120.0),
        )
        if response.status_code >= 400:
            raise error_of(response)
        try:
            uri = str(response.json().get("content_uri") or "")
        except (ValueError, AttributeError):
            uri = ""
        if parse_mxc(uri) is None:
            raise MatrixError("unexpected answer from the homeserver: no content uri")
        return uri

    async def download(self, uri: str, *, cap: int) -> AsyncIterator[bytes]:
        """Stream ``mxc://`` media, authenticated first, the legacy route as a fallback; raise
        `MediaTooLarge` once more than ``cap`` bytes are declared or read."""
        parsed = parse_mxc(uri)
        if parsed is None:
            raise MatrixError("not a matrix content uri", detail=uri[:80])
        server, media = parsed
        paths = (
            f"/_matrix/client/v1/media/download/{_q(server)}/{_q(media)}",
            f"/_matrix/media/v3/download/{_q(server)}/{_q(media)}",
        )
        for n, path in enumerate(paths):
            async with self._http.stream("GET", path, timeout=httpx.Timeout(120.0)) as response:
                if response.status_code >= 400:
                    await response.aread()
                    error = error_of(response)
                    retry_legacy = response.status_code in (400, 404, 405) and n == 0
                    if retry_legacy and error.errcode in ("M_UNRECOGNIZED", "M_NOT_FOUND", ""):
                        continue
                    raise error
                declared = response.headers.get("Content-Length", "")
                if is_digits(declared) and int(declared) > cap:
                    raise MediaTooLarge(int(declared), cap)
                seen = 0
                async for chunk in response.aiter_bytes():
                    seen += len(chunk)
                    if seen > cap:
                        raise MediaTooLarge(seen, cap)
                    yield chunk
                return


def compact_json(value: Any) -> str:
    """Canonical-ish JSON (UTF-8, no spaces): how the server counts an event's size."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
