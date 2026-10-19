"""P15-07: the one session-key builder; keys frozen by a golden file (R06.7)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.profiles import ID_PATTERN
from indus.gateway.core.events import Source
from indus.gateway.core.session_keys import (
    DEFAULT_NAMESPACE,
    build_session_key,
    key_namespace,
    origin_of,
)
from tests.unit.gateway.builders import replace, src

GOLDEN = Path(__file__).resolve().parents[3] / "fixtures" / "gateway" / "session_keys.golden.json"

DM = src("telegram", "100", "dm", user_id="100")
GROUP = src("telegram", "-200", "group", user_id="7")
SLACK = src("slack", "C1", "group", user_id="U1", scope_id="T1")
DISCORD = src("discord", "chan9", "group", user_id="u5", guild_id="g1")

# name -> (source, build options)
SHAPES: dict[str, tuple[Source, dict[str, Any]]] = {
    "dm": (DM, {}),
    "dm_named_profile": (DM, {"profile": "work"}),
    "dm_profile_default": (DM, {"profile": "default"}),
    "dm_no_chat_id": (replace(DM, chat_id=None), {}),
    "dm_no_ids": (replace(DM, chat_id=None, user_id=None), {}),
    "dm_thread": (replace(DM, thread_id="55"), {}),
    "dm_no_chat_id_thread": (replace(DM, chat_id=None, thread_id="55"), {}),
    "dm_prospective_ignored": (replace(DM, prospective_thread_id="999"), {}),
    "dm_alt_id_no_chat": (replace(DM, chat_id=None, user_id_alt="alt-1"), {}),
    "group_per_user": (GROUP, {}),
    "group_shared": (GROUP, {"group_sessions_per_user": False}),
    "group_alt_id": (replace(GROUP, user_id_alt="alt-7"), {}),
    "group_no_user": (replace(GROUP, user_id=None), {}),
    "forum_topic_shared": (replace(GROUP, chat_type="forum", thread_id="12"), {}),
    "thread_shared": (replace(GROUP, chat_type="thread", thread_id="12"), {}),
    "thread_per_user": (
        replace(GROUP, chat_type="thread", thread_id="12"),
        {"thread_sessions_per_user": True},
    ),
    "thread_per_user_group_off": (
        replace(GROUP, chat_type="thread", thread_id="12"),
        {"thread_sessions_per_user": True, "group_sessions_per_user": False},
    ),
    "channel": (replace(GROUP, chat_type="channel"), {}),
    "slack_scope": (SLACK, {}),
    "slack_other_workspace": (replace(SLACK, scope_id="T2"), {}),
    "slack_thread": (replace(SLACK, thread_id="1700000000.0001"), {}),
    "discord_guild_not_in_key": (DISCORD, {}),
    "discord_prospective_thread": (replace(DISCORD, prospective_thread_id="th77"), {}),
    "discord_real_thread": (
        replace(DISCORD, chat_id="chan9", chat_type="thread", thread_id="th77"),
        {},
    ),
    "enum_fields_named_profile": (replace(GROUP, chat_type="group"), {"profile": "main"}),
}


def _keys() -> dict[str, str]:
    return {name: build_session_key(s, **opts) for name, (s, opts) in SHAPES.items()}


def test_keys_match_the_golden_file() -> None:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert len(golden) >= 24
    assert _keys() == golden


def test_namespace() -> None:
    assert key_namespace(None) == DEFAULT_NAMESPACE == "@default"
    assert key_namespace("") == "@default"
    assert key_namespace("default") == "@default"
    assert key_namespace("work") == "work"
    # No valid profile id can produce the default namespace.
    assert not ID_PATTERN.match(DEFAULT_NAMESPACE)


def test_discord_follow_up_in_thread_matches_the_spawning_message() -> None:
    spawning = build_session_key(replace(DISCORD, prospective_thread_id="th77"))
    follow_up = build_session_key(src("discord", "chan9", "thread", user_id="u5", thread_id="th77"))
    assert spawning == follow_up
    # A real thread id beats a stale prospective one.
    both = replace(DISCORD, chat_type="thread", thread_id="real1", prospective_thread_id="th77")
    assert build_session_key(both).endswith(":real1")


def test_guild_id_never_in_any_key() -> None:
    for name, (source, opts) in SHAPES.items():
        if source.guild_id:
            assert source.guild_id not in build_session_key(source, **opts).split(":"), name


def test_two_slack_workspaces_same_channel_differ() -> None:
    assert build_session_key(SLACK) != build_session_key(replace(SLACK, scope_id="T2"))
    # scope ids are Slack-only.
    assert "T1" not in build_session_key(replace(GROUP, scope_id="T1"))


def test_profiles_never_share_a_key() -> None:
    keys = {build_session_key(DM, profile=p) for p in (None, "work", "play", "main")}
    assert len(keys) == 4


@pytest.mark.parametrize("blank", ["", None])
def test_blank_fields_leave_no_holes(blank: str | None) -> None:
    key = build_session_key(src("telegram", blank, "group", user_id=blank, thread_id=blank))
    assert "::" not in key and not key.endswith(":")


def test_origin_is_json_safe() -> None:
    source = replace(DISCORD, chat_type="thread", thread_id="t1", is_bot=True)
    origin = origin_of(source)
    assert json.loads(json.dumps(origin)) == origin
    assert origin["chat_type"] == "thread" and origin["is_bot"] is True
    assert "user_name" not in origin and origin["guild_id"] == "g1"
