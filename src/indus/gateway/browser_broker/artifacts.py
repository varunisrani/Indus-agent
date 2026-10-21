"""Files a browser controller and its callers hand each other, by id only (P24-47).

A screenshot, a downloaded file or a file to put into a page never travels inside a command: it
is stored here and only its id moves. Rules:

- ids are minted by the server: 32 lowercase hex characters from ``secrets``; anything else is
  refused before a path is built from it;
- at most ``MAX_BYTES`` (10 MiB) and a MIME type on ``ALLOWED_MIME`` (PNG, JPEG, WebP, GIF, PDF,
  plain text, JSON, CSV);
- every artefact is bound to the exact scope key it was stored under (the controller identity's
  ``scope_key``) and lives ``TTL_S`` (300 s) on the store's monotonic clock;
- bytes are written to a temp file in the store folder and renamed into place; the SHA-256 taken
  on the way in is checked again on every read, so bytes changed on disk are refused;
- ``consume`` takes an artefact once: the record leaves the table under the lock before the file
  is read, so two consumers can never both get it; the file is removed whatever the outcome.

Refusals raise ``ArtifactRefused`` with a short reason; nothing here logs file contents.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ALLOWED_MIME",
    "ARTIFACT_ID",
    "MAX_BYTES",
    "TTL_S",
    "ArtifactReceipt",
    "ArtifactRefused",
    "ArtifactStore",
]

MAX_BYTES = 10 * 1024 * 1024
TTL_S = 300.0
ALLOWED_MIME: frozenset[str] = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/gif",
        "application/pdf",
        "text/plain",
        "application/json",
        "text/csv",
    }
)
ARTIFACT_ID = re.compile(r"^[0-9a-f]{32}$")
_SUFFIX = ".bin"


class ArtifactRefused(Exception):
    """An artefact could not be stored or read; ``reason`` is a short code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ArtifactReceipt:
    """What the store recorded for one artefact (no bytes)."""

    artifact_id: str
    scope_key: str
    mime: str
    size: int
    sha256: str
    expires_at: float


def _mime(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


class ArtifactStore:
    """Artefacts of one process, kept in ``root`` (see the module notes). Thread-safe."""

    def __init__(
        self,
        root: Path,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        ttl: float = TTL_S,
        max_bytes: int = MAX_BYTES,
    ) -> None:
        self.root = Path(root)
        self.ttl = ttl
        self.max_bytes = max_bytes
        self._now = monotonic
        self._lock = threading.Lock()
        self._records: dict[str, ArtifactReceipt] = {}

    def _path(self, artifact_id: str) -> Path:
        return self.root / f"{artifact_id}{_SUFFIX}"

    def _prune_locked(self, now: float) -> list[str]:
        stale = [key for key, rec in self._records.items() if rec.expires_at <= now]
        for key in stale:
            del self._records[key]
        return stale

    def _unlink(self, artifact_ids: list[str]) -> None:
        for artifact_id in artifact_ids:
            with contextlib.suppress(OSError):
                self._path(artifact_id).unlink()

    def sweep(self) -> int:
        """Forget and delete expired artefacts; how many went."""
        with self._lock:
            stale = self._prune_locked(self._now())
        self._unlink(stale)
        return len(stale)

    def put(self, scope_key: str, data: bytes, mime: str) -> ArtifactReceipt:
        """Store ``data`` for ``scope_key``; the receipt carries the new id."""
        if not scope_key:
            raise ArtifactRefused("scope", "an artefact needs a scope key")
        kind = _mime(mime)
        if kind not in ALLOWED_MIME:
            raise ArtifactRefused("mime", "this file type is not accepted")
        if len(data) > self.max_bytes:
            raise ArtifactRefused("size", f"artefacts are limited to {self.max_bytes} bytes")
        self.sweep()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        artifact_id = secrets.token_hex(16)
        target = self._path(artifact_id)
        tmp = self.root / f".{artifact_id}.tmp-{secrets.token_hex(4)}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
        receipt = ArtifactReceipt(
            artifact_id=artifact_id,
            scope_key=scope_key,
            mime=kind,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            expires_at=self._now() + self.ttl,
        )
        with self._lock:
            self._records[artifact_id] = receipt
        return receipt

    def _live_locked(self, artifact_id: object, scope_key: str) -> ArtifactReceipt:
        if not isinstance(artifact_id, str) or not ARTIFACT_ID.match(artifact_id):
            raise ArtifactRefused("malformed", "not an artefact id")
        stale = self._prune_locked(self._now())
        if stale:
            self._unlink(stale)
        record = self._records.get(artifact_id)
        if record is None:
            raise ArtifactRefused("unknown", "no such artefact (or it expired)")
        if not hmac.compare_digest(record.scope_key, scope_key):
            raise ArtifactRefused("scope", "the artefact belongs to another scope")
        return record

    def check(self, artifact_id: object, scope_key: str) -> ArtifactReceipt:
        """The receipt of a live artefact of ``scope_key`` (nothing is consumed)."""
        with self._lock:
            return self._live_locked(artifact_id, scope_key)

    def consume(self, artifact_id: object, scope_key: str) -> tuple[bytes, ArtifactReceipt]:
        """Take the artefact once: its bytes after the SHA-256 check, and its receipt."""
        with self._lock:
            record = self._live_locked(artifact_id, scope_key)
            del self._records[record.artifact_id]
        path = self._path(record.artifact_id)
        try:
            data = path.read_bytes()
        except OSError:
            raise ArtifactRefused("missing", "the artefact file is gone") from None
        finally:
            with contextlib.suppress(OSError):
                path.unlink()
        if len(data) != record.size or not hmac.compare_digest(
            hashlib.sha256(data).hexdigest(), record.sha256
        ):
            raise ArtifactRefused("changed", "the artefact changed on disk; refused")
        return data, record

    def clear(self) -> None:
        """Delete every artefact (shutdown)."""
        with self._lock:
            ids = list(self._records)
            self._records.clear()
        self._unlink(ids)

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)
