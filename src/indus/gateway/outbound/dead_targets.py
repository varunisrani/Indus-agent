"""Chats proven unreachable (P15-17).

When a send proves a whole chat gone (forbidden, or a chat-level not-found), the target is stored
per profile; unsolicited deliveries (cron output, notices) skip it until a send there succeeds
again. Thread, topic and message not-found readings never mark a chat. The file is best effort:
corrupt → start empty, unwritable → keep the state in memory; delivery never breaks over it.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from indus.foundation.fsio import atomic_write_json
from indus.foundation.home import secure_dir
from indus.foundation.scope import Scope
from indus.gateway.core.send import SendResult
from indus.gateway.outbound import ports

__all__ = ["DeadTargets"]

log = logging.getLogger(__name__)

_REASON_CHARS = 200


def _key(platform: str, chat_id: str) -> str:
    return f"{platform.strip().lower()}:{chat_id.strip()}"


class DeadTargets:
    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._dead: dict[str, dict[str, Any]] = {}
        if path is not None:
            self._dead = self._load(path)

    @classmethod
    def for_scope(cls, scope: Scope) -> DeadTargets:
        return cls(scope.home / "gateway" / "dead_targets.json")

    @staticmethod
    def _load(path: Path) -> dict[str, dict[str, Any]]:
        try:
            raw: object = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.warning("dead-target list unreadable, starting empty: %s", type(exc).__name__)
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): v for k, v in raw.items() if isinstance(v, dict)}  # pyright: ignore[reportUnknownVariableType,reportUnknownArgumentType]

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            secure_dir(self._path.parent)
            atomic_write_json(self._path, self._dead, sort_keys=True, mode=0o600)
        except OSError as exc:
            log.warning("dead-target list not saved (kept in memory): %s", type(exc).__name__)

    def is_dead(self, platform: str, chat_id: str) -> bool:
        with self._lock:
            return bool(chat_id) and _key(platform, chat_id) in self._dead

    def entries(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._dead.items()}

    def mark(self, platform: str, chat_id: str, reason: str = "") -> bool:
        """Store the target as unreachable; True when it was not stored before."""
        if not chat_id:
            return False
        key = _key(platform, chat_id)
        with self._lock:
            new = key not in self._dead
            self._dead[key] = {
                "platform": platform.strip().lower(),
                "chat_id": chat_id,
                "reason": reason[:_REASON_CHARS],
                "marked_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
            self._save()
        if new:
            log.info("chat %s marked unreachable; unsolicited deliveries will skip it", key)
        return new

    def clear(self, platform: str, chat_id: str) -> bool:
        """Forget the target (a send there worked); True when it was stored."""
        key = _key(platform, chat_id)
        with self._lock:
            if self._dead.pop(key, None) is None:
                return False
            self._save()
        log.info("chat %s reachable again", key)
        return True

    def note(self, platform: str, chat_id: str, result: SendResult) -> None:
        """Update the list from one send's result."""
        if result.ok:
            self.clear(platform, chat_id)
        elif ports.chat_unreachable(result):
            self.mark(platform, chat_id, result.error or result.kind or "")
