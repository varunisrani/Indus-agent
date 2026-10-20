"""DingTalk for the gateway (P16-16).

``create_adapter(config)`` is what the platform registry's lazy factory calls
(``indus.gateway.adapters.dingtalk:create_adapter``). Credentials come from the profile's secret
store first, then its environment (vendor names, D-16.22): ``DINGTALK_CLIENT_ID`` (the app key)
and ``DINGTALK_CLIENT_SECRET``. REST runs on httpx; Stream Mode needs the ``dingtalk`` extra
(httpx-ws), imported only when the stream opens, so ``indus send`` works without it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from indus.gateway.adapters.dingtalk.adapter import (
    CLIENT_ID_ENV,
    CLIENT_SECRET_ENV,
    DingTalkAdapter,
    DingTalkOptions,
)
from indus.gateway.config import AdapterConfig, read_secret

if TYPE_CHECKING:
    import httpx

    from indus.gateway.adapters.dingtalk.stream import SocketOpener

__all__ = ["CREDENTIALS", "DingTalkAdapter", "create_adapter"]

# The vendor credentials the gateway's enablement and token lock key on.
CREDENTIALS: tuple[str, ...] = (CLIENT_ID_ENV, CLIENT_SECRET_ENV)


def create_adapter(
    config: AdapterConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    opener: SocketOpener | None = None,
) -> DingTalkAdapter:
    """The DingTalk adapter for ``config`` (``transport`` / ``opener`` replace the network in
    tests)."""
    settings = config.settings

    def secret(name: str) -> str:
        return settings.credentials.get(name) or read_secret(config.scope, name) or ""

    return DingTalkAdapter(
        clock=config.clock,
        settings=settings,
        client_id=secret(CLIENT_ID_ENV),
        client_secret=secret(CLIENT_SECRET_ENV),
        options=DingTalkOptions.read(config.extra, config.scope.env),
        transport=transport,
        proxy=config.proxy,
        opener=opener,
        send_only=config.send_only,
    )
