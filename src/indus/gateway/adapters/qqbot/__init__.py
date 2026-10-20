"""QQ Bot for the gateway (P16-20).

``create_adapter(config)`` is what the platform registry's lazy factory calls
(``indus.gateway.adapters.qqbot:create_adapter``). Credentials come from the profile's secret store
first, then its environment (vendor names, D-16.22): ``QQ_APP_ID`` and ``QQ_CLIENT_SECRET``.
Settings: ``platforms.qqbot.markdown`` (send QQ Markdown; needs QQ's permission),
``platforms.qqbot.sandbox`` (QQ's sandbox API host), ``platforms.qqbot.max_media_bytes``. REST runs
on httpx; the gateway socket needs the ``qqbot`` extra (httpx-ws), imported only when it opens.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from indus.gateway.adapters.qqbot.adapter import (
    APP_ID_ENV,
    SECRET_ENV,
    QQBotAdapter,
    QQBotMarkdownAdapter,
    QQBotOptions,
)
from indus.gateway.config import AdapterConfig, read_secret

if TYPE_CHECKING:
    import httpx

    from indus.gateway.adapters.qqbot.client import SocketOpener

__all__ = ["CREDENTIALS", "QQBotAdapter", "create_adapter"]

# The vendor credentials the gateway's enablement and token lock key on.
CREDENTIALS: tuple[str, ...] = (APP_ID_ENV, SECRET_ENV)


def create_adapter(
    config: AdapterConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    opener: Callable[[httpx.AsyncClient], SocketOpener] | None = None,
) -> QQBotAdapter:
    """The QQ Bot adapter for ``config`` (``transport`` / ``opener`` replace the network in
    tests)."""

    def value(name: str) -> str:
        found = config.settings.credentials.get(name) or read_secret(config.scope, name)
        return (found or "").strip()

    options = QQBotOptions.read(config.extra)
    cls = QQBotMarkdownAdapter if options.markdown else QQBotAdapter
    return cls(
        clock=config.clock,
        app_id=value(APP_ID_ENV),
        secret=value(SECRET_ENV),
        settings=config.settings,
        options=options,
        transport=transport,
        proxy=config.proxy,
        opener=opener,
        send_only=config.send_only,
    )
