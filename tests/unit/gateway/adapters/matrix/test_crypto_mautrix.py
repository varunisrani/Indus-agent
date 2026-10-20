"""Matrix end-to-end encryption with the real library (P16-07): the ``matrix-e2ee`` extra lane.

Skipped unless mautrix, python-olm (libolm), aiosqlite and asyncpg are installed
(``uv sync --extra matrix-e2ee``, Linux). Everything runs over the fake homeserver's keys API;
no socket is opened. Other people are real ``OlmMachine`` s too (our `MautrixEngine` on their
own tokens), so every key, signature and ciphertext here is produced and checked by libolm.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import socket
import threading
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mautrix.crypto")
pytest.importorskip("olm")
pytest.importorskip("aiosqlite")
pytest.importorskip("asyncpg")

from indus.foundation.scope import make_scope  # noqa: E402
from indus.gateway.adapters.matrix.adapter import MatrixAdapter, MatrixOptions  # noqa: E402
from indus.gateway.adapters.matrix.client import MatrixClient  # noqa: E402
from indus.gateway.adapters.matrix.crypto import (  # noqa: E402
    RECOVERY_KEY_NAME,
    VENDOR_NAMES,
    EngineSetup,
    KeyPolicy,
    MatrixE2ee,
    MautrixEngine,
    RoomBook,
    Undecryptable,
    _load_mautrix,  # pyright: ignore[reportPrivateUsage]
    create_end_to_end,
)
from indus.gateway.core.events import InboundEvent  # noqa: E402
from tests.fakes.clock import FakeClock  # noqa: E402
from tests.fakes.platform.matrix_wire import BOT, HOMESERVER, MatrixWire  # noqa: E402

ROOM = "!secret:example.org"
BOB = "@bob:example.org"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("real network used in a Matrix encryption test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


def run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)  # pyright: ignore[reportArgumentType]


async def settle(clock: FakeClock, rounds: int = 8) -> None:
    for _ in range(rounds):
        await asyncio.to_thread(lambda: None)
        await clock.advance_and_settle(1.0)


async def until(clock: FakeClock, done: Callable[[], bool], *, rounds: int = 400) -> None:
    """Advance the fake clock until ``done()``; the store's worker thread runs in real time."""
    for _ in range(rounds):
        if done():
            return
        await asyncio.to_thread(threading.Event().wait, 0.01)
        await clock.advance_and_settle(0.5)
    raise AssertionError("the condition never came true")


class FakeSecrets:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get(self, name: str, /) -> str | None:
        return self.values.get(name)

    def known_values(self) -> list[str]:
        return list(self.values.values())

    @property
    def generation(self) -> int:
        return 1


async def everyone(user: str) -> bool:
    return True


async def person(wire: MatrixWire, user: str, device: str, store: Path) -> MautrixEngine:
    """Somebody's client device: a real Olm machine on its own token, keys uploaded."""
    store.mkdir(parents=True, exist_ok=True)
    client = MatrixClient(HOMESERVER, wire.add_device(user, device), transport=wire.transport())
    rooms = RoomBook()
    rooms.set_encryption(ROOM, {"algorithm": "m.megolm.v1.aes-sha2"})
    engine = MautrixEngine(
        EngineSetup(client, user, device, store, KeyPolicy(everyone), rooms), _load_mautrix()
    )
    try:
        await engine.start(send_only=False)
    except BaseException:
        await engine.close()
        raise
    return engine


async def read_inbox(engine: MautrixEngine, wire: MatrixWire) -> None:
    setup = engine.setup
    events = wire.keys.take(setup.user_id, setup.device_id)
    await engine.absorb({"to_device": {"events": events}})


def test_the_pinned_library_has_every_name_we_call() -> None:
    missing: list[str] = []
    for target, names in VENDOR_NAMES.items():
        module_name, _, qualname = target.partition(":")
        found: Any = importlib.import_module(module_name)
        for part in filter(None, qualname.split(".")):
            found = getattr(found, part)
        missing.extend(f"{target}.{n}" for n in names if not hasattr(found, n))
    assert missing == []


