"""Feishu / Lark for the gateway (P16-14) and its document tools (P16-15).

``create_adapter(config)`` is what the platform registry's lazy factory calls
(``indus.gateway.adapters.feishu:create_adapter``). Credentials come from the profile's secret
store first, then its environment (vendor names, D-16.22): ``FEISHU_APP_ID`` and
``FEISHU_APP_SECRET`` (required), ``FEISHU_ENCRYPT_KEY`` and ``FEISHU_VERIFICATION_TOKEN`` (webhook
mode; the token is required there). The domain (``feishu`` or ``lark``) and the connection mode
(``websocket`` or ``webhook``) come from ``platforms.feishu.extra`` or ``FEISHU_DOMAIN`` /
``FEISHU_CONNECTION_MODE``. REST runs on httpx; the long connection needs the ``feishu`` extra
(httpx-ws), imported only when the socket opens. ``tools.register_tools`` registers the document
tools (see ``tools``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from indus.gateway.adapters.feishu.adapter import (
    APP_ID_ENV,
    APP_SECRET_ENV,
    ENCRYPT_KEY_ENV,
    VERIFICATION_TOKEN_ENV,
    FeishuAdapter,
    FeishuOptions,
)
from indus.gateway.config import AdapterConfig, read_secret

if TYPE_CHECKING:
    import httpx

    from indus.gateway.adapters.feishu.client import SocketOpener

__all__ = ["CREDENTIALS", "FeishuAdapter", "create_adapter"]

# The vendor credentials the gateway's enablement and token lock key on.
CREDENTIALS: tuple[str, ...] = (APP_ID_ENV, APP_SECRET_ENV)


def create_adapter(
    config: AdapterConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    opener: SocketOpener | None = None,
    serve: bool = True,
) -> FeishuAdapter:
    """The Feishu / Lark adapter for ``config`` (``transport`` / ``opener`` / ``serve=False``
    replace the network in tests)."""
    settings = config.settings
    scope = config.scope

    def secret(name: str) -> str:
        return settings.credentials.get(name) or read_secret(scope, name) or ""

    return FeishuAdapter(
        clock=config.clock,
        settings=settings,
        app_id=secret(APP_ID_ENV),
        app_secret=secret(APP_SECRET_ENV),
        encrypt_key=secret(ENCRYPT_KEY_ENV),
        verification_token=secret(VERIFICATION_TOKEN_ENV),
        options=FeishuOptions.read(config.extra, scope.env),
        transport=transport,
        proxy=config.proxy,
        opener=opener,
        send_only=config.send_only,
        serve=serve,
        extra=config.extra,
    )
