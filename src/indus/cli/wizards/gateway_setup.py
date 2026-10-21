"""``indus gateway setup [PLATFORM]``: set up one messaging platform step by step (P16-31).

Without a platform a menu lists every registered platform with its state until "Done". For one
platform the wizard:

- prints what to create on the vendor's side (Slack: an app manifest to paste);
- checks the platform's library through its registry probe (``check_fn``) and prints the install
  hint when it is missing;
- asks each value the adapter needs. Secrets are typed masked and stored in the profile's secret
  store (the OS keychain or its owner-only file), never in ``.env`` or ``config.yaml`` and never
  printed; a value already stored is kept unless the person asks to replace it. Plain settings
  (server URLs, hosts, the public webhook URL) go to ``platforms.<name>`` through the config writer;
- saves nothing when a required value is missing, and nothing when the bot those values make (what
  keys the gateway's token lock) already belongs to another profile on this machine: one bot serves
  one profile, so a cloned profile needs its own bot (R04.26, R06.16);
- asks who may talk to the bot and what unknown senders get, then prints the mode the gateway will
  apply and why (its own authorizer: pair, ignore or decline);
- offers a home channel, checked with the platform's target rule (P16-32);
- runs a connection check: the adapter is built from the registry the way ``indus send`` builds it
  (send-only), connected and closed. Telegram then prints the link that opens a chat with the bot.

Without a terminal nothing is asked: the secret names and config keys to set are printed and the
command exits 2 (R04.19). The CLI and the gateway are sibling layers (D-16.246), so the gateway's
registry, settings, authorizer, adapter builder and target rules are reached by module name when the
wizard runs, as ``indus gateway run`` and ``indus send`` are wired.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import importlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

import click

from indus.cli.prompts import (
    Cancelled,
    Interactive,
    ask_choice,
    ask_secret,
    ask_text,
    ask_yes_no,
    io_for,
)

if TYPE_CHECKING:
    from indus.cli.args import CliState
    from indus.foundation.scope import Scope
    from indus.foundation.secrets import SecretStore

__all__ = [
    "MENU_DONE",
    "STEPS",
    "Ask",
    "CheckResult",
    "GatewayPort",
    "GatewaySetupDeps",
    "LiveGateway",
    "PlatformInfo",
    "PlatformSteps",
    "ProfileIdentities",
    "non_tty_guidance",
    "run_gateway_setup",
    "setup_command",
    "slack_manifest",
]

CANCELLED = "Gateway setup cancelled. Platforms finished before it keep their settings."
MENU_DONE = "Done"
CHECK_TIMEOUT_S = 20.0
OK, CANCEL_EXIT, USAGE = 0, 1, 2

type AskKind = Literal["secret", "public", "setting"]


# -- what each platform asks ----------------------------------------------------------------------


@dataclass(frozen=True)
class Ask:
    """One value a platform needs.

    ``secret``: a vendor credential, typed masked, kept in the secret store. ``public``: a vendor
    env name that turns the platform on but is not a secret (an IRC server, an ntfy topic): kept in
    the secret store too, because that is where the gateway reads it, but shown. ``setting``: a key
    under ``platforms.<name>`` in ``config.yaml``.
    """

    name: str
    question: str
    kind: AskKind = "secret"
    required: bool = True
    default: str = ""


@dataclass(frozen=True)
class PlatformSteps:
    intro: tuple[str, ...] = ()
    asks: tuple[Ask, ...] = ()
    access: bool = True
    home: bool = True
    # A platform set up elsewhere: the wizard prints this and asks nothing.
    elsewhere: str = ""
    # A platform its credentials do not turn on (shipped disabled, P16-25): the wizard sets
    # ``platforms.<name>.enabled: true`` once every value is saved.
    enable: bool = False


def _secret(name: str, question: str, *, required: bool = True) -> Ask:
    return Ask(name, question, "secret", required)


def _public(name: str, question: str, *, required: bool = True, default: str = "") -> Ask:
    return Ask(name, question, "public", required, default)


def _setting(key: str, question: str, *, required: bool = True, default: str = "") -> Ask:
    return Ask(key, question, "setting", required, default)


_PUBLIC_URL = _setting(
    "webhook_public_url",
    "Public HTTPS URL the platform calls (the gateway listens on 127.0.0.1; put a proxy or tunnel "
    "in front)",
    required=False,
)

STEPS: Mapping[str, PlatformSteps] = {
    "telegram": PlatformSteps(
        ("Create a bot by talking to @BotFather in Telegram (/newbot) and copy its token.",),
        (_secret("TELEGRAM_BOT_TOKEN", "Bot token"),),
    ),
    "discord": PlatformSteps(
        (
            "Create an application at the Discord developer portal, add a bot, turn on the "
            "Message Content intent and copy the bot token.",
        ),
        (_secret("DISCORD_BOT_TOKEN", "Bot token"),),
    ),
    "slack": PlatformSteps(
        (
            "Create a Slack app from the manifest below (api.slack.com/apps → Create New App → "
            "From a manifest), install it to your workspace, then copy the bot token (xoxb-…) and "
            "an app-level token with connections:write (xapp-…).",
        ),
        (
            _secret("SLACK_BOT_TOKEN", "Bot token (xoxb-…)"),
            _secret("SLACK_APP_TOKEN", "App-level token (xapp-…)"),
        ),
    ),
    "signal": PlatformSteps(
        (
            "Signal runs through a signal-cli daemon you start yourself: "
            "signal-cli --account <number> daemon --http 127.0.0.1:8080",
        ),
        (
            _setting("http_url", "signal-cli HTTP address", default="http://127.0.0.1:8080"),
            _secret("SIGNAL_ACCOUNT", "The bot's phone number (+ and country code)"),
        ),
    ),
    "matrix": PlatformSteps(
        ("Create a user for the bot on your homeserver and copy an access token for it.",),
        (
            _setting("homeserver", "Homeserver URL (https://…)"),
            _secret("MATRIX_ACCESS_TOKEN", "Access token"),
            _setting("user_id", "The bot's user id (@bot:example.org)", required=False),
        ),
    ),
    "mattermost": PlatformSteps(
        ("Create a bot account (Integrations → Bot Accounts) and copy its token.",),
        (
            _setting("url", "Server URL (https://…)"),
            _secret("MATTERMOST_TOKEN", "Bot token"),
        ),
    ),
    "email": PlatformSteps(
        ("Use a mailbox of its own for the agent; an app password is safer than the main one.",),
        (
            _secret("EMAIL_ADDRESS", "The agent's email address"),
            _secret("EMAIL_PASSWORD", "Its password or app password"),
            _setting("imap_host", "IMAP server (e.g. imap.example.com)"),
            _setting("smtp_host", "SMTP server (e.g. smtp.example.com)"),
        ),
    ),
    "whatsapp": PlatformSteps(
        elsewhere="WhatsApp runs through the local bridge (Node.js 24) with a WhatsApp Web "
        "provider you supply: set platforms.whatsapp.provider to a module that exports "
        "createWaSocket and platforms.whatsapp.enabled to true, then start the gateway. Pairing a "
        "phone from this wizard is not built yet.",
    ),
    "whatsapp_cloud": PlatformSteps(
        (
            "In Meta's developer console create a WhatsApp Business app; copy a permanent access "
            "token, the phone number id and the app secret, and choose a verify token for the "
            "webhook.",
        ),
        (
            _secret("WHATSAPP_CLOUD_ACCESS_TOKEN", "Access token"),
            _secret("WHATSAPP_CLOUD_PHONE_NUMBER_ID", "Phone number id"),
            _secret("WHATSAPP_CLOUD_APP_SECRET", "App secret"),
            _secret("WHATSAPP_CLOUD_VERIFY_TOKEN", "Webhook verify token (one you choose)"),
            _PUBLIC_URL,
        ),
    ),
    "sms": PlatformSteps(
        ("Copy the Account SID, auth token and phone number from the Twilio console.",),
        (
            _secret("TWILIO_ACCOUNT_SID", "Account SID"),
            _secret("TWILIO_AUTH_TOKEN", "Auth token"),
            _secret("TWILIO_PHONE_NUMBER", "Twilio phone number (+ and country code)"),
            _setting(
                "webhook_public_url",
                "Public URL set as the number's messaging webhook in the Twilio console",
            ),
        ),
    ),
    "line": PlatformSteps(
        ("Create a Messaging API channel in the LINE console; copy its access token and secret.",),
        (
            _secret("LINE_CHANNEL_ACCESS_TOKEN", "Channel access token"),
            _secret("LINE_CHANNEL_SECRET", "Channel secret"),
            _PUBLIC_URL,
        ),
    ),
    "ntfy": PlatformSteps(
        ("Pick a topic name that is hard to guess: anyone who knows it can read and post to it.",),
        (
            _public("NTFY_TOPIC", "Topic"),
            _setting("server", "Server", required=False, default="https://ntfy.sh"),
            _secret("NTFY_TOKEN", "Access token (blank: none)", required=False),
        ),
    ),
    "irc": PlatformSteps(
        ("TLS is used by default; passwords are only sent over TLS.",),
        (
            _public("IRC_SERVER", "Server (e.g. irc.libera.chat)"),
            _public("IRC_NICKNAME", "The bot's nick"),
            _setting("channels", "Channels to join (comma-separated)", required=False),
            _secret("IRC_SASL_PASSWORD", "SASL password (blank: none)", required=False),
        ),
    ),
    "teams": PlatformSteps(
        (
            "Register a bot in Azure (Azure Bot resource) and point its messaging endpoint at "
            "the gateway; copy the app (client) id, a client secret and the tenant id.",
        ),
        (
            _secret("TEAMS_CLIENT_ID", "App (client) id"),
            _secret("TEAMS_CLIENT_SECRET", "Client secret"),
            _secret("TEAMS_TENANT_ID", "Tenant id"),
        ),
    ),
    "google_chat": PlatformSteps(
        (
            "Create a Chat app in Google Cloud with a service account; paste the service "
            "account's key JSON on one line.",
        ),
        (
            _secret("GOOGLE_CHAT_SERVICE_ACCOUNT_JSON", "Service account key JSON"),
            _setting(
                "subscription",
                "Pub/Sub subscription (projects/…/subscriptions/…; blank: HTTP endpoint mode)",
                required=False,
            ),
        ),
    ),
    "qqbot": PlatformSteps(
        ("Create a bot on the QQ open platform and copy its app id and client secret.",),
        (_secret("QQ_APP_ID", "App id"), _secret("QQ_CLIENT_SECRET", "Client secret")),
    ),
    "dingtalk": PlatformSteps(
        ("Create a robot app (Stream Mode) in the DingTalk developer console.",),
        (
            _secret("DINGTALK_CLIENT_ID", "Client id (app key)"),
            _secret("DINGTALK_CLIENT_SECRET", "Client secret (app secret)"),
        ),
    ),
    "wecom": PlatformSteps(
        ("Create a smart robot in the WeCom admin console (API mode) and copy its id and secret.",),
        (_secret("WECOM_BOT_ID", "Bot id"), _secret("WECOM_SECRET", "Secret")),
    ),
    "wecom_callback": PlatformSteps(
        (
            "Create a self-built app in the WeCom admin console and set its callback URL, token "
            "and EncodingAESKey to the values you enter here.",
        ),
        (
            _secret("WECOM_CALLBACK_CORP_ID", "Corp id"),
            _secret("WECOM_CALLBACK_CORP_SECRET", "App secret"),
            _setting("agent_id", "Agent id"),
            _secret("WECOM_CALLBACK_TOKEN", "Callback token"),
            _secret("WECOM_CALLBACK_ENCODING_AES_KEY", "EncodingAESKey (43 characters)"),
        ),
    ),
    "weixin": PlatformSteps(
        (
            "Log the bot's Weixin account in to the iLink bot API and copy its bot token and "
            "account id (logging in by QR code from this wizard is not built yet).",
        ),
        (_secret("WEIXIN_TOKEN", "Bot token"), _secret("WEIXIN_ACCOUNT_ID", "Bot account id")),
    ),
    "feishu": PlatformSteps(
        ("Create a custom app with the bot feature in the Feishu / Lark developer console.",),
        (_secret("FEISHU_APP_ID", "App id"), _secret("FEISHU_APP_SECRET", "App secret")),
    ),
    "bluebubbles": PlatformSteps(
        ("Run BlueBubbles Server on a Mac signed in to iMessage; copy its URL and password.",),
        (
            _setting("server_url", "Server URL"),
            _secret("BLUEBUBBLES_PASSWORD", "Server password"),
        ),
    ),
    "photon": PlatformSteps(
        (
            "Photon runs iMessage lines for you: copy your Photon project's id and secret. The "
            "gateway runs its own sidecar on Node.js 24 (loopback port 8789) with a Spectrum "
            "provider module you supply (one that exports createSpectrumLink); none ships.",
        ),
        (
            _secret("PHOTON_PROJECT_ID", "Project id"),
            _secret("PHOTON_PROJECT_SECRET", "Project secret"),
            _setting("provider", "Provider module (an absolute path or a package name)"),
        ),
        enable=True,
    ),
    "simplex": PlatformSteps(
        ("Run the SimpleX Chat CLI as a local daemon: simplex-chat -p 5225",),
        (_public("SIMPLEX_WS_URL", "Daemon address", default="ws://127.0.0.1:5225"),),
    ),
    "buzz": PlatformSteps(
        ("The bot signs as its own Nostr key: paste its private key (nsec or hex).",),
        (
            _setting("relay_url", "Relay URL (wss://…)"),
            _secret("BUZZ_PRIVATE_KEY", "Private key"),
        ),
    ),
    "yuanbao": PlatformSteps(
        (
            "In the Yuanbao app create a bot (PAI → My Bot) and copy its App ID and App Secret; "
            "the gateway signs in over Yuanbao's bot socket.",
        ),
        (_secret("YUANBAO_APP_ID", "App ID"), _secret("YUANBAO_APP_SECRET", "App Secret")),
    ),
    "raft": PlatformSteps(
        (
            "Create the external agent in Raft and log the Raft CLI in as it (raft agent login). "
            "The gateway listens on 127.0.0.1 and writes the wake URL and token to "
            "platforms/raft/bridge.json in this profile's home; point `raft agent bridge` there.",
        ),
        (_public("RAFT_PROFILE", "Raft agent profile slug"),),
        access=False,
        home=False,
    ),
    "msgraph": PlatformSteps(
        (
            "Graph change notifications become internal events; choose a client state secret of "
            "16-128 characters for your subscriptions.",
        ),
        (
            _secret("MSGRAPH_WEBHOOK_CLIENT_STATE", "Client state secret"),
            _setting(
                "deliver_to",
                "Where replies go (a send target on another platform, blank: nowhere)",
                required=False,
            ),
        ),
        access=False,
        home=False,
    ),
    "api_server": PlatformSteps(
        elsewhere="The API server is turned on with gateway.api_server.enabled and its keys are "
        "made with `indus gateway api-key create`.",
    ),
    "webhook": PlatformSteps(
        elsewhere="Webhook routes are set up with `indus webhook`.",
    ),
    "a2a": PlatformSteps(
        elsewhere="A2A peer agents are set up in config.yaml: platforms.a2a.enabled, the peers "
        "allowed in under a2a.inbound_peers (each one's token is the stored secret "
        "INDUS_A2A_TOKEN_<PEER>), and the agents you call under a2a.peers.",
    ),
}
"""Platform name → what the wizard asks (a platform without a row asks its registry env names)."""


def slack_manifest(name: str = "Indus Agent") -> str:
    """A Slack app manifest for the socket-mode app the Slack adapter expects (JSON)."""
    manifest = {
        "display_information": {"name": name, "description": "Your Indus Agent in Slack"},
        "features": {
            "app_home": {"messages_tab_enabled": True, "messages_tab_read_only_enabled": False},
            "bot_user": {"display_name": name, "always_online": True},
        },
        "oauth_config": {
            "scopes": {
                "bot": [
                    "app_mentions:read",
                    "assistant:write",
                    "channels:history",
                    "channels:read",
                    "chat:write",
                    "files:read",
                    "files:write",
                    "groups:history",
                    "groups:read",
                    "im:history",
                    "im:read",
                    "im:write",
                    "mpim:history",
                    "mpim:read",
                    "users:read",
                ]
            }
        },
        "settings": {
            "event_subscriptions": {
                "bot_events": [
                    "app_mention",
                    "message.channels",
                    "message.groups",
                    "message.im",
                    "message.mpim",
                ]
            },
            "interactivity": {"is_enabled": True},
            "socket_mode_enabled": True,
        },
    }
    return json.dumps(manifest, indent=2)


def _steps_for(info: PlatformInfo) -> PlatformSteps:
    found = STEPS.get(info.name)
    if found is not None:
        return found
    # A plugin platform: its registry entry names the env values it needs; treat them as secrets.
    return PlatformSteps(asks=tuple(_secret(n, n) for n in info.required_env))


# -- what the wizard needs from the gateway -------------------------------------------------------


@dataclass(frozen=True)
class PlatformInfo:
    name: str
    label: str
    ready: bool = True
    install_hint: str = ""
    required_env: tuple[str, ...] = ()
    credentials: tuple[str, ...] = ()
    # Env names that turn the platform on without being secrets (an IRC server, an ntfy topic).
    public_env: tuple[str, ...] = ()
    lock_env: tuple[str, ...] = ()
    dm_is_private: bool = True
    trusted_inbound: bool = False


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    detail: str = ""
    # Who the platform says we are (Telegram: the bot's username), when it says.
    identity: str = ""


class GatewayPort(Protocol):
    """The gateway pieces the wizard uses (the registry, settings, authorizer, targets)."""

    def platforms(self) -> list[PlatformInfo]: ...

    def states(self, scope: Scope) -> Mapping[str, str]: ...

    def access(self, scope: Scope, name: str) -> tuple[str, str]: ...

    def check(self, scope: Scope, name: str, timeout: float) -> CheckResult: ...

    def parse_target(self, name: str, address: str) -> tuple[str, str | None]: ...


class LiveGateway:
    """``GatewayPort`` over the real gateway modules, imported by name when first used."""

    def __init__(self, registry: Any | None = None) -> None:
        self._registry_value = registry

    @staticmethod
    def _module(name: str) -> Any:
        return importlib.import_module(f"indus.gateway.{name}")

    def registry(self) -> Any:
        if self._registry_value is None:
            self._registry_value = self._module("registry").PlatformRegistry()
        return self._registry_value

    def platforms(self) -> list[PlatformInfo]:
        config = self._module("config")
        registry = self.registry()
        found: list[PlatformInfo] = []
        for entry in registry.entries():
            name = str(entry.name)
            found.append(
                PlatformInfo(
                    name=name,
                    label=str(entry.label or name),
                    ready=registry.missing_deps(name) is None,
                    install_hint=str(entry.install_hint or ""),
                    required_env=tuple(entry.required_env),
                    credentials=tuple(config.BUILTIN_CREDENTIALS.get(name, ())),
                    public_env=tuple(config.BUILTIN_PUBLIC_ENV.get(name, ())),
                    lock_env=tuple(entry.lock_env),
                    dm_is_private=bool(entry.dm_is_private),
                    trusted_inbound=bool(entry.trusted_inbound),
                )
            )
        return found

    def states(self, scope: Scope) -> Mapping[str, str]:
        """Platform → "on", "off in config" or "not set up" (one settings pass for all)."""
        settings = self._module("config").load_gateway_settings(scope)
        found: dict[str, str] = {}
        for name, platform in settings.platforms.items():
            if platform.enabled:
                found[name] = "on"
            elif platform.reason == "disabled in config":
                found[name] = "off in config"
            else:
                found[name] = "not set up"
        return found

    def access(self, scope: Scope, name: str) -> tuple[str, str]:
        config = self._module("config")
        authz = self._module("core.authz")
        settings = config.load_gateway_settings(scope)
        own = settings.get(name)
        extra: Mapping[str, Any] = own.extra if own is not None else {}
        if name == "email":
            # The mailbox adapter answers unknown senders only when told to (R06.8).
            behaviour = str(extra.get("unauthorized_dm_behavior") or "").strip().lower()
            if behaviour in ("pair", "decline"):
                return behaviour, "set by platforms.email.extra.unauthorized_dm_behavior"
            return "ignore", "email never answers unknown senders unless told to pair or decline"

        def extra_for(platform: str) -> Mapping[str, Any] | None:
            found = settings.get(platform)
            return None if found is None else found.extra

        authorizer = authz.Authorizer.from_registry(
            self.registry(),
            env=lambda n: config.read_secret(scope, n),
            extra_for=extra_for,
            global_unknown_dm=lambda: settings.gateway.unauthorized_dm_behavior,
        )
        mode = authorizer.unknown_dm_mode(name)
        return str(mode.mode), str(mode.why)

    def check(self, scope: Scope, name: str, timeout: float) -> CheckResult:
        return asyncio.run(self._check(scope, name, timeout))

    async def _check(self, scope: Scope, name: str, timeout: float) -> CheckResult:
        config = self._module("config")
        send = self._module("delivery.send_command")
        base = self._module("adapters.base")
        settings = config.load_gateway_settings(scope).get(name)
        if settings is None or not settings.enabled:
            why = settings.reason if settings is not None else "not configured"
            return CheckResult(False, f"{name} is not on ({why})")
        registry = self.registry()
        adapter = send.build_adapter(scope, name, settings, registry)
        if adapter is None:
            missing = registry.missing_deps(name)
            return CheckResult(False, missing or "the adapter could not be built")
        try:
            await asyncio.wait_for(adapter.connect(), timeout)
        except TimeoutError:
            return CheckResult(False, f"no answer within {timeout:g} s")
        except base.AdapterConnectError as exc:
            return CheckResult(False, str(exc))
        try:
            me = getattr(adapter, "me", None)
            identity = str(getattr(me, "username", "") or "")
            return CheckResult(True, "connected", identity)
        finally:
            with contextlib.suppress(Exception):
                await adapter.disconnect()

    def parse_target(self, name: str, address: str) -> tuple[str, str | None]:
        return self._module("adapters.targets").parse_target(name, address)


# -- other profiles (R04.26) ----------------------------------------------------------------------


class ProfileIdentities(Protocol):
    """Values other live profiles on this machine hold for env names (their store and .env)."""

    def values(
        self, scope: Scope, names: Sequence[str]
    ) -> Iterable[tuple[str, dict[str, str]]]: ...


class OtherProfiles:
    """Reads every other live profile's secret store and ``.env`` (never the process env)."""

    def values(self, scope: Scope, names: Sequence[str]) -> Iterable[tuple[str, dict[str, str]]]:
        from indus.foundation import profiles
        from indus.foundation.config import loader
        from indus.foundation.envfile import read_env_file
        from indus.foundation.envload import env_file_path
        from indus.foundation.secrets import open_secret_store

        for pid in profiles.list_profiles(scope.root):
            if pid == scope.profile_id:
                continue
            home = profiles.profile_home(scope.root, pid)
            found: dict[str, str] = {}
            try:
                env_file = read_env_file(env_file_path(home))
            except Exception:
                env_file = {}
            store: SecretStore | None = None
            try:
                tree = loader.read_user_tree((home / loader.CONFIG_FILE).read_bytes())
                security = tree.get("security")
                mode = "auto"
                if isinstance(security, dict):
                    raw = cast(dict[str, Any], security).get("secret_store")
                    mode = raw if raw in ("auto", "keychain", "file") else "auto"
            except Exception:
                mode = "auto"
            with contextlib.suppress(Exception):
                store = open_secret_store(home, pid, mode=mode, clock=scope.clock)
            for name in names:
                value = ""
                if store is not None:
                    with contextlib.suppress(Exception):
                        value = (store.get(name) or "").strip()
                found[name] = value or str(env_file.get(name) or "").strip()
            yield pid, found


