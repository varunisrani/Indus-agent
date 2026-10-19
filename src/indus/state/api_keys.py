"""The API server's bearer keys: made by ``indus gateway api-key``, checked by the daemon (P15-30).

It lives in the state layer so the CLI and the gateway share it without importing each other
(D-16.246).

- A key is 32 random bytes, URL-safe base64 (``secrets.token_urlsafe``). Its value goes into the
  profile's secret store as ``INDUS_API_KEY_<ID>`` (``ID``: 8 upper-case hex); it is shown once,
  when it is made, and never written anywhere else or logged.
- Labels and creation times sit in ``<home>/gateway/api_keys.json`` (0600, no values).
- ``verify`` compares the offered token with every listed key in constant time and never stops
  early, so the time taken says nothing about which key (if any) is close.
- The list of live keys is read from the index on every check, so a key revoked by another process
  (the CLI while the daemon runs) stops working on the next request even though the daemon's
  secret store still caches its value.
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from indus.foundation import fsio
from indus.foundation.profiles import mkdir_in_profile
from indus.foundation.scope import Scope

__all__ = [
    "INDEX_FILE",
    "KEY_ID_RE",
    "SECRET_PREFIX",
    "ApiKeyInfo",
    "ApiKeyRing",
    "KeyStore",
    "secret_name",
]

SECRET_PREFIX = "INDUS_API_KEY_"
INDEX_FILE = Path("gateway") / "api_keys.json"
KEY_ID_RE = re.compile(r"^[0-9A-F]{8}$")
LABEL_MAX = 80
LOCK_TIMEOUT = 10.0
_FORMAT = 1
_lock = threading.Lock()


class KeyStore(Protocol):
    """The part of ``foundation.secrets.SecretStore`` the ring uses."""

    def get(self, name: str, /) -> str | None: ...

    def set(self, name: str, value: str, /) -> None: ...

    def delete(self, name: str, /) -> bool: ...

    def names(self) -> list[str]: ...


@dataclass(frozen=True, slots=True)
class ApiKeyInfo:
    key_id: str
    label: str
    created_at: str


def secret_name(key_id: str) -> str:
    """The secret-store name holding the value of key ``key_id``."""
    return SECRET_PREFIX + key_id.upper()


class ApiKeyRing:
    """One profile's API keys (see the module notes)."""

    def __init__(self, scope: Scope, store: KeyStore) -> None:
        self.scope = scope
        self.store = store

    # -- index ----------------------------------------------------------------------------------

    @property
    def index_path(self) -> Path:
        return self.scope.home / INDEX_FILE

    def _read_index(self) -> dict[str, dict[str, str]]:
        try:
            raw = self.index_path.read_bytes()
        except OSError:
            return {}
        try:
            data: object = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        keys = cast(dict[str, Any], data).get("keys")
        if not isinstance(keys, dict):
            return {}
        out: dict[str, dict[str, str]] = {}
        for key_id, info in cast(dict[str, Any], keys).items():
            if KEY_ID_RE.match(key_id) and isinstance(info, dict):
                fields = cast(dict[str, Any], info)
                out[key_id] = {
                    "label": str(fields.get("label") or ""),
                    "created_at": str(fields.get("created_at") or ""),
                }
        return out

    def _write_index(self, keys: dict[str, dict[str, str]]) -> None:
        mkdir_in_profile(self.scope.home, INDEX_FILE.parent)
        fsio.atomic_write_json(self.index_path, {"version": _FORMAT, "keys": keys}, mode=0o600)

    def _locked(self) -> fsio.FileLock:
        mkdir_in_profile(self.scope.home, INDEX_FILE.parent)
        return fsio.FileLock(self.index_path.with_suffix(".lock"), timeout=LOCK_TIMEOUT)

    # -- api ------------------------------------------------------------------------------------

    def list(self) -> list[ApiKeyInfo]:
        """Keys that are listed in the index and still have a stored value, oldest first."""
        stored = set(self.store.names())
        rows = [
            ApiKeyInfo(key_id, info["label"], info["created_at"])
            for key_id, info in self._read_index().items()
            if secret_name(key_id) in stored
        ]
        return sorted(rows, key=lambda r: r.created_at)  # stable: ties keep creation order

    def create(self, label: str = "") -> tuple[ApiKeyInfo, str]:
        """Make a key; returns its record and its value (show the value once, then drop it)."""
        clean = " ".join(label.split())[:LABEL_MAX]
        value = secrets.token_urlsafe(32)
        with _lock, self._locked():
            keys = self._read_index()
            key_id = secrets.token_hex(4).upper()
            while key_id in keys:
                key_id = secrets.token_hex(4).upper()
            created = self.scope.clock.now().isoformat(timespec="seconds")
            self.store.set(secret_name(key_id), value)
            keys[key_id] = {"label": clean, "created_at": created}
            self._write_index(keys)
        return ApiKeyInfo(key_id, clean, created), value

    def revoke(self, key_id: str) -> bool:
        """Remove a key (its index row and its stored value); False when there was none."""
        wanted = key_id.strip().upper()
        if not KEY_ID_RE.match(wanted):
            return False
        with _lock, self._locked():
            keys = self._read_index()
            listed = keys.pop(wanted, None) is not None
            if listed:
                self._write_index(keys)
        removed = self.store.delete(secret_name(wanted))
        return listed or removed

    def has_keys(self) -> bool:
        return bool(self.list())

    def verify(self, token: str) -> str | None:
        """The id of the key ``token`` matches, else None. Constant time per listed key."""
        offered = token.encode("utf-8", "surrogatepass")
        found: str | None = None
        for info in self.list():
            value = self.store.get(secret_name(info.key_id))
            if not value:
                continue
            # Every key is compared, match or not: no early exit.
            if hmac.compare_digest(offered, value.encode("utf-8")) and found is None:
                found = info.key_id
        return found
