# pyright: strict
"""What a persistent sandbox restores from, kept per backend kind (P21-01).

A persistent cloud sandbox or Singularity overlay outlives the process that made it. The next
process finds its restore point here: one JSON file per backend kind at
``<profile home>/sandboxes/records/<kind>.json``, mapping ``<profile id>:<task key>`` to a record
(vendor snapshot id, overlay directory, sandbox name, created/updated times).

Every read and write goes through ``mutate_records``: an exclusive lock on ``<kind>.json.lock``
(10 s to acquire, then ``SandboxRecordBusy``), a fresh read inside the lock, the caller's mutation,
and, only when the data changed, an atomic write (temp file, fsync, rename) with mode 0600 in a
0700 directory. A file that does not parse is moved aside to ``<kind>.json.corrupt-<UTC stamp>``
with one warning and read as empty. Keys always carry the profile id, so one profile never sees
another's records (R02.8); there are no legacy un-prefixed keys (D-16.3). Nothing here reads the
home at import: the scope (or home and profile id) is passed in.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any, cast

from indus.backends.contract import BackendError
from indus.foundation.clock import Clock, SystemClock
from indus.foundation.fsio import FileLock, LockTimeout, atomic_write_json
from indus.foundation.scope import Scope

__all__ = [
    "LOCK_TIMEOUT_S",
    "RECORDS_DIR",
    "SandboxRecord",
    "SandboxRecordBusy",
    "SandboxRecords",
    "mutate_records",
    "record_key",
]

log = logging.getLogger(__name__)

RECORDS_DIR = Path("sandboxes") / "records"
LOCK_TIMEOUT_S = 10.0
LOCK_POLL_S = 0.05
FILE_MODE = 0o600
DIR_MODE = 0o700
_KIND = re.compile(r"^[a-z0-9_]+$")
_FIELDS = ("snapshot_id", "overlay_path", "sandbox_name")
_UNSET: Any = object()

type RawRecords = dict[str, Any]


class SandboxRecordBusy(BackendError):
    """The records file stayed locked past the deadline (another process is stuck on it)."""

    default_code = "backend.sandbox_records_busy"


@dataclass(frozen=True, slots=True)
class SandboxRecord:
    """One task's restore point; any of the three fields may be missing."""

    created_at: str
    updated_at: str
    snapshot_id: str | None = None
    overlay_path: str | None = None
    sandbox_name: str | None = None

    def to_json(self) -> dict[str, str]:
        out = {"created_at": self.created_at, "updated_at": self.updated_at}
        for name in _FIELDS:
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        return out

    @staticmethod
    def from_json(raw: object) -> SandboxRecord | None:
        if not isinstance(raw, dict):
            return None
        data = cast(dict[str, object], raw)

        def text(name: str) -> str | None:
            value = data.get(name)
            return value if isinstance(value, str) and value else None

        return SandboxRecord(
            created_at=text("created_at") or "",
            updated_at=text("updated_at") or "",
            snapshot_id=text("snapshot_id"),
            overlay_path=text("overlay_path"),
            sandbox_name=text("sandbox_name"),
        )


def record_key(profile_id: str, task_key: str) -> str:
    """The stored key: always namespaced by profile."""
    if not profile_id:
        raise ValueError("a sandbox record needs a profile id")
    return f"{profile_id}:{task_key}"


def _stamp(clock: Clock) -> str:
    return clock.now().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _iso(clock: Clock) -> str:
    return clock.now().astimezone(UTC).isoformat(timespec="seconds")


def _quarantine(path: Path, clock: Clock, why: str) -> None:
    target = path.with_name(f"{path.name}.corrupt-{_stamp(clock)}")
    n = 1
    while target.exists():
        n += 1
        target = path.with_name(f"{path.name}.corrupt-{_stamp(clock)}-{n}")
    try:
        os.replace(path, target)
    except OSError:
        log.warning("sandbox records %s is unreadable (%s) and could not be moved aside", path, why)
        return
    log.warning(
        "sandbox records %s was unreadable (%s); moved to %s and starting empty",
        path,
        why,
        target.name,
    )


def _read(path: Path, clock: Clock) -> RawRecords:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        data: object = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        _quarantine(path, clock, f"not JSON: {exc}")
        return {}
    if not isinstance(data, dict):
        _quarantine(path, clock, "not a JSON object")
        return {}
    return cast(RawRecords, data)


def _ensure_dir(directory: Path) -> None:
    directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    if os.name != "nt":
        with contextlib.suppress(OSError):
            os.chmod(directory, DIR_MODE)