def bot_identity(
    read: Callable[[str], str], lock_env: Sequence[str], credentials: Sequence[str]
) -> str:
    """What the gateway's token lock keys on for these values (``gateway.config.lock_key``):
    the ``lock_env`` values when any is set, else every credential (sorted by name); "" for none."""
    if lock_env:
        values = [read(n) for n in lock_env]
        if any(values):
            return "\n".join(values)
    if credentials:
        values = [read(n) for n in sorted(credentials)]
        if all(values):
            return "\n".join(values)
    return ""


# -- the run --------------------------------------------------------------------------------------


@dataclass
class GatewaySetupDeps:
    """Seams for tests: the terminal, the gateway, the other profiles, the secret store."""

    io: Interactive | None = None
    gateway: GatewayPort = field(default_factory=LiveGateway)
    others: ProfileIdentities = field(default_factory=OtherProfiles)
    store: SecretStore | None = None
    check_timeout: float = CHECK_TIMEOUT_S


@dataclass
class _Run:
    scope: Scope
    io: Interactive
    deps: GatewaySetupDeps
    store: SecretStore

    def say(self, line: str = "") -> None:
        self.io.say(line)

    @property
    def gateway(self) -> GatewayPort:
        return self.deps.gateway

    def live_scope(self) -> Scope:
        """The scope with the secret store and the settings as saved right now."""
        from indus.foundation import config

        with_store = self.scope.with_secrets(self.store)
        return with_store.with_config(config.load(with_store))

    def stored(self, name: str) -> str:
        try:
            return (self.store.get(name) or "").strip()
        except Exception:
            return ""

    def current(self, name: str) -> str:
        """``name`` as the gateway reads it: the secret store, then the environment."""
        return self.stored(name) or (self.scope.env.get(name) or "").strip()

    def where(self) -> str:
        return (
            "the OS keychain"
            if self.store.backend_name == "keychain"
            else "the profile's owner-only secret file"
        )