def test_end_to_end_over_the_fake_homeserver(tmp_path: Path) -> None:
    async def main() -> None:
        clock = FakeClock(datetime(2026, 1, 1, 9, tzinfo=UTC))
        wire = MatrixWire(clock=clock)
        wire.encrypt_room(ROOM)
        wire.members[ROOM] = [BOT, BOB]

        # The bot account's cross-signing was set up earlier from another of its devices.
        setup_device = await person(wire, BOT, "BOTSETUP", tmp_path / "botsetup")
        recovery_key = await setup_device.machine.generate_recovery_key()
        await setup_device.close()

        # Bob: a cross-signed phone and an old device nobody verified.
        phone = await person(wire, BOB, "BOBPHONE", tmp_path / "bobphone")
        await phone.machine.generate_recovery_key()
        old = await person(wire, BOB, "BOBOLD", tmp_path / "bobold")

        scope = make_scope(tmp_path / "home", env={}, clock=clock).with_secrets(
            FakeSecrets({RECOVERY_KEY_NAME: recovery_key})
        )
        adapter: MatrixAdapter = wire.adapter(
            options=MatrixOptions(e2ee="required", inviters=frozenset({BOB})),
            scope=scope,
            end_to_end=create_end_to_end,
        )
        events: list[InboundEvent] = []

        async def sink(event: InboundEvent) -> None:
            events.append(event)

        adapter.set_sink(sink)
        await adapter.connect()
        try:
            await _scenario(wire, clock, adapter, events, phone, old, tmp_path)
        finally:
            await adapter.disconnect()
            for engine in (phone, old):
                await engine.close()

    run(main())


async def _scenario(
    wire: MatrixWire,
    clock: FakeClock,
    adapter: MatrixAdapter,
    events: list[InboundEvent],
    phone: MautrixEngine,
    old: MautrixEngine,
    tmp_path: Path,
) -> None:
    if True:
        await settle(clock)
        crypto = adapter._crypto  # pyright: ignore[reportPrivateUsage]
        assert isinstance(crypto, MatrixE2ee)

        # Our device's keys are on the server and cross-signed with the account's key.
        ours = wire.keys.device_keys[BOT][wire.device_id]
        ssk = next(iter(wire.keys.cross_signing[BOT]["self_signing"]["keys"]))
        assert ssk in ours["signatures"][BOT]

        # The bot writes: Bob's verified phone can read it, his unverified device cannot.
        sent = await adapter.send(ROOM, "hello bob")
        assert sent.ok
        event = next(e for e in reversed(wire.sent) if e.room == ROOM)
        assert event.event_type == "m.room.encrypted" and "hello bob" not in json.dumps(
            event.content
        )
        timeline = {
            "type": "m.room.encrypted",
            "sender": BOT,
            "event_id": sent.message_id,
            "origin_server_ts": 1,
            "content": event.content,
        }
        await read_inbox(phone, wire)
        clear = await phone.decrypt(ROOM, timeline)
        assert clear["content"]["body"] == "hello bob"
        await read_inbox(old, wire)
        with pytest.raises(Undecryptable):
            await old.decrypt(ROOM, timeline)

        # Bob writes from his phone: the key reaches the bot in its sync, then the message.
        sealed = await phone.encrypt(
            ROOM, "m.room.message", {"msgtype": "m.text", "body": "hi bot"}, [BOT]
        )
        wire.push_event(
            ROOM,
            wire.event(BOB, sealed, event_type="m.room.encrypted"),
        )
        await until(clock, lambda: bool(events))
        assert [e.text for e in events] == ["hi bot"]

        # An encrypted attachment round trip through the library.
        photo = tmp_path / "plan.png"
        photo.write_bytes(b"\x89PNG plan")
        assert (await adapter.send_image(ROOM, str(photo))).ok
        image = next(e for e in reversed(wire.sent) if e.room == ROOM)
        media = list(wire.media.values())[-1][0]
        assert b"PNG plan" not in media
        await read_inbox(phone, wire)
        clear = await phone.decrypt(
            ROOM,
            {
                "type": "m.room.encrypted",
                "sender": BOT,
                "event_id": "$img",
                "origin_server_ts": 2,
                "content": image.content,
            },
        )
        assert await phone.decrypt_attachment(media, clear["content"]["file"]) == b"\x89PNG plan"


def test_a_new_device_resets_the_store_and_a_stale_store_is_refused(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def main() -> None:
        clock = FakeClock(datetime(2026, 1, 1, 9, tzinfo=UTC))
        wire = MatrixWire(clock=clock)
        store = tmp_path / "store"
        first = await person(wire, BOB, "DEV1", store)
        first_key = first.machine.account.identity_key
        await first.close()
        with caplog.at_level(logging.WARNING):
            second = await person(wire, BOB, "DEV2", store)
        assert second.machine.account.identity_key != first_key
        assert any("starting a new encryption identity" in r.getMessage() for r in caplog.records)
        await second.close()
        # The server now holds other keys for DEV2 than our (already published) store.
        wire.keys.device_keys[BOB]["DEV2"]["keys"]["ed25519:DEV2"] = "A" * 43
        with pytest.raises(RuntimeError, match="local store is stale"):
            await person(wire, BOB, "DEV2", store)

    run(main())
