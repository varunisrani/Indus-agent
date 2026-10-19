"""Skill discovery and resolution: one catalog that every consumer asks (R07.27).

Roots are scanned in tier order, highest first:

1. project: ``.indus/skills`` and ``.agents/skills`` of the current repo, only once the user
   trusted it (``project_trust``; P13-28),
2. the profile's ``<home>/skills/`` (the source of truth),
3. ``skills.create_dir`` (where agent-created skills go when set),
4. each ``skills.external_dirs`` entry (``~`` and ``${VAR}`` expanded; missing folders skipped).

A skill in a higher tier hides same-named skills below it (each hidden copy is logged once per
process). Inside the winning tier, two different skills with one name are both listed under their
paths relative to their root, and the bare name refuses to load ("ambiguous skill name"). Copies
that are provably one skill — one real file reached through a symlink, or byte-identical files —
are not a conflict: the shallowest path wins. ``skills.disabled`` and
``skills.platform_disabled.<surface>`` hide a skill and make ``resolve`` refuse it. Plugin skills
are named ``plugin:skill`` and live outside the tiers.

A project skill the content scan found dangerous is ``quarantined``: it takes no part in name
resolution (it hides nothing below it), is never listed, and ``resolve`` refuses it with the
reason (``SkillQuarantined``).

External folders are read and written in place; they are a convenience, not a protection boundary.

Parsing is cached per ``SKILL.md`` by ``(path, mtime_ns, size)``, so a rescan only re-reads files
that changed.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from indus.foundation.config import loader
from indus.foundation.config.schema import default_for
from indus.foundation.home import expand_path_text
from indus.foundation.scope import Scope
from indus.learning.skills.format import (
    CATEGORY_FILE,
    SKILL_FILE,
    SkillFormatError,
    SkillFrontmatter,
    VisibilityContext,
    parse_skill,
    split_frontmatter,
    visible,
)

__all__ = [
    "TIER_CREATE_DIR",
    "TIER_EXTERNAL",
    "TIER_LOCAL",
    "TIER_PLUGIN",
    "TIER_PROJECT",
    "AmbiguousSkill",
    "SkillBroken",
    "SkillCatalog",
    "SkillDisabled",
    "SkillError",
    "SkillNotFound",
    "SkillQuarantined",
    "SkillRecord",
    "SkillsSettings",
    "clear_scan_cache",
    "load_catalog",
    "skills_setting",
]

log = logging.getLogger(__name__)

TIER_PROJECT = 0
TIER_LOCAL = 1
TIER_CREATE_DIR = 2
TIER_EXTERNAL = 3
TIER_PLUGIN = 9

TIER_NAMES = {
    TIER_PROJECT: "project",
    TIER_LOCAL: "local",
    TIER_CREATE_DIR: "create_dir",
    TIER_EXTERNAL: "external",
    TIER_PLUGIN: "plugin",
}

# Folder names never scanned for skills (plus every name starting with a dot).
SKIP_DIRS = frozenset({"node_modules", "__pycache__", "venv", "site-packages", "dist", "build"})

Status = Literal["unique", "ambiguous", "shadowed", "broken", "quarantined"]


class SkillError(LookupError):
    """A skill lookup failed; ``str()`` is the text a tool result shows."""


class SkillNotFound(SkillError):
    pass


class AmbiguousSkill(SkillError):
    def __init__(self, name: str, choices: Sequence[str]) -> None:
        self.name = name
        self.choices = tuple(choices)
        listed = ", ".join(self.choices)
        super().__init__(f"ambiguous skill name {name!r}, use one of: {listed}")


class SkillDisabled(SkillError):
    pass


class SkillBroken(SkillError):
    pass


class SkillQuarantined(SkillError):
    pass


# -- settings -----------------------------------------------------------------------------------


def skills_setting(scope: Scope, key: str) -> Any:
    """``key`` from the scope's config (re-read when the file changed), else the default."""
    config: Any = scope.config
    try:
        if isinstance(config, loader.ConfigView) and config.path == loader.config_path(scope):
            config = loader.load(scope)
        value = config.get(key) if config is not None else None
    except Exception:
        log.debug("config read failed for %s", key, exc_info=True)
        value = None
    return default_for(key) if value is None else value


def _str_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list | tuple):
        return [str(v).strip() for v in value if str(v).strip()]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    return []