def _platform_settings(scope: Scope, name: str) -> dict[str, Any]:
    from indus.foundation import config

    section = config.load(scope).get(f"platforms.{name}")
    return dict(cast(Mapping[str, Any], section)) if isinstance(section, Mapping) else {}


def _platform_extra(scope: Scope, name: str) -> dict[str, Any]:
    settings = _platform_settings(scope, name)
    extra = settings.get("extra")
    merged = {k: v for k, v in settings.items() if k != "extra"}
    if isinstance(extra, Mapping):
        merged.update(cast(Mapping[str, Any], extra))
    return merged


def _save_platform(scope: Scope, name: str, values: Mapping[str, Any]) -> None:
    from indus.foundation import config

    config.save(scope, {"platforms": {name: dict(values)}})


def _text(value: object) -> str:
    if isinstance(value, list | tuple):
        items = cast(Sequence[object], value)
        return ",".join(str(v).strip() for v in items if str(v).strip())
    return str(value or "").strip()


def _mask(text: str, secrets: Iterable[str]) -> str:
    for secret in sorted({s for s in secrets if len(s) >= 4}, key=len, reverse=True):
        text = text.replace(secret, "****")
    return text


def _collect(
    run: _Run, info: PlatformInfo, asks: Sequence[Ask]
) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """Ask every value: (secret-store values, config settings, required names still missing)."""
    io = run.io
    store_values: dict[str, str] = {}
    settings: dict[str, str] = {}
    missing: list[str] = []
    current_settings = _platform_settings(run.scope, info.name)
    for ask in asks:
        if ask.kind == "secret":
            have = bool(run.current(ask.name))
            if run.stored(ask.name):
                if not ask_yes_no(io, f"{ask.name} is already stored. Replace it?", False):
                    continue
            elif have:
                run.say(
                    f"{ask.name} is set in the environment; a value stored here takes its place."
                )
            value = ask_secret(io, ask.question).strip()
            if value:
                store_values[ask.name] = value
            elif not have and ask.required:
                missing.append(ask.name)
            continue
        if ask.kind == "public":
            now = run.current(ask.name)
            value = ask_text(io, ask.question, now or ask.default or None).strip()
            if value and value != now:
                store_values[ask.name] = value
            elif not value and ask.required:
                missing.append(ask.name)
            continue
        now = _text(current_settings.get(ask.name))
        value = ask_text(io, ask.question, now or ask.default or None).strip()
        if value and value != now:
            settings[ask.name] = value
        elif not value and ask.required:
            missing.append(f"platforms.{info.name}.{ask.name}")
    return store_values, settings, missing


