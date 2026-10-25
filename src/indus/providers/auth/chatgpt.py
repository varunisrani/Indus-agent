"""Signing in with a ChatGPT account (D-16.1282): the device-code flow.

The browser flow is the generic authorization code + PKCE flow (``auth.pkce``) with the row in
``auth.flows``; this module holds what is particular to OpenAI's sign-in service:

- **Device code.** Not RFC 8628: ``POST usercode`` with ``{client_id}`` answers ``{device_auth_id,
  user_code, interval}``; the person enters the code on ``/codex/device``; ``POST token`` with
  ``{device_auth_id, user_code}`` answers 403/404 until they approve, then ``{authorization_code,
  code_verifier}``, which the ordinary token endpoint exchanges (authorization-code grant with the
  device redirect URI). We poll at the server's interval (never under three seconds); a dropped
  connection, a 429 or a 5xx backs off (``Retry-After`` when given, else doubling up to 60 s) and
  six failures in a row end the flow; fifteen minutes end it too. Asking for a code is retried on
  429 a few times (the service limits how often one address may ask).
- **Claims, request shape, model list URL** live in ``auth.chatgpt_wire`` (no HTTP imports, the
  catalogue loads it at start-up) and are re-exported here.

No message raised here carries a token, a code, the verifier or a response body: only status codes
and the provider's own short error codes.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

import httpx

from indus.foundation.clock import Clock
from indus.providers.auth import chatgpt_facts as facts
from indus.providers.auth.chatgpt_wire import (
    DEFAULT_INSTRUCTIONS,
    DROPPED_FIELDS,
    account_headers,
    account_id,
    claims,
    models_url,
    plan,
    prepare_body,
)
from indus.providers.auth.device_code import (
    BACKOFF_CAP,
    DeviceAuthorization,
    DeviceCodeExpired,
    DeviceFlowError,
    SignInServiceDown,
    TokenSet,
)
from indus.providers.retry import parse_retry_after

__all__ = [
    "DEFAULT_INSTRUCTIONS",
    "DROPPED_FIELDS",
    "ChatGptDeviceSpec",
    "account_headers",
    "account_id",
    "claims",
    "models_url",
    "plan",
    "prepare_body",
    "request_user_code",
    "wait_for_approval",
]

log = logging.getLogger(__name__)

USERCODE_ATTEMPTS = 4
MAX_FAILURES_IN_A_ROW = 6
_STRUGGLING = frozenset({408, 429})


@dataclass(frozen=True)
class ChatGptDeviceSpec:
    """OpenAI's device sign-in endpoints (see the module notes)."""

    provider: str = facts.PROVIDER
    usercode_url: str = facts.DEVICE_USERCODE_URL
    poll_url: str = facts.DEVICE_TOKEN_URL
    verify_page: str = facts.DEVICE_VERIFY_PAGE
    token_url: str = facts.TOKEN_URL
    redirect_uri: str = facts.DEVICE_REDIRECT_URI
    client_id: str = facts.CLIENT_ID
    deadline_seconds: int = facts.DEVICE_DEADLINE_SECONDS
    min_interval: int = facts.DEVICE_MIN_INTERVAL
    doc_url: str = "https://github.com/openai/codex (codex-rs/login/src/device_code_auth.rs)"


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


# -- device code ---------------------------------------------------------------------------------


def _json(response: httpx.Response) -> dict[str, Any] | None:
    try:
        data = response.json()
    except (ValueError, UnicodeDecodeError):
        return None
    return cast(dict[str, Any], data) if isinstance(data, dict) else None


def _seconds(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int | float) and value > 0:
        return int(value)
    if isinstance(value, str) and value.strip().isdigit() and int(value.strip()) > 0:
        return int(value.strip())
    return default


def _wait_after(response: httpx.Response | None, previous: float, clock: Clock) -> float:
    retry_after = (
        parse_retry_after(dict(response.headers), now=clock.now()) if response is not None else None
    )
    if retry_after is not None:
        return min(max(1.0, retry_after), BACKOFF_CAP)
    return min(max(previous * 2, 2.0), BACKOFF_CAP)


