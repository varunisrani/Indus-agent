"""Google Chat for the gateway (P16-13).

``create_adapter(config)`` is what the platform registry's lazy factory calls
(``indus.gateway.adapters.google_chat:create_adapter``). The one credential is the service
account's key JSON, ``GOOGLE_CHAT_SERVICE_ACCOUNT_JSON`` (secret store first, then the environment;
vendor name, D-16.22). Inbound settings come from ``platforms.google_chat`` (``mode``,
``subscription``, ``audience``, ``audience_type``, ``token_email``, ``webhook_*``) or the vendor env
names ``GOOGLE_CHAT_SUBSCRIPTION_NAME``, ``GOOGLE_CHAT_HTTP_EVENTS_AUDIENCE`` and
``GOOGLE_CHAT_HTTP_EVENTS_SERVICE_ACCOUNT_EMAIL``. No Google SDK is imported; RSA comes from
``cryptography`` (the ``google-chat`` extra) when a key is first used.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from indus.gateway.adapters.google_chat.adapter import (
    ACCOUNT_ENV,
    GoogleChatAdapter,
    GoogleChatOptions,
)
from indus.gateway.config import AdapterConfig, read_secret

if TYPE_CHECKING:
    import httpx

    from indus.gateway.adapters.google_chat.auth import KeyFetcher

__all__ = ["CREDENTIALS", "GoogleChatAdapter", "create_adapter"]

# The vendor credential the gateway's enablement and token lock key on.
CREDENTIALS: tuple[str, ...] = (ACCOUNT_ENV,)


def create_adapter(
    config: AdapterConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    key_fetcher: KeyFetcher | None = None,
    serve: bool = True,
) -> GoogleChatAdapter:
    """The Google Chat adapter for ``config`` (``transport`` / ``key_fetcher`` / ``serve=False``
    replace the network in tests). A bad ``webhook_*`` value raises ``ListenerError`` (the
    registry logs the build failure)."""
    settings = config.settings
    account = settings.credentials.get(ACCOUNT_ENV) or read_secret(config.scope, ACCOUNT_ENV) or ""
    options = GoogleChatOptions.read(config.extra, lambda name: read_secret(config.scope, name))
    listener = None
    if options.mode == "http" and not config.send_only:
        listener = GoogleChatAdapter.listener_settings(config.extra)
    return GoogleChatAdapter(
        clock=config.clock,
        account_json=account,
        settings=settings,
        options=options,
        listener=listener,
        transport=transport,
        proxy=config.proxy,
        key_fetcher=key_fetcher,
        serve=serve,
        send_only=config.send_only,
    )
