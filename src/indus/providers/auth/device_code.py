"""OAuth 2.0 Device Authorization Grant (RFC 8628) and the refresh grant (RFC 6749 §6).

Generic over any provider whose profile publishes a device endpoint (``DeviceFlowSpec``); no
bundled provider does today (D-16.42), so the flow is ready for the first one that documents it.

Polling, per RFC 8628 §3.4–3.5:

- ``authorization_pending`` → wait ``interval`` (5 s when the server named none) and ask again;
- ``slow_down`` → the interval grows by 5 s for this and every later poll (a provider may document
  another step through ``DeviceFlowSpec.slow_down_step``), never beyond 30 s;
- ``access_denied`` / ``expired_token`` → ``AccessDenied`` / ``DeviceCodeExpired``;
- an answer that is not OAuth JSON with status 408, 429, 5xx or 403 (an edge or firewall block;
  a real OAuth error is always JSON), or a transport failure, means the endpoint is struggling:
  wait ``Retry-After`` when given (at least the interval, at most 60 s), else back off from
  ``max(5, 2 × interval)`` doubling up to 60 s; never past the deadline (``expires_in``).

When the deadline passes while the endpoint was failing, ``SignInServiceDown`` says so instead of
claiming the person did not approve (R04.17).

Tokens are a ``TokenSet`` serialised to JSON and kept in the SecretStore, never in ``auth.json``.
They are refreshed ``REFRESH_SKEW_SECONDS`` before they expire; a dead grant (``invalid_grant``,
``invalid_token``, a reused refresh token, or no refresh token at all) raises ``ReloginRequired``.
"""

from __future__ import annotations

import json
import logging
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx

from indus.foundation.clock import Clock
from indus.foundation.digits import is_digits
from indus.foundation.errors import AuthError
from indus.providers.profile import USER_AGENT
from indus.providers.retry import parse_retry_after

log = logging.getLogger(__name__)

DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
REFRESH_GRANT_TYPE = "refresh_token"
DEFAULT_INTERVAL = 5
SLOW_DOWN_STEP = 5
INTERVAL_CAP = 30
BACKOFF_FLOOR = 5.0
BACKOFF_CAP = 60.0
REFRESH_SKEW_SECONDS = 120
HTTP_TIMEOUT_SECONDS = 30.0
DEAD_GRANT_ERRORS = frozenset({"invalid_grant", "invalid_token", "refresh_token_reused"})
_STRUGGLING = frozenset({408, 429})

type Say = Callable[[str], None]
type OpenUrl = Callable[[str], bool]


# -- errors ------------------------------------------------------------------------------------


class DeviceFlowError(AuthError):
    """The authorization server answered in a way the flow cannot continue from."""

    default_code = "auth.flow_failed"


class AccessDenied(DeviceFlowError):
    default_code = "auth.denied"


class DeviceCodeExpired(DeviceFlowError):
    default_code = "auth.expired"


class SignInServiceDown(DeviceFlowError):
    """The deadline passed while the token endpoint was failing (an outage, not a refusal)."""

    default_code = "auth.service_down"

    def __init__(self, message: str, *, last_status: int | None = None) -> None:
        super().__init__(message)
        self.last_status: int | None = last_status


class ReloginRequired(DeviceFlowError):
    default_code = "auth.relogin_required"


# -- records -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviceFlowSpec:
    """A provider's documented device endpoints. ``doc_url`` is where they were checked."""

    provider: str
    device_authorization_url: str
    token_url: str
    client_id: str
    scope: str = ""
    slow_down_step: int = SLOW_DOWN_STEP
    doc_url: str = ""


