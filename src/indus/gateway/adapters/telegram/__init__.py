"""Telegram for the gateway (P15-33): ``create_adapter`` is the registry's entry point.

The registry row (``indus.gateway.registry``) points at ``create_adapter``. It reads the bot token
(``TELEGRAM_BOT_TOKEN``, already resolved into the platform settings from the secret store or the
environment), the webhook URL and secret (``TELEGRAM_WEBHOOK_URL`` / ``TELEGRAM_WEBHOOK_SECRET``,
secret store first, or ``platforms.telegram.webhook_url``), the proxy the gateway chose and the
``platforms.telegram`` extra keys, and builds the adapter over our own Bot API client.
``BOT_FACTORY`` builds the client (tests put the wire fake there). Native drafts follow
``display.platforms.telegram.native_stream``; sticker descriptions are kept in
``<home>/gateway/telegram_stickers.json``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from indus.gateway.adapters._native import native_stream_enabled
from indus.gateway.adapters.telegram.adapter import TelegramAdapter, TelegramOptions
from indus.gateway.adapters.telegram.bot_api import BotApi
from indus.gateway.adapters.telegram.inbound import UpdateReceipts
from indus.gateway.adapters.telegram.stickers import CACHE_FILE, StickerCache, StickerNotes
from indus.gateway.config import AdapterConfig, read_secret

__all__ = ["BOT_FACTORY", "TOKEN_ENV", "TelegramAdapter", "create_adapter", "default_bot"]

TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
WEBHOOK_URL_ENV = "TELEGRAM_WEBHOOK_URL"
WEBHOOK_SECRET_ENV = "TELEGRAM_WEBHOOK_SECRET"


def default_bot(token: str, config: AdapterConfig) -> BotApi:
    base_url = str(config.extra.get("base_url") or "").strip() or None
    return BotApi(token, base_url=base_url, proxy=config.proxy)


BOT_FACTORY: Callable[[str, AdapterConfig], BotApi] = default_bot


def _receipts_path(home: Path) -> Callable[[int], Path]:
    return lambda bot_id: home / "gateway" / f"telegram_receipts_{bot_id}.json"


def create_adapter(config: AdapterConfig) -> TelegramAdapter:
    token = config.settings.credentials.get(TOKEN_ENV) or read_secret(config.scope, TOKEN_ENV)
    if not token:
        raise ValueError(f"{TOKEN_ENV} is not set")
    secret = read_secret(config.scope, WEBHOOK_SECRET_ENV)
    options = TelegramOptions.from_extra(
        config.extra,
        webhook_url=read_secret(config.scope, WEBHOOK_URL_ENV),
        webhook_secret=secret,
        native_stream=native_stream_enabled(config.scope, "telegram"),
    )
    stickers = StickerNotes(
        StickerCache(config.scope.home / "gateway" / CACHE_FILE),
        config.clock,
        enabled=options.sticker_descriptions,
    )
    return TelegramAdapter(
        BOT_FACTORY(token, config),
        clock=config.clock,
        settings=config.settings,
        secrets=tuple(s for s in (token, secret) if s),
        options=options,
        receipts=UpdateReceipts(config.clock, path_for=_receipts_path(config.scope.home)),
        send_only=config.send_only,
        stickers=stickers,
    )
