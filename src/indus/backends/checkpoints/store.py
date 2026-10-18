"""The shared shadow git store behind checkpoints and ``/rollback`` (P11-15).

Layout under ``<home>/checkpoints/``::

    store/                      a bare repository; its objects are shared by every project
        refs/indus/<key>        one project's checkpoint history (``key`` = sha256(folder)[:16])
        indexes/<key>           that project's staging index
        projects/<key>.json     {workdir, created_at, last_touch}
        ledgers/<key>.jsonl     the newest 2,000 snapshot entries of that project
        info/exclude            paths never captured
    .last_prune                 when the automatic prune last ran (``prune.py``)

Every git call gets ``GIT_DIR`` (the store), ``GIT_WORK_TREE`` (the folder, when one is involved)
and ``GIT_INDEX_FILE`` (an index of ours), with the user's global and system git config switched
off, a fixed identity and dates from the scope clock. Nothing reads or writes the user's own
repository. Calls are bounded (30 s; staging gets twice that) and a missing git or a timeout is a
``GitError`` that the public methods turn into ``Skipped``.

``ShadowStore`` implements ``checkpoint_api.CheckpointStore`` (``base``, ``list_projects``,
``size``, ``prune``, ``clear``) for the ``indus checkpoints`` command; ``store_factory`` is what
``checkpoint_api.set_store_factory`` installs.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from indus.backends.checkpoint_api import (
    CheckpointProject,
    ClearResult,
    PrunePlan,
    PruneResult,
)
from indus.foundation.clock import Clock, SystemClock
from indus.foundation.fsio import FileLock, atomic_write_json, atomic_write_text
from indus.foundation.scope import Scope
from indus.platform.locate import which
from indus.safety.child_env import child_env

__all__ = [
    "DEFAULT_EXCLUDES",
    "GIT_TIMEOUT",
    "LEDGER_MAX",
    "MAX_FILES",
    "REF_PREFIX",
    "GitError",
    "GitResult",
    "ShadowStore",
    "Skipped",
    "Snapshot",
    "check_commit",
    "check_relpath",
    "config_value",
    "parse_trailers",
    "project_key",
    "store_factory",
]

log = logging.getLogger(__name__)

REF_PREFIX = "refs/indus"
MAX_FILES = 50_000
GIT_TIMEOUT = 30.0
LEDGER_MAX = 2_000
MB = 1024 * 1024

# Folders that are never captured, so they are not counted towards the file limit either.
_UNCOUNTED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".worktrees",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".cache",
        ".next",
        "dist",
        "build",
        "target",
    }
)

DEFAULT_EXCLUDES: tuple[str, ...] = (
    # version control and our own worktrees
    *(".git/", ".hg/", ".svn/", ".worktrees/"),
    # environments, dependency folders and tool caches
    *("node_modules/", ".venv/", "venv/", "env/", "__pycache__/", "*.py[co]", ".tox/"),
    *(".mypy_cache/", ".pytest_cache/", ".ruff_cache/", ".cache/", ".coverage", "coverage/"),
    # build output
    *("dist/", "build/", "target/", "out/", ".next/", ".nuxt/"),
    # compiled and packaged binaries
    *("*.o", "*.a", "*.so", "*.dylib", "*.dll", "*.exe", "*.obj", "*.class", "*.jar"),
    *("*.zip", "*.tar", "*.tar.gz", "*.tgz", "*.7z", "*.rar", "*.iso"),
    # media
    *("*.mp4", "*.mov", "*.mkv", "*.webm"),
    # secrets, logs and desktop clutter
    *(".env", ".env.*", "*.log", ".DS_Store", "Thumbs.db"),
)

_HEX = re.compile(r"[0-9a-f]{4,64}")
_TRAILER_SESSION = "Indus-Session"
_TRAILER_TURN = "Indus-Turn"

SkipKind = Literal[
    "missing", "too_broad", "too_many_files", "empty", "unchanged", "unsupported", "error"
]


class GitError(RuntimeError):
    """git could not run, timed out, or a step the caller needs failed."""


@dataclass(frozen=True)
class GitResult:
    code: int
    out: bytes
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def text(self) -> str:
        return self.out.decode("utf-8", "surrogateescape").strip()

    def fields(self) -> list[str]:
        """NUL-separated output (``-z``) as names, empty ones dropped."""
        return [part for part in self.out.decode("utf-8", "surrogateescape").split("\0") if part]


@dataclass(frozen=True)
class Snapshot:
    commit: str
    key: str
    workdir: str
    files_changed: int
    nested: tuple[str, ...] = ()


@dataclass(frozen=True)
class Skipped:
    kind: SkipKind
    reason: str


# -- validation ------------------------------------------------------------------------------------


def project_key(workdir: str | Path) -> str:
    """The project id of a folder: sha256 of its resolved absolute path, 16 hex characters."""
    resolved = str(Path(workdir).expanduser().resolve())
    return hashlib.sha256(resolved.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def check_commit(text: str) -> str:
    """``text`` when it is a plain lower-case hex commit id (4–64 chars), else ``ValueError``.
    Anything starting with ``-`` would reach git as an option, so it never passes."""
    value = text.strip().lower() if isinstance(text, str) else ""
    if not value or not _HEX.fullmatch(value):
        raise ValueError(f"not a checkpoint id: {text!r}")
    return value


def check_relpath(path: str, workdir: str | Path) -> str:
    """``path`` as a clean relative POSIX path inside ``workdir`` (symlinks resolved), else
    ``ValueError``."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("a file path is required")
    raw = path.strip()
    if os.path.isabs(raw) or raw.startswith("~"):
        raise ValueError(f"give the path relative to the working folder: {path!r}")
    clean = os.path.normpath(raw)
    if clean in (".", "") or clean == ".." or clean.startswith(".." + os.sep):
        raise ValueError(f"the path leaves the working folder: {path!r}")
    root = Path(workdir).resolve()
    try:
        (root / clean).resolve().relative_to(root)
    except ValueError:
        raise ValueError(f"the path leaves the working folder: {path!r}") from None
    return Path(clean).as_posix()


