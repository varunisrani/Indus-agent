"""Skill format, limits and visibility (P09-05)."""

from __future__ import annotations

from pathlib import Path

import pytest

from indus.learning.skills import format as fmt
from indus.learning.skills.format import (
    MAX_DESCRIPTION_CHARS,
    MAX_SKILL_CHARS,
    MAX_SUPPORT_BYTES,
    SkillFormatError,
    VisibilityContext,
    check_category,
    check_name,
    check_skill_text,
    check_support_path,
    check_support_size,
    hidden_reason,
    parse_skill,
    split_frontmatter,
    visible,
)

INVALID = Path(__file__).resolve().parents[3] / "fixtures" / "skills" / "invalid"


def skill_text(
    name: str = "demo", description: str = "Does a thing.", body: str = "Body.\n"
) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}"


@pytest.mark.parametrize(
    ("name", "ok"),
    [
        ("a", True),
        ("0abc", True),
        ("git-workflow", True),
        ("v1.2_beta-x", True),
        ("a" * 64, True),
        ("a" * 65, False),
        ("", False),
        ("-lead", False),
        (".hidden", False),
        ("_under", False),
        ("Upper", False),
        ("with space", False),
        ("slash/name", False),
        ("ünï", False),
    ],
)
def test_name_rules(name: str, ok: bool) -> None:
    assert (check_name(name) is None) is ok


@pytest.mark.parametrize("word", ["on", "off", "yes", "no", "y", "n", "true", "false", "null"])
def test_yaml_boolean_names_refused(word: str) -> None:
    """YAML 1.1 reads these unquoted as booleans or null, so the name would not round-trip."""
    problem = check_name(word)
    assert problem is not None and "YAML 1.1" in problem
    assert check_category(word) is not None
    with pytest.raises(SkillFormatError, match="YAML"):
        parse_skill(skill_text(name=word))
    # quoted, the frontmatter parses, but an agent write is still refused by the name rule
    quoted = skill_text(name=f'"{word}"')
    assert parse_skill(quoted).frontmatter.name == word
    with pytest.raises(SkillFormatError, match="YAML 1.1"):
        check_skill_text(quoted)


@pytest.mark.parametrize("name", ["123", "1.5", "1_000", "0x1f"])
def test_numeric_names_refused(name: str) -> None:
    assert check_name(name) is not None


@pytest.mark.parametrize("name", ["off-switch", "no-op", "yes-man", "on-call", "2fa", "y2k"])
def test_words_containing_boolean_words_allowed(name: str) -> None:
    assert check_name(name) is None


def test_category_rules() -> None:
    assert check_category(None) is None
    assert check_category("software-development") is None
    problem = check_category("Bad Cat")
    assert problem is not None and "category" in problem


@pytest.mark.parametrize(("size", "ok"), [(MAX_DESCRIPTION_CHARS, True), (1025, False)])
def test_description_limit(size: int, ok: bool) -> None:
    text = skill_text(description="d" * size)
    if ok:
        assert check_skill_text(text).frontmatter.description == "d" * size
    else:
        with pytest.raises(SkillFormatError, match="description is 1,025"):
            check_skill_text(text)


def test_content_limit() -> None:
    base = skill_text(body="")
    exact = base + "x" * (MAX_SKILL_CHARS - len(base))
    assert len(exact) == MAX_SKILL_CHARS
    check_skill_text(exact)
    with pytest.raises(SkillFormatError, match="100,000"):
        check_skill_text(exact + "y")