def _clash(run: _Run, info: PlatformInfo, pending: Mapping[str, str]) -> str | None:
    """The other profile already holding the bot these values make, or None (R04.26)."""
    names = tuple(dict.fromkeys((*info.lock_env, *info.credentials)))
    if not names or not any(n in pending for n in names):
        return None
    mine = bot_identity(lambda n: pending.get(n) or run.current(n), info.lock_env, info.credentials)
    if not mine:
        return None
    for pid, values in run.deps.others.values(run.scope, names):
        theirs = bot_identity(lambda n, v=values: v.get(n, ""), info.lock_env, info.credentials)
        if theirs and hmac.compare_digest(mine.encode(), theirs.encode()):
            return pid
    return None


_ACCESS_ROWS: tuple[tuple[str, str], ...] = (
    ("pair", "Send them a pairing code you approve with `indus pairing approve`"),
    ("ignore", "Ignore them"),
    ("decline", "Tell them once that the bot is private, then ignore them"),
)


def _allow_list(text: str) -> list[str]:
    out: list[str] = []
    for raw in text.split(","):
        item = raw.strip()
        if item.startswith("<@") and item.endswith(">"):
            item = item[2:-1].lstrip("!&")
        item = item.replace(" ", "")
        if item and item not in out:
            out.append(item)
    return out


