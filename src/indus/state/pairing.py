"""The pairing store: one-time codes that let a stranger in once the owner approves (P15-09).

It lives in the state layer, below both the gateway (which issues codes and checks approvals)
and the CLI (``indus pairing``), so neither reaches into the other (lead rule, P15 wave 1).

A stranger who DMs the bot on a platform in ``pair`` mode gets a code; the owner approves it with
``indus pairing approve <platform> <code>`` and the stranger is let in from then on.

- Codes: 8 characters from a 32-letter alphabet without 0/O/1/I, drawn with ``secrets``. Only a
  salted SHA-256 (16-byte salt) is stored, under a random 16-hex request id; a code is never
  logged and cannot be read back.
- A code lives 3,600 s; at most 3 are pending per platform; a user may ask once per 600 s.
- 5 failed code approvals lock the platform for 3,600 s; a success resets the streak. The lockout
  is checked before any lookup, so a correct code is refused while it lasts.
- Typed codes lose all whitespace and are upper-cased (chat clients add spacing); the compare is
  constant-time.
- An admin approving by request id never sees the code, is not gated by the lockout and does not
  count toward it.
- Approval adds the user to the platform's allowlist when one is configured, so the operator's
  list stays the visible truth; revoke removes it there and tells live adapters
  (``add_revoke_listener``) to forget the user. An empty allowlist is never created: that would
  flip unknown DMs to ``ignore`` (R06.8).
- Files are 0600 in a 0700 directory resolved from the scope on every call, never at import
  (R06.9): a code the gateway issued is visible to the CLI that approves it.
- A user refused a code (rate limit, full queue, lockout) is told "try later" once, then silence.
- Decline stamps for the ``decline`` unknown-sender mode (P15-08) live here: one per sender per
  24 h, written before the reply is sent.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import threading
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from indus.foundation import envfile, fsio
from indus.foundation.envload import env_file_path
from indus.foundation.scope import Scope, current_scope

__all__ = [
    "ALPHABET",
    "CODE_LENGTH",
    "CODE_TTL_SECONDS",
    "DECLINE_WINDOW_SECONDS",
    "LOCKOUT_SECONDS",
    "MAX_FAILED_APPROVALS",
    "MAX_PENDING_PER_PLATFORM",
    "REQUEST_INTERVAL_SECONDS",
    "ApprovedUser",
    "Approval",
    "CodeRequest",
    "EnvAllowlist",
    "PairingStore",
    "PendingRequest",
    "add_revoke_listener",
    "allowlist_env_name",
    "looks_like_request_id",
    "normalise_code",
    "pairing_dir",
]

log = logging.getLogger(__name__)

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
CODE_TTL_SECONDS = 3600
MAX_PENDING_PER_PLATFORM = 3
REQUEST_INTERVAL_SECONDS = 600
MAX_FAILED_APPROVALS = 5
LOCKOUT_SECONDS = 3600
DECLINE_WINDOW_SECONDS = 24 * 3600
SALT_BYTES = 16
LOCK_TIMEOUT = 10.0

_PLATFORM_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_REQUEST_ID_RE = re.compile(r"^[0-9a-fA-F]{16}$")
_LIMITS = "_limits.json"
_DECLINED = "_declined.json"

type RequestStatus = Literal["issued", "rate_limited", "pending_full", "locked_out"]
type ApprovalStatus = Literal["approved", "invalid", "locked_out"]


def allowlist_env_name(platform: str) -> str:
    """``INDUS_<PLATFORM>_ALLOWED_USERS`` (the gateway registry's ``policy_env_name`` rule)."""
    return f"INDUS_{platform.upper()}_ALLOWED_USERS"


def pairing_dir(scope: Scope) -> Path:
    """The pairing directory of ``scope``'s profile."""
    return scope.home / "platforms" / "pairing"


def normalise_code(value: str) -> str:
    """What the user typed, as a code: every whitespace removed, upper-cased."""
    return "".join(str(value or "").split()).upper()


def looks_like_request_id(value: str) -> bool:
    """16 hex characters: a request id from ``indus pairing list`` (codes are 8 letters)."""
    return bool(_REQUEST_ID_RE.match(str(value or "").strip()))


def _check_platform(platform: str) -> str:
    name = str(platform or "").strip().lower()
    if not _PLATFORM_RE.match(name):
        raise ValueError(f"{platform!r} is not a platform name")
    return name


def _as_dict(value: object) -> dict[str, Any]:
    """``value`` when it is a JSON object (string keys), else an empty dict."""
    if not isinstance(value, dict):
        return {}
    items = cast(dict[object, Any], value).items()
    return {k: v for k, v in items if isinstance(k, str)}


def _hash(code: str, salt: bytes) -> str:
    return hashlib.sha256(salt + code.encode("utf-8")).hexdigest()


# -- live adapters' allowlists ------------------------------------------------------------------

type RevokeListener = Callable[[str, str], object]
_revoke_listeners: list[RevokeListener] = []
_listeners_lock = threading.Lock()


def add_revoke_listener(listener: RevokeListener) -> Callable[[], None]:
    """Call ``listener(platform, user_id)`` after each revoke (adapters drop in-memory copies).

    Returns a function that removes the listener.
    """
    with _listeners_lock:
        _revoke_listeners.append(listener)

    def remove() -> None:
        with _listeners_lock, contextlib.suppress(ValueError):
            _revoke_listeners.remove(listener)

    return remove


def _notify_revoked(platform: str, user_id: str) -> None:
    with _listeners_lock:
        listeners = list(_revoke_listeners)
    for listener in listeners:
        try:
            listener(platform, user_id)
        except Exception as exc:
            log.warning("a revoke listener failed for %s: %s", platform, exc)


# -- results ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CodeRequest:
    status: RequestStatus
    code: str | None = None
    request_id: str | None = None
    # True when the caller should tell the user to try later (the first refusal only).
    notify: bool = False


@dataclass(frozen=True)
class Approval:
    status: ApprovalStatus
    user_id: str = ""
    user_name: str = ""
    lockout_seconds: float = 0.0

    @property
    def approved(self) -> bool:
        return self.status == "approved"


@dataclass(frozen=True)
class PendingRequest:
    platform: str
    request_id: str
    user_id: str
    user_name: str
    age_seconds: float


@dataclass(frozen=True)
class ApprovedUser:
    platform: str
    user_id: str
    user_name: str
    approved_at: float


# -- allowlist mirror ---------------------------------------------------------------------------


class EnvAllowlist:
    """Mirrors approvals into the profile's ``INDUS_<PLATFORM>_ALLOWED_USERS`` when it is set."""

    def __init__(self, env_name_for: Callable[[str], str | None] | None = None) -> None:
        self._env_name_for = env_name_for or allowlist_env_name

    def _current(self, scope: Scope, name: str) -> tuple[Path, list[str]]:
        path = env_file_path(scope.home)
        raw = envfile.read_env_file(path).get(name)
        if raw is None:
            raw = scope.env.get(name, "")
        return path, [part.strip() for part in raw.split(",") if part.strip()]

    def add(self, scope: Scope, platform: str, user_id: str) -> bool:
        name = self._env_name_for(platform)
        if not name:
            return False
        path, ids = self._current(scope, name)
        if not ids or "*" in ids or user_id in ids:
            return False
        envfile.set_value(path, name, ",".join([*ids, user_id]))
        return True

    def remove(self, scope: Scope, platform: str, user_id: str) -> bool:
        name = self._env_name_for(platform)
        if not name:
            return False
        path, ids = self._current(scope, name)
        kept = [i for i in ids if i != user_id]
        if len(kept) == len(ids):
            return False
        envfile.set_value(path, name, ",".join(kept) if kept else None)
        return True


# -- the store ----------------------------------------------------------------------------------

_dir_locks: dict[str, threading.RLock] = {}
_dir_locks_guard = threading.Lock()


def _thread_lock(directory: Path) -> threading.RLock:
    with _dir_locks_guard:
        return _dir_locks.setdefault(str(directory), threading.RLock())


class PairingStore:
    """Pairing codes, approvals, limits and decline stamps of one profile.

    With ``scope=None`` every call resolves the current scope (``current_scope()``) afresh.
    """

    def __init__(
        self, scope: Scope | None = None, *, allowlist: EnvAllowlist | None = None
    ) -> None:
        self._scope = scope
        self._allowlist = allowlist if allowlist is not None else EnvAllowlist()
        self._warned_unreadable: set[str] = set()

    # -- plumbing ---------------------------------------------------------------------------------

    def scope(self) -> Scope:
        return self._scope if self._scope is not None else current_scope()

    def directory(self) -> Path:
        return pairing_dir(self.scope())

    def _now(self) -> float:
        return self.scope().clock.now().timestamp()

    def _ready_dir(self) -> Path:
        directory = self.directory()
        directory.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(directory, 0o700)
            os.chmod(directory.parent, 0o700)
        return directory

    @contextlib.contextmanager
    def _locked(self) -> Generator[Path]:
        """Cross-process file lock first, then the thread lock (gateway and CLI share files)."""
        directory = self._ready_dir()
        with fsio.FileLock(directory / ".lock", timeout=LOCK_TIMEOUT), _thread_lock(directory):
            yield directory

    def _load(self, path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return {}
        except PermissionError as exc:
            if str(path) not in self._warned_unreadable:
                self._warned_unreadable.add(str(path))
                log.warning(
                    "pairing file %s is not readable by this user (%s); fix its owner or run "
                    "the command as the gateway's user",
                    path,
                    exc,
                )
            return {}
        except (OSError, ValueError):
            return {}
        return _as_dict(data)

    def _save(self, path: Path, data: Mapping[str, Any]) -> None:
        fsio.atomic_write_json(path, dict(data), mode=0o600)

    @staticmethod
    def _pending_path(directory: Path, platform: str) -> Path:
        return directory / f"{platform}-pending.json"

    @staticmethod
    def _approved_path(directory: Path, platform: str) -> Path:
        return directory / f"{platform}-approved.json"

    def _live_pending(self, directory: Path, platform: str, now: float) -> dict[str, Any]:
        """Pending rows still inside their lifetime; expired or malformed rows are dropped."""
        path = self._pending_path(directory, platform)
        pending = self._load(path)
        live: dict[str, Any] = {}
        for rid, raw in pending.items():
            row = _as_dict(raw)
            created = row.get("created_at")
            if (
                isinstance(created, int | float)
                and isinstance(row.get("hash"), str)
                and isinstance(row.get("salt"), str)
                and now - float(created) <= CODE_TTL_SECONDS
            ):
                live[rid] = row
        if len(live) != len(pending):
            self._save(path, live)
        return live

    # -- limits -----------------------------------------------------------------------------------

    def _limits(self, directory: Path) -> dict[str, Any]:
        return self._load(directory / _LIMITS)

    def _save_limits(self, directory: Path, limits: Mapping[str, Any]) -> None:
        self._save(directory / _LIMITS, limits)

    @staticmethod
    def _number(data: Mapping[str, Any], key: str) -> float:
        value = data.get(key)
        return (
            float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0
        )

    def _lockout_left(self, limits: Mapping[str, Any], platform: str, now: float) -> float:
        return max(0.0, self._number(limits, f"lockout:{platform}") - now)

    def lockout_remaining(self, platform: str) -> float:
        """Seconds until the platform's approval lockout ends (0 when not locked out)."""
        platform = _check_platform(platform)
        directory = self._ready_dir()
        return self._lockout_left(self._limits(directory), platform, self._now())

    def _failed(self, directory: Path, platform: str, now: float) -> None:
        limits = self._limits(directory)
        key = f"failures:{platform}"
        count = int(self._number(limits, key)) + 1
        if count >= MAX_FAILED_APPROVALS:
            limits[f"lockout:{platform}"] = now + LOCKOUT_SECONDS
            limits[key] = 0
            log.warning(
                "pairing approvals for %s are locked for %d s after %d wrong codes",
                platform,
                LOCKOUT_SECONDS,
                MAX_FAILED_APPROVALS,
            )
        else:
            limits[key] = count
        self._save_limits(directory, limits)

    def _reset_failures(self, directory: Path, platform: str) -> None:
        limits = self._limits(directory)
        if limits.get(f"failures:{platform}"):
            limits[f"failures:{platform}"] = 0
            self._save_limits(directory, limits)

    # -- codes ------------------------------------------------------------------------------------

    def request_code(self, platform: str, user_id: str, user_name: str = "") -> CodeRequest:
        """Issue a code for ``user_id``, or say why not (and whether to tell them once)."""
        platform = _check_platform(platform)
        user = str(user_id).strip()
        if not user:
            raise ValueError("a pairing code needs a user id")
        with self._locked() as directory:
            now = self._now()
            pending = self._live_pending(directory, platform, now)
            limits = self._limits(directory)
            asked_key = f"asked:{platform}:{user}"
            told_key = f"told:{platform}:{user}"
            status: RequestStatus | None = None
            if self._lockout_left(limits, platform, now) > 0:
                status = "locked_out"
            elif now - self._number(limits, asked_key) < REQUEST_INTERVAL_SECONDS:
                status = "rate_limited"
            elif len(pending) >= MAX_PENDING_PER_PLATFORM:
                status = "pending_full"
            if status is not None:
                # Tell them once per refusal spell. A spell ends with their next issued code, or
                # after the longest refusal (an hour: lockout length and code lifetime).
                told = self._number(limits, told_key)
                asked = self._number(limits, asked_key)
                notify = told <= 0 or told < asked or now - told >= CODE_TTL_SECONDS
                if notify:
                    limits[told_key] = now
                    self._save_limits(directory, limits)
                return CodeRequest(status=status, notify=notify)
            code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
            salt = secrets.token_bytes(SALT_BYTES)
            request_id = secrets.token_hex(8)
            pending[request_id] = {
                "hash": _hash(code, salt),
                "salt": salt.hex(),
                "user_id": user,
                "user_name": str(user_name or ""),
                "created_at": now,
            }
            self._save(self._pending_path(directory, platform), pending)
            limits[asked_key] = now
            self._save_limits(directory, limits)
            return CodeRequest(status="issued", code=code, request_id=request_id)

    def approve(self, platform: str, value: str) -> Approval:
        """Approve by request id (16 hex) or by the code the user received."""
        if looks_like_request_id(value):
            return self.approve_request(platform, value)
        return self.approve_code(platform, value)

    def approve_code(self, platform: str, code: str) -> Approval:
        platform = _check_platform(platform)
        typed = normalise_code(code)
        with self._locked() as directory:
            now = self._now()
            pending = self._live_pending(directory, platform, now)
            left = self._lockout_left(self._limits(directory), platform, now)
            if left > 0:  # before the lookup: a valid code must not slip past a lockout
                return Approval(status="locked_out", lockout_seconds=left)
            match: str | None = None
            for request_id, row in pending.items():
                try:
                    salt = bytes.fromhex(str(row["salt"]))
                except ValueError:
                    continue
                if secrets.compare_digest(_hash(typed, salt), str(row["hash"])):
                    match = request_id
            if match is None:
                self._failed(directory, platform, now)
                left = self._lockout_left(self._limits(directory), platform, now)
                if left > 0:
                    return Approval(status="locked_out", lockout_seconds=left)
                return Approval(status="invalid")
            self._reset_failures(directory, platform)
            return self._finish(directory, platform, pending, match, now)

    def approve_request(self, platform: str, request_id: str) -> Approval:
        """Approve by request id: not gated by the lockout and never counted toward it."""
        platform = _check_platform(platform)
        wanted = str(request_id or "").strip().lower()
        with self._locked() as directory:
            now = self._now()
            pending = self._live_pending(directory, platform, now)
            for rid in pending:
                if wanted and secrets.compare_digest(rid.lower(), wanted):
                    return self._finish(directory, platform, pending, rid, now)
            return Approval(status="invalid")

    def _finish(
        self,
        directory: Path,
        platform: str,
        pending: dict[str, Any],
        request_id: str,
        now: float,
    ) -> Approval:
        row = pending.pop(request_id)
        self._save(self._pending_path(directory, platform), pending)
        user_id = str(row.get("user_id") or "")
        user_name = str(row.get("user_name") or "")
        path = self._approved_path(directory, platform)
        approved = self._load(path)
        approved[user_id] = {"user_name": user_name, "approved_at": now}
        self._save(path, approved)
        try:
            self._allowlist.add(self.scope(), platform, user_id)
        except Exception as exc:  # the pairing grant still authorizes on its own
            log.warning("could not add the approved user to the %s allowlist: %s", platform, exc)
        return Approval(status="approved", user_id=user_id, user_name=user_name)

    # -- approvals --------------------------------------------------------------------------------

    def is_approved(self, platform: str, user_id: str) -> bool:
        try:
            platform = _check_platform(platform)
        except ValueError:
            return False
        user = str(user_id or "").strip()
        if not user:
            return False
        return user in self._load(self._approved_path(self.directory(), platform))

    def revoke(self, platform: str, user_id: str) -> bool:
        platform = _check_platform(platform)
        user = str(user_id or "").strip()
        with self._locked() as directory:
            path = self._approved_path(directory, platform)
            approved = self._load(path)
            if user not in approved:
                return False
            del approved[user]
            self._save(path, approved)
            try:
                self._allowlist.remove(self.scope(), platform, user)
            except Exception as exc:
                log.warning("could not remove the user from the %s allowlist: %s", platform, exc)
        _notify_revoked(platform, user)
        return True

    def _platforms(self, directory: Path, suffix: str) -> list[str]:
        tail = f"-{suffix}.json"
        try:
            names = sorted(p.name for p in directory.iterdir() if p.name.endswith(tail))
        except OSError:
            return []
        found = [n[: -len(tail)] for n in names]
        return [p for p in found if _PLATFORM_RE.match(p)]

    def list_pending(self, platform: str | None = None) -> list[PendingRequest]:
        """Pending requests (codes are never part of the listing)."""
        with self._locked() as directory:
            now = self._now()
            names = (
                [_check_platform(platform)] if platform else self._platforms(directory, "pending")
            )
            out: list[PendingRequest] = []
            for name in names:
                for rid, row in self._live_pending(directory, name, now).items():
                    out.append(
                        PendingRequest(
                            platform=name,
                            request_id=rid,
                            user_id=str(row.get("user_id") or ""),
                            user_name=str(row.get("user_name") or ""),
                            age_seconds=max(0.0, now - float(row["created_at"])),
                        )
                    )
            return out

    def list_approved(self, platform: str | None = None) -> list[ApprovedUser]:
        directory = self._ready_dir()
        names = [_check_platform(platform)] if platform else self._platforms(directory, "approved")
        out: list[ApprovedUser] = []
        for name in names:
            for user_id, info in self._load(self._approved_path(directory, name)).items():
                row = _as_dict(info)
                out.append(
                    ApprovedUser(
                        platform=name,
                        user_id=str(user_id),
                        user_name=str(row.get("user_name") or ""),
                        approved_at=self._number(row, "approved_at"),
                    )
                )
        return out

    def clear_pending(self, platform: str | None = None) -> int:
        with self._locked() as directory:
            names = (
                [_check_platform(platform)] if platform else self._platforms(directory, "pending")
            )
            count = 0
            for name in names:
                path = self._pending_path(directory, name)
                count += len(self._load(path))
                self._save(path, {})
            return count

    # -- decline stamps ---------------------------------------------------------------------------

    def has_recent_decline(self, platform: str, user_id: str) -> bool:
        platform = _check_platform(platform)
        stamps = self._load(self.directory() / _DECLINED)
        stamp = self._number(stamps, f"{platform}:{user_id}")
        return stamp > 0 and self._now() - stamp < DECLINE_WINDOW_SECONDS

    def record_decline(self, platform: str, user_id: str) -> None:
        """Stamp the sender (callers stamp before sending, so a failed send cannot repeat)."""
        platform = _check_platform(platform)
        with self._locked() as directory:
            now = self._now()
            path = directory / _DECLINED
            stamps = {
                key: value
                for key, value in self._load(path).items()
                if isinstance(value, int | float) and now - float(value) < DECLINE_WINDOW_SECONDS
            }
            stamps[f"{platform}:{user_id}"] = now
            self._save(path, stamps)
