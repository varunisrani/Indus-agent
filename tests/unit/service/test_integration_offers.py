"""Suggested jobs when an integration is connected (P24-46 follow-up, wave 45, D-16.981): the
blueprints waiting for it are offered with source ``integration``; nothing is scheduled."""

from __future__ import annotations

import pytest

from indus.cron.blueprints import CATALOGUE, Blueprint
from indus.cron.suggestions import SuggestionStore, store_path
from indus.foundation.scope import Scope
from indus.service import integration_offers
from indus.service.integration_offers import connected, notice_lines, wants

GITHUB = (
    'blueprint: {key: prs, title: Open pull requests, schedule: "every 120m", '
    'prompt: "List my open pull requests.", integrations: [GitHub], '
    "slots: []}\n"
)
NEEDS_INPUT = (
    'blueprint: {key: digest, title: Repo digest, schedule: "every 60m", '
    'prompt: "Digest {repo}.", integrations: ["mcp:github"], slots: [{name: repo, kind: text}]}\n'
)


def skill(scope: Scope, name: str, frontmatter: str) -> None:
    folder = scope.home / "skills" / name
    folder.mkdir(parents=True, exist_ok=True)
    text = f"---\nname: {name}\ndescription: d\n{frontmatter}---\n\n# {name}\n"
    (folder / "SKILL.md").write_text(text, encoding="utf-8")


def _bp(*tags: str) -> Blueprint:
    return Blueprint("k", "T", "", "every 60m", "p", integrations=tags)


def test_a_blueprint_waits_for_a_kind_a_name_or_both() -> None:
    assert wants(_bp("platform"), "platform", "telegram")
    assert wants(_bp("telegram"), "platform", "Telegram")
    assert wants(_bp("mcp:github"), "mcp", "github")
    assert not wants(_bp("mcp:github"), "platform", "github")
    assert not wants(_bp("platform"), "mcp", "files")
    assert not wants(_bp(), "platform", "telegram")


def test_the_starter_briefing_waits_for_a_chat_platform_only() -> None:
    waiting = [bp.key for bp in CATALOGUE if bp.integrations]
    assert waiting == ["morning-brief"]


def test_turning_a_platform_on_offers_the_briefing_once(scope: Scope) -> None:
    added = connected(scope, "platform", "telegram")
    assert [(s.dedup_key, s.source) for s in added] == [("blueprint:morning-brief", "integration")]
    assert added[0].spec["schedule"] == "0 8 * * 1-5"
    assert notice_lines(added) == [
        "Suggested job: Morning briefing (nothing runs until you accept it: "
        "`indus cron suggestions`)."
    ]
    assert connected(scope, "platform", "slack") == []  # one offer per blueprint
    store = SuggestionStore(scope)
    assert [s.status for s in store.all()] == ["pending"]  # offered, never scheduled


def test_a_dismissed_offer_never_returns(scope: Scope) -> None:
    store = SuggestionStore(scope)
    found_first = store.refresh()  # the starter set, found first by the refresh
    brief = next(s for s in found_first if s.dedup_key == "blueprint:morning-brief")
    store.dismiss(brief.id)
    assert connected(scope, "platform", "discord") == []
    found = [s for s in store.all() if s.dedup_key == "blueprint:morning-brief"]
    assert [s.status for s in found] == ["dismissed"]


def test_an_mcp_server_offers_the_skill_blueprints_waiting_for_it(scope: Scope) -> None:
    skill(scope, "gh", GITHUB)
    skill(scope, "digest", NEEDS_INPUT)
    assert connected(scope, "mcp", "gitlab") == []
    added = connected(scope, "mcp", "github")
    # the digest needs a repository first (no default): its /blueprint form asks for it
    assert [s.dedup_key for s in added] == ["blueprint:gh:prs"]
    assert added[0].source == "integration"


def test_nothing_waiting_writes_nothing(scope: Scope) -> None:
    assert connected(scope, "mcp", "files") == []
    assert connected(scope, "platform", "  ") == []
    assert integration_offers.offer_for(scope, "calendar", "work") == []
    assert not store_path(scope).exists()


def test_a_failing_offer_never_fails_the_connect(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(scope: Scope, kind: str, name: str) -> list[object]:
        raise OSError("disk full")

    monkeypatch.setattr(integration_offers, "offer_for", broken)
    assert connected(scope, "platform", "telegram") == []