def mutate_records[T](
    path: Path,
    mutation: Callable[[RawRecords], T],
    *,
    clock: Clock | None = None,
    lock_timeout: float = LOCK_TIMEOUT_S,
    wait: Callable[[float], object] | None = None,
) -> T:
    """Lock ``path``, read it again, apply ``mutation`` to the raw dict and write it back when it
    changed. Returns what ``mutation`` returned. Raises ``SandboxRecordBusy`` when the lock is not
    had within ``lock_timeout`` seconds (measured on ``clock``)."""
    clock = clock if clock is not None else SystemClock()
    _ensure_dir(path.parent)
    lock = FileLock(
        path.with_name(f"{path.name}.lock"),
        timeout=lock_timeout,
        poll=LOCK_POLL_S,
        clock=clock,
        wait=wait,
    )
    try:
        lock.acquire()
    except LockTimeout as exc:
        raise SandboxRecordBusy(
            f"the sandbox records file {path.name} stayed locked for {lock_timeout:g}s",
            hint="another Indus process is holding it; try again shortly",
        ) from exc
    try:
        data = _read(path, clock)
        before = json.dumps(data, sort_keys=True)
        result = mutation(data)
        if json.dumps(data, sort_keys=True) != before:
            atomic_write_json(path, data, sort_keys=True, mode=FILE_MODE)
        return result
    finally:
        lock.release()


class SandboxRecords:
    """One profile's view of one backend kind's records (see the module notes)."""

    def __init__(
        self,
        home: Path,
        kind: str,
        *,
        profile_id: str,
        clock: Clock | None = None,
        lock_timeout: float = LOCK_TIMEOUT_S,
        wait: Callable[[float], object] | None = None,
    ) -> None:
        if not _KIND.match(kind):
            raise ValueError(f"bad sandbox record kind {kind!r}")
        if not profile_id:
            raise ValueError("sandbox records need a profile id")
        self.path = home / RECORDS_DIR / f"{kind}.json"
        self.kind = kind
        self.profile_id = profile_id
        self.clock: Clock = clock if clock is not None else SystemClock()
        self.lock_timeout = lock_timeout
        self.wait = wait

    @classmethod
    def for_scope(cls, scope: Scope, kind: str, *, clock: Clock | None = None) -> SandboxRecords:
        return cls(
            scope.home,
            kind,
            profile_id=scope.profile_id,
            clock=clock if clock is not None else scope.clock,
        )

    def _mutate[T](self, mutation: Callable[[RawRecords], T]) -> T:
        return mutate_records(
            self.path,
            mutation,
            clock=self.clock,
            lock_timeout=self.lock_timeout,
            wait=self.wait,
        )

    def _key(self, task_key: str) -> str:
        return record_key(self.profile_id, task_key)

    def get(self, task_key: str) -> SandboxRecord | None:
        key = self._key(task_key)
        return self._mutate(lambda data: SandboxRecord.from_json(data.get(key)))

    def keys(self) -> list[str]:
        prefix = self._key("")
        return self._mutate(
            lambda data: sorted(k[len(prefix) :] for k in data if k.startswith(prefix))
        )

    def put(
        self,
        task_key: str,
        *,
        snapshot_id: str | None = _UNSET,
        overlay_path: str | None = _UNSET,
        sandbox_name: str | None = _UNSET,
    ) -> SandboxRecord:
        """Create or update a record; a field left out keeps its value, ``None`` clears it."""
        key = self._key(task_key)
        now = _iso(self.clock)
        given = {
            "snapshot_id": snapshot_id,
            "overlay_path": overlay_path,
            "sandbox_name": sandbox_name,
        }

        def apply(data: RawRecords) -> SandboxRecord:
            old = SandboxRecord.from_json(data.get(key))
            fields: dict[str, str | None] = {
                name: getattr(old, name) if old is not None else None for name in _FIELDS
            }
            for name, value in given.items():
                if value is not _UNSET:
                    fields[name] = value or None
            record = SandboxRecord(
                created_at=(old.created_at if old is not None and old.created_at else now),
                updated_at=now,
                **fields,
            )
            data[key] = record.to_json()
            return record

        return self._mutate(apply)

    def delete(self, task_key: str, *, only_if_snapshot: str | None = None) -> bool:
        """Remove a record; with ``only_if_snapshot``, only while it still holds that id."""
        key = self._key(task_key)

        def apply(data: RawRecords) -> bool:
            if key not in data:
                return False
            if only_if_snapshot is not None:
                old = SandboxRecord.from_json(data.get(key))
                if old is None or old.snapshot_id != only_if_snapshot:
                    return False
            del data[key]
            return True

        return self._mutate(apply)
