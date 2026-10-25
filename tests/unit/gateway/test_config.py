"""Gateway settings and platform enablement through the one loader (P15-02; R06.15, R06.25)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest

from indus.foundation import config as config_mod
from indus.foundation.config import loader
from indus.foundation.config.schema import PlatformSection
from indus.foundation.scope import Scope
from indus.gateway.config import (
    BUILTIN_CREDENTIALS,
    BUILTIN_PUBLIC_ENV,
    GLOBAL_ALLOW_ALL_ENV,
    GLOBAL_ALLOWED_USERS_ENV,
    HomeChannel,
    legacy_reset_notice,
    load_gateway_settings,
    lock_key,
    policy_env,
    read_secret,
    reset_warnings,
)


def _fake_token(tag: str) -> str:
    # Built at runtime: never a credential-shaped literal in the source.
    return "-".join(["fake", tag, "0123456789abcdef"])


@pytest.fixture(autouse=True)
def _fresh() -> Iterator[None]:
    reset_warnings()
    loader.reset()
    yield
    reset_warnings()
    loader.reset()


def _write(scope: Scope, text: str) -> None:
    (scope.home / "config.yaml").write_text(text, encoding="utf-8")
    loader.reset()


def _with_env(scope: Scope, **env: str) -> Scope:
    return scope.with_env(scope.env.with_overlay(env))


def _enabled_line(value: str) -> str:
    return "" if value == "missing" else f"    enabled: {value}\n"


CREDS = {
    "telegram": {"TELEGRAM_BOT_TOKEN": _fake_token("tg")},
    "discord": {"DISCORD_BOT_TOKEN": _fake_token("dc")},
    "slack": {"SLACK_BOT_TOKEN": _fake_token("sb"), "SLACK_APP_TOKEN": _fake_token("sa")},
}

# (credentials present, enabled value) → (started, reason)
TABLE = [
    (True, "true", True, "credentials"),
    (True, "false", False, "disabled in config"),
    (True, "missing", True, "credentials"),
    (False, "true", False, "missing credentials"),
    (False, "false", False, "disabled in config"),
    (False, "missing", False, "not configured"),
]


@pytest.mark.parametrize("platform", ["telegram", "discord", "slack"])
@pytest.mark.parametrize(("present", "enabled", "started", "reason"), TABLE)
def test_enablement_table(
    scope: Scope, platform: str, present: bool, enabled: str, started: bool, reason: str
) -> None:
    _write(scope, f"platforms:\n  {platform}:\n{_enabled_line(enabled)}    reply_to_mode: all\n")
    if present:
        scope = _with_env(scope, **CREDS[platform])
    settings = load_gateway_settings(scope)
    entry = settings.platforms[platform]
    assert entry.enabled is started
    assert entry.reason.startswith(reason)
    assert entry.reply_to_mode == "all"
    assert settings.enabled() == ([platform] if started else [])
    if started:
        assert dict(entry.credentials) == CREDS[platform]
    else:
        assert dict(entry.credentials) == {}
    assert (f"{platform} is enabled in config but" in " ".join(settings.problems)) is (
        not present and enabled == "true"
    )


def test_slack_needs_both_tokens(scope: Scope) -> None:
    scope = _with_env(scope, SLACK_BOT_TOKEN=_fake_token("sb"), SLACK_APP_TOKEN="   ")
    entry = load_gateway_settings(scope).platforms["slack"]
    assert not entry.enabled and entry.reason == "not configured"
    _write(scope, "platforms:\n  slack:\n    enabled: true\n")
    entry = load_gateway_settings(scope).platforms["slack"]
    assert not entry.enabled and entry.reason == "missing credentials: SLACK_APP_TOKEN"


def test_explicit_disable_warns_once_per_platform_names_not_values(
    scope: Scope, caplog: pytest.LogCaptureFixture
) -> None:
    _write(scope, "platforms:\n  telegram:\n    enabled: false\n  discord:\n    enabled: false\n")
    scope = _with_env(scope, **CREDS["telegram"], **CREDS["discord"])
    with caplog.at_level(logging.WARNING, logger="indus.gateway.config"):
        load_gateway_settings(scope)
        load_gateway_settings(scope)
    warnings = [r.getMessage() for r in caplog.records if "is turned off" in r.getMessage()]
    assert len(warnings) == 2
    assert any("TELEGRAM_BOT_TOKEN" in w for w in warnings)
    assert any("DISCORD_BOT_TOKEN" in w for w in warnings)
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert CREDS["telegram"]["TELEGRAM_BOT_TOKEN"] not in joined


def test_no_warning_when_disabled_without_credentials(
    scope: Scope, caplog: pytest.LogCaptureFixture
) -> None:
    _write(scope, "platforms:\n  telegram:\n    enabled: false\n")
    with caplog.at_level(logging.WARNING, logger="indus.gateway.config"):
        load_gateway_settings(scope)
    assert not [r for r in caplog.records if "is turned off" in r.getMessage()]


def test_secret_store_comes_before_env(scope: Scope) -> None:
    assert scope.secrets is not None
    stored = _fake_token("stored")
    scope.secrets.set("TELEGRAM_BOT_TOKEN", stored)  # type: ignore[attr-defined]
    scope = _with_env(scope, TELEGRAM_BOT_TOKEN=_fake_token("env"))
    assert read_secret(scope, "TELEGRAM_BOT_TOKEN") == stored
    entry = load_gateway_settings(scope).platforms["telegram"]
    assert entry.enabled and entry.credentials["TELEGRAM_BOT_TOKEN"] == stored
    assert read_secret(scope, "NOT_SET_ANYWHERE") is None


def test_settings_repr_never_shows_credentials(scope: Scope) -> None:
    scope = _with_env(scope, **CREDS["discord"])
    settings = load_gateway_settings(scope)
    assert CREDS["discord"]["DISCORD_BOT_TOKEN"] not in repr(settings)


def test_home_channel_config_wins_over_env(scope: Scope) -> None:
    home_env = policy_env("telegram").home_channel
    scope = _with_env(scope, **{home_env: "-100777"})
    entry = load_gateway_settings(scope).platforms["telegram"]
    assert entry.home_channel == HomeChannel("telegram", "-100777")
    _write(
        scope,
        "platforms:\n  telegram:\n    home_channel:\n      chat_id: '-100123'\n"
        "      name: Ops\n      thread_id: '42'\n",
    )
    entry = load_gateway_settings(scope).platforms["telegram"]
    assert entry.home_channel == HomeChannel("telegram", "-100123", "Ops", "42")


def test_policy_env_names_use_our_prefix() -> None:
    names = policy_env("slack")
    assert names.allowed_users == "INDUS_SLACK_ALLOWED_USERS"
    assert names.allow_all_users == "INDUS_SLACK_ALLOW_ALL_USERS"
    assert names.allow_bots == "INDUS_SLACK_ALLOW_BOTS"
    assert names.home_channel == "INDUS_SLACK_HOME_CHANNEL"
    assert GLOBAL_ALLOWED_USERS_ENV == "INDUS_GATEWAY_ALLOWED_USERS"
    assert GLOBAL_ALLOW_ALL_ENV == "INDUS_GATEWAY_ALLOW_ALL_USERS"
    assert dict(BUILTIN_CREDENTIALS) == {
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
        "weixin": ("WEIXIN_TOKEN", "WEIXIN_ACCOUNT_ID"),
        "feishu": ("FEISHU_APP_ID", "FEISHU_APP_SECRET"),
        "bluebubbles": ("BLUEBUBBLES_PASSWORD",),
        "buzz": ("BUZZ_PRIVATE_KEY",),
        "yuanbao": ("YUANBAO_APP_ID", "YUANBAO_APP_SECRET"),
    }
    # Servers, nicks and topics turn their platform on but are not secrets (never masked).
    assert dict(BUILTIN_PUBLIC_ENV) == {
        "ntfy": ("NTFY_TOPIC",),
        "irc": ("IRC_SERVER", "IRC_NICKNAME"),
        "simplex": ("SIMPLEX_WS_URL",),
        "raft": ("RAFT_PROFILE",),
    }
    assert not set(BUILTIN_PUBLIC_ENV) & set(BUILTIN_CREDENTIALS)
    # URLs and hosts are read by the adapters, never masked or lock-keyed as credentials.
    for names in BUILTIN_CREDENTIALS.values():
        assert not any(n.endswith(("_URL", "_HOST", "_HOMESERVER", "_SERVER")) for n in names)


def test_defaults_come_from_the_shared_table(scope: Scope) -> None:
    """A key set only in defaults reads the same through the CLI's view and the gateway's."""
    view = config_mod.load(scope)
    settings = load_gateway_settings(scope)
    for name in ("telegram", "discord", "slack"):
        entry = settings.platforms[name]
        assert entry.reply_to_mode == view.get(f"platforms.{name}.reply_to_mode") == "first"
        assert entry.restart_notice is view.get(f"platforms.{name}.restart_notice") is True
        assert entry.typing_indicator is view.get(f"platforms.{name}.typing_indicator") is True
        assert entry.explicit is None and view.get(f"platforms.{name}.enabled") is None
        assert view.source(f"platforms.{name}.reply_to_mode") == "default"
    assert settings.gateway.message_timestamps.enabled is False
    assert view.get("gateway.message_timestamps.enabled") is False
    assert view.get("display.busy_text_quiet_s") == 0.35


def test_settings_round_trip_through_the_writer(scope: Scope) -> None:
    data: dict[str, Any] = {
        "platforms": {
            "telegram": {
                "enabled": False,
                "reply_to_mode": "off",
                "home_channel": {"chat_id": "-100123", "name": "Ops"},
                "channel_overrides": {"-100123": {"model": "small-model"}},
            },
            "discord": {"typing_indicator": False, "extra": {"free_response": ["general"]}},
        },
        "gateway": {"message_timestamps": {"enabled": True}},
    }
    config_mod.save(scope, data)
    loader.reset()
    settings = load_gateway_settings(_with_env(scope, **CREDS["telegram"]))
    telegram = settings.platforms["telegram"]
    assert not telegram.enabled and telegram.explicit is False
    assert telegram.reply_to_mode == "off"
    assert telegram.home_channel == HomeChannel("telegram", "-100123", "Ops")
    override = telegram.channel_override("-100123")
    assert override is not None and override.model == "small-model"
    assert telegram.channel_override("other") is None
    discord = settings.platforms["discord"]
    assert discord.typing_indicator is False and discord.extra == {"free_response": ["general"]}
    assert settings.gateway.message_timestamps.enabled is True
    # Saving the loaded view again changes nothing.
    text = (scope.home / "config.yaml").read_text(encoding="utf-8")
    config_mod.save(scope, config_mod.load(scope))
    assert (scope.home / "config.yaml").read_text(encoding="utf-8") == text


def test_invalid_platform_block_falls_back_alone(scope: Scope) -> None:
    _write(
        scope,
        "platforms:\n  telegram:\n    reply_to_mode: sometimes\n  discord:\n    enabled: true\n",
    )
    scope = _with_env(scope, **CREDS["discord"])
    settings = load_gateway_settings(scope)
    assert settings.platforms["telegram"].section == PlatformSection()
    assert any("platforms.telegram.reply_to_mode" in p for p in settings.problems)
    assert settings.platforms["discord"].enabled


def test_plugin_platform_without_credentials_runs_on_config(scope: Scope) -> None:
    _write(
        scope, "platforms:\n  fakechat:\n    enabled: true\n    extra:\n      server: chat.test\n"
    )
    settings = load_gateway_settings(scope)
    assert settings.platforms["fakechat"].enabled
    assert settings.platforms["fakechat"].extra == {"server": "chat.test"}
    _write(scope, "platforms:\n  fakechat:\n    extra: {}\n")
    assert not load_gateway_settings(scope).platforms["fakechat"].enabled


def test_public_names_turn_a_platform_on_but_are_not_credentials(scope: Scope) -> None:
    assert not load_gateway_settings(scope).platforms["irc"].enabled
    half = load_gateway_settings(_with_env(scope, IRC_SERVER="irc.example.test")).platforms["irc"]
    assert not half.enabled and half.reason == "not configured"
    scope = _with_env(scope, IRC_SERVER="irc.example.test", IRC_NICKNAME="indus")
    irc = load_gateway_settings(scope).platforms["irc"]
    assert irc.enabled and irc.reason == "credentials"
    assert dict(irc.credentials) == {}  # nothing to mask: the server and nick are public
    _write(scope, "platforms:\n  ntfy:\n    enabled: true\n")
    ntfy = load_gateway_settings(scope).platforms["ntfy"]
    assert not ntfy.enabled and ntfy.reason == "missing credentials: NTFY_TOPIC"


def test_lock_keys_follow_the_registry_entry(scope: Scope) -> None:
    from indus.gateway.registry import PlatformRegistry

    registry = PlatformRegistry()

    def key(platform: str, **env: str) -> str:
        entry = registry.get(platform)
        assert entry is not None
        local = _with_env(scope, **env)
        settings = load_gateway_settings(local).platforms[platform]
        assert settings.enabled
        return lock_key(local, settings, entry.lock_env)

    # IRC: server + nick, in that order (R06.16); the channel is not part of it.
    irc = key("irc", IRC_SERVER="irc.example.test", IRC_NICKNAME="indus", IRC_CHANNEL="#a")
    assert irc == "irc.example.test\nindus"
    assert key("irc", IRC_SERVER="irc.example.test", IRC_NICKNAME="indus") == irc
    assert key("irc", IRC_SERVER="irc.example.test", IRC_NICKNAME="indus2") != irc
    assert key("ntfy", NTFY_TOPIC="alerts") == "alerts"
    # Email: address + IMAP host; a new password is the same mailbox.
    mail = {
        "EMAIL_ADDRESS": "bot@example.test",
        "EMAIL_IMAP_HOST": "imap.example.test",
        "EMAIL_SMTP_HOST": "smtp.example.test",
    }
    first = key("email", EMAIL_PASSWORD=_fake_token("p1"), **mail)
    assert first == "bot@example.test\nimap.example.test"
    assert key("email", EMAIL_PASSWORD=_fake_token("p2"), **mail) == first
    assert _fake_token("p1") not in first
    # Without ``lock_env``: the credential values, as before (Signal keeps its number).
    signal = registry.get("signal")
    assert signal is not None and signal.lock_env == ()
    assert key("telegram", TELEGRAM_BOT_TOKEN=_fake_token("tg")) == _fake_token("tg")


def test_p16_e6_to_e8_lock_keys_are_the_stable_ids_never_the_secrets(scope: Scope) -> None:
    from indus.gateway.registry import PlatformRegistry

    registry = PlatformRegistry()

    def key(platform: str, **env: str) -> str:
        entry = registry.get(platform)
        assert entry is not None
        local = _with_env(scope, **env)
        settings = load_gateway_settings(local).platforms[platform]
        assert settings.enabled, settings.reason
        return lock_key(local, settings, entry.lock_env)

    def rotated(platform: str, secret_name: str, expected: str, **env: str) -> None:
        first = key(platform, **{secret_name: _fake_token("s1")}, **env)
        assert first == expected
        assert key(platform, **{secret_name: _fake_token("s2")}, **env) == first
        assert _fake_token("s1") not in first

    teams = {"TEAMS_CLIENT_ID": "app-1", "TEAMS_TENANT_ID": "tenant-1"}
    rotated("teams", "TEAMS_CLIENT_SECRET", "app-1\ntenant-1", **teams)
    rotated("qqbot", "QQ_CLIENT_SECRET", "102000009", QQ_APP_ID="102000009")
    rotated("dingtalk", "DINGTALK_CLIENT_SECRET", "ding-app", DINGTALK_CLIENT_ID="ding-app")
    rotated("wecom", "WECOM_SECRET", "bot-1", WECOM_BOT_ID="bot-1")
    callback = {
        "WECOM_CALLBACK_CORP_ID": "corp-1",
        "WECOM_CALLBACK_AGENT_ID": "1000002",
        "WECOM_CALLBACK_TOKEN": _fake_token("tok"),
        "WECOM_CALLBACK_ENCODING_AES_KEY": _fake_token("aes"),
    }
    rotated("wecom_callback", "WECOM_CALLBACK_CORP_SECRET", "corp-1\n1000002", **callback)
    other_agent = {**callback, "WECOM_CALLBACK_AGENT_ID": "1000003"}
    assert key("wecom_callback", WECOM_CALLBACK_CORP_SECRET="x", **other_agent) != key(
        "wecom_callback", WECOM_CALLBACK_CORP_SECRET="x", **callback
    )
    # Google Chat (the key JSON) and the Graph receiver (its clientState) keep the credential.
    for name in ("google_chat", "msgraph"):
        entry = registry.get(name)
        assert entry is not None and entry.lock_env == ()
    state = _fake_token("graph-client-state")
    assert key("msgraph", MSGRAPH_WEBHOOK_CLIENT_STATE=state) == state


def test_p16_e9_e10_lock_keys(scope: Scope) -> None:
    from indus.gateway.registry import PlatformRegistry

    registry = PlatformRegistry()

    def key(platform: str, **env: str) -> str:
        entry = registry.get(platform)
        assert entry is not None
        local = _with_env(scope, **env)
        settings = load_gateway_settings(local).platforms[platform]
        assert settings.enabled, settings.reason
        return lock_key(local, settings, entry.lock_env)

    # Feishu: the app id; a new app secret is the same bot.
    first = key("feishu", FEISHU_APP_ID="cli_a1", FEISHU_APP_SECRET=_fake_token("f1"))
    assert first == "cli_a1"
    assert key("feishu", FEISHU_APP_ID="cli_a1", FEISHU_APP_SECRET=_fake_token("f2")) == first
    # BlueBubbles: the server; a new password is the same Mac. A server URL set only in config
    # (``platforms.bluebubbles.server_url``) is not readable here, so the password keys it then.
    server = {"BLUEBUBBLES_SERVER_URL": "http://mac.example.test:1234"}
    bb = key("bluebubbles", BLUEBUBBLES_PASSWORD=_fake_token("b1"), **server)
    assert bb == "http://mac.example.test:1234"
    assert key("bluebubbles", BLUEBUBBLES_PASSWORD=_fake_token("b2"), **server) == bb
    assert key("bluebubbles", BLUEBUBBLES_PASSWORD=_fake_token("b1")) == _fake_token("b1")
    # SimpleX: the daemon's address turns it on and keys the lock, never masked.
    url = "ws://127.0.0.1:5225"
    assert key("simplex", SIMPLEX_WS_URL=url) == url
    simplex = load_gateway_settings(_with_env(scope, SIMPLEX_WS_URL=url)).platforms["simplex"]
    assert dict(simplex.credentials) == {}
    # Buzz: the private key is the identity (one key, one npub), so the credential keys the lock;
    # another relay with the same key is the same identity.
    nsec = _fake_token("nostr")
    relay = {"BUZZ_RELAY_URL": "https://relay.example.test"}
    entry = registry.get("buzz")
    assert entry is not None and entry.lock_env == ()
    assert key("buzz", BUZZ_PRIVATE_KEY=nsec, **relay) == nsec
    assert key("buzz", BUZZ_PRIVATE_KEY=nsec, BUZZ_RELAY_URL="https://other.example.test") == nsec


def test_custom_credential_table(scope: Scope) -> None:
    scope = _with_env(scope, FAKECHAT_TOKEN=_fake_token("fc"))
    assert "fakechat" not in load_gateway_settings(scope).platforms
    table = {**BUILTIN_CREDENTIALS, "fakechat": ("FAKECHAT_TOKEN",)}
    assert load_gateway_settings(scope, credentials=table).platforms["fakechat"].enabled


@pytest.mark.parametrize(
    ("text", "warns"),
    [
        ("session_reset:\n  mode: idle\n  idle_minutes: 60\n", True),
        ("gateway:\n  session_reset:\n    mode: daily\n", True),
        ("session_reset:\n  mode: none\n", False),
        ("", False),
    ],
)
def test_legacy_reset_block_is_not_read_and_warns_once(
    scope: Scope, caplog: pytest.LogCaptureFixture, text: str, warns: bool
) -> None:
    _write(scope, text)
    with caplog.at_level(logging.WARNING, logger="indus.gateway.config"):
        first = load_gateway_settings(scope)
        load_gateway_settings(scope)
    hits = [r for r in caplog.records if "is not used" in r.getMessage()]
    assert len(hits) == (1 if warns else 0)
    assert any("/new" in p for p in first.problems) is warns
    tree = config_mod.load(scope).user_raw()
    assert (legacy_reset_notice(tree) is not None) is warns


def test_settings_under_the_platform_block_reach_extra(scope: Scope) -> None:
    _write(
        scope,
        "platforms:\n  discord:\n    free_response_channels: [general]\n"
        "    require_mention: true\n    extra:\n      require_mention: false\n",
    )
    extra = load_gateway_settings(scope).platforms["discord"].extra
    assert extra == {"free_response_channels": ["general"], "require_mention": False}


# -- rows other wave-1 slices read (P15-06..10, P15-16..18; integration) --------------------------


def test_gateway_rows_reach_their_readers(scope: Scope) -> None:
    from indus.gateway.core.bot_loop import BotLoopSettings, settings_reader
    from indus.gateway.core.session_keys import key_options
    from indus.gateway.outbound.media_policy import MediaPolicy

    defaults = load_gateway_settings(scope)
    assert key_options(defaults.gateway) == {
        "group_sessions_per_user": True,
        "thread_sessions_per_user": False,
    }
    assert BotLoopSettings() == BotLoopSettings(True, 20, 300.0, 600.0)
    assert not MediaPolicy.for_scope(scope, defaults.gateway).trust_recent
    assert defaults.display.ephemeral_system_ttl == 0.0
    assert defaults.display.heartbeat_seconds == 180.0
    _write(
        scope,
        "gateway:\n"
        "  thread_sessions_per_user: true\n"
        "  unauthorized_dm_behavior: decline\n"
        "  bot_loop_guard: {max_messages: 5}\n"
        "  media_strict: true\n"
        "display:\n"
        "  ephemeral_system_ttl: 30\n"
        "  tool_progress_grouping: separate\n",
    )
    got = load_gateway_settings(scope)
    assert key_options(got.gateway)["thread_sessions_per_user"] is True
    assert got.gateway.unauthorized_dm_behavior == "decline"
    view = config_mod.load(scope)
    assert settings_reader(view.get)() == BotLoopSettings(max_messages=5)
    assert MediaPolicy.for_scope(scope).strict
    assert got.display.ephemeral_system_ttl == 30.0
    assert got.display.tool_progress_grouping == "separate"


def test_streaming_and_tool_progress_follow_platform_defaults(scope: Scope) -> None:
    got = load_gateway_settings(scope)
    assert [got.platforms[n].streaming for n in ("telegram", "discord", "slack")] == [
        True,
        False,
        False,
    ]
    assert got.platforms["telegram"].tool_progress == "off"
    assert got.platforms["slack"].tool_progress == "all"
    _write(
        scope,
        "platforms:\n  telegram:\n    streaming: false\n    tool_progress: verbose\n",
    )
    got = load_gateway_settings(scope)
    assert got.platforms["telegram"].streaming is False
    assert got.platforms["telegram"].tool_progress == "verbose"


def test_every_chat_platform_has_a_config_section() -> None:
    # `indus doctor` and `config set` treat a key outside the schema as unknown; the web Channels
    # page writes `platforms.<name>.enabled` for every platform it lists (LINE, ntfy and IRC were
    # missing). The HTTP surfaces are configured under `gateway:` instead.
    from indus.foundation.config.schema import PlatformsSection
    from indus.gateway.registry import BUILTIN_NAMES

    under_gateway = {"api_server", "webhook"}
    missing = set(BUILTIN_NAMES) - under_gateway - set(PlatformsSection.model_fields)
    assert missing == set()