@dataclass(frozen=True)
class SkillsSettings:
    """The skill settings of one profile, read once per catalog build."""

    local_root: Path
    create_dir: Path | None = None
    external_dirs: tuple[Path, ...] = ()
    disabled: frozenset[str] = frozenset()
    project_dirs: tuple[Path, ...] = ()
    plugin_roots: Mapping[str, Path] = field(default_factory=dict[str, Path])
    quarantined: Mapping[str, str] = field(default_factory=dict[str, str])
    """``realpath(project skill folder)`` -> why it may not load (P13-28)."""
    project_root: Path | None = None
    project_pending: int = 0
    """Skills found in an untrusted repo: counted for the banner, never loaded."""

    @property
    def write_root(self) -> Path:
        """Where a new agent-created skill goes."""
        return self.create_dir if self.create_dir is not None else self.local_root

    @classmethod
    def from_scope(cls, scope: Scope, *, platform: str | None = None) -> SkillsSettings:
        env = dict(scope.env)
        home_dir = Path(env.get("HOME") or Path.home())

        def expand(raw: str) -> Path:
            return expand_path_text(raw, env, lambda: home_dir)

        create_raw = skills_setting(scope, "skills.create_dir")
        create_dir = expand(create_raw) if isinstance(create_raw, str) and create_raw else None
        externals = tuple(
            expand(raw) for raw in _str_list(skills_setting(scope, "skills.external_dirs"))
        )
        disabled = set(_str_list(skills_setting(scope, "skills.disabled")))
        if platform:
            per = skills_setting(scope, "skills.platform_disabled")
            if isinstance(per, Mapping):
                disabled.update(_str_list(per.get(platform)))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        from indus.learning.skills import project_trust

        try:
            project = project_trust.project_skills(scope)
        except Exception:
            log.warning("project skills could not be checked; none loaded", exc_info=True)
            project = project_trust.ProjectSkills()
        return cls(
            local_root=scope.home / "skills",
            create_dir=create_dir,
            external_dirs=externals,
            disabled=frozenset(disabled),
            project_dirs=project.load_dirs,
            quarantined=dict(project.quarantined),
            project_root=project.root,
            project_pending=project.pending,
        )

    def roots(self) -> list[tuple[int, Path]]:
        """``(tier, folder)`` in precedence order, each folder once (missing ones included)."""
        listed: list[tuple[int, Path]] = [(TIER_PROJECT, d) for d in self.project_dirs]
        listed.append((TIER_LOCAL, self.local_root))
        if self.create_dir is not None:
            listed.append((TIER_CREATE_DIR, self.create_dir))
        listed.extend((TIER_EXTERNAL, d) for d in self.external_dirs)
        seen: set[str] = set()
        out: list[tuple[int, Path]] = []
        for tier, folder in listed:
            key = os.path.normcase(os.path.abspath(folder))
            if key in seen:
                continue
            seen.add(key)
            out.append((tier, folder))
        return out


# -- records ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillRecord:
    name: str
    tier: int
    root: Path
    skill_dir: Path
    relative_path: str
    frontmatter: SkillFrontmatter | None
    error: str | None = None
    status: Status = "unique"
    load_name: str | None = None
    disabled: bool = False
    plugin: str | None = None

    @property
    def skill_md(self) -> Path:
        return self.skill_dir / SKILL_FILE

    @property
    def description(self) -> str:
        return self.frontmatter.description.strip() if self.frontmatter is not None else ""

    @property
    def category(self) -> str | None:
        if self.frontmatter is not None and self.frontmatter.indus.category:
            return self.frontmatter.indus.category
        parent = Path(self.relative_path).parent.as_posix()
        return None if parent in ("", ".") else parent

    @property
    def tier_name(self) -> str:
        return TIER_NAMES.get(self.tier, str(self.tier))


@dataclass(frozen=True)
class _Parsed:
    name: str
    frontmatter: SkillFrontmatter | None
    error: str | None


_CACHE: dict[str, tuple[int, int, _Parsed]] = {}
_CACHE_LOCK = threading.Lock()
_SHADOW_LOGGED: set[tuple[str, str]] = set()


def clear_scan_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
        _SHADOW_LOGGED.clear()


def _parse_cached(skill_md: Path, folder_name: str) -> _Parsed:
    try:
        st = skill_md.stat()
    except OSError as exc:
        return _Parsed(folder_name, None, f"cannot read SKILL.md: {exc.strerror or exc}")
    key = str(skill_md)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
            return hit[2]
    try:
        parsed_skill = parse_skill(skill_md.read_text(encoding="utf-8"))
        parsed = _Parsed(parsed_skill.frontmatter.name.strip(), parsed_skill.frontmatter, None)
    except (OSError, UnicodeDecodeError) as exc:
        parsed = _Parsed(folder_name, None, f"cannot read SKILL.md: {exc}")
    except SkillFormatError as exc:
        parsed = _Parsed(folder_name, None, str(exc))
    with _CACHE_LOCK:
        _CACHE[key] = (st.st_mtime_ns, st.st_size, parsed)
    return parsed


