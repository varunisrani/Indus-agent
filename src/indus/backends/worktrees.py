"""Session worktrees: create, check, prune (P11-19).

``create(scope, repo)`` makes ``<repo>/.worktrees/<name>`` on a new branch ``indus/<name>``
(default name ``indus-<YYYYmmdd-HHMMSS>-<hex4>``). The base is the freshest ref available
cheaply: the current branch's upstream (fetched within ``worktrees.fetch_timeout_s``), else the
remote default branch ``origin/HEAD`` (fetched), else ``HEAD``; a failed fetch uses the cached
remote ref. ``.worktrees/`` goes into the repo's ``.gitignore`` once, and the entries of
``.worktreeinclude`` (gitignored files such as ``.env``) are copied in when they stay inside both
trees. A failed ``worktree add`` removes the half-made tree, its admin entry and the branch, then
tries once more from ``HEAD`` when the base was a remote ref.

``removal_blockers(tree)`` lists what removing a tree would lose; any of these keeps it, and so
does any failure to check (fail safe toward keeping): changes to tracked files, untracked files
that are not ignored, commits on no other branch, tag or remote ref, an embedded repository
anywhere below the tree (ignored folders too, bounded walk), per-worktree refs. The branches
``main``, ``master``, ``develop``, ``dev`` and ``trunk`` are never deleted.

``prune(scope, repo, dry_run=)`` removes trees under ``.worktrees/`` older than
``worktrees.prune_after_hours`` (24) that have no blocker, and reports every other tree with its
reason. ``<home>/worktrees.json`` records ``{path, repo, branch, base, session_id, created_at}``.

Every git call is bounded (10 s; ``worktree add`` 120 s) and non-interactive (prompts off, stdin
closed); the environment comes from ``safety.child_env``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from indus.foundation.fsio import FileLock, atomic_write_json
from indus.foundation.scope import Scope
from indus.platform.locate import which
from indus.safety.child_env import child_env

__all__ = [
    "GIT_TIMEOUT",
    "PROTECTED_BRANCHES",
    "REGISTRY_FILE",
    "Git",
    "PruneReport",
    "TreeStatus",
    "WorktreeError",
    "WorktreeInfo",
    "bind_session",
    "clean_name",
    "create",
    "divergence_note",
    "git_for",
    "load_registry",
    "prune",
    "remove",
    "removal_blockers",
    "repo_root",
    "resolve_base",
    "statuses",
    "worktree_of",
]

log = logging.getLogger(__name__)

GIT_TIMEOUT = 10.0
ADD_TIMEOUT = 120.0
PROTECTED_BRANCHES = frozenset({"main", "master", "develop", "dev", "trunk"})
REGISTRY_FILE = "worktrees.json"
TREES_DIR = ".worktrees"
BRANCH_PREFIX = "indus/"
INCLUDE_FILE = ".worktreeinclude"
MAX_SCAN_ENTRIES = 200_000
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_PER_WORKTREE_REFS = ("refs/worktree/", "refs/bisect/", "refs/rewritten/")
# Dependency stores: never walked when looking for embedded repositories.
_OPAQUE_DIRS = frozenset(
    {"node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", ".tox"}
)


class WorktreeError(RuntimeError):
    """A worktree could not be made or git could not answer; the message is for the person."""


@dataclass(frozen=True)
class WorktreeInfo:
    path: str
    repo: str
    branch: str
    base: str
    session_id: str | None
    created_at: float


@dataclass(frozen=True)
class TreeStatus:
    path: str
    branch: str | None
    age_hours: float | None
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class PruneReport:
    removed: tuple[str, ...]
    kept: tuple[tuple[str, str], ...]
    dry_run: bool


@dataclass(frozen=True)
class _Run:
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0


class Git:
    """Runs git with one environment and a default bound."""

    def __init__(
        self,
        env: Mapping[str, str],
        *,
        git: str | None = None,
        timeout: float = GIT_TIMEOUT,
    ) -> None:
        self.env = dict(env)
        self.env.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"})
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            self.env.pop(name, None)
        self.binary = git if git is not None else which("git", path=self.env.get("PATH"))
        self.timeout = timeout

    def run(self, args: Sequence[str], cwd: str | Path, *, timeout: float | None = None) -> _Run:
        if not self.binary:
            raise WorktreeError("git is not installed (or not on PATH)")
        limit = timeout if timeout is not None else self.timeout
        try:
            done = subprocess.run(
                [self.binary, *args],
                cwd=str(cwd),
                env=self.env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=limit,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise WorktreeError(f"git {args[0]} did not finish within {limit:g}s") from None
        except OSError as exc:
            raise WorktreeError(f"git {args[0]} could not run: {exc.strerror or exc}") from None
        return _Run(
            done.returncode,
            done.stdout.decode("utf-8", "surrogateescape"),
            done.stderr.decode("utf-8", "replace").strip(),
        )

    def out(self, args: Sequence[str], cwd: str | Path, **kwargs: Any) -> str | None:
        """Stripped stdout of a successful call, else None."""
        result = self.run(args, cwd, **kwargs)
        return result.out.strip() if result.ok else None


def git_for(scope: Scope) -> Git:
    return Git(child_env(scope))


def _setting(scope: Scope, key: str, fallback: Any) -> Any:
    value = scope.config.get(key) if scope.config is not None else None
    if value is None or isinstance(value, bool) != isinstance(fallback, bool):
        return fallback
    if isinstance(fallback, int | float) and not isinstance(value, int | float):
        return fallback
    return value


# -- registry -------------------------------------------------------------------------------------


def _registry_path(scope: Scope) -> Path:
    return scope.home / REGISTRY_FILE


def load_registry(scope: Scope) -> list[dict[str, Any]]:
    try:
        data = json.loads(_registry_path(scope).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    items = data.get("worktrees") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [dict(item) for item in items if isinstance(item, dict) and item.get("path")]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]


def _update_registry(scope: Scope, change: Any) -> None:
    path = _registry_path(scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(path.with_name(path.name + ".lock")):
            entries = change(load_registry(scope))
            atomic_write_json(path, {"version": 1, "worktrees": entries}, mode=0o600)
    except OSError as exc:
        log.warning("the worktree registry could not be updated: %s", exc)


def _record(scope: Scope, info: WorktreeInfo) -> None:
    def change(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        kept = [e for e in entries if e.get("path") != info.path]
        return [*kept, asdict(info)]

    _update_registry(scope, change)


def _forget(scope: Scope, paths: Iterable[str]) -> None:
    gone = set(paths)
    _update_registry(scope, lambda entries: [e for e in entries if e.get("path") not in gone])


def bind_session(scope: Scope, path: str, session_id: str) -> None:
    """Record that ``session_id`` works in the tree at ``path``."""

    def change(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for entry in entries:
            if entry.get("path") == path:
                entry["session_id"] = session_id
        return entries

    _update_registry(scope, change)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def worktree_of(scope: Scope, folder: str | Path) -> str | None:
    """The registered worktree holding ``folder``, if any."""
    try:
        here = Path(folder).expanduser().resolve()
    except OSError:
        return None
    best: str | None = None
    for entry in load_registry(scope):
        root = Path(str(entry["path"]))
        if _inside(here, root) and (best is None or len(str(root)) > len(best)):
            best = str(root)
    return best


def divergence_note(scope: Scope, cwd: str | Path, paths: Iterable[str]) -> str | None:
    """A warning when the session works in a worktree and an edited path lies outside it."""
    tree = worktree_of(scope, cwd)
    if tree is None:
        return None
    root = Path(tree)
    for raw in paths:
        try:
            target = Path(raw).expanduser().resolve()
        except OSError:
            continue
        if not _inside(target, root):
            return (
                f"{raw} is outside this session's worktree ({tree}); the change lands in another "
                "checkout (for example the main one), not on the worktree's branch."
            )
    return None


# -- creating -------------------------------------------------------------------------------------


def repo_root(git: Git, folder: str | Path) -> Path | None:
    """The main checkout of the repository holding ``folder`` (also from inside a worktree)."""
    common = git.out(["rev-parse", "--path-format=absolute", "--git-common-dir"], folder)
    if common:
        common_dir = Path(common)
        if common_dir.name == ".git":
            return common_dir.parent
    top = git.out(["rev-parse", "--show-toplevel"], folder)
    return Path(top) if top else None


def clean_name(name: str) -> str:
    cleaned = _UNSAFE_NAME.sub("-", name).strip("-._")[:40]
    if not cleaned:
        raise WorktreeError(f"{name!r} is not usable as a worktree name")
    return cleaned


def _default_name(scope: Scope) -> str:
    now: datetime = scope.clock.now()
    return f"indus-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def _ref_exists(git: Git, repo: Path, ref: str) -> bool:
    return git.run(["rev-parse", "--verify", "--quiet", ref + "^{commit}"], repo).ok


def _refresh(git: Git, repo: Path, remote: str, branch: str, ref: str, timeout: float) -> str:
    try:
        fetched = git.run(["fetch", "--quiet", remote, branch], repo, timeout=timeout)
        if not fetched.ok:
            log.info("worktree base: fetch of %s failed: %s", ref, fetched.err)
    except WorktreeError as exc:
        log.info("worktree base: %s", exc)
    return ref if _ref_exists(git, repo, ref) else "HEAD"


def resolve_base(git: Git, repo: Path, *, fetch_timeout: float = 5.0, sync: bool = True) -> str:
    """The ref a new worktree starts from (see the module notes)."""
    if not sync:
        return "HEAD"
    branch = git.out(["symbolic-ref", "--quiet", "--short", "HEAD"], repo)
    if branch:
        remote = git.out(["config", "--get", f"branch.{branch}.remote"], repo)
        merge = git.out(["config", "--get", f"branch.{branch}.merge"], repo)
        upstream = git.out(["rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"], repo)
        if remote and remote != "." and merge and upstream:
            return _refresh(git, repo, remote, merge, upstream, fetch_timeout)
    default = git.out(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], repo)
    if default and default.startswith("origin/"):
        return _refresh(git, repo, "origin", default.split("/", 1)[1], default, fetch_timeout)
    return "HEAD"


def _ensure_ignored(repo: Path) -> None:
    ignore = repo / ".gitignore"
    line = f"{TREES_DIR}/"
    try:
        text = ignore.read_text(encoding="utf-8-sig") if ignore.exists() else ""
        if line in (entry.strip() for entry in text.splitlines()):
            return
        with ignore.open("a", encoding="utf-8") as handle:
            if text and not text.endswith("\n"):
                handle.write("\n")
            handle.write(line + "\n")
    except OSError as exc:
        log.warning("could not add %s to %s: %s", line, ignore, exc)


def _include_entries(repo: Path) -> list[str]:
    try:
        text = (repo / INCLUDE_FILE).read_text(encoding="utf-8-sig")
    except OSError:
        return []
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _copy_includes(repo: Path, tree: Path) -> list[str]:
    copied: list[str] = []
    repo_real, tree_real = repo.resolve(), tree.resolve()
    for entry in _include_entries(repo):
        if os.path.isabs(entry):
            log.warning("%s: skipped absolute entry %s", INCLUDE_FILE, entry)
            continue
        source, target = repo / entry, tree / entry
        if not _inside(source.resolve(), repo_real) or not _inside(target.resolve(), tree_real):
            log.warning("%s: skipped %s (it leaves the repository)", INCLUDE_FILE, entry)
            continue
        try:
            if source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            elif source.is_dir() and not target.exists():
                shutil.copytree(source, target, symlinks=True)
            else:
                continue
        except OSError as exc:
            log.warning("%s: could not copy %s: %s", INCLUDE_FILE, entry, exc)
            continue
        copied.append(entry)
    return copied


def _sweep_failed_add(git: Git, repo: Path, tree: Path, branch: str) -> None:
    with contextlib.suppress(WorktreeError):
        git.run(["worktree", "remove", "--force", str(tree)], repo)
    if tree.exists():
        shutil.rmtree(tree, ignore_errors=True)
    with contextlib.suppress(WorktreeError):
        git.run(["worktree", "prune"], repo)
        if _ref_exists(git, repo, f"refs/heads/{branch}"):
            git.run(["branch", "-D", branch], repo)


def _add(git: Git, repo: Path, tree: Path, branch: str, base: str) -> str | None:
    """``worktree add``; None on success, else git's complaint (leftovers already swept)."""
    try:
        added = git.run(
            ["worktree", "add", "--quiet", "--no-track", "-b", branch, str(tree), base],
            repo,
            timeout=ADD_TIMEOUT,
        )
    except WorktreeError as exc:
        _sweep_failed_add(git, repo, tree, branch)
        return str(exc)
    if added.ok:
        return None
    _sweep_failed_add(git, repo, tree, branch)
    return added.err or f"exit {added.code}"


