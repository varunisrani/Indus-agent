"""Microsoft Teams for the gateway (P16-11).

``create_adapter(config)`` is what the platform registry's lazy factory calls
(``indus.gateway.adapters.teams:create_adapter``). The app registration's credentials come from
the profile's secret store first, then its environment (vendor names, D-16.22):
``TEAMS_CLIENT_ID``, ``TEAMS_CLIENT_SECRET``, ``TEAMS_TENANT_ID``. Everything else is read from
``platforms.teams`` (listener ``webhook_*`` keys, ``service_url`` — else ``TEAMS_SERVICE_URL`` —,
``extra_service_hosts``, ``openid_metadata_url``). No extra is needed: the adapter speaks the Bot
Framework REST API over httpx and checks the Bot Connector's tokens itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from indus.gateway.config import AdapterConfig, read_secret

if TYPE_CHECKING:
    import httpx

    from indus.gateway.adapters.teams.adapter import TeamsAdapter
    from indus.gateway.adapters.teams.botauth import FetchJson

__all__ = ["CLIENT_ID_ENV", "CLIENT_SECRET_ENV", "CREDENTIALS", "TENANT_ID_ENV", "create_adapter"]

CLIENT_ID_ENV = "TEAMS_CLIENT_ID"
CLIENT_SECRET_ENV = "TEAMS_CLIENT_SECRET"
TENANT_ID_ENV = "TEAMS_TENANT_ID"
SERVICE_URL_ENV = "TEAMS_SERVICE_URL"
# The vendor credentials the gateway's enablement and token lock key on.
CREDENTIALS: tuple[str, ...] = (CLIENT_ID_ENV, CLIENT_SECRET_ENV, TENANT_ID_ENV)


def _credential(config: AdapterConfig, name: str) -> str:
    return config.settings.credentials.get(name) or read_secret(config.scope, name) or ""


def create_adapter(
    config: AdapterConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    key_fetch: FetchJson | None = None,
) -> TeamsAdapter:
    """The Teams adapter for ``config`` (``transport`` / ``key_fetch`` replace the network in
    tests). Raises ``ValueError`` for missing credentials or unusable settings."""
    from indus.gateway.adapters._http import ListenerSettings
    from indus.gateway.adapters.teams.adapter import (
        DEFAULT_PORT,
        MAX_BODY_BYTES,
        WEBHOOK_PATH,
        TeamsAdapter,
        TeamsOptions,
    )
    from indus.gateway.adapters.teams.conversations import ConversationBook

    app_id = _credential(config, CLIENT_ID_ENV)
    secret = _credential(config, CLIENT_SECRET_ENV)
    tenant = _credential(config, TENANT_ID_ENV)
    missing = [n for n, v in zip(CREDENTIALS, (app_id, secret, tenant), strict=True) if not v]
    if missing:
        raise ValueError(f"Teams needs {', '.join(missing)}")
    extra = dict(config.extra)
    if not str(extra.get("service_url") or "").strip():
        # The regional or sovereign Bot Connector base may also come from the environment.
        extra["service_url"] = read_secret(config.scope, SERVICE_URL_ENV) or ""
    options = TeamsOptions.read(extra)
    listener = ListenerSettings.from_extra(
        config.extra, port=DEFAULT_PORT, max_body_bytes=MAX_BODY_BYTES, path=WEBHOOK_PATH
    )
    book = ConversationBook(
        config.scope.home / "gateway" / "teams_conversations.json",
        extra_hosts=options.extra_service_hosts,
    )
    return TeamsAdapter(
        clock=config.clock,
        settings=config.settings,
        app_id=app_id,
        app_secret=secret,
        tenant_id=tenant,
        listener=listener,
        options=options,
        book=book,
        transport=transport,
        proxy=config.proxy,
        key_fetch=key_fetch,
        send_only=config.send_only,
    )
