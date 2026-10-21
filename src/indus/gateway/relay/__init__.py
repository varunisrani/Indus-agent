"""The relay connector: the gateway dials out to a connector that fronts chat platforms (P24-08/09).

``transport`` is the one outbound WebSocket (signed hello, descriptors, events, operations,
reconnect, dormant mode); ``descriptor`` reads each fronted platform's capability descriptor;
``adapter`` turns every fronted platform into an ordinary gateway adapter and holds the delivery
router's resolution rule. The wire contract is our own (D-16.1), written up for connector authors
in ``docs/user/relay-contract.md``.

``build_front(scope, clock)`` reads ``gateway.relay`` and the ``INDUS_RELAY_SECRET`` secret and
returns a `RelayFront` (None when no relay is configured). The WebSocket library (httpx-ws) is
imported only when the production opener dials.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from indus.gateway.relay.descriptor import Descriptor, DescriptorError
from indus.gateway.relay.transport import (
    SECRET_ENV,
    RelayConnectError,
    RelaySettings,
    RelayTransport,
    SocketOpener,
)

if TYPE_CHECKING:
    from indus.foundation.clock import Clock
    from indus.foundation.scope import Scope
    from indus.gateway.relay.adapter import RelayFront

__all__ = [
    "SECRET_ENV",
    "Descriptor",
    "DescriptorError",
    "RelayConnectError",
    "RelaySettings",
    "RelayTransport",
    "build_front",
    "relay_settings",
]


def _number(value: object, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return default
    return float(value)


def relay_settings(scope: Scope) -> RelaySettings | None:
    """``gateway.relay`` for ``scope``, or None when no relay URL is configured."""
    from indus.gateway.config import read_secret

    config = scope.config
    if config is None:
        return None
    url = config.get("gateway.relay.url")
    if not isinstance(url, str) or not url.strip():
        return None
    gateway_id = config.get("gateway.relay.gateway_id")
    return RelaySettings(
        url=url.strip(),
        gateway_id=gateway_id.strip() if isinstance(gateway_id, str) else "",
        secret=read_secret(scope, SECRET_ENV) or "",
        connect_timeout=_number(config.get("gateway.relay.connect_timeout_seconds"), 30.0),
        request_timeout=_number(config.get("gateway.relay.request_timeout_seconds"), 30.0),
    )


def build_front(
    scope: Scope, clock: Clock, *, opener: SocketOpener | None = None
) -> RelayFront | None:
    """The relay front for ``scope`` (transport + media client), or None without a relay URL."""
    from indus.gateway.relay.adapter import RelayFront, RelayMediaClient

    settings = relay_settings(scope)
    if settings is None:
        return None
    transport = RelayTransport(settings, clock=clock, opener=opener)
    try:
        media = RelayMediaClient(settings.url, settings.gateway_id, settings.secret, clock=clock)
    except ValueError:
        media = None  # a bad URL is reported when the transport starts
    return RelayFront(transport, media=media)
