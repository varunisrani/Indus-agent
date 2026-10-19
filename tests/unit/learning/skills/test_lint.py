"""Advisory skill linter (P09-05)."""

from __future__ import annotations

from pathlib import Path

from indus.learning.skills.lint import MAX_BODY_CHARS, MAX_REFERENCE_FILES, lint_skill

HEAD = "---\nname: demo\ndescription: A clean demo skill.\n---\n\n"
CLEAN = HEAD + (
    "## When to use\nWhen a tidy example is needed.\n\n## Procedure\n1. Read the file.\n"
    "2. Change it.\n\n## Pitfalls\nNone worth noting.\n\n## Verification\nRun the tests.\n"
)


def rules(text: str, skill_dir: Path | None = None) -> list[str]:
    return [f.rule for f in lint_skill(text, skill_dir)]


def test_clean_skill_has_no_findings(tmp_path: Path) -> None:
    (tmp_path / "references").mkdir()
    (tmp_path / "references" / "one.md").write_text("x")
    assert rules(CLEAN, tmp_path) == []


def test_incident_log_fires_on_dense_ticket_numbers() -> None:
    body = "Fixed in #1234. See PR 4521, issue #77, #9001 and PR #8888 for history.\n"
    assert rules(HEAD + body) == ["lint.incident-log"]


def test_incident_log_needs_five_refs() -> None:
    body = "Fixed in #1234. See PR 4521, issue #77 and #9001.\n"
    assert rules(HEAD + body) == []


def test_incident_log_ignores_sparse_refs_and_code() -> None:
    sparse = "Rule text. " * 120 + "#1 #22 #333 #4444 #55555"
    assert "lint.incident-log" not in rules(HEAD + sparse)
    fenced = "```\n#1234 #2345 #3456 #4567 #5678 #6789\n```\nPlain rules here.\n"
    assert rules(HEAD + fenced) == []
    anchors = "See [x](#123) and a&#1234; entity."  # anchors and entities are not tickets
    assert rules(HEAD + anchors) == []


def test_oversized_body() -> None:
    assert rules(HEAD + "x" * MAX_BODY_CHARS) == []
    assert rules(HEAD + "x" * (MAX_BODY_CHARS + 1)) == ["lint.oversized"]


def test_reference_sprawl(tmp_path: Path) -> None:
    refs = tmp_path / "references" / "deep"
    refs.mkdir(parents=True)
    for i in range(MAX_REFERENCE_FILES):
        (refs / f"n{i}.md").write_text("x")
    assert rules(CLEAN, tmp_path) == []
    (refs / "one-more.md").write_text("x")
    assert rules(CLEAN, tmp_path) == ["lint.sprawl"]


def test_broken_frontmatter_is_linted_as_plain_text() -> None:
    assert rules("---\nname: [x\n") == []
