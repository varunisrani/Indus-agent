"""Mattermost for the gateway (P16-08).

``create_adapter(config)`` is what the platform registry's lazy factory calls
(``indus.gateway.adapters.mattermost:create_adapter``). Credentials come from the profile's secret
store first, then its environment (vendor names, D-16.22): ``MATTERMOST_TOKEN`` (a bot or
personal-access token) and ``MATTERMOST_URL`` (or ``platforms.mattermost.url``). REST runs on httpx;
the event socket needs the ``mattermost`` extra (httpx-ws), imported only when it opens.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from indus.gateway.adapters.mattermost.adapter import (
    TOKEN_ENV,
    URL_ENV,
    MattermostAdapter,
    MattermostOptions,
)
from indus.gateway.config import AdapterConfig, read_secret

if TYPE_CHECKING:
    import httpx

    from indus.gateway.adapters.mattermost.client import SocketOpener

__all__ = ["CREDENTIALS", "MattermostAdapter", "create_adapter"]

# The vendor credential the gateway's enablement and token lock key on.
CREDENTIALS: tuple[str, ...] = (TOKEN_ENV,)


def create_adapter(
    config: AdapterConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    opener: SocketOpener | None = None,
) -> MattermostAdapter:
    """The Mattermost adapter for ``config`` (``transport`` / ``opener`` replace the network in
    tests)."""
    settings = config.settings
    token = settings.credentials.get(TOKEN_ENV) or read_secret(config.scope, TOKEN_ENV) or ""
    configured = settings.extra.get("url")
    if isinstance(configured, str) and configured.strip():
        url = configured.strip()
    else:
        url = settings.credentials.get(URL_ENV) or read_secret(config.scope, URL_ENV) or ""
    return MattermostAdapter(
        clock=config.clock,
        settings=settings,
        base_url=url,
        token=token,
        options=MattermostOptions.read(config.extra, config.scope.env),
        transport=transport,
        proxy=config.proxy,
        opener=opener,
        send_only=config.send_only,
    )
