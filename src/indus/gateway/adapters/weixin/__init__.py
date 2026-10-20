"""Weixin (personal WeChat through Tencent's iLink Bot API) for the gateway (P16-19).

``create_adapter(config)`` is what the platform registry's lazy factory calls
(``indus.gateway.adapters.weixin:create_adapter``). Credentials come from the profile's secret
store first, then its environment (vendor names, D-16.22): ``WEIXIN_TOKEN`` (the bot token) and
``WEIXIN_ACCOUNT_ID`` (the bot account). Optional: ``WEIXIN_BASE_URL`` /
``platforms.weixin.base_url`` (the iLink host the login gave; https only),
``WEIXIN_CDN_BASE_URL`` / ``cdn_base_url``; ``platforms.weixin`` keys ``group_policy``
(``disabled``, ``open``, ``allowlist``), ``group_allow_from``, ``mention_names``,
``max_media_bytes``. Conversation tokens and the poll cursor live in
``<profile home>/platforms/weixin/<account>/`` (0600 files). AES for media comes from
``cryptography`` (the ``weixin`` extra), imported only when a file moves.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from indus.gateway.adapters.weixin.adapter import (
    WeixinAdapter,
    WeixinOptions,
    state_dir_for,
)
from indus.gateway.adapters.weixin.api import CDN_BASE, ILINK_BASE
from indus.gateway.config import AdapterConfig, read_secret

if TYPE_CHECKING:
    import httpx

__all__ = ["ACCOUNT_ENV", "CREDENTIALS", "TOKEN_ENV", "WeixinAdapter", "create_adapter"]

TOKEN_ENV = "WEIXIN_TOKEN"
ACCOUNT_ENV = "WEIXIN_ACCOUNT_ID"
BASE_ENV = "WEIXIN_BASE_URL"
CDN_ENV = "WEIXIN_CDN_BASE_URL"
# The vendor credentials the gateway's enablement and token lock read (the lock keys on the
# account id: a new token for the same bot is the same bot).
CREDENTIALS: tuple[str, ...] = (TOKEN_ENV, ACCOUNT_ENV)


def _secret(config: AdapterConfig, name: str) -> str:
    found = config.settings.credentials.get(name) or read_secret(config.scope, name)
    return (found or "").strip()


def _setting(config: AdapterConfig, key: str, env: str, default: str) -> str:
    configured = config.extra.get(key)
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    return (read_secret(config.scope, env) or default).strip()


def create_adapter(
    config: AdapterConfig, *, transport: httpx.AsyncBaseTransport | None = None
) -> WeixinAdapter:
    """The Weixin adapter for ``config`` (``transport`` replaces the network in tests)."""
    account = _secret(config, ACCOUNT_ENV)
    return WeixinAdapter(
        clock=config.clock,
        settings=config.settings,
        token=_secret(config, TOKEN_ENV),
        account_id=account,
        base_url=_setting(config, "base_url", BASE_ENV, ILINK_BASE),
        cdn_base_url=_setting(config, "cdn_base_url", CDN_ENV, CDN_BASE),
        options=WeixinOptions.read(config.extra, config.scope.env),
        state_dir=state_dir_for(config.scope.home, account) if account else None,
        transport=transport,
        proxy=config.proxy,
        send_only=config.send_only,
    )
