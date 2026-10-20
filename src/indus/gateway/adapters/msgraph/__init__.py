"""The Microsoft Graph change-notification receiver for the gateway (P16-12).

``create_adapter(config)`` is what the platform registry's lazy factory calls
(``indus.gateway.adapters.msgraph:create_adapter``). Secrets come from the profile's secret store
first, then its environment: ``MSGRAPH_WEBHOOK_CLIENT_STATE`` (required: the shared secret every
notification must carry) and, for keeping subscriptions alive, ``MSGRAPH_TENANT_ID``,
``MSGRAPH_CLIENT_ID`` and ``MSGRAPH_CLIENT_SECRET``. Everything else is read from
``platforms.msgraph`` (listener ``webhook_*`` keys, ``allowed_source_cidrs``,
``accepted_resources``, ``session``, ``deliver_to``, ``prompt``, ``subscriptions``). httpx only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from indus.gateway.config import AdapterConfig, read_secret

if TYPE_CHECKING:
    import httpx

    from indus.gateway.adapters.msgraph.adapter import GraphReceiver

__all__ = [
    "CLIENT_ID_ENV",
    "CLIENT_SECRET_ENV",
    "CLIENT_STATE_ENV",
    "CREDENTIALS",
    "TENANT_ID_ENV",
    "create_adapter",
]

CLIENT_STATE_ENV = "MSGRAPH_WEBHOOK_CLIENT_STATE"
TENANT_ID_ENV = "MSGRAPH_TENANT_ID"
CLIENT_ID_ENV = "MSGRAPH_CLIENT_ID"
CLIENT_SECRET_ENV = "MSGRAPH_CLIENT_SECRET"
# The secret the gateway's enablement and token lock key on.
CREDENTIALS: tuple[str, ...] = (CLIENT_STATE_ENV,)


def _secret(config: AdapterConfig, name: str) -> str:
    return config.settings.credentials.get(name) or read_secret(config.scope, name) or ""


def create_adapter(
    config: AdapterConfig, *, transport: httpx.AsyncBaseTransport | None = None
) -> GraphReceiver:
    """The Graph receiver for ``config`` (``transport`` replaces the network in tests). Raises
    ``ValueError`` for a missing clientState or unusable settings."""
    from indus.gateway.adapters._entra import GRAPH_SCOPE, ClientCredentials
    from indus.gateway.adapters._http import ListenerSettings
    from indus.gateway.adapters.msgraph.adapter import (
        DEFAULT_PORT,
        MAX_BODY_BYTES,
        WEBHOOK_PATH,
        GraphOptions,
        GraphReceiver,
        client_state_problem,
    )

    client_state = _secret(config, CLIENT_STATE_ENV)
    if not client_state:
        raise ValueError(f"the Graph receiver needs {CLIENT_STATE_ENV}")
    problem = client_state_problem(client_state)
    if problem is not None:
        raise ValueError(f"{CLIENT_STATE_ENV} {problem}")
    options = GraphOptions.read(config.extra)
    listener = ListenerSettings.from_extra(
        config.extra, port=DEFAULT_PORT, max_body_bytes=MAX_BODY_BYTES, path=WEBHOOK_PATH
    )
    tenant, app, app_secret = (
        _secret(config, TENANT_ID_ENV),
        _secret(config, CLIENT_ID_ENV),
        _secret(config, CLIENT_SECRET_ENV),
    )
    credentials = None
    if tenant and app and app_secret:
        credentials = ClientCredentials(
            tenant_id=tenant,
            client_id=app,
            client_secret=app_secret,
            scope=GRAPH_SCOPE,
            clock=config.clock,
        )
    return GraphReceiver(
        clock=config.clock,
        settings=config.settings,
        client_state=client_state,
        listener=listener,
        options=options,
        credentials=credentials,
        store_path=config.scope.home / "gateway" / "msgraph_subscriptions.json",
        transport=transport,
        proxy=config.proxy,
        send_only=config.send_only,
        extra_secrets=(app_secret,) if app_secret else (),
    )
