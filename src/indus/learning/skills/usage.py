"""Skill usage telemetry (``<home>/skills/.usage.json``) and write provenance.

One record per skill::

    {use_count, view_count, patch_count, last_used_at, last_viewed_at, last_patched_at,
     created_at, state: active|stale|archived, pinned, archived_at, origin: agent|learn|null,
     last_activity_at}

Updates take a cross-process lock and replace the file atomically. Bundled skills (listed in
``.bundled_manifest``) and hub skills (``.hub/lock.json``) are never written. Telemetry never
raises into its caller: failures are logged and the event is dropped.

Provenance: the one write-origin context (``learning.pending.bind_origin`` /
``current_origin``, shared with the memory tool) says who is writing right now — ``foreground``
(default), ``background_review`` (set by the review fork) or ``learn`` (a ``/learn`` turn). Only a
create made by the background review records ``origin: agent``, the marker that puts a skill under
the curator's care (R07.6).
"""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
from collections.abc import Callable, Mapping
from datetime import UTC
from pathlib import Path
from typing import Any, cast

from indus.foundation.clock import Clock, SystemClock
from indus.foundation.fsio import FileLock, atomic_write_json
from indus.foundation.scope import Scope
from indus.learning.pending import Origin, current_origin

__all__ = [
    "BACKGROUND_REVIEW",
    "FOREGROUND",
    "LEARN",
    "STATES",
    "USAGE_FILE",
    "Origin",
    "SkillUsage",
    "bundled_names",
    "creation_origin",
    "hub_names",
    "usage_for",
]

log = logging.getLogger(__name__)

USAGE_FILE = ".usage.json"
LOCK_FILE = ".usage.lock"
BUNDLED_MANIFEST = ".bundled_manifest"
HUB_LOCK = Path(".hub") / "lock.json"
STATES = ("active", "stale", "archived")
LOCK_TIMEOUT_S = 10.0

FOREGROUND = "foreground"
BACKGROUND_REVIEW = "background_review"
LEARN = "learn"


def creation_origin(origin: str | None = None) -> str:
    """The ``origin`` a new skill records: ``agent`` only for the background review."""
    return "agent" if (origin or current_origin()) == BACKGROUND_REVIEW else "learn"


def bundled_names(skills_root: Path) -> set[str]:
    """Names in ``.bundled_manifest`` (``<name>:<sha256>`` lines)."""
    try:
        text = (skills_root / BUNDLED_MANIFEST).read_text(encoding="utf-8")
    except OSError:
        return set()
    names: set[str] = set()
    for line in text.splitlines():
        name = line.strip().split(":", 1)[0].strip()
        if name and not name.startswith("#"):
            names.add(name)
    return names


