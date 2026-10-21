"""The platform registry: every platform the gateway can run, in one table (P15-06).

Each ``PlatformEntry`` says how to build the platform's adapter, how to tell whether its library is
installed without importing it, which env names carry its credential and its access lists, its
message cap, whether its DMs are really private (R06.24) and an optional cron target parser.
Telegram, Discord, Slack, Signal, Matrix, Mattermost, Email, WhatsApp (Cloud API), SMS, LINE, ntfy,
IRC, Microsoft Teams, Google Chat, QQ Bot, DingTalk, WeCom (smart robot and callback app), Feishu /
Lark, BlueBubbles (iMessage), SimpleX Chat, Buzz, the API server, the webhook receiver, the
Microsoft Graph change-notification receiver and A2A (Agent2Agent, P24-10) are built in; plugins
add entries through the P13 provider registry (``load_plugin_entries``).

A platform's SDK is imported in exactly one place, ``PlatformRegistry.create_adapter``, which the
gateway calls only for enabled platforms: importing this module, listing entries or probing
dependencies (``importlib.util.find_spec``) never imports an SDK.

``proxy_for`` decides which proxy an adapter uses: a per-platform proxy setting always wins;
otherwise the standard proxy env vars, unless ``gateway.trust_env`` is false.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal

__all__ = [
    "BUILTIN_NAMES",
    "PlatformEntry",
    "PlatformRegistry",
    "TargetParser",
    "builtin_entries",
    "modules_installed",
    "policy_env_name",
    "proxy_for",
]

log = logging.getLogger(__name__)

type TargetParser = Callable[[str], tuple[str, str | None] | None]
type AdapterFactory = Callable[[Any], Any]

BUILTIN_NAMES: tuple[str, ...] = (
    "telegram",
    "discord",
    "slack",
    "signal",
    "matrix",
    "mattermost",
    "email",
    "whatsapp",
    "whatsapp_cloud",
    "sms",
    "line",
    "ntfy",
    "irc",
    "teams",
    "google_chat",
    "qqbot",
    "dingtalk",
    "wecom",
    "wecom_callback",
    "weixin",
    "feishu",
    "bluebubbles",
    "photon",
    "simplex",
    "buzz",
    "yuanbao",
    "raft",
    "api_server",
    "webhook",
    "msgraph",
    "a2a",
)


def policy_env_name(platform: str, suffix: str) -> str:
    """``INDUS_<PLATFORM>_<SUFFIX>``: the env name of one of a platform's policy settings."""
    return f"INDUS_{platform.upper()}_{suffix}"


def modules_installed(*names: str) -> Callable[[], bool]:
    """A passive dependency probe: true when every top-level module can be found.

    ``find_spec`` on a top-level name locates the package without running it, so the probe is safe
    from status screens and the enablement pass.
    """

    def probe() -> bool:
        for name in names:
            try:
                if importlib.util.find_spec(name) is None:
                    return False
            except (ImportError, ValueError):
                return False
        return True

    return probe


def _lazy_factory(target: str) -> AdapterFactory:
    """A factory that imports ``module:attr`` only when an adapter is actually built."""
    module_name, _, attr = target.partition(":")

    def build(config: Any) -> Any:
        module = importlib.import_module(module_name)
        return getattr(module, attr)(config)

    build.__qualname__ = f"lazy({target})"
    return build


@dataclass(frozen=True)
class PlatformEntry:
    """One platform the gateway can run."""

    name: str
    label: str
    factory: AdapterFactory
    check_fn: Callable[[], bool]
    required_env: tuple[str, ...] = ()
    install_hint: str = ""
    allowlist_env: str = ""
    allow_all_env: str = ""
    allow_bots_env: str | None = None
    home_env: str = ""
    max_len: int = 0
    dm_is_private: bool = True
    platform_hint: str = ""
    cron_target_parser: TargetParser | None = None
    # The env names whose values, joined in this order, key the daemon's token lock (R06.16). Empty:
    # the platform's credential values (``BUILTIN_CREDENTIALS``). Set it when the unique identity
    # is not the secret (IRC: server + nick) or is not only the secret (email: address + IMAP host,
    # never the password).
    lock_env: tuple[str, ...] = ()
    # Group allowlists read from env (empty: config only). Authorization reads them (P15-08).
    group_users_env: str = ""
    group_chats_env: str = ""
    # Inbound events are authenticated by the platform's own check (signature, key), so user
    # allowlists and pairing do not apply. Never for a platform where people send messages.
    trusted_inbound: bool = False
    source: Literal["builtin", "plugin"] = "builtin"
    plugin_name: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict[str, Any])


