"""iMessage through a BlueBubbles server (P16-24); see ``adapter`` for the details.

``create_adapter(config)`` is what the platform registry's lazy factory calls
(``indus.gateway.adapters.bluebubbles:create_adapter``). Credentials come from the profile's secret
store first, then its environment (vendor names, D-16.22): ``BLUEBUBBLES_SERVER_URL`` (or
``platforms.bluebubbles.server_url``) and ``BLUEBUBBLES_PASSWORD``. Everything runs on httpx: no
extra to install.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlparse

from indus.gateway.adapters._http import ListenerSettings
from indus.gateway.adapters.bluebubbles.adapter import (
    PASSWORD_ENV,
    URL_ENV,
    WEBHOOK_MAX_BODY,
    WEBHOOK_PATH,
    WEBHOOK_PORT,
    BlueBubblesAdapter,
    BlueBubblesOptions,
)
from indus.gateway.config import AdapterConfig, read_secret
from indus.gateway.http.serving import is_loopback

if TYPE_CHECKING:
    import httpx

__all__ = ["CREDENTIALS", "BlueBubblesAdapter", "create_adapter"]

# The vendor credential the gateway's enablement, masking and token lock key on (the server URL is
# read by the adapter, like every other host).
CREDENTIALS: tuple[str, ...] = (PASSWORD_ENV,)


def create_adapter(
    config: AdapterConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    serve: bool = True,
) -> BlueBubblesAdapter:
    """The BlueBubbles adapter for ``config`` (``transport`` replaces the network in tests).
    A bad ``webhook_*`` setting raises ``ListenerError`` (the registry logs it)."""
    settings, scope, extra = config.settings, config.scope, config.extra
    url = (
        settings.credentials.get(URL_ENV)
        or read_secret(scope, URL_ENV)
        or str(extra.get("server_url") or "").strip()
    )
    password = settings.credentials.get(PASSWORD_ENV) or read_secret(scope, PASSWORD_ENV) or ""
    listener = ListenerSettings.from_extra(
        extra, port=WEBHOOK_PORT, max_body_bytes=WEBHOOK_MAX_BODY, path=WEBHOOK_PATH
    )
    host = urlparse(url if "://" in url else f"http://{url}").hostname or ""
    return BlueBubblesAdapter(
        clock=config.clock,
        server_url=url,
        password=password,
        listener=listener,
        settings=settings,
        options=BlueBubblesOptions.read(extra),
        transport=transport,
        proxy=None if is_loopback(host) else config.proxy,
        serve=serve,
        send_only=config.send_only,
    )