def create(
    scope: Scope,
    repo: str | Path,
    name: str | None = None,
    *,
    session_id: str | None = None,
    git: Git | None = None,
) -> WorktreeInfo:
    """Make a new worktree of ``repo`` (see the module notes); raises ``WorktreeError``."""
    runner = git if git is not None else git_for(scope)
    root = repo_root(runner, repo)
    if root is None:
        raise WorktreeError(f"{repo} is not inside a git repository")
    tree_name = clean_name(name) if name else _default_name(scope)
    branch = BRANCH_PREFIX + tree_name
    tree = root / TREES_DIR / tree_name
    if tree.exists() or os.path.lexists(tree):
        raise WorktreeError(f"a worktree called {tree_name} already exists: {tree}")
    if _ref_exists(runner, root, f"refs/heads/{branch}"):
        raise WorktreeError(f"the branch {branch} already exists; choose another name")
    _ensure_ignored(root)
    tree.parent.mkdir(parents=True, exist_ok=True)
    base = resolve_base(
        runner,
        root,
        fetch_timeout=float(_setting(scope, "worktrees.fetch_timeout_s", 5.0)),
        sync=bool(_setting(scope, "worktrees.sync_base", True)),
    )
    problem = _add(runner, root, tree, branch, base)
    if problem is not None and base != "HEAD":
        log.warning("worktree from %s failed (%s); trying the local HEAD", base, problem)
        base = "HEAD"
        problem = _add(runner, root, tree, branch, base)
    if problem is not None:
        raise WorktreeError(f"the worktree could not be created: {problem}")
    _copy_includes(root, tree)
    info = WorktreeInfo(
        path=str(tree),
        repo=str(root),
        branch=branch,
        base=base,
        session_id=session_id,
        created_at=scope.clock.now().timestamp(),
    )
    _record(scope, info)
    return info


