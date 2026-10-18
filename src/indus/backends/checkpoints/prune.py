"""Keeping the checkpoint store bounded (P11-18).

- ``apply_plan(store, plan)``: the ``CheckpointStore.prune`` the ``indus checkpoints prune``
  command calls — delete the planned projects in order (orphans, stale, oversize), then reclaim
  space with ``git gc --prune=now``. Failures are counted, never raised.
- ``trim_history(store, key, keep)``: the snapshot path's per-project cap (``max_snapshots``):
  rebuild the newest ``keep`` checkpoints as a fresh chain (same trees, messages and dates) and
  leave the gc to the next prune (marked by ``.gc-pending``).
- ``fit_size(store, cap)``: gc, then drop the globally oldest checkpoint (never a project's
  newest) and gc again until the store fits ``cap``; bounded number of drops.
- ``prune_store(...)``: the full order — orphans (unless kept), stale projects, gc, size pass.
- ``auto_prune(...)``: ``prune_store`` at most once per ``min_interval_hours``, throttled by
  ``<base>/.last_prune``; the marker is written before the work starts so a prune that dies costs
  one interval, not a gc on every start. The automatic prune never deletes orphans: a missing
  folder may be a drive that is not mounted right now.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, replace

from indus.backends.checkpoint_api import PrunePlan, PruneResult, plan_prune
from indus.backends.checkpoints.store import GitError, ShadowStore

__all__ = [
    "GC_PENDING",
    "MAX_SIZE_DROPS",
    "PRUNE_MARKER",
    "AutoPrune",
    "StorePrune",
    "apply_plan",
    "auto_prune",
    "delete_project",
    "fit_size",
    "gc",
    "prune_store",
    "rewrite_history",
    "trim_history",
]

log = logging.getLogger(__name__)

PRUNE_MARKER = ".last_prune"
GC_PENDING = ".gc-pending"
MAX_SIZE_DROPS = 200
HOUR = 3600.0
MB = 1024 * 1024


@dataclass(frozen=True)
class StorePrune:
    """What a full prune did: the project deletions, checkpoints dropped for size, and whether
    the store fits its cap now."""

    result: PruneResult
    dropped_checkpoints: int = 0
    fits: bool = True


@dataclass(frozen=True)
class AutoPrune:
    ran: bool
    outcome: StorePrune | None = None
    reason: str = ""


def gc(store: ShadowStore) -> None:
    """Reclaim unreachable objects now. Raises ``GitError``."""
    store.must(["reflog", "expire", "--expire=now", "--all"])
    store.must(["gc", "--prune=now", "--quiet"], timeout=store.timeout * 4)
    with contextlib.suppress(OSError):
        (store.store_dir / GC_PENDING).unlink()


def _mark_gc_pending(store: ShadowStore) -> None:
    with contextlib.suppress(OSError):
        (store.store_dir / GC_PENDING).touch()


def delete_project(store: ShadowStore, key: str) -> bool:
    """Drop one project's ref, index, metadata and ledger; False when the ref could not go."""
    if store.tip(key) is not None and not store.git(["update-ref", "-d", store.ref(key)]).ok:
        return False
    for path in (store.index_path(key), store.meta_path(key), store.ledger_path(key)):
        with contextlib.suppress(OSError):
            path.unlink()
    return True


def apply_plan(store: ShadowStore, plan: PrunePlan) -> PruneResult:
    """Delete the planned projects in order, then gc (see the module notes)."""
    if not store.exists():
        return PruneResult()
    try:
        with store.lock():
            before = store.size()
            scanned = len(store.project_keys())
            counts = {"orphan": 0, "stale": 0, "oversize": 0}
            errors = 0
            for group, keys in (
                ("orphan", plan.orphans),
                ("stale", plan.stale),
                ("oversize", plan.oversize),
            ):
                for key in keys:
                    if delete_project(store, key):
                        counts[group] += 1
                    else:
                        errors += 1
            if sum(counts.values()) or (store.store_dir / GC_PENDING).exists():
                try:
                    gc(store)
                except GitError as exc:
                    log.warning("checkpoint gc failed: %s", exc)
                    errors += 1
            freed = max(0, before - store.size())
    except (GitError, OSError) as exc:
        log.warning("checkpoint prune stopped: %s", exc)
        return PruneResult(errors=1)
    return PruneResult(
        scanned=scanned,
        deleted_orphan=counts["orphan"],
        deleted_stale=counts["stale"],
        deleted_oversize=counts["oversize"],
        errors=errors,
        bytes_freed=freed,
    )


