"""The skill mutation ledger: one JSON line per change to a skill, and single-entry rollback.

``<home>/skills/.ledger.jsonl`` gets one line per change, whoever made it::

    {id, ts, actor: curator|agent|user, action, skill,
     evidence: {absorbed_into?, archived?, session_id?, file_path?, rollback_of?},
     before: [{path, sha256}], after: [{path, sha256}]}

``before`` / ``after`` list every file of the skill package around the change (absolute paths).
The bytes behind each hash are stored once in ``<home>/skills/.ledger-blobs/<sha256>``.

The ledger records; it never decides. ``record`` swallows every failure (logged) so a change goes
through even when its line cannot be written. ``rollback`` is the exception: it fails closed. It
checks every blob it needs, then appends its own entry carrying the current bytes of every path it
will touch (so the rollback can itself be rolled back), and only then restores. If that capture
fails nothing is changed.

Size: past ``skills.ledger_max_bytes`` (default 5 MiB, ``0`` = never) the ledger is rewritten
without unchanged-file pairs, then without its oldest lines down to three quarters of the cap (the
newest line always stays), and blobs no line references are deleted once older than an hour.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import logging
import os
import secrets
from collections.abc import Generator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any, Literal, cast

from indus.foundation.clock import Clock, SystemClock
from indus.foundation.fsio import FileLock, atomic_write_bytes
from indus.foundation.scope import Scope

__all__ = [
    "ACTIONS",
    "ACTORS",
    "BLOB_DIR",
    "BLOB_GRACE_S",
    "DEFAULT_MAX_BYTES",
    "LEDGER_FILE",
    "Actor",
    "FileRef",
    "LedgerEntry",
    "RollbackResult",
    "SkillLedger",
    "bind_actor",
    "current_actor",
]

log = logging.getLogger(__name__)

LEDGER_FILE = ".ledger.jsonl"
BLOB_DIR = ".ledger-blobs"
LOCK_FILE = Path(".locks") / "ledger.lock"
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
BLOB_GRACE_S = 3600.0
TRIM_FRACTION = 0.75
LOCK_TIMEOUT_S = 15.0

type Actor = Literal["curator", "agent", "user"]
ACTORS: tuple[Actor, ...] = ("curator", "agent", "user")
ACTIONS = (
    "create",
    "patch",
    "edit",
    "write_file",
    "remove_file",
    "delete",
    "archive",
    "restore",
    "rollback",
    "purge",
)
# Folder names that are regenerated or private and never part of a skill package.
SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
)

_ACTOR: contextvars.ContextVar[Actor | None] = contextvars.ContextVar(
    "indus_ledger_actor", default=None
)


def current_actor() -> Actor:
    """The bound actor, else ``agent`` (a model write, foreground or review)."""
    return _ACTOR.get() or "agent"


@contextlib.contextmanager
def bind_actor(actor: Actor) -> Generator[None]:
    """Record changes in this context as made by ``actor`` (the CLI binds ``user``, the curator
    ``curator``)."""
    if actor not in ACTORS:
        raise ValueError(f"unknown ledger actor {actor!r}")
    token = _ACTOR.set(actor)
    try:
        yield
    finally:
        _ACTOR.reset(token)


@dataclass(frozen=True)
class FileRef:
    path: str
    sha256: str

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256}


def _refs(raw: object) -> list[FileRef]:
    out: list[FileRef] = []
    if not isinstance(raw, list):
        return out
    for item in cast(list[object], raw):
        if isinstance(item, Mapping):
            row = cast(Mapping[str, Any], item)
            path, digest = row.get("path"), row.get("sha256")
            if isinstance(path, str) and isinstance(digest, str):
                out.append(FileRef(path, digest))
    return out


def changed_pairs(
    before: Sequence[FileRef], after: Sequence[FileRef]
) -> tuple[list[FileRef], list[FileRef]]:
    """``before`` / ``after`` without the paths whose hash is the same on both sides."""
    old = {r.path: r.sha256 for r in before}
    new = {r.path: r.sha256 for r in after}
    same = {p for p, h in old.items() if new.get(p) == h}
    return [r for r in before if r.path not in same], [r for r in after if r.path not in same]


@dataclass(frozen=True)
class LedgerEntry:
    id: str
    ts: str
    actor: str
    action: str
    skill: str
    evidence: Mapping[str, Any] = field(default_factory=dict[str, Any])
    before: tuple[FileRef, ...] = ()
    after: tuple[FileRef, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "actor": self.actor,
            "action": self.action,
            "skill": self.skill,
            "evidence": dict(self.evidence),
            "before": [r.as_dict() for r in self.before],
            "after": [r.as_dict() for r in self.after],
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> LedgerEntry | None:
        ident = data.get("id")
        if not isinstance(ident, str) or not ident:
            return None
        evidence = data.get("evidence")
        return cls(
            id=ident,
            ts=str(data.get("ts") or ""),
            actor=str(data.get("actor") or ""),
            action=str(data.get("action") or ""),
            skill=str(data.get("skill") or ""),
            evidence=dict(cast(Mapping[str, Any], evidence))
            if isinstance(evidence, Mapping)
            else {},
            before=tuple(_refs(data.get("before"))),
            after=tuple(_refs(data.get("after"))),
        )

    def changes(self) -> tuple[list[FileRef], list[FileRef]]:
        return changed_pairs(self.before, self.after)


@dataclass(frozen=True)
class RollbackResult:
    ok: bool
    message: str
    entry_id: str | None = None
    """The id of the rollback's own entry (the undo of this rollback)."""
    restored: int = 0
    removed: int = 0