def test_support_file_size() -> None:
    check_support_size(b"x" * MAX_SUPPORT_BYTES)
    with pytest.raises(SkillFormatError, match="1 MiB"):
        check_support_size(b"x" * (MAX_SUPPORT_BYTES + 1))
    with pytest.raises(SkillFormatError):
        check_support_size("é" * (MAX_SUPPORT_BYTES // 2 + 1))  # counted in UTF-8 bytes


@pytest.mark.parametrize(
    "path",
    ["references/api.md", "templates/a/b.yaml", "scripts/run.py", "assets/logo.png"],
)
def test_support_path_allowed(path: str) -> None:
    assert check_support_path(path).as_posix() == path


@pytest.mark.parametrize(
    "path",
    [
        "",
        "SKILL.md",
        "examples/demo.md",
        "notes/x.md",
        "references",
        "/etc/passwd",
        "C:/x/references/a.md",
        "references/../SKILL.md",
        "references/./a.md",
        "../other/references/a.md",
    ],
)
def test_support_path_rules(path: str) -> None:
    with pytest.raises(SkillFormatError):
        check_support_path(path)


def test_frontmatter_parsing_edges() -> None:
    assert split_frontmatter("no fence\n") == ({}, "no fence\n")
    raw, body = split_frontmatter("\ufeff---\nname: a\ndescription: b\n---\nbody\n")
    assert raw == {"name": "a", "description": "b"} and body == "body\n"
    with pytest.raises(SkillFormatError, match="closing"):
        split_frontmatter("---\nname: a\n")
    with pytest.raises(SkillFormatError, match="mapping"):
        split_frontmatter("---\n- a\n- b\n---\nbody\n")
    with pytest.raises(SkillFormatError, match="YAML"):
        split_frontmatter("---\nname: [unclosed\n---\nbody\n")


def test_nested_metadata_and_env_vars() -> None:
    text = (
        "---\nname: demo\ndescription: d\nplatforms: linux\nversion: 1.0\n"
        "required_environment_variables:\n  - name: DEMO_TOKEN\n    prompt: Token please\n"
        "metadata:\n  indus:\n    tags: [a, b]\n    requires_tools: terminal\n"
        "    config:\n      key: demo.path\n      description: where\n      default: /tmp\n"
        "  other_tool: {x: 1}\n---\nbody\n"
    )
    parsed = parse_skill(text)
    fm = parsed.frontmatter
    assert fm.platforms == ["linux"] and fm.version == "1.0"
    assert fm.required_environment_variables[0].name == "DEMO_TOKEN"
    assert fm.indus.tags == ["a", "b"] and fm.indus.requires_tools == ["terminal"]
    assert fm.indus.config[0].key == "demo.path"
    with pytest.raises(SkillFormatError, match="environment variable"):
        parse_skill(text.replace("DEMO_TOKEN", "bad name"))


def test_missing_metadata_gives_empty_indus() -> None:
    assert parse_skill(skill_text()).frontmatter.indus.requires_tools == []


@pytest.mark.parametrize(
    ("folder", "expected"),
    [
        ("no-frontmatter", "frontmatter"),
        ("bad-name", "not allowed"),
        ("long-description", "description is"),
        ("missing-description", "description"),
        ("unclosed-fence", "closing"),
        ("name-mismatch", "does not match"),
        ("empty-body", "no body"),
    ],
)
def test_invalid_fixture_tree(folder: str, expected: str) -> None:
    text = (INVALID / folder / "SKILL.md").read_text(encoding="utf-8")
    with pytest.raises(SkillFormatError, match=expected):
        check_skill_text(text, expected_name=folder)


def _fm(extra: str) -> fmt.SkillFrontmatter:
    return parse_skill(f"---\nname: s\ndescription: d\n{extra}---\nbody\n").frontmatter


def _meta(line: str) -> str:
    return f"metadata:\n  indus:\n    {line}\n"


def _known_envs(tag: str) -> bool | None:
    return {"docker": False, "kanban": True}.get(tag)


def _apps(name: str) -> bool | None:
    return {"present": True, "declared-off": False}.get(name)


BASE = VisibilityContext(
    os_platform="linux",
    toolsets=frozenset({"web", "file"}),
    tools=frozenset({"read_file", "web_search"}),
    session_platform="cli",
    environment=_known_envs,
    app_offerable=_apps,
)


@pytest.mark.parametrize(
    ("extra", "shown"),
    [
        ("", True),
        ("platforms: [linux]\n", True),
        ("platforms: [macos, windows]\n", False),
        ("platforms: [Linux]\n", True),
        (_meta("requires_toolsets: [web]"), True),
        (_meta("requires_toolsets: [web, browser]"), False),
        (_meta("requires_tools: [read_file]"), True),
        (_meta("requires_tools: [vision_analyze]"), False),
        (_meta("fallback_for_toolsets: [browser]"), True),
        (_meta("fallback_for_toolsets: [web]"), False),
        (_meta("fallback_for_tools: [vision_analyze]"), True),
        (_meta("fallback_for_tools: [web_search]"), False),
        ("environments: [kanban]\n", True),
        ("environments: [docker]\n", False),
        ("environments: [docker, kanban]\n", True),
        ("environments: [never-heard-of]\n", True),  # unknown tag fails open
        ("requires_apps: [present]\n", True),
        ("requires_apps: [declared-off]\n", False),
        ("requires_apps: [nobody-declared]\n", False),  # unknown app fails closed
        ("requires_apps: [present, nobody-declared]\n", False),
        (_meta("session_platforms: [cli, tui]"), True),
        (_meta("session_platforms: [telegram]"), False),
    ],
)
def test_visibility_table(extra: str, shown: bool) -> None:
    fm = _fm(extra)
    assert visible(fm, BASE) is shown
    assert (hidden_reason(fm, BASE) is None) is shown


def test_session_platform_unknown_shows() -> None:
    fm = _fm(_meta("session_platforms: [telegram]"))
    ctx = VisibilityContext(os_platform="linux", session_platform=None)
    assert visible(fm, ctx)


def test_os_names_map_to_sys_platform() -> None:
    fm = _fm("platforms: [macos]\n")
    assert visible(fm, VisibilityContext(os_platform="darwin"))
    assert not visible(_fm("platforms: [windows]\n"), VisibilityContext(os_platform="darwin"))
    assert visible(_fm("platforms: [windows]\n"), VisibilityContext(os_platform="win32"))
