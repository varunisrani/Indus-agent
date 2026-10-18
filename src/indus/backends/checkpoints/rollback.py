"""Listing, diffing and restoring checkpoints (P11-17).

Checkpoints of a folder are numbered newest first from 1. Every restore first snapshots the files
as they are now ("before rollback to <id>"), so a rollback can itself be rolled back; the restore
is refused when that safety snapshot fails (an unchanged tree is fine: it is already captured).

- ``list_checkpoints(store, folder)``: ``Checkpoint`` rows (time, reason, session/turn, files
  changed against the checkpoint before).
- ``diff(store, folder, n)``: the unified diff from checkpoint ``n`` to the files on disk now (only
  what the store would capture: excluded and oversize files are left out).
- ``restore(store, folder, n)``: write every captured file of checkpoint ``n`` back, and delete
  files that exist now, would be captured, and were not in checkpoint ``n``. Excluded paths
  (``node_modules``, ``.env``, oversize files, nested repositories) are never touched.
- ``restore_file(store, folder, n, path)``: the same for one relative path held by the
  checkpoint.

All work runs on throwaway index files, so a project's own staging index is never disturbed.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path

from indus.backends.checkpoints.store import (
    GitError,
    ShadowStore,
    Skipped,
    check_commit,
    check_relpath,
    parse_trailers,
    project_key,
)

__all__ = [
    "Checkpoint",
    "RestoreResult",
    "RollbackError",
    "diff",
    "list_all",
    "list_checkpoints",
    "resolve",
    "restore",
    "restore_file",
]

_LIST_LIMIT = 200


class RollbackError(ValueError):
    """A rollback request that cannot be carried out; the message is for the person."""


@dataclass(frozen=True)
class Checkpoint:
    number: int
    commit: str
    at: float
    reason: str
    session_id: str | None
    turn_id: str | None
    files_changed: int

    @property
    def short(self) -> str:
        return self.commit[:8]


@dataclass(frozen=True)
class RestoreResult:
    restored_to: str
    safety: str | None
    written: int
    removed: tuple[str, ...] = ()
    not_captured: tuple[str, ...] = ()
    file: str | None = None


def _folder(workdir: str | Path) -> Path:
    folder = Path(workdir).expanduser()
    with contextlib.suppress(OSError):
        folder = folder.resolve()
    return folder


def list_checkpoints(
    store: ShadowStore, workdir: str | Path, *, limit: int = _LIST_LIMIT
) -> list[Checkpoint]:
    """The folder's checkpoints, newest first (see the module notes)."""
    if not store.exists():
        return []
    key = project_key(_folder(workdir))
    if store.tip(key) is None:
        return []
    shown = store.must(
        ["log", f"--max-count={max(limit, 1)}", "--format=%H%x1f%ct%x1f%B%x1e", store.ref(key)]
    )
    rows: list[Checkpoint] = []
    for record in shown.out.decode("utf-8", "surrogateescape").split("\x1e"):
        parts = record.strip("\n").split("\x1f", 2)
        if len(parts) != 3:
            continue
        commit, stamp, message = parts
        subject, _, body = message.partition("\n")
        trailers = parse_trailers(body)
        changed = store.must(
            ["diff-tree", "--root", "-r", "-z", "--name-only", "--no-commit-id", commit]
        ).fields()
        try:
            at = float(stamp)
        except ValueError:
            at = 0.0
        rows.append(
            Checkpoint(
                number=len(rows) + 1,
                commit=commit,
                at=at,
                reason=subject.strip(),
                session_id=trailers.get("Indus-Session"),
                turn_id=trailers.get("Indus-Turn"),
                files_changed=len(changed),
            )
        )
    return rows


def list_all(store: ShadowStore) -> list[tuple[str, Checkpoint]]:
    """Every project's checkpoints as ``(folder, checkpoint)``, newest first."""
    out: list[tuple[str, Checkpoint]] = []
    for key in store.project_keys():
        meta = store.read_meta(key) or {}
        folder = meta.get("workdir")
        if isinstance(folder, str) and folder:
            out.extend((folder, cp) for cp in list_checkpoints(store, folder))
    out.sort(key=lambda item: item[1].at, reverse=True)
    return out


