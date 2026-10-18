"""The checkpoint store as the CLI sees it: a protocol, its records, and the prune order (P07-24).

The real store (a shared shadow repository under ``<home>/checkpoints/``) arrives in P11; it
implements ``CheckpointStore`` and installs itself with ``set_store_factory``. Until then
``store_for`` returns None and the commands report that no checkpoints exist.

``plan_prune`` is the one prune order, shared by the store and its fakes: projects whose working
folder is gone (unless orphans are kept, and only those the person was shown), then projects not
touched within the retention window, then the oldest remaining projects until the total fits the
size cap. A manual prune runs now; it does not wait for the automatic prune's once-a-day marker.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from indus.foundation.scope import Scope

__all__ = [
    "CheckpointProject",
    "CheckpointStore",
    "ClearResult",
    "PrunePlan",
    "PruneResult",
    "plan_prune",
    "set_store_factory",
    "store_for",
]

DAY = 86_400.0


@dataclass(frozen=True)
class CheckpointProject:
    """One working folder's checkpoint history."""

    key: str
    workdir: str | None
    commits: int
    size_bytes: int
    last_touch: float | None
    exists: bool


@dataclass(frozen=True)
class PrunePlan:
    """Project keys to delete, in order, and why."""

    orphans: tuple[str, ...] = ()
    stale: tuple[str, ...] = ()
    oversize: tuple[str, ...] = ()

    @property
    def keys(self) -> tuple[str, ...]:
        return (*self.orphans, *self.stale, *self.oversize)


@dataclass(frozen=True)
class PruneResult:
    scanned: int = 0
    deleted_orphan: int = 0
    deleted_stale: int = 0
    deleted_oversize: int = 0
    errors: int = 0
    bytes_freed: int = 0


@dataclass(frozen=True)
class ClearResult:
    deleted: bool
    bytes_freed: int = 0
    errors: list[str] = field(default_factory=lambda: [])


class CheckpointStore(Protocol):
    @property
    def base(self) -> Path: ...

    def list_projects(self) -> list[CheckpointProject]: ...

    def size(self) -> int:
        """Bytes the whole store takes on disk."""
        ...

    def prune(self, plan: PrunePlan) -> PruneResult:
        """Delete the planned projects in order; failures are counted, never raised."""
        ...

    def clear(self) -> ClearResult:
        """Remove every checkpoint (irreversible)."""
        ...


def plan_prune(
    projects: Sequence[CheckpointProject],
    *,
    now: float,
    retention_days: float,
    max_size_bytes: int,
    delete_orphans: bool = True,
    orphan_allowlist: Collection[str] | None = None,
) -> PrunePlan:
    """The prune order (see the module notes). ``orphan_allowlist`` limits orphan deletion to the
    projects the person was shown; None means no limit."""
    remaining = list(projects)
    orphans: list[str] = []
    if delete_orphans:
        for project in projects:
            if project.exists:
                continue
            if orphan_allowlist is not None and project.key not in orphan_allowlist:
                continue
            orphans.append(project.key)
    remaining = [p for p in remaining if p.key not in set(orphans)]
    cutoff = now - max(retention_days, 0.0) * DAY
    stale = [
        p.key for p in remaining if p.exists and p.last_touch is not None and p.last_touch < cutoff
    ]
    remaining = [p for p in remaining if p.key not in set(stale)]
    total = sum(p.size_bytes for p in remaining)
    oversize: list[str] = []
    # Orphans that are kept (``--keep-orphans`` or not shown) count towards the size but are not
    # deleted to reach the cap.
    trimmable = [p for p in remaining if p.exists]
    for project in sorted(trimmable, key=lambda p: (p.last_touch or 0.0, p.key)):
        if total <= max_size_bytes:
            break
        oversize.append(project.key)
        total -= project.size_bytes
    return PrunePlan(tuple(orphans), tuple(stale), tuple(oversize))


StoreFactory = Callable[[Scope], CheckpointStore | None]
_factory: StoreFactory | None = None
_lock = threading.Lock()


def set_store_factory(factory: StoreFactory | None) -> StoreFactory | None:
    """Install the store maker (P11, or a test fake); returns the previous one."""
    global _factory
    with _lock:
        previous, _factory = _factory, factory
    return previous


def store_for(scope: Scope) -> CheckpointStore | None:
    """The checkpoint store of ``scope``'s profile, or None when none is available."""
    with _lock:
        factory = _factory
    return factory(scope) if factory is not None else None
