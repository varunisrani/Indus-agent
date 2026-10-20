"""Gateway settings and platform enablement (P15-02).

Settings come through the one config loader and defaults table (D-16.32, R06.25): the
``platforms.<name>`` and ``gateway`` rows live in ``foundation/config/schema.py`` like every other
section, so a value set only in the defaults reads the same here as in the CLI.

A platform starts when its vendor credentials are present — secret store first, environment
second — unless its config says ``enabled: false``. An explicit false always wins and logs one
warning per platform and profile per process, naming the ignored credentials (never their values;
R06.15). A missing key keeps credential enablement; ``enabled: true`` without credentials does not
start the platform and is reported as a problem.

The legacy idle/daily conversation reset (``session_reset``) is never read: conversations reset
only on ``/new``. A timed one still in the file gets one warning (and a ``doctor`` line).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, cast

from pydantic import ValidationError

from indus.foundation import config as config_mod
from indus.foundation.clock import Clock
from indus.foundation.config.schema import (
    ChannelOverride,
    DisplaySection,
    GatewaySection,
    PlatformSection,
    PlatformsSection,
)
from indus.foundation.scope import Scope

log = logging.getLogger(__name__)

# Vendor credential names (kept as the vendors spell them, D-16.22). Every name must be present.
# Their values are masked in every log line and error text (``Adapter.scrub``) and, unless the
# registry entry names its own ``lock_env``, joined they key the token lock, so only secrets and
# identities belong here: server URLs and hosts are read by the adapter itself (Signal's account
# number and the SMS and WhatsApp numbers, a person's phone number, are masked too).
BUILTIN_CREDENTIALS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "telegram": ("TELEGRAM_BOT_TOKEN",),
        "discord": ("DISCORD_BOT_TOKEN",),
        "slack": ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"),
        "signal": ("SIGNAL_ACCOUNT",),
        "matrix": ("MATRIX_ACCESS_TOKEN",),
        "mattermost": ("MATTERMOST_TOKEN",),
        "email": ("EMAIL_ADDRESS", "EMAIL_PASSWORD"),
        "whatsapp_cloud": ("WHATSAPP_CLOUD_ACCESS_TOKEN", "WHATSAPP_CLOUD_PHONE_NUMBER_ID"),
        "sms": ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER"),
        "line": ("LINE_CHANNEL_ACCESS_TOKEN", "LINE_CHANNEL_SECRET"),
        "teams": ("TEAMS_CLIENT_ID", "TEAMS_CLIENT_SECRET", "TEAMS_TENANT_ID"),
        "msgraph": ("MSGRAPH_WEBHOOK_CLIENT_STATE",),
        "google_chat": ("GOOGLE_CHAT_SERVICE_ACCOUNT_JSON",),
        "qqbot": ("QQ_APP_ID", "QQ_CLIENT_SECRET"),
        "dingtalk": ("DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET"),
        "wecom": ("WECOM_BOT_ID", "WECOM_SECRET"),
        "wecom_callback": (
            "WECOM_CALLBACK_CORP_ID",
            "WECOM_CALLBACK_CORP_SECRET",
            "WECOM_CALLBACK_TOKEN",
            "WECOM_CALLBACK_ENCODING_AES_KEY",
        ),
    }
)

# Env names a platform needs that are not secrets (a server, a nick, a topic). Like credentials,
# all of them present turn the platform on, but their values are never masked; the registry
# entry's ``lock_env`` decides whether they key the token lock.
BUILTIN_PUBLIC_ENV: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "ntfy": ("NTFY_TOPIC",),
        "irc": ("IRC_SERVER", "IRC_NICKNAME"),
    }
)

GLOBAL_ALLOWED_USERS_ENV = "INDUS_GATEWAY_ALLOWED_USERS"
GLOBAL_ALLOW_ALL_ENV = "INDUS_GATEWAY_ALLOW_ALL_USERS"

_TIMED_RESET_MODES = frozenset({"idle", "daily", "both"})


@dataclass(frozen=True)
class PolicyEnvNames:
    """Per-platform policy variables (our prefix; glossary proposal (e)4)."""

    allowed_users: str
    allow_all_users: str
    allow_bots: str
    home_channel: str


def policy_env(platform: str) -> PolicyEnvNames:
    stem = f"INDUS_{platform.upper()}"
    return PolicyEnvNames(
        allowed_users=f"{stem}_ALLOWED_USERS",
        allow_all_users=f"{stem}_ALLOW_ALL_USERS",
        allow_bots=f"{stem}_ALLOW_BOTS",
        home_channel=f"{stem}_HOME_CHANNEL",
    )


def read_secret(scope: Scope, name: str) -> str | None:
    """A credential: the profile's secret store first, then its environment. Blank = absent."""
    if scope.secrets is not None:
        try:
            value = scope.secrets.get(name)
        except Exception as exc:  # an unreadable store must not stop the env fallback
            log.warning("secret store could not be read for %s: %s", name, type(exc).__name__)
            value = None
        if value and value.strip():
            return value.strip()
    value = scope.env.get(name)
    return value.strip() if value and value.strip() else None


