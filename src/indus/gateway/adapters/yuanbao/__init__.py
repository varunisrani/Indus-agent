"""Tencent Yuanbao for the gateway (P16-21) and its model tools (P16-22, ``tools.py``).

``create_adapter(config)`` is what the platform registry's lazy factory calls
(``indus.gateway.adapters.yuanbao:create_adapter``). Credentials come from the profile's secret
store first, then its environment (vendor names, D-16.22): ``YUANBAO_APP_ID`` and
``YUANBAO_APP_SECRET``; the App ID keys the gateway's token lock. Optional: ``YUANBAO_BOT_ID``,
``YUANBAO_WS_URL``, ``YUANBAO_API_DOMAIN``, ``YUANBAO_ROUTE_ENV`` (or the ``platforms.yuanbao``
keys ``bot_id``, ``ws_url``, ``api_domain``, ``route_env``), and ``dm_policy``, ``group_policy``,
``allow_from``, ``group_allowed_chats``, ``media_hosts``, ``max_media_bytes``, ``instance_id``.
HTTP runs on httpx; the socket needs the ``yuanbao`` extra (httpx-ws), imported only when it opens.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from indus.gateway.adapters.yuanbao.adapter import (
    APP_ID_ENV,
    SECRET_ENV,
    YuanbaoAdapter,
    YuanbaoOptions,
)
from indus.gateway.config import AdapterConfig, read_secret
from indus.tools.registry import scope_key

if TYPE_CHECKING:
    import httpx

    from indus.gateway.adapters.yuanbao.client import SocketOpener

__all__ = ["CREDENTIALS", "LOCK_ENV", "YuanbaoAdapter", "create_adapter"]

# The vendor credentials the gateway's enablement and token lock key on.
CREDENTIALS: tuple[str, ...] = (APP_ID_ENV, SECRET_ENV)
# One bot (App ID) answers in one gateway; a new App Secret is the same bot.
LOCK_ENV: tuple[str, ...] = (APP_ID_ENV,)


def create_adapter(
    config: AdapterConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    opener: SocketOpener | None = None,
) -> YuanbaoAdapter:
    """The Yuanbao adapter for ``config`` (``transport`` / ``opener`` replace the network in
    tests). ``ValueError`` for a URL setting that would send the token in clear."""

    def value(name: str) -> str:
        found = config.settings.credentials.get(name) or read_secret(config.scope, name)
        return (found or "").strip()

    options = YuanbaoOptions.read(config.extra, lambda name: read_secret(config.scope, name))
    return YuanbaoAdapter(
        clock=config.clock,
        app_id=value(APP_ID_ENV),
        secret=value(SECRET_ENV),
        settings=config.settings,
        options=options,
        transport=transport,
        proxy=config.proxy,
        opener=opener,
        send_only=config.send_only,
        live_key=scope_key(config.scope),
    )
