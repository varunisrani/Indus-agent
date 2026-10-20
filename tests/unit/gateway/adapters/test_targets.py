"""P16-32: send and cron target rules per platform (table per platform, round trip through P14)."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from indus.cron import targets as cron_targets
from indus.foundation.scope import Scope
from indus.gateway.adapters import targets
from indus.gateway.adapters._identity import hex_to_npub
from indus.gateway.config import HomeChannel
from indus.gateway.delivery.directory import ChannelDirectory
from indus.gateway.delivery.router import DeliveryRouter, Resolved
from indus.gateway.registry import BUILTIN_NAMES

PUBKEY = "ab" * 32
EVENT = "cd" * 32
UUID = "123e4567-e89b-12d3-a456-426614174000"
MM_ID = "k" * 26
LINE_USER = "U" + "4f" * 16

# (token, chat, thread): every accepted shape and how it reads.
VALID: list[tuple[str, str, str | None]] = [
    ("telegram:-1001234567890", "-1001234567890", None),
    ("telegram:-1001234567890:42", "-1001234567890", "42"),
    ("telegram:123456", "123456", None),
    ("telegram:@newsroom", "@newsroom", None),
    ("discord:123456789012345678", "123456789012345678", None),
    ("discord:123456789012345678:223456789012345678", "123456789012345678", "223456789012345678"),
    ("slack:C0123456789", "C0123456789", None),
    ("slack:C0123456789:1700000000.000100", "C0123456789", "1700000000.000100"),
    ("slack:U0123456789", "U0123456789", None),
    ("slack:<@U0123456789|alice>", "U0123456789", None),
    ("signal:+15551234567", "+15551234567", None),
    ("signal:group.Q2hhdEdyb3VwSWQ=", "group.Q2hhdEdyb3VwSWQ=", None),
    ("signal:" + UUID.upper(), UUID, None),
    ("matrix:!ops:example.org", "!ops:example.org", None),
    ("matrix:!ops:example.org:$root", "!ops:example.org", "$root"),
    ("matrix:!ops:example.org:8448", "!ops:example.org:8448", None),  # a port, not a thread
    ("matrix:!ops:example.org:8448:$root", "!ops:example.org:8448", "$root"),
    ("matrix:#ops:example.org", "#ops:example.org", None),
    ("matrix:#ops:example.org:$ev:example.org", "#ops:example.org", "$ev:example.org"),
    ("matrix:@alice:example.org", "@alice:example.org", None),
    ("mattermost:" + MM_ID, MM_ID, None),
    ("mattermost:" + MM_ID + ":" + "r" * 26, MM_ID, "r" * 26),
    ("mattermost:town-square", "#town-square", None),  # a name goes to the directory
    ("mattermost:" + "a1" * 13, "a1" * 13, None),  # 26 lower-case characters: read as an id
    ("email:Ops@Example.com", "ops@example.com", None),
    ("whatsapp_cloud:+1-555-123-4567", "15551234567", None),
    ("whatsapp_cloud:15551234567", "15551234567", None),
    ("sms:+15551234567", "+15551234567", None),
    ("line:" + LINE_USER, LINE_USER, None),
    ("line:C" + "0" * 32, "C" + "0" * 32, None),
    ("ntfy:alerts_prod-1", "alerts_prod-1", None),
    ("irc:#ops", "#ops", None),
    ("irc:alice", "alice", None),
    ("irc:&local", "&local", None),
    ("teams:19:abc@thread.tacv2", "19:abc@thread.tacv2", None),
    ("teams:19:abc@thread.tacv2;messageid=1700000000000", "19:abc@thread.tacv2", "1700000000000"),
    ("teams:19:abc@thread.tacv2:1700000000000", "19:abc@thread.tacv2", "1700000000000"),
    ("teams:a:1Xyz-abc_def", "a:1Xyz-abc_def", None),
    ("teams:a:1234567", "a:1234567", None),  # cutting :1234567 would leave no conversation
    ("teams:a:1234567:89", "a:1234567", "89"),
    (
        "teams:8:orgid:0f0e0d0c-0b0a-0908-0706-050403020100",
        "8:orgid:0f0e0d0c-0b0a-0908-0706-050403020100",
        None,
    ),
    ("google_chat:spaces/AAAA", "spaces/AAAA", None),
    ("google_chat:spaces/AAAA:BBBB", "spaces/AAAA", "spaces/AAAA/threads/BBBB"),
    ("google_chat:spaces/AAAA:spaces/AAAA/threads/BBBB", "spaces/AAAA", "spaces/AAAA/threads/BBBB"),
    ("google_chat:spaces/AAAA/threads/BBBB", "spaces/AAAA", "spaces/AAAA/threads/BBBB"),
    ("qqbot:ABCDEF0123", "ABCDEF0123", None),
    ("qqbot:c2c:ABCDEF0123", "ABCDEF0123", None),
    ("qqbot:group:ABCDEF0123", "group:ABCDEF0123", None),
    ("qqbot:channel:12345", "channel:12345", None),
    ("qqbot:dm:67890", "dm:67890", None),
    ("dingtalk:cidAbCdEf12==", "cidAbCdEf12==", None),
    ("dingtalk:user:manager1", "user:manager1", None),
    ("wecom:zhangsan", "zhangsan", None),
    ("wecom:wrkSFfCgAAxxxx", "wrkSFfCgAAxxxx", None),
    ("wecom_callback:zhangsan", "zhangsan", None),
    ("feishu:oc_abc123", "oc_abc123", None),
    ("feishu:oc_abc123:omt_1a2b", "oc_abc123", "omt_1a2b"),
    ("feishu:ou_abc123", "ou_abc123", None),
    ("feishu:cmt_docx_AbCd1234:whole", "cmt_docx_AbCd1234", "whole"),
    ("feishu:cmt_docx_AbCd1234:7012345678901234567", "cmt_docx_AbCd1234", "7012345678901234567"),
    ("bluebubbles:iMessage;-;+15551234567", "iMessage;-;+15551234567", None),
    ("bluebubbles:iMessage;+;chat123456", "iMessage;+;chat123456", None),
    ("bluebubbles:+15551234567", "+15551234567", None),
    ("bluebubbles:Friend@iCloud.com", "friend@icloud.com", None),
    ("simplex:12", "12", None),
    ("simplex:group:3", "group:3", None),
    ("buzz:" + UUID, UUID, None),
    ("buzz:" + UUID + ":" + EVENT, UUID, EVENT),
    ("buzz:" + PUBKEY, PUBKEY, None),
    ("buzz:" + hex_to_npub(PUBKEY), hex_to_npub(PUBKEY), None),
    # friendly names stay directory names on every platform
    ("discord:#general", "#general", None),
    ("discord:#general:223456789012345678", "#general", "223456789012345678"),
    ("telegram:#Team", "#Team", None),
    ("teams:#standup", "#standup", None),
    ("qqbot:#players", "#players", None),
    ("matrix:#ops", "#ops", None),
]

# (token, a word the message must contain)
INVALID: list[tuple[str, str]] = [
    ("telegram:abc", "telegram"),
    ("telegram:-100:topic", "telegram"),
    ("discord:general", "channel id"),
    ("discord:123", "discord"),
    ("slack:c0123456789", "slack"),
    ("slack:C0123456789:yesterday", "slack"),
    ("signal:5551234567", "group.<id>"),
    ("signal:+15551234567:1", "signal"),
    ("matrix:ops", "!room:server"),
    ("matrix:!ops:example.org:$", "matrix"),
    ("matrix:!ops", "matrix"),
    ("mattermost:" + MM_ID + ":thread", "root post id"),
    ("email:not-an-address", "email"),
    ("email:ops@example.com:1", "email"),
    ("email:Ops <ops@example.com>", "spaces"),
    ("whatsapp_cloud:12", "whatsapp_cloud"),
    ("whatsapp_cloud:alice", "whatsapp_cloud"),
    ("sms:5551234567", "E.164"),
    ("sms:+05551234567", "sms"),
    ("line:U123", "line"),
    ("line:X" + "0" * 32, "line"),
    ("ntfy:../etc", "ntfy"),
    ("ntfy:" + "a" * 65, "ntfy"),
    ("irc:#ops:thread", "irc"),
    ("irc:9lives", "irc"),
    ("teams:hello", "teams"),
    ("teams:19:abc@thread.tacv2;messageid=", "teams"),
    ("teams:19:abc@thread.tacv2;messageid=abc", "teams"),
    ("google_chat:rooms/AAAA", "google_chat"),
    ("google_chat:spaces/AAAA:spaces/BBBB/threads/CCCC", "another space"),
    ("qqbot:foo:bar", "qqbot"),
    ("qqbot:group:a:b", "qqbot"),
    ("qqbot:ABC:1", "qqbot"),
    ("dingtalk:abc", "dingtalk"),
    ("dingtalk:boss:abc", "dingtalk"),
    ("wecom:a:b", "wecom"),
    ("feishu:abc123", "feishu"),
    ("feishu:oc_abc:t:u", "feishu"),
    ("bluebubbles:hello", "bluebubbles"),
    ("simplex:alice", "simplex"),
    ("simplex:group:alice", "simplex"),
    ("buzz:" + PUBKEY + ":" + EVENT, "buzz"),
    ("buzz:" + UUID + ":xyz", "buzz"),
    ("buzz:npub1qqqqqqqq", "buzz"),
    ("telegram:a\tb", "spaces"),
]


@pytest.fixture
def installed() -> Iterator[None]:
    undo = targets.install()
    try:
        yield
    finally:
        undo()


@pytest.mark.parametrize(("token", "chat", "thread"), VALID, ids=[v[0] for v in VALID])
def test_valid_rows(installed: None, token: str, chat: str, thread: str | None) -> None:
    parsed = cron_targets.parse_targets(token)
    assert parsed.ok, parsed.problems
    (target,) = parsed.targets
    assert (target.chat_id, target.thread_id) == (chat, thread)
    assert target.is_explicit
    # round trip: P14's rendering reads back as the same target
    again = cron_targets.parse_targets(target.render())
    assert again.ok and again.targets[0].key == target.key
    assert targets.validate_target(target.platform, chat, thread) is None


@pytest.mark.parametrize(("token", "word"), INVALID, ids=[v[0] for v in INVALID])
def test_invalid_rows(installed: None, token: str, word: str) -> None:
    parsed = cron_targets.parse_targets(token)
    assert not parsed.targets
    (problem,) = parsed.problems
    assert problem.code == "bad_address"
    assert word in problem.message


def test_every_chat_platform_has_a_rule() -> None:
    not_targets = {"api_server", "webhook", "msgraph"}
    assert set(targets.RULES) == set(BUILTIN_NAMES) - not_targets
    for name, rule in targets.RULES.items():
        assert rule.platform == name and rule.forms and rule.examples
        assert cron_targets.is_known_platform(name), name
        for example in rule.examples:
            platform, _, address = example.partition(":")
            assert platform == name
            targets.parse_target(name, address)  # every documented example parses


def test_whatsapp_cloud_is_known_without_install() -> None:
    assert "whatsapp_cloud" in cron_targets.KNOWN_PLATFORMS
    parsed = cron_targets.parse_targets("whatsapp_cloud:15551234567")
    assert parsed.ok and parsed.targets[0].chat_id == "15551234567"


def test_install_is_idempotent_and_undone() -> None:
    before = cron_targets.parse_targets("matrix:!ops:example.org").targets[0]
    assert (before.chat_id, before.thread_id) == ("!ops", "example.org")  # P14's plain split
    undo = targets.install()
    second = targets.install()
    after = cron_targets.parse_targets("matrix:!ops:example.org").targets[0]
    assert (after.chat_id, after.thread_id) == ("!ops:example.org", None)
    second()
    undo()
    plain = cron_targets.parse_targets("matrix:!ops:example.org").targets[0]
    assert plain.chat_id == "!ops"
    # a platform without a rule keeps P14's split
    assert targets.parse_target("homeassistant", "light.kitchen:x") == ("light.kitchen", "x")


def test_duplicates_collapse_on_the_canonical_form(installed: None) -> None:
    parsed = cron_targets.parse_targets(
        "email:Ops@Example.com, email:ops@example.com, qqbot:c2c:ABC, qqbot:ABC"
    )
    assert [t.render() for t in parsed.targets] == ["email:ops@example.com", "qqbot:ABC"]


def test_parse_and_validate_helpers() -> None:
    assert targets.parse_target("matrix", " !a:b.org:$e ") == ("!a:b.org", "$e")
    with pytest.raises(targets.TargetError, match="sms target"):
        targets.parse_target("sms", "12345")
    assert targets.render_target("Teams", "19:x@thread.v2", "5") == "teams:19:x@thread.v2:5"
    assert targets.validate_target("telegram", "-100", "7") is None
    assert targets.validate_target("telegram", "", None) == "no chat id"
    assert "reads as" in (targets.validate_target("telegram", "1:2", None) or "")
    assert "reads as" in (targets.validate_target("email", "Ops@Example.com", None) or "")
    assert "sms target" in (targets.validate_target("sms", "12345", None) or "")
    assert targets.hash_is_native("irc") and targets.hash_is_native("MATRIX")
    assert not targets.hash_is_native("discord") and not targets.hash_is_native("nope")
    assert targets.rule_for("api_server") is None


def test_long_addresses_are_shortened_in_messages() -> None:
    with pytest.raises(targets.TargetError) as caught:
        targets.parse_target("ntfy", "x" * 300)
    assert "..." in str(caught.value) and len(str(caught.value)) < 200


def _directory(scope: Scope, platform: str, entries: list[dict[str, str]]) -> ChannelDirectory:
    path = scope.home / "gateway" / "channel_directory.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"platforms": {platform: entries}}), encoding="utf-8")
    return ChannelDirectory(scope).load()


def test_matrix_alias_resolves_through_the_directory(installed: None, scope: Scope) -> None:
    # a room without a name is listed under its canonical alias (the routes index's chat name)
    directory = _directory(
        scope,
        "matrix",
        [{"id": "!abc123:example.org", "name": "#ops:example.org", "type": "group"}],
    )
    router = DeliveryRouter(scope, homes=lambda _p: None, directory=directory)
    (target,) = cron_targets.parse_targets("matrix:#ops:example.org").targets
    assert router.resolve(target) == Resolved("matrix", "!abc123:example.org", None)
    (threaded,) = cron_targets.parse_targets("matrix:#ops:example.org:$root").targets
    assert router.resolve(threaded) == Resolved("matrix", "!abc123:example.org", "$root")
    # an alias the directory does not know is a real Matrix id: sent as written (the adapter
    # resolves it with the homeserver)
    (unknown,) = cron_targets.parse_targets("matrix:#other:example.org").targets
    assert router.resolve(unknown) == Resolved("matrix", "#other:example.org", None)


def test_without_the_rule_the_alias_is_cut_wrongly(scope: Scope) -> None:
    (target,) = cron_targets.parse_targets("matrix:#ops:example.org").targets
    assert (target.chat_id, target.thread_id) == ("#ops", "example.org")


def test_hash_names_stay_directory_names(installed: None, scope: Scope) -> None:
    directory = _directory(
        scope,
        "mattermost",
        [
            {"id": MM_ID, "name": "town-square", "type": "channel"},
            {"id": "m" * 26, "name": "town-hall", "type": "channel"},
        ],
    )
    homes = {"mattermost": HomeChannel("mattermost", "h" * 26)}
    router = DeliveryRouter(scope, homes=homes.get, directory=directory)
    (named,) = cron_targets.parse_targets("mattermost:town-square").targets
    assert router.resolve(named) == Resolved("mattermost", MM_ID, None)
    # ambiguous: "town" is a prefix of two names, so nothing is picked
    (vague,) = cron_targets.parse_targets("mattermost:town").targets
    assert str(router.resolve(vague)).startswith("unknown_channel")
    (home,) = cron_targets.parse_targets("mattermost").targets
    assert router.resolve(home) == Resolved("mattermost", "h" * 26, None)
