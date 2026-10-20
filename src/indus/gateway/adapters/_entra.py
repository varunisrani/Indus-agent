"""App-only access tokens from the Microsoft identity platform (P16-11 Teams, P16-12 Graph).

Both Microsoft adapters authenticate as an app registration with the OAuth 2.0 client-credentials
grant: ``POST <authority>/<tenant>/oauth2/v2.0/token`` with the app id, its secret and one
``<resource>/.default`` scope. ``ClientCredentials`` asks for a token over the adapter's own httpx
client, keeps it until five minutes before it expires (one request at a time, so a burst of sends
cannot stampede the endpoint) and forgets it on ``invalidate()`` (a 401 from the API).

Secrets stay out of every text this module produces: the client secret is sent only in the form
body; errors carry the HTTP status and the OAuth error code (a fixed vocabulary), never the
response body, the secret or a token; ``repr`` names the app and tenant only.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

from indus.foundation.clock import Clock

__all__ = [
    "AUTHORITY",
    "BOT_FRAMEWORK_SCOPE",
    "GRAPH_SCOPE",
    "REFRESH_MARGIN_S",
    "ClientCredentials",
    "TokenError",
    "valid_tenant",
]

AUTHORITY = "https://login.microsoftonline.com"
BOT_FRAMEWORK_SCOPE = "https://api.botframework.com/.default"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
# A cached token is replaced this long before the expiry the server gave.
REFRESH_MARGIN_S = 300.0
TOKEN_TIMEOUT_S = 15.0

# A directory (tenant) id is a GUID or a domain name; nothing that could change the URL path.
_TENANT = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_ERROR_CODE = re.compile(r"^[a-z_]{1,48}$")
# OAuth error codes that mean the app registration or its secret is wrong: retrying cannot help.
_CREDENTIAL_ERRORS = frozenset(
    {"invalid_client", "unauthorized_client", "invalid_request", "invalid_scope", "invalid_grant"}
)


def valid_tenant(tenant_id: str) -> bool:
    return bool(_TENANT.match(tenant_id)) and ".." not in tenant_id


class TokenError(Exception):
    """No token. ``retryable`` false means the credentials themselves were refused."""

    def __init__(self, message: str, *, retryable: bool, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class ClientCredentials:
    """One app's tokens for one scope (see the module notes)."""

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        scope: str,
        clock: Clock,
        authority: str = AUTHORITY,
    ) -> None:
        self.tenant_id = tenant_id.strip()
        self.client_id = client_id.strip()
        self._secret = client_secret
        self.scope = scope
        self.clock = clock
        self.authority = authority.rstrip("/")
        self._token: str | None = None
        self._good_until = 0.0
        self._lock: asyncio.Lock | None = None

    def __repr__(self) -> str:
        return f"<ClientCredentials app={self.client_id or '?'} tenant={self.tenant_id or '?'}>"

    @property
    def configured(self) -> bool:
        return bool(self.tenant_id and self.client_id and self._secret)

    @property
    def token_url(self) -> str:
        return f"{self.authority}/{self.tenant_id}/oauth2/v2.0/token"

    def held(self) -> str | None:
        """The cached token, for masking it in log text (never for sending)."""
        return self._token

    def invalidate(self) -> None:
        self._token = None
        self._good_until = 0.0

    async def token(self, client: httpx.AsyncClient, *, fresh: bool = False) -> str:
        """A valid access token (cached unless ``fresh``); `TokenError` otherwise."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if not fresh and self._token is not None and self.clock.monotonic() < self._good_until:
                return self._token
            token, lifetime = await self._request(client)
            margin = REFRESH_MARGIN_S if lifetime > 2 * REFRESH_MARGIN_S else lifetime / 2
            self._token = token
            self._good_until = self.clock.monotonic() + lifetime - margin
            return token

    async def _request(self, client: httpx.AsyncClient) -> tuple[str, float]:
        if not self.configured:
            raise TokenError("the app id, secret and tenant id are all required", retryable=False)
        if not valid_tenant(self.tenant_id):
            raise TokenError("the tenant id is not a directory id or domain", retryable=False)
        form = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self._secret,
            "scope": self.scope,
        }
        try:
            answer = await client.post(
                self.token_url, data=form, timeout=TOKEN_TIMEOUT_S, follow_redirects=False
            )
        except httpx.HTTPError as exc:
            raise TokenError(
                f"the token request failed: {type(exc).__name__}", retryable=True
            ) from None
        status = answer.status_code
        if status >= 400:
            code = _error_code(answer)
            shown = f" ({code})" if code else ""
            refused = status in (400, 401) and (code in _CREDENTIAL_ERRORS or status == 401)
            raise TokenError(
                f"the identity platform answered HTTP {status}{shown}",
                retryable=not refused,
                status=status,
            )
        try:
            data: Any = answer.json()
        except ValueError:
            raise TokenError(
                "the token answer is not JSON", retryable=True, status=status
            ) from None
        token = data.get("access_token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise TokenError("the token answer has no access token", retryable=True, status=status)
        return token, _lifetime(data.get("expires_in"))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]


def _error_code(answer: httpx.Response) -> str:
    try:
        data: Any = answer.json()
    except ValueError:
        return ""
    code = data.get("error") if isinstance(data, dict) else None
    return code if isinstance(code, str) and _ERROR_CODE.match(code) else ""


def _lifetime(raw: object) -> float:
    """``expires_in`` seconds (a number or a numeric string), kept within one minute .. one day."""
    if isinstance(raw, bool):
        return 3600.0
    try:
        seconds = float(raw)  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError):
        return 3600.0
    if seconds != seconds:
        return 3600.0
    return min(max(seconds, 60.0), 86_400.0)
