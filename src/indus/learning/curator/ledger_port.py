"""Where the curator reports skill mutations: a small port the skill ledger (P09-13) plugs into.

The curator moves folders (archive, restore), deletes old archives (purge) and lets a person edit
a skill from the journey; each of those is ledgered so it can be audited and rolled back. Ledger
writes are telemetry, never a gate: with no ledger, or when it fails, the mutation still happens
and a debug line is logged.

Which ledger: one plugged in with ``set_ledger`` wins (tests plug a fake); otherwise, when the
caller names its ``scope``, the profile's own ``SkillLedger`` (``<home>/skills/.ledger.jsonl``)
through ``SkillLedgerPort`` — so every host gets the real ledger without wiring a process global.

``capture(skill_dir)`` is called before the change and returns an opaque value the ledger later
gets back in ``record``; ``after_dir`` is where the files are once the change is done (None when
they are gone).
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable, Generator, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from indus.foundation.scope import Scope

__all__ = [
    "LedgerPort",
    "SkillLedgerPort",
    "capture",
    "get_ledger",
    "ledgered",
    "record",
    "resolve",
    "set_ledger",
]

log = logging.getLogger(__name__)


@runtime_checkable
class LedgerPort(Protocol):
    def capture(self, skill_dir: Path, *, skill: str) -> Any:
        """The before-state of ``skill_dir`` (file hashes, contents stored once per hash)."""
        ...

    def record(
        self,
        action: str,
        skill: str,
        *,
        actor: str,
        before: Any,
        after_dir: Path | None,
        evidence: Mapping[str, Any] | None = None,
    ) -> str | None:
        """Append one entry; returns its id."""
        ...

    def entries(self, *, skill: str | None = None, limit: int = 20) -> Sequence[Mapping[str, Any]]:
        """Newest first."""
        ...

    def get(self, entry_id: str) -> Mapping[str, Any] | None: ...

    def rollback(self, entry_id: str) -> tuple[bool, str]:
        """Restore exactly what one entry changed (fails closed)."""
        ...


_lock = threading.Lock()
_port: LedgerPort | None = None


def set_ledger(port: LedgerPort | None) -> Callable[[], None]:
    """Plug ``port`` in (None unplugs); the returned callable restores the previous one."""
    global _port
    with _lock:
        previous = _port
        _port = port

    def restore() -> None:
        global _port
        with _lock:
            _port = previous

    return restore


def get_ledger() -> LedgerPort | None:
    with _lock:
        return _port


class SkillLedgerPort:
    """``LedgerPort`` over one profile's ``SkillLedger`` (P09-13)."""

    def __init__(self, scope: Scope) -> None:
        from indus.learning.skills.ledger import SkillLedger

        self.ledger = SkillLedger.for_scope(scope)

    def capture(self, skill_dir: Path, *, skill: str) -> Any:
        return self.ledger.capture(skill_dir)

    def record(
        self,
        action: str,
        skill: str,
        *,
        actor: str,
        before: Any,
        after_dir: Path | None,
        evidence: Mapping[str, Any] | None = None,
    ) -> str | None:
        from indus.learning.skills.ledger import ACTORS, Actor

        known: Actor | None = next((a for a in ACTORS if a == actor), None)
        return self.ledger.record(
            action,
            skill,
            before=before if isinstance(before, list) else [],
            after_dir=after_dir,
            actor=known,
            evidence=evidence,
        )

    def entries(self, *, skill: str | None = None, limit: int = 20) -> Sequence[Mapping[str, Any]]:
        return [e.to_json() for e in self.ledger.entries(skill=skill, limit=limit)]

    def get(self, entry_id: str) -> Mapping[str, Any] | None:
        entry = self.ledger.get(entry_id)
        return entry.to_json() if entry is not None else None

    def rollback(self, entry_id: str) -> tuple[bool, str]:
        result = self.ledger.rollback(entry_id, actor="user")
        return result.ok, result.message


def resolve(scope: Scope | None = None) -> LedgerPort | None:
    """The plugged-in port, else the ledger of ``scope``'s profile (None without either)."""
    port = get_ledger()
    if port is not None or scope is None:
        return port
    try:
        return SkillLedgerPort(scope)
    except Exception:
        log.debug("the skill ledger of %s is unavailable", scope.home, exc_info=True)
        return None


def capture(skill_dir: Path, skill: str, *, scope: Scope | None = None) -> Any:
    port = resolve(scope)
    if port is None:
        return None
    try:
        return port.capture(skill_dir, skill=skill)
    except Exception:
        log.debug("ledger capture of %s failed", skill, exc_info=True)
        return None


def record(
    action: str,
    skill: str,
    *,
    actor: str,
    before: Any,
    after_dir: Path | None,
    evidence: Mapping[str, Any] | None = None,
    scope: Scope | None = None,
) -> str | None:
    port = resolve(scope)
    if port is None:
        return None
    try:
        return port.record(
            action, skill, actor=actor, before=before, after_dir=after_dir, evidence=evidence
        )
    except Exception:
        log.debug("ledger entry %s for %s not written", action, skill, exc_info=True)
        return None


@contextlib.contextmanager
def ledgered(
    action: str,
    skill: str,
    skill_dir: Path,
    *,
    actor: str,
    evidence: Mapping[str, Any] | None = None,
    scope: Scope | None = None,
) -> Generator[dict[str, Path | None]]:
    """Capture before the block, record after it when it finished; the block sets
    ``out["after"]`` to where the files are now (default: ``skill_dir``)."""
    port = resolve(scope)
    before = None
    if port is not None:
        try:
            before = port.capture(skill_dir, skill=skill)
        except Exception:
            log.debug("ledger capture of %s failed", skill, exc_info=True)
    out: dict[str, Path | None] = {"after": skill_dir}
    yield out
    if port is None:
        return
    try:
        port.record(
            action, skill, actor=actor, before=before, after_dir=out["after"], evidence=evidence
        )
    except Exception:
        log.debug("ledger entry %s for %s not written", action, skill, exc_info=True)