@dataclass(frozen=True)
class HomeChannel:
    platform: str
    chat_id: str
    name: str = ""
    thread_id: str | None = None


@dataclass(frozen=True)
class PlatformSettings:
    name: str
    enabled: bool
    # Why: "credentials", "disabled in config", "missing credentials: X", "not configured".
    reason: str
    section: PlatformSection
    # The config's own value: True, False or None (unset).
    explicit: bool | None = None
    credentials: Mapping[str, str] = field(default_factory=dict[str, str], repr=False)
    home_channel: HomeChannel | None = None

    @property
    def reply_to_mode(self) -> str:
        return self.section.reply_to_mode

    @property
    def restart_notice(self) -> bool:
        return self.section.restart_notice

    @property
    def typing_indicator(self) -> bool:
        return self.section.typing_indicator

    @property
    def streaming(self) -> bool:
        """``platforms.<name>.streaming``, else the platform's default (P15-16)."""
        if self.section.streaming is not None:
            return self.section.streaming
        from indus.gateway.outbound.stream import streaming_default

        return streaming_default(self.name)

    @property
    def tool_progress(self) -> str:
        """``platforms.<name>.tool_progress``, else the platform's default (P15-16)."""
        if self.section.tool_progress is not None:
            return self.section.tool_progress
        from indus.gateway.outbound.progress import progress_defaults

        return progress_defaults(self.name).mode

    @property
    def extra(self) -> Mapping[str, Any]:
        """Platform-specific settings: keys written straight under ``platforms.<name>`` (where
        ``indus config set`` puts them) plus the ``extra`` block, which wins on a clash."""
        return {**(self.section.model_extra or {}), **self.section.extra}

    def channel_override(self, chat_id: str) -> ChannelOverride | None:
        return self.section.channel_overrides.get(str(chat_id))


@dataclass(frozen=True)
class AdapterConfig:
    """What a platform entry's factory gets: ``entry.factory(AdapterConfig(...)) -> Adapter``
    (the daemon, P15-24, and ``indus send``, P15-20, build adapters the same way)."""

    name: str
    settings: PlatformSettings
    scope: Scope
    clock: Clock
    proxy: str | None = None
    # One-shot sends (``indus send``): connect for sending only — no listener, no update
    # consumption, so a running gateway keeps every inbound event.
    send_only: bool = False

    @property
    def extra(self) -> Mapping[str, Any]:
        return self.settings.extra


@dataclass(frozen=True)
class GatewaySettings:
    platforms: Mapping[str, PlatformSettings]
    gateway: GatewaySection
    problems: tuple[str, ...] = ()
    # ``display`` rows the gateway reads: ephemeral_system_ttl, tool_progress_grouping,
    # heartbeat_seconds, busy_ack, busy_text_* (P15-12, 16, 17).
    display: DisplaySection = field(default_factory=DisplaySection)

    def enabled(self) -> list[str]:
        return sorted(name for name, p in self.platforms.items() if p.enabled)

    def get(self, name: str) -> PlatformSettings | None:
        return self.platforms.get(name)


# -- one warning per platform per profile per process --------------------------------------------

_warned: set[tuple[str, str]] = set()
_warned_lock = threading.Lock()


def _warn_once(key: tuple[str, str], message: str, *args: object) -> None:
    with _warned_lock:
        if key in _warned:
            return
        _warned.add(key)
    log.warning(message, *args)


def reset_warnings() -> None:
    """Forget which warnings were given (a fresh process, for tests)."""
    with _warned_lock:
        _warned.clear()


# -- reading ------------------------------------------------------------------------------------


def _view(scope: Scope) -> Any:
    return scope.config if scope.config is not None else config_mod.load(scope)