@dataclass(frozen=True)
class DeviceAuthorization:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int = DEFAULT_INTERVAL
    verification_uri_complete: str | None = None

    @property
    def url_to_show(self) -> str:
        return self.verification_uri_complete or self.verification_uri


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    obtained_at: datetime
    refresh_token: str | None = None
    token_type: str = "Bearer"
    scope: str | None = None
    expires_at: datetime | None = None

    def __repr__(self) -> str:  # never print token values
        return f"TokenSet(token_type={self.token_type!r}, expires_at={self.expires_at!r})"

    def to_secret(self) -> str:
        """The JSON kept in the SecretStore."""
        data: dict[str, Any] = {
            "access_token": self.access_token,
            "token_type": self.token_type,
            "obtained_at": self.obtained_at.isoformat(),
        }
        if self.refresh_token:
            data["refresh_token"] = self.refresh_token
        if self.scope:
            data["scope"] = self.scope
        if self.expires_at is not None:
            data["expires_at"] = self.expires_at.isoformat()
        return json.dumps(data, sort_keys=True)

    @classmethod
    def from_secret(cls, text: str) -> TokenSet:
        data = json.loads(text)
        if not isinstance(data, dict) or not cast(dict[str, Any], data).get("access_token"):
            raise ValueError("stored sign-in tokens are not in the expected shape")
        raw = cast(dict[str, Any], data)
        expires = raw.get("expires_at")
        return cls(
            access_token=str(raw["access_token"]),
            obtained_at=datetime.fromisoformat(str(raw["obtained_at"])),
            refresh_token=str(raw["refresh_token"]) if raw.get("refresh_token") else None,
            token_type=str(raw.get("token_type") or "Bearer"),
            scope=str(raw["scope"]) if raw.get("scope") else None,
            expires_at=datetime.fromisoformat(str(expires)) if expires else None,
        )

    @classmethod
    def from_response(
        cls, payload: Mapping[str, Any], now: datetime, *, keep_refresh: str | None = None
    ) -> TokenSet:
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise DeviceFlowError("the token answer carried no access_token")
        expires_at: datetime | None = None
        lifetime = payload.get("expires_in")
        if isinstance(lifetime, int | float) and not isinstance(lifetime, bool) and lifetime > 0:
            expires_at = now + timedelta(seconds=float(lifetime))
        refresh = payload.get("refresh_token")
        return cls(
            access_token=token,
            obtained_at=now,
            refresh_token=refresh if isinstance(refresh, str) and refresh else keep_refresh,
            token_type=str(payload.get("token_type") or "Bearer"),
            scope=str(payload["scope"]) if payload.get("scope") else None,
            expires_at=expires_at,
        )


def needs_refresh(tokens: TokenSet, now: datetime, skew: float = REFRESH_SKEW_SECONDS) -> bool:
    """True from ``skew`` seconds before expiry on (never for tokens without an expiry)."""
    if tokens.expires_at is None:
        return False
    return now >= tokens.expires_at - timedelta(seconds=skew)


# -- TLS ---------------------------------------------------------------------------------------


def tls_verify(
    ca_bundle: str | None = None, insecure: bool = False, *, warn: Say | None = None
) -> bool | ssl.SSLContext:
    """httpx ``verify``: off with ``insecure`` (loud warning), a PEM bundle, or the default."""
    if insecure:
        message = (
            "WARNING: TLS certificate checks are OFF for this sign-in (--insecure). Anyone on the "
            "network path can read or change it. Use --ca-bundle with your proxy's certificate "
            "instead whenever you can."
        )
        (warn or log.warning)(message)
        return False
    if ca_bundle:
        path = Path(ca_bundle).expanduser()
        if not path.is_file():
            raise DeviceFlowError(f"the CA bundle {path} does not exist")
        return ssl.create_default_context(cafile=str(path))
    return True


def make_client(
    verify: bool | ssl.SSLContext = True, timeout: float = HTTP_TIMEOUT_SECONDS
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        verify=verify, timeout=timeout, headers={"User-Agent": USER_AGENT}, follow_redirects=False
    )


# -- protocol steps ----------------------------------------------------------------------------