def _access(run: _Run, info: PlatformInfo) -> None:
    io = run.io
    extra = _platform_extra(run.scope, info.name)
    now = _allow_list(_text(extra.get("allow_from")))
    run.say(
        "Only people you allow can talk to the bot. Give their user ids (blank keeps the list)."
    )
    answer = ask_text(io, "Allowed user ids, comma-separated", ",".join(now) or None)
    wanted = _allow_list(answer)
    changes: dict[str, Any] = {}
    if wanted and wanted != now:
        changes["allow_from"] = wanted
    own = str(extra.get("unauthorized_dm_behavior") or "").strip().lower()
    rows = [(key, label) for key, label in _ACCESS_ROWS if info.dm_is_private or key != "pair"]
    keep = f"Keep the current setting ({own})" if own else "Let the gateway decide (see below)"
    labels = [keep, *(label for _, label in rows)]
    picked = ask_choice(io, "What should people who are not allowed get?", labels, 0)
    if picked > 0 and rows[picked - 1][0] != own:
        changes["unauthorized_dm_behavior"] = rows[picked - 1][0]
    if changes:
        _save_platform(run.scope, info.name, {"extra": changes})
    mode, why = run.gateway.access(run.live_scope(), info.name)
    run.say(f"Unknown senders on {info.label}: {mode} ({why}).")
    if not info.dm_is_private:
        run.say(
            f"Pairing codes and sign-in links are never sent on {info.label}: anyone who knows "
            "the address can read what is posted there."
        )


