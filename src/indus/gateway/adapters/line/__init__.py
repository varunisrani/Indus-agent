"""LINE for the gateway (P16-23): ``create_adapter`` is the registry's entry point.

Credentials: ``LINE_CHANNEL_ACCESS_TOKEN`` and ``LINE_CHANNEL_SECRET`` (vendor names, D-16.22;
resolved into the platform settings, else read from the secret store or the environment).
``platforms.line`` keys: the listener kit's ``webhook_*`` keys (port 8646, path
``/line/webhook``, body cap 1 MiB; ``webhook_public_url`` is the HTTPS URL LINE calls and the
origin of our media URLs), ``slow_reply_seconds`` (45; 0 turns the "still working" card off),
``slow_reply_hold_seconds`` (600), ``api_base_url`` / ``data_base_url``. No SDK: our own client
over httpx. ``CLIENT_FACTORY`` builds the HTTP client (tests put the wire fake there).
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

from indus.gateway.adapters._http import ListenerSettings
from indus.gateway.adapters.line.adapter import (
    DEFAULT_PORT,
    MAX_BODY_BYTES,
    WEBHOOK_PATH,
    LineAdapter,
    LineOptions,
)
from indus.gateway.adapters.line.api import API_BASE, DATA_BASE, LineApi
from indus.gateway.config import AdapterConfig, read_secret

__all__ = ["CLIENT_FACTORY", "SECRET_ENV", "TOKEN_ENV", "LineAdapter", "create_adapter"]

TOKEN_ENV = "LINE_CHANNEL_ACCESS_TOKEN"
SECRET_ENV = "LINE_CHANNEL_SECRET"


def default_client(config: AdapterConfig) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(20.0, connect=10.0), proxy=config.proxy, follow_redirects=False
    )


CLIENT_FACTORY: Callable[[AdapterConfig], httpx.AsyncClient] = default_client


def _credential(config: AdapterConfig, name: str) -> str | None:
    return config.settings.credentials.get(name) or read_secret(config.scope, name)


def _https(value: object, default: str, key: str) -> str:
    url = str(value or default).strip().rstrip("/")
    if not url.lower().startswith("https://"):
        raise ValueError(f"platforms.line.{key} must be an https URL (the token travels with it)")
    return url


def create_adapter(config: AdapterConfig) -> LineAdapter:
    token = _credential(config, TOKEN_ENV)
    secret = _credential(config, SECRET_ENV)
    if not token or not secret:
        raise ValueError(f"{TOKEN_ENV} and {SECRET_ENV} must both be set")
    extra = config.extra
    listener = ListenerSettings.from_extra(
        extra, port=DEFAULT_PORT, max_body_bytes=MAX_BODY_BYTES, path=WEBHOOK_PATH
    )
    options = LineOptions.from_extra(
        extra, public_url=listener.public_url, webhook_path=listener.path
    )
    api = LineApi(
        token,
        client_factory=lambda: CLIENT_FACTORY(config),
        api_base=_https(extra.get("api_base_url"), API_BASE, "api_base_url"),
        data_base=_https(extra.get("data_base_url"), DATA_BASE, "data_base_url"),
    )
    return LineAdapter(
        api,
        channel_secret=secret,
        listener=listener,
        clock=config.clock,
        settings=config.settings,
        secrets=(token,),
        options=options,
        send_only=config.send_only,
    )