def resolve(store: ShadowStore, workdir: str | Path, number: int) -> Checkpoint:
    """Checkpoint ``number`` (1 = newest) of the folder, or ``RollbackError``."""
    rows = list_checkpoints(store, workdir)
    if not rows:
        raise RollbackError(f"there are no checkpoints for {_folder(workdir)}")
    if not 1 <= number <= len(rows):
        raise RollbackError(f"choose a checkpoint from 1 to {len(rows)} (got {number})")
    check_commit(rows[number - 1].commit)
    return rows[number - 1]


def diff(store: ShadowStore, workdir: str | Path, number: int) -> str:
    """Unified diff from checkpoint ``number`` to the folder now ("" when they match)."""
    folder = _folder(workdir)
    checkpoint = resolve(store, folder, number)
    with store.lock(), store.temp_index() as index:
        store.stage(folder, index)
        shown = store.must(
            ["diff", "--cached", "--no-color", "--no-ext-diff", "--no-renames", checkpoint.commit],
            workdir=folder,
            index=index,
        )
    return shown.out.decode("utf-8", "surrogateescape")


def _safety(
    store: ShadowStore, folder: Path, checkpoint: Checkpoint, session_id: str | None
) -> str | None:
    taken = store.snapshot(folder, f"before rollback to {checkpoint.short}", session_id=session_id)
    if isinstance(taken, Skipped):
        if taken.kind in ("unchanged", "empty"):
            return None
        raise RollbackError(f"nothing was restored: the safety checkpoint failed ({taken.reason})")
    return taken.commit


def _remove(folder: Path, rel: str) -> bool:
    target = folder / rel
    try:
        target.parent.resolve().relative_to(folder)
    except (OSError, ValueError):
        return False
    try:
        target.unlink()
    except OSError:
        return False
    parent = target.parent
    while parent != folder:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent
    return True


def restore(
    store: ShadowStore, workdir: str | Path, number: int, *, session_id: str | None = None
) -> RestoreResult:
    """Put the folder back to checkpoint ``number`` (see the module notes)."""
    folder = _folder(workdir)
    checkpoint = resolve(store, folder, number)
    try:
        with store.lock():
            safety = _safety(store, folder, checkpoint, session_id)
            entries = store.tree_entries(checkpoint.commit)
            nested = tuple(path for mode, path in entries if mode == "160000")
            captured = {path for mode, path in entries if mode != "160000"}
            with store.temp_index() as now_index:
                store.stage(folder, now_index)
                present = store.must(
                    ["ls-files", "-z", "--cached"], workdir=folder, index=now_index
                ).fields()
            removed: list[str] = []
            for rel in sorted(set(present) - captured):
                if any(rel == repo or rel.startswith(repo + "/") for repo in nested):
                    continue
                if store.oversize(folder / rel):
                    continue
                if _remove(folder, rel):
                    removed.append(rel)
            with store.temp_index() as target:
                store.must(["read-tree", checkpoint.commit], workdir=folder, index=target)
                store.must(["checkout-index", "-a", "-f"], workdir=folder, index=target)
    except GitError as exc:
        raise RollbackError(f"the restore failed: {exc}") from None
    return RestoreResult(
        restored_to=checkpoint.short,
        safety=safety,
        written=len(captured),
        removed=tuple(removed),
        not_captured=nested,
    )


def restore_file(
    store: ShadowStore,
    workdir: str | Path,
    number: int,
    path: str,
    *,
    session_id: str | None = None,
) -> RestoreResult:
    """Put one file back as checkpoint ``number`` holds it; other files are not touched."""
    folder = _folder(workdir)
    try:
        rel = check_relpath(path, folder)
    except ValueError as exc:
        raise RollbackError(str(exc)) from None
    checkpoint = resolve(store, folder, number)
    held = store.git(["cat-file", "-e", f"{checkpoint.commit}:{rel}"])
    if not held.ok:
        raise RollbackError(f"checkpoint {number} ({checkpoint.short}) does not hold {rel}")
    try:
        with store.lock():
            safety = _safety(store, folder, checkpoint, session_id)
            with store.temp_index() as target:
                store.must(["read-tree", checkpoint.commit], workdir=folder, index=target)
                parent = (folder / rel).parent
                if parent.exists() and not parent.is_dir():
                    raise RollbackError(f"{os.path.dirname(rel)} is not a folder now")
                store.must(["checkout-index", "-f", "--", rel], workdir=folder, index=target)
    except GitError as exc:
        raise RollbackError(f"the restore failed: {exc}") from None
    return RestoreResult(restored_to=checkpoint.short, safety=safety, written=1, file=rel)
