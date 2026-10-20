"""WeCom (Enterprise WeChat) for the gateway: two platforms from one folder.

- ``wecom`` (P16-17): the smart robot over its WebSocket API —
  ``indus.gateway.adapters.wecom:create_adapter``; credentials ``WECOM_BOT_ID`` and
  ``WECOM_SECRET`` (secret store first, then the environment; vendor names, D-16.22). The socket
  needs the ``wecom`` extra (httpx-ws).
- ``wecom_callback`` (P16-18): a self-built app receiving encrypted HTTP callbacks —
  ``indus.gateway.adapters.wecom:create_callback_adapter``; credentials
  ``WECOM_CALLBACK_CORP_ID``, ``WECOM_CALLBACK_CORP_SECRET``, ``WECOM_CALLBACK_TOKEN``,
  ``WECOM_CALLBACK_ENCODING_AES_KEY``, plus ``WECOM_CALLBACK_AGENT_ID`` (or
  ``platforms.wecom_callback.agent_id``). The message crypto needs the ``wecom`` extra
  (cryptography); listener keys are the kit's ``webhook_*`` keys (port 8645, path
  ``/wecom/callback``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from indus.gateway.adapters._http import ListenerSettings
from indus.gateway.adapters.wecom.adapter import (
    BOT_ID_ENV,
    SECRET_ENV,
    WeComAdapter,
    WeComOptions,
)
from indus.gateway.adapters.wecom.callback import (
    AGENT_ID_ENV,
    API_BASE,
    CORP_ID_ENV,
    CORP_SECRET_ENV,
    DEFAULT_PATH,
    DEFAULT_PORT,
    ENCODING_AES_KEY_ENV,
    MAX_BODY_BYTES,
    TOKEN_ENV,
    WeComCallbackAdapter,
)
from indus.gateway.config import AdapterConfig, read_secret

if TYPE_CHECKING:
    import httpx

    from indus.gateway.adapters.wecom.adapter import SocketOpener

__all__ = [
    "CALLBACK_CREDENTIALS",
    "CREDENTIALS",
    "WeComAdapter",
    "WeComCallbackAdapter",
    "create_adapter",
    "create_callback_adapter",
]

# The vendor credentials the gateway's enablement and token lock key on.
CREDENTIALS: tuple[str, ...] = (BOT_ID_ENV, SECRET_ENV)
CALLBACK_CREDENTIALS: tuple[str, ...] = (
    CORP_ID_ENV,
    CORP_SECRET_ENV,
    TOKEN_ENV,
    ENCODING_AES_KEY_ENV,
)


def _secret(config: AdapterConfig, name: str) -> str:
    return config.settings.credentials.get(name) or read_secret(config.scope, name) or ""


def create_adapter(
    config: AdapterConfig,
    *,
    opener: SocketOpener | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> WeComAdapter:
    """The smart-robot adapter for ``config`` (``opener`` / ``transport`` replace the network in
    tests)."""
    return WeComAdapter(
        clock=config.clock,
        settings=config.settings,
        bot_id=_secret(config, BOT_ID_ENV),
        secret=_secret(config, SECRET_ENV),
        options=WeComOptions.read(config.extra, config.scope.env),
        opener=opener,
        transport=transport,
        proxy=config.proxy,
        send_only=config.send_only,
    )


def create_callback_adapter(
    config: AdapterConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    serve: bool = True,
    api_base: str | None = None,
) -> WeComCallbackAdapter:
    """The callback-app adapter for ``config``; a listener setting that cannot be served raises
    ``ListenerError`` (the registry logs the failed build)."""
    extra = config.extra
    listener = ListenerSettings.from_extra(
        extra, port=DEFAULT_PORT, max_body_bytes=MAX_BODY_BYTES, path=DEFAULT_PATH
    )
    agent_id = str(extra.get("agent_id") or "").strip() or _secret(config, AGENT_ID_ENV)
    return WeComCallbackAdapter(
        clock=config.clock,
        settings=config.settings,
        corp_id=_secret(config, CORP_ID_ENV),
        corp_secret=_secret(config, CORP_SECRET_ENV),
        agent_id=agent_id,
        token=_secret(config, TOKEN_ENV),
        encoding_aes_key=_secret(config, ENCODING_AES_KEY_ENV),
        listener=listener,
        transport=transport,
        proxy=config.proxy,
        serve=serve and not config.send_only,
        send_only=config.send_only,
        api_base=api_base or API_BASE,
    )