def _scan_root(
    tier: int,
    root: Path,
    plugin: str | None = None,
    quarantined: Mapping[str, str] | None = None,
) -> tuple[list[SkillRecord], dict[str, str]]:
    """Records under ``root`` and the category labels found there (category path → text)."""
    records: list[SkillRecord] = []
    labels: dict[str, str] = {}
    if not root.is_dir():
        log.debug("skill folder %s is missing; skipped", root)
        return records, labels
    # Symlinked folders are followed (a symlink view of a skill is one skill); the real paths
    # on the way down are tracked so a link back to an ancestor cannot loop.
    chains: dict[str, frozenset[str]] = {}
    for current, dirs, files in os.walk(root, followlinks=True):
        real = os.path.realpath(current)
        above = chains.get(os.path.dirname(current), frozenset())
        if real in above:
            dirs[:] = []
            continue
        chains[current] = above | {real}
        here = Path(current)
        rel = here.relative_to(root).as_posix()
        if SKILL_FILE in files and here != root:
            dirs[:] = []
            parsed = _parse_cached(here / SKILL_FILE, here.name)
            name = f"{plugin}:{parsed.name}" if plugin else parsed.name
            held = (quarantined or {}).get(os.path.realpath(here)) if tier == TIER_PROJECT else None
            if held:
                records.append(
                    SkillRecord(
                        name=name,
                        tier=tier,
                        root=root,
                        skill_dir=here,
                        relative_path=rel,
                        frontmatter=parsed.frontmatter,
                        error=held,
                        status="quarantined",
                    )
                )
                continue
            records.append(
                SkillRecord(
                    name=name,
                    tier=tier,
                    root=root,
                    skill_dir=here,
                    relative_path=rel,
                    frontmatter=parsed.frontmatter,
                    error=parsed.error,
                    status="broken" if parsed.error else "unique",
                    load_name=None if parsed.error else name,
                    plugin=plugin,
                )
            )
            continue
        if CATEGORY_FILE in files and here != root:
            label = _category_label(here / CATEGORY_FILE)
            if label:
                labels[rel] = label
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS)
    return records, labels


def _category_label(path: Path) -> str:
    try:
        raw, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SkillFormatError):
        return ""
    value = raw.get("description")
    return value.strip() if isinstance(value, str) else ""


def _same_skill(paths: Iterable[Path]) -> bool:
    items = list(paths)
    try:
        if len({os.path.realpath(p) for p in items}) == 1:
            return True
        return len({hashlib.sha256(p.read_bytes()).hexdigest() for p in items}) == 1
    except OSError:
        return False


def _depth(record: SkillRecord) -> tuple[int, str]:
    return (len(Path(record.relative_path).parts), record.relative_path)


def _resolve_statuses(records: list[SkillRecord]) -> list[SkillRecord]:
    out = list(records)
    groups: dict[str, list[int]] = {}
    for i, rec in enumerate(out):
        if rec.status not in ("broken", "quarantined"):
            groups.setdefault(rec.name, []).append(i)
    rel_owners: dict[tuple[int, str], int] = {}
    for rec in out:
        key = (rec.tier, rec.relative_path)
        rel_owners[key] = rel_owners.get(key, 0) + 1
    for name, idxs in groups.items():
        top = min(out[i].tier for i in idxs)
        contenders = sorted((i for i in idxs if out[i].tier == top), key=lambda i: _depth(out[i]))
        if len(contenders) == 1 or _same_skill(out[i].skill_md for i in contenders):
            winner = contenders[0]
            out[winner] = replace(out[winner], status="unique", load_name=name)
            for i in contenders[1:]:
                out[i] = replace(out[i], status="shadowed", load_name=None)
        else:
            for i in contenders:
                rec = out[i]
                path_free = rel_owners[(rec.tier, rec.relative_path)] == 1
                out[i] = replace(
                    rec, status="ambiguous", load_name=rec.relative_path if path_free else None
                )
            winner = None
        for i in idxs:
            rec = out[i]
            if rec.tier == top:
                continue
            out[i] = replace(rec, status="shadowed", load_name=None)
            owner = out[winner].skill_md if winner is not None else None
            key = (str(rec.skill_md), name)
            if key in _SHADOW_LOGGED:
                continue
            _SHADOW_LOGGED.add(key)
            if owner is not None and _same_skill([rec.skill_md, owner]):
                continue
            log.warning(
                "skill %r at %s is hidden by a copy in a higher tier (%s)",
                name,
                rec.skill_dir,
                TIER_NAMES.get(top, top),
            )
    return out


