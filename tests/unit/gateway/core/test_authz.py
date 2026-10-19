"""P15-08: the authorization chain, unknown-sender behaviour and mention gating (K14, R06.8)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.gateway.core.authz import (
    Authorizer,
    AuthzDecision,
    MentionPolicy,
    PlatformPolicy,
    Response,
    UnauthorizedHandler,
    allow_set,
    decline_text,
    should_answer,
    try_later_text,
)
from indus.gateway.core.bot_loop import BotLoopGuard, BotLoopSettings, conversation_of
from indus.gateway.core.events import Source
from indus.gateway.registry import PlatformEntry, PlatformRegistry
from indus.state.pairing import DECLINE_WINDOW_SECONDS, PairingStore
from tests.fakes.clock import FakeClock
from tests.unit.gateway.builders import PolicyAdapter, policy_adapter, replace, src

T = "telegram"
DM = src(T, "100", "dm", user_id="100", user_name="Ada")
GROUP = src(T, "-200", "group", user_id="7", user_name="Bo")


def _registry() -> PlatformRegistry:
    registry = PlatformRegistry()
    registry.register(
        PlatformEntry(
            name="ntfy",
            label="ntfy",
            factory=lambda c: None,
            check_fn=lambda: True,
            allowlist_env="INDUS_NTFY_ALLOWED_USERS",
            dm_is_private=False,
        )
    )
    registry.register(
        PlatformEntry(
            name="grouped",
            label="Grouped",
            factory=lambda c: None,
            check_fn=lambda: True,
            allowlist_env="INDUS_GROUPED_ALLOWED_USERS",
            group_users_env="INDUS_GROUPED_GROUP_USERS",
            group_chats_env="INDUS_GROUPED_GROUP_CHATS",
        )
    )
    registry.register(
        PlatformEntry(
            name="homebus",
            label="Home bus",
            factory=lambda c: None,
            check_fn=lambda: True,
            trusted_inbound=True,
        )
    )
    return registry


@dataclass
class Setup:
    env: dict[str, str] = field(default_factory=dict[str, str])
    extra: dict[str, Any] = field(default_factory=dict[str, Any])
    paired: set[tuple[str, str]] = field(default_factory=set[tuple[str, str]])
    adapter: PolicyAdapter | None = None
    global_dm: object = None
    guard: BotLoopGuard | None = None

    def build(self) -> Authorizer:
        return Authorizer.from_registry(
            _registry(),
            env=self.env.get,
            extra_for=lambda p: self.extra,
            adapter_for=lambda p: self.adapter,
            is_paired=lambda p, u: (p, u) in self.paired,
            global_unknown_dm=lambda: self.global_dm,
            bot_guard=self.guard,
            profile="work",
        )


A_ALL = "INDUS_TELEGRAM_ALLOW_ALL_USERS"
A_LIST = "INDUS_TELEGRAM_ALLOWED_USERS"
A_BOTS = "INDUS_TELEGRAM_ALLOW_BOTS"
G_LIST = "INDUS_GATEWAY_ALLOWED_USERS"
G_ALL = "INDUS_GATEWAY_ALLOW_ALL_USERS"

# (row id, source, setup, granted, deciding step)
ROWS: list[tuple[str, Source, Setup, bool, str]] = [
    # 1. trusted platforms
    ("webhook", src("webhook", "r1", "dm"), Setup(), True, "trusted_platform"),
    ("api_server", src("api_server", None, "dm"), Setup(), True, "trusted_platform"),
    ("plugin_trusted", src("homebus", "x", "dm"), Setup(), True, "trusted_platform"),
    (
        "unknown_platform",
        src("nope", "1", "dm", user_id="1"),
        Setup(env={G_ALL: "1"}),
        False,
        "unknown_platform",
    ),
    # 2. chat-scoped grants
    ("upstream", DM, Setup(adapter=policy_adapter(auth_upstream=True)), True, "upstream"),
    (
        "upstream_no_user",
        replace(DM, user_id=None),
        Setup(adapter=policy_adapter(auth_upstream=True)),
        True,
        "upstream",
    ),
    (
        "upstream_truthy_not_true",
        DM,
        Setup(adapter=policy_adapter(auth_upstream=1)),  # type: ignore[arg-type]
        False,
        "default_deny",
    ),
    (
        "group_chat_env",
        replace(GROUP, user_id=None),
        Setup(env={"INDUS_GROUPED_GROUP_CHATS": "-200"}),
        False,
        "no_user_id",
    ),
    (
        "group_chat_config",
        replace(GROUP, user_id=None),
        Setup(extra={"group_allowed_chats": ["-200"]}),
        True,
        "group_chat_allowlist",
    ),
    (
        "group_chat_config_other",
        replace(GROUP, user_id=None),
        Setup(extra={"group_allowed_chats": "-999"}),
        False,
        "no_user_id",
    ),
    (
        "group_chat_star",
        GROUP,
        Setup(extra={"group_allowed_chats": "*"}),
        True,
        "group_chat_allowlist",
    ),
    (
        "group_chat_in_dm_ignored",
        DM,
        Setup(extra={"group_allowed_chats": "100"}),
        False,
        "default_deny",
    ),
    (
        "bot_all",
        replace(GROUP, is_bot=True, user_id=None),
        Setup(env={A_BOTS: "all"}),
        True,
        "bot_allowed",
    ),
    (
        "bot_mentions",
        replace(GROUP, is_bot=True),
        Setup(env={A_BOTS: "Mentions"}),
        True,
        "bot_allowed",
    ),
    (
        "bot_config_wins",
        replace(GROUP, is_bot=True),
        Setup(env={A_BOTS: "all"}, extra={"allow_bots": "none"}),
        False,
        "default_deny",
    ),
    ("bot_none", replace(GROUP, is_bot=True), Setup(env={A_BOTS: "none"}), False, "default_deny"),
    ("bot_unset", replace(GROUP, is_bot=True, user_id=None), Setup(), False, "no_user_id"),
    ("human_ignores_allow_bots", DM, Setup(env={A_BOTS: "all"}), False, "default_deny"),
    # 3. no user id
    ("no_user_dm", replace(DM, user_id=None), Setup(env={A_ALL: "true"}), False, "no_user_id"),
    ("no_user_group", replace(GROUP, user_id=""), Setup(env={G_ALL: "1"}), False, "no_user_id"),
    # 4. platform allow-all
    ("allow_all_true", DM, Setup(env={A_ALL: "true"}), True, "platform_allow_all"),
    ("allow_all_yes", GROUP, Setup(env={A_ALL: "YES"}), True, "platform_allow_all"),
    ("allow_all_false", DM, Setup(env={A_ALL: "false"}), False, "default_deny"),
    (
        "allow_all_beats_list",
        DM,
        Setup(env={A_ALL: "1", A_LIST: "999"}),
        True,
        "platform_allow_all",
    ),
    # 5. role grant
    ("role", replace(GROUP, role_authorized=True), Setup(env={A_LIST: "999"}), True, "role"),
    ("role_not_true", replace(GROUP, role_authorized=1), Setup(), False, "default_deny"),  # type: ignore[arg-type]
    # 6. pairing
    ("paired", DM, Setup(paired={(T, "100")}, env={A_LIST: "999"}), True, "paired"),
    ("paired_other_platform", DM, Setup(paired={("discord", "100")}), False, "default_deny"),
    # 7. no env allowlist
    (
        "own_policy_allowlist_yes",
        DM,
        Setup(
            adapter=policy_adapter(
                owns_access_policy=True, policies={"dm": "allowlist"}, dm_allow={"100"}
            )
        ),
        True,
        "own_policy",
    ),
    (
        "own_policy_allowlist_no",
        DM,
        Setup(
            adapter=policy_adapter(
                owns_access_policy=True, policies={"dm": "allowlist"}, dm_allow={"5"}
            )
        ),
        False,
        "own_policy",
    ),
    (
        "own_policy_group_allowlist",
        GROUP,
        Setup(adapter=policy_adapter(owns_access_policy=True, policies={"group": "Allowlist"})),
        True,
        "own_policy",
    ),
    (
        "own_policy_open_no_verdict",
        DM,
        Setup(adapter=policy_adapter(owns_access_policy=True, policies={"dm": "open"})),
        False,
        "default_deny",
    ),
    (
        "own_policy_open_then_global",
        DM,
        Setup(
            env={G_ALL: "1"},
            adapter=policy_adapter(owns_access_policy=True, policies={"dm": "open"}),
        ),
        True,
        "global_allow_all",
    ),
    (
        "own_policy_flag_off",
        DM,
        Setup(
            adapter=policy_adapter(
                owns_access_policy=False, policies={"dm": "allowlist"}, dm_allow={"100"}
            )
        ),
        False,
        "default_deny",
    ),
    ("config_allow_from", DM, Setup(extra={"allow_from": ["100"]}), True, "config_allowlist"),
    (
        "config_allow_from_json",
        DM,
        Setup(extra={"allow_from": '["5", "100"]'}),
        True,
        "config_allowlist",
    ),
    ("config_allow_from_star", DM, Setup(extra={"allow_from": "*"}), True, "config_allowlist"),
    (
        "config_dm_list_not_for_group",
        GROUP,
        Setup(extra={"allow_from": "7"}),
        False,
        "default_deny",
    ),
    (
        "config_group_allow_from",
        GROUP,
        Setup(extra={"group_allow_from": "7, 8"}),
        True,
        "config_allowlist",
    ),
    (
        "config_alt_id",
        replace(DM, user_id_alt="alt9"),
        Setup(extra={"allow_from": "alt9"}),
        True,
        "config_allowlist",
    ),
    ("global_allow_all", DM, Setup(env={G_ALL: "on"}), True, "global_allow_all"),
    ("nothing_configured", DM, Setup(), False, "default_deny"),
    # 8. env allowlists
    ("platform_list", DM, Setup(env={A_LIST: "5,100"}), True, "allowlist"),
    ("platform_list_star", DM, Setup(env={A_LIST: "*"}), True, "allowlist"),
    ("platform_list_miss", DM, Setup(env={A_LIST: "5"}), False, "default_deny"),
    ("global_list", DM, Setup(env={G_LIST: "100"}), True, "allowlist"),
    (
        "global_list_miss_ignores_global_all",
        DM,
        Setup(env={G_LIST: "5", G_ALL: "1"}),
        False,
        "default_deny",
    ),
    (
        "env_list_skips_config",
        DM,
        Setup(env={A_LIST: "5"}, extra={"allow_from": "100"}),
        False,
        "default_deny",
    ),
    (
        "group_users_list",
        src("grouped", "-2", "group", user_id="7"),
        Setup(env={"INDUS_GROUPED_GROUP_USERS": "7"}),
        True,
        "allowlist",
    ),
    (
        "group_users_list_not_for_dm",
        src("grouped", "7", "dm", user_id="7"),
        Setup(env={"INDUS_GROUPED_GROUP_USERS": "7"}),
        False,
        "default_deny",
    ),
    (
        "group_chats_env",
        src("grouped", "-2", "group", user_id="7"),
        Setup(env={"INDUS_GROUPED_GROUP_CHATS": "-2"}),
        True,
        "group_chat_allowlist",
    ),
    (
        "resolved_ids",
        DM,
        Setup(env={A_LIST: "ada_name"}, adapter=policy_adapter(resolved_ids={"100"})),
        True,
        "allowlist",
    ),
    (
        "resolved_ids_need_platform_list",
        DM,
        Setup(env={G_LIST: "x"}, adapter=policy_adapter(resolved_ids={"100"})),
        False,
        "default_deny",
    ),
    (
        "thread_counts_as_group",
        src(T, "-200", "thread", user_id="7", thread_id="9"),
        Setup(extra={"group_allow_from": "7"}),
        True,
        "config_allowlist",
    ),
]


@pytest.mark.parametrize(
    ("row", "source", "setup", "granted", "step"), ROWS, ids=[r[0] for r in ROWS]
)
def test_chain_order(row: str, source: Source, setup: Setup, granted: bool, step: str) -> None:
    decision = setup.build().check(source)
    assert (decision.granted, decision.step) == (granted, step), decision


def test_table_has_forty_plus_rows() -> None:
    assert len(ROWS) >= 40


def test_delegation_off_skips_adapter_grants() -> None:
    up = Setup(adapter=policy_adapter(auth_upstream=True)).build()
    assert not up.check(DM, allow_delegation=False).granted
    role = Setup().build()
    assert not role.check(replace(DM, role_authorized=True), allow_delegation=False).granted


def test_errors_fail_closed(caplog: pytest.LogCaptureFixture) -> None:
    def broken_env(name: str) -> str:
        raise RuntimeError("secret store down")

    authz = Authorizer.from_registry(_registry(), env=broken_env)
    with caplog.at_level(logging.ERROR):
        decision = authz.check(DM)
    assert decision == AuthzDecision(False, "error", "secret store down")

    def broken_policy(kind: str) -> str:
        raise RuntimeError("adapter")

    adapter = policy_adapter(owns_access_policy=True)
    adapter.access_policy = broken_policy  # type: ignore[method-assign]
    assert Setup(adapter=adapter).build().check(DM).step == "error"


def test_button_press_uses_the_same_chain() -> None:
    authz = Setup(env={A_LIST: "100"}).build()
    presser = src(T, "-200", "group", user_id="100")
    stranger = src(T, "-200", "group", user_id="666")
    assert authz.check(presser).granted
    assert not authz.check(stranger).granted


def test_bot_in_cooldown_is_refused_after_a_grant() -> None:
    now = [0.0]
    guard = BotLoopGuard(lambda: BotLoopSettings(max_messages=1), monotonic=lambda: now[0])
    bot = replace(GROUP, is_bot=True)
    authz = Setup(env={A_BOTS: "all"}, guard=guard).build()
    conv = conversation_of(bot, "work")
    assert guard.admit(conv) == "ok"
    assert authz.check(bot).granted
    assert guard.admit(conv) == "tripped"
    assert authz.check(bot) == AuthzDecision(False, "bot_cooldown", authz.check(bot).detail)
    # Asking many times never counts a message.
    for _ in range(10):
        authz.check(bot)
    now[0] += 601
    assert authz.check(bot).granted
    # Humans are never subject to the guard.
    assert Setup(env={A_LIST: "7"}, guard=guard).build().check(GROUP).granted


def test_allow_set_shapes() -> None:
    assert allow_set(None) == set()
    assert allow_set(" a, b ,,c ") == {"a", "b", "c"}
    assert allow_set(["1", 2, " "]) == {"1", "2"}
    assert allow_set('["x", "y"]') == {"x", "y"}
    assert allow_set("[broken") == {"[broken"}
    assert allow_set(42) == {"42"}


# -- unknown DM behaviour --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("setup", "mode", "why"),
    [
        (Setup(), "pair", "no allowlist"),
        (
            Setup(extra={"unauthorized_dm_behavior": "decline"}, env={A_LIST: "1"}),
            "decline",
            "platforms.telegram",
        ),
        (
            Setup(extra={"unauthorized_dm_behavior": "PAIR"}, env={A_LIST: "1"}),
            "pair",
            "platforms.telegram",
        ),
        (Setup(extra={"unauthorized_dm_behavior": "bogus"}), "pair", "no allowlist"),
        (Setup(global_dm="ignore"), "ignore", "gateway.unauthorized_dm_behavior"),
        (
            Setup(global_dm="decline", extra={"unauthorized_dm_behavior": "pair"}),
            "pair",
            "platforms.telegram",
        ),
        (Setup(global_dm="pair", env={A_LIST: "1"}), "ignore", "INDUS_TELEGRAM_ALLOWED_USERS"),
        (
            Setup(adapter=policy_adapter(policies={"dm": "pairing"}), env={A_LIST: "1"}),
            "pair",
            "DM policy is pairing",
        ),
        (
            Setup(adapter=policy_adapter(policies={"dm": "disabled"})),
            "ignore",
            "DM policy is disabled",
        ),
        (Setup(extra={"dm_policy": "allowlist"}), "ignore", "DM policy is allowlist"),
        (Setup(env={A_LIST: "1"}), "ignore", "INDUS_TELEGRAM_ALLOWED_USERS"),
        (Setup(env={G_LIST: "1"}), "ignore", "INDUS_GATEWAY_ALLOWED_USERS"),
        (Setup(extra={"allow_from": ["1"]}), "ignore", "platforms.telegram.extra.allow_from"),
        (Setup(extra={"group_allowed_chats": "-5"}), "ignore", "group_allowed_chats"),
        (Setup(env={A_ALL: "1"}), "pair", "no allowlist"),
    ],
)
def test_unknown_dm_mode(setup: Setup, mode: str, why: str) -> None:
    got = setup.build().unknown_dm_mode(T)
    assert got.mode == mode and why in got.why


def test_explain_names_the_mode() -> None:
    text = Setup(env={A_LIST: "1"}).build().explain(T)
    assert text.startswith("telegram: unknown direct messages → ignore (")


def test_any_allowlist_flips_pair_to_ignore() -> None:
    assert Setup().build().unknown_dm_mode(T).mode == "pair"
    for env in ({A_LIST: "1"}, {G_LIST: "1"}):
        assert Setup(env=env).build().unknown_dm_mode(T).mode == "ignore"
    grouped = Setup(env={"INDUS_GROUPED_GROUP_CHATS": "-1"}).build()
    assert grouped.unknown_dm_mode("grouped").mode == "ignore"


# -- the handler -----------------------------------------------------------------------------------


class Outbox:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.fail = fail

    async def __call__(self, text: str) -> None:
        self.sent.append(text)
        if self.fail:
            raise RuntimeError("platform said no")


def _handler(scope: Scope, setup: Setup) -> tuple[UnauthorizedHandler, PairingStore]:
    store = PairingStore(scope)
    return UnauthorizedHandler(setup.build(), store), store


def _respond(
    handler: UnauthorizedHandler,
    source: Source,
    out: Outbox,
    home: Outbox | None = None,
    home_chat: str | None = "home",
) -> Response:
    return asyncio.run(
        handler.respond(source, send_reply=out, notify_home=home, home_chat_id=home_chat)
    )


def test_pair_sends_a_code_once_then_try_later_once(scope: Scope) -> None:
    handler, store = _handler(scope, Setup())
    out = Outbox()
    first = _respond(handler, DM, out)
    assert first.action == "paired"
    assert "indus -p work pairing approve telegram " in out.sent[0]
    code = out.sent[0].split("approve telegram ", 1)[1].split()[0]
    assert store.approve_code(T, code).approved
    second = _respond(handler, replace(DM, user_id="101", chat_id="101"), out)
    assert second.action == "paired"
    rate = _respond(handler, replace(DM, user_id="101", chat_id="101"), out)
    assert rate.action == "try_later" and out.sent[-1] == try_later_text()
    quiet = _respond(handler, replace(DM, user_id="101", chat_id="101"), out)
    assert quiet.action == "silent" and len(out.sent) == 3


def test_groups_and_idless_are_silent(scope: Scope) -> None:
    handler, store = _handler(scope, Setup())
    out = Outbox()
    for source in (GROUP, replace(DM, user_id=None), src(T, "-1", "thread", user_id="5")):
        assert _respond(handler, source, out).action == "silent"
    assert out.sent == [] and store.list_pending() == []


def test_bot_never_gets_a_code(scope: Scope, caplog: pytest.LogCaptureFixture) -> None:
    handler, store = _handler(scope, Setup())
    out = Outbox()
    with caplog.at_level(logging.WARNING):
        got = _respond(handler, replace(DM, is_bot=True), out)
    assert got.action == "ignored" and out.sent == [] and store.list_pending() == []


def test_broadcast_dm_never_gets_a_code(scope: Scope) -> None:
    handler, store = _handler(scope, Setup())
    out = Outbox()
    got = _respond(handler, src("ntfy", "topic", "dm", user_id="anyone"), out)
    assert got.action == "ignored" and out.sent == [] and store.list_pending() == []
    # An adapter that says its DMs are public wins over the entry.
    handler2, store2 = _handler(scope, Setup(adapter=policy_adapter(dm_is_private=False)))
    assert _respond(handler2, DM, out).action == "ignored"
    assert store2.list_pending() == []


def test_decline_stamp_written_before_send(scope: Scope) -> None:
    setup = Setup(extra={"unauthorized_dm_behavior": "decline"})
    handler, store = _handler(scope, setup)
    failing = Outbox(fail=True)
    assert _respond(handler, DM, failing).action == "declined"
    assert failing.sent == [decline_text()]
    assert store.has_recent_decline(T, "100")
    again = _respond(handler, DM, failing)
    assert again.action == "silent" and len(failing.sent) == 1
    assert isinstance(scope.clock, FakeClock)
    scope.clock.advance(DECLINE_WINDOW_SECONDS)
    assert _respond(handler, DM, failing).action == "declined"
    assert len(failing.sent) == 2


def test_ignore_logs_and_notifies_home_once_per_sender(
    scope: Scope, caplog: pytest.LogCaptureFixture
) -> None:
    handler, store = _handler(scope, Setup(env={A_LIST: "1"}))
    out, home = Outbox(), Outbox()
    hostile = replace(DM, user_name="@everyone <b>[x](http://evil)</b>" + "z" * 100)
    with caplog.at_level(logging.WARNING):
        assert _respond(handler, hostile, out, home).action == "ignored"
        assert _respond(handler, hostile, out, home).action == "silent"
    assert out.sent == [] and store.list_pending() == []
    assert len(home.sent) == 1
    notice = home.sent[0]
    assert "100" in notice and "INDUS_TELEGRAM_ALLOWED_USERS" in notice
    assert "@" not in notice.split("from ", 1)[1].split(",")[0]
    assert "<" not in notice and "](" not in notice
    assert caplog.text.count("Ignored a direct message on telegram") == 1
    # A different sender is reported once too.
    assert _respond(handler, replace(DM, user_id="5", chat_id="5"), out, home).action == "ignored"
    assert len(home.sent) == 2


def test_ignore_never_answers_into_the_strangers_own_chat(scope: Scope) -> None:
    handler, _store = _handler(scope, Setup(env={A_LIST: "1"}))
    out, home = Outbox(), Outbox()
    assert _respond(handler, DM, out, home, home_chat="100").action == "ignored"
    assert home.sent == [] and out.sent == []


def test_home_notice_failure_is_contained(scope: Scope) -> None:
    handler, _store = _handler(scope, Setup(env={A_LIST: "1"}))
    assert _respond(handler, DM, Outbox(), Outbox(fail=True)).action == "ignored"


def test_seen_senders_are_bounded(scope: Scope) -> None:
    handler = UnauthorizedHandler(Setup().build(), PairingStore(scope), max_seen=2)
    assert handler.first_time(T, "a") and handler.first_time(T, "b")
    assert not handler.first_time(T, "a")
    assert handler.first_time(T, "c")  # evicts b (a was refreshed)
    assert handler.first_time(T, "b")


def test_pairing_reply_for_default_profile() -> None:
    authz = Authorizer.from_registry(_registry(), env=lambda n: None, profile="default")
    from indus.gateway.core.authz import pairing_reply

    text = pairing_reply(T, "ABCD2345", authz.profile)
    assert "indus pairing approve telegram ABCD2345" in text and "one hour" in text


# -- mention gating --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "mentioned", "extra", "bot_mode", "answer"),
    [
        (DM, False, {}, "none", True),
        (GROUP, False, {}, "none", False),
        (GROUP, True, {}, "none", True),
        (GROUP, False, {"require_mention": False}, "none", True),
        (GROUP, False, {"require_mention": "false"}, "none", True),
        (GROUP, False, {"free_response_channels": ["-200"]}, "none", True),
        (GROUP, False, {"free_response_channels": "-1,-2"}, "none", False),
        (
            src("discord", "c1", "thread", user_id="1", thread_id="t1", parent_chat_id="c0"),
            False,
            {"free_response_channels": "c0"},
            "none",
            True,
        ),
        (replace(GROUP, is_bot=True), False, {}, "mentions", False),
        (replace(GROUP, is_bot=True), True, {}, "mentions", True),
        (replace(GROUP, is_bot=True), False, {}, "all", True),
    ],
)
def test_mention_gating(
    source: Source, mentioned: bool, extra: Mapping[str, Any], bot_mode: str, answer: bool
) -> None:
    policy = MentionPolicy.from_extra(extra)
    assert should_answer(source, mentioned=mentioned, policy=policy, bot_mode=bot_mode) is answer


def test_mention_policy_defaults() -> None:
    assert MentionPolicy.from_extra(None) == MentionPolicy()
    assert Setup(env={A_BOTS: "ALL"}).build().bot_mode(T) == "all"
    assert Setup(env={A_BOTS: "weird"}).build().bot_mode(T) == "none"


def test_policy_from_entry_and_standard() -> None:
    entry = PlatformRegistry().get("discord")
    assert entry is not None
    policy = PlatformPolicy.from_entry(entry)
    assert policy.allowlist_env == "INDUS_DISCORD_ALLOWED_USERS"
    assert policy.allow_bots_env == "INDUS_DISCORD_ALLOW_BOTS" and policy.dm_is_private
    std = PlatformPolicy.standard("irc")
    assert std.allow_all_env == "INDUS_IRC_ALLOW_ALL_USERS" and not std.trusted_inbound
