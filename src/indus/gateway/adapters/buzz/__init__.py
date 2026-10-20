"""Buzz, a Nostr community relay (P16-29); see ``adapter`` and ``crypto``.

``create_adapter(config)`` is what the platform registry's lazy factory calls
(``indus.gateway.adapters.buzz:create_adapter``). ``BUZZ_PRIVATE_KEY`` (``nsec`` or hex) comes from
the profile's secret store first, then its environment, and is never written anywhere;
``BUZZ_RELAY_URL`` from there or ``platforms.buzz.relay_url``. Signing (coincurve) and the relay
socket (httpx-ws) need the ``buzz`` extra, imported only when the adapter connects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from indus.gateway.adapters.buzz.adapter import (
    ALLOWLIST_ENV,
    KEY_ENV,
    URL_ENV,
    BuzzAdapter,
    BuzzOptions,
)
from indus.gateway.config import AdapterConfig, read_secret
from indus.gateway.core.authz import allow_set

if TYPE_CHECKING:
    from indus.gateway.adapters.buzz.adapter import SocketOpener
    from indus.gateway.adapters.buzz.crypto import NostrCrypto

__all__ = ["CREDENTIALS", "BuzzAdapter", "create_adapter"]

# The secret that enables the platform and keys the token lock (one identity, one gateway).
CREDENTIALS: tuple[str, ...] = (KEY_ENV,)


def create_adapter(
    config: AdapterConfig,
    *,
    crypto: NostrCrypto | None = None,
    opener: SocketOpener | None = None,
) -> BuzzAdapter:
    """The Buzz adapter for ``config`` (``crypto`` / ``opener`` replace the extra in tests)."""
    settings, scope, extra = config.settings, config.scope, config.extra
    url = (
        settings.credentials.get(URL_ENV)
        or read_secret(scope, URL_ENV)
        or str(extra.get("relay_url") or "").strip()
    )
    key = settings.credentials.get(KEY_ENV) or read_secret(scope, KEY_ENV) or ""
    allowlist = allow_set(read_secret(scope, ALLOWLIST_ENV)) | allow_set(extra.get("allow_from"))
    return BuzzAdapter(
        clock=config.clock,
        relay_url=url,
        private_key=key,
        settings=settings,
        options=BuzzOptions.read(extra, scope.env),
        allowlist=sorted(allowlist),
        crypto=crypto,
        opener=opener,
        proxy=config.proxy,
        send_only=config.send_only,
    )