def _json_object(response: httpx.Response) -> dict[str, Any] | None:
    try:
        data = response.json()
    except (ValueError, UnicodeDecodeError):
        return None
    return cast(dict[str, Any], data) if isinstance(data, dict) else None


def _positive_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int | float) and value > 0:
        return int(value)
    if isinstance(value, str) and is_digits(value.strip()) and int(value) > 0:
        return int(value)
    return default


async def request_device_authorization(
    client: httpx.AsyncClient, spec: DeviceFlowSpec
) -> DeviceAuthorization:
    """POST the device authorization request (RFC 8628 §3.1–3.2)."""
    form = {"client_id": spec.client_id}
    if spec.scope:
        form["scope"] = spec.scope
    try:
        response = await client.post(spec.device_authorization_url, data=form)
    except httpx.HTTPError as exc:
        raise DeviceFlowError(f"could not reach {spec.provider}'s sign-in service: {exc}") from exc
    payload = _json_object(response)
    if response.status_code != 200 or payload is None:
        what = f" ({payload.get('error')})" if payload and payload.get("error") else ""
        raise DeviceFlowError(
            f"{spec.provider}'s sign-in service refused the request: HTTP {response.status_code}"
            + what
        )
    missing = [
        f
        for f in ("device_code", "user_code", "verification_uri", "expires_in")
        if not payload.get(f)
    ]
    if missing:
        raise DeviceFlowError(
            f"{spec.provider}'s device authorization answer lacks {', '.join(missing)}"
        )
    complete = payload.get("verification_uri_complete")
    return DeviceAuthorization(
        device_code=str(payload["device_code"]),
        user_code=str(payload["user_code"]),
        verification_uri=str(payload["verification_uri"]),
        expires_in=_positive_int(payload.get("expires_in"), 600),
        interval=_positive_int(payload.get("interval"), DEFAULT_INTERVAL),
        verification_uri_complete=str(complete) if complete else None,
    )


def _backoff(previous: float, interval: float, retry_after: float | None) -> float:
    if retry_after is not None:
        return min(max(interval, retry_after), BACKOFF_CAP)
    return min(max(previous * 2, interval * 2, BACKOFF_FLOOR), BACKOFF_CAP)


async def poll_for_token(
    client: httpx.AsyncClient,
    spec: DeviceFlowSpec,
    grant: DeviceAuthorization,
    *,
    clock: Clock,
) -> TokenSet:
    """Poll the token endpoint until approval, refusal, expiry or the deadline (RFC 8628 §3.4)."""
    deadline = clock.monotonic() + max(1, grant.expires_in)
    interval = float(max(1, grant.interval))
    step = max(1, spec.slow_down_step)
    backoff = 0.0
    failing_status: int | None = None
    form = {
        "grant_type": DEVICE_GRANT_TYPE,
        "device_code": grant.device_code,
        "client_id": spec.client_id,
    }
    while True:
        remaining = deadline - clock.monotonic()
        if remaining <= 0:
            break
        wait = interval
        try:
            response = await client.post(spec.token_url, data=form)
        except httpx.TransportError as exc:
            log.debug("token endpoint unreachable: %s", exc)
            failing_status = 0
            backoff = _backoff(backoff, interval, None)
            wait = backoff
        else:
            payload = _json_object(response)
            status = response.status_code
            if status == 200 and payload is not None:
                return TokenSet.from_response(payload, clock.now())
            if payload is None:
                if status in _STRUGGLING or status >= 500 or status == 403:
                    failing_status = status
                    retry_after = parse_retry_after(dict(response.headers), now=clock.now())
                    backoff = _backoff(backoff, interval, retry_after)
                    wait = backoff
                else:
                    raise DeviceFlowError(
                        f"{spec.provider}'s token endpoint answered HTTP {status} without a JSON "
                        "body"
                    )
            else:
                backoff, failing_status = 0.0, None
                error = str(payload.get("error") or "")
                if error == "authorization_pending":
                    pass
                elif error == "slow_down":
                    interval = min(interval + step, INTERVAL_CAP)
                    wait = interval
                elif error == "access_denied":
                    raise AccessDenied("the sign-in request was declined")
                elif error == "expired_token":
                    raise DeviceCodeExpired(
                        "the sign-in code expired before it was approved; start again"
                    )
                else:
                    described = payload.get("error_description")
                    detail = f": {described}" if described else ""
                    raise DeviceFlowError(
                        f"{spec.provider} refused the sign-in ({error or f'HTTP {status}'}){detail}"
                    )
        remaining = deadline - clock.monotonic()
        if remaining <= 0:
            break
        await clock.sleep(min(wait, remaining))
    if failing_status is not None:
        shown = f"HTTP {failing_status}" if failing_status else "no connection"
        raise SignInServiceDown(
            f"{spec.provider}'s sign-in service kept failing ({shown}) until the code expired. "
            "This is not about your approval; try again later.",
            last_status=failing_status or None,
        )
    raise DeviceCodeExpired("the sign-in code expired before it was approved; start again")


