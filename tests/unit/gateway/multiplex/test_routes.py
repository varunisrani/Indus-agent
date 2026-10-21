"""P24-03: profile routes, identity first, the intake and delivery resolvers."""

from __future__ import annotations

import json
import logging
import stat
from pathlib import Path
from typing import Any

import pytest

from indus.gateway.core.session_keys import profile_of_key
from indus.gateway.multiplex.routes import (
    RESEND_TEXT,
    AdapterTable,
    ProfileRoute,
    ProfileRouter,
    ReceivingBots,
    RouteQuery,
    RoutingIdentity,
    Unresolved,
    match_route,
    parse_routes,
)
from tests.unit.gateway.builders import src

DEFAULT_BOT = object()
B_BOT = object()
SERVED = {"default", "b", "ops", "guilds", "chats", "threads", "users", "c"}


def table(**bots: object) -> AdapterTable:
    adapters = AdapterTable()
    for profile, adapter in bots.items():
        adapters.register(profile, "telegram", adapter)
    return adapters


def router(routes: list[dict[str, Any]], adapters: AdapterTable, **kw: Any) -> ProfileRouter:
    parsed, problems = parse_routes(routes)
    assert problems == []
    kw.setdefault("served", lambda: SERVED)
    return ProfileRouter(parsed, adapters, **kw)


def ident(found: RoutingIdentity | Unresolved) -> RoutingIdentity:
    assert isinstance(found, RoutingIdentity), found
    return found


# -- matching --------------------------------------------------------------------------------------

LADDER = [
    {"name": "g", "platform": "telegram", "profile": "guilds", "guild": "G"},
    {"name": "c", "platform": "telegram", "profile": "chats", "chat": "C"},
    {"name": "t", "platform": "telegram", "profile": "threads", "thread": "T"},
    {"name": "u", "platform": "telegram", "profile": "users", "user": "U"},
]


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"guild_id": "G", "chat_id": "C", "thread_id": "T", "user_id": "U"}, "users"),
        ({"guild_id": "G", "chat_id": "C", "thread_id": "T", "user_id": "x"}, "threads"),
        ({"guild_id": "G", "chat_id": "C", "thread_id": "x", "user_id": "x"}, "chats"),
        ({"guild_id": "G", "chat_id": "x", "thread_id": "x", "user_id": "x"}, "guilds"),
        ({"guild_id": "x", "chat_id": "x", "thread_id": "x", "user_id": "x"}, "default"),
    ],
)
def test_specificity_table(fields: dict[str, str], expected: str) -> None:
    chat = fields.pop("chat_id")
    source = src("telegram", chat, "group", **fields)
    found = ident(router(LADDER, table(default=DEFAULT_BOT)).identify(source, DEFAULT_BOT))
    assert found.runtime_profile == expected and found.transport_profile == "default"


def test_weights_add_up_and_ties_keep_config_order() -> None:
    routes, _ = parse_routes(
        [
            {"name": "chat", "platform": "telegram", "profile": "a", "chat": "C"},
            {"name": "chat-guild", "platform": "telegram", "profile": "b", "chat": "C",
             "guild": "G"},
            {"name": "thread", "platform": "telegram", "profile": "c", "thread": "T"},
            {"name": "all3", "platform": "telegram", "profile": "d", "thread": "T", "chat": "C",
             "guild": "G"},
            {"name": "user", "platform": "telegram", "profile": "e", "user": "U"},
            {"name": "chat-2", "platform": "telegram", "profile": "f", "chat": "C"},
        ]
    )  # fmt: skip
    assert [r.specificity for r in routes] == [16, 14, 8, 6, 4, 4]
    assert [r.name for r in routes][-2:] == ["chat", "chat-2"]
    query = RouteQuery(platform="telegram", guild="G", chat="C", thread="T", user="U")
    assert [r.name for r in routes if r.matches(query)][0] == "user"
    found = match_route(routes, RouteQuery(platform="telegram", chat="C", guild="G"))
    assert found is not None and found.name == "chat-guild"


