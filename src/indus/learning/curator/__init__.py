"""The curator: idle-time upkeep of the skills the background review wrote (P09-18..20).

This package marker also holds the ``SkillReferenceSource`` protocol and its registry. A reference
source is anything outside the skills tree that names skills — P14's cron store is the first.
The curator never archives a skill a source names (R07.8), and after a consolidation pass it asks
every source to follow the renames. P09 ships the protocol with an empty registry.

A source that raises while listing its references makes the protected set unknown; the curator
then archives nothing in that run (``referenced_skills`` returns ``None``).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

__all__ = [
    "ReferenceRewrite",
    "SkillReferenceSource",
    "clear_reference_sources",
    "reference_sources",
    "referenced_skills",
    "register_reference_source",
    "rewrite_references",
]

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReferenceRewrite:
    """What one source changed: ``changes`` are human-readable lines (``job x: a → b``)."""

    source: str
    changes: tuple[str, ...] = ()
    error: str | None = None
    details: Mapping[str, object] = field(default_factory=dict[str, object])


@runtime_checkable
class SkillReferenceSource(Protocol):
    """Something that names skills by name and must keep working when they move."""

    @property
    def name(self) -> str: ...

    def referenced_skills(self) -> set[str]:
        """Every skill name this source refers to (paused or disabled users included)."""
        ...

    def rewrite(self, renames: Mapping[str, str], dropped: Collection[str]) -> ReferenceRewrite:
        """Point references at the new names; ``dropped`` ones were archived with no successor."""
        ...


_LOCK = threading.Lock()
_SOURCES: list[SkillReferenceSource] = []


def register_reference_source(source: SkillReferenceSource) -> Callable[[], None]:
    """Add ``source``; the returned callable removes it again."""
    with _LOCK:
        _SOURCES.append(source)

    def remove() -> None:
        with _LOCK:
            if source in _SOURCES:
                _SOURCES.remove(source)

    return remove


def reference_sources() -> list[SkillReferenceSource]:
    with _LOCK:
        return list(_SOURCES)


def clear_reference_sources() -> None:
    """Tests only: forget every registered source."""
    with _LOCK:
        _SOURCES.clear()


def referenced_skills() -> set[str] | None:
    """The union of every source's names; ``None`` when any source failed (protection unknown)."""
    names: set[str] = set()
    for source in reference_sources():
        try:
            names.update(source.referenced_skills())
        except Exception as exc:
            log.warning(
                "skill reference source %s failed (%s); nothing is archived this run",
                getattr(source, "name", source),
                exc,
            )
            return None
    return names


def rewrite_references(
    renames: Mapping[str, str], dropped: Collection[str]
) -> list[ReferenceRewrite]:
    """Ask every source to follow ``renames``; a failing source is reported, never raised."""
    if not renames and not dropped:
        return []
    out: list[ReferenceRewrite] = []
    for source in reference_sources():
        label = str(getattr(source, "name", type(source).__name__))
        try:
            out.append(source.rewrite(dict(renames), list(dropped)))
        except Exception as exc:
            log.warning("skill reference source %s could not follow renames: %s", label, exc)
            out.append(ReferenceRewrite(label, error=f"{type(exc).__name__}: {exc}"))
    return out
