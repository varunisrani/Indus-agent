"""The fifteen starter skills (P09-22): shape, validation, lint, names and seeding."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from indus.core.prompt import texts
from indus.foundation.scope import Scope
from indus.learning.skills.bundled_sync import read_manifest, sync_bundled
from indus.learning.skills.format import (
    LAYOUT_DIRS,
    MAX_SUPPORT_BYTES,
    SkillFrontmatter,
    check_skill_text,
)
from indus.learning.skills.lint import lint_skill
from indus.learning.skills.resolver import load_catalog
from tooling.checks.banned import load_patterns, scan_text

REPO = Path(__file__).resolve().parents[4]
SKILLS = REPO / "skills"

EXPECTED = {
    "git-workflow": "software-development",
    "code-review": "software-development",
    "systematic-debugging": "software-development",
    "test-first": "software-development",
    "github-cli": "software-development",
    "shell-scripting": "software-development",
    "project-onboarding": "software-development",
    "writing-clearly": "writing",
    "research-with-sources": "research",
    "data-wrangling": "data",
    "pdf-and-documents": "data",
    "scheduling-jobs": "productivity",
    "meeting-notes-to-actions": "productivity",
    "skill-authoring": "indus",
    "indus-guide": "indus",
}
SECTIONS = ["## When to use", "## Procedure", "## Pitfalls", "## Verification"]
OPTIONAL_LAST = "## Quick reference"
DESCRIPTION_SHOWN = 60


def _skill_md(name: str) -> Path:
    return SKILLS / EXPECTED[name] / name / "SKILL.md"


def parse_skill_md(text: str, name: str) -> tuple[SkillFrontmatter, str]:
    """P09-05's validation of an agent write (limits, name, description, body)."""
    parsed = check_skill_text(text, expected_name=name)
    return parsed.frontmatter, parsed.body


def test_exactly_the_starter_set() -> None:
    found = {p.parent.name: p.parent.parent.name for p in SKILLS.rglob("SKILL.md")}
    assert found == EXPECTED


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_frontmatter_valid(name: str) -> None:
    meta, _body = parse_skill_md(_skill_md(name).read_text(encoding="utf-8"), name)
    assert meta.name == name
    assert meta.license == "MIT"
    assert meta.version and meta.author
    assert len(meta.description) <= DESCRIPTION_SHOWN
    assert not meta.description.endswith(".")
    assert meta.metadata is not None and meta.metadata.indus is not None
    assert meta.metadata.indus.category == EXPECTED[name]
    assert meta.metadata.indus.tags


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_sections_in_our_order(name: str) -> None:
    _meta, body = parse_skill_md(_skill_md(name).read_text(encoding="utf-8"), name)
    headings = [line.rstrip() for line in body.splitlines() if line.startswith("## ")]
    if headings and headings[-1] == OPTIONAL_LAST:
        headings = headings[:-1]
    assert headings == SECTIONS
    assert re.search(r"^# \S", body, re.MULTILINE), "a level-one title opens the body"


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_lint_clean(name: str) -> None:
    path = _skill_md(name)
    assert lint_skill(path.read_text(encoding="utf-8"), path.parent) == []


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_support_files_in_allowed_dirs_and_linked(name: str) -> None:
    skill_dir = _skill_md(name).parent
    text = _skill_md(name).read_text(encoding="utf-8")
    for path in skill_dir.rglob("*"):
        if not path.is_file() or path.name == "SKILL.md":
            continue
        rel = path.relative_to(skill_dir)
        assert rel.parts[0] in LAYOUT_DIRS, rel
        assert path.stat().st_size <= MAX_SUPPORT_BYTES
        assert rel.as_posix() in text, f"{rel} is never mentioned in SKILL.md"


def test_only_some_skills_carry_references() -> None:
    with_refs = sorted(p.parent.name for p in SKILLS.rglob("references"))
    assert with_refs == ["git-workflow", "indus-guide", "skill-authoring"]


def test_no_banned_identifiers() -> None:
    patterns, problems = load_patterns()
    assert not problems
    hits = []
    for path in SKILLS.rglob("*"):
        if path.is_file():
            rel = path.relative_to(REPO).as_posix()
            hits += [(rel, m) for _, _, m, _ in scan_text(rel, patterns)]
            text = path.read_text(encoding="utf-8")
            hits += [(rel, m) for _, _, m, _ in scan_text(text, patterns)]
    assert hits == []


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_names_tools_we_ship(name: str) -> None:
    """Backticked snake_case words that look like tool names must be ours."""
    ours = {
        "terminal",
        "process_manage",
        "read_file",
        "write_file",
        "patch",
        "search_files",
        "web_search",
        "web_extract",
        "execute_code",
        "todo_list",
        "clarify",
        "memory",
        "session_search",
        "delegate_task",
        "skills_list",
        "skill_view",
        "skill_manage",
        "cronjob_manage",
        "vision_analyze",
    }
    body = _skill_md(name).read_text(encoding="utf-8")
    mentioned = set(re.findall(r"`([a-z]+_[a-z_]+)`", body))
    suffixes = ("_file", "_files", "_code", "_list", "_manage")
    tool_like = {m for m in mentioned if m.endswith(suffixes)}
    assert tool_like <= ours


def test_seeding_installs_exactly_the_starter_set(tmp_path: Path) -> None:
    home = tmp_path / "profile-home"
    report = sync_bundled(home, source=SKILLS)
    assert sorted(report.added) == sorted(EXPECTED)
    assert report.failed == []
    assert sorted(read_manifest(home)) == sorted(EXPECTED)
    for name, category in EXPECTED.items():
        assert (home / "skills" / category / name / "SKILL.md").is_file()


def test_seeded_set_resolves_and_help_skill_is_listed(scope: Scope) -> None:
    """P09-06's resolver loads every seeded skill; the prompt's help pointer names a real one."""
    sync_bundled(scope.home, source=SKILLS)
    catalog = load_catalog(scope)
    for name in EXPECTED:
        rec = catalog.resolve(name)
        assert rec.name == name and rec.category == EXPECTED[name]
    assert texts.HELP_SKILL_NAME in EXPECTED