def test_every_named_discriminator_must_match() -> None:
    route = ProfileRoute(name="r", platform="telegram", profile="ops", chat="1", user="5")
    assert route.matches(RouteQuery(platform="telegram", chat="1", user="5"))
    assert not route.matches(RouteQuery(platform="telegram", chat="1", user="6"))
    assert not route.matches(RouteQuery(platform="slack", chat="1", user="5"))
    disabled = ProfileRoute(name="r", platform="telegram", profile="ops", chat="1", enabled=False)
    assert not disabled.matches(RouteQuery(platform="telegram", chat="1"))


def test_a_route_for_bot_a_is_ignored_for_bot_b() -> None:
    adapters = table(default=DEFAULT_BOT, b=B_BOT)
    shared = router([{"platform": "telegram", "profile": "ops", "chat": "100"}], adapters)
    message = src("telegram", "100", user_id="100")
    assert ident(shared.identify(message, DEFAULT_BOT)).runtime_profile == "ops"
    own = ident(shared.identify(message, B_BOT))
    assert (own.runtime_profile, own.transport_profile) == ("b", "b")

    for_b = router(
        [{"platform": "telegram", "profile": "ops", "chat": "100", "bot": "b"}], adapters
    )
    assert ident(for_b.identify(message, B_BOT)).runtime_profile == "ops"
    assert ident(for_b.identify(message, DEFAULT_BOT)).runtime_profile == "default"


def test_a_chat_route_matches_threads_under_it() -> None:
    routed = router(
        [{"platform": "telegram", "profile": "ops", "chat": "100"}], table(default=DEFAULT_BOT)
    )
    thread = src("telegram", "555", "thread", thread_id="555", parent_chat_id="100")
    assert ident(routed.identify(thread, DEFAULT_BOT)).runtime_profile == "ops"
    elsewhere = src("telegram", "556", "thread", thread_id="556", parent_chat_id="101")
    assert ident(routed.identify(elsewhere, DEFAULT_BOT)).runtime_profile == "default"