def hub_names(skills_root: Path) -> set[str]:
    """Names in the hub lock (P13 owns the format; a mapping or ``installed`` mapping here)."""
    try:
        data = json.loads((skills_root / HUB_LOCK).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if isinstance(data, dict) and isinstance(data.get("installed"), dict):
        data = data["installed"]
    return {str(k) for k in cast(dict[str, Any], data)} if isinstance(data, dict) else set()


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(value, 0)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def empty_record() -> dict[str, Any]:
    return {
        "use_count": 0,
        "view_count": 0,
        "patch_count": 0,
        "last_used_at": None,
        "last_viewed_at": None,
        "last_patched_at": None,
        "created_at": None,
        "state": "active",
        "pinned": False,
        "archived_at": None,
        "origin": None,
        "last_activity_at": None,
    }


def normalise(raw: object) -> dict[str, Any]:
    """A full record from whatever is on disk (missing or wrong-typed fields back-filled)."""
    rec = empty_record()
    if not isinstance(raw, Mapping):
        return rec
    data = cast(Mapping[str, Any], raw)
    for key in ("use_count", "view_count", "patch_count"):
        rec[key] = _count(data.get(key))
    for key in (
        "last_used_at",
        "last_viewed_at",
        "last_patched_at",
        "created_at",
        "archived_at",
        "last_activity_at",
    ):
        rec[key] = _text(data.get(key))
    state = data.get("state")
    rec["state"] = state if state in STATES else "active"
    rec["pinned"] = data.get("pinned") is True
    origin = data.get("origin")
    rec["origin"] = origin if origin in ("agent", "learn") else None
    return rec


class SkillUsage:
    """The usage sidecar of one skills root."""

    def __init__(
        self,
        skills_root: Path,
        *,
        clock: Clock | None = None,
        lock_timeout: float = LOCK_TIMEOUT_S,
    ) -> None:
        self.root = skills_root
        self.path = skills_root / USAGE_FILE
        self._clock = clock if clock is not None else SystemClock()
        self._lock_timeout = lock_timeout

    def _now(self) -> str:
        return self._clock.now().astimezone(UTC).isoformat(timespec="seconds")

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            log.warning("could not read skill usage %s (%s); starting empty", self.path, exc)
            return {}
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
        except ValueError as exc:
            log.warning(
                "skill usage file %s is damaged (%s); it is kept as %s and rebuilt empty",
                self.path,
                exc,
                self.path.name + ".corrupt",
            )
            with contextlib.suppress(OSError):
                shutil.copyfile(self.path, self.path.with_name(self.path.name + ".corrupt"))
            return {}
        records = cast(dict[str, Any], data)
        return {str(k): normalise(v) for k, v in records.items()}

    def load(self) -> dict[str, dict[str, Any]]:
        return self._read()

    def get(self, name: str) -> dict[str, Any]:
        return self._read().get(name, empty_record())

    def excluded(self, name: str) -> bool:
        return name in bundled_names(self.root) or name in hub_names(self.root)

    def update(
        self, name: str, change: Callable[[dict[str, Any], str], None], *, force: bool = False
    ) -> dict[str, Any] | None:
        """Apply ``change(record, now)`` under the lock; None when skipped or failed."""
        if not force and self.excluded(name):
            return None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with FileLock(self.root / LOCK_FILE, timeout=self._lock_timeout):
                records = self._read()
                rec = records.get(name, empty_record())
                change(rec, self._now())
                records[name] = rec
                atomic_write_json(self.path, records, sort_keys=True)
                return dict(rec)
        except Exception as exc:
            log.warning("skill usage for %s not recorded: %s: %s", name, type(exc).__name__, exc)
            return None

    def forget(self, name: str) -> None:
        try:
            with FileLock(self.root / LOCK_FILE, timeout=self._lock_timeout):
                records = self._read()
                if records.pop(name, None) is not None:
                    atomic_write_json(self.path, records, sort_keys=True)
        except Exception as exc:
            log.warning("skill usage for %s not removed: %s", name, exc)

    # -- events -------------------------------------------------------------------------------

    @staticmethod
    def _touch(rec: dict[str, Any], count_key: str, time_key: str, now: str) -> None:
        rec[count_key] = _count(rec.get(count_key)) + 1
        rec[time_key] = now
        rec["last_activity_at"] = now
        if rec.get("created_at") is None:
            rec["created_at"] = now

    def bump_use(self, name: str) -> dict[str, Any] | None:
        return self.update(name, lambda r, now: self._touch(r, "use_count", "last_used_at", now))

    def bump_view(self, name: str) -> dict[str, Any] | None:
        return self.update(name, lambda r, now: self._touch(r, "view_count", "last_viewed_at", now))

    def bump_patch(self, name: str) -> dict[str, Any] | None:
        return self.update(
            name, lambda r, now: self._touch(r, "patch_count", "last_patched_at", now)
        )

    def record_created(self, name: str, *, origin: str | None = None) -> dict[str, Any] | None:
        recorded = creation_origin(origin)

        def change(rec: dict[str, Any], now: str) -> None:
            rec.update(empty_record())
            rec["created_at"] = now
            rec["last_activity_at"] = now
            rec["origin"] = recorded

        return self.update(name, change)

    def set_state(self, name: str, state: str) -> dict[str, Any] | None:
        if state not in STATES:
            raise ValueError(f"unknown skill state {state!r}")

        def change(rec: dict[str, Any], now: str) -> None:
            rec["state"] = state
            rec["archived_at"] = now if state == "archived" else None

        return self.update(name, change)

    def set_pinned(self, name: str, pinned: bool) -> dict[str, Any] | None:
        return self.update(name, lambda rec, _now: rec.__setitem__("pinned", pinned))

    def set_origin(self, name: str, origin: str | None) -> dict[str, Any] | None:
        if origin not in ("agent", "learn", None):
            raise ValueError(f"unknown skill origin {origin!r}")
        return self.update(name, lambda rec, _now: rec.__setitem__("origin", origin))

    def is_pinned(self, name: str) -> bool:
        return bool(self.get(name).get("pinned"))

    def is_curator_managed(self, name: str) -> bool:
        return self.get(name).get("origin") == "agent" and not self.excluded(name)


def usage_for(scope: Scope) -> SkillUsage:
    """The usage sidecar of the scope's profile (``<home>/skills``)."""
    return SkillUsage(scope.home / "skills", clock=scope.clock)
