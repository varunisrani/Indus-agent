"""ntfy for the gateway (P16-26): ``create_adapter`` is the registry's entry point.

``NTFY_TOPIC`` (one topic or several, comma-separated) turns the platform on and keys the
gateway's token lock (the registry entry's ``lock_env``; not masked as a secret); ``NTFY_TOKEN``
(optional: an access token, or ``user:pass`` for basic auth) and ``NTFY_SERVER_URL`` (default
``https://ntfy.sh``) come from the secret store or the environment.
``platforms.ntfy`` keys: ``server``, ``topics``, ``reply_topic`` (where replies to the subscribed
topics go; default the topic itself), ``markdown`` (false), ``stream_timeout_seconds`` (90). A
credential is only ever sent to an https server (or one on this machine). No SDK: our own client
over httpx. ``CLIENT_FACTORY`` builds the HTTP client (tests put the wire fake there).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from indus.gateway.adapters.ntfy.adapter import (
    DEFAULT_SERVER,
    STREAM_TIMEOUT_S,
    NtfyAdapter,
    NtfyApi,
    NtfyMarkdownAdapter,
    auth_header,
    plain_http_allowed,
    server_url,
)
from indus.gateway.config import AdapterConfig, read_secret

__all__ = [
    "CLIENT_FACTORY",
    "SERVER_ENV",
    "TOKEN_ENV",
    "TOPIC_ENV",
    "NtfyAdapter",
    "create_adapter",
]

TOPIC_ENV = "NTFY_TOPIC"
TOKEN_ENV = "NTFY_TOKEN"
SERVER_ENV = "NTFY_SERVER_URL"


def default_client(config: AdapterConfig) -> httpx.AsyncClient:
    # No overall read timeout: the subscription is a long-lived stream (the adapter has its own
    # silence watchdog); connect and write stay bounded.
    timeout = httpx.Timeout(None, connect=15.0, write=15.0, pool=15.0)
    return httpx.AsyncClient(timeout=timeout, proxy=config.proxy, follow_redirects=False)


CLIENT_FACTORY: Callable[[AdapterConfig], httpx.AsyncClient] = default_client


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return value is True


def _topics(value: Any) -> list[str]:
    if isinstance(value, list | tuple):
        return [str(t).strip() for t in value if str(t).strip()]
    return [t.strip() for t in str(value or "").split(",") if t.strip()]


def _secret_forms(token: str | None) -> tuple[str, ...]:
    """The credential as configured and as it travels (basic auth is base64 on the wire)."""
    if not token:
        return ()
    header = auth_header(token).get("Authorization", "")
    return (token, header.split(" ", 1)[1]) if header else (token,)


def create_adapter(config: AdapterConfig) -> NtfyAdapter:
    extra = config.extra
    topics = _topics(
        config.settings.credentials.get(TOPIC_ENV)
        or read_secret(config.scope, TOPIC_ENV)
        or extra.get("topics")
    )
    if not topics:
        raise ValueError(f"{TOPIC_ENV} is not set")
    server = server_url(
        str(extra.get("server") or read_secret(config.scope, SERVER_ENV) or DEFAULT_SERVER)
    )
    token = read_secret(config.scope, TOKEN_ENV)
    if token and not plain_http_allowed(server):
        raise ValueError("NTFY_TOKEN is only sent to an https ntfy server (or one on this machine)")
    raw_timeout = extra.get("stream_timeout_seconds")
    timeout = float(raw_timeout) if raw_timeout not in (None, "") else STREAM_TIMEOUT_S
    if not timeout > 0:
        raise ValueError("platforms.ntfy.stream_timeout_seconds must be positive")
    reply = str(extra.get("reply_topic") or "").strip() or None
    cls = NtfyMarkdownAdapter if _truthy(extra.get("markdown")) else NtfyAdapter
    api = NtfyApi(server, credential=token, client_factory=lambda: CLIENT_FACTORY(config))
    return cls(
        api,
        topics=topics,
        clock=config.clock,
        settings=config.settings,
        secrets=_secret_forms(token),
        reply_topic=reply,
        send_only=config.send_only,
        stream_timeout=timeout,
    )
