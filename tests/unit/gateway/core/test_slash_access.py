"""P15-10: slash-command admin tiers (K17)."""

from __future__ import annotations

from typing import Any

import pytest

from indus.gateway.core.slash_access import (
    FLOOR,
    SlashPolicy,
    policy_for,
    policy_from_extra,
    runnable,
    scope_of,
    tier_label,
)

EXTRA: dict[str, Any] = {
    "allow_admin_from": ["1", 2],
    "user_allowed_commands": "/Status, model",
    "group_allow_admin_from": "9",
    "group_user_allowed_commands": ["new"],
}


@pytest.mark.parametrize(
    ("extra", "chat_type", "user", "command", "allowed"),
    [
        # No admin list in a scope: tiers off, everyone runs everything.
        ({}, "dm", "5", "model", True),
        ({}, "group", None, "reload-mcp", True),
        ({"user_allowed_commands": "status"}, "dm", "5", "model", True),
        ({"group_allow_admin_from": "9"}, "dm", "5", "model", True),
        # DM scope gated.
        (EXTRA, "dm", "1", "model", True),
        (EXTRA, "dm", "2", "my-plugin-cmd", True),
        (EXTRA, "dm", "5", "status", True),
        (EXTRA, "dm", "5", "/MODEL", True),
        (EXTRA, "dm", "5", "new", False),
        (EXTRA, "dm", "5", "my-plugin-cmd", False),
        (EXTRA, "dm", "5", "help", True),
        (EXTRA, "dm", "5", "whoami", True),
        (EXTRA, "dm", None, "help", True),
        (EXTRA, "dm", None, "status", True),
        (EXTRA, "dm", None, "model", True),
        (EXTRA, "dm", "5", "", False),
        # Group scope gated separately; a DM admin is not a group admin.
        (EXTRA, "group", "9", "reload-mcp", True),
        (EXTRA, "group", "1", "model", False),
        (EXTRA, "group", "1", "new", True),
        (EXTRA, "group", "1", "status", False),
        (EXTRA, "thread", "1", "whoami", True),
        (EXTRA, "channel", "9", "model", True),
        # Admin-only gating with no user list: just the floor for others.
        ({"allow_admin_from": "1"}, "dm", "5", "status", False),
        ({"allow_admin_from": "1"}, "dm", "5", "help", True),
    ],
)
def test_tier_table(
    extra: dict[str, Any], chat_type: object, user: str | None, command: str, allowed: bool
) -> None:
    assert policy_for(extra, chat_type).can_run(user, command) is allowed


def test_no_admin_list_means_full_access() -> None:
    policy = policy_from_extra({}, "dm")
    assert policy == SlashPolicy(enabled=False)
    assert policy.is_admin(None) and policy.can_run("anyone", "anything")
    assert tier_label(policy, "x").startswith("full access")


def test_blank_chat_type_takes_the_gated_scope() -> None:
    only_dm = {"allow_admin_from": "1"}
    assert policy_for(only_dm, "").enabled
    assert policy_for(only_dm, None).admins == {"1"}
    only_group = {"group_allow_admin_from": "9"}
    assert policy_for(only_group, "").admins == {"9"}
    both = {"allow_admin_from": "1", "group_allow_admin_from": "9"}
    assert policy_for(both, "").admins == {"9"}
    assert not policy_for({}, "").enabled


def test_help_lists_only_runnable_commands() -> None:
    names = ["help", "model", "new", "status", "whoami", "my-plugin-cmd"]
    policy = policy_for(EXTRA, "dm")
    assert runnable(policy, "5", names) == ["help", "model", "status", "whoami"]
    assert runnable(policy, "1", names) == names
    assert runnable(policy_for({}, "dm"), "5", names) == names


def test_labels_and_scopes() -> None:
    policy = policy_for(EXTRA, "dm")
    assert tier_label(policy, "1").startswith("admin")
    assert tier_label(policy, "5").startswith("user")
    assert scope_of("DM") == "dm" and scope_of("private") == "dm"
    assert scope_of("forum") == "group" and scope_of("dm") == "dm"
    assert scope_of("") is None and scope_of(None) is None
    assert {"help", "whoami"} == FLOOR
    assert policy_from_extra(None, "group") == SlashPolicy(enabled=False)
    assert policy_from_extra({"allow_admin_from": 7}, "dm").admins == {"7"}