async def request_user_code(
    client: httpx.AsyncClient, spec: ChatGptDeviceSpec, *, clock: Clock
) -> DeviceAuthorization:
    """Ask for a code to show; retried a few times when the service says 429."""
    pause = 0.0
    response: httpx.Response | None = None
    for attempt in range(1, USERCODE_ATTEMPTS + 1):
        try:
            response = await client.post(spec.usercode_url, json={"client_id": spec.client_id})
        except httpx.HTTPError as exc:
            raise DeviceFlowError(
                f"could not reach OpenAI's sign-in service ({type(exc).__name__})"
            ) from None
        if response.status_code != 429 or attempt == USERCODE_ATTEMPTS:
            break
        pause = _wait_after(response, pause, clock)
        log.info("OpenAI is limiting sign-in requests; asking again in %.0fs", pause)
        await clock.sleep(pause)
    assert response is not None
    if response.status_code == 429:
        raise SignInServiceDown(
            "OpenAI is limiting sign-in requests from here right now (HTTP 429); wait a few "
            "minutes and try again",
            last_status=429,
        )
    payload = _json(response)
    if response.status_code != 200 or payload is None:
        raise DeviceFlowError(
            f"OpenAI's sign-in service refused to start a device sign-in (HTTP "
            f"{response.status_code})"
        )
    auth_id = _text(payload.get("device_auth_id"))
    user_code = _text(payload.get("user_code") or payload.get("usercode"))
    if not auth_id or not user_code:
        raise DeviceFlowError("OpenAI's device sign-in answer lacks its code")
    interval = max(
        spec.min_interval, _seconds(payload.get("interval"), facts.DEVICE_DEFAULT_INTERVAL)
    )
    return DeviceAuthorization(
        device_code=auth_id,
        user_code=user_code,
        verification_uri=spec.verify_page,
        expires_in=spec.deadline_seconds,
        interval=interval,
    )


async def _exchange(
    client: httpx.AsyncClient, spec: ChatGptDeviceSpec, payload: Mapping[str, Any], clock: Clock
) -> TokenSet:
    code = _text(payload.get("authorization_code"))
    verifier = _text(payload.get("code_verifier"))
    if not code or not verifier:
        raise DeviceFlowError("OpenAI approved the sign-in but sent no authorization code")
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": spec.redirect_uri,
        "client_id": spec.client_id,
        "code_verifier": verifier,
    }
    try:
        response = await client.post(spec.token_url, data=form)
    except httpx.HTTPError as exc:
        raise DeviceFlowError(
            f"could not reach OpenAI to finish the sign-in ({type(exc).__name__})"
        ) from None
    body = _json(response)
    if response.status_code != 200 or body is None:
        raise DeviceFlowError(
            f"OpenAI did not finish the sign-in (HTTP {response.status_code}); start again"
        )
    return TokenSet.from_response(body, clock.now())


async def wait_for_approval(
    client: httpx.AsyncClient,
    spec: ChatGptDeviceSpec,
    grant: DeviceAuthorization,
    *,
    clock: Clock,
) -> TokenSet:
    """Poll until the person approves (then exchange the code), or the deadline passes."""
    deadline = clock.monotonic() + max(1, grant.expires_in)
    interval = float(max(1, grant.interval))
    body = {"device_auth_id": grant.device_code, "user_code": grant.user_code}
    failures = 0
    backoff = 0.0
    last_status: int | None = None
    wait = interval
    while True:
        remaining = deadline - clock.monotonic()
        if remaining <= 0:
            break
        await clock.sleep(min(wait, remaining))
        if deadline - clock.monotonic() <= 0:
            break
        response: httpx.Response | None
        try:
            response = await client.post(spec.poll_url, json=body)
        except httpx.TransportError as exc:
            log.debug("device sign-in poll failed: %s", type(exc).__name__)
            response = None
        status = response.status_code if response is not None else 0
        if response is not None and status == 200:
            payload = _json(response)
            if payload is None:
                raise DeviceFlowError("OpenAI's approval answer was not JSON")
            return await _exchange(client, spec, payload, clock)
        if status in facts.DEVICE_PENDING_STATUSES:
            failures, backoff, last_status = 0, 0.0, None
            wait = interval
            continue
        if response is None or status in _STRUGGLING or status >= 500:
            failures += 1
            last_status = status
            if failures >= MAX_FAILURES_IN_A_ROW:
                shown = f"HTTP {status}" if status else "no connection"
                raise SignInServiceDown(
                    f"OpenAI's sign-in service kept failing ({shown}); try again later",
                    last_status=status or None,
                )
            backoff = _wait_after(response, max(backoff, interval), clock)
            wait = backoff
            continue
        raise DeviceFlowError(f"OpenAI refused the device sign-in (HTTP {status})")
    if last_status is not None:
        shown = f"HTTP {last_status}" if last_status else "no connection"
        raise SignInServiceDown(
            f"OpenAI's sign-in service kept failing ({shown}) until the code expired; try again "
            "later",
            last_status=last_status or None,
        )
    raise DeviceCodeExpired("the sign-in code expired before it was approved; start again")
