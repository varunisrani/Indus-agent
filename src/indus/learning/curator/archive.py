"""Which skills the curator may touch, and the archive it moves them into.

Jurisdiction is declared, never inferred (R07.6): a skill is *managed* only when its usage record
says ``origin: agent`` (written by a background-review create, or by ``indus curator adopt``) and
it is neither bundled (``.bundled_manifest``) nor hub-installed (``.hub/lock.json``). Only skills
found in the profile's own folders (``<home>/skills`` and ``skills.create_dir``) count; external,
project and plugin skills belong to someone else.

Archiving moves the skill folder to ``<home>/skills/.archive/<name>/`` (a ``-<utc stamp>`` suffix
when that name is taken) and marks the usage record ``archived``; nothing is ever deleted here
except by ``purge``, which only a person runs. Restoring moves the newest archived copy back to
``<home>/skills/<name>/`` (the original nesting is not rebuilt). Each move is ledgered through
``ledger_port``.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from indus.foundation.scope import Scope
from indus.learning.curator import ledger_port
from indus.learning.skills.format import SkillFormatError, split_frontmatter
from indus.learning.skills.resolver import (
    TIER_CREATE_DIR,
    TIER_LOCAL,
    SkillRecord,
    clear_scan_cache,
    load_catalog,
)
from indus.learning.skills.usage import SkillUsage, bundled_names, hub_names, usage_for

__all__ = [
    "ARCHIVE_DIR",
    "ArchivedSkill",
    "Ownership",
    "Refused",
    "SkillOwner",
    "archive_dir",
    "archive_skill",
    "list_archived",
    "local_skills",
    "manage_archiver",
    "managed_skills",
    "ownership",
    "parse_time",
    "purge_archived",
    "restore_skill",
    "unmanaged_skills",
]

log = logging.getLogger(__name__)

ARCHIVE_DIR = ".archive"
SKILL_FILE = "SKILL.md"
_STAMP = "%Y%m%d%H%M%S"
_STAMPED = re.compile(r"^(?P<name>.+)-(?P<stamp>\d{14})$")

type SkillOwner = Literal["managed", "learn", "untracked", "bundled", "hub", "external", "missing"]
LOCAL_TIERS = (TIER_LOCAL, TIER_CREATE_DIR)

UNMANAGED_REASONS: Mapping[str, str] = {
    "learn": "created in the foreground",
    "untracked": "created before tracking",
}


class Refused(Exception):
    """A curator action that is not allowed; the message says why and what to do instead."""


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def archive_dir(scope: Scope) -> Path:
    return scope.home / "skills" / ARCHIVE_DIR


def local_skills(scope: Scope) -> dict[str, SkillRecord]:
    """Live skills in the profile's own folders by name (same-tier duplicates left out)."""
    catalog = load_catalog(scope)
    out: dict[str, SkillRecord] = {}
    for rec in catalog.records:
        if rec.tier in LOCAL_TIERS and rec.status == "unique" and rec.plugin is None:
            out.setdefault(rec.name, rec)
    return out


def _external_names(scope: Scope) -> set[str]:
    catalog = load_catalog(scope)
    return {r.name for r in catalog.records if r.tier not in LOCAL_TIERS}


@dataclass(frozen=True)
class Ownership:
    name: str
    owner: SkillOwner
    record: Mapping[str, Any]
    path: Path | None

    @property
    def managed(self) -> bool:
        return self.owner == "managed"

    @property
    def reason(self) -> str:
        return UNMANAGED_REASONS.get(self.owner, self.owner)


def ownership(scope: Scope, name: str, *, usage: SkillUsage | None = None) -> Ownership:
    """Who owns ``name`` as far as the curator is concerned."""
    usage = usage or usage_for(scope)
    root = scope.home / "skills"
    record = usage.get(name)
    if name in hub_names(root):
        return Ownership(name, "hub", record, None)
    rec = local_skills(scope).get(name)
    if name in bundled_names(root):
        return Ownership(name, "bundled", record, rec.skill_dir if rec else None)
    if rec is None:
        owner: SkillOwner = "external" if name in _external_names(scope) else "missing"
        return Ownership(name, owner, record, None)
    origin = record.get("origin")
    owner = "managed" if origin == "agent" else "learn" if origin == "learn" else "untracked"
    return Ownership(name, owner, record, rec.skill_dir)