# -- checking and removing -------------------------------------------------------------------------


def _embedded_repos(tree: Path) -> tuple[list[str], bool]:
    """Folders below ``tree`` holding their own ``.git``; True when the walk gave up."""
    found: list[str] = []
    seen = 0
    for dirpath, dirnames, filenames in os.walk(tree, onerror=lambda _exc: None):
        here = Path(dirpath)
        if here != tree and (".git" in dirnames or ".git" in filenames):
            found.append(here.relative_to(tree).as_posix())
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d != ".git" and d not in _OPAQUE_DIRS]
        seen += len(dirnames) + len(filenames)
        if seen > MAX_SCAN_ENTRIES:
            return found, True
    return found, False


def _named(items: Sequence[str], limit: int = 5) -> str:
    more = f" (+{len(items) - limit} more)" if len(items) > limit else ""
    return ", ".join(items[:limit]) + more


def removal_blockers(git: Git, tree: str | Path) -> list[str]:
    """What removing ``tree`` would lose (empty: safe to remove). See the module notes."""
    folder = Path(tree)
    try:
        if not folder.is_dir():
            return []
        status = git.run(
            [
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=normal",
                "--ignore-submodules=none",
            ],
            folder,
        )
        if not status.ok:
            return [f"could not read its status: {status.err or status.code}"]
        entries = [e for e in status.out.split("\0") if e]
        reasons: list[str] = []
        tracked = [e[3:] for e in entries if not e.startswith("?? ") and len(e) > 3]
        untracked = [e[3:] for e in entries if e.startswith("?? ")]
        if tracked:
            reasons.append(f"uncommitted changes: {_named(tracked)}")
        if untracked:
            reasons.append(f"untracked files: {_named(untracked)}")
        branch = git.out(["symbolic-ref", "--quiet", "--short", "HEAD"], folder)
        args = ["rev-list", "--max-count=1", "HEAD", "--not"]
        if branch:
            args.append(f"--exclude={branch}")
        args += ["--branches", "--tags", "--remotes"]
        alone = git.run(args, folder)
        if not alone.ok:
            reasons.append(f"could not compare its commits: {alone.err or alone.code}")
        elif alone.out.strip():
            reasons.append("commits that no other branch, tag or remote holds")
        embedded, gave_up = _embedded_repos(folder)
        if embedded:
            reasons.append(f"embedded repository: {_named(embedded)}")
        elif gave_up:
            reasons.append("too many files to rule out an embedded repository")
        refs = git.run(["for-each-ref", "--format=%(refname)", *_PER_WORKTREE_REFS], folder)
        if not refs.ok:
            reasons.append(f"could not list its refs: {refs.err or refs.code}")
        elif refs.out.strip():
            reasons.append(f"per-worktree refs: {_named(refs.out.split())}")
        return reasons
    except (WorktreeError, OSError) as exc:
        return [f"could not check it: {exc}"]


