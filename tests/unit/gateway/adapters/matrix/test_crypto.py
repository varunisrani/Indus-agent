"""Matrix end-to-end encryption (P16-07): our rules around the Olm/Megolm library.

The library is replaced by `FakeEngine` (no extra installed): it shares a room key with exactly the
devices the `KeyPolicy` admits, "encrypts" by wrapping the JSON, and knows the sessions it was
told about. The real mautrix engine runs in ``test_crypto_mautrix.py`` (skipped without the extra).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import socket
import stat
import threading
from collections.abc import Awaitable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.config.schema import PlatformSection
from indus.foundation.scope import Scope, make_scope
from indus.gateway.adapters.base import AdapterConnectError
from indus.gateway.adapters.matrix import create_adapter
from indus.gateway.adapters.matrix import crypto as e2ee
from indus.gateway.adapters.matrix.adapter import MatrixAdapter, MatrixOptions
from indus.gateway.adapters.matrix.crypto import (
    RECOVERY_KEY_NAME,
    UNDECRYPTABLE_NOTICE,
    Device,
    EngineSetup,
    KeyPolicy,
    MatrixE2ee,
    RoomBook,
    StoreLock,
    Trust,
    Undecryptable,
    start_end_to_end,
    store_dir,
)
from indus.gateway.config import AdapterConfig, PlatformSettings
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind
from tests.fakes.clock import FakeClock
from tests.fakes.platform.matrix_wire import BOT, KEY_ROUTES, MatrixWire, fake_token

ROOM = "!secret:example.org"
PLAIN = "!open:example.org"
ANA = "@ana:example.org"
BEN = "@ben:example.org"
CAT = "@cat:example.org"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("real network used in a Matrix encryption test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


def run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)  # pyright: ignore[reportArgumentType]


async def settle(clock: FakeClock, rounds: int = 6) -> None:
    """Let syncs, worker-thread file work and spawned sends finish on the fake clock."""
    for _ in range(rounds):
        await asyncio.to_thread(threading.Event().wait, 0.005)  # the io pool's file work
        await clock.advance_and_settle(1.0)


async def long_polls(clock: FakeClock, count: int = 4) -> None:
    """Let ``count`` idle long polls (30 s each on the fake homeserver) come back empty."""
    for _ in range(count):
        await asyncio.to_thread(threading.Event().wait, 0.005)  # the io pool's file work
        await clock.advance_and_settle(31.0)


class FakeSecrets:
    def __init__(self, values: Mapping[str, str]) -> None:
        self.values = dict(values)

    def get(self, name: str, /) -> str | None:
        return self.values.get(name)

    def known_values(self) -> list[str]:
        return list(self.values.values())

    @property
    def generation(self) -> int:
        return 1


class FakeEngine:
    """Stands in for mautrix: devices with trust levels, sessions by id, wrapped "ciphertext"."""

    def __init__(self, setup: EngineSetup, devices: Mapping[str, Mapping[str, Trust]]) -> None:
        self.setup = setup
        self.devices = devices
        self.started: list[bool] = []
        self.absorbed: list[Mapping[str, Any]] = []
        self.known_sessions: set[str] = set()
        # Room → (session id, devices it was shared with).
        self.outbound: dict[str, tuple[str, list[tuple[str, str]]]] = {}
        self.shares: list[tuple[str, list[tuple[str, str]]]] = []
        self.rotations: list[str] = []
        self.cross_signed: list[str] = []
        self.cross_sign_error: Exception | None = None
        self.plain: list[dict[str, Any]] = []
        self.closed = False
        self._sessions = 0

    async def start(self, *, send_only: bool) -> None:
        self.started.append(send_only)

    async def absorb(self, sync: Mapping[str, Any]) -> None:
        self.absorbed.append(sync)
        for event in (sync.get("to_device") or {}).get("events", []):
            key = (event.get("content") or {}).get("fake_room_key")
            if key:
                self.known_sessions.add(str(key))

    async def decrypt(self, room_id: str, event: Mapping[str, Any]) -> dict[str, Any]:
        content = event.get("content") or {}
        session = str(content.get("session_id") or "")
        if session not in self.known_sessions:
            raise Undecryptable(session, "no key", missing_key=True)
        clear = json.loads(str(content.get("ciphertext")))
        return {"type": clear["type"], "content": clear["content"]}

    async def encrypt(
        self, room_id: str, event_type: str, content: Mapping[str, Any], users: Sequence[str]
    ) -> dict[str, Any]:
        if room_id not in self.outbound:
            self._sessions += 1
            reached: list[tuple[str, str]] = []
            for user in users:
                for device, trust in self.devices.get(user, {}).items():
                    if await self.setup.policy.may_receive(Device(user, device, trust)):
                        reached.append((user, device))
            session = f"S{self._sessions}"
            self.outbound[room_id] = (session, reached)
            self.shares.append((room_id, reached))
        session, _ = self.outbound[room_id]
        self.plain.append(dict(content))
        return {
            "algorithm": "m.megolm.v1.aes-sha2",
            "session_id": session,
            "ciphertext": json.dumps({"type": event_type, "content": dict(content)}),
        }

    async def rotate(self, room_id: str) -> None:
        self.rotations.append(room_id)
        self.outbound.pop(room_id, None)

    async def cross_sign(self, recovery_key: str) -> None:
        if self.cross_sign_error is not None:
            raise self.cross_sign_error
        self.cross_signed.append(recovery_key)

    async def encrypt_attachment(self, data: bytes) -> tuple[bytes, dict[str, Any]]:
        return data[::-1], {"key": {"k": "fake"}, "iv": "iv", "hashes": {"sha256": "h"}, "v": "v2"}

    async def decrypt_attachment(self, data: bytes, info: Mapping[str, Any]) -> bytes:
        assert info["key"] == {"k": "fake"}
        return data[::-1]

    async def close(self) -> None:
        self.closed = True


DEVICES: dict[str, dict[str, Trust]] = {
    ANA: {"ANA-PHONE": Trust.OWNER_SIGNED, "ANA-NEW": Trust.UNVERIFIED},
    BEN: {"BEN-LAPTOP": Trust.VERIFIED},
    CAT: {"CAT-DESK": Trust.VERIFIED, "CAT-OLD": Trust.BLOCKED},
    BOT: {"BOT-OTHER": Trust.OWNER_SIGNED},
}


class Rig:
    def __init__(
        self,
        home: Path,
        *,
        e2ee: str = "required",
        send_only: bool = False,
        allowed: frozenset[str] = frozenset({ANA, CAT}),
        secrets: Mapping[str, str] | None = None,
        env: Mapping[str, str] | None = None,
        trust: str = "owner_signed",
        wire: MatrixWire | None = None,
        listen: bool = True,
    ) -> None:
        self.clock = (
            wire.clock if wire is not None else FakeClock(datetime(2026, 1, 1, 9, tzinfo=UTC))
        )
        self.wire = wire or MatrixWire(clock=self.clock)
        self.wire.platform.add_chat(ANA, "dm", "Ana")
        self.wire.encrypt_room(ROOM)
        self.wire.members.setdefault(ROOM, [BOT, ANA, BEN])
        scope: Scope = make_scope(home, env=dict(env or {}), clock=self.clock)
        if secrets is not None:
            scope = scope.with_secrets(FakeSecrets(secrets))
        self.scope = scope
        self.engines: list[FakeEngine] = []
        options = MatrixOptions(e2ee=e2ee, inviters=allowed, e2ee_trust=trust)  # pyright: ignore[reportArgumentType]
        self.adapter: MatrixAdapter = self.wire.adapter(
            options=options,
            scope=scope,
            send_only=send_only,
            end_to_end=partial(start_end_to_end, engine_factory=self._engine),
        )
        self.events: list[InboundEvent] = []
        if listen:
            self.adapter.set_sink(self._sink)

    def _engine(self, setup: EngineSetup) -> FakeEngine:
        engine = FakeEngine(setup, DEVICES)
        self.engines.append(engine)
        return engine

    @property
    def engine(self) -> FakeEngine:
        return self.engines[-1]

    @property
    def crypto(self) -> MatrixE2ee:
        found = self.adapter._crypto  # pyright: ignore[reportPrivateUsage]
        assert isinstance(found, MatrixE2ee)
        return found

    async def _sink(self, event: InboundEvent) -> None:
        self.events.append(event)

    async def connect(self) -> None:
        await self.adapter.connect()
        await settle(self.clock, 2)

    async def close(self) -> None:
        await self.adapter.disconnect()

    def encrypted(self, sender: str, session: str, body: str, **extra: Any) -> dict[str, Any]:
        event = self.wire.event(
            sender,
            {
                "algorithm": "m.megolm.v1.aes-sha2",
                "session_id": session,
                "ciphertext": json.dumps(
                    {"type": "m.room.message", "content": {"msgtype": "m.text", "body": body}}
                ),
            },
            event_type="m.room.encrypted",
        )
        event.update(extra)
        return event

    def notices(self) -> list[dict[str, Any]]:
        return [c for c in self.engine.plain if c.get("body") == UNDECRYPTABLE_NOTICE]


# -- the row's Verify: unverified device gets no keys ----------------------------------------------


def test_only_verified_devices_of_allowed_users_get_room_keys(tmp_path: Path) -> None:
    async def main() -> None:
        rig = Rig(tmp_path / "home")
        await rig.connect()
        sent = await rig.adapter.send(ROOM, "the plan is ready")
        assert sent.ok
        # Ana is allowed: her cross-signed phone gets the key, her unverified new device does not.
        # Ben is a member with a verified laptop but on no allowlist: nothing.
        [(room, reached)] = rig.engine.shares
        assert room == ROOM
        assert reached == [(ANA, "ANA-PHONE"), (BOT, "BOT-OTHER")]
        [event] = [e for e in rig.wire.sent if e.room == ROOM]
        assert event.event_type == "m.room.encrypted"
        assert "the plan is ready" not in json.dumps(event.content.get("body", ""))
        assert "body" not in event.content
        await rig.close()

    run(main())


def test_key_policy_table() -> None:
    async def main() -> None:
        async def allowed(user: str) -> bool:
            return user == ANA

        default = KeyPolicy(allowed)
        strict = KeyPolicy(allowed, Trust.VERIFIED)
        cases = [
            (Device(ANA, "A", Trust.UNVERIFIED), False, False),
            (Device(ANA, "A", Trust.OWNER_SIGNED), True, False),
            (Device(ANA, "A", Trust.VERIFIED), True, True),
            (Device(ANA, "A", Trust.BLOCKED), False, False),
            (Device(BEN, "B", Trust.VERIFIED), False, False),
        ]
        for device, by_default, by_strict in cases:
            assert await default.may_receive(device) is by_default, device
            assert await strict.may_receive(device) is by_strict, device

    run(main())


def test_e2ee_trust_verified_needs_more_than_an_owner_signature(tmp_path: Path) -> None:
    async def main() -> None:
        rig = Rig(tmp_path / "home", trust="verified", allowed=frozenset({ANA, CAT}))
        rig.wire.members[ROOM] = [BOT, ANA, CAT]
        await rig.connect()
        assert (await rig.adapter.send(ROOM, "hello")).ok
        [(_, reached)] = rig.engine.shares
        assert reached == [(CAT, "CAT-DESK")]  # Ana's phone is only owner-signed; CAT-OLD blocked
        await rig.close()

    run(main())


def test_a_change_in_allowed_members_rotates_the_room_key(tmp_path: Path) -> None:
    async def main() -> None:
        rig = Rig(tmp_path / "home")
        await rig.connect()
        assert (await rig.adapter.send(ROOM, "one")).ok
        assert (await rig.adapter.send(ROOM, "two")).ok
        assert len(rig.engine.shares) == 1  # one session for both messages
        # Cat (allowed) joins: the sync's member event rotates the session.
        rig.wire.push_event(
            ROOM,
            {
                "type": "m.room.member",
                "state_key": CAT,
                "sender": CAT,
                "event_id": "$join",
                "content": {"membership": "join"},
            },
        )
        await settle(rig.clock)
        assert ROOM in rig.engine.rotations
        assert (await rig.adapter.send(ROOM, "three")).ok
        assert rig.engine.shares[-1][1] == [
            (ANA, "ANA-PHONE"),
            (CAT, "CAT-DESK"),
            (BOT, "BOT-OTHER"),
        ]
        # Ana leaves: rotated again, and she is not given the next key.
        rig.wire.push_event(
            ROOM,
            {
                "type": "m.room.member",
                "state_key": ANA,
                "sender": ANA,
                "event_id": "$leave",
                "content": {"membership": "leave"},
            },
        )
        await settle(rig.clock)
        assert (await rig.adapter.send(ROOM, "four")).ok
        assert rig.engine.shares[-1][1] == [(CAT, "CAT-DESK"), (BOT, "BOT-OTHER")]
        await rig.close()

    run(main())


# -- the row's Verify: undecryptable event → one notice --------------------------------------------


def test_an_undecryptable_event_gives_one_notice_per_session_and_no_event(tmp_path: Path) -> None:
    async def main() -> None:
        rig = Rig(tmp_path / "home")
        await rig.connect()
        rig.wire.push_event(ROOM, rig.encrypted(ANA, "LOST1", "first"))
        await settle(rig.clock)
        assert rig.notices() == []  # waiting a few syncs for the key first
        await long_polls(rig.clock)
        assert len(rig.notices()) == 1
        rig.wire.push_event(ROOM, rig.encrypted(ANA, "LOST1", "second"))
        await long_polls(rig.clock)
        assert len(rig.notices()) == 1  # one per session
        rig.wire.push_event(ROOM, rig.encrypted(BEN, "LOST2", "third"))
        await long_polls(rig.clock)
        assert len(rig.notices()) == 2
        assert rig.events == []  # never passed on, never as empty text
        # The notices themselves went out encrypted.
        notice_events = [e for e in rig.wire.sent if e.room == ROOM]
        assert notice_events and all(e.event_type == "m.room.encrypted" for e in notice_events)
        await rig.close()

    run(main())


def test_backlog_and_own_events_cause_no_notice(tmp_path: Path) -> None:
    async def main() -> None:
        rig = Rig(tmp_path / "home")
        old = rig.encrypted(ANA, "OLD", "from before")
        old["origin_server_ts"] -= 60_000
        rig.wire.push_event(ROOM, old)
        await rig.connect()
        rig.wire.push_event(ROOM, rig.encrypted(BOT, "MINE", "ours from another device"))
        await long_polls(rig.clock)
        assert rig.notices() == [] and rig.events == []
        await rig.close()

    run(main())


def test_a_key_that_arrives_a_sync_later_still_decrypts(tmp_path: Path) -> None:
    async def main() -> None:
        rig = Rig(tmp_path / "home")
        await rig.connect()
        rig.wire.push_event(ROOM, rig.encrypted(ANA, "LATE", "worth the wait"))
        await rig.clock.advance_and_settle(1.0)
        assert rig.events == []
        rig.wire.keys.to_device[(BOT, rig.wire.device_id)] = [
            {"type": "m.room.encrypted", "sender": ANA, "content": {"fake_room_key": "LATE"}}
        ]
        rig.wire.wake()
        await settle(rig.clock)
        assert [e.text for e in rig.events] == ["worth the wait"]
        assert rig.events[0].source.chat_id == ROOM
        assert rig.notices() == []
        await rig.close()

    run(main())


def test_to_device_events_reach_the_engine_before_the_timeline(tmp_path: Path) -> None:
    async def main() -> None:
        rig = Rig(tmp_path / "home")
        await rig.connect()
        rig.wire.keys.to_device[(BOT, rig.wire.device_id)] = [
            {"type": "m.room.encrypted", "sender": ANA, "content": {"fake_room_key": "SAME"}}
        ]
        rig.wire.push_event(ROOM, rig.encrypted(ANA, "SAME", "key and message together"))
        await settle(rig.clock)
        assert [e.text for e in rig.events] == ["key and message together"]
        await rig.close()

    run(main())


# -- the row's Verify: missing extra ---------------------------------------------------------------


def _mautrix_installed() -> bool:
    return (
        importlib.util.find_spec("mautrix") is not None
        and importlib.util.find_spec("olm") is not None
    )


def _config(home: Path, e2ee_mode: str) -> AdapterConfig:
    clock = FakeClock(datetime(2026, 1, 1, 9, tzinfo=UTC))
    scope = make_scope(home, env={}, clock=clock)
    section = PlatformSection.model_validate(
        {"enabled": True, "extra": {"homeserver": "https://matrix.example.org", "e2ee": e2ee_mode}}
    )
    settings = PlatformSettings(
        "matrix",
        True,
        "enabled in config",
        section,
        credentials={"MATRIX_ACCESS_TOKEN": fake_token()},
    )
    return AdapterConfig("matrix", settings, scope, clock, None)


@pytest.mark.skipif(_mautrix_installed(), reason="the matrix-e2ee extra is installed here")
def test_without_the_extra_required_is_fatal_with_a_hint_and_optional_runs_plain(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def main() -> None:
        wire = MatrixWire(clock=FakeClock(datetime(2026, 1, 1, 9, tzinfo=UTC)))
        # The real factory path: create_adapter → load_end_to_end → crypto.create_end_to_end.
        required = create_adapter(_config(tmp_path / "a", "required"), transport=wire.transport())
        with pytest.raises(AdapterConnectError) as failed:
            await required.connect()
        assert failed.value.code == "e2ee_unavailable" and not failed.value.retryable
        assert "indus-agent[matrix-e2ee]" in str(failed.value) and "Linux" in str(failed.value)
        assert not required.connected
        optional = create_adapter(_config(tmp_path / "b", "optional"), transport=wire.transport())
        with caplog.at_level(logging.WARNING):
            await optional.connect()
        assert optional.connected
        assert any("matrix-e2ee" in r.getMessage() for r in caplog.records)
        assert (await optional.send(PLAIN, "plain rooms still work")).ok
        await optional.disconnect()

    run(main())


def test_the_extra_is_reported_missing_off_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(e2ee.sys, "platform", "darwin")
    with pytest.raises(e2ee.E2eeUnavailable, match="Linux only"):
        e2ee._load_mautrix()  # pyright: ignore[reportPrivateUsage]


# -- store, lock, recovery key, send-only ----------------------------------------------------------


def test_the_store_lives_under_the_profile_home_with_private_modes(tmp_path: Path) -> None:
    async def main() -> None:
        home = tmp_path / "home"
        rig = Rig(home)
        await rig.connect()
        directory = store_dir(home)
        assert directory == home / "platforms" / "matrix" / "crypto"
        assert rig.engine.setup.store == directory
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        files = [p for p in directory.iterdir() if p.is_file()]
        assert {p.name for p in files} >= {"crypto.db", "store.lock"}
        assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in files)
        assert rig.engine.setup.device_id == rig.wire.device_id
        await rig.close()
        assert rig.engine.closed

    run(main())


def test_a_second_process_on_the_same_store_is_refused(tmp_path: Path) -> None:
    async def main() -> None:
        home = tmp_path / "home"
        first = Rig(home)
        await first.connect()
        second = Rig(home, wire=first.wire)
        with pytest.raises(AdapterConnectError) as busy:
            await second.adapter.connect()
        assert busy.value.code == "e2ee_store_busy" and busy.value.retryable
        # ``indus send`` meanwhile: plain rooms work, the encrypted room is refused, never plain.
        sender = Rig(home, wire=first.wire, send_only=True, listen=False)
        await sender.connect()
        assert sender.crypto.locked_out
        before = len(first.wire.sent)
        refused = await sender.adapter.send(ROOM, "secret")
        assert not refused.ok and "running gateway" in (refused.error or "")
        assert len(first.wire.sent) == before
        assert (await sender.adapter.send(PLAIN, "hello")).ok
        await sender.close()
        await first.close()
        # The lock is released on close.
        lock = StoreLock(store_dir(home))
        assert lock.acquire()
        lock.release()

    run(main())


def test_the_recovery_key_comes_only_from_the_secret_store(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    key = " ".join(["EsTj", "3yST", "y93F", "SLpB"])  # recovery-key shaped, built at run time

    async def main() -> None:
        from_env = Rig(tmp_path / "a", env={RECOVERY_KEY_NAME: key})
        await from_env.connect()
        assert from_env.engine.cross_signed == []
        await from_env.close()

        stored = Rig(tmp_path / "b", secrets={RECOVERY_KEY_NAME: key})
        await stored.connect()
        assert stored.engine.cross_signed == [key]
        assert key not in repr(stored.crypto) and key not in repr(stored.adapter)
        await stored.close()

        failing = Rig(tmp_path / "c", secrets={RECOVERY_KEY_NAME: key})
        original = failing._engine  # pyright: ignore[reportPrivateUsage]

        def broken(setup: EngineSetup) -> FakeEngine:
            engine = original(setup)
            engine.cross_sign_error = ValueError(f"Key MAC does not match for {key}")
            return engine

        failing.adapter._end_to_end_factory = partial(  # pyright: ignore[reportPrivateUsage]
            start_end_to_end, engine_factory=broken
        )
        with caplog.at_level(logging.DEBUG):
            await failing.connect()
        assert failing.adapter.connected  # a bad key is a warning, not a failed connect
        await failing.close()

    run(main())
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "cross-signing with the recovery key failed" in text
    assert key not in text and key.replace(" ", "") not in text


def test_send_only_never_syncs_or_uploads_keys(tmp_path: Path) -> None:
    async def main() -> None:
        rig = Rig(tmp_path / "home", send_only=True, secrets={RECOVERY_KEY_NAME: "k" * 12})
        await rig.connect()
        rig.adapter.set_sink(rig._sink)  # pyright: ignore[reportPrivateUsage]
        await settle(rig.clock)
        assert rig.engine.started == [True]
        assert rig.engine.absorbed == [] and rig.engine.cross_signed == []
        assert rig.wire.sync_paths() == []
        # Whether the room is encrypted comes from its state; the message goes out encrypted.
        assert (await rig.adapter.send(ROOM, "from a script")).ok
        assert rig.wire.sent[-1].event_type == "m.room.encrypted"
        assert (await rig.adapter.send(PLAIN, "plain")).ok
        assert rig.wire.sent[-1].event_type == "m.room.message"
        await rig.close()

    run(main())


# -- attachments -----------------------------------------------------------------------------------


def test_attachments_in_encrypted_rooms_travel_encrypted(tmp_path: Path) -> None:
    async def main() -> None:
        rig = Rig(tmp_path / "home")
        await rig.connect()
        photo = tmp_path / "plan.png"
        photo.write_bytes(b"PNGDATA")
        sent = await rig.adapter.send_image(ROOM, str(photo), caption="the plan")
        assert sent.ok
        [stored] = list(rig.wire.media.values())
        assert stored == (b"ATADGNP", "application/octet-stream")  # ciphertext uploaded
        clear = rig.engine.plain[-1]
        assert "url" not in clear and clear["file"]["url"].startswith("mxc://")
        assert clear["file"]["key"] == {"k": "fake"}
        assert clear["info"] == {"mimetype": "image/png", "size": 7}

        # Inbound: an encrypted image is fetched and decrypted for the intake.
        rig.wire.media["in9"] = (b"OLLEH", "application/octet-stream")
        image = {
            "msgtype": "m.image",
            "body": "photo.jpg",
            "info": {"mimetype": "image/jpeg", "size": 5},
            "file": {
                "url": "mxc://example.org/in9",
                "key": {"k": "fake"},
                "iv": "iv",
                "hashes": {"sha256": "h"},
                "v": "v2",
            },
        }
        rig.engine.known_sessions.add("PIC")
        event = rig.wire.event(
            ANA,
            {
                "algorithm": "m.megolm.v1.aes-sha2",
                "session_id": "PIC",
                "ciphertext": json.dumps({"type": "m.room.message", "content": image}),
            },
            event_type="m.room.encrypted",
        )
        rig.wire.push_event(ROOM, event)
        await settle(rig.clock)
        [got] = rig.events
        assert got.kind is MessageKind.PHOTO
        chunks = [c async for c in rig.adapter.fetch_media(Attachment(ref="mxc://example.org/in9"))]
        assert b"".join(chunks) == b"HELLO"
        await rig.close()

    run(main())


# -- small pieces ----------------------------------------------------------------------------------


def test_room_book_tracks_encryption_and_member_changes() -> None:
    book = RoomBook()
    assert book.is_encrypted("!a:x") is None
    book.members["!a:x"] = {ANA}
    sync = {
        "rooms": {
            "join": {
                "!a:x": {
                    "state": {
                        "events": [
                            {
                                "type": "m.room.encryption",
                                "state_key": "",
                                "content": {"algorithm": "m.megolm.v1.aes-sha2"},
                            }
                        ]
                    },
                    "timeline": {
                        "events": [
                            {
                                "type": "m.room.member",
                                "state_key": BEN,
                                "content": {"membership": "join"},
                            },
                            {"type": "m.room.message", "content": {"body": "x"}},
                        ]
                    },
                }
            }
        }
    }
    assert book.absorb(sync) == {"!a:x"}
    assert book.is_encrypted("!a:x") is True and book.members["!a:x"] == {ANA, BEN}
    assert book.absorb(sync) == set()  # nothing new
    book.set_encryption("!a:x", None)  # a later "not encrypted" never downgrades a room
    assert book.is_encrypted("!a:x") is True
    assert book.shared_with(BEN) == ["!a:x"]


def test_the_store_lock_is_exclusive(tmp_path: Path) -> None:
    tmp_path.mkdir(exist_ok=True)
    first, second = StoreLock(tmp_path), StoreLock(tmp_path)
    assert first.acquire() and first.held
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()
    assert stat.S_IMODE(os.stat(tmp_path / "store.lock").st_mode) == 0o600


def test_the_fake_homeserver_serves_the_keys_api() -> None:
    assert set(KEY_ROUTES) == {
        "keys_upload",
        "keys_query",
        "keys_claim",
        "send_to_device",
        "device_signing_upload",
        "signatures_upload",
    }