def _all_owners(scope: Scope) -> list[Ownership]:
    usage = usage_for(scope)
    records = usage.load()
    root = scope.home / "skills"
    bundled, hub = bundled_names(root), hub_names(root)
    out: list[Ownership] = []
    for name, rec in sorted(local_skills(scope).items()):
        record = records.get(name) or {}
        if name in hub:
            owner: SkillOwner = "hub"
        elif name in bundled:
            owner = "bundled"
        elif record.get("origin") == "agent":
            owner = "managed"
        elif record.get("origin") == "learn":
            owner = "learn"
        else:
            owner = "untracked"
        out.append(Ownership(name, owner, record, rec.skill_dir))
    return out


def managed_skills(scope: Scope) -> list[Ownership]:
    return [o for o in _all_owners(scope) if o.managed]


def unmanaged_skills(scope: Scope) -> list[Ownership]:
    """Profile skills the curator leaves alone that a person could ``adopt``."""
    return [o for o in _all_owners(scope) if o.owner in ("learn", "untracked")]


def bundled_local(scope: Scope) -> list[Ownership]:
    return [o for o in _all_owners(scope) if o.owner == "bundled"]


# -- archive ------------------------------------------------------------------------------------


def _now(scope: Scope) -> datetime:
    return scope.clock.now().astimezone(UTC)


