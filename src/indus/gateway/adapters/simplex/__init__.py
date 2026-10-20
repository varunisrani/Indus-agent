"""SimpleX Chat through a user-run ``simplex-chat`` daemon (P16-28); see ``adapter``.

``create_adapter(config)`` is what the platform registry's lazy factory calls
(``indus.gateway.adapters.simplex:create_adapter``). The daemon's WebSocket URL comes from the
profile's secret store first, then its environment (``SIMPLEX_WS_URL``), then
``platforms.simplex.ws_url``, else ``ws://127.0.0.1:5225``. The socket needs the ``simplex`` extra
(httpx-ws), imported only when it opens.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from indus.gateway.adapters.simplex.adapter import (
    DEFAULT_URL,
    URL_ENV,
    SimplexAdapter,
    SimplexOptions,
)
from indus.gateway.config import AdapterConfig, read_secret

if TYPE_CHECKING:
    from indus.gateway.adapters.simplex.adapter import SocketOpener

__all__ = ["PUBLIC_ENV", "SimplexAdapter", "create_adapter"]

# The value whose presence enables the platform and keys the token lock (``BUILTIN_PUBLIC_ENV``
# and the registry entry's ``lock_env``): not a secret, so never masked; the daemon has no
# credential.
PUBLIC_ENV: tuple[str, ...] = (URL_ENV,)


def create_adapter(config: AdapterConfig, *, opener: SocketOpener | None = None) -> SimplexAdapter:
    """The SimpleX adapter for ``config`` (``opener`` replaces the WebSocket in tests)."""
    settings, scope, extra = config.settings, config.scope, config.extra
    url = read_secret(scope, URL_ENV) or str(extra.get("ws_url") or "").strip() or DEFAULT_URL
    return SimplexAdapter(
        clock=config.clock,
        ws_url=url,
        settings=settings,
        options=SimplexOptions.read(extra, scope.env),
        opener=opener,
        send_only=config.send_only,
    )