def _chat_entry(
    name: str,
    label: str,
    *,
    sdk: tuple[str, ...],
    extra_name: str,
    required_env: tuple[str, ...],
    max_len: int,
    hint: str,
) -> PlatformEntry:
    return PlatformEntry(
        name=name,
        label=label,
        factory=_lazy_factory(f"indus.gateway.adapters.{name}:create_adapter"),
        check_fn=modules_installed(*sdk),
        required_env=required_env,
        install_hint=f"pip install 'indus-agent[{extra_name}]'",
        allowlist_env=policy_env_name(name, "ALLOWED_USERS"),
        allow_all_env=policy_env_name(name, "ALLOW_ALL_USERS"),
        allow_bots_env=policy_env_name(name, "ALLOW_BOTS"),
        home_env=policy_env_name(name, "HOME_CHANNEL"),
        max_len=max_len,
        dm_is_private=True,
        platform_hint=hint,
    )


def builtin_entries() -> tuple[PlatformEntry, ...]:
    """The platforms that ship with Indus Agent, in a fixed order."""
    # The package's __init__ holds only names (stdlib imports); the adapter itself stays lazy.
    from indus.gateway.adapters import photon
    from indus.gateway.adapters.a2a import A2A_ENTRY

    return (
        replace(
            _chat_entry(
                "telegram",
                "Telegram",
                # Our own Bot API client over httpx (a base dependency): no extra to install.
                sdk=("httpx",),
                extra_name="telegram",
                required_env=("TELEGRAM_BOT_TOKEN",),
                max_len=4096,
                hint=(
                    "You are talking through Telegram. Keep replies short enough to read on a "
                    "phone; Markdown is converted for you."
                ),
            ),
            install_hint="pip install indus-agent (Telegram needs no extra)",
        ),
        _chat_entry(
            "discord",
            "Discord",
            sdk=("discord",),
            extra_name="discord",
            required_env=("DISCORD_BOT_TOKEN",),
            max_len=2000,
            hint="You are talking through Discord. Standard Markdown renders natively.",
        ),
        _chat_entry(
            "slack",
            "Slack",
            sdk=("slack_bolt", "slack_sdk"),
            extra_name="slack",
            required_env=("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"),
            max_len=39000,
            hint="You are talking through Slack. Formatting is converted to Slack's markup.",
        ),
        replace(
            _chat_entry(
                "signal",
                "Signal",
                # Our own client for a signal-cli daemon the user runs (GPL-3.0, never bundled).
                sdk=("httpx",),
                extra_name="signal",
                required_env=("SIGNAL_HTTP_URL", "SIGNAL_ACCOUNT"),
                max_len=8000,
                hint=(
                    "You are talking through Signal. Formatting is not rendered, so write plain "
                    "text."
                ),
            ),
            install_hint=(
                "install signal-cli and run `signal-cli --account <number> daemon --http "
                "127.0.0.1:8080` (no Python extra)"
            ),
            group_chats_env=policy_env_name("signal", "GROUP_ALLOWED_CHATS"),
        ),
        replace(
            _chat_entry(
                "matrix",
                "Matrix",
                # Our own Client-Server API client over httpx (end-to-end encryption, P16-07, is
                # the optional Linux-only ``matrix-e2ee`` extra).
                sdk=("httpx",),
                extra_name="matrix",
                required_env=("MATRIX_ACCESS_TOKEN", "MATRIX_HOMESERVER"),
                max_len=15000,
                hint="You are talking through Matrix. Markdown is rendered as HTML for you.",
            ),
            install_hint=(
                "pip install indus-agent (Matrix needs no extra; end-to-end encryption: "
                "pip install 'indus-agent[matrix-e2ee]' on Linux)"
            ),
        ),
        _chat_entry(
            "mattermost",
            "Mattermost",
            # REST runs on httpx; the event socket needs httpx-ws (the ``mattermost`` extra).
            sdk=("httpx_ws",),
            extra_name="mattermost",
            required_env=("MATTERMOST_TOKEN", "MATTERMOST_URL"),
            max_len=4000,
            hint="You are talking through Mattermost. Standard Markdown renders natively.",
        ),
        replace(
            _chat_entry(
                "email",
                "Email",
                # IMAP and SMTP from the standard library.
                sdk=("imaplib", "smtplib"),
                extra_name="email",
                required_env=(
                    "EMAIL_ADDRESS",
                    "EMAIL_PASSWORD",
                    "EMAIL_IMAP_HOST",
                    "EMAIL_SMTP_HOST",
                ),
                max_len=50000,
                hint=(
                    "You are answering email. Write a complete, self-contained reply in plain "
                    "text; formatting is not rendered."
                ),
            ),
            install_hint="pip install indus-agent (email needs no extra)",
            # One mailbox on one server belongs to one gateway; a new password is not a new box.
            lock_env=("EMAIL_ADDRESS", "EMAIL_IMAP_HOST"),
        ),
        PlatformEntry(
            name="whatsapp",
            label="WhatsApp",
            # Our own Node bridge (apps/whatsapp-bridge) on loopback, driven over httpx; the
            # WhatsApp Web provider is operator-supplied (none ships). No credential: the
            # platform turns on with platforms.whatsapp.enabled: true.
            factory=_lazy_factory("indus.gateway.adapters.whatsapp:create_adapter"),
            check_fn=modules_installed("httpx"),
            install_hint=(
                "install Node.js 24 and a WhatsApp Web provider module (exports createWaSocket; "
                "platforms.whatsapp.provider), then set platforms.whatsapp.enabled: true"
            ),
            allowlist_env=policy_env_name("whatsapp", "ALLOWED_USERS"),
            allow_all_env=policy_env_name("whatsapp", "ALLOW_ALL_USERS"),
            allow_bots_env=policy_env_name("whatsapp", "ALLOW_BOTS"),
            home_env=policy_env_name("whatsapp", "HOME_CHANNEL"),
            max_len=4096,
            dm_is_private=True,
            platform_hint=(
                "You are talking through WhatsApp. Markdown is converted to WhatsApp formatting; "
                "avoid tables."
            ),
        ),
        replace(
            _chat_entry(
                "whatsapp_cloud",
                "WhatsApp Business (Cloud API)",
                # Our own Graph API client over httpx; the webhook runs on the P16-01 kit.
                sdk=("httpx",),
                extra_name="whatsapp",
                required_env=("WHATSAPP_CLOUD_ACCESS_TOKEN", "WHATSAPP_CLOUD_PHONE_NUMBER_ID"),
                max_len=4096,
                hint=(
                    "You are talking through WhatsApp. Markdown is converted to WhatsApp "
                    "formatting; avoid tables. A reply more than 24 hours after the person's last "
                    "message cannot be delivered."
                ),
            ),
            install_hint="pip install indus-agent (WhatsApp Cloud needs no extra)",
        ),
        replace(
            _chat_entry(
                "sms",
                "SMS (Twilio)",
                # Our own Twilio REST client over httpx; the webhook runs on the P16-01 kit.
                sdk=("httpx",),
                extra_name="sms",
                required_env=("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER"),
                max_len=1600,
                hint=(
                    "You are talking through SMS. Use plain text only and keep replies short "
                    "(about 1,600 characters per message)."
                ),
            ),
            install_hint="pip install indus-agent (SMS needs no extra)",
        ),
        replace(
            _chat_entry(
                "line",
                "LINE",
                # Our own Messaging API client over httpx; the webhook runs on the P16-01 kit.
                sdk=("httpx",),
                extra_name="line",
                required_env=("LINE_CHANNEL_ACCESS_TOKEN", "LINE_CHANNEL_SECRET"),
                max_len=20000,
                hint=(
                    "You are talking through LINE. Replies are plain text (Markdown is removed); "
                    "keep them short."
                ),
            ),
            install_hint="pip install indus-agent (LINE needs no extra)",
        ),
        replace(
            _chat_entry(
                "ntfy",
                "ntfy",
                # Our own client over httpx (JSON stream in, POST out).
                sdk=("httpx",),
                extra_name="ntfy",
                required_env=("NTFY_TOPIC",),
                max_len=4096,
                hint=(
                    "You are talking through ntfy push notifications: plain text, at most 4096 "
                    "bytes per message; anyone who knows the topic can read it."
                ),
            ),
            install_hint="pip install indus-agent (ntfy needs no extra)",
            # Every topic reports "dm" but anyone who knows its name reads it (R06.24).
            dm_is_private=False,
            # Two gateways never answer one topic list; the topic is not masked as a secret.
            lock_env=("NTFY_TOPIC",),
        ),
        replace(
            _chat_entry(
                "irc",
                "IRC",
                # asyncio streams and the standard ``ssl`` module: no library.
                sdk=("ssl",),
                extra_name="irc",
                required_env=("IRC_SERVER", "IRC_NICKNAME"),
                max_len=450,
                hint=(
                    "You are chatting on IRC: plain text only, short lines; people address you by "
                    "your nick."
                ),
            ),
            install_hint="pip install indus-agent (IRC needs no extra)",
            # One nick on one network belongs to one gateway (R06.16); neither is a secret.
            lock_env=("IRC_SERVER", "IRC_NICKNAME"),
        ),
        replace(
            _chat_entry(
                "teams",
                "Microsoft Teams",
                # Our own Bot Connector client over httpx; the activity endpoint runs on the
                # P16-01 kit; the token check uses cryptography (a base-install dependency).
                sdk=("httpx",),
                extra_name="teams",
                required_env=("TEAMS_CLIENT_ID", "TEAMS_CLIENT_SECRET", "TEAMS_TENANT_ID"),
                max_len=28000,
                hint=(
                    "You are talking through Microsoft Teams. Markdown is rendered (bold, italic, "
                    "code, lists); avoid tables and raw HTML. Keep replies clear and professional."
                ),
            ),
            install_hint="pip install indus-agent (Teams needs no extra)",
            # The bot is its app registration in its tenant; a new client secret is the same bot.
            lock_env=("TEAMS_CLIENT_ID", "TEAMS_TENANT_ID"),
        ),
        _chat_entry(
            "google_chat",
            "Google Chat",
            # Chat and Pub/Sub REST over httpx; RS256 through cryptography (the extra names it).
            sdk=("cryptography",),
            extra_name="google-chat",
            required_env=("GOOGLE_CHAT_SERVICE_ACCOUNT_JSON",),
            max_len=4000,
            hint=(
                "You are talking through Google Chat. *bold*, _italic_, ~strike~ and code render; "
                "headings and tables do not."
            ),
        ),
        replace(
            _chat_entry(
                "qqbot",
                "QQ Bot",
                # REST over httpx; the gateway socket needs httpx-ws (the ``qqbot`` extra).
                sdk=("httpx_ws",),
                extra_name="qqbot",
                required_env=("QQ_APP_ID", "QQ_CLIENT_SECRET"),
                max_len=4000,
                hint="You are talking through QQ. Formatting is not rendered, so write plain text.",
            ),
            # One QQ bot (app id) answers in one gateway; a new client secret is the same bot.
            lock_env=("QQ_APP_ID",),
        ),
        replace(
            _chat_entry(
                "dingtalk",
                "DingTalk",
                # REST over httpx; the Stream Mode socket needs httpx-ws (the ``dingtalk`` extra).
                sdk=("httpx_ws",),
                extra_name="dingtalk",
                required_env=("DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET"),
                max_len=20000,
                hint=(
                    "You are talking through DingTalk. Markdown is rendered (headings, bold, "
                    "lists, links); avoid tables and raw HTML."
                ),
            ),
            # The app key identifies the robot; a new app secret is the same robot.
            lock_env=("DINGTALK_CLIENT_ID",),
        ),
        replace(
            _chat_entry(
                "wecom",
                "WeCom",
                # The smart-robot socket needs httpx-ws (the ``wecom`` extra).
                sdk=("httpx_ws",),
                extra_name="wecom",
                required_env=("WECOM_BOT_ID", "WECOM_SECRET"),
                max_len=4000,
                hint=(
                    "You are talking through WeCom (WeChat Work). Markdown is rendered (bold, "
                    "lists, links, code); avoid tables. Keep replies short."
                ),
            ),
            # The bot id identifies the robot; a new secret is the same robot.
            lock_env=("WECOM_BOT_ID",),
        ),
        replace(
            _chat_entry(
                "wecom_callback",
                "WeCom (callback app)",
                # AES for the callback crypto through cryptography (the ``wecom`` extra).
                sdk=("cryptography",),
                extra_name="wecom",
                required_env=(
                    "WECOM_CALLBACK_CORP_ID",
                    "WECOM_CALLBACK_CORP_SECRET",
                    "WECOM_CALLBACK_AGENT_ID",
                    "WECOM_CALLBACK_TOKEN",
                    "WECOM_CALLBACK_ENCODING_AES_KEY",
                ),
                max_len=2048,
                hint=(
                    "You are talking through a WeCom (WeChat Work) app. Formatting is not "
                    "rendered, so write plain text, and keep each message short."
                ),
            ),
            factory=_lazy_factory("indus.gateway.adapters.wecom:create_callback_adapter"),
            # One app (agent) of one company answers in one gateway; secrets may rotate.
            lock_env=("WECOM_CALLBACK_CORP_ID", "WECOM_CALLBACK_AGENT_ID"),
        ),
        replace(
            _chat_entry(
                "weixin",
                "Weixin / WeChat",
                # Our own iLink bot client over httpx; AES-128-ECB media through cryptography (the
                # ``weixin`` extra).
                sdk=("cryptography",),
                extra_name="weixin",
                required_env=("WEIXIN_TOKEN", "WEIXIN_ACCOUNT_ID"),
                max_len=2000,
                hint=(
                    "You are talking through Weixin (WeChat). Markdown is shown as typed, so keep "
                    "formatting light and replies short."
                ),
            ),
            # The account is the bot; a new token for the same account is the same bot.
            lock_env=("WEIXIN_ACCOUNT_ID",),
        ),
        replace(
            _chat_entry(
                "feishu",
                "Feishu / Lark",
                # Our own Open API client over httpx; the long connection needs httpx-ws (the
                # ``feishu`` extra), webhook mode runs on the P16-01 kit, AES via cryptography.
                sdk=("httpx",),
                extra_name="feishu",
                required_env=("FEISHU_APP_ID", "FEISHU_APP_SECRET"),
                max_len=8000,
                hint=(
                    "You are talking through Feishu / Lark. Markdown is sent as rich text; images "
                    "and files can be sent."
                ),
            ),
            install_hint=(
                "pip install 'indus-agent[feishu]' (the long connection; webhook mode and sending "
                "need no extra)"
            ),
            # The app id is the bot; a new app secret is the same bot.
            lock_env=("FEISHU_APP_ID",),
        ),
        replace(
            _chat_entry(
                "bluebubbles",
                "BlueBubbles (iMessage)",
                # Our own client for a BlueBubbles server the user runs on a Mac, over httpx; the
                # webhook runs on the P16-01 kit.
                sdk=("httpx",),
                extra_name="bluebubbles",
                required_env=("BLUEBUBBLES_SERVER_URL", "BLUEBUBBLES_PASSWORD"),
                max_len=4000,
                hint=(
                    "You are talking through iMessage. Formatting is not rendered, so write plain "
                    "text; separate paragraphs arrive as separate bubbles."
                ),
            ),
            install_hint=(
                "pip install indus-agent (BlueBubbles needs no extra); run BlueBubbles Server on "
                "a Mac"
            ),
            # One BlueBubbles server is one iMessage account; a new password is the same server.
            lock_env=("BLUEBUBBLES_SERVER_URL",),
        ),
        PlatformEntry(
            name="photon",
            label="iMessage (Photon)",
            # Our own Node sidecar (apps/photon-sidecar) on loopback, driven over httpx; the
            # Spectrum provider is operator-supplied (none ships), so the platform ships disabled:
            # no credential table row, it turns on only with platforms.photon.enabled: true.
            factory=_lazy_factory("indus.gateway.adapters.photon:create_adapter"),
            check_fn=modules_installed("httpx"),
            required_env=photon.CREDENTIAL_ENV,
            install_hint=(
                "install Node.js 24 and a Photon Spectrum provider module (exports "
                "createSpectrumLink; platforms.photon.provider), then set "
                "platforms.photon.enabled: true"
            ),
            allowlist_env=policy_env_name("photon", "ALLOWED_USERS"),
            allow_all_env=policy_env_name("photon", "ALLOW_ALL_USERS"),
            allow_bots_env=policy_env_name("photon", "ALLOW_BOTS"),
            home_env=policy_env_name("photon", "HOME_CHANNEL"),
            max_len=8000,
            dm_is_private=True,
            platform_hint=photon.PLATFORM_HINT,
            # One Photon project answers in one gateway; a new project secret is the same project.
            lock_env=photon.LOCK_ENV,
        ),
        replace(
            _chat_entry(
                "simplex",
                "SimpleX Chat",
                # A simplex-chat daemon the user runs (AGPL-3.0, never bundled); its WebSocket
                # needs httpx-ws (the ``simplex`` extra).
                sdk=("httpx_ws",),
                extra_name="simplex",
                required_env=("SIMPLEX_WS_URL",),
                max_len=8000,
                hint=(
                    "You are talking through SimpleX Chat. Bold, italic, code and links render; "
                    "headings and tables do not."
                ),
            ),
            install_hint="pip install 'indus-agent[simplex]' and run `simplex-chat -p 5225`",
            group_chats_env=policy_env_name("simplex", "GROUP_ALLOWED_CHATS"),
            # One daemon (one SimpleX profile) answers in one gateway; the URL is not a secret.
            lock_env=("SIMPLEX_WS_URL",),
        ),
        _chat_entry(
            "buzz",
            "Buzz",
            # Nostr over httpx-ws, BIP-340 signatures through coincurve (both the ``buzz`` extra).
            sdk=("coincurve", "httpx_ws"),
            extra_name="buzz",
            required_env=("BUZZ_RELAY_URL", "BUZZ_PRIVATE_KEY"),
            max_len=16000,
            hint=(
                "You are talking through Buzz, a Nostr community relay. Markdown renders in most "
                "clients; to address someone, write @ and their name."
            ),
        ),
        replace(
            _chat_entry(
                "yuanbao",
                "Yuanbao",
                # HTTP on httpx; the bot socket needs httpx-ws (the ``yuanbao`` extra).
                sdk=("httpx_ws",),
                extra_name="yuanbao",
                required_env=("YUANBAO_APP_ID", "YUANBAO_APP_SECRET"),
                max_len=4000,
                hint=(
                    "You are talking through Tencent Yuanbao. Markdown renders (code blocks, "
                    "tables, bold). To send a sticker, find one with yuanbao_search_sticker and "
                    "send it with yuanbao_send_sticker; a plain emoji is not a sticker."
                ),
            ),
            # One bot (App ID) answers in one gateway; a new App Secret is the same bot.
            lock_env=("YUANBAO_APP_ID",),
        ),
        PlatformEntry(
            name="raft",
            label="Raft",
            factory=_lazy_factory("indus.gateway.adapters.raft:create_adapter"),
            # The wake endpoint runs on the P16-01 listener kit (uvicorn, a base dependency).
            check_fn=modules_installed("uvicorn"),
            required_env=("RAFT_PROFILE",),
            install_hint=(
                "pip install indus-agent (Raft needs no extra); install the Raft CLI and run "
                "`raft agent bridge` against the URL in <profile home>/platforms/raft/bridge.json"
            ),
            # Every chat reads as a DM but is the bridge, never a person (R06.24).
            dm_is_private=False,
            platform_hint=(
                "You are a Raft external agent. Wake notices only say that Raft messages may be "
                "waiting: read and answer them with the raft command-line tool, passing --profile "
                "with your Raft profile on every call. Your replies in this chat go nowhere."
            ),
            # One Raft agent profile is served by one gateway.
            lock_env=("RAFT_PROFILE",),
        ),
        PlatformEntry(
            name="api_server",
            label="API server",
            factory=_lazy_factory("indus.gateway.http.api_server:create_adapter"),
            check_fn=modules_installed("fastapi", "uvicorn"),
            install_hint="pip install 'indus-agent[gateway]'",
            dm_is_private=False,
            trusted_inbound=True,
        ),
        PlatformEntry(
            name="webhook",
            label="Webhook receiver",
            factory=_lazy_factory("indus.gateway.http.webhook:create_adapter"),
            check_fn=modules_installed("fastapi", "uvicorn"),
            install_hint="pip install 'indus-agent[gateway]'",
            dm_is_private=False,
            trusted_inbound=True,
        ),
        PlatformEntry(
            name="msgraph",
            label="Microsoft Graph change notifications",
            factory=_lazy_factory("indus.gateway.adapters.msgraph:create_adapter"),
            check_fn=modules_installed("httpx"),
            # The clientState secret every notification must carry (16-128 printable characters).
            required_env=("MSGRAPH_WEBHOOK_CLIENT_STATE",),
            install_hint="pip install indus-agent (no extra)",
            # Notifications become internal events, never chat; the clientState authenticates them.
            dm_is_private=False,
            trusted_inbound=True,
        ),
        # P24-10: peer agents over A2A; no credential, on with platforms.a2a.enabled.
        A2A_ENTRY,
    )