def _is_inside(path: str, root: Path) -> bool:
    base = os.path.normpath(os.path.abspath(root))
    norm = os.path.normpath(path)
    return norm != base and norm.startswith(base + os.sep)


class SkillLedger:
    """The ledger and blob store of one profile (see the module notes)."""

    def __init__(
        self,
        home: Path,
        *,
        clock: Clock | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        roots: Iterable[Path] = (),
    ) -> None:
        self.home = home
        self.skills_root = home / "skills"
        self.path = self.skills_root / LEDGER_FILE
        self.blobs = self.skills_root / BLOB_DIR
        self.clock: Clock = clock if clock is not None else SystemClock()
        self.max_bytes = max_bytes
        self.roots: tuple[Path, ...] = (self.skills_root, *roots)

    @classmethod
    def for_scope(cls, scope: Scope) -> SkillLedger:
        from indus.learning.skills.resolver import SkillsSettings, skills_setting

        raw = skills_setting(scope, "skills.ledger_max_bytes")
        max_bytes = raw if isinstance(raw, int) and not isinstance(raw, bool) else DEFAULT_MAX_BYTES
        try:
            settings = SkillsSettings.from_scope(scope)
            roots = [root for _tier, root in settings.roots()]
        except Exception:
            log.debug(
                "skill roots unreadable; the ledger trusts the local root only", exc_info=True
            )
            roots = []
        return cls(scope.home, clock=scope.clock, max_bytes=max_bytes, roots=roots)

    def _lock(self) -> FileLock:
        lock = self.skills_root / LOCK_FILE
        lock.parent.mkdir(parents=True, exist_ok=True)
        return FileLock(lock, timeout=LOCK_TIMEOUT_S)

    def _now(self) -> str:
        return self.clock.now().astimezone(UTC).isoformat(timespec="seconds")

    # -- blobs ------------------------------------------------------------------------------------

    def store_blob(self, data: bytes) -> str:
        """Keep ``data`` under its hash (once); returns the hash. Raises on IO failure."""
        digest = hashlib.sha256(data).hexdigest()
        target = self.blobs / digest
        if not target.is_file():
            self.blobs.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(target, data, mode=0o600)
        return digest

    def read_blob(self, digest: str) -> bytes | None:
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            return None
        try:
            return (self.blobs / digest).read_bytes()
        except OSError:
            return None

    # -- capture ----------------------------------------------------------------------------------

    @staticmethod
    def package_files(folder: Path) -> list[Path]:
        """Every file of a skill package, sorted (regenerated and private folders skipped)."""
        if folder.is_file():
            return [folder]
        if not folder.is_dir():
            return []
        found: list[Path] = []
        for current, dirs, files in os.walk(folder):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            found.extend(Path(current) / name for name in files)
        return sorted(found)

    def capture(self, folder: Path | None) -> list[FileRef]:
        """``{path, sha256}`` for every file of ``folder`` (blobs stored). Raises on IO failure."""
        if folder is None:
            return []
        return [
            FileRef(str(p), self.store_blob(p.read_bytes())) for p in self.package_files(folder)
        ]

    def capture_quietly(self, folder: Path | None) -> list[FileRef] | None:
        """``capture`` for telemetry: None (logged) instead of raising."""
        try:
            return self.capture(folder)
        except Exception as exc:
            log.warning("skill ledger: could not capture %s: %s", folder, exc)
            return None

    # -- writing ----------------------------------------------------------------------------------

    def _append(self, entry: LedgerEntry) -> None:
        line = json.dumps(entry.to_json(), ensure_ascii=False) + "\n"
        self.skills_root.mkdir(parents=True, exist_ok=True)
        with self._lock():
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            self._maintain_locked()

    def _entry(
        self,
        action: str,
        skill: str,
        before: Sequence[FileRef],
        after: Sequence[FileRef],
        actor: Actor | None,
        evidence: Mapping[str, Any] | None,
    ) -> LedgerEntry:
        return LedgerEntry(
            id=secrets.token_hex(6),
            ts=self._now(),
            actor=actor if actor in ACTORS else current_actor(),
            action=action,
            skill=skill,
            evidence={k: v for k, v in (evidence or {}).items() if v is not None},
            before=tuple(before),
            after=tuple(after),
        )

    def record(
        self,
        action: str,
        skill: str,
        *,
        before: Sequence[FileRef] | None = None,
        after: Sequence[FileRef] | None = None,
        after_dir: Path | None = None,
        actor: Actor | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> str | None:
        """Append one entry; its id, or None when it could not be written (never raises).
        ``after`` wins over ``after_dir`` (captured now)."""
        try:
            after_refs = list(after) if after is not None else self.capture(after_dir)
            entry = self._entry(action, skill, before or [], after_refs, actor, evidence)
            self._append(entry)
            return entry.id
        except Exception as exc:
            log.warning("skill ledger: %s of %s not recorded: %s", action, skill, exc)
            return None

    # -- reading ----------------------------------------------------------------------------------

    def _lines(self) -> list[str] | None:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return []
        except OSError as exc:
            log.warning("skill ledger %s unreadable: %s", self.path, exc)
            return None
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            log.warning("skill ledger %s is not UTF-8", self.path)
            return None
        return [line for line in text.split("\n") if line.strip()]

    def entries(self, skill: str | None = None, limit: int | None = None) -> list[LedgerEntry]:
        """Entries, newest first; malformed lines are skipped."""
        rows: list[LedgerEntry] = []
        for line in reversed(self._lines() or []):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if not isinstance(data, Mapping):
                continue
            entry = LedgerEntry.from_json(cast(Mapping[str, Any], data))
            if entry is None or (skill is not None and entry.skill != skill):
                continue
            rows.append(entry)
            if limit is not None and len(rows) >= limit:
                break
        return rows

    def get(self, entry_id: str) -> LedgerEntry | None:
        return next((e for e in self.entries() if e.id == entry_id), None)

    # -- rollback ---------------------------------------------------------------------------------

    def _allowed(self, path: str) -> bool:
        if not os.path.isabs(path) or ".." in Path(path).parts:
            return False
        private = (self.blobs, self.skills_root / ".locks")
        if any(_is_inside(path, p) for p in private) or os.path.normpath(path) == str(self.path):
            return False
        return any(_is_inside(path, root) for root in self.roots)

    def rollback(self, entry_id: str, *, actor: Actor | None = None) -> RollbackResult:
        """Put back the files ``entry_id`` changed (see the module notes)."""
        entry = self.get(entry_id)
        if entry is None:
            return RollbackResult(False, f"no ledger entry {entry_id!r}")
        before, after = entry.changes()
        if not before and not after:
            return RollbackResult(False, f"entry {entry_id} changed no files; nothing to roll back")
        for ref in (*before, *after):
            if not self._allowed(ref.path):
                return RollbackResult(
                    False, f"refused: entry {entry_id} names {ref.path}, outside the skill folders"
                )
        restore: list[tuple[Path, bytes]] = []
        for ref in before:
            data = self.read_blob(ref.sha256)
            if data is None:
                return RollbackResult(
                    False,
                    f"the saved copy of {ref.path} is missing ({ref.sha256[:12]}); nothing changed",
                )
            restore.append((Path(ref.path), data))
        touched = sorted({ref.path for ref in (*before, *after)})
        try:
            current = [
                FileRef(p, self.store_blob(Path(p).read_bytes()))
                for p in touched
                if Path(p).is_file()
            ]
            safety = self._entry(
                "rollback",
                entry.skill,
                current,
                [FileRef(str(p), hashlib.sha256(d).hexdigest()) for p, d in restore],
                actor,
                {"rollback_of": entry_id},
            )
            self._append(safety)
        except Exception as exc:
            log.warning("skill ledger: rollback safety capture failed: %s", exc)
            return RollbackResult(
                False,
                f"could not save the current files before rolling back ({exc}); nothing changed",
            )
        keep = {ref.path for ref in before}
        restored = removed = 0
        try:
            for path, data in restore:
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_bytes(path, data)
                restored += 1
            for ref in after:
                if ref.path in keep:
                    continue
                target = Path(ref.path)
                if target.is_file() or target.is_symlink():
                    target.unlink()
                    removed += 1
                    self._prune_empty(target.parent)
        except OSError as exc:
            log.error("skill ledger: rollback of %s stopped part-way: %s", entry_id, exc)
            return RollbackResult(
                False,
                f"rollback stopped part-way ({exc}); roll back entry {safety.id} to return to "
                "where you were",
                safety.id,
                restored,
                removed,
            )
        return RollbackResult(
            True,
            f"rolled back {entry.action} of {entry.skill!r} ({entry_id}): {restored} file(s) "
            f"restored, {removed} removed; undo with entry {safety.id}",
            safety.id,
            restored,
            removed,
        )

    def _prune_empty(self, folder: Path) -> None:
        """Remove empty folders upward, never a skills root itself."""
        roots = {os.path.normpath(os.path.abspath(r)) for r in self.roots}
        current = folder
        while os.path.normpath(os.path.abspath(current)) not in roots and any(
            _is_inside(str(current), r) for r in self.roots
        ):
            try:
                current.rmdir()
            except OSError:
                return
            current = current.parent

    # -- size -------------------------------------------------------------------------------------

    def maintain(self) -> None:
        """Run the size rules now (normally after each append)."""
        try:
            with self._lock():
                self._maintain_locked()
        except Exception as exc:
            log.warning("skill ledger maintenance failed: %s", exc)

    def _maintain_locked(self) -> None:
        if self.max_bytes <= 0:
            return
        try:
            if self.path.stat().st_size <= self.max_bytes:
                return
            size = self._compact_locked()
            if size > self.max_bytes:
                self._trim_locked(int(self.max_bytes * TRIM_FRACTION))
            self._sweep_locked()
        except Exception as exc:
            log.warning("skill ledger maintenance failed; the ledger is left as it is: %s", exc)

    def _rewrite(self, lines: Sequence[str]) -> int:
        data = "".join(line + "\n" for line in lines).encode("utf-8")
        atomic_write_bytes(self.path, data)
        return len(data)

    def compact(self) -> int:
        """Drop unchanged-file pairs from every entry; the new size in bytes."""
        with self._lock():
            return self._compact_locked()

    def _compact_locked(self) -> int:
        lines = self._lines()
        if lines is None:
            return self.path.stat().st_size if self.path.exists() else 0
        out: list[str] = []
        for line in lines:
            try:
                data = json.loads(line)
            except ValueError:
                out.append(line)
                continue
            entry = (
                LedgerEntry.from_json(cast(Mapping[str, Any], data))
                if isinstance(data, Mapping)
                else None
            )
            if entry is None or entry.action == "rollback":
                out.append(line)
                continue
            before, after = entry.changes()
            slim = LedgerEntry(
                entry.id,
                entry.ts,
                entry.actor,
                entry.action,
                entry.skill,
                entry.evidence,
                tuple(before),
                tuple(after),
            )
            out.append(json.dumps(slim.to_json(), ensure_ascii=False))
        return self._rewrite(out)

    def _trim_locked(self, target: int) -> int:
        lines = self._lines()
        if not lines:
            return 0
        kept: list[str] = []
        size = 0
        for line in reversed(lines):
            cost = len(line.encode("utf-8")) + 1
            if kept and size + cost > target:
                break
            kept.append(line)
            size += cost
        dropped = len(lines) - len(kept)
        if dropped:
            kept.reverse()
            self._rewrite(kept)
        return dropped

    def sweep_blobs(self) -> int:
        """Delete blobs no entry references and older than ``BLOB_GRACE_S``; how many went."""
        with self._lock():
            return self._sweep_locked()

    def _sweep_locked(self) -> int:
        if not self.blobs.is_dir():
            return 0
        lines = self._lines()
        if lines is None:
            return 0
        referenced: set[str] = set()
        for line in lines:
            try:
                data = json.loads(line)
            except ValueError:
                log.warning("skill ledger has an unreadable line; blob sweep skipped")
                return 0
            entry = (
                LedgerEntry.from_json(cast(Mapping[str, Any], data))
                if isinstance(data, Mapping)
                else None
            )
            if entry is None:
                log.warning("skill ledger has an unreadable line; blob sweep skipped")
                return 0
            referenced.update(r.sha256 for r in (*entry.before, *entry.after))
        cutoff = self.clock.now().timestamp() - BLOB_GRACE_S
        removed = 0
        for blob in self.blobs.iterdir():
            if blob.name in referenced or not blob.is_file():
                continue
            try:
                if blob.stat().st_mtime > cutoff:
                    continue
                blob.unlink()
            except OSError:
                continue
            removed += 1
        return removed