def config_value(scope: Scope | None, key: str, fallback: Any) -> Any:
    """A setting from the scope's config with the type of ``fallback``; ``fallback`` when unset
    or of another type."""
    if scope is None or scope.config is None:
        return fallback
    value = scope.config.get(key)
    if value is None:
        return fallback
    if isinstance(fallback, bool):
        return value if isinstance(value, bool) else fallback
    if isinstance(fallback, int | float):
        if isinstance(value, bool) or not isinstance(value, int | float):
            return fallback
        return type(fallback)(value)
    return value


# -- the store -------------------------------------------------------------------------------------


class ShadowStore:
    """The checkpoint store of one profile (see the module notes)."""

    def __init__(
        self,
        base: Path,
        *,
        env: Mapping[str, str] | None = None,
        clock: Clock | None = None,
        git: str | None = None,
        timeout: float = GIT_TIMEOUT,
        max_file_size_mb: float = 10,
        max_files: int = MAX_FILES,
        too_broad: Sequence[str | Path] = (),
    ) -> None:
        self._base = Path(base)
        self._env = {k: v for k, v in (env or {}).items() if not k.startswith("GIT_")}
        self.clock: Clock = clock if clock is not None else SystemClock()
        self._git = git if git is not None else which("git", path=self._env.get("PATH"))
        self.timeout = float(timeout)
        self.max_file_size_mb = float(max_file_size_mb)
        self.max_files = int(max_files)
        self._too_broad = {str(Path(p).expanduser().resolve()) for p in too_broad if str(p)}

    @classmethod
    def for_scope(cls, scope: Scope) -> ShadowStore:
        env = child_env(scope, variant="minimal")
        home = env.get("HOME")
        return cls(
            scope.home / "checkpoints",
            env=env,
            clock=scope.clock,
            max_file_size_mb=config_value(scope, "checkpoints.max_file_size_mb", 10.0),
            too_broad=[home] if home else [],
        )

    # -- places ------------------------------------------------------------------------------------

    @property
    def base(self) -> Path:
        return self._base

    @property
    def store_dir(self) -> Path:
        return self._base / "store"

    def ref(self, key: str) -> str:
        return f"{REF_PREFIX}/{key}"

    def index_path(self, key: str) -> Path:
        return self.store_dir / "indexes" / key

    def meta_path(self, key: str) -> Path:
        return self.store_dir / "projects" / f"{key}.json"

    def ledger_path(self, key: str) -> Path:
        return self.store_dir / "ledgers" / f"{key}.jsonl"

    def exists(self) -> bool:
        return (self.store_dir / "HEAD").is_file()

    def lock(self) -> FileLock:
        """The one lock for snapshots, restores, prunes and clear (outside ``base`` so clear
        cannot delete it from under a holder)."""
        self._base.parent.mkdir(parents=True, exist_ok=True)
        return FileLock(self._base.parent / f".{self._base.name}.lock")

    def now(self) -> float:
        return self.clock.now().timestamp()

    # -- git ---------------------------------------------------------------------------------------

    def git_env(
        self, *, workdir: str | Path | None = None, index: Path | None = None
    ) -> dict[str, str]:
        env = dict(self._env)
        stamp = f"@{int(self.now())} +0000"
        env.update(
            {
                "GIT_DIR": str(self.store_dir),
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_AUTHOR_NAME": "Indus Agent Checkpoint",
                "GIT_AUTHOR_EMAIL": "checkpoints@indus.invalid",
                "GIT_COMMITTER_NAME": "Indus Agent Checkpoint",
                "GIT_COMMITTER_EMAIL": "checkpoints@indus.invalid",
                "GIT_AUTHOR_DATE": stamp,
                "GIT_COMMITTER_DATE": stamp,
            }
        )
        if workdir is not None:
            env["GIT_WORK_TREE"] = str(workdir)
        if index is not None:
            env["GIT_INDEX_FILE"] = str(index)
        return env

    def git(
        self,
        args: Sequence[str],
        *,
        workdir: str | Path | None = None,
        index: Path | None = None,
        input: bytes | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> GitResult:
        """Run ``git ARGS`` against the store; raises ``GitError`` only when git cannot run or
        times out (a non-zero exit is in the result)."""
        if not self._git:
            raise GitError("git is not installed (or not on PATH)")
        full_env = self.git_env(workdir=workdir, index=index)
        if env:
            full_env.update(env)
        cwd = str(workdir) if workdir is not None else str(self.store_dir)
        limit = timeout if timeout is not None else self.timeout
        try:
            done = subprocess.run(
                [self._git, *args],
                cwd=cwd,
                env=full_env,
                input=input,
                stdin=None if input is not None else subprocess.DEVNULL,
                capture_output=True,
                timeout=limit,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise GitError(f"git {args[0]} did not finish within {limit:g}s") from None
        except OSError as exc:
            raise GitError(f"git {args[0]} could not start: {exc.strerror or exc}") from None
        err = done.stderr.decode("utf-8", "replace").strip()
        if done.returncode != 0:
            log.debug("git %s exited %d: %s", " ".join(args[:3]), done.returncode, err[:500])
        return GitResult(done.returncode, done.stdout, err)

    def must(self, args: Sequence[str], **kwargs: Any) -> GitResult:
        """``git`` that raises ``GitError`` on a non-zero exit."""
        result = self.git(args, **kwargs)
        if not result.ok:
            raise GitError(f"git {args[0]} failed: {result.err or f'exit {result.code}'}")
        return result

    def ensure(self) -> None:
        """Create the store on first use (bare repository, settings, excludes, folders)."""
        if self.exists():
            return
        self._base.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            self._base.chmod(0o700)
        if not self._git:
            raise GitError("git is not installed (or not on PATH)")
        env = self.git_env()
        env.pop("GIT_DIR", None)
        try:
            done = subprocess.run(
                [self._git, "init", "--bare", "--quiet", str(self.store_dir)],
                env=env,
                cwd=str(self._base),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise GitError(f"the checkpoint store could not be created: {exc}") from None
        if done.returncode != 0:
            raise GitError(
                "the checkpoint store could not be created: "
                + done.stderr.decode("utf-8", "replace").strip()
            )
        for name, value in (
            ("gc.auto", "0"),
            ("core.logAllRefUpdates", "false"),
            ("commit.gpgSign", "false"),
            ("core.autocrlf", "false"),
            ("advice.addEmbeddedRepo", "false"),
        ):
            self.must(["config", name, value])
        for sub in ("indexes", "projects", "ledgers", "info", "tmp"):
            (self.store_dir / sub).mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.store_dir / "info" / "exclude", "\n".join(DEFAULT_EXCLUDES) + "\n")

    @contextlib.contextmanager
    def temp_index(self) -> Iterator[Path]:
        """A throwaway index path (not created) removed afterwards."""
        folder = self.store_dir / "tmp"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"index-{uuid.uuid4().hex}"
        try:
            yield path
        finally:
            for leftover in (path, path.with_name(path.name + ".lock")):
                with contextlib.suppress(OSError):
                    leftover.unlink()

    # -- projects ----------------------------------------------------------------------------------

    def project_keys(self) -> list[str]:
        folder = self.store_dir / "projects"
        if not folder.is_dir():
            return []
        return sorted(p.stem for p in folder.glob("*.json"))

    def read_meta(self, key: str) -> dict[str, Any] | None:
        try:
            data = json.loads(self.meta_path(key).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None  # pyright: ignore[reportUnknownVariableType]

    def touch(self, key: str, workdir: str) -> None:
        now = self.now()
        meta = self.read_meta(key) or {}
        created = meta.get("created_at")
        record = {
            "workdir": workdir,
            "created_at": created if isinstance(created, int | float) else now,
            "last_touch": now,
        }
        atomic_write_json(self.meta_path(key), record)

    def tip(self, key: str) -> str | None:
        result = self.git(["rev-parse", "--verify", "--quiet", self.ref(key) + "^{commit}"])
        return (result.text or None) if result.ok else None

    def commits(self, key: str) -> list[str]:
        """The project's checkpoints, oldest first."""
        if self.tip(key) is None:
            return []
        return self.must(["rev-list", "--reverse", self.ref(key)]).text.split()

    def _ledger_add(self, key: str, entry: Mapping[str, Any]) -> None:
        path = self.ledger_path(key)
        try:
            lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        except OSError:
            lines = []
        lines.append(json.dumps(dict(entry), ensure_ascii=False, sort_keys=True))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, "\n".join(lines[-LEDGER_MAX:]) + "\n")
        except OSError as exc:
            log.debug("checkpoint ledger %s not written: %s", path, exc)

    def ledger(self, key: str) -> list[dict[str, Any]]:
        try:
            lines = self.ledger_path(key).read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out: list[dict[str, Any]] = []
        for line in lines:
            with contextlib.suppress(ValueError):
                item = json.loads(line)
                if isinstance(item, dict):
                    out.append(item)  # pyright: ignore[reportUnknownArgumentType]
        return out

    # -- staging -----------------------------------------------------------------------------------

    def _count_files(self, root: Path) -> int:
        """Files below ``root`` (never-captured folders skipped); stops just over the limit."""
        count = 0
        for _dirpath, dirnames, filenames in os.walk(root, onerror=lambda _exc: None):
            dirnames[:] = [d for d in dirnames if d not in _UNCOUNTED_DIRS]
            count += len(filenames)
            if count > self.max_files:
                break
        return count

    def oversize(self, path: Path) -> bool:
        cap = self.max_file_size_mb * MB
        if cap <= 0:
            return False
        try:
            return path.lstat().st_size > cap
        except OSError:
            return False

    def stage(self, workdir: Path, index: Path) -> list[str]:
        """Stage ``workdir`` into ``index`` (``add -A`` over whatever the index holds), then drop
        files over the size cap; returns the dropped paths."""
        self.must(["add", "-A"], workdir=workdir, index=index, timeout=self.timeout * 2)
        staged = self.must(["ls-files", "-z", "--cached"], workdir=workdir, index=index).fields()
        dropped = [rel for rel in staged if self.oversize(workdir / rel)]
        if dropped:
            payload = b"".join(os.fsencode(rel) + b"\0" for rel in dropped)
            self.must(
                ["update-index", "--force-remove", "-z", "--stdin"],
                workdir=workdir,
                index=index,
                input=payload,
            )
        return dropped

    def tree_entries(self, treeish: str) -> list[tuple[str, str]]:
        """``(mode, path)`` of every entry of ``treeish`` (recursive)."""
        out: list[tuple[str, str]] = []
        for record in self.must(["ls-tree", "-r", "-z", treeish]).fields():
            meta, _, path = record.partition("\t")
            if path:
                out.append((meta.split(" ", 1)[0], path))
        return out

    # -- snapshot ----------------------------------------------------------------------------------

    def snapshot(
        self,
        workdir: str | Path,
        reason: str,
        *,
        session_id: str | None = None,
        turn_id: str | None = None,
        keep: int | None = None,
    ) -> Snapshot | Skipped:
        """Capture ``workdir`` (see the module notes). Never raises for git trouble."""
        folder = Path(workdir).expanduser()
        with contextlib.suppress(OSError):
            folder = folder.resolve()
        if not folder.is_dir():
            return Skipped("missing", f"{folder} is not a folder")
        if folder == Path(folder.anchor) or str(folder) in self._too_broad:
            return Skipped("too_broad", f"{folder} is too broad to checkpoint")
        if self._count_files(folder) > self.max_files:
            return Skipped("too_many_files", f"{folder} has more than {self.max_files:,} files")
        try:
            with self.lock():
                return self._snapshot(folder, reason, session_id, turn_id, keep)
        except GitError as exc:
            return Skipped("error", str(exc))
        except OSError as exc:
            return Skipped("error", f"checkpoint store: {exc.strerror or exc}")

    def _snapshot(
        self,
        folder: Path,
        reason: str,
        session_id: str | None,
        turn_id: str | None,
        keep: int | None,
    ) -> Snapshot | Skipped:
        self.ensure()
        key = project_key(folder)
        index = self.index_path(key)
        index.parent.mkdir(parents=True, exist_ok=True)
        parent = self.tip(key)
        if parent is not None:
            self.must(["read-tree", parent], workdir=folder, index=index)
        else:
            with contextlib.suppress(OSError):
                index.unlink()
        self.stage(folder, index)
        if parent is not None:
            same = self.git(
                ["diff-index", "--cached", "--quiet", parent], workdir=folder, index=index
            )
            if same.ok:
                self.touch(key, str(folder))
                return Skipped("unchanged", "nothing changed since the last checkpoint")
            if same.code != 1:
                raise GitError(f"git diff-index failed: {same.err}")
        elif not self.must(["ls-files", "-z"], workdir=folder, index=index).fields():
            return Skipped("empty", "there are no files to capture")
        tree = self.must(["write-tree"], workdir=folder, index=index).text
        nested = tuple(path for mode, path in self.tree_entries(tree) if mode == "160000")
        if parent is not None:
            changed = self.must(["diff-tree", "-r", "-z", "--name-only", parent, tree]).fields()
        else:
            changed = [path for _mode, path in self.tree_entries(tree)]
        subject = " ".join(line.strip() for line in reason.splitlines() if line.strip())
        subject = subject or "checkpoint"
        if nested:
            shown = ", ".join(nested[:3]) + (f" (+{len(nested) - 3})" if len(nested) > 3 else "")
            subject += f" [nested repositories not captured: {shown}]"
        trailers = [
            f"{name}: {value}"
            for name, value in ((_TRAILER_SESSION, session_id), (_TRAILER_TURN, turn_id))
            if value
        ]
        message = subject + ("\n\n" + "\n".join(trailers) if trailers else "") + "\n"
        args = ["commit-tree", tree, "--no-gpg-sign", "-F", "-"]
        if parent is not None:
            args[2:2] = ["-p", parent]
        commit = self.must(args, input=message.encode("utf-8", "surrogateescape")).text
        update = ["update-ref", self.ref(key), commit] + ([parent] if parent else [""])
        self.must(update)
        self.touch(key, str(folder))
        self._ledger_add(
            key,
            {
                "commit": commit,
                "at": self.now(),
                "reason": subject,
                "session": session_id,
                "turn": turn_id,
                "files": len(changed),
            },
        )
        if keep is not None and keep > 0:
            from indus.backends.checkpoints import prune

            try:
                prune.trim_history(self, key, keep)
            except GitError as exc:
                log.warning("checkpoint history of %s was not trimmed: %s", folder, exc)
            commit = self.tip(key) or commit
        return Snapshot(commit, key, str(folder), len(changed), nested)

    # -- CheckpointStore protocol ------------------------------------------------------------------

    def project_size(self, key: str, others: Sequence[str]) -> int:
        """Bytes only this project's history holds (what deleting it frees)."""
        args = ["rev-list", "--objects", "--disk-usage", self.ref(key)]
        if others:
            args += ["--not", *(self.ref(k) for k in others)]
        result = self.git(args)
        try:
            return int(result.text) if result.ok else 0
        except ValueError:
            return 0

    def list_projects(self) -> list[CheckpointProject]:
        if not self.exists():
            return []
        keys = self.project_keys()
        live = [k for k in keys if self.tip(k) is not None]
        out: list[CheckpointProject] = []
        for key in keys:
            meta = self.read_meta(key) or {}
            workdir = meta.get("workdir")
            workdir = workdir if isinstance(workdir, str) and workdir else None
            touch = meta.get("last_touch")
            commits = 0
            size = 0
            if key in live:
                counted = self.git(["rev-list", "--count", self.ref(key)])
                with contextlib.suppress(ValueError):
                    commits = int(counted.text) if counted.ok else 0
                size = self.project_size(key, [k for k in live if k != key])
            out.append(
                CheckpointProject(
                    key=key,
                    workdir=workdir,
                    commits=commits,
                    size_bytes=size,
                    last_touch=float(touch) if isinstance(touch, int | float) else None,
                    exists=workdir is not None and Path(workdir).is_dir(),
                )
            )
        return out

    def size(self) -> int:
        total = 0
        for dirpath, _dirs, files in os.walk(self._base, onerror=lambda _exc: None):
            for name in files:
                with contextlib.suppress(OSError):
                    total += os.lstat(os.path.join(dirpath, name)).st_size
        return total

    def prune(self, plan: PrunePlan) -> PruneResult:
        from indus.backends.checkpoints import prune

        return prune.apply_plan(self, plan)

    def clear(self) -> ClearResult:
        if not self._base.exists():
            return ClearResult(True, 0)
        try:
            with self.lock():
                freed = self.size()
                shutil.rmtree(self._base)
        except OSError as exc:
            return ClearResult(False, 0, [f"{exc.filename or self._base}: {exc.strerror or exc}"])
        return ClearResult(True, freed)


def parse_trailers(body: str) -> dict[str, str]:
    """``Name: value`` lines of a checkpoint message body."""
    found: dict[str, str] = {}
    for line in body.splitlines():
        name, sep, value = line.partition(":")
        if sep and name in (_TRAILER_SESSION, _TRAILER_TURN) and value.strip():
            found[name] = value.strip()
    return found


def store_factory(scope: Scope) -> ShadowStore | None:
    """The profile's store for ``indus checkpoints``; None until a first checkpoint created it."""
    store = ShadowStore.for_scope(scope)
    return store if store.exists() else None