# Platforms where a direct chat's id is the person's own id.
_DM_CHAT_IS_USER: frozenset[str] = frozenset({"telegram"})


def _home(run: _Run, info: PlatformInfo) -> None:
    current = _platform_settings(run.scope, info.name).get("home_channel")
    now = ""
    if isinstance(current, Mapping):
        home = cast(Mapping[str, Any], current)
        now = _text(home.get("chat_id"))
        if now and home.get("thread_id"):
            now = f"{now}:{_text(home.get('thread_id'))}"
    parsed: list[tuple[str, str | None]] = []

    def check(text: str) -> str | None:
        if not text or text == now:
            return None
        if text.startswith("#"):
            return "Give the chat id itself: names are looked up only when sending."
        try:
            parsed[:] = [run.gateway.parse_target(info.name, text)]
        except ValueError as exc:
            return str(exc)
        return None

    answer = ""
    if not now and info.name in _DM_CHAT_IS_USER:
        # A DM's chat id is the person's id here: the first allowed person is a likely home.
        allowed = _allow_list(_text(_platform_extra(run.scope, info.name).get("allow_from")))
        if allowed and check(allowed[0]) is None:
            question = f"Use {allowed[0]} (the first allowed person) as the home channel?"
            if ask_yes_no(run.io, question, True):
                answer = allowed[0]
    if not answer:
        answer = ask_text(
            run.io,
            "Home channel for cron output and notices (a chat id; blank: set it later with "
            "/sethome)",
            now or None,
            validate=check,
        )
    if not answer or answer == now or not parsed:
        return
    chat, thread = parsed[0]
    home_value: dict[str, Any] = {"chat_id": chat}
    if thread:
        home_value["thread_id"] = thread
    _save_platform(run.scope, info.name, {"home_channel": home_value})
    run.say(f"Home channel: {info.name}:{chat}" + (f":{thread}" if thread else ""))


