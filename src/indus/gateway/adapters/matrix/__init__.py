"""Matrix for the gateway (P16-06; end-to-end encryption arrives with P16-07).

``create_adapter(config)`` is what the platform registry's lazy factory calls
(``indus.gateway.adapters.matrix:create_adapter``). Credentials come from the profile's secret store
first, then its environment (vendor names, D-16.22): ``MATRIX_ACCESS_TOKEN`` (required),
``MATRIX_HOMESERVER`` (or ``platforms.matrix.homeserver``), optional ``MATRIX_USER_ID`` and
``MATRIX_DEVICE_ID``. Nothing here imports a Matrix SDK.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from indus.gateway.adapters.matrix.adapter import (
    DEVICE_ENV,
    HOMESERVER_ENV,
    TOKEN_ENV,
    USER_ENV,
    MatrixAdapter,
    MatrixOptions,
    load_end_to_end,
)
from indus.gateway.config import AdapterConfig, read_secret

if TYPE_CHECKING:
    import httpx

__all__ = ["CREDENTIALS", "MatrixAdapter", "create_adapter", "split_target"]

# The vendor credential the gateway's enablement and token lock key on (the homeserver URL is read
# separately so it is never masked as a secret in log lines).
CREDENTIALS: tuple[str, ...] = (TOKEN_ENV,)


def _value(config: AdapterConfig, name: str, key: str) -> str:
    configured = config.settings.extra.get(key)
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    found = config.settings.credentials.get(name) or read_secret(config.scope, name)
    return (found or "").strip()


def create_adapter(
    config: AdapterConfig, *, transport: httpx.AsyncBaseTransport | None = None
) -> MatrixAdapter:
    """The Matrix adapter for ``config`` (``transport`` replaces the network in tests)."""
    token = config.settings.credentials.get(TOKEN_ENV) or read_secret(config.scope, TOKEN_ENV)
    options = MatrixOptions.read(config.extra, config.scope.env)
    return MatrixAdapter(
        clock=config.clock,
        settings=config.settings,
        homeserver=_value(config, HOMESERVER_ENV, "homeserver"),
        token=token or "",
        user_id=_value(config, USER_ENV, "user_id"),
        device_id=_value(config, DEVICE_ENV, "device_id"),
        options=options,
        transport=transport,
        proxy=config.proxy,
        scope=config.scope,
        end_to_end=load_end_to_end() if options.e2ee != "off" else None,
        send_only=config.send_only,
    )


def split_target(address: str) -> tuple[str, str | None]:
    """A send/cron address → (room, thread). Room ids and aliases contain ``:``, so the thread
    part is only an event id (``$…``) after the last ``:$``: ``!ops:example.org:$root`` →
    (``!ops:example.org``, ``$root``)."""
    text = address.strip()
    cut = text.rfind(":$")
    if cut > 0:
        return text[:cut], text[cut + 1 :]
    return text, None