def _mapping(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(cast(Mapping[str, Any], value))
    return {}


def _section(name: str, raw: object, problems: list[str]) -> PlatformSection:
    try:
        return PlatformSection.model_validate(_mapping(raw))
    except ValidationError as exc:
        errors = exc.errors()
        where = ".".join(str(p) for p in errors[0]["loc"]) if errors else ""
        problems.append(f"platforms.{name}.{where}: invalid value; using the built-in settings")
        return PlatformSection()


def _home_channel(scope: Scope, name: str, section: PlatformSection) -> HomeChannel | None:
    home = section.home_channel
    if home is not None and home.chat_id.strip():
        return HomeChannel(
            platform=home.platform.strip() or name,
            chat_id=home.chat_id.strip(),
            name=home.name,
            thread_id=home.thread_id or None,
        )
    chat_id = (scope.env.get(policy_env(name).home_channel) or "").strip()
    return HomeChannel(platform=name, chat_id=chat_id) if chat_id else None


def _platform(
    scope: Scope,
    name: str,
    section: PlatformSection,
    names: tuple[str, ...],
    problems: list[str],
    public: tuple[str, ...] = (),
) -> PlatformSettings:
    found = {n: v for n in names if (v := read_secret(scope, n)) is not None}
    missing = [n for n in names if n not in found]
    missing += [n for n in public if read_secret(scope, n) is None]
    names = (*names, *public)
    has_credentials = bool(names) and not missing
    explicit = section.enabled
    home = _home_channel(scope, name, section)
    if explicit is False:
        if has_credentials:
            _warn_once(
                (str(scope.home), name),
                "platform %s is turned off by platforms.%s.enabled: false, so its credentials "
                "(%s) are ignored; remove the key or set it to true to turn it back on",
                name,
                name,
                ", ".join(names),
            )
        return PlatformSettings(name, False, "disabled in config", section, False, {}, home)
    if has_credentials:
        return PlatformSettings(name, True, "credentials", section, explicit, found, home)
    if explicit is True:
        if not names:
            # A platform with no credential table (a plugin's own transport) runs on config alone.
            return PlatformSettings(name, True, "enabled in config", section, True, {}, home)
        reason = "missing credentials: " + ", ".join(missing)
        problems.append(f"{name} is enabled in config but {reason}")
        _warn_once((str(scope.home), f"{name}:missing"), "%s is enabled but %s", name, reason)
        return PlatformSettings(name, False, reason, section, True, {}, home)
    return PlatformSettings(name, False, "not configured", section, explicit, {}, home)


def legacy_reset_notice(tree: Mapping[str, Any]) -> str | None:
    """The `doctor` / startup line for a timed ``session_reset`` block still in the config."""
    gateway = _mapping(tree.get("gateway"))
    for path, block in (
        ("session_reset", tree.get("session_reset")),
        ("gateway.session_reset", gateway.get("session_reset")),
    ):
        mode = str(_mapping(block).get("mode") or "").strip().lower()
        if mode in _TIMED_RESET_MODES:
            return (
                f"{path}.mode: {mode} is not used: messaging conversations reset only when you "
                "send /new (or /reset); remove the block to silence this warning"
            )
    return None


def gateway_section(scope: Scope) -> GatewaySection:
    """The profile's ``gateway`` section on its own, schema defaults filled in (an invalid
    section gives the built-in settings, as ``load_gateway_settings`` does)."""
    try:
        return GatewaySection.model_validate(_mapping(_view(scope).get("gateway")))
    except ValidationError:
        return GatewaySection()


def load_gateway_settings(
    scope: Scope,
    *,
    credentials: Mapping[str, tuple[str, ...]] = BUILTIN_CREDENTIALS,
    public: Mapping[str, tuple[str, ...]] = BUILTIN_PUBLIC_ENV,
) -> GatewaySettings:
    """Effective gateway settings for ``scope``: one entry per known or configured platform."""
    view = _view(scope)
    problems: list[str] = []
    raw_platforms = _mapping(view.get("platforms"))
    names = sorted(
        set(credentials) | set(public) | set(raw_platforms) | set(PlatformsSection.model_fields)
    )
    platforms: dict[str, PlatformSettings] = {}
    for name in names:
        section = _section(name, raw_platforms.get(name), problems)
        platforms[name] = _platform(
            scope,
            name,
            section,
            tuple(credentials.get(name, ())),
            problems,
            tuple(public.get(name, ())),
        )
    try:
        gateway = GatewaySection.model_validate(_mapping(view.get("gateway")))
    except ValidationError:
        problems.append("gateway: invalid value; using the built-in settings")
        gateway = GatewaySection()
    tree = {"session_reset": view.get("session_reset"), "gateway": view.get("gateway")}
    notice = legacy_reset_notice(tree)
    if notice is not None:
        problems.append(notice)
        _warn_once((str(scope.home), "session_reset"), "%s", notice)
    try:
        display = DisplaySection.model_validate(_mapping(view.get("display")))
    except ValidationError:
        problems.append("display: invalid value; using the built-in settings")
        display = DisplaySection()
    return GatewaySettings(MappingProxyType(platforms), gateway, tuple(problems), display)


def lock_key(scope: Scope, settings: PlatformSettings, lock_env: tuple[str, ...] = ()) -> str:
    """What the daemon's token lock is keyed on (R06.16; the lock file name holds only a hash).

    With ``lock_env`` (the registry entry's): those values, read like credentials, joined in that
    order. Without: the platform's credential values joined in name order. Empty: no lock.
    """
    if lock_env:
        values = [read_secret(scope, n) or "" for n in lock_env]
        return "\n".join(values) if any(values) else ""
    return "\n".join(settings.credentials[k] for k in sorted(settings.credentials))


__all__ = [
    "BUILTIN_CREDENTIALS",
    "BUILTIN_PUBLIC_ENV",
    "AdapterConfig",
    "GLOBAL_ALLOWED_USERS_ENV",
    "GLOBAL_ALLOW_ALL_ENV",
    "GatewaySettings",
    "HomeChannel",
    "PlatformSettings",
    "PolicyEnvNames",
    "gateway_section",
    "legacy_reset_notice",
    "load_gateway_settings",
    "lock_key",
    "policy_env",
    "read_secret",
    "reset_warnings",
]
