"""The curator's state file ``<home>/skills/.curator_state`` and its run claim.

Shape: ``{last_run_at, paused, claim, run_count, last_summary, last_report, last_duration_s}``.
``claim`` is ``{pid, at}`` while a pass runs: it keeps two processes of one home from running the
automatic pass together. A claim older than ``CLAIM_STALE_S`` (an hour) belongs to a process that
died and is taken over. Every read-modify-write holds ``.curator_state.lock``; a damaged file
reads as empty (and is overwritten on the next save).
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from indus.foundation.fsio import FileLock, atomic_write_json
from indus.foundation.scope import Scope
from indus.learning.curator.archive import parse_time
from indus.learning.curator.settings import CLAIM_STALE_S

__all__ = [
    "STATE_FILE",
    "CuratorState",
    "state_path",
]

log = logging.getLogger(__name__)

STATE_FILE = ".curator_state"
LOCK_FILE = ".curator_state.lock"
LOCK_TIMEOUT_S = 10.0

_DEFAULTS: dict[str, Any] = {
    "last_run_at": None,
    "paused": False,
    "claim": None,
    "run_count": 0,
    "last_summary": None,
    "last_report": None,
    "last_duration_s": None,
}


def state_path(scope: Scope) -> Path:
    return scope.home / "skills" / STATE_FILE


class CuratorState:
    """Read and change one profile's curator state (every call re-reads the file)."""

    def __init__(self, scope: Scope) -> None:
        self.scope = scope
        self.path = state_path(scope)

    def _lock(self) -> FileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return FileLock(self.path.with_name(LOCK_FILE), timeout=LOCK_TIMEOUT_S)

    def load(self) -> dict[str, Any]:
        data = dict(_DEFAULTS)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return data
        except (OSError, ValueError) as exc:
            log.warning("curator state %s is unreadable (%s); starting over", self.path, exc)
            return data
        if isinstance(raw, dict):
            data.update(cast(dict[str, Any], raw))
        return data

    def change(self, fn: Callable[[dict[str, Any]], object]) -> dict[str, Any]:
        """Apply ``fn(state)`` under the lock and save; returns the saved state."""
        with self._lock():
            data = self.load()
            fn(data)
            atomic_write_json(self.path, data, sort_keys=True)
            return data

    def update(self, **values: Any) -> dict[str, Any]:
        return self.change(lambda data: data.update(values))

    # -- facts --------------------------------------------------------------------------------

    @property
    def paused(self) -> bool:
        return self.load().get("paused") is True

    def set_paused(self, paused: bool) -> None:
        self.update(paused=paused)

    def last_run_at(self) -> datetime | None:
        return parse_time(self.load().get("last_run_at"))

    # -- claim --------------------------------------------------------------------------------

    def claim(self, now: datetime) -> bool:
        """Take the run claim; False when a live claim (younger than an hour) is held."""
        taken = False

        def take(data: dict[str, Any]) -> None:
            nonlocal taken
            held = data.get("claim")
            at = parse_time(held.get("at")) if isinstance(held, dict) else None
            if at is not None and (now - at).total_seconds() < CLAIM_STALE_S:
                return
            if at is not None:
                log.info("curator claim from %s is stale; taking it over", held)
            data["claim"] = {"pid": os.getpid(), "at": now.astimezone(UTC).isoformat()}
            taken = True

        self.change(take)
        return taken

    def release(self) -> None:
        self.update(claim=None)
