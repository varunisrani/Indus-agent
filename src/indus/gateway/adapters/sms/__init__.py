"""SMS over Twilio for the gateway (P16-10): ``create_adapter`` is the registry's entry point.

Credentials (vendor names, D-16.22; secret store first, then the environment):
``TWILIO_ACCOUNT_SID``, ``TWILIO_AUTH_TOKEN`` and ``TWILIO_PHONE_NUMBER`` (E.164, the number
replies come from). ``platforms.sms`` keys: the listener's ``webhook_*`` (port 8080, path
``/webhooks/twilio``, body ≤64 KiB; ``webhook_public_url`` = the URL set in the Twilio console,
required to receive) and ``base_url``. ``CLIENT_FACTORY`` builds the Twilio HTTP client (tests put
the wire fake there).
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

from indus.gateway.adapters._http import ListenerSettings
from indus.gateway.adapters.sms.adapter import (
    DEFAULT_PATH,
    DEFAULT_PORT,
    MAX_BODY_BYTES,
    TWILIO_BASE,
    SmsAdapter,
    TwilioSettings,
)
from indus.gateway.config import AdapterConfig, read_secret

__all__ = [
    "ACCOUNT_SID_ENV",
    "AUTH_TOKEN_ENV",
    "CLIENT_FACTORY",
    "PHONE_NUMBER_ENV",
    "SmsAdapter",
    "create_adapter",
    "default_client",
]

ACCOUNT_SID_ENV = "TWILIO_ACCOUNT_SID"
AUTH_TOKEN_ENV = "TWILIO_AUTH_TOKEN"
PHONE_NUMBER_ENV = "TWILIO_PHONE_NUMBER"


def default_client(twilio: TwilioSettings, proxy: str | None) -> httpx.AsyncClient:
    """The Twilio client: no redirects by default (only the media fetch follows them, and httpx
    drops the credentials when a redirect leaves the origin), bounded timeouts."""
    return httpx.AsyncClient(
        base_url=twilio.base_url,
        proxy=proxy,
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=False,
    )


CLIENT_FACTORY: Callable[[TwilioSettings, str | None], httpx.AsyncClient] = default_client


def create_adapter(config: AdapterConfig) -> SmsAdapter:
    def secret(name: str) -> str:
        return (
            config.settings.credentials.get(name) or read_secret(config.scope, name) or ""
        ).strip()

    sid, token, number = secret(ACCOUNT_SID_ENV), secret(AUTH_TOKEN_ENV), secret(PHONE_NUMBER_ENV)
    if not sid or not token or not number:
        raise ValueError(f"{ACCOUNT_SID_ENV}, {AUTH_TOKEN_ENV} and {PHONE_NUMBER_ENV} are required")
    listener = ListenerSettings.from_extra(
        config.extra, port=DEFAULT_PORT, max_body_bytes=MAX_BODY_BYTES, path=DEFAULT_PATH
    )
    twilio = TwilioSettings(
        account_sid=sid,
        auth_token=token,
        phone_number=number,
        public_url=listener.public_url,
        base_url=str(config.extra.get("base_url") or TWILIO_BASE).strip().rstrip("/"),
    )
    proxy = config.proxy
    return SmsAdapter(
        clock=config.clock,
        settings=config.settings,
        twilio=twilio,
        listener=listener,
        client_factory=lambda: CLIENT_FACTORY(twilio, proxy),
        send_only=config.send_only,
    )