def _registered_trees(git: Git, repo: Path) -> list[tuple[Path, str | None]]:
    listing = git.run(["worktree", "list", "--porcelain"], repo)
    if not listing.ok:
        raise WorktreeError(f"git worktree list failed: {listing.err}")
    trees: list[tuple[Path, str | None]] = []
    current: Path | None = None
    branch: str | None = None
    for line in [*listing.out.splitlines(), ""]:
        if line.startswith("worktree "):
            current, branch = Path(line[len("worktree ") :]), None
        elif line.startswith("branch "):
            branch = line[len("branch ") :].removeprefix("refs/heads/")
        elif not line and current is not None:
            if _inside(current, repo / TREES_DIR) and current != repo / TREES_DIR:
                trees.append((current, branch))
            current = None
    return trees


def statuses(scope: Scope, repo: str | Path, *, git: Git | None = None) -> list[TreeStatus]:
    """Every tree under ``<repo>/.worktrees`` with its age and blockers."""
    runner = git if git is not None else git_for(scope)
    root = repo_root(runner, repo)
    if root is None:
        raise WorktreeError(f"{repo} is not inside a git repository")
    created = {str(e["path"]): e.get("created_at") for e in load_registry(scope)}
    now = scope.clock.now().timestamp()
    out: list[TreeStatus] = []
    for tree, branch in _registered_trees(runner, root):
        stamp = created.get(str(tree))
        if not isinstance(stamp, int | float):
            try:
                stamp = tree.stat().st_mtime
            except OSError:
                stamp = None
        age = None if stamp is None else max(0.0, (now - float(stamp)) / 3600)
        out.append(TreeStatus(str(tree), branch, age, tuple(removal_blockers(runner, tree))))
    return out


