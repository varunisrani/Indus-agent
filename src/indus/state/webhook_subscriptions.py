"""Webhook routes added with ``indus webhook subscribe`` (P15-32).

It lives in the state layer so the CLI writes and the gateway's receiver reads the same file
without importing each other (D-16.246).

The file is ``<home>/gateway/webhook_subscriptions.json`` (0600): ``{"version": 1, "routes":
{name: route}}``. A route never holds a secret value: ``secret`` is the *name* of a secret in the
profile's store (``secret_name_for(route)`` for generated ones). Writers take a file lock, re-read
and publish by atomic rename, so a reader (the receiver, on every request) always sees a whole
snapshot without locking; ``stamp()`` (mtime, size, inode) tells it when to read again. A
malformed or unreadable file reads as no routes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, cast

from indus.foundation import fsio
from indus.foundation.profiles import mkdir_in_profile
from indus.foundation.scope import Scope

__all__ = [
    "ROUTE_NAME_RE",
    "SECRET_PREFIX",
    "SUBSCRIPTIONS_FILE",
    "SubscriptionStore",
    "normalise_name",
    "secret_name_for",
    "sign_request",
]

SUBSCRIPTIONS_FILE = Path("gateway") / "webhook_subscriptions.json"
SECRET_PREFIX = "INDUS_WEBHOOK_"
ROUTE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
LOCK_TIMEOUT = 10.0
_FORMAT = 1
_lock = threading.Lock()


def normalise_name(name: str) -> str:
    """Lower case, spaces to dashes; the caller checks the result with ``ROUTE_NAME_RE``."""
    return "-".join(name.strip().lower().split())


def secret_name_for(route: str) -> str:
    """The secret-store name a generated secret for ``route`` is kept under."""
    return SECRET_PREFIX + re.sub(r"[^A-Z0-9_]", "_", route.upper())


class SubscriptionStore:
    """One profile's subscribed webhook routes (see the module notes)."""

    def __init__(self, scope: Scope) -> None:
        self.scope = scope

    @property
    def path(self) -> Path:
        return self.scope.home / SUBSCRIPTIONS_FILE

    def stamp(self) -> tuple[int, int, int] | None:
        """Changes whenever a writer publishes a new snapshot; None when there is no file."""
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = self.path.read_bytes()
        except OSError:
            return {}
        try:
            data: object = json.loads(raw.decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        routes = cast(dict[str, Any], data).get("routes")
        if not isinstance(routes, dict):
            return {}
        return {
            str(name): dict(cast(dict[str, Any], route))
            for name, route in cast(dict[str, Any], routes).items()
            if isinstance(route, dict)
        }

    def _save(self, routes: dict[str, dict[str, Any]]) -> None:
        fsio.atomic_write_json(
            self.path, {"version": _FORMAT, "routes": routes}, mode=0o600, sort_keys=True
        )

    def _locked(self) -> fsio.FileLock:
        mkdir_in_profile(self.scope.home, SUBSCRIPTIONS_FILE.parent)
        return fsio.FileLock(self.path.with_suffix(".lock"), timeout=LOCK_TIMEOUT)

    def put(self, name: str, route: dict[str, Any]) -> bool:
        """Add or replace ``name``; True when it was new."""
        if not ROUTE_NAME_RE.match(name):
            raise ValueError(f"invalid webhook route name {name!r}")
        with _lock, self._locked():
            routes = self.load()
            new = name not in routes
            routes[name] = dict(route)
            self._save(routes)
        return new

    def remove(self, name: str) -> dict[str, Any] | None:
        """Drop ``name``; returns the removed route, or None when there was none."""
        with _lock, self._locked():
            routes = self.load()
            gone = routes.pop(name, None)
            if gone is not None:
                self._save(routes)
        return gone


def _hex(secret: str, data: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), data, hashlib.sha256).hexdigest()


def sign_request(auth: str, secret: str, body: bytes, now: int) -> dict[str, str]:
    """The headers a sender puts on ``body`` for route auth ``auth`` (``auto`` signs with the
    timestamped scheme). The receiver checks the same names (``gateway.http.webhook``)."""
    if auth == "github":
        return {"X-Hub-Signature-256": "sha256=" + _hex(secret, body)}
    if auth == "gitlab":
        return {"X-Gitlab-Token": secret}
    if auth == "hmac":
        return {"X-Indus-Signature": "sha256=" + _hex(secret, body)}
    signed = str(now).encode() + b"." + body
    return {
        "X-Indus-Timestamp": str(now),
        "X-Indus-Signature-Ts": "sha256=" + _hex(secret, signed),
    }
