"""End-to-end encryption for the Matrix adapter (P16-07).

``create_end_to_end(adapter, client)`` is the factory the adapter's `EndToEnd` seam loads when
``platforms.matrix.e2ee`` is ``optional`` or ``required``. Olm and Megolm (the ratchets, the keys,
the ciphers) come from mautrix's crypto layer, which wraps libolm through python-olm: the
``matrix-e2ee`` extra, Linux only, imported lazily here and nowhere else. Everything around the
library is ours:

- **Store.** ``<profile home>/platforms/matrix/crypto``: the directory is 0700, the SQLite database
  and its journals 0600. One process at a time holds it (``fcntl.flock`` on ``store.lock``): a
  second gateway is refused with a retryable error, and ``indus send`` while the gateway runs gets
  no keys, so it refuses to write into an encrypted room (it never writes there in clear).
- **Trust.** Room keys go only to devices that are verified — signed by their owner's
  cross-signing key (``e2ee_trust: owner_signed``, the default) or by a user we verified
  (``e2ee_trust: verified``) — of users the Matrix allowlists name (or paired users, or anyone under
  allow-all). The rule is `KeyPolicy`; the library asks it about every device before it shares.
  When the set of allowed members of a room changes, the room's outbound session is rotated, so a
  user who left (or left the allowlist) never gets the next messages' key.
- **Cross-signing.** With ``MATRIX_RECOVERY_KEY`` in the profile's **secret store** (never env,
  config or a file) the bot's device is cross-signed from secret storage at start. The key is never
  logged, echoed in an error or put in a repr.
- **Undecryptable events** are never passed on. One whose key has not arrived waits a few syncs
  (keys travel as to-device events, often a moment after the message); if it still cannot be read,
  the room gets one short notice per Megolm session (`UNDECRYPTABLE_NOTICE`) and the event is
  dropped. Events from before the listener started and our own events never cause a notice.
- **Send-only** (``indus send``): the store is opened for sending only — no sync work, no key
  upload, no cross-signing; whether a room is encrypted is read from its state.

Without the extra (or on another OS) ``required`` fails the connect for good with
`INSTALL_HINT`; ``optional`` logs one warning and the adapter runs plain (encrypted rooms skipped).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from indus.foundation.blocking import run_blocking
from indus.foundation.redact import redact_text
from indus.gateway.adapters.base import AdapterConnectError
from indus.gateway.adapters.matrix.client import MatrixClient, MatrixError

if TYPE_CHECKING:
    from indus.gateway.adapters.matrix.adapter import MatrixAdapter

__all__ = [
    "INSTALL_HINT",
    "RECOVERY_KEY_NAME",
    "UNDECRYPTABLE_NOTICE",
    "VENDOR_NAMES",
    "Device",
    "E2eeUnavailable",
    "Engine",
    "EngineSetup",
    "KeyPolicy",
    "MatrixE2ee",
    "RoomBook",
    "StoreLock",
    "Trust",
    "Undecryptable",
    "create_end_to_end",
    "start_end_to_end",
    "store_dir",
]

log = logging.getLogger(__name__)

INSTALL_HINT = (
    "pip install 'indus-agent[matrix-e2ee]' (Linux only; python-olm builds libolm, which needs "
    "cmake and a C++ compiler)"
)
# The secret-store entry holding the account's recovery key (as Element shows it).
RECOVERY_KEY_NAME = "MATRIX_RECOVERY_KEY"
STORE_PARTS = ("platforms", "matrix", "crypto")
DB_NAME = "crypto.db"
LOCK_NAME = "store.lock"
UNDECRYPTABLE_NOTICE = (
    "I could not decrypt a message here: no key for it reached this bot's device. Verify the "
    "bot's device in your client, or send the message again."
)
# How many syncs an event without its key waits before it counts as undecryptable.
PARK_SYNCS = 3
PARKED_CAP = 200
NOTICED_CAP = 1_000
# Events sent more than this before the listener started never cause a notice (the backlog).
NOTICE_GRACE_MS = 5_000

# What we call on mautrix (``"module:Object"`` → attribute names); the extras lane checks that
# each exists in the pinned version (a library bump that renames one fails there).
VENDOR_NAMES: Mapping[str, tuple[str, ...]] = {
    "mautrix.crypto:OlmMachine": (
        "load",
        "share_keys",
        "handle_otk_count",
        "handle_device_lists",
        "handle_to_device_event",
        "handle_room_key_request",
        "decrypt_megolm_event",
        "encrypt_megolm_event",
        "share_group_session",
        "verify_with_recovery_key",
        "resolve_trust",
        "is_sharing_group_session",
        "wait_group_session_share",
    ),
    "mautrix.crypto.store.asyncpg:PgCryptoStore": (
        "upgrade_table",
        "open",
        "close",
        "delete",
        "get_device_id",
        "put_device_id",
        "remove_outbound_group_session",
    ),
    "mautrix.api:HTTPAPI": ("request",),
    "mautrix.crypto.attachments": ("encrypt_attachment", "decrypt_attachment"),
    "mautrix.util.async_db:Database": ("create",),
    "mautrix.types:TrustState": ("BLACKLISTED", "CROSS_SIGNED_TOFU", "CROSS_SIGNED_TRUSTED"),
}


class E2eeUnavailable(RuntimeError):
    """The encryption extra is not installed here (or this is not Linux)."""


class Undecryptable(Exception):
    """An encrypted event this device cannot read. ``missing_key``: the room key has not (yet)
    arrived, so a later sync may still bring it."""

    def __init__(self, session_id: str, reason: str, *, missing_key: bool = False) -> None:
        super().__init__(reason)
        self.session_id = session_id
        self.missing_key = missing_key


class Trust(IntEnum):
    """How far we trust one device (ours, mapped from the library's trust states)."""

    BLOCKED = -1
    UNVERIFIED = 0
    # Signed by its owner's self-signing key (the owner verified it; we trust the owner's key on
    # first use).
    OWNER_SIGNED = 1
    # Its owner is verified by us (our user-signing key), or the device was verified directly.
    VERIFIED = 2


@dataclass(frozen=True)
class Device:
    user_id: str
    device_id: str
    trust: Trust


@dataclass(frozen=True)
class KeyPolicy:
    """Which devices may receive room keys: verified ones (``min_trust``) of allowed users."""

    allowed: Callable[[str], Awaitable[bool]]
    min_trust: Trust = Trust.OWNER_SIGNED

    async def may_receive(self, device: Device) -> bool:
        if device.trust is Trust.BLOCKED or device.trust < self.min_trust:
            return False
        return await self.allowed(device.user_id)


@dataclass
class RoomBook:
    """What the encryption layer knows about rooms: encryption state and joined members."""

    encryption: dict[str, dict[str, Any] | None] = field(default_factory=dict[str, Any])
    members: dict[str, set[str]] = field(default_factory=dict[str, set[str]])

    def is_encrypted(self, room_id: str) -> bool | None:
        """True / False when known, None when the room's state has not been seen."""
        if room_id not in self.encryption:
            return None
        return self.encryption[room_id] is not None

    def set_encryption(self, room_id: str, content: Mapping[str, Any] | None) -> None:
        if content is not None or self.encryption.get(room_id) is None:
            self.encryption[room_id] = dict(content) if content is not None else None

    def shared_with(self, user_id: str) -> list[str]:
        """Encrypted rooms ``user_id`` is a member of (all encrypted rooms when unknown)."""
        rooms = [r for r, c in self.encryption.items() if c is not None]
        known = [r for r in rooms if user_id in self.members.get(r, set())]
        return known or rooms

    def absorb(self, sync: Mapping[str, Any]) -> set[str]:
        """Note encryption and membership state from one sync; the rooms whose members changed."""
        changed: set[str] = set()
        rooms = _mapping(_mapping(sync.get("rooms")).get("join"))
        for room_id, room in rooms.items():
            room = _mapping(room)
            events = [*_events(room.get("state")), *_events(room.get("timeline"))]
            for event in events:
                if "state_key" not in event:
                    continue
                kind = event.get("type")
                content = _mapping(event.get("content"))
                if kind == "m.room.encryption" and content.get("algorithm"):
                    self.set_encryption(str(room_id), content)
                elif kind == "m.room.member":
                    who = str(event.get("state_key") or "")
                    known = self.members.get(str(room_id))
                    if not who or known is None:
                        continue
                    joined = content.get("membership") == "join"
                    if joined and who not in known:
                        known.add(who)
                        changed.add(str(room_id))
                    elif not joined and who in known:
                        known.discard(who)
                        changed.add(str(room_id))
        for room_id in _mapping(_mapping(sync.get("rooms")).get("leave")):
            self.members.pop(str(room_id), None)
        return changed


class Engine(Protocol):
    """The Olm/Megolm library behind our rules (mautrix in production, a fake in tests)."""

    async def start(self, *, send_only: bool) -> None:
        """Open the store and load (or create) the device's account; unless ``send_only``, make
        sure the homeserver has our device keys and enough one-time keys."""
        ...

    async def absorb(self, sync: Mapping[str, Any]) -> None:
        """Device-list changes, to-device events (room keys) and one-time key counts of a sync."""
        ...

    async def decrypt(self, room_id: str, event: Mapping[str, Any]) -> dict[str, Any]:
        """``{"type", "content"}`` of an ``m.room.encrypted`` event; raises `Undecryptable`."""
        ...

    async def encrypt(
        self, room_id: str, event_type: str, content: Mapping[str, Any], users: Sequence[str]
    ) -> dict[str, Any]:
        """The ``m.room.encrypted`` content; shares a room key with ``users``' devices the
        `KeyPolicy` allows first when the room has no current outbound session."""
        ...

    async def rotate(self, room_id: str) -> None:
        """Drop the room's outbound session: the next message starts (and shares) a new one."""
        ...

    async def cross_sign(self, recovery_key: str) -> None:
        """Cross-sign this device with keys from secret storage, unlocked by ``recovery_key``."""
        ...

    async def encrypt_attachment(self, data: bytes) -> tuple[bytes, dict[str, Any]]: ...

    async def decrypt_attachment(self, data: bytes, info: Mapping[str, Any]) -> bytes: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class EngineSetup:
    client: MatrixClient
    user_id: str
    device_id: str
    store: Path
    policy: KeyPolicy
    rooms: RoomBook


type EngineFactory = Callable[[EngineSetup], Engine]


# -- the store ------------------------------------------------------------------------------------


def store_dir(home: Path) -> Path:
    return home.joinpath(*STORE_PARTS)


def _prepare_store(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    for name in (DB_NAME, LOCK_NAME):
        path = directory / name
        # Created 0600 before SQLite opens it (its journals copy the database's mode).
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
    _tighten(directory)


def _tighten(directory: Path) -> None:
    with contextlib.suppress(OSError):
        os.chmod(directory, 0o700)
    for entry in directory.iterdir():
        if entry.is_file() and not entry.is_symlink():
            with contextlib.suppress(OSError):
                os.chmod(entry, 0o600)


def _lock_byte(fd: int, *, acquire: bool) -> None:
    """Take (non-blocking) or drop the store lock: ``flock``, or one ``msvcrt.locking`` byte at
    offset 0 on Windows. Raises OSError when another process holds it."""
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK if acquire else msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(fd, (fcntl.LOCK_EX | fcntl.LOCK_NB) if acquire else fcntl.LOCK_UN)


class StoreLock:
    """One process at a time per device store (an exclusive ``flock``; released on close)."""

    def __init__(self, directory: Path) -> None:
        self.path = directory / LOCK_NAME
        self._fd: int | None = None

    def acquire(self) -> bool:
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            _lock_byte(fd, acquire=True)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        with contextlib.suppress(OSError):
            _lock_byte(fd, acquire=False)
        os.close(fd)

    @property
    def held(self) -> bool:
        return self._fd is not None


# -- our layer -------------------------------------------------------------------------------------


@dataclass
class _Parked:
    room_id: str
    event: dict[str, Any]
    syncs_left: int


class MatrixE2ee:
    """The adapter's `EndToEnd`: our rules around an `Engine` (see the module notes)."""

    def __init__(
        self,
        adapter: MatrixAdapter,
        client: MatrixClient,
        engine: Engine | None,
        *,
        rooms: RoomBook,
        policy: KeyPolicy,
        lock: StoreLock | None,
        directory: Path,
        send_only: bool,
        started_ms: int,
        locked_out: bool = False,
    ) -> None:
        self._adapter = adapter
        self._client = client
        self._engine = engine
        self.rooms = rooms
        self.policy = policy
        self._lock = lock
        self.directory = directory
        self.send_only = send_only
        self._started_ms = started_ms
        # ``indus send`` while the gateway holds the store: no keys, so encrypted rooms refuse.
        self.locked_out = locked_out
        self._shared_to: dict[str, frozenset[str]] = {}
        self._noticed: OrderedDict[str, None] = OrderedDict()
        self._parked: list[_Parked] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._room_locks: dict[str, asyncio.Lock] = {}
        self.notices_sent = 0

    def __repr__(self) -> str:
        state = "locked out" if self.locked_out else ("send-only" if self.send_only else "on")
        return f"<MatrixE2ee {state}>"

    # -- inbound ---------------------------------------------------------------------------------

    async def absorb_sync(self, sync: Mapping[str, Any]) -> None:
        changed = self.rooms.absorb(sync)
        engine = self._engine
        if engine is None:
            return
        for room_id in changed:
            await self._rotate(room_id)
        await engine.absorb(sync)
        await self._retry_parked()

    async def decrypt(self, room_id: str, event: Mapping[str, Any]) -> dict[str, Any] | None:
        content = _mapping(event.get("content"))
        if self.rooms.is_encrypted(room_id) is not True:
            # An encrypted event proves the room is encrypted even before its state is seen.
            algorithm = str(content.get("algorithm") or "m.megolm.v1.aes-sha2")
            self.rooms.set_encryption(room_id, {"algorithm": algorithm})
        engine = self._engine
        if engine is None:
            return None
        try:
            return await engine.decrypt(room_id, event)
        except Undecryptable as exc:
            if exc.missing_key and len(self._parked) < PARKED_CAP:
                self._parked.append(_Parked(room_id, dict(event), PARK_SYNCS))
                return None
            self._undecryptable(room_id, event, exc)
            return None

    async def _retry_parked(self) -> None:
        engine = self._engine
        if engine is None or not self._parked:
            return
        waiting, self._parked = self._parked, []
        for parked in waiting:
            try:
                clear = await engine.decrypt(parked.room_id, parked.event)
            except Undecryptable as exc:
                parked.syncs_left -= 1
                if exc.missing_key and parked.syncs_left > 0:
                    self._parked.append(parked)
                else:
                    self._undecryptable(parked.room_id, parked.event, exc)
                continue
            except Exception as exc:
                log.warning("matrix: a delayed decryption failed: %s", type(exc).__name__)
                continue
            merged = dict(parked.event)
            merged.update(clear)
            await self._adapter.take_late_event(parked.room_id, merged)

    def _undecryptable(self, room_id: str, event: Mapping[str, Any], exc: Undecryptable) -> None:
        sender = str(event.get("sender") or "")
        sent = event.get("origin_server_ts")
        log.warning(
            "matrix: an encrypted event in %s could not be decrypted (%s)", room_id, str(exc)[:120]
        )
        if not sender or sender.lower() == self._adapter.user_id.lower():
            return
        if isinstance(sent, int | float) and sent < self._started_ms - NOTICE_GRACE_MS:
            return
        key = f"{room_id}\n{exc.session_id or event.get('event_id') or ''}"
        if key in self._noticed:
            return
        self._noticed[key] = None
        while len(self._noticed) > NOTICED_CAP:
            self._noticed.popitem(last=False)
        self.notices_sent += 1
        task = asyncio.ensure_future(self._adapter.send(room_id, UNDECRYPTABLE_NOTICE))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- outbound ---------------------------------------------------------------------------------

    async def is_encrypted(self, room_id: str) -> bool:
        known = self.rooms.is_encrypted(room_id)
        if known is not None:
            return known
        try:
            content = await self._client.room_state(room_id, "m.room.encryption")
        except MatrixError as exc:
            # Only "no such state event" means a plain room; anything else (an unknown endpoint,
            # a refusal, a timeout) fails the send rather than risk a message in clear.
            if exc.errcode == "M_NOT_FOUND":
                self.rooms.set_encryption(room_id, None)
                return False
            raise
        self.rooms.set_encryption(room_id, content if content.get("algorithm") else None)
        return bool(content.get("algorithm"))

    async def encrypt(
        self, room_id: str, event_type: str, content: Mapping[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        engine = self._engine
        if engine is None:
            raise MatrixError(
                "forbidden: this room is end-to-end encrypted and the running gateway holds this "
                "device's keys; send through the gateway"
                if self.locked_out
                else "forbidden: end-to-end encryption is not running"
            )
        lock = self._room_locks.setdefault(room_id, asyncio.Lock())
        async with lock:
            users = await self._recipients(room_id)
            previous = self._shared_to.get(room_id)
            if previous is not None and previous != users:
                await self._rotate(room_id)
            self._shared_to[room_id] = users
            sealed = await engine.encrypt(room_id, event_type, content, sorted(users))
        return "m.room.encrypted", sealed

    async def _recipients(self, room_id: str) -> frozenset[str]:
        members = self.rooms.members.get(room_id)
        if members is None:
            members = set(await self._client.joined_members(room_id))
            self.rooms.members[room_id] = members
        own = self._adapter.user_id
        allowed = {own} if own else set()
        for user in members:
            if user != own and await self.policy.allowed(user):
                allowed.add(user)
        return frozenset(allowed)

    async def _rotate(self, room_id: str) -> None:
        engine = self._engine
        if engine is not None:
            await engine.rotate(room_id)
        self._shared_to.pop(room_id, None)

    async def encrypt_attachment(self, data: bytes) -> tuple[bytes, dict[str, Any]]:
        engine = self._engine
        if engine is None:
            raise MatrixError("forbidden: end-to-end encryption is not running")
        return await engine.encrypt_attachment(data)

    async def decrypt_attachment(self, data: bytes, info: Mapping[str, Any]) -> bytes:
        engine = self._engine
        if engine is None:
            raise MatrixError("end-to-end encryption is not running")
        return await engine.decrypt_attachment(data, info)

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        engine, self._engine = self._engine, None
        try:
            if engine is not None:
                await engine.close()
        finally:
            if self._lock is not None:
                self._lock.release()
            with contextlib.suppress(OSError):
                await run_blocking(lambda: _tighten(self.directory), label="matrix crypto store")


# -- the factory -----------------------------------------------------------------------------------


def _policy_for(adapter: MatrixAdapter) -> KeyPolicy:
    options = adapter.options

    async def allowed(user_id: str) -> bool:
        if not user_id:
            return False
        if user_id == adapter.user_id or options.anyone_may_invite or user_id in options.inviters:
            return True
        scope = adapter.scope
        if scope is None:
            return False
        from indus.state.pairing import PairingStore

        store = PairingStore(scope)
        try:
            return await run_blocking(
                lambda: store.is_approved(adapter.platform, user_id), label="matrix key policy"
            )
        except Exception:
            return False

    minimum = Trust.VERIFIED if options.e2ee_trust == "verified" else Trust.OWNER_SIGNED
    return KeyPolicy(allowed, minimum)


def _recovery_key(adapter: MatrixAdapter) -> str:
    """``MATRIX_RECOVERY_KEY`` from the profile's secret store only (never env or config)."""
    scope = adapter.scope
    secrets = scope.secrets if scope is not None else None
    if secrets is None:
        return ""
    try:
        value = secrets.get(RECOVERY_KEY_NAME)
    except Exception as exc:
        log.warning("matrix: the secret store could not be read (%s)", type(exc).__name__)
        return ""
    return (value or "").strip()


async def create_end_to_end(adapter: MatrixAdapter, client: MatrixClient) -> MatrixE2ee:
    """The adapter's encryption layer over mautrix (the ``EndToEnd`` seam's factory)."""
    return await start_end_to_end(adapter, client)


async def start_end_to_end(
    adapter: MatrixAdapter,
    client: MatrixClient,
    *,
    engine_factory: EngineFactory | None = None,
) -> MatrixE2ee:
    """Build and start the encryption layer (``engine_factory`` replaces mautrix in tests)."""
    required = adapter.options.e2ee == "required"
    try:
        factory = engine_factory or _mautrix_factory()
    except E2eeUnavailable as exc:
        if required:
            raise AdapterConnectError(
                f"end-to-end encryption is not available: {exc}; install it with {INSTALL_HINT} "
                "or set platforms.matrix.e2ee to off or optional",
                retryable=False,
                code="e2ee_unavailable",
            ) from None
        raise E2eeUnavailable(f"{exc}; install it with {INSTALL_HINT}") from None
    scope = adapter.scope
    if scope is None:
        raise E2eeUnavailable("no profile home to keep the device store in")
    if not adapter.user_id or not adapter.device_id:
        raise E2eeUnavailable("the access token's device is unknown (whoami gave no device id)")
    directory = store_dir(scope.home)
    await run_blocking(lambda: _prepare_store(directory), label="matrix crypto store")
    lock = StoreLock(directory)
    rooms = RoomBook()
    policy = _policy_for(adapter)
    started_ms = int(adapter.clock.now().timestamp() * 1000)
    acquired = await run_blocking(lock.acquire, label="matrix crypto store")
    if not acquired:
        if adapter.send_only:
            log.warning(
                "matrix: the running gateway holds this device's keys; encrypted rooms are not "
                "reachable from this send"
            )
            return MatrixE2ee(
                adapter,
                client,
                None,
                rooms=rooms,
                policy=policy,
                lock=None,
                directory=directory,
                send_only=True,
                started_ms=started_ms,
                locked_out=True,
            )
        raise AdapterConnectError(
            "another Indus process is using this Matrix device's encryption store "
            f"({directory}); retrying",
            retryable=True,
            code="e2ee_store_busy",
        )
    engine: Engine | None = None
    try:
        engine = factory(
            EngineSetup(client, adapter.user_id, adapter.device_id, directory, policy, rooms)
        )
        await engine.start(send_only=adapter.send_only)
        if not adapter.send_only:
            await _cross_sign(adapter, engine)
        await run_blocking(lambda: _tighten(directory), label="matrix crypto store")
    except BaseException:
        if engine is not None:
            with contextlib.suppress(Exception):
                await engine.close()
        lock.release()
        raise
    log.info(
        "matrix: end-to-end encryption on for device %s (%s)",
        adapter.device_id,
        "send-only" if adapter.send_only else "store " + str(directory),
    )
    return MatrixE2ee(
        adapter,
        client,
        engine,
        rooms=rooms,
        policy=policy,
        lock=lock,
        directory=directory,
        send_only=adapter.send_only,
        started_ms=started_ms,
    )


async def _cross_sign(adapter: MatrixAdapter, engine: Engine) -> None:
    key = _recovery_key(adapter)
    if not key:
        log.info(
            "matrix: no %s in the secret store; this device is not cross-signed (clients show it "
            "as unverified)",
            RECOVERY_KEY_NAME,
        )
        return
    try:
        await engine.cross_sign(key)
    except Exception as exc:
        text = redact_text(f"{type(exc).__name__}: {exc}", (key, key.replace(" ", "")))
        log.warning("matrix: cross-signing with the recovery key failed: %s", text[:200])
        return
    log.info("matrix: this device is cross-signed with the account's recovery key")


# -- the mautrix engine ----------------------------------------------------------------------------


def _mautrix_factory() -> EngineFactory:
    lib = _load_mautrix()
    return lambda setup: MautrixEngine(setup, lib)


def _load_mautrix() -> Any:
    """The mautrix pieces we use, imported now (the extra is optional and Linux only)."""
    if not sys.platform.startswith("linux"):
        raise E2eeUnavailable("Matrix end-to-end encryption is supported on Linux only")
    try:
        import aiosqlite  # pyright: ignore[reportMissingImports, reportUnusedImport]  # noqa: F401
        import olm  # pyright: ignore[reportMissingImports, reportUnusedImport]  # noqa: F401
        from mautrix import api as m_api  # pyright: ignore[reportMissingImports]
        from mautrix import client as m_client  # pyright: ignore[reportMissingImports]
        from mautrix import errors as m_errors  # pyright: ignore[reportMissingImports]
        from mautrix import types as m_types  # pyright: ignore[reportMissingImports]
        from mautrix.crypto import (  # pyright: ignore[reportMissingImports]
            OlmMachine,
            StateStore,
            attachments,
        )
        from mautrix.crypto.store.asyncpg import (  # pyright: ignore[reportMissingImports]
            PgCryptoStore,
        )
        from mautrix.util.async_db import Database  # pyright: ignore[reportMissingImports]
    except ImportError as exc:
        missing = getattr(exc, "name", None) or "a library"
        raise E2eeUnavailable(
            f"the matrix-e2ee extra is not installed ({missing} is missing)"
        ) from None
    lib: dict[str, Any] = {
        "api": m_api,
        "client": m_client,
        "errors": m_errors,
        "types": m_types,
        "OlmMachine": OlmMachine,
        "StateStore": StateStore,
        "attachments": attachments,
        "PgCryptoStore": PgCryptoStore,
        "Database": Database,
    }
    return type("MautrixLib", (), lib)


_NO_SESSION = object()


def _bridged_api(lib: Any, client: MatrixClient) -> Any:
    """mautrix's request sender, sending every request through our `MatrixClient` (one HTTP
    stack: our token handling, no redirects, the same wire fake in tests)."""
    base: Any = lib.api.HTTPAPI
    errors: Any = lib.errors

    class BridgedAPI(base):  # pyright: ignore[reportUntypedBaseClass]
        def __init__(self) -> None:
            super().__init__(
                client.homeserver,
                "",
                client_session=_NO_SESSION,
                default_retry_count=0,
                log=logging.getLogger("indus.matrix.crypto.http"),
            )

        async def request(  # pyright: ignore[reportIncompatibleMethodOverride]
            self,
            method: Any,
            path: Any,
            content: Any = None,
            headers: Any = None,
            query_params: Any = None,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            verb = str(getattr(method, "value", method)).upper()
            body: Any = content
            if isinstance(body, bytes | bytearray | str):
                body = json.loads(body) if body else None
            params = {
                str(k): str(v)
                for k, v in dict(query_params or {}).items()  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
                if v is not None
            }
            full = "/" + str(path).lstrip("/")
            try:
                return await client.request(
                    verb, full, body=None if verb == "GET" else (body or {}), params=params
                )
            except MatrixError as exc:
                raise errors.make_request_error(
                    http_status=exc.status or 500,
                    text=str(exc),
                    errcode=exc.errcode or None,
                    message=exc.detail or exc.reading,
                ) from None

    return BridgedAPI()


def _state_store(lib: Any, rooms: RoomBook) -> Any:
    base: Any = lib.StateStore
    content_type: Any = lib.types.RoomEncryptionStateEventContent

    class RoomState(base):  # pyright: ignore[reportUntypedBaseClass]
        async def is_encrypted(self, room_id: str) -> bool:
            return rooms.is_encrypted(room_id) is True

        async def get_encryption_info(self, room_id: str) -> Any:
            content = rooms.encryption.get(room_id)
            return content_type.deserialize(content) if content else None

        async def find_shared_rooms(self, user_id: str) -> list[str]:
            return rooms.shared_with(user_id)

    return RoomState()


class MautrixEngine:
    """Olm/Megolm through mautrix's ``OlmMachine`` and its SQLite crypto store."""

    def __init__(self, setup: EngineSetup, lib: Any) -> None:
        self.setup = setup
        self.lib = lib
        self._db: Any = None
        self._store: Any = None
        self.machine: Any = None

    def __repr__(self) -> str:
        return f"<MautrixEngine {self.setup.device_id}>"

    async def start(self, *, send_only: bool) -> None:
        lib, setup = self.lib, self.setup
        path = setup.store / DB_NAME
        self._db = lib.Database.create(
            f"sqlite:///{path}", upgrade_table=lib.PgCryptoStore.upgrade_table
        )
        await self._db.start()
        store = lib.PgCryptoStore(
            account_id=setup.user_id,
            pickle_key=f"indus:{setup.user_id}:{setup.device_id}",
            db=self._db,
        )
        await store.open()
        stored = await store.get_device_id()
        if stored and stored != setup.device_id:
            log.warning(
                "matrix: the access token belongs to device %s, the store to %s; starting a new "
                "encryption identity for %s",
                setup.device_id,
                stored,
                setup.device_id,
            )
            await store.delete()
        await store.put_device_id(setup.device_id)
        self._store = store
        mclient = lib.client.Client(
            mxid=setup.user_id, device_id=setup.device_id, api=_bridged_api(lib, setup.client)
        )
        machine = lib.OlmMachine(mclient, store, _state_store(lib, setup.rooms))
        trust = lib.types.TrustState
        floor = (
            trust.CROSS_SIGNED_TRUSTED
            if setup.policy.min_trust is Trust.VERIFIED
            else trust.CROSS_SIGNED_TOFU
        )
        machine.send_keys_min_trust = floor
        machine.share_keys_min_trust = floor
        library_trust = machine.resolve_trust
        policy = setup.policy

        async def resolve_trust(device: Any, allow_fetch: bool = True) -> Any:
            found = await library_trust(device, allow_fetch)
            ours = Device(str(device.user_id), str(device.device_id), _ours(trust, found))
            if found >= floor and not await policy.may_receive(ours):
                return trust.UNVERIFIED
            return found

        machine.resolve_trust = resolve_trust
        await machine.load()
        self.machine = machine
        if not send_only:
            await self._check_server_keys()
            await machine.share_keys()

    async def _check_server_keys(self) -> None:
        """Our device keys must be the homeserver's: upload them when missing, refuse when the
        server holds other keys for this device (our store is then stale)."""
        setup, machine = self.setup, self.machine
        answer = await setup.client.request(
            "POST",
            "/_matrix/client/v3/keys/query",
            body={"device_keys": {setup.user_id: [setup.device_id]}},
        )
        devices = _mapping(_mapping(answer.get("device_keys")).get(setup.user_id))
        ours = _mapping(devices.get(setup.device_id))
        server_key = _mapping(ours.get("keys")).get(f"ed25519:{setup.device_id}")
        local_key = machine.account.signing_key
        if not server_key:
            machine.account.shared = False
            return
        if server_key != local_key:
            if machine.account.shared:
                raise RuntimeError(
                    f"the homeserver holds other identity keys for device {setup.device_id}: the "
                    f"local store is stale; delete {setup.store} and restart, or use an access "
                    "token for a new device"
                )
            raise RuntimeError(
                f"device {setup.device_id} already has other identity keys on the homeserver; use "
                "an access token for a new device"
            )

    async def absorb(self, sync: Mapping[str, Any]) -> None:
        machine, types = self.machine, self.lib.types
        lists = _mapping(sync.get("device_lists"))
        if lists.get("changed") or lists.get("left"):
            try:
                await machine.handle_device_lists(types.DeviceLists.deserialize(dict(lists)))
            except Exception as exc:
                log.warning("matrix: a device-list update failed: %s", type(exc).__name__)
        for raw in _events(sync.get("to_device")):
            kind = raw.get("type")
            try:
                event = types.ToDeviceEvent.deserialize(dict(raw))
                if kind == "m.room.encrypted":
                    await machine.handle_to_device_event(event)
                elif kind == "m.room_key_request":
                    await machine.handle_room_key_request(event)
            except Exception as exc:
                log.warning(
                    "matrix: a to-device %s from %s could not be handled: %s",
                    kind,
                    raw.get("sender"),
                    type(exc).__name__,
                )
        counts = sync.get("device_one_time_keys_count")
        if isinstance(counts, Mapping):
            try:
                await machine.handle_otk_count(types.DeviceOTKCount.deserialize(dict(counts)))  # pyright: ignore[reportUnknownArgumentType]
            except Exception as exc:
                log.warning("matrix: topping up one-time keys failed: %s", type(exc).__name__)

    async def decrypt(self, room_id: str, event: Mapping[str, Any]) -> dict[str, Any]:
        lib = self.lib
        content = _mapping(event.get("content"))
        session_id = str(content.get("session_id") or "")
        try:
            sealed = lib.types.EncryptedEvent.deserialize({**dict(event), "room_id": room_id})
            clear = await self.machine.decrypt_megolm_event(sealed)
        except lib.errors.SessionNotFound:
            raise Undecryptable(session_id, "no key for this session", missing_key=True) from None
        except Exception as exc:
            raise Undecryptable(session_id, type(exc).__name__) from None
        data = clear.serialize()
        return {"type": str(data.get("type") or ""), "content": dict(data.get("content") or {})}

    async def encrypt(
        self, room_id: str, event_type: str, content: Mapping[str, Any], users: Sequence[str]
    ) -> dict[str, Any]:
        lib, machine = self.lib, self.machine
        kind = lib.types.EventType.find(event_type, lib.types.EventType.Class.MESSAGE)
        try:
            sealed = await machine.encrypt_megolm_event(room_id, kind, dict(content))
        except lib.errors.EncryptionError:
            if machine.is_sharing_group_session(room_id):
                await machine.wait_group_session_share(room_id)
            else:
                await machine.share_group_session(room_id, list(users))
            sealed = await machine.encrypt_megolm_event(room_id, kind, dict(content))
        return dict(sealed.serialize())

    async def rotate(self, room_id: str) -> None:
        await self._store.remove_outbound_group_session(room_id)

    async def cross_sign(self, recovery_key: str) -> None:
        await self.machine.verify_with_recovery_key(recovery_key)

    async def encrypt_attachment(self, data: bytes) -> tuple[bytes, dict[str, Any]]:
        ciphertext, info = self.lib.attachments.encrypt_attachment(data)
        sealed = {k: v for k, v in dict(info.serialize()).items() if k != "url" and v is not None}
        return bytes(ciphertext), sealed

    async def decrypt_attachment(self, data: bytes, info: Mapping[str, Any]) -> bytes:
        key = _mapping(info.get("key")).get("k")
        digest = _mapping(info.get("hashes")).get("sha256")
        iv = info.get("iv")
        if not (isinstance(key, str) and isinstance(digest, str) and isinstance(iv, str)):
            raise MatrixError("can't parse the encrypted attachment's key")
        return bytes(self.lib.attachments.decrypt_attachment(data, key, digest, iv))

    async def close(self) -> None:
        db, self._db = self._db, None
        if db is not None:
            await db.stop()


def _ours(trust: Any, found: Any) -> Trust:
    if found == trust.BLACKLISTED:
        return Trust.BLOCKED
    if found >= trust.CROSS_SIGNED_TRUSTED:
        return Trust.VERIFIED
    if found >= trust.CROSS_SIGNED_TOFU:
        return Trust.OWNER_SIGNED
    return Trust.UNVERIFIED


def _mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value  # pyright: ignore[reportUnknownVariableType]
    return {}


def _events(section: object) -> list[Mapping[str, Any]]:
    raw = _mapping(section).get("events")
    if not isinstance(raw, list):
        return []
    return [e for e in raw if isinstance(e, Mapping)]  # pyright: ignore[reportUnknownVariableType]