def _connection_check(run: _Run, info: PlatformInfo, typed: Iterable[str]) -> None:
    if not info.ready:
        run.say("Connection check skipped: the platform's library is not installed.")
        return
    scope = run.live_scope()
    state = run.gateway.states(scope).get(info.name, "not set up")
    if state != "on":
        run.say(f"{info.label} is {state}, so there is nothing to check yet.")
        return
    if not ask_yes_no(run.io, f"Check the connection to {info.label} now?", True):
        return
    result = run.gateway.check(scope, info.name, run.deps.check_timeout)
    secrets = [*typed, *(run.current(n) for n in (*info.credentials, *info.public_env))]
    if not result.ok:
        run.say(f"Connection check failed: {_mask(result.detail, secrets)}")
        run.say("The settings are saved; fix the value and run this again.")
        return
    run.say(f"Connection check: {info.label} answered.")
    if info.name == "telegram" and result.identity:
        run.say(f"Open https://t.me/{result.identity} on your phone to start a chat with the bot.")


def _configure(run: _Run, info: PlatformInfo) -> None:
    steps = _steps_for(info)
    run.say("")
    run.say(f"== {info.label} ==")
    if steps.elsewhere:
        run.say(steps.elsewhere)
        return
    for line in steps.intro:
        run.say(line)
    if info.name == "slack":
        run.say(slack_manifest())
    if not info.ready:
        hint = info.install_hint or "install its extra"
        run.say(f"The {info.label} library is not installed: {hint}")
    run.say(f"Secrets are kept in {run.where()}; they are never written to .env or shown.")
    store_values, settings, missing = _collect(run, info, steps.asks)
    if missing:
        run.say(
            f"{info.label} still needs {', '.join(missing)}; nothing was saved, so it stays off."
        )
        return
    other = _clash(run, info, store_values)
    if other is not None:
        run.say(
            f"Refused: profile {other!r} already uses this {info.label} bot. Two gateways on one "
            "bot fight over its messages, so each profile needs a bot of its own (a cloned profile "
            "too). Nothing was saved."
        )
        return
    for name, value in store_values.items():
        run.store.set(name, value)
        run.say(f"{name}: saved.")
    if settings:
        _save_platform(run.scope, info.name, settings)
        for key, value in settings.items():
            run.say(f"platforms.{info.name}.{key}: {value}")
    if steps.enable and _platform_settings(run.scope, info.name).get("enabled") is not True:
        _save_platform(run.scope, info.name, {"enabled": True})
        run.say(f"platforms.{info.name}.enabled: true")
    if steps.access and not info.trusted_inbound:
        _access(run, info)
    if steps.home:
        _home(run, info)
    _connection_check(run, info, store_values.values())