async def refresh_tokens(
    client: httpx.AsyncClient, spec: DeviceFlowSpec, tokens: TokenSet, *, clock: Clock
) -> TokenSet:
    """Use the refresh grant (RFC 6749 §6); a dead grant raises ``ReloginRequired``."""
    if not tokens.refresh_token:
        raise ReloginRequired(f"the {spec.provider} sign-in cannot be renewed; sign in again")
    form = {
        "grant_type": REFRESH_GRANT_TYPE,
        "refresh_token": tokens.refresh_token,
        "client_id": spec.client_id,
    }
    if spec.scope:
        form["scope"] = spec.scope
    try:
        response = await client.post(spec.token_url, data=form)
    except httpx.HTTPError as exc:
        raise DeviceFlowError(f"could not reach {spec.provider}'s token endpoint: {exc}") from exc
    payload = _json_object(response)
    if response.status_code == 200 and payload is not None:
        return TokenSet.from_response(payload, clock.now(), keep_refresh=tokens.refresh_token)
    error = str((payload or {}).get("error") or "")
    described = str((payload or {}).get("error_description") or "")
    if error in DEAD_GRANT_ERRORS or "reuse" in described.lower():
        raise ReloginRequired(
            f"the {spec.provider} sign-in is no longer valid ({error or 'rejected'}); sign in again"
        )
    raise DeviceFlowError(
        f"{spec.provider}'s token endpoint did not renew the sign-in "
        f"(HTTP {response.status_code}{', ' + error if error else ''})"
    )


async def fresh_tokens(
    client: httpx.AsyncClient, spec: DeviceFlowSpec, tokens: TokenSet, *, clock: Clock
) -> TokenSet:
    """``tokens`` when still good for ``REFRESH_SKEW_SECONDS``, else renewed ones."""
    if needs_refresh(tokens, clock.now()):
        return await refresh_tokens(client, spec, tokens, clock=clock)
    return tokens


# -- the whole login ---------------------------------------------------------------------------


async def run_device_login(
    spec: DeviceFlowSpec,
    *,
    clock: Clock,
    say: Say,
    open_url: OpenUrl | None,
    client: httpx.AsyncClient,
) -> TokenSet:
    """Ask for a code, show it, open the page when ``open_url`` is given, wait for approval."""
    grant = await request_device_authorization(client, spec)
    say(f"To sign in to {spec.provider}:")
    say(f"  1. Open {grant.url_to_show}")
    say(f"  2. Enter the code {grant.user_code} if the page asks for it")
    minutes = max(1, round(grant.expires_in / 60))
    say(f"The code is valid for about {minutes} minute(s).")
    if open_url is not None:
        if open_url(grant.url_to_show):
            say("(A browser window was opened for you.)")
        else:
            say("(No browser could be opened; use the address above.)")
    say("Waiting for approval…")
    return await poll_for_token(client, spec, grant, clock=clock)
