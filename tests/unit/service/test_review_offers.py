"""Suggested jobs from the background review (P24-46 wiring, D-16.799): a skill the review wrote
that declares a blueprint becomes one pending ``usage`` suggestion; nothing is scheduled."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from indus.core.transcript import TranscriptRow
from indus.cron.suggestions import SuggestionStore
from indus.foundation.scope import Scope
from indus.learning.review.fork import ReviewOutcome, ReviewRunner
from indus.learning.review.summary import written_skills
from indus.service import review_offers

PING = (
    'blueprint: {key: ping, title: Ping the service, schedule: "every {interval}m", '
    'prompt: "Ping the service and report.", slots: [{name: every, kind: interval, default: 30}]}\n'
)
NEEDS_INPUT = (
    'blueprint: {key: digest, title: Repo digest, schedule: "every 60m", '
    'prompt: "Digest {repo}.", slots: [{name: repo, kind: text}]}\n'
)


def skill(scope: Scope, name: str, frontmatter: str) -> None:
    folder = scope.home / "skills" / name
    folder.mkdir(parents=True, exist_ok=True)
    text = f"---\nname: {name}\ndescription: d\n{frontmatter}---\n\n# {name}\n"
    (folder / "SKILL.md").write_text(text, encoding="utf-8")


def call_rows(*calls: tuple[dict[str, Any], dict[str, Any]]) -> tuple[TranscriptRow, ...]:
    """One assistant row per ``skill_manage`` call and its result row."""
    rows: list[TranscriptRow] = []
    for index, (args, body) in enumerate(calls):
        ident = f"c{index}"
        rows.append(
            TranscriptRow(
                role="assistant",
                tool_calls=[
                    {
                        "id": ident,
                        "type": "function",
                        "function": {"name": "skill_manage", "arguments": json.dumps(args)},
                    }
                ],
            )
        )
        rows.append(TranscriptRow(role="tool", tool_call_id=ident, content=json.dumps(body)))
    return tuple(rows)


def done(*calls: tuple[dict[str, Any], dict[str, Any]], status: str = "done") -> ReviewOutcome:
    return ReviewOutcome(status, frozenset({"skills"}), rows=call_rows(*calls))


def test_written_skills_counts_only_applied_writes() -> None:
    rows = call_rows(
        ({"action": "create", "name": "pinger"}, {"success": True}),
        ({"action": "patch", "name": "staged"}, {"success": True, "staged": True}),
        ({"action": "edit", "name": "failed"}, {"success": False}),
        ({"action": "delete", "name": "gone"}, {"success": True}),
        (
            {
                "operations": [
                    {"action": "edit", "name": "batch-a"},
                    {"action": "create", "name": "batch-b"},
                ]
            },
            {"success": True, "results": [{"success": True}, {"success": True, "staged": True}]},
        ),
    )
    assert written_skills(rows) == ["pinger", "batch-a"]


def test_a_skill_the_review_wrote_offers_its_blueprint_once(scope: Scope) -> None:
    skill(scope, "pinger", PING)
    skill(scope, "digester", NEEDS_INPUT)
    skill(scope, "untouched", PING.replace("key: ping", "key: other"))
    wrote = done(
        ({"action": "create", "name": "pinger"}, {"success": True}),
        ({"action": "edit", "name": "digester"}, {"success": True}),
    )

    first = asyncio.run(review_offers.offer_from_review(scope, wrote))
    again = asyncio.run(review_offers.offer_from_review(scope, wrote))

    assert [(s.title, s.source) for s in first] == [("Ping the service", "usage")]
    assert again == []  # the dedup key holds it
    store = SuggestionStore(scope)
    pending = store.pending()
    assert [s.dedup_key for s in pending] == ["blueprint:pinger:ping"]
    assert pending[0].spec["schedule"] == "every 30m"
    store.dismiss("1")
    assert asyncio.run(review_offers.offer_from_review(scope, wrote)) == []
    assert store.pending() == []


def test_nothing_is_offered_without_a_finished_skill_write(scope: Scope) -> None:
    skill(scope, "pinger", PING)
    create = ({"action": "create", "name": "pinger"}, {"success": True})
    staged = ({"action": "create", "name": "pinger"}, {"success": True, "staged": True})
    for outcome in (done(create, status="cancelled"), done(staged), done()):
        assert asyncio.run(review_offers.offer_from_review(scope, outcome)) == []
    assert SuggestionStore(scope).all() == []


def test_listen_hangs_the_offer_on_the_runner(scope: Scope) -> None:
    skill(scope, "pinger", PING)
    runner = ReviewRunner.__new__(ReviewRunner)
    runner.listeners = []
    review_offers.listen(runner, scope)
    outcome = done(({"action": "create", "name": "pinger"}, {"success": True}))
    asyncio.run(runner._tell(outcome))  # pyright: ignore[reportPrivateUsage]
    assert [s.source for s in SuggestionStore(scope).pending()] == ["usage"]