def test_parse_coerces_numbers_and_skips_bad_rows(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        routes, problems = parse_routes(
            [
                {"name": "num", "platform": "Telegram", "profile": "Ops", "chat": -100, "bot": "B"},
                {"name": "float", "platform": "telegram", "profile": "ops", "chat": 1.5},
                {"name": "flag", "platform": "telegram", "profile": "ops", "user": True},
                {"name": "blank", "platform": "telegram", "profile": "ops", "user": "  "},
                {"name": "bad", "platform": "telegram", "profile": "Bad Name!"},
                {"name": "half", "profile": "ops"},
                "not a mapping",
                {"name": "off", "platform": "telegram", "profile": "ops", "enabled": False},
            ]
        )
    assert [(r.name, r.platform, r.profile, r.bot, r.chat) for r in routes] == [
        ("num", "telegram", "ops", "b", "-100"),
        ("off", "telegram", "ops", "default", None),
    ]
    assert routes[1].enabled is False
    assert len(problems) == 6
    assert "quote it" in problems[0] and "gateway.profile_routes" in caplog.text
    assert parse_routes(None) == ([], [])
    assert parse_routes({"a": 1}) == ([], ["profile_routes must be a list"])


# -- identity --------------------------------------------------------------------------------------


def test_a_route_to_a_profile_not_served_is_unresolved() -> None:
    routed = router(
        [{"platform": "telegram", "profile": "ghost", "chat": "1"}], table(default=DEFAULT_BOT)
    )
    found = routed.identify(src("telegram", "1"), DEFAULT_BOT)
    assert found == Unresolved("unserved_profile", "ghost")


def test_an_unregistered_adapter_is_unresolved() -> None:
    routed = router([], table(default=DEFAULT_BOT))
    assert routed.identify(src("telegram", "1"), object()) == Unresolved("unknown_bot")


def test_the_receiving_bot_answers_a_routed_message() -> None:
    adapters = table(default=DEFAULT_BOT, b=B_BOT)
    routed = router([{"platform": "telegram", "profile": "ops", "chat": "1"}], adapters)
    found = ident(routed.identify(src("telegram", "1"), DEFAULT_BOT))
    assert found.route is not None and found.live
    assert routed.intake_adapter(found) is DEFAULT_BOT
    assert routed.delivery_adapter(found) is DEFAULT_BOT


def test_a_pinned_bot_that_is_offline_delivers_nothing() -> None:
    adapters = table(default=DEFAULT_BOT, b=B_BOT)
    routed = router([{"platform": "telegram", "profile": "b", "chat": "1"}], adapters)
    found = ident(routed.identify(src("telegram", "9"), B_BOT))
    adapters.unregister("b", "telegram")
    assert routed.intake_adapter(found) is None
    assert routed.delivery_adapter(found) is None  # never the default bot by guess


def test_a_restored_source_with_two_possible_owners_delivers_nowhere(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapters = table(default=DEFAULT_BOT, b=B_BOT)
    routes = [
        {"platform": "telegram", "profile": "ops", "chat": "1"},
        {"platform": "telegram", "profile": "ops", "chat": "2", "bot": "b"},
    ]
    routed = router(routes, adapters)
    restored = routed.restored_identity("ops:telegram:dm:1", "telegram")
    assert (restored.runtime_profile, restored.transport_profile, restored.live) == (
        "ops",
        None,
        False,
    )
    assert routed.owners("telegram", "ops") == ["b", "default"]
    with caplog.at_level(logging.WARNING):
        assert routed.delivery_adapter(restored) is None
        assert routed.delivery_adapter(restored) is None
    assert len([r for r in caplog.records if "no single bot" in r.message]) == 1

    adapters.unregister("b")
    assert routed.delivery_adapter(restored) is DEFAULT_BOT  # now the unique owner


def test_a_secondary_without_a_bot_never_borrows_the_default() -> None:
    adapters = table(default=DEFAULT_BOT)
    routed = router([], adapters)
    restored = routed.restored_identity("c:telegram:dm:1", "telegram")
    assert adapters.for_profile("c") == {}
    assert routed.delivery_adapter(restored) is None
    own = routed.restored_identity("@default:telegram:dm:1", "telegram")
    assert routed.delivery_adapter(own) is DEFAULT_BOT


def test_the_receiving_bot_survives_a_restart(tmp_path: Path) -> None:
    adapters = table(default=DEFAULT_BOT, b=B_BOT)
    routes = [{"platform": "telegram", "profile": "ops", "chat": "1", "bot": "b"}]
    first = router(routes, adapters, bots=ReceivingBots(tmp_path))
    message = src("telegram", "1", user_id="1")
    found = ident(first.identify(message, B_BOT))
    key = first.session_key(message, found)
    first.remember(key, found)

    again = router(routes, adapters, bots=ReceivingBots(tmp_path))
    restored = again.restored_identity(key, "telegram")
    assert (restored.runtime_profile, restored.transport_profile) == ("ops", "b")
    assert again.delivery_adapter(restored) is B_BOT
    path = tmp_path / "gateway" / "receiving_bots.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_receiving_bots_keep_the_newest_rows(tmp_path: Path) -> None:
    bots = ReceivingBots(tmp_path, limit=3)
    for n in range(4):
        bots.remember(f"k{n}", "b")
    reloaded = ReceivingBots(tmp_path, limit=3)
    assert [reloaded.get(f"k{n}") for n in range(4)] == [None, "b", "b", "b"]
    reloaded.forget("k3")
    assert ReceivingBots(tmp_path).get("k3") is None
    bots.path.write_text(json.dumps({"x": "Not An Id!", "y": "ops", "z": 3}))
    assert ReceivingBots(tmp_path).get("x") is None and ReceivingBots(tmp_path).get("y") == "ops"
    bots.path.write_text("garbage")
    assert ReceivingBots(tmp_path).get("y") is None


def test_session_keys_use_the_runtime_profiles_namespace() -> None:
    routed = router(
        [{"platform": "telegram", "profile": "ops", "chat": "1"}], table(default=DEFAULT_BOT)
    )
    routed_dm = src("telegram", "1", user_id="1")
    key = routed.session_key(routed_dm, ident(routed.identify(routed_dm, DEFAULT_BOT)))
    assert key == "ops:telegram:dm:1" and profile_of_key(key) == "ops"
    plain = src("telegram", "2", user_id="2")
    own = routed.session_key(plain, ident(routed.identify(plain, DEFAULT_BOT)))
    assert own == "@default:telegram:dm:2" and profile_of_key(own) == "default"
    assert profile_of_key("") == "default"


# -- dropping --------------------------------------------------------------------------------------


def test_unresolved_is_dropped_with_one_warning_and_one_resend(
    caplog: pytest.LogCaptureFixture,
) -> None:
    routed = router(
        [{"platform": "telegram", "profile": "ghost", "chat": "1"}], table(default=DEFAULT_BOT)
    )
    sent: list[tuple[str, str]] = []

    def notify(source: Any, text: str) -> None:
        sent.append((source.chat_id, text))

    message = src("telegram", "1")
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            unresolved = routed.identify(message, DEFAULT_BOT)
            assert isinstance(unresolved, Unresolved)
            routed.drop_unresolved(message, unresolved, acknowledged=True, notify=notify)
    assert sent == [("1", RESEND_TEXT)]
    assert len([r for r in caplog.records if "dropped" in r.message]) == 1

    unacked = src("telegram", "7")
    routed.drop_unresolved(unacked, Unresolved("unknown_bot"), acknowledged=False, notify=notify)
    assert len(sent) == 1


def test_a_target_that_resolves_again_is_warned_about_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    served = {"default"}
    routed = router(
        [{"platform": "telegram", "profile": "ops", "chat": "1"}],
        table(default=DEFAULT_BOT),
        served=lambda: served,
    )
    message = src("telegram", "1")
    notices: list[str] = []
    with caplog.at_level(logging.WARNING):
        miss = routed.identify(message, DEFAULT_BOT)
        assert isinstance(miss, Unresolved)
        routed.drop_unresolved(
            message, miss, acknowledged=True, notify=lambda _s, t: notices.append(t)
        )
        served.add("ops")
        ident(routed.identify(message, DEFAULT_BOT))
        served.discard("ops")
        miss = routed.identify(message, DEFAULT_BOT)
        assert isinstance(miss, Unresolved)
        routed.drop_unresolved(
            message, miss, acknowledged=True, notify=lambda _s, t: notices.append(t)
        )
    assert len(notices) == 2
    assert len([r for r in caplog.records if "dropped" in r.message]) == 2


def test_a_failing_resend_is_logged() -> None:
    routed = router([], table(default=DEFAULT_BOT))

    def broken(_source: Any, _text: str) -> None:
        raise RuntimeError("send failed")

    sent = routed.drop_unresolved(
        src("telegram", "1"), Unresolved("unknown_bot"), acknowledged=True, notify=broken
    )
    assert sent is False


def test_a_profile_with_its_own_bot_never_borrows_a_route_bot() -> None:
    adapters = table(default=DEFAULT_BOT)
    adapters.register("ops", "slack", object())
    routed = router([{"platform": "telegram", "profile": "ops", "chat": "1"}], adapters)
    restored = routed.restored_identity("ops:telegram:dm:1", "telegram")
    assert adapters.claims("ops") == {"slack"}
    assert routed.owners("telegram", "ops") == []
    assert routed.delivery_adapter(restored) is None

    own = AdapterTable()
    own.register("c", "telegram", B_BOT)
    own.register("default", "telegram", DEFAULT_BOT)
    satellite = router([{"platform": "telegram", "profile": "c", "chat": "1"}], own)
    own.unregister("c", "telegram")  # its bot went offline: still its own boundary
    assert satellite.owners("telegram", "c") == []
    own.unregister("c", release=True)  # unserved
    assert own.claims("c") == set()
    assert satellite.owners("telegram", "c") == ["default"]