def _probe(entry: PlatformEntry) -> bool:
    try:
        return entry.check_fn() is True
    except Exception as exc:  # a probe must never take the gateway down
        log.warning("platform %s: dependency check failed: %s", entry.name, exc)
        return False


class PlatformRegistry:
    """Name → ``PlatformEntry``. Thread-safe; built-ins first, plugin entries on top."""

    def __init__(self, builtins: Iterable[PlatformEntry] | None = None) -> None:
        self._lock = threading.RLock()
        self._entries: dict[str, PlatformEntry] = {}
        self._builtin_names: frozenset[str] = frozenset()
        start = tuple(builtin_entries() if builtins is None else builtins)
        for entry in start:
            self._entries[entry.name] = entry
        self._builtin_names = frozenset(e.name for e in start if e.source == "builtin")

    # -- lookup ---------------------------------------------------------------------------------

    def get(self, name: str) -> PlatformEntry | None:
        with self._lock:
            return self._entries.get(name.strip().lower())

    def names(self) -> list[str]:
        with self._lock:
            return list(self._entries)

    def entries(self) -> list[PlatformEntry]:
        with self._lock:
            return list(self._entries.values())

    def is_builtin(self, name: str) -> bool:
        return name.strip().lower() in self._builtin_names

    def missing_deps(self, name: str) -> str | None:
        """The install hint when the platform's library is missing, else None."""
        entry = self.get(name)
        if entry is None:
            return f"unknown platform {name!r}"
        if _probe(entry):
            return None
        return entry.install_hint or f"the {entry.label} library is not installed"

    # -- changes --------------------------------------------------------------------------------

    def register(self, entry: PlatformEntry, *, replace_existing: bool = False) -> bool:
        """Add ``entry``; an existing name is replaced only with ``replace_existing``."""
        name = entry.name.strip().lower()
        if not name or not name.replace("_", "").isalnum():
            raise ValueError(f"platform name {entry.name!r} must be letters, digits and _")
        if entry.trusted_inbound and entry.source == "plugin" and name in self._builtin_names:
            # People send messages on a shipped chat platform; a plugin replacing its adapter must
            # not switch off allowlists and pairing for it.
            log.warning(
                "plugin %s: platform %s cannot be marked trusted_inbound; flag dropped",
                entry.plugin_name or "?",
                name,
            )
            entry = replace(entry, trusted_inbound=False)
        if name != entry.name:
            entry = replace(entry, name=name)
        with self._lock:
            if name in self._entries and not replace_existing:
                log.warning("platform %s is already registered; refused", name)
                return False
            self._entries[name] = entry
        return True

    def unregister(self, name: str) -> bool:
        with self._lock:
            return self._entries.pop(name.strip().lower(), None) is not None

    def load_plugin_entries(self, providers: Mapping[str, Mapping[str, Any]]) -> int:
        """Register the platform entries plugins offered (the P13 provider slot ``platform``).

        A plugin entry may replace a built-in adapter (user beats bundled, P13-14). Returns how
        many entries were registered.
        """
        slot = providers.get("platform") or {}
        added = 0
        for label, offered in slot.items():
            provider = getattr(offered, "provider", offered)
            # A P13 ``ProviderEntry``: the offering plugin's id is its ``plugin`` field.
            plugin_id = str(getattr(offered, "plugin", "") or "")
            if not isinstance(provider, PlatformEntry):
                log.warning(
                    "plugin %s: platform %s is not a PlatformEntry; ignored", plugin_id, label
                )
                continue
            entry = replace(
                provider, source="plugin", plugin_name=plugin_id or provider.plugin_name
            )
            if self.register(entry, replace_existing=True):
                added += 1
        return added

    # -- building -------------------------------------------------------------------------------

    def create_adapter(self, name: str, config: Any) -> Any | None:
        """Build the adapter for an enabled platform: the only place an SDK gets imported.

        Returns None (and logs why) for an unknown platform, a missing library or a factory error.
        """
        entry = self.get(name)
        if entry is None:
            log.warning("no platform named %s is registered", name)
            return None
        if not _probe(entry):
            log.warning(
                "platform %s is enabled but its library is missing: %s",
                entry.name,
                entry.install_hint or "install its extra",
            )
            return None
        try:
            return entry.factory(config)
        except Exception as exc:
            log.error("platform %s: building the adapter failed: %s", entry.name, exc)
            return None