def rewrite_history(store: ShadowStore, key: str, keep: list[str]) -> str:
    """Make ``keep`` (oldest first, a tail of the project's history) the whole history: each
    commit is recreated with its tree, message and dates on top of the previous one. The ref
    moves only if nobody moved it meanwhile. Returns the new tip; raises ``GitError``."""
    if not keep:
        raise GitError("a project keeps at least one checkpoint")
    old_tip = store.tip(key)
    if old_tip is None:
        raise GitError(f"checkpoint project {key} has no history")
    parent: str | None = None
    for sha in keep:
        shown = store.must(["log", "-1", "--format=%T%x00%at%x00%ct%x00%B", sha])
        fields = shown.out.decode("utf-8", "surrogateescape").split("\0", 3)
        if len(fields) != 4:
            raise GitError(f"checkpoint {sha[:8]} could not be read")
        tree, authored, committed, message = fields
        args = ["commit-tree", tree, "--no-gpg-sign", "-F", "-"]
        if parent is not None:
            args += ["-p", parent]
        dates = {
            "GIT_AUTHOR_DATE": f"@{authored.strip()} +0000",
            "GIT_COMMITTER_DATE": f"@{committed.strip()} +0000",
        }
        parent = store.must(
            args, input=message.rstrip("\n").encode("utf-8", "surrogateescape") + b"\n", env=dates
        ).text
    assert parent is not None
    store.must(["update-ref", store.ref(key), parent, old_tip])
    return parent


def trim_history(store: ShadowStore, key: str, keep: int) -> int:
    """Keep the newest ``keep`` (at least one) checkpoints of a project; returns how many were
    dropped. The objects are reclaimed by the next prune."""
    commits = store.commits(key)
    keep = max(1, keep)
    if len(commits) <= keep:
        return 0
    rewrite_history(store, key, commits[-keep:])
    _mark_gc_pending(store)
    return len(commits) - keep


def fit_size(
    store: ShadowStore, cap_bytes: int, *, max_drops: int = MAX_SIZE_DROPS
) -> tuple[int, bool]:
    """gc, then drop the oldest checkpoint of the whole store (a project's newest is never
    dropped) and gc again until the store fits; returns ``(dropped, fits)``."""
    if cap_bytes <= 0:
        return 0, True
    gc(store)
    dropped = 0
    while store.size() > cap_bytes:
        if dropped >= max_drops:
            return dropped, False
        oldest: tuple[float, str, list[str]] | None = None
        for key in store.project_keys():
            commits = store.commits(key)
            if len(commits) < 2:
                continue
            when = store.must(["log", "-1", "--format=%ct", commits[0]]).text
            try:
                stamp = float(when)
            except ValueError:
                stamp = 0.0
            if oldest is None or (stamp, key) < (oldest[0], oldest[1]):
                oldest = (stamp, key, commits)
        if oldest is None:
            return dropped, False
        rewrite_history(store, oldest[1], oldest[2][1:])
        dropped += 1
        gc(store)
    return dropped, True


def prune_store(
    store: ShadowStore,
    *,
    now: float,
    retention_days: float = 7,
    max_total_size_mb: int = 500,
    delete_orphans: bool = True,
) -> StorePrune:
    """The full prune order (see the module notes). Never raises."""
    if not store.exists():
        return StorePrune(PruneResult())
    projects = store.list_projects()
    plan = plan_prune(
        projects,
        now=now,
        retention_days=retention_days,
        max_size_bytes=2**62,  # sizes are trimmed per checkpoint below, not per project
        delete_orphans=delete_orphans,
    )
    result = apply_plan(store, plan)
    try:
        with store.lock():
            before = store.size()
            dropped, fits = fit_size(store, max(0, max_total_size_mb) * MB)
            freed = max(0, before - store.size())
    except (GitError, OSError) as exc:
        log.warning("checkpoint size pass stopped: %s", exc)
        return StorePrune(replace(result, errors=result.errors + 1), 0, False)
    if not fits:
        log.warning(
            "the checkpoint store is still over %d MB; every project keeps its newest checkpoint",
            max_total_size_mb,
        )
    result = replace(result, bytes_freed=result.bytes_freed + freed)
    return StorePrune(result, dropped, fits)


def _last_prune(store: ShadowStore) -> float | None:
    try:
        return float((store.base / PRUNE_MARKER).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def auto_prune(
    store: ShadowStore,
    *,
    now: float,
    retention_days: float = 7,
    max_total_size_mb: int = 500,
    min_interval_hours: float = 24,
) -> AutoPrune:
    """``prune_store`` without orphan deletion, at most once per ``min_interval_hours``."""
    if not store.exists():
        return AutoPrune(False, reason="no checkpoint store yet")
    last = _last_prune(store)
    if last is not None and 0 <= now - last < max(min_interval_hours, 0) * HOUR:
        return AutoPrune(False, reason="pruned recently")
    try:
        (store.base / PRUNE_MARKER).write_text(repr(now), encoding="utf-8")
    except OSError as exc:
        log.debug("checkpoint prune marker not written: %s", exc)
    outcome = prune_store(
        store,
        now=now,
        retention_days=retention_days,
        max_total_size_mb=max_total_size_mb,
        delete_orphans=False,
    )
    removed = outcome.result.deleted_stale + outcome.result.deleted_orphan
    if removed or outcome.dropped_checkpoints:
        log.info(
            "checkpoint prune: %d project(s) and %d checkpoint(s) removed, %d bytes freed",
            removed,
            outcome.dropped_checkpoints,
            outcome.result.bytes_freed,
        )
    return AutoPrune(True, outcome)