def remove(
    scope: Scope, repo: str | Path, tree: str | Path, *, git: Git | None = None
) -> str | None:
    """Remove a tree that loses nothing; returns the reason it was kept, or None when removed.
    Its branch goes too unless protected."""
    runner = git if git is not None else git_for(scope)
    root = repo_root(runner, repo)
    if root is None:
        return f"{repo} is not inside a git repository"
    folder = Path(tree)
    blockers = removal_blockers(runner, folder)
    if blockers:
        return "; ".join(blockers)
    try:
        branch = runner.out(["symbolic-ref", "--quiet", "--short", "HEAD"], folder)
        removed = runner.run(["worktree", "remove", "--force", str(folder)], root)
        if not removed.ok:
            return f"git could not remove it: {removed.err}"
        if branch and branch not in PROTECTED_BRANCHES:
            deleted = runner.run(["branch", "-D", branch], root)
            if not deleted.ok:
                log.warning("branch %s was kept: %s", branch, deleted.err)
    except WorktreeError as exc:
        return str(exc)
    _forget(scope, [str(folder)])
    return None


def prune(
    scope: Scope,
    repo: str | Path,
    *,
    dry_run: bool = False,
    max_age_hours: float | None = None,
    git: Git | None = None,
) -> PruneReport:
    """Remove old trees that lose nothing; report the rest (see the module notes)."""
    runner = git if git is not None else git_for(scope)
    limit = (
        float(_setting(scope, "worktrees.prune_after_hours", 24))
        if max_age_hours is None
        else max_age_hours
    )
    removed: list[str] = []
    kept: list[tuple[str, str]] = []
    for item in statuses(scope, repo, git=runner):
        if item.age_hours is None or item.age_hours < limit:
            kept.append((item.path, f"younger than {limit:g} h"))
        elif item.blockers:
            kept.append((item.path, "; ".join(item.blockers)))
        elif dry_run:
            removed.append(item.path)
        else:
            reason = remove(scope, repo, item.path, git=runner)
            if reason is None:
                removed.append(item.path)
            else:
                kept.append((item.path, reason))
    if not dry_run:
        root = repo_root(runner, repo)
        if root is not None:
            with contextlib.suppress(WorktreeError):
                runner.run(["worktree", "prune"], root)
        gone = [str(e["path"]) for e in load_registry(scope) if not Path(str(e["path"])).exists()]
        if gone:
            _forget(scope, gone)
    return PruneReport(tuple(removed), tuple(kept), dry_run)
