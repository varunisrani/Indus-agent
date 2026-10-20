"""The SimpleX adapter (P16-28) over the fake daemon socket."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.config.schema import PlatformSection
from indus.foundation.scope import Scope
from indus.gateway.adapters.base import AdapterConnectError
from indus.gateway.adapters.simplex import create_adapter
from indus.gateway.adapters.simplex.adapter import (
    HEALTH_EVERY,
    STALE_AFTER,
    SimplexAdapter,
    SimplexOptions,
)
from indus.gateway.adapters.simplex.adapter import (
    _error_text as error_text,  # pyright: ignore[reportPrivateUsage]
)
from indus.gateway.config import AdapterConfig, PlatformSettings
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind
from indus.gateway.core.send import ErrorKind
from indus.gateway.intake.media import MediaRejected
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FailureKind, FakeChatPlatform
from tests.fakes.platform.simplex_wire import URL, FakeSocket, SimplexWire


class Rig:
    def __init__(self, *, options: SimplexOptions | None = None, **kw: Any) -> None:
        self.clock = FakeClock(datetime(2026, 3, 1, 9, 0, tzinfo=UTC))
        self.platform = FakeChatPlatform("simplex", clock=self.clock, max_len=8000)
        self.wire = SimplexWire(self.platform, files_folder=kw.pop("files_folder", None))
        self.opened_at: list[float] = []
        self.adapter = SimplexAdapter(
            clock=self.clock,
            ws_url=kw.pop("ws_url", URL),
            options=options or SimplexOptions(group_chats=frozenset({"300"})),
            opener=self.opener,
            rng=lambda: 0.0,
            **kw,
        )
        self.events: list[InboundEvent] = []

        async def sink(event: InboundEvent) -> None:
            self.events.append(event)

        self.adapter.set_sink(sink)

    @contextlib.asynccontextmanager
    async def opener(self, url: str) -> AsyncIterator[FakeSocket]:
        self.opened_at.append(self.clock.monotonic())
        async with self.wire.open(url) as socket:
            yield socket

    async def step(self, seconds: float = 1.0, times: int = 1) -> None:
        for _ in range(times):
            await self.clock.advance_and_settle(seconds)

    async def settle(self, awaitable: Awaitable[Any]) -> Any:
        task = asyncio.ensure_future(awaitable)
        for _ in range(400):
            await self.clock.run_until_idle()
            if not task.done():
                await asyncio.wait({task}, timeout=0.002)  # file reads run in a worker thread
            if task.done():
                return task.result()
            self.clock.advance(0.5)
        raise AssertionError("never finished")

    async def frames(self, *frames: dict[str, Any]) -> None:
        for frame in frames:
            self.wire.push(frame)
        await self.clock.run_until_idle()


def run(main: Callable[[Rig], Awaitable[None]], **kw: Any) -> None:
    async def outer() -> None:
        rig = Rig(**kw)
        try:
            await main(rig)
        finally:
            await rig.adapter.disconnect()

    asyncio.run(outer())


def dm_row(text: str = "hi", item: int = 7, contact: int = 12, **chat_item: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "chatInfo": {
            "type": "direct",
            "contact": {"contactId": contact, "localDisplayName": "ana", "profile": {}},
        },
        "chatItem": {
            "chatDir": {"type": "directRcv"},
            "meta": {"itemId": item, "itemTs": "2026-03-01T09:00:00Z"},
            "content": {"type": "rcvMsgContent", "msgContent": {"type": "text", "text": text}},
        },
    }
    row["chatItem"].update(chat_item)
    return row


def group_row(text: str, group: int = 300, **chat_item: Any) -> dict[str, Any]:
    row = dm_row(text, **chat_item)
    row["chatInfo"] = {"type": "group", "groupInfo": {"groupId": group, "localDisplayName": "ops"}}
    member = {"memberId": "bWVtYmVy", "localDisplayName": "bo", "memberProfile": {}}
    row["chatItem"]["chatDir"] = {"type": "groupRcv", "groupMember": member}
    return row


def items(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"resp": {"type": "newChatItems", "chatItems": list(rows)}}


# -- contact requests ------------------------------------------------------------------------------


def request(ident: int = 5) -> dict[str, Any]:
    body = {"contactRequestId": ident, "localDisplayName": "stranger"}
    return {"resp": {"type": "receivedContactRequest", "contactRequest": body}}


def test_a_contact_request_is_not_auto_accepted_by_default(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="indus.gateway.adapters.simplex.adapter")

    async def main(rig: Rig) -> None:
        await rig.settle(rig.adapter.connect())
        await rig.frames(request(5), request(5))
        assert rig.wire.accepted == []
        assert not any(c.startswith("/_accept") for c in rig.wire.commands)
        hints = [r.getMessage() for r in caplog.records if "not accepted" in r.getMessage()]
        assert len(hints) == 1 and "/_accept 5" in hints[0]

    run(main)


def test_contact_requests_are_accepted_when_the_operator_opts_in() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.adapter.connect())
        await rig.frames(request(9))
        await rig.step(0.1)
        assert rig.wire.accepted == ["9"]

    run(main, options=SimplexOptions(auto_accept=True))


def test_options_read_the_settings_and_env() -> None:
    options = SimplexOptions.read(
        {"group_allowed_chats": "12, group:13", "files_folder": "/srv/simplex"},
        {"SIMPLEX_AUTO_ACCEPT": "true", "INDUS_SIMPLEX_GROUP_ALLOWED_CHATS": "14"},
    )
    assert options.auto_accept and options.group_chats == {"12", "group:13", "14"}
    assert options.files_folder == Path("/srv/simplex")
    assert not SimplexOptions.read({}, {}).auto_accept
    assert not SimplexOptions.read({"auto_accept": False}, {"SIMPLEX_AUTO_ACCEPT": "1"}).auto_accept


# -- inbound ---------------------------------------------------------------------------------------


def test_direct_and_allowed_group_messages_become_events() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.adapter.connect())
        own = dm_row("echo", chatDir={"type": "directSnd"})
        sent = dm_row("sent", content={"type": "sndMsgContent", "msgContent": {"type": "text"}})
        event = dm_row("event", content={"type": "rcvGroupEvent"})
        await rig.frames(
            items(dm_row("hello"), own, sent, event),
            items(group_row("in ops", meta={"itemId": 8, "userMention": True})),
            items(group_row("elsewhere", group=301)),
        )
        texts = [(e.text, e.source.chat_id, e.source.chat_type) for e in rig.events]
        assert texts == [("hello", "12", "dm"), ("in ops", "group:300", "group")]
        dm, group = rig.events
        assert dm.user_id == "12" and dm.message_id == "7" and dm.timestamp is not None
        assert group.user_id == "bWVtYmVy" and group.metadata == {"mentioned": True}
        assert group.source.chat_name == "ops"

    run(main)


def test_edits_quotes_and_bots() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.adapter.connect())
        edited = {"resp": {"type": "chatItemUpdated", "chatItem": dm_row("fixed text")}}
        quote = {"itemId": 3, "chatDir": {"type": "directSnd"}, "content": {"text": "ours"}}
        bot = dm_row("beep", item=9)
        bot["chatInfo"]["contact"]["profile"] = {"peerType": "bot"}
        await rig.frames(edited, items(dm_row("re", item=8, quotedItem=quote)), items(bot))
        fixed, reply, beep = rig.events
        assert fixed.metadata == {"edited": True}
        assert reply.reply_to_id == "3" and reply.reply_to_is_own and reply.reply_to_text == "ours"
        assert beep.source.is_bot

    run(main)


def test_frames_that_are_not_events_are_ignored() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.adapter.connect())
        await rig.frames({"resp": {"type": "chatItemsStatusesUpdated"}})
        rig.wire.push("not json")
        rig.wire.push("[1, 2]")
        await rig.frames({"corrId": "indus-999", "resp": {"type": "late answer"}})
        await rig.frames(items(dm_row("still alive")))
        assert [e.text for e in rig.events] == ["still alive"]

    run(main)


def test_send_only_handles_no_events() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.adapter.connect())
        await rig.frames(items(dm_row("hello")), request(4))
        assert rig.events == [] and rig.wire.accepted == []
        assert rig.adapter._health is None  # pyright: ignore[reportPrivateUsage]
        assert (await rig.settle(rig.adapter.send("12", "from a script"))).ok

    run(main, send_only=True, options=SimplexOptions(auto_accept=True))


# -- outbound --------------------------------------------------------------------------------------


def test_sends_address_by_id_and_return_item_ids() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.adapter.connect())
        result = await rig.settle(rig.adapter.send("12", "hello", reply_to="7"))
        assert result.ok and result.message_id is not None
        command = rig.wire.commands[-1]
        assert command.startswith("/_send @12 json ") and '"quotedItemId": 7' in command
        grouped = await rig.settle(rig.adapter.send("group:300", "hi all"))
        assert grouped.ok and rig.wire.commands[-1].startswith("/_send #300 json ")
        bad = await rig.settle(rig.adapter.send("ana", "by name"))
        assert bad.kind is ErrorKind.NOT_FOUND and bad.chat_unreachable

    run(main)


def test_daemon_errors_and_timeouts_are_classified() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.adapter.connect())
        store = {"type": "errorStore", "storeError": {"type": "contactNotFound", "contactId": 99}}
        error = {"type": "chatCmdError", "chatError": store}
        assert "chat not found" in error_text(error)
        rig.platform.fail(FailureKind.READ_TIMEOUT, op="send")
        slow = await rig.settle(rig.adapter.send("12", "x"))
        assert slow.maybe_delivered and not slow.retryable
        rig.platform.fail(FailureKind.CONNECT_TIMEOUT, op="send")
        lost = await rig.settle(rig.adapter.send("12", "x"))
        assert lost.kind is ErrorKind.TRANSIENT and lost.retryable

    run(main)


def test_files_go_as_paths_the_daemon_reads(tmp_path: Path) -> None:
    note = tmp_path / "note.ogg"
    note.write_bytes(b"OggS")

    async def main(rig: Rig) -> None:
        await rig.settle(rig.adapter.connect())
        result = await rig.settle(rig.adapter.send_voice("12", str(note), caption="listen"))
        assert result.ok
        command = rig.wire.commands[-1]
        assert '"filePath": "' + str(note) in command and '"type": "voice"' in command
        media = rig.platform.ops_of("media")[0]
        assert media.extra["kind"] == "voice" and media.text == "listen"

    run(main)


def test_list_channels_names_contacts_and_allowed_groups() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.adapter.connect())
        rig.wire.contacts = {"12": "ana"}
        rig.platform.add_chat("group:300", "group", "ops")
        rig.platform.add_chat("group:301", "group", "other")
        found = await rig.settle(rig.adapter.list_channels())
        assert [(c.id, c.name, c.type) for c in found] == [
            ("12", "ana", "dm"),
            ("group:300", "ops", "group"),
        ]

    run(main)


# -- the socket ------------------------------------------------------------------------------------


def test_a_dropped_socket_reopens_after_2_4_8_doubling_to_60_seconds() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.adapter.connect())
        rig.wire.refuse = True
        rig.wire.drop()
        await rig.step(1.0, 200)
        gaps = [b - a for a, b in zip(rig.opened_at[1:], rig.opened_at[2:], strict=False)]
        assert rig.opened_at[1] - rig.opened_at[0] == pytest.approx(2.0, abs=1.0)
        assert gaps[:5] == pytest.approx([4.0, 8.0, 16.0, 32.0, 60.0], abs=1.0)
        rig.wire.refuse = False
        await rig.step(1.0, 70)
        assert rig.wire.socket is not None
        await rig.frames(items(dm_row("back")))
        assert [e.text for e in rig.events] == ["back"]

    run(main)


def test_a_silent_socket_is_probed_and_reopened_when_mute() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.adapter.connect())
        await rig.step(HEALTH_EVERY, int(STALE_AFTER // HEALTH_EVERY))
        assert rig.wire.commands.count("/user") == 1  # quiet but not yet stale
        await rig.step(HEALTH_EVERY)
        assert rig.wire.commands.count("/user") == 2 and len(rig.opened_at) == 1  # answered
        rig.wire.mute = True
        for _ in range(40):
            await rig.step(HEALTH_EVERY)
            if len(rig.opened_at) >= 2:
                break
        assert len(rig.opened_at) == 2  # the mute socket was given up and a new one opened
        rig.wire.mute = False
        await rig.step(1.0, 120)
        assert rig.wire.socket is not None
        await rig.frames(items(dm_row("after the probe")))
        assert [e.text for e in rig.events] == ["after the probe"]

    run(main)


def test_connect_rules() -> None:
    async def main(rig: Rig) -> None:
        remote = Rig(ws_url="ws://10.0.0.5:5225").adapter
        with pytest.raises(AdapterConnectError) as far:
            await remote.connect()
        assert not far.value.retryable and "allow_remote" in str(far.value)
        wrong = Rig(ws_url="http://127.0.0.1:5225").adapter
        with pytest.raises(AdapterConnectError):
            await wrong.connect()
        rig.wire.refuse = True
        with pytest.raises(AdapterConnectError) as down:
            await rig.settle(rig.adapter.connect())
        assert down.value.retryable and not rig.adapter.connected

    run(main)


def test_a_remote_daemon_is_allowed_on_request() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.adapter.connect())
        assert rig.adapter.connected and rig.adapter.user_name == "indus"

    run(main, ws_url="ws://10.0.0.5:5225", options=SimplexOptions(allow_remote=True))


def test_a_missing_websocket_library_is_a_fatal_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "httpx_ws", None)

    async def main() -> None:
        clock = FakeClock(datetime(2026, 3, 1, tzinfo=UTC))
        adapter = SimplexAdapter(clock=clock, ws_url=URL)
        with pytest.raises(AdapterConnectError) as missing:
            await adapter.connect()
        assert missing.value.code == "missing_dependency"
        assert "indus-agent[simplex]" in str(missing.value) and not missing.value.retryable

    asyncio.run(main())


def test_create_adapter_reads_the_url_and_options(scope: Scope) -> None:
    section = PlatformSection.model_validate({"extra": {"group_allowed_chats": ["*"]}})
    settings = PlatformSettings("simplex", True, "enabled in config", section)
    adapter = create_adapter(AdapterConfig("simplex", settings, scope, scope.clock, None))
    assert adapter.ws_url == "ws://127.0.0.1:5225" and adapter.options.group_chats == {"*"}
    secrets: Any = scope.secrets
    secrets.set("SIMPLEX_WS_URL", "ws://localhost:6000")
    config = AdapterConfig("simplex", settings, scope, scope.clock, None, send_only=True)
    one_shot = create_adapter(config)
    assert one_shot.ws_url == "ws://localhost:6000" and one_shot.send_only


# -- inbound files ---------------------------------------------------------------------------------


def test_files_are_fetched_lazily_from_the_files_folder(tmp_path: Path) -> None:
    folder = tmp_path / "files"
    folder.mkdir()

    async def main(rig: Rig) -> None:
        await rig.settle(rig.adapter.connect())
        rig.platform.voice("12", "12", "note.ogg")
        rig.wire.push_inbound()
        await rig.clock.run_until_idle()
        event = rig.events[-1]
        assert event.kind is MessageKind.VOICE and event.media[0].ref.startswith("simplex-file:")
        assert rig.wire.received == []  # nothing accepted before the intake asks

        async def fetch(attachment: Attachment) -> bytes:
            return b"".join([c async for c in rig.adapter.fetch_file(attachment)])

        data = await rig.settle(fetch(event.media[0]))
        assert data.startswith(b"OggS") and rig.wire.received == ["1"]
        with pytest.raises(MediaRejected):
            await rig.settle(fetch(Attachment(ref="simplex-file:../1")))
        rig.adapter._file_paths["77"] = str(tmp_path / "outside.txt")  # pyright: ignore[reportPrivateUsage]
        (tmp_path / "outside.txt").write_text("secret")
        with pytest.raises(MediaRejected):
            await rig.settle(fetch(Attachment(ref="simplex-file:77")))

    run(main, files_folder=folder, options=SimplexOptions(files_folder=folder))


def test_without_a_files_folder_nothing_is_read() -> None:
    async def main(rig: Rig) -> None:
        await rig.settle(rig.adapter.connect())
        with pytest.raises(MediaRejected) as refused:
            async for _ in rig.adapter.fetch_file(Attachment(ref="simplex-file:1")):
                pass
        assert "files_folder" in refused.value.reason
        undo = rig.adapter.register_fetchers(_Seat())
        assert undo is not None

    run(main)


class _Seat:
    def register_fetcher(self, platform: str, fetcher: Any) -> Callable[[], None]:
        return lambda: None
