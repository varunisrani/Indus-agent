"""P14-13: the delivery target grammar (shared with P15)."""

from __future__ import annotations

import pytest

from indus.cron import targets
from indus.cron.targets import DeliveryTarget, first_concrete, parse_targets, pin_origin


def rendered(value: str | list[str] | None) -> list[str]:
    return [t.render() for t in parse_targets(value).targets]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ["local"]),
        ("", ["local"]),
        ("  ,  ", ["local"]),
        ("local", ["local"]),
        ("ORIGIN", ["origin"]),
        ("origin,all", ["origin", "all"]),
        ("telegram", ["telegram"]),
        ("Telegram:-100123", ["telegram:-100123"]),
        ("telegram:-100123:42", ["telegram:-100123:42"]),
        ("telegram:-100123:42, telegram:-100123:42 ,local", ["telegram:-100123:42", "local"]),
        ("slack:C0ABC, Slack:C0ABC", ["slack:C0ABC"]),
        ("slack:C0ABC,slack:c0abc", ["slack:C0ABC", "slack:c0abc"]),  # chat ids keep their case
        (["discord:1", "local,origin"], ["discord:1", "local", "origin"]),
        ("telegram:-100123:", ["telegram:-100123"]),
    ],
)
def test_parser_table(value: str | list[str] | None, expected: list[str]) -> None:
    parsed = parse_targets(value)
    assert parsed.ok
    assert [t.render() for t in parsed.targets] == expected


def test_explicit_target_fields() -> None:
    (target,) = parse_targets("telegram:-100123:42").targets
    assert target == DeliveryTarget(
        "telegram", "-100123", "42", is_explicit=True, raw="telegram:-100123:42"
    )
    assert target.is_concrete and not target.is_origin
    (home,) = parse_targets("discord").targets
    assert home.chat_id is None and not home.is_explicit and not home.is_concrete
    (origin,) = parse_targets("origin").targets
    assert origin.is_origin


def test_unknown_platform_is_reported_never_local() -> None:
    parsed = parse_targets("myspace, telegram:1, myspace:abc")
    assert [t.render() for t in parsed.targets] == ["telegram:1"]
    assert [(p.token, p.code) for p in parsed.problems] == [
        ("myspace", "unknown_platform"),
        ("myspace:abc", "unknown_platform"),
    ]
    only_bad = parse_targets("myspace")
    assert only_bad.targets == () and not only_bad.ok


def test_the_p16_e6_to_e8_chat_platforms_are_targets_and_the_graph_receiver_is_not() -> None:
    names = ("teams", "google_chat", "qqbot", "dingtalk", "wecom", "wecom_callback")
    parsed = parse_targets([f"{name}:chat-1" for name in names])
    assert parsed.ok and [t.platform for t in parsed.targets] == list(names)
    assert [p.code for p in parse_targets("msgraph:sub-1").problems] == ["unknown_platform"]


@pytest.mark.parametrize("token", [":123", "telegram:", "local:abc", "all:x"])
def test_bad_addresses(token: str) -> None:
    parsed = parse_targets(token)
    assert parsed.problems and parsed.problems[0].code == "bad_address"


def test_registered_platform_with_its_own_splitter() -> None:
    def matrix_like(address: str) -> tuple[str, str | None]:
        room, _, thread = address.partition("|")
        return room, thread or None

    assert not parse_targets("chatnet:x").ok
    undo = targets.register_platform("ChatNet", splitter=matrix_like)
    try:
        assert rendered("chatnet:!room:server.org|t1") == ["chatnet:!room:server.org:t1"]
        (t,) = parse_targets("chatnet:!room:server.org").targets
        assert (t.chat_id, t.thread_id) == ("!room:server.org", None)
    finally:
        undo()
    assert not parse_targets("chatnet:x").ok


def test_register_platform_refuses_reserved_names() -> None:
    for name in ("local", "origin", "ALL", "a:b", ""):
        with pytest.raises(ValueError):
            targets.register_platform(name)


def test_first_concrete_resolves_origin() -> None:
    origin = {"platform": "telegram", "chat_id": "-5", "thread_id": "9"}
    assert first_concrete("origin,slack:C1", origin) == DeliveryTarget("telegram", "-5", "9")
    assert first_concrete("origin,slack:C1", None) == parse_targets("slack:C1").targets[0]
    assert first_concrete("local,discord", None) is None


def test_cron_created_job_origin_is_pinned() -> None:
    parent = DeliveryTarget("telegram", "-100123", "42")
    assert pin_origin("origin", parent) == "telegram:-100123:42"
    assert pin_origin(None, parent) == "telegram:-100123:42"  # omitted counts as origin
    assert pin_origin("origin,local", None) == "local"  # no concrete parent: local, deduped
    assert pin_origin("slack:C1, origin", parent) == "slack:C1,telegram:-100123:42"
    home = DeliveryTarget("discord")
    assert pin_origin("origin", home) == "local"  # a home channel is not a pinned address
