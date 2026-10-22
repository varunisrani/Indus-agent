"""Skills inside a repository: found, counted, and loaded only once the user trusts it (P13-28).

A repo can carry skills for the agent in ``<root>/.indus/skills/`` and ``<root>/.agents/skills/``.
Anyone can put files in a repo, so those skills are an injection path (R07.26): they fill the
resolver's project tier only when the user trusted the root.

- The root is the nearest folder at or above the working directory that holds ``.git`` (a folder,
  or the file a worktree or submodule has). The walk stops after 64 levels, and the user's home
  never counts as a root (a dotfiles checkout there would make every folder a project).
- The working directory is the surface's own: ``bind_project_cwd`` (a cron job's ``workdir``, a
  session's folder) wins, then ``terminal.cwd``, then the process cwd.
- Trusted roots are ``skills.trusted_project_dirs``. ``trust_project`` adds one (``indus skills
  trust-project``); ``offer_trust`` asks once on an interactive surface and remembers a "no".
  Unattended surfaces (cron, API, ACP, gateways) never ask: they use the list as it is.
- ``skills.project_discovery: false`` switches the tier off.
- An untrusted root's skills are only counted (``ProjectSkills.pending``) for the banner; their
  files are never parsed.
- Every skill of a trusted root is scanned with ``safety.content_scan`` before it is indexed. The
  report is cached by the folder's content hash in ``<home>/cache/project_skill_scans/`` (outside
  the repo), so any edit means a new scan. ``dangerous`` (or a failed scan) quarantines the skill:
  it is left out of the index and lists, and loading it by name is refused with the reason.

Agent-created skills never land here (``SkillsSettings.write_root`` is never a project folder),
and the curator only manages skills of the profile's own folder.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from indus.foundation.fsio import atomic_write_json
from indus.foundation.home import expand_path_text
from indus.foundation.scope import Scope
from indus.learning.skills.format import SKILL_FILE
from indus.learning.skills.hub.lock import tree_hash
from indus.learning.skills.resolver import SKIP_DIRS, skills_setting
from indus.tools.builtin.paths import base_dir

__all__ = [
    "INTERACTIVE_SURFACES",
    "MAX_WALK",
    "PROJECT_SUBDIRS",
    "ProjectSkills",
    "bind_project_cwd",
    "find_project_root",
    "offer_trust",
    "project_cwd",
    "project_skills",
    "scan_project_skill",
    "trust_project",
    "trusted_roots",
]

log = logging.getLogger(__name__)

PROJECT_SUBDIRS: tuple[str, ...] = (".indus/skills", ".agents/skills")
MAX_WALK = 64
INTERACTIVE_SURFACES = frozenset({"cli", "tui", "desktop"})
SCAN_CACHE_DIR = Path("cache") / "project_skill_scans"
PROMPTS_FILE = Path("cache") / "project_skill_prompts.json"

_CWD: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "indus_project_cwd", default=None
)


@contextlib.contextmanager
def bind_project_cwd(path: Path | str | None) -> Iterator[None]:
    """Use ``path`` as the working directory for project skills inside the block."""
    token = _CWD.set(Path(path) if path else None)
    try:
        yield
    finally:
        _CWD.reset(token)


def project_cwd(scope: Scope) -> Path:
    bound = _CWD.get()
    return bound if bound is not None else Path(base_dir(scope))


def find_project_root(start: Path, *, home: Path | None = None) -> Path | None:
    """The nearest ancestor of ``start`` (itself included) holding ``.git``; None past 64 levels."""
    try:
        current = start.resolve()
    except OSError:
        return None
    stop = None
    if home is not None:
        with contextlib.suppress(OSError):
            stop = home.resolve()
    for _ in range(MAX_WALK):
        try:
            if (current / ".git").exists():
                return None if current == stop else current
        except OSError:
            return None
        if current.parent == current:
            return None
        current = current.parent
    return None


def _user_home(scope: Scope) -> Path:
    raw = scope.env.get("HOME")
    return Path(raw) if raw else Path.home()


def trusted_roots(scope: Scope) -> set[Path]:
    raw = skills_setting(scope, "skills.trusted_project_dirs")
    entries = (
        [raw] if isinstance(raw, str) else cast(list[Any], raw) if isinstance(raw, list) else []
    )
    home = _user_home(scope)
    roots: set[Path] = set()
    for entry in entries:
        text = str(entry).strip()
        if not text:
            continue
        with contextlib.suppress(OSError):
            roots.add(expand_path_text(text, dict(scope.env), lambda: home).resolve())
    return roots


def _skill_dirs(folder: Path) -> list[Path]:
    """Skill folders under ``folder`` (the same walk rules as the resolver)."""
    found: list[Path] = []
    for current, dirs, files in os.walk(folder, followlinks=True):
        here = Path(current)
        if SKILL_FILE in files and here != folder:
            found.append(here)
            dirs[:] = []
            continue
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS)
    return found


@dataclass(frozen=True)
class ProjectSkills:
    """The project tier for one working directory."""

    root: Path | None = None
    trusted: bool = False
    dirs: tuple[Path, ...] = ()
    found: int = 0
    quarantined: Mapping[str, str] = field(default_factory=dict[str, str])
    """``realpath(skill folder)`` -> why it is quarantined (trusted roots only)."""

    @property
    def pending(self) -> int:
        """Skills found in an untrusted root (shown, never loaded)."""
        return 0 if self.trusted else self.found

    @property
    def load_dirs(self) -> tuple[Path, ...]:
        return self.dirs if self.trusted else ()


def _candidate_dirs(scope: Scope, root: Path) -> tuple[Path, ...]:
    own = (scope.home / "skills").resolve()
    dirs: list[Path] = []
    for sub in PROJECT_SUBDIRS:
        candidate = root / sub
        try:
            if candidate.is_dir() and candidate.resolve() != own:
                dirs.append(candidate.resolve())
        except OSError:
            continue
    return tuple(dict.fromkeys(dirs))


def project_skills(scope: Scope, cwd: Path | None = None) -> ProjectSkills:
    """Find the root for ``cwd`` (default ``project_cwd``), its trust and its skills."""
    if skills_setting(scope, "skills.project_discovery") is False:
        return ProjectSkills()
    root = find_project_root(cwd or project_cwd(scope), home=_user_home(scope))
    if root is None:
        return ProjectSkills()
    dirs = _candidate_dirs(scope, root)
    trusted = root in trusted_roots(scope)
    skill_dirs = [d for folder in dirs for d in _skill_dirs(folder)]
    quarantined: dict[str, str] = {}
    if trusted:
        for skill_dir in skill_dirs:
            reason = quarantine_reason(scope, skill_dir)
            if reason:
                quarantined[os.path.realpath(skill_dir)] = reason
    return ProjectSkills(root, trusted, dirs, len(skill_dirs), quarantined)


# -- scanning -----------------------------------------------------------------------------------


def _cache_path(scope: Scope, digest: str) -> Path:
    return scope.home / SCAN_CACHE_DIR / f"{digest.removeprefix('sha256:')}.json"


def scan_project_skill(scope: Scope, skill_dir: Path) -> dict[str, Any]:
    """``{scanner, verdict, summary, report}`` for ``skill_dir``, from the cache when the folder's
    content hash and the scanner id match, else scanned now and cached."""
    # Imported here: the scanner's patterns cost ~25 ms to compile and a run whose project has no
    # trusted skills never scans (W48-g-3).
    from indus.safety.content_scan import SCANNER_ID, render_report, scan_tree

    digest = tree_hash(skill_dir)
    path = _cache_path(scope, digest)
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(cached, dict) and cached.get("scanner") == SCANNER_ID:
            return cast(dict[str, Any], cached)
    except (OSError, ValueError):
        pass
    report = scan_tree(skill_dir, kind="skill", name=skill_dir.name)
    row: dict[str, Any] = {
        "scanner": report.scanner,
        "hash": digest,
        "verdict": report.verdict,
        "summary": report.summary(),
        "report": render_report(report),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, row)
    except OSError:
        log.debug("could not cache the scan of %s", skill_dir, exc_info=True)
    return row


def quarantine_reason(scope: Scope, skill_dir: Path) -> str:
    """Why ``skill_dir`` must not load ("" when it may). A scan that fails quarantines."""
    try:
        row = scan_project_skill(scope, skill_dir)
    except Exception:
        log.warning("project skill scan failed for %s; quarantined", skill_dir, exc_info=True)
        return "the content scan could not run on it"
    if row.get("verdict") == "dangerous":
        log.warning("project skill quarantined: %s (%s)", skill_dir, row.get("summary"))
        return f"the content scan found dangerous content ({row.get('summary')})"
    return ""


# -- trust decisions ----------------------------------------------------------------------------


def trust_project(scope: Scope, path: Path | None = None) -> Path:
    """Add the project root of ``path`` (default: the working directory) to the trusted list.

    Raises ``ValueError`` when ``path`` is not inside a repository.
    """
    from indus.foundation.config import loader, writer

    start = path or project_cwd(scope)
    root = find_project_root(start, home=_user_home(scope))
    if root is None:
        raise ValueError(f"{start} is not inside a git repository (no .git found above it)")
    current = loader.load(scope).get("skills.trusted_project_dirs")
    listed = [str(v) for v in cast(list[Any], current)] if isinstance(current, list) else []
    if root not in trusted_roots(scope):
        writer.save(scope, {"skills": {"trusted_project_dirs": [*listed, str(root)]}})
    return root


def _declined(scope: Scope) -> set[str]:
    try:
        data = json.loads((scope.home / PROMPTS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    rows = cast(dict[str, Any], data).get("declined") if isinstance(data, dict) else None
    return {str(r) for r in cast(list[Any], rows)} if isinstance(rows, list) else set()


def _remember_decline(scope: Scope, root: Path) -> None:
    path = scope.home / PROMPTS_FILE
    rows = sorted(_declined(scope) | {str(root)})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, {"declined": rows})
    except OSError:
        log.debug("could not remember the declined project %s", root, exc_info=True)


def offer_trust(
    scope: Scope,
    *,
    platform: str,
    ask: Callable[[Path, int], bool | None],
    cwd: Path | None = None,
) -> ProjectSkills:
    """Ask once whether to trust the current repo's skills, on interactive surfaces only.

    ``ask(root, count)`` answers True (trust), False (remembered, never asked again) or None (no
    answer: ask next time). Unattended surfaces return the state without asking.
    """
    state = project_skills(scope, cwd)
    if platform not in INTERACTIVE_SURFACES or state.root is None or not state.pending:
        return state
    if str(state.root) in _declined(scope):
        return state
    answer = ask(state.root, state.pending)
    if answer is None:
        return state
    if not answer:
        _remember_decline(scope, state.root)
        return state
    trust_project(scope, state.root)
    return project_skills(scope, cwd)