def _menu(run: _Run, platforms: Sequence[PlatformInfo]) -> None:
    while True:
        states = run.gateway.states(run.live_scope())
        rows: list[str] = []
        for info in platforms:
            state = states.get(info.name, "not set up")
            if not info.ready:
                state = f"{state}; library missing"
            rows.append(f"{info.label} ({state})")
        rows.append(MENU_DONE)
        picked = ask_choice(run.io, "Which platform do you want to set up?", rows, len(rows) - 1)
        if picked == len(platforms):
            return
        _configure(run, platforms[picked])


def non_tty_guidance(platforms: Sequence[PlatformInfo]) -> str:
    """What to set without a terminal: per platform its secret names and config keys."""
    lines = [
        "indus gateway setup asks questions, so it needs a terminal. To set a platform up "
        "without one:",
        "  put the secrets in the profile's environment (or its secret store),",
        "  set the settings with `indus config set platforms.<platform>.<key> <value>`,",
        "  then check it with `indus gateway status` or `indus doctor`.",
    ]
    for info in platforms:
        steps = _steps_for(info)
        lines.append(f"{info.name}:")
        if steps.elsewhere:
            lines.append(f"  {steps.elsewhere}")
            continue
        env = [
            a.name + ("" if a.required else " (optional)")
            for a in steps.asks
            if a.kind != "setting"
        ]
        keys = [
            f"platforms.{info.name}.{a.name}" + ("" if a.required else " (optional)")
            for a in steps.asks
            if a.kind == "setting"
        ]
        if steps.access and not info.trusted_inbound:
            keys += [
                f"platforms.{info.name}.extra.allow_from (optional)",
                f"platforms.{info.name}.extra.unauthorized_dm_behavior (optional: pair, ignore, "
                "decline)",
            ]
        if env:
            lines.append("  environment: " + ", ".join(env))
        if keys:
            lines.append("  settings:    " + ", ".join(keys))
    return "\n".join(lines)


def run_gateway_setup(
    state: CliState, platform: str | None, *, deps: GatewaySetupDeps | None = None
) -> int:
    """Run ``indus gateway setup``; returns the exit code."""
    from indus.cli.commands._support import secret_store
    from indus.foundation.errors import IndusError

    deps = deps or GatewaySetupDeps()
    scope = state.scope
    known = deps.gateway.platforms()
    chosen: list[PlatformInfo] = known
    if platform is not None:
        wanted = platform.strip().lower()
        chosen = [p for p in known if p.name == wanted]
        if not chosen:
            click.echo(f"indus gateway setup: there is no platform called {platform!r}.", err=True)
            click.echo("Platforms: " + ", ".join(p.name for p in known), err=True)
            return USAGE
    io = deps.io or io_for(state)
    if not io.tty:
        click.echo(non_tty_guidance(chosen))
        return USAGE
    try:
        store = deps.store or secret_store(scope)
        run = _Run(scope, io, deps, store)
        if platform is not None:
            _configure(run, chosen[0])
        else:
            io.say("Gateway setup. Press Ctrl+C at any time to stop.")
            _menu(run, known)
    except Cancelled:
        io.say("")
        io.say(CANCELLED)
        return CANCEL_EXIT
    except IndusError as exc:
        click.echo(f"indus gateway setup: {exc.message}", err=True)
        return CANCEL_EXIT
    io.say("")
    io.say(
        "Done. Start the gateway with `indus gateway start` (or restart it to pick up changes); "
        "`indus gateway install` keeps it running as a service."
    )
    return OK


@click.command(
    "setup",
    help="Set up a messaging platform step by step: credentials, who may talk to the bot, a "
    "connection check. PLATFORM picks one; without it a menu lists them all.",
    context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100},
)
@click.argument("platform", required=False)
@click.pass_context
def setup_command(ctx: click.Context, platform: str | None) -> int:
    from indus.cli.commands._support import state_of

    return run_gateway_setup(state_of(ctx), platform)
