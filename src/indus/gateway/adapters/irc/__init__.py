"""IRC for the gateway (P16-27): ``create_adapter`` is the registry's entry point.

``IRC_SERVER`` and ``IRC_NICKNAME`` turn the platform on. Neither is a secret, so neither is
masked; the gateway's token lock is taken on the two together (the registry entry's ``lock_env``:
one nick on one network belongs to one gateway).
Channels: ``IRC_CHANNEL`` (comma-separated) or ``platforms.irc.channels``. Optional secrets
``IRC_SERVER_PASSWORD`` (``PASS``), ``IRC_SASL_PASSWORD`` (SASL PLAIN, account
``platforms.irc.sasl_username`` or the nick) and ``IRC_NICKSERV_PASSWORD``. ``platforms.irc``
keys (``IRC_PORT`` / ``IRC_USE_TLS`` in the environment also work): ``port`` (6697 with TLS,
6667 without), ``use_tls`` (true), ``allow_plaintext_auth`` (false), ``realname``. No library:
asyncio streams. ``CONNECTOR`` opens the connection (tests put the fake server there).
"""

from __future__ import annotations

from typing import Any

from indus.gateway.adapters.irc.adapter import Connector, IrcAdapter, IrcOptions, open_tcp
from indus.gateway.adapters.irc.protocol import is_channel, valid_target
from indus.gateway.config import AdapterConfig, read_secret

__all__ = [
    "CHANNEL_ENV",
    "CONNECTOR",
    "NICK_ENV",
    "SERVER_ENV",
    "IrcAdapter",
    "create_adapter",
]

SERVER_ENV = "IRC_SERVER"
NICK_ENV = "IRC_NICKNAME"
CHANNEL_ENV = "IRC_CHANNEL"
SERVER_PASSWORD_ENV = "IRC_SERVER_PASSWORD"
SASL_PASSWORD_ENV = "IRC_SASL_PASSWORD"
NICKSERV_PASSWORD_ENV = "IRC_NICKSERV_PASSWORD"
PORT_ENV = "IRC_PORT"
TLS_ENV = "IRC_USE_TLS"

CONNECTOR: Connector = open_tcp


def _truthy(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return value is True


def _channels(value: Any) -> tuple[str, ...]:
    items = value if isinstance(value, list | tuple) else str(value or "").split(",")
    found = tuple(str(c).strip() for c in items if str(c).strip())
    bad = [c for c in found if not (is_channel(c) and valid_target(c))]
    if bad:
        raise ValueError(f"not IRC channel names: {', '.join(bad)}")
    return found


def create_adapter(config: AdapterConfig) -> IrcAdapter:
    def secret(name: str) -> str | None:
        return config.settings.credentials.get(name) or read_secret(config.scope, name)

    extra = config.extra
    server = (secret(SERVER_ENV) or "").strip()
    nick = (secret(NICK_ENV) or "").strip()
    if not server or not nick:
        raise ValueError(f"{SERVER_ENV} and {NICK_ENV} must both be set")
    if not valid_target(nick) or nick[:1] in "#&+!":
        raise ValueError(f"{NICK_ENV} is not a usable nick")
    use_tls = _truthy(extra.get("use_tls", secret(TLS_ENV)), True)
    raw_port = extra.get("port") or secret(PORT_ENV)
    port = int(str(raw_port).strip()) if raw_port not in (None, "") else (6697 if use_tls else 6667)
    if not 0 < port < 65536:
        raise ValueError("platforms.irc.port is not a port number")
    options = IrcOptions(
        server=server,
        nickname=nick,
        port=port,
        channels=_channels(extra.get("channels") or secret(CHANNEL_ENV)),
        use_tls=use_tls,
        realname=str(extra.get("realname") or "Indus Agent"),
        allow_plaintext_auth=_truthy(extra.get("allow_plaintext_auth"), False),
        sasl_username=str(extra.get("sasl_username") or "").strip() or None,
        server_password=secret(SERVER_PASSWORD_ENV),
        sasl_password=secret(SASL_PASSWORD_ENV),
        nickserv_password=secret(NICKSERV_PASSWORD_ENV),
    )
    return IrcAdapter(
        options,
        clock=config.clock,
        connector=CONNECTOR,
        settings=config.settings,
        send_only=config.send_only,
    )