# -- proxy ----------------------------------------------------------------------------------------

_PROXY_NAMES = {
    "https": ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"),
    "http": ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"),
}


def _bypassed(host: str, no_proxy: str) -> bool:
    host = host.strip().lower().rstrip(".")
    if not host:
        return False
    for raw in no_proxy.split(","):
        item = raw.strip().lower()
        if not item:
            continue
        if item == "*":
            return True
        item = item.split(":", 1)[0].lstrip(".").rstrip(".")
        if item and (host == item or host.endswith("." + item)):
            return True
    return False


def proxy_for(
    *,
    env: Mapping[str, str],
    trust_env: bool = True,
    platform_proxy: str | None = None,
    target_host: str | None = None,
    scheme: Literal["https", "http"] = "https",
) -> str | None:
    """The proxy URL an adapter should use, or None for a direct connection.

    A per-platform proxy setting always wins. Otherwise the standard env vars apply unless
    ``trust_env`` is false; ``NO_PROXY`` exempts ``target_host``.
    """
    explicit = (platform_proxy or "").strip()
    if explicit:
        return explicit
    if not trust_env:
        return None
    no_proxy = env.get("NO_PROXY") or env.get("no_proxy") or ""
    if target_host and _bypassed(target_host, no_proxy):
        return None
    for name in _PROXY_NAMES[scheme]:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return None
