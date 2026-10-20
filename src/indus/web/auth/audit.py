"""The dashboard's auth audit log (P18-03).

One JSON object per line in ``<home>/logs/dashboard-auth.log``: when (UTC ISO time), the event
name and a few plain fields (provider, user id, reason, client IP, path). Before a line is written:

- fields named like a credential (tokens, codes, verifiers, state, tickets, cookies, passwords,
  secrets, Authorization) are dropped whatever their value;
- every remaining string goes through the credential-shape masker (``foundation.redact``) and the
  exact values the caller registered (the session token, the signing secret), then is cut to 256
  characters.

The file is created 0600 (a 0700 log directory), rotated at 5 MiB keeping three older files. A
failed write is logged as a warning and never raises: login must not fail because the log broke.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, cast

from indus.foundation.redact import redact_text

__all__ = ["AUDIT_FILE", "AuditEvent", "AuditLog", "SECRET_FIELDS"]

log = logging.getLogger(__name__)

AUDIT_FILE: Final = "dashboard-auth.log"
MAX_BYTES: Final = 5 * 1024 * 1024
BACKUPS: Final = 3
MAX_FIELD: Final = 256
_CUT: Final = "…[cut]"

SECRET_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "access_token",
        "refresh_token",
        "id_token",
        "token",
        "code",
        "code_verifier",
        "verifier",
        "state",
        "nonce",
        "ticket",
        "cookie",
        "cookies",
        "authorization",
        "password",
        "password_hash",
        "secret",
    }
)


class AuditEvent(StrEnum):
    LOGIN_START = "login_start"
    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILURE = "login_failure"
    LOGOUT = "logout"
    VERIFY_FAILURE = "session_verify_failure"
    SESSION_REJECTED = "session_rejected"
    REFRESH_SUCCESS = "refresh_success"
    REFRESH_FAILURE = "refresh_failure"
    WS_TICKET_MINTED = "ws_ticket_minted"
    WS_TICKET_REFUSED = "ws_ticket_refused"
    TOKEN_AUTH_FAILURE = "token_auth_failure"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class AuditLog:
    """Writer for one home's audit file (thread-safe)."""

    def __init__(
        self,
        path: Path,
        *,
        now: Callable[[], str] = _now_iso,
        max_bytes: int = MAX_BYTES,
        secrets: Iterable[str] = (),
    ) -> None:
        self.path = path
        self._now = now
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._secrets: list[str] = [value for value in secrets if value]

    @classmethod
    def for_home(cls, home: Path, **kwargs: Any) -> AuditLog:
        return cls(home / "logs" / AUDIT_FILE, **kwargs)

    def add_secret(self, value: str) -> None:
        """An exact value that must never appear in a line (masked if it does)."""
        if value:
            with self._lock:
                self._secrets.append(value)

    def _clean(self, value: object) -> object:
        if isinstance(value, bool | int | float) or value is None:
            return value
        if isinstance(value, dict):
            return {
                str(key): self._clean(item)
                for key, item in cast(dict[object, object], value).items()
                if str(key).lower() not in SECRET_FIELDS
            }
        if isinstance(value, list | tuple):
            return [self._clean(item) for item in cast(Iterable[object], value)]
        text = value if isinstance(value, str) else repr(value)
        text = redact_text(text, self._secrets)
        if len(text) > MAX_FIELD:
            text = text[: MAX_FIELD - len(_CUT)] + _CUT
        return text

    def line(self, event: AuditEvent | str, **fields: object) -> str:
        """The JSON line ``record`` would write (without the newline)."""
        entry: dict[str, object] = {"ts": self._now(), "event": str(event)}
        for key, value in fields.items():
            if key.lower() in SECRET_FIELDS:
                continue
            entry[key] = self._clean(value)
        return json.dumps(entry, separators=(",", ":"), ensure_ascii=False)

    def record(self, event: AuditEvent | str, **fields: object) -> None:
        try:
            data = (self.line(event, **fields) + "\n").encode("utf-8")
            with self._lock:
                self._open_dir()
                self._rotate(len(data))
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    os.write(fd, data)
                finally:
                    os.close(fd)
        except Exception as exc:
            log.warning("the dashboard auth audit log could not be written: %s", type(exc).__name__)

    def _open_dir(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _rotate(self, pending: int) -> None:
        try:
            if self.path.stat().st_size + pending < self._max_bytes:
                return
        except OSError:
            return
        try:
            for index in range(BACKUPS - 1, 0, -1):
                older = self.path.with_name(f"{self.path.name}.{index}")
                if older.exists():
                    os.replace(older, self.path.with_name(f"{self.path.name}.{index + 1}"))
            os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))
        except OSError:
            pass
