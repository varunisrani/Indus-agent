"""WhatsApp Business Cloud for the gateway (P16-04): ``create_adapter`` is the registry's entry.

Credentials (vendor names, D-16.22; secret store first, then the environment):
``WHATSAPP_CLOUD_ACCESS_TOKEN`` and ``WHATSAPP_CLOUD_PHONE_NUMBER_ID`` (required),
``WHATSAPP_CLOUD_APP_SECRET`` (the webhook signature key; required to receive),
``WHATSAPP_CLOUD_VERIFY_TOKEN`` (Meta's subscription check) and ``WHATSAPP_CLOUD_WABA_ID``
(optional: callbacks must then name this business account). ``platforms.whatsapp_cloud`` keys:
the listener's ``webhook_*`` (port 8090, path ``/whatsapp/webhook``, body ≤3 MiB), ``api_version``,
``base_url`` and ``waba_id``. ``CLIENT_FACTORY`` builds the Graph HTTP client (tests put the wire
fake there).
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

from indus.gateway.adapters._http import ListenerSettings
from indus.gateway.adapters.whatsapp_cloud.adapter import (
    DEFAULT_API_VERSION,
    DEFAULT_PATH,
    DEFAULT_PORT,
    GRAPH_BASE,
    MAX_BODY_BYTES,
    CloudSettings,
    WhatsAppCloudAdapter,
)
from indus.gateway.config import AdapterConfig, read_secret

__all__ = [
    "APP_SECRET_ENV",
    "CLIENT_FACTORY",
    "PHONE_NUMBER_ID_ENV",
    "TOKEN_ENV",
    "VERIFY_TOKEN_ENV",
    "WABA_ID_ENV",
    "WhatsAppCloudAdapter",
    "create_adapter",
    "default_client",
]

TOKEN_ENV = "WHATSAPP_CLOUD_ACCESS_TOKEN"
PHONE_NUMBER_ID_ENV = "WHATSAPP_CLOUD_PHONE_NUMBER_ID"
APP_SECRET_ENV = "WHATSAPP_CLOUD_APP_SECRET"
VERIFY_TOKEN_ENV = "WHATSAPP_CLOUD_VERIFY_TOKEN"
WABA_ID_ENV = "WHATSAPP_CLOUD_WABA_ID"


def default_client(cloud: CloudSettings, proxy: str | None) -> httpx.AsyncClient:
    """The Graph client: no redirects (the token must not follow one), bounded timeouts."""
    return httpx.AsyncClient(
        base_url=cloud.base_url,
        proxy=proxy,
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=False,
    )


CLIENT_FACTORY: Callable[[CloudSettings, str | None], httpx.AsyncClient] = default_client


def create_adapter(config: AdapterConfig) -> WhatsAppCloudAdapter:
    def secret(name: str) -> str:
        return config.settings.credentials.get(name) or read_secret(config.scope, name) or ""

    token, phone_id = secret(TOKEN_ENV), secret(PHONE_NUMBER_ID_ENV)
    if not token or not phone_id:
        raise ValueError(f"{TOKEN_ENV} and {PHONE_NUMBER_ID_ENV} are required")
    extra = config.extra
    cloud = CloudSettings(
        access_token=token,
        phone_number_id=phone_id.strip(),
        app_secret=secret(APP_SECRET_ENV),
        verify_token=secret(VERIFY_TOKEN_ENV),
        waba_id=(secret(WABA_ID_ENV) or str(extra.get("waba_id") or "")).strip(),
        api_version=str(extra.get("api_version") or DEFAULT_API_VERSION).strip(),
        base_url=str(extra.get("base_url") or GRAPH_BASE).strip().rstrip("/"),
    )
    listener = ListenerSettings.from_extra(
        extra, port=DEFAULT_PORT, max_body_bytes=MAX_BODY_BYTES, path=DEFAULT_PATH
    )
    proxy = config.proxy
    return WhatsAppCloudAdapter(
        clock=config.clock,
        settings=config.settings,
        cloud=cloud,
        listener=listener,
        client_factory=lambda: CLIENT_FACTORY(cloud, proxy),
        send_only=config.send_only,
    )
