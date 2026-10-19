"""The hub install pipeline and the life of an installed hub skill (P13-26).

``install`` runs, in order:

1. resolve the identifier to one adapter (a pinned source for updates, the identifier's shape, or
   a search for a bare name) and fetch the bundle;
2. write it to ``<home>/skills/.hub/quarantine/<id>/`` — only the files the adapter fetched
   (``SKILL.md`` plus the support files it refers to);
3. check the format (``check_skill_text``; a skill that would not load is refused), lint it
   (advice only) and scan it with ``safety.content_scan``;
4. apply the trust × verdict policy (``POLICY``); ``force`` lifts a ``caution`` block, never a
   ``dangerous`` one;
5. check the target: no link on the way, never onto a category folder, never inside another
   skill, never over a skill the hub did not install; then move it to
   ``<home>/skills/[category/]<name>/``;
6. record the lock entry (``hub.lock``) and one audit line.

Open sessions need nothing more: the catalog rescans, and the P09 index delta tells the model
about the new skill on its next message.

``check_updates`` compares each entry's upstream bundle hash with the recorded one (orphaned and
unsafe entries are reported without any network call and never removed); ``update`` reinstalls
only changed skills and keeps locally edited ones unless forced; ``audit`` re-scans every hub
skill; ``uninstall`` removes the folder an entry names and the entry.

An optional second opinion: when ``skills.hub.advisory_scanner`` names a program on ``PATH`` it
is run on the quarantined folder and its output is shown; it never blocks.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from indus.foundation.clock import Clock
from indus.foundation.scope import Scope
from indus.learning.skills.format import (
    SKILL_FILE,
    SkillFormatError,
    check_category,
    check_name,
    check_skill_text,
)
from indus.learning.skills.hub.lock import (
    QUARANTINE_DIR,
    HubLock,
    append_audit,
    bundle_hash,
    hub_dir,
    is_redirect,
    safe_rel_path,
    safe_skill_name,
    tree_hash,
)
from indus.learning.skills.hub.sources import (
    Bundle,
    HubError,
    SourceSet,
    find_by_name,
    frontmatter_of,
)
from indus.learning.skills.lint import LintFinding, lint_skill
from indus.safety.child_env import child_env
from indus.safety.content_scan import ScanReport, Verdict, render_report, scan_tree

__all__ = [
    "POLICY",
    "AuditRow",
    "InstallOptions",
    "InstallResult",
    "Preview",
    "UpdateRow",
    "audit",
    "check_updates",
    "decide",
    "inspect",
    "install",
    "uninstall",
    "update",
]

log = logging.getLogger(__name__)

ADVISORY_TIMEOUT_S = 60.0

POLICY: Mapping[str, Mapping[str, bool]] = {
    "builtin": {"safe": True, "caution": True, "dangerous": True},
    "trusted": {"safe": True, "caution": True, "dangerous": False},
    "community": {"safe": True, "caution": False, "dangerous": False},
}

Status = Literal["installed", "blocked", "declined", "exists", "failed"]


def decide(trust: str, verdict: Verdict, *, force: bool = False) -> tuple[bool, str]:
    """``(allowed, why)`` for a scanned bundle."""
    row = POLICY.get(trust, POLICY["community"])
    if row.get(verdict, False):
        return True, f"{verdict} from a {trust} source"
    if verdict == "caution" and force:
        return True, f"caution from a {trust} source, installed because of --force"
    if verdict == "dangerous":
        return False, (
            f"the scan found dangerous content in a {trust} skill; it is never installed, "
            "--force included"
        )
    return False, f"the scan flagged this {trust} skill (caution); --force installs it anyway"


# -- records ------------------------------------------------------------------------------------


@dataclass
class InstallOptions:
    """How one install runs. ``confirm`` / ``ask_name`` are the surface's prompts; ``None`` means
    the surface cannot ask (non-interactive)."""

    category: str = ""
    name: str = ""
    force: bool = False
    replace: bool = False
    """Replace an existing hub install of the same name (``--force`` on install, every update)."""
    source_id: str | None = None
    confirm: Callable[[Preview], bool] | None = None
    ask_name: Callable[[str], str | None] | None = None
    say: Callable[[str], None] = field(default=lambda _line: None)


@dataclass
class Preview:
    """A scanned bundle in quarantine, before the move."""

    bundle: Bundle
    name: str
    category: str
    install_path: str
    quarantine: Path
    report: ScanReport
    lint: list[LintFinding]
    allowed: bool
    reason: str

    def render(self) -> str:
        lines = [
            f"Skill:      {self.name}",
            f"Source:     {self.bundle.source} ({self.bundle.trust})",
            f"Identifier: {self.bundle.identifier}",
            f"Files:      {', '.join(sorted(self.bundle.files))}",
            f"Goes to:    skills/{self.install_path}/",
        ]
        if self.bundle.missing:
            lines.append(f"Not found upstream (skipped): {', '.join(self.bundle.missing)}")
        lines.append(render_report(self.report))
        lines.extend(f"lint  {f.rule}: {f.message}" for f in self.lint)
        lines.append(("Allowed: " if self.allowed else "Blocked: ") + self.reason)
        return "\n".join(lines)


@dataclass
class InstallResult:
    status: Status
    name: str = ""
    message: str = ""
    preview: Preview | None = None
    install_dir: Path | None = None
    entry: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.status == "installed"


# -- helpers ------------------------------------------------------------------------------------


def skills_root(scope: Scope) -> Path:
    return scope.home / "skills"


def _quarantine_root(scope: Scope) -> Path:
    return hub_dir(skills_root(scope)) / QUARANTINE_DIR


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", text.strip().lower()).strip("-.")


def _clean_category(raw: str, *, strict: bool) -> str:
    """A category path; a user's value must already be clean, a source's label is slugified."""
    if not raw.strip():
        return ""
    try:
        clean = safe_rel_path(raw, what="category")
    except ValueError as exc:
        raise HubError(str(exc)) from None
    parts = clean.split("/") if strict else [_slug(p) for p in clean.split("/")]
    for part in parts:
        problem = check_category(part)
        if problem is not None:
            raise HubError(problem)
    return "/".join(p for p in parts if p)


_NAME_LINE = re.compile(r"^name:.*$", re.MULTILINE)


def _with_name(text: str, name: str) -> str:
    """``text`` whose top-level frontmatter ``name`` is ``name`` (added when absent)."""
    content = text.removeprefix("﻿")
    first, sep, rest = content.partition("\n")
    if first.rstrip() != "---":
        return f"---\nname: {name}\n---\n\n{content}"
    end = re.search(r"^---[ \t]*$", rest, re.MULTILINE)
    if end is None:
        return content
    block, tail = rest[: end.start()], rest[end.start() :]
    if _NAME_LINE.search(block):
        block = _NAME_LINE.sub(f"name: {name}", block, count=1)
    else:
        block = f"name: {name}\n{block}"
    return f"{first}{sep}{block}{tail}"


def _write_quarantine(scope: Scope, bundle: Bundle, name: str) -> Path:
    tag = hashlib.sha256(bundle.identifier.encode("utf-8")).hexdigest()[:8]
    dest = _quarantine_root(scope) / f"{name}-{tag}"
    if dest.is_symlink() or dest.is_file():
        dest.unlink()
    elif dest.exists():
        shutil.rmtree(dest)
    checked = [(safe_rel_path(rel, what="bundle file"), data) for rel, data in bundle.files.items()]
    dest.mkdir(parents=True)
    for rel, data in checked:
        target = dest.joinpath(*rel.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return dest


def _discard(path: Path | None) -> None:
    if path is not None and path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _target_problem(root: Path, install_path: str, lock: HubLock, name: str, replace: bool) -> str:
    """Why ``root/install_path`` cannot take this skill ("" when it can)."""
    target = root
    for part in install_path.split("/"):
        target = target / part
        if is_redirect(target):
            return f"skills/{install_path} goes through a symlink; refused"
        if target != root / install_path and (target / SKILL_FILE).is_file():
            return f"skills/{target.relative_to(root).as_posix()} is a skill, not a category"
    if not target.exists():
        return ""
    if not target.is_dir():
        return f"skills/{install_path} exists and is not a folder"
    if not (target / SKILL_FILE).is_file():
        held = sorted(p.parent.name for p in target.rglob(SKILL_FILE))
        if held:
            return f"skills/{install_path} is a category holding {', '.join(held)}; use --name"
        return ""
    entry = lock.get(name)
    if entry is None or entry.get("install_path") != install_path:
        return f"skills/{install_path} is a skill the hub did not install; use another --name"
    if not replace:
        return f"{name!r} is already installed; use --force to reinstall it"
    return ""


def _name_taken(scope: Scope, name: str, install_path: str, lock: HubLock) -> str:
    """Another skill of this profile already answering to ``name`` (not the hub copy)."""
    from indus.learning.skills.resolver import TIER_LOCAL, load_catalog

    for rec in load_catalog(scope).records:
        if rec.name != name or rec.tier != TIER_LOCAL:
            continue
        if rec.relative_path == install_path and lock.get(name) is not None:
            continue
        return f"a skill called {name!r} already exists at {rec.skill_dir}; use --name"
    return ""


def _advisory(scope: Scope, folder: Path, say: Callable[[str], None]) -> None:
    from indus.learning.skills.resolver import skills_setting

    program = skills_setting(scope, "skills.hub.advisory_scanner")
    if not isinstance(program, str) or not program.strip():
        return
    from indus.platform.locate import which

    found = which(program.strip(), scope.env.get("PATH"))
    if found is None:
        return
    try:
        done = subprocess.run(
            [found, str(folder)],
            env=child_env(scope),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=ADVISORY_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        say(f"advisory scanner {program} did not run: {exc}")
        return
    text = (done.stdout or done.stderr or "").strip()
    say(f"advisory scanner {program} (does not block):")
    say(text or f"(no output, exit {done.returncode})")


# -- fetch and stage ----------------------------------------------------------------------------


async def fetch_bundle(
    sources: SourceSet, identifier: str, *, source_id: str | None = None, clock: Clock | None = None
) -> Bundle:
    ident = identifier.strip()
    if not ident:
        raise HubError("no skill identifier given")
    if source_id:
        source = sources.by_id(source_id)
        if source is None:
            raise HubError(f"the source {source_id!r} that installed this skill is not available")
    else:
        source = sources.for_identifier(ident)
        if source is None:
            ident = await find_by_name(sources, ident, clock=clock)
            source = sources.for_identifier(ident)
        if source is None:
            raise HubError(f"no source understands {identifier!r}")
    bundle = await source.fetch(ident)
    if bundle is None:
        raise HubError(f"{identifier!r} was not found in {source.id}")
    if SKILL_FILE not in bundle.files:
        raise HubError(f"{identifier!r} has no SKILL.md")
    return bundle


def _choose_name(bundle: Bundle, options: InstallOptions) -> str:
    name = options.name.strip() or bundle.name.strip()
    if not name and options.ask_name is not None:
        name = (options.ask_name(bundle.identifier) or "").strip()
    if not name:
        raise HubError(
            f"could not work out a name for {bundle.identifier}; pass --name <name> to choose one"
        )
    problem = check_name(name)
    if problem is not None:
        raise HubError(problem)
    return safe_skill_name(name)


def stage(scope: Scope, bundle: Bundle, options: InstallOptions) -> Preview:
    """Quarantine, check, lint and scan ``bundle``; nothing outside the quarantine changes."""
    name = _choose_name(bundle, options)
    category = (
        _clean_category(options.category, strict=True)
        if options.category.strip()
        else _clean_category(bundle.category, strict=False)
    )
    try:
        text = bundle.skill_md
    except UnicodeDecodeError:
        raise HubError(f"{name}: SKILL.md is not UTF-8 text") from None
    if _frontmatter_name(text) != name:
        text = _with_name(text, name)
    staged = replace(bundle, files={**bundle.files, SKILL_FILE: text.encode("utf-8")})
    quarantine = _write_quarantine(scope, staged, name)
    try:
        check_skill_text(text, expected_name=name)
    except SkillFormatError as exc:
        _discard(quarantine)
        raise HubError(f"{name}: not a valid skill ({'; '.join(exc.problems)})") from None
    lint = lint_skill(text, quarantine)
    report = scan_tree(quarantine, kind="skill", name=name)
    allowed, reason = decide(bundle.trust, report.verdict, force=options.force)
    install_path = f"{category}/{name}" if category else name
    return Preview(staged, name, category, install_path, quarantine, report, lint, allowed, reason)


def _frontmatter_name(text: str) -> str | None:
    value = frontmatter_of(text).get("name")
    return value.strip() if isinstance(value, str) else None


# -- install ------------------------------------------------------------------------------------


def _audit(scope: Scope, action: str, preview: Preview | Bundle, verdict: str, detail: str) -> None:
    bundle = preview.bundle if isinstance(preview, Preview) else preview
    name = preview.name if isinstance(preview, Preview) else (bundle.name or "?")
    append_audit(
        skills_root(scope),
        action,
        name,
        source=bundle.source,
        trust=bundle.trust,
        verdict=verdict,
        detail=detail,
        now=scope.clock.now(),
    )


def commit(scope: Scope, preview: Preview, *, replace: bool = False) -> InstallResult:
    """Move a staged, allowed skill into place and record it."""
    root = skills_root(scope)
    lock = HubLock(root)
    problem = _target_problem(root, preview.install_path, lock, preview.name, replace)
    problem = problem or _name_taken(scope, preview.name, preview.install_path, lock)
    for path in preview.quarantine.rglob("*"):
        if is_redirect(path):
            problem = problem or f"the bundle holds a link ({path.name}); refused"
    if problem:
        _discard(preview.quarantine)
        _audit(scope, "REFUSED", preview, preview.report.verdict, problem)
        return InstallResult("failed", preview.name, problem, preview)
    target = root / preview.install_path
    old: Path | None = None
    if target.exists():
        old = preview.quarantine.with_name(preview.quarantine.name + ".old")
        _discard(old)
        target.rename(old)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(preview.quarantine), str(target))
    except OSError:
        if old is not None and not target.exists():
            old.rename(target)
        raise
    _discard(old)
    entry: dict[str, Any] = {
        "name": preview.name,
        "source": preview.bundle.source,
        "identifier": preview.bundle.identifier,
        "install_path": preview.install_path,
        "content_hash": tree_hash(target),
        "upstream_hash": str(
            preview.bundle.metadata.get("upstream_hash") or bundle_hash(preview.bundle.files)
        ),
        "trust": preview.bundle.trust,
        "verdict": preview.report.verdict,
        "scanner_version": preview.report.scanner,
        "findings": [f.to_dict() for f in preview.report.findings],
        "files": sorted(preview.bundle.files),
        "ts": scope.clock.now().isoformat(),
    }
    lock.record(entry)
    _audit(scope, "INSTALL", preview, preview.report.verdict, entry["content_hash"])
    return InstallResult(
        "installed",
        preview.name,
        f"installed skills/{preview.install_path}",
        preview,
        target,
        entry,
    )


async def install(
    scope: Scope,
    sources: SourceSet,
    identifier: str,
    options: InstallOptions | None = None,
) -> InstallResult:
    """Fetch, quarantine, scan, decide and (when allowed and confirmed) install one skill."""
    opts = options or InstallOptions()
    try:
        bundle = await fetch_bundle(
            sources, identifier, source_id=opts.source_id, clock=scope.clock
        )
    except HubError as exc:
        return InstallResult("failed", "", str(exc))
    bundle.metadata["upstream_hash"] = bundle_hash(bundle.files)
    try:
        preview = stage(scope, bundle, opts)
    except (HubError, ValueError) as exc:
        _audit(scope, "REFUSED", bundle, "n/a", str(exc))
        return InstallResult("failed", bundle.name, str(exc))
    opts.say(preview.render())
    if not preview.allowed:
        _discard(preview.quarantine)
        _audit(
            scope,
            "BLOCKED",
            preview,
            preview.report.verdict,
            f"{len(preview.report.findings)}_findings",
        )
        return InstallResult("blocked", preview.name, preview.reason, preview)
    _advisory(scope, preview.quarantine, opts.say)
    if opts.confirm is not None and not opts.confirm(preview):
        _discard(preview.quarantine)
        return InstallResult("declined", preview.name, "nothing installed", preview)
    replace = opts.replace or opts.force
    entry = HubLock(skills_root(scope)).get(preview.name)
    if entry is not None and not replace:
        _discard(preview.quarantine)
        return InstallResult(
            "exists",
            preview.name,
            f"{preview.name!r} is already installed; --force reinstalls it",
            preview,
        )
    return commit(scope, preview, replace=replace)


async def inspect(scope: Scope, sources: SourceSet, identifier: str, *, name: str = "") -> Preview:
    """Fetch and scan into quarantine, then remove the quarantine copy: nothing is installed."""
    bundle = await fetch_bundle(sources, identifier, clock=scope.clock)
    bundle.metadata["upstream_hash"] = bundle_hash(bundle.files)
    if not bundle.name and not name:
        name = "unnamed-skill"
    preview = stage(scope, bundle, InstallOptions(name=name))
    _discard(preview.quarantine)
    return preview


# -- lifecycle ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class UpdateRow:
    name: str
    source: str
    identifier: str
    status: Literal["up_to_date", "update_available", "orphaned", "unsafe", "unavailable"]
    detail: str = ""


async def check_updates(
    scope: Scope, sources: SourceSet, name: str | None = None
) -> list[UpdateRow]:
    lock = HubLock(skills_root(scope))
    rows: list[UpdateRow] = []
    for entry_name, entry in sorted(lock.installed().items()):
        if name is not None and entry_name != name:
            continue
        source_id, ident = str(entry.get("source", "")), str(entry.get("identifier", ""))
        base = (entry_name, source_id, ident)

        try:
            folder = lock.install_dir(entry)
        except ValueError as exc:
            rows.append(UpdateRow(*base, "unsafe", str(exc)))
            continue
        if not folder.is_dir():
            gone = f"skills/{entry.get('install_path')} is missing"
            rows.append(UpdateRow(*base, "orphaned", gone))
            continue
        try:
            bundle = await fetch_bundle(sources, ident, source_id=source_id, clock=scope.clock)
        except HubError as exc:
            rows.append(UpdateRow(*base, "unavailable", str(exc)))
            continue
        recorded = str(entry.get("upstream_hash") or entry.get("content_hash") or "")
        latest = bundle_hash(bundle.files)
        status = "up_to_date" if latest == recorded else "update_available"
        rows.append(UpdateRow(*base, status))
    return rows


@dataclass
class UpdateReport:
    updated: list[str] = field(default_factory=list[str])
    kept_local: list[str] = field(default_factory=list[str])
    failed: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    rows: list[UpdateRow] = field(default_factory=list[UpdateRow])


def has_local_edits(scope: Scope, entry: Mapping[str, Any]) -> bool:
    lock = HubLock(skills_root(scope))
    try:
        folder = lock.install_dir(entry)
    except ValueError:
        return False
    recorded = str(entry.get("content_hash") or "")
    return bool(recorded) and folder.is_dir() and tree_hash(folder) != recorded


async def update(
    scope: Scope,
    sources: SourceSet,
    name: str | None = None,
    *,
    force: bool = False,
    say: Callable[[str], None] = lambda _line: None,
) -> UpdateReport:
    """Reinstall the hub skills whose upstream changed; locally edited ones only with ``force``."""
    report = UpdateReport(rows=await check_updates(scope, sources, name))
    lock = HubLock(skills_root(scope))
    for row in report.rows:
        if row.status != "update_available":
            continue
        entry = lock.get(row.name)
        if entry is None:
            continue
        if not force and has_local_edits(scope, entry):
            report.kept_local.append(row.name)
            continue
        parent = str(entry.get("install_path", "")).rpartition("/")[0]
        result = await install(
            scope,
            sources,
            row.identifier,
            InstallOptions(
                category=parent, name=row.name, replace=True, source_id=row.source, say=say
            ),
        )
        if result.ok:
            report.updated.append(row.name)
        else:
            report.failed.append((row.name, result.message))
    return report


@dataclass(frozen=True)
class AuditRow:
    name: str
    report: ScanReport | None
    detail: str = ""


def audit(scope: Scope, name: str | None = None) -> list[AuditRow]:
    """Re-scan every hub skill (or one) where it is installed."""
    lock = HubLock(skills_root(scope))
    rows: list[AuditRow] = []
    for entry_name, entry in sorted(lock.installed().items()):
        if name is not None and entry_name != name:
            continue
        try:
            folder = lock.install_dir(entry)
        except ValueError as exc:
            rows.append(AuditRow(entry_name, None, str(exc)))
            continue
        if not folder.is_dir():
            missing = f"skills/{entry.get('install_path')} is missing"
            rows.append(AuditRow(entry_name, None, missing))
            continue
        rows.append(AuditRow(entry_name, scan_tree(folder, kind="skill", name=entry_name)))
    return rows


def uninstall(scope: Scope, name: str) -> str:
    """Remove a hub skill's folder and lock entry; ``HubError`` when it is not a hub skill."""
    root = skills_root(scope)
    lock = HubLock(root)
    entry = lock.get(name)
    if entry is None:
        raise HubError(f"{name!r} was not installed from the hub")
    try:
        folder = lock.install_dir(entry)
    except ValueError as exc:
        raise HubError(f"refusing to remove {name!r}: {exc}") from None
    if folder.is_dir():
        shutil.rmtree(folder)
    lock.forget(name)
    append_audit(
        root,
        "UNINSTALL",
        name,
        source=str(entry.get("source", "")),
        trust=str(entry.get("trust") or "community"),
        verdict="n/a",
        detail="user",
        now=scope.clock.now(),
    )
    return f"removed skills/{entry.get('install_path')}"