# -- the catalog --------------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillCatalog:
    """Every scanned skill with its resolution status. Build with ``load_catalog``."""

    settings: SkillsSettings
    records: tuple[SkillRecord, ...]
    category_labels: Mapping[str, str] = field(default_factory=dict[str, str])

    def loadable(self) -> list[SkillRecord]:
        """Unique and ambiguous skills, disabled ones included (sorted by category, name)."""
        rows = [r for r in self.records if r.status in ("unique", "ambiguous")]
        return sorted(rows, key=lambda r: (r.category or "", r.name, r.relative_path))

    def visible(self, ctx: VisibilityContext | None = None) -> list[SkillRecord]:
        """What a session is offered: loadable, not disabled, passing the visibility rules."""
        rows: list[SkillRecord] = []
        for rec in self.loadable():
            if rec.disabled or rec.frontmatter is None:
                continue
            if ctx is not None and not visible(rec.frontmatter, ctx):
                continue
            rows.append(rec)
        return rows

    def ambiguous_names(self) -> dict[str, list[SkillRecord]]:
        out: dict[str, list[SkillRecord]] = {}
        for rec in self.loadable():
            if rec.status == "ambiguous" and not rec.disabled:
                out.setdefault(rec.name, []).append(rec)
        return out

    def _match(self, identifier: str) -> list[SkillRecord]:
        ident = identifier.strip().strip("/")
        live = [r for r in self.records if r.status != "shadowed"]
        by_name = [r for r in live if r.name == ident]
        if by_name:
            return by_name
        by_path = [r for r in live if r.relative_path == ident]
        if by_path:
            return by_path
        candidate = Path(identifier).expanduser()
        if candidate.is_absolute():
            real = os.path.realpath(candidate)
            return [
                r
                for r in live
                if os.path.realpath(r.skill_dir) == real or os.path.realpath(r.skill_md) == real
            ]
        return []

    def resolve(self, identifier: str) -> SkillRecord:
        """The one skill ``identifier`` (name, relative path or absolute path) names."""
        found = self._match(identifier)
        held = [r for r in found if r.status == "quarantined"]
        healthy = [r for r in found if r.status not in ("broken", "quarantined")]
        if not found:
            raise SkillNotFound(f"no skill named {identifier!r}")
        if not healthy and held:
            raise SkillQuarantined(
                f"skill {identifier!r} from the project at {held[0].root} is quarantined: "
                f"{held[0].error}. It stays unloaded until the file is fixed"
            )
        if not healthy:
            raise SkillBroken(f"skill {identifier!r} cannot be loaded: {found[0].error}")
        if len(healthy) > 1:
            choices = [r.load_name or str(r.skill_dir) for r in healthy]
            raise AmbiguousSkill(identifier.strip(), choices)
        rec = healthy[0]
        if rec.status == "ambiguous" and identifier.strip() == rec.name:
            raise AmbiguousSkill(rec.name, [rec.load_name or str(rec.skill_dir)])
        if rec.disabled:
            raise SkillDisabled(
                f"skill {rec.name!r} is disabled in this profile (skills.disabled); enable it "
                "with `indus config` before using it"
            )
        return rec

    def find(self, identifier: str) -> SkillRecord | None:
        try:
            return self.resolve(identifier)
        except SkillError:
            return None


def load_catalog(
    scope: Scope | None = None,
    *,
    platform: str | None = None,
    settings: SkillsSettings | None = None,
) -> SkillCatalog:
    """Scan every root of ``settings`` (default: read from ``scope``) and resolve names."""
    if settings is None:
        if scope is None:
            raise ValueError("load_catalog needs a scope or settings")
        settings = SkillsSettings.from_scope(scope, platform=platform)
    records: list[SkillRecord] = []
    labels: dict[str, str] = {}
    for tier, root in settings.roots():
        found, root_labels = _scan_root(tier, root, quarantined=settings.quarantined)
        records.extend(found)
        for key, text in root_labels.items():
            labels.setdefault(key, text)
    resolved = _resolve_statuses(records)
    for plugin, root in sorted(settings.plugin_roots.items()):
        found, _ = _scan_root(TIER_PLUGIN, root, plugin=plugin)
        resolved.extend(found)
    disabled = settings.disabled
    if disabled:
        resolved = [
            replace(r, disabled=True)
            if r.name in disabled or (r.load_name is not None and r.load_name in disabled)
            else r
            for r in resolved
        ]
    return SkillCatalog(settings=settings, records=tuple(resolved), category_labels=labels)