def _move(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(src, dest)
    except OSError:
        shutil.move(str(src), str(dest))


def _after_move(scope: Scope) -> None:
    clear_scan_cache()
    try:
        from indus.learning.skills.view_tools import clear_list_cache

        clear_list_cache(scope)
    except Exception:
        log.debug("skills list cache not cleared", exc_info=True)


def archive_skill(
    scope: Scope,
    name: str,
    *,
    actor: str = "curator",
    allow_bundled: bool = False,
    evidence: Mapping[str, Any] | None = None,
    ledger: bool = True,
) -> Path:
    """Move ``name`` into the archive; returns where it went. ``Refused`` when not allowed.

    Any skill in the profile's own folders may be archived (the callers decide about pins and
    management); hub skills never, bundled ones only with ``allow_bundled`` (``prune_builtins``).
    ``ledger=False``: the caller writes its own ledger entry (``skill_manage`` does).
    """
    owner = ownership(scope, name)
    if owner.owner == "hub":
        raise Refused(f"{name!r} came from the skills hub; remove it there instead")
    if owner.owner == "bundled" and not allow_bundled:
        raise Refused(
            f"{name!r} ships with Indus Agent; set curator.prune_builtins to let the curator "
            "archive bundled skills"
        )
    if owner.owner == "external":
        raise Refused(f"{name!r} lives in an external skills folder; the curator does not move it")
    if owner.path is None:
        raise Refused(f"no skill named {name!r} in this profile")
    dest = archive_dir(scope) / name
    if dest.exists():
        dest = dest.with_name(f"{name}-{_now(scope).strftime(_STAMP)}")
    if ledger:
        with ledger_port.ledgered(
            "archive", name, owner.path, actor=actor, evidence=evidence, scope=scope
        ) as out:
            _move(owner.path, dest)
            with contextlib.suppress(OSError):
                os.utime(dest)
            out["after"] = dest
    else:
        _move(owner.path, dest)
        with contextlib.suppress(OSError):
            os.utime(dest)
    usage_for(scope).set_state(name, "archived")
    _after_move(scope)
    log.info("skill %s archived to %s", name, dest)
    return dest


def manage_archiver(scope: Scope, folder: Path, name: str) -> Path:
    """``skill_manage``'s archiver (``manage_tool.ARCHIVER``): a review-fork delete goes through
    the curator's archive (same folder, name stamping and usage state); ``skill_manage`` writes the
    ledger entry itself, so none is written here. A refusal surfaces as ``OSError`` so the tool
    rolls the whole call back and reports it."""
    try:
        owner = ownership(scope, name)
        if owner.path is not None and os.path.realpath(owner.path) != os.path.realpath(folder):
            raise Refused(f"{name!r} resolves to {owner.path}, not {folder}")
        return archive_skill(scope, name, actor="agent", ledger=False)
    except Refused as exc:
        raise OSError(f"not archived: {exc}") from exc


# -- the archive --------------------------------------------------------------------------------


@dataclass(frozen=True)
class ArchivedSkill:
    name: str
    path: Path
    archived_at: datetime | None


def _archived_name(folder: Path) -> str:
    try:
        raw, _ = split_frontmatter((folder / SKILL_FILE).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SkillFormatError):
        raw = {}
    value = raw.get("name")
    if isinstance(value, str) and value.strip():
        return value.strip()
    match = _STAMPED.match(folder.name)
    return match.group("name") if match else folder.name


def list_archived(scope: Scope) -> list[ArchivedSkill]:
    """Archived skills, by name then newest first."""
    root = archive_dir(scope)
    if not root.is_dir():
        return []
    records = usage_for(scope).load()
    out: list[ArchivedSkill] = []
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        name = _archived_name(folder)
        stamp = parse_time((records.get(name) or {}).get("archived_at"))
        if stamp is None:
            with contextlib.suppress(OSError):
                stamp = datetime.fromtimestamp(folder.stat().st_mtime, UTC)
        out.append(ArchivedSkill(name, folder, stamp))
    return sorted(
        out,
        key=lambda a: (a.name, -(a.archived_at or datetime.min.replace(tzinfo=UTC)).timestamp()),
    )


def _archived_copy(scope: Scope, name: str) -> Path | None:
    found = [a for a in list_archived(scope) if a.name == name or a.path.name == name]
    if not found:
        return None
    exact = [a for a in found if a.path.name == name]
    if exact:
        return exact[0].path
    stamped = sorted((a.path for a in found), key=lambda p: p.name, reverse=True)
    return stamped[0]


def restore_skill(
    scope: Scope, name: str, *, actor: str = "user", allow_bundled: bool = False
) -> Path:
    """Move the archived ``name`` back to ``<home>/skills/<name>``; ``Refused`` when not allowed."""
    root = scope.home / "skills"
    if name in hub_names(root):
        raise Refused(f"a skills-hub skill now owns the name {name!r}; restoring would shadow it")
    if name in bundled_names(root) and not allow_bundled:
        raise Refused(f"a bundled skill now owns the name {name!r}; restoring would shadow it")
    source = _archived_copy(scope, name)
    if source is None:
        raise Refused(f"no archived skill named {name!r} (`indus curator list-archived`)")
    dest = root / name
    if dest.exists() or name in local_skills(scope):
        raise Refused(f"a skill named {name!r} already exists in this profile")
    with ledger_port.ledgered("restore", name, source, actor=actor, scope=scope) as out:
        _move(source, dest)
        out["after"] = dest
    usage_for(scope).set_state(name, "active")
    _after_move(scope)
    log.info("skill %s restored to %s", name, dest)
    return dest


def purge_archived(
    scope: Scope, items: Iterable[ArchivedSkill], *, ttl_days: float
) -> tuple[list[str], list[tuple[str, str]]]:
    """Delete archived copies for good (ledgered first); returns (purged names, failures)."""
    purged: list[str] = []
    failed: list[tuple[str, str]] = []
    for item in items:
        try:
            with ledger_port.ledgered(
                "purge",
                item.name,
                item.path,
                actor="user",
                evidence={"ttl_days": ttl_days},
                scope=scope,
            ) as out:
                shutil.rmtree(item.path)
                out["after"] = None
        except OSError as exc:
            failed.append((item.name, str(exc)))
            continue
        purged.append(item.name)
    return purged, failed
