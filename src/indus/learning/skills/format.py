"""The skill file format: frontmatter model, hard limits on agent writes, and visibility.

A skill is a folder ``<root>/[category/]<name>/`` holding ``SKILL.md``: YAML frontmatter between
two ``---`` fences, then a Markdown body. Optional folders beside it: ``references/``,
``templates/``, ``scripts/``, ``examples/`` and ``assets/``. A category folder may carry a
``DESCRIPTION.md`` whose frontmatter ``description`` labels the category.

Field names follow the agentskills.io layout; everything specific to this product lives under
``metadata.indus``. Unknown keys are kept (``extra="allow"``) so a skill written for another tool
still loads.

Visibility answers "should this session be offered the skill"; it never blocks an explicit load by
path. Each condition is independent and all must pass:

- ``platforms``: host OS names (``macos``, ``linux``, ``windows``); absent = every OS;
- ``metadata.indus.requires_toolsets`` / ``requires_tools``: hidden while any is absent;
- ``metadata.indus.fallback_for_toolsets`` / ``fallback_for_tools``: hidden while any is present;
- ``environments``: shown when any tag is active; a tag nobody can detect counts as active;
- ``requires_apps``: shown only when every app is offerable; an app nobody declared hides it;
- ``metadata.indus.session_platforms``: shown only on those surfaces (when the surface is known).
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from indus.foundation import yamlio

__all__ = [
    "CATEGORY_FILE",
    "LAYOUT_DIRS",
    "MAX_DESCRIPTION_CHARS",
    "MAX_NAME_CHARS",
    "MAX_SKILL_CHARS",
    "MAX_SUPPORT_BYTES",
    "NAME_PATTERN",
    "SKILL_FILE",
    "WRITABLE_DIRS",
    "ConfigVar",
    "EnvVarDecl",
    "IndusMetadata",
    "ParsedSkill",
    "SkillFormatError",
    "SkillFrontmatter",
    "SkillMetadata",
    "VisibilityContext",
    "check_category",
    "check_name",
    "check_skill_text",
    "check_support_path",
    "check_support_size",
    "hidden_reason",
    "parse_skill",
    "split_frontmatter",
    "visible",
]

SKILL_FILE = "SKILL.md"
CATEGORY_FILE = "DESCRIPTION.md"
MAX_NAME_CHARS = 64
MAX_DESCRIPTION_CHARS = 1024
MAX_SKILL_CHARS = 100_000
MAX_SUPPORT_BYTES = 1024 * 1024
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
# Folders an agent may write support files into.
WRITABLE_DIRS: tuple[str, ...] = ("references", "templates", "scripts", "assets")
# Every optional folder of the layout (readable through skill_view).
LAYOUT_DIRS: tuple[str, ...] = ("references", "templates", "scripts", "examples", "assets")

_OS_PREFIX = {"macos": "darwin", "darwin": "darwin", "linux": "linux", "windows": "win32"}
_FENCE_END = re.compile(r"^---[ \t]*$", re.MULTILINE)


class SkillFormatError(ValueError):
    """A skill text or path breaks a format rule; ``problems`` lists each one."""

    def __init__(self, problems: Iterable[str]) -> None:
        self.problems: tuple[str, ...] = tuple(problems)
        super().__init__("; ".join(self.problems) or "invalid skill")


def _as_list(value: object) -> object:
    """A single string where a list is expected becomes a one-item list."""
    if isinstance(value, str):
        return [value] if value.strip() else []
    return value


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class EnvVarDecl(_Model):
    """An environment variable the skill needs (``required_environment_variables``)."""

    name: str
    prompt: str | None = None
    help: str | None = None
    required_for: str | None = None

    @field_validator("name")
    @classmethod
    def _name_shape(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError(f"{value!r} is not an environment variable name")
        return value


class ConfigVar(_Model):
    """A setting the skill reads, stored under ``skills.config.<key>``."""

    key: str
    description: str
    default: Any = None
    prompt: str | None = None


class IndusMetadata(_Model):
    tags: list[str] = Field(default_factory=list[str])
    category: str | None = None
    fallback_for_toolsets: list[str] = Field(default_factory=list[str])
    requires_toolsets: list[str] = Field(default_factory=list[str])
    fallback_for_tools: list[str] = Field(default_factory=list[str])
    requires_tools: list[str] = Field(default_factory=list[str])
    session_platforms: list[str] = Field(default_factory=list[str])
    config: list[ConfigVar] = Field(default_factory=list[ConfigVar])
    blueprint: Any = None

    @field_validator(
        "tags",
        "fallback_for_toolsets",
        "requires_toolsets",
        "fallback_for_tools",
        "requires_tools",
        "session_platforms",
        mode="before",
    )
    @classmethod
    def _listify(cls, value: object) -> object:
        return _as_list(value)

    @field_validator("config", mode="before")
    @classmethod
    def _config_list(cls, value: object) -> object:
        return [value] if isinstance(value, Mapping) else value


class SkillMetadata(_Model):
    indus: IndusMetadata | None = None


def _yaml_kind(value: object) -> str | None:
    """What YAML 1.1 made of an unquoted scalar that was meant as text (None when it is text)."""
    if isinstance(value, bool):
        return "boolean"
    if value is None:
        return "null"
    if isinstance(value, int | float):
        return "number"
    return None


class SkillFrontmatter(_Model):
    name: str
    description: str

    @field_validator("name", mode="before")
    @classmethod
    def _name_is_text(cls, value: object) -> object:
        kind = _yaml_kind(value)
        if kind is not None:
            raise ValueError(
                f"the name reads as a YAML {kind} ({value!r}): YAML 1.1 turns unquoted "
                "on/off/yes/no/y/n/true/false/null and numbers into non-text; quote it or "
                "rename the skill"
            )
        return value

    version: str | None = None
    author: str | None = None
    license: str | None = None
    platforms: list[str] = Field(default_factory=list[str])
    environments: list[str] = Field(default_factory=list[str])
    requires_apps: list[str] = Field(default_factory=list[str])
    required_environment_variables: list[EnvVarDecl] = Field(default_factory=list[EnvVarDecl])
    metadata: SkillMetadata | None = None

    @field_validator("platforms", "environments", "requires_apps", mode="before")
    @classmethod
    def _listify(cls, value: object) -> object:
        return _as_list(value)

    @field_validator("version", mode="before")
    @classmethod
    def _version_text(cls, value: object) -> object:
        return str(value) if isinstance(value, int | float) else value

    @property
    def indus(self) -> IndusMetadata:
        """``metadata.indus`` (an empty one when absent)."""
        if self.metadata is not None and self.metadata.indus is not None:
            return self.metadata.indus
        return IndusMetadata()


@dataclass(frozen=True)
class ParsedSkill:
    frontmatter: SkillFrontmatter
    raw: dict[str, Any]
    body: str
    text: str


# -- parsing ------------------------------------------------------------------------------------


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """``(frontmatter mapping, body)``. Raises ``SkillFormatError`` when the fences or the YAML
    are broken; text without a leading fence gives ``({}, text)``."""
    content = text.removeprefix("﻿")
    first, _, rest = content.partition("\n")
    if first.rstrip() != "---":
        return {}, content
    end = _FENCE_END.search(rest)
    if end is None:
        raise SkillFormatError(["frontmatter has no closing '---' line"])
    block = rest[: end.start()]
    body = rest[end.end() :].removeprefix("\n")
    try:
        loaded = yamlio.load_safe(block) if block.strip() else {}
    except yamlio.YamlError as exc:
        raise SkillFormatError([f"frontmatter is not valid YAML: {exc}"]) from exc
    if not isinstance(loaded, dict):
        raise SkillFormatError(["frontmatter must be a mapping of keys to values"])
    return cast(dict[str, Any], loaded), body


def _validation_problems(exc: ValidationError) -> list[str]:
    out: list[str] = []
    for err in exc.errors():
        where = ".".join(str(p) for p in err["loc"]) or "frontmatter"
        out.append(f"{where}: {err['msg']}")
    return out


def parse_skill(text: str) -> ParsedSkill:
    """Parse ``SKILL.md`` text into a validated frontmatter and body (no size limits here)."""
    raw, body = split_frontmatter(text)
    if not raw:
        raise SkillFormatError(["SKILL.md must start with a '---' frontmatter block"])
    try:
        frontmatter = SkillFrontmatter.model_validate(raw)
    except ValidationError as exc:
        raise SkillFormatError(_validation_problems(exc)) from exc
    return ParsedSkill(frontmatter=frontmatter, raw=raw, body=body, text=text)


# -- hard limits on agent writes ----------------------------------------------------------------


def check_name(name: str) -> str | None:
    """Why ``name`` cannot name a new skill, or None when it can."""
    if not name:
        return "a skill name is required"
    if len(name) > MAX_NAME_CHARS:
        return f"skill name is {len(name)} characters; the limit is {MAX_NAME_CHARS}"
    if not NAME_PATTERN.match(name):
        return (
            f"skill name {name!r} is not allowed: use lowercase letters, digits, '.', '_' and "
            "'-', starting with a letter or digit"
        )
    kind = _plain_scalar_kind(name)
    if kind is not None:
        return (
            f"skill name {name!r} is not allowed: written unquoted in frontmatter, YAML 1.1 "
            f"reads it as a {kind}; pick a name with a word in it (for example {name}-skill)"
        )
    return None


def _plain_scalar_kind(text: str) -> str | None:
    """The YAML 1.1 type ``text`` takes as an unquoted value, when that is not text."""
    try:
        loaded = yamlio.load_safe(f"v: {text}\n")
    except yamlio.YamlError:
        return None
    if isinstance(loaded, dict):
        value = cast(dict[str, Any], loaded).get("v")
        if value != text:
            return _yaml_kind(value) or "non-text value"
    return None


def check_category(category: str | None) -> str | None:
    """Why ``category`` cannot hold a new skill (one path segment, same shape as a name)."""
    if category is None or category == "":
        return None
    problem = check_name(category)
    return None if problem is None else problem.replace("skill name", "category")


def check_skill_text(text: str, *, expected_name: str | None = None) -> ParsedSkill:
    """Validate a ``SKILL.md`` an agent is about to write; raises with every problem found."""
    problems: list[str] = []
    if len(text) > MAX_SKILL_CHARS:
        problems.append(f"SKILL.md is {len(text):,} characters; the limit is {MAX_SKILL_CHARS:,}")
    try:
        parsed = parse_skill(text)
    except SkillFormatError as exc:
        raise SkillFormatError([*problems, *exc.problems]) from exc
    fm = parsed.frontmatter
    if not fm.description.strip():
        problems.append("description must not be empty")
    if len(fm.description) > MAX_DESCRIPTION_CHARS:
        problems.append(
            f"description is {len(fm.description):,} characters; the limit is "
            f"{MAX_DESCRIPTION_CHARS:,}"
        )
    name_problem = check_name(fm.name)
    if name_problem is not None:
        problems.append(name_problem)
    elif expected_name is not None and fm.name != expected_name:
        problems.append(f"frontmatter name {fm.name!r} does not match the skill {expected_name!r}")
    if not parsed.body.strip():
        problems.append("SKILL.md has no body after the frontmatter")
    if problems:
        raise SkillFormatError(problems)
    return parsed


def check_support_path(file_path: str) -> PurePosixPath:
    """The normalised relative path of a support file an agent may write, or raise."""
    raw = file_path.strip().replace("\\", "/")
    if not raw:
        raise SkillFormatError(["file_path is required"])
    rel = PurePosixPath(raw)
    if rel.is_absolute() or re.match(r"^[A-Za-z]:", raw):
        raise SkillFormatError([f"file_path {file_path!r} must be relative to the skill folder"])
    if any(part in ("..", ".") for part in raw.split("/")):
        raise SkillFormatError([f"file_path {file_path!r} may not contain '.' or '..' segments"])
    if len(rel.parts) < 2 or rel.parts[0] not in WRITABLE_DIRS:
        allowed = ", ".join(f"{d}/" for d in WRITABLE_DIRS)
        raise SkillFormatError([f"support files go under {allowed}; got {file_path!r}"])
    return rel


def check_support_size(data: bytes | str) -> None:
    size = len(data.encode("utf-8")) if isinstance(data, str) else len(data)
    if size > MAX_SUPPORT_BYTES:
        raise SkillFormatError(
            [f"support file is {size:,} bytes; the limit is {MAX_SUPPORT_BYTES:,} (1 MiB)"]
        )


# -- visibility ---------------------------------------------------------------------------------


def _default_environment(tag: str) -> bool | None:
    if tag in ("docker", "container"):
        return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()
    return None


def _no_apps(_name: str) -> bool | None:
    return None


@dataclass(frozen=True)
class VisibilityContext:
    """What the current session offers. ``environment`` answers True/False for a tag it knows and
    None for one it does not (shown); ``app_offerable`` likewise, but None hides the skill.
    ``tools_known`` False skips the tool and toolset conditions (a caller that cannot see the
    session's tool array)."""

    os_platform: str = field(default_factory=lambda: sys.platform)
    toolsets: frozenset[str] = frozenset()
    tools: frozenset[str] = frozenset()
    tools_known: bool = True
    session_platform: str | None = None
    environment: Callable[[str], bool | None] = _default_environment
    app_offerable: Callable[[str], bool | None] = _no_apps


def _matches_os(platforms: list[str], current: str) -> bool:
    if not platforms:
        return True
    for name in platforms:
        key = name.strip().lower()
        if current.startswith(_OS_PREFIX.get(key, key)):
            return True
    return False


def _tool_reason(meta: IndusMetadata, ctx: VisibilityContext) -> str | None:
    missing_sets = [t for t in meta.requires_toolsets if t not in ctx.toolsets]
    if missing_sets:
        return f"needs toolset {missing_sets[0]}"
    missing_tools = [t for t in meta.requires_tools if t not in ctx.tools]
    if missing_tools:
        return f"needs tool {missing_tools[0]}"
    present_sets = [t for t in meta.fallback_for_toolsets if t in ctx.toolsets]
    if present_sets:
        return f"fallback while toolset {present_sets[0]} is on"
    present_tools = [t for t in meta.fallback_for_tools if t in ctx.tools]
    if present_tools:
        return f"fallback while tool {present_tools[0]} is on"
    return None


def hidden_reason(fm: SkillFrontmatter, ctx: VisibilityContext) -> str | None:
    """Why the skill is not offered in ``ctx``, or None when it is."""
    meta = fm.indus
    if not _matches_os(fm.platforms, ctx.os_platform):
        return f"made for {', '.join(fm.platforms)}"
    reason = _tool_reason(meta, ctx) if ctx.tools_known else None
    if reason is not None:
        return reason
    tags = [t.strip().lower() for t in fm.environments if t.strip()]
    if tags and not any(ctx.environment(tag) is not False for tag in tags):
        return f"only for environment {', '.join(tags)}"
    for app in fm.requires_apps:
        if ctx.app_offerable(app.strip()) is not True:
            return f"needs app {app}"
    surfaces = [s.strip().lower() for s in meta.session_platforms if s.strip()]
    platform = ctx.session_platform
    if surfaces and platform is not None and platform.lower() not in surfaces:
        return f"only on {', '.join(surfaces)}"
    return None


def visible(fm: SkillFrontmatter, ctx: VisibilityContext) -> bool:
    return hidden_reason(fm, ctx) is None
