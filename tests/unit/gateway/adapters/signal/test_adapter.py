"""The Signal adapter over the fake signal-cli daemon (P16-05)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.config.schema import PlatformSection
from indus.foundation.scope import Scope, make_scope
from indus.gateway.adapters.base import AdapterConnectError
from indus.gateway.adapters.signal import create_adapter
from indus.gateway.adapters.signal.adapter import (
    MAX_ATTACHMENT_BYTES,
    RPC_METHODS,
    SignalAdapter,
    mask_number,
    sniff_family,
)
from indus.gateway.config import AdapterConfig, PlatformSettings
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind
from indus.gateway.core.send import ErrorKind
from indus.gateway.intake.media import MediaRejected
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FailureKind
from tests.fakes.platform.signal_wire import ACCOUNT, RPC_SURFACE, SignalDaemon, uuid_for

URL = "http://127.0.0.1:8080"
ANA = "+15550000200"
BEN = "+15550000300"
GROUP = "group.R3JvdXAtQQ"


def _clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))


class Rig:
    def __init__(self, **kw: Any) -> None:
        self.clock = _clock()
        self.daemon = SignalDaemon()
        self.daemon.platform.clock = self.clock
        self.events: list[InboundEvent] = []
        kw.setdefault("group_chats", ())
        self.adapter = SignalAdapter(
            clock=self.clock,
            http_url=URL,
            account=ACCOUNT,
            transport=self.daemon.transport,
            rng=lambda: 0.0,
            **kw,
        )

        async def sink(event: InboundEvent) -> None:
            self.events.append(event)

        self.adapter.set_sink(sink)

    async def step(self, seconds: float = 1.0, times: int = 1) -> None:
        for _ in range(times):
            await self.clock.advance_and_settle(seconds)


def run(main: Callable[[Rig], Awaitable[None]], **kw: Any) -> None:
    async def outer() -> None:
        rig = Rig(**kw)
        try:
            await main(rig)
        finally:
            await rig.adapter.disconnect()

    asyncio.run(outer())


def envelope(sender: str = ANA, stamp: int = 1000, **data: Any) -> dict[str, Any]:
    return {
        "sourceNumber": sender,
        "sourceUuid": uuid_for(sender),
        "sourceName": "Ana",
        "timestamp": stamp,
        "dataMessage": {"timestamp": stamp, **data},
    }


# -- the stream ------------------------------------------------------------------------------------


def test_events_url_percent_encodes_the_account() -> None:
    async def main(rig: Rig) -> None:
        seen: list[str] = []
        handle = rig.daemon.handle

        async def spy(request: Any) -> Any:
            if request.url.path == "/api/v1/events":
                seen.append(str(request.url))
            return await handle(request)

        rig.daemon.transport.handler = spy  # pyright: ignore[reportAttributeAccessIssue]
        await rig.adapter.connect()
        await rig.step()
        assert seen and "account=%2B15550000999" in seen[0]

    run(main)


def test_a_dropped_stream_reopens_after_2_4_8_doubling_to_60_seconds() -> None:
    async def main(rig: Rig) -> None:
        rig.daemon.fail_streams = True
        await rig.adapter.connect()
        await rig.step(0.5, times=400)  # 200 s
        opens = rig.daemon.stream_opens
        gaps = [round(b - a, 2) for a, b in zip(opens, opens[1:], strict=False)]
        assert gaps[:7] == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]

    run(main)


def test_streams_that_open_and_close_at_once_also_back_off() -> None:
    async def main(rig: Rig) -> None:
        rig.daemon.end_streams_at_once = True
        await rig.adapter.connect()
        await rig.step(0.5, times=80)  # 40 s
        opens = rig.daemon.stream_opens
        gaps = [round(b - a, 2) for a, b in zip(opens, opens[1:], strict=False)]
        assert gaps[:3] == [2.0, 4.0, 8.0]

    run(main)


def test_a_stream_that_delivered_resets_the_wait() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        await rig.step()
        rig.daemon.drop_stream()
        await rig.step(0.25, times=40)
        assert len(rig.daemon.stream_opens) == 2
        rig.daemon.platform.dm(ANA, "hi")
        await rig.step(0.5, times=4)
        assert [e.text for e in rig.events] == ["hi"]
        dropped_at = rig.clock.monotonic()
        rig.daemon.drop_stream()
        await rig.step(0.25, times=40)
        opens = rig.daemon.stream_opens
        # The second stream delivered a message, so the wait starts over at 2 s (else 4 s).
        assert len(opens) == 3 and opens[2] - dropped_at <= 2.5

    run(main)


def test_a_quiet_stream_with_a_dead_daemon_is_reopened() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        await rig.step()
        assert len(rig.daemon.stream_opens) == 1
        rig.daemon.healthy = False
        await rig.step(10, times=16)  # 160 s of silence
        assert len(rig.daemon.checks) >= 2
        rig.daemon.healthy = True
        await rig.step(1, times=30)
        assert len(rig.daemon.stream_opens) >= 2

    run(main)


def test_a_quiet_stream_with_a_live_daemon_is_left_alone() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        await rig.step()
        await rig.step(10, times=30)  # 300 s of silence, daemon healthy
        assert len(rig.daemon.stream_opens) == 1
        assert len(rig.daemon.checks) >= 2  # connect + at least one stale check

    run(main)


def test_send_only_open_checks_the_daemon_and_opens_no_stream() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        await rig.step(10, times=20)
        assert rig.daemon.checks and rig.daemon.stream_opens == []
        result = await rig.adapter.send(ANA, "from a script")
        assert result.ok

    run(main, send_only=True)


def test_indus_send_builds_a_send_only_adapter_that_opens_no_stream(
    capsys: pytest.CaptureFixture[str], scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    from indus.gateway.delivery import send_command
    from tests.unit.cli.commands.cmdrun import run_cli

    daemon = SignalDaemon()
    secrets: Any = scope.secrets
    secrets.set("SIGNAL_ACCOUNT", ACCOUNT)
    secrets.set("SIGNAL_HTTP_URL", URL)
    built: list[SignalAdapter] = []
    real_build = send_command.build_adapter

    def build(*args: Any) -> Any:
        adapter = real_build(*args)  # the registry row and ``AdapterConfig(send_only=True)``
        assert isinstance(adapter, SignalAdapter)
        adapter._transport = daemon.transport  # pyright: ignore[reportPrivateUsage]
        built.append(adapter)
        return adapter

    monkeypatch.setattr(send_command, "ADAPTER_BUILDER", build)
    ran = run_cli(capsys, scope, "send", f"signal:{ANA}", "deploy done")
    assert ran.code == 0, ran.err
    assert built[0].send_only and not built[0].connected
    assert daemon.checks and daemon.stream_opens == []
    assert [c.params["message"] for c in daemon.calls("send")] == ["deploy done"]


def test_connect_errors() -> None:
    async def main(rig: Rig) -> None:
        rig.daemon.healthy = False
        with pytest.raises(AdapterConnectError) as down:
            await rig.adapter.connect()
        assert down.value.retryable
        empty = SignalAdapter(clock=rig.clock, http_url="", account=ACCOUNT)
        with pytest.raises(AdapterConnectError) as config:
            await empty.connect()
        assert not config.value.retryable and "SIGNAL_HTTP_URL" in str(config.value)
        bad = SignalAdapter(clock=rig.clock, http_url="ftp://x", account=ACCOUNT)
        with pytest.raises(AdapterConnectError):
            await bad.connect()

    run(main)


# -- inbound ---------------------------------------------------------------------------------------


def test_note_to_self_is_handled_and_our_own_echo_dropped() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        sent = await rig.adapter.send(ACCOUNT, "reply to myself")
        assert sent.ok and sent.message_id is not None
        echo = {
            "sourceNumber": ACCOUNT,
            "timestamp": int(sent.message_id),
            "syncMessage": {
                "sentMessage": {
                    "destinationNumber": ACCOUNT,
                    "timestamp": int(sent.message_id),
                    "message": "reply to myself",
                }
            },
        }
        note = {
            "sourceNumber": ACCOUNT,
            "timestamp": 77,
            "syncMessage": {
                "sentMessage": {"destinationNumber": ACCOUNT, "timestamp": 77, "message": "todo"}
            },
        }
        to_someone_else = {
            "sourceNumber": ACCOUNT,
            "timestamp": 78,
            "syncMessage": {
                "sentMessage": {"destinationNumber": ANA, "timestamp": 78, "message": "hey"}
            },
        }
        receipt = {"sourceNumber": ACCOUNT, "syncMessage": {"readMessages": []}}
        for item in (echo, note, to_someone_else, receipt):
            rig.daemon.push_envelope(item)
        await rig.step(times=3)
        assert [e.text for e in rig.events] == ["todo"]
        event = rig.events[0]
        assert event.source.chat_id == ACCOUNT and event.metadata.get("note_to_self") is True

    run(main)


def test_own_messages_stories_receipts_and_other_accounts_are_ignored() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        rig.daemon.push_envelope(envelope(ACCOUNT, message="loop"))
        rig.daemon.push_envelope({**envelope(ANA), "storyMessage": {}})
        rig.daemon.push_envelope({"sourceNumber": ANA, "receiptMessage": {"isRead": True}})
        rig.daemon.push_envelope(envelope(ANA, 5, message=""))  # profile key update
        rig.daemon.push_envelope({"timestamp": 1, "dataMessage": {"message": "no sender"}})
        rig.daemon._raw.append(  # pyright: ignore[reportPrivateUsage]
            {"envelope": envelope(ANA, message="not ours"), "account": "+15559999999"}
        )
        rig.daemon.push_envelope(envelope(ANA, 9, message="kept"))
        await rig.step(times=3)
        assert [e.text for e in rig.events] == ["kept"]

    run(main)


def test_groups_are_off_unless_listed() -> None:
    def group_message(text: str, gid: str) -> dict[str, Any]:
        return envelope(ANA, message=text, groupInfo={"groupId": gid, "groupName": "Team"})

    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        rig.daemon.push_envelope(group_message("unlisted", "T3RoZXI"))
        rig.daemon.push_envelope(group_message("listed", "R3JvdXAtQQ"))
        await rig.step(times=3)
        assert [e.text for e in rig.events] == ["listed"]
        source = rig.events[0].source
        assert (source.chat_id, source.chat_type, source.chat_name) == (GROUP, "group", "Team")

    run(main, group_chats=(GROUP,))


def test_mentions_of_us_set_mentioned_and_others_render_as_handles() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        mentions = [
            {"start": 3, "length": 1, "number": ACCOUNT},
            {"start": 14, "length": 1, "uuid": uuid_for(BEN)},
        ]
        # "😀" is two UTF-16 units: Signal's offsets count UTF-16, not code points.
        text = "😀 \ufffc /help to \ufffc"
        rig.daemon.push_envelope(
            envelope(ANA, message=text, mentions=mentions, groupInfo={"groupId": "R3JvdXAtQQ"})
        )
        await rig.step(times=3)
        event = rig.events[0]
        assert event.metadata.get("mentioned") is True
        assert event.text == f"😀 /help to @{uuid_for(BEN)}"

    run(main, group_chats=("*",))


def test_uuid_only_sender_and_alt_id() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        hidden = uuid_for("+15550000777")
        rig.daemon.push_envelope(
            {"sourceUuid": hidden, "timestamp": 3, "dataMessage": {"message": "private number"}}
        )
        rig.daemon.push_envelope(envelope(ANA, 4, message="with number"))
        await rig.step(times=3)
        first, second = rig.events
        assert first.source.chat_id == hidden and first.user_id == hidden
        assert first.source.user_id_alt is None
        assert second.user_id == ANA and second.source.user_id_alt == uuid_for(ANA)

    run(main)


def test_quotes_and_reply_context() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        mine = await rig.adapter.send(ANA, "an answer")
        assert mine.message_id is not None
        quote = {"id": int(mine.message_id), "authorNumber": ACCOUNT, "text": "an answer"}
        rig.daemon.push_envelope(envelope(ANA, 2000, message="why?", quote=quote))
        await rig.step(times=3)
        event = rig.events[0]
        assert event.reply_to_id == mine.message_id
        assert event.reply_to_is_own and event.reply_to_text == "an answer"
        # Replying to that inbound message quotes it.
        await rig.adapter.send(ANA, "because", reply_to="2000")
        params = rig.daemon.calls("send")[-1].params
        assert params["quoteTimestamp"] == 2000 and params["quoteAuthor"] == ANA

    run(main)


def test_edits_carry_the_edited_message_id() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        rig.daemon.push_envelope(
            {
                "sourceNumber": ANA,
                "timestamp": 51,
                "editMessage": {
                    "targetSentTimestamp": 50,
                    "dataMessage": {"timestamp": 51, "message": "fixed"},
                },
            }
        )
        await rig.step(times=3)
        event = rig.events[0]
        assert event.message_id == "50" and event.metadata.get("edited") is True

    run(main)


def test_attachments_are_listed_then_fetched_lazily_and_sniffed() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        rig.daemon.attachments["a1"] = b"\xff\xd8\xff\xe0jpeg"
        rig.daemon.attachments["fake"] = b"<html><body>not an image"
        attachments = [
            {"id": "a1", "contentType": "image/jpeg", "filename": "cat.jpg", "size": 8},
            {"id": "fake", "contentType": "image/png", "size": 24},
            {"id": "huge", "contentType": "video/mp4", "size": MAX_ATTACHMENT_BYTES + 1},
        ]
        rig.daemon.push_envelope(envelope(ANA, message="", attachments=attachments))
        await rig.step(times=3)
        event = rig.events[0]
        assert event.kind is MessageKind.PHOTO and len(event.media) == 2
        assert rig.daemon.calls("getAttachment") == []  # nothing downloaded before admission
        good = [chunk async for chunk in rig.adapter.fetch_attachment(event.media[0])]
        assert good == [b"\xff\xd8\xff\xe0jpeg"]
        assert rig.daemon.calls("getAttachment")[0].params["id"] == "a1"
        with pytest.raises(MediaRejected):
            async for _ in rig.adapter.fetch_attachment(event.media[1]):
                pass
        with pytest.raises(MediaRejected):
            async for _ in rig.adapter.fetch_attachment(Attachment(ref="/etc/passwd")):
                pass

    run(main)


def test_the_attachment_fetcher_feeds_the_intake(scope: Scope) -> None:
    from indus.gateway.extras import attach_fetcher
    from indus.gateway.intake.media import MediaIntake

    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        rig.daemon.attachments["n1"] = b"line one\nline two\n"
        note = {"id": "n1", "contentType": "text/plain", "filename": "notes.txt", "size": 18}
        rig.daemon.push_envelope(envelope(ANA, message="summarise", attachments=[note]))
        await rig.step(times=3)
        intake = MediaIntake(scope)
        undo = attach_fetcher(intake, rig.adapter)
        assert undo is not None
        text = await intake.enrich(rig.events[0])
        undo()
        assert "line one\nline two" in text and text.endswith("summarise")

    run(main)


def test_voice_notes_are_voice_messages() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        voice = {"id": "v1", "contentType": "audio/aac", "size": 9, "voiceNote": True}
        song = {"id": "s1", "contentType": "audio/mpeg", "size": 9}
        rig.daemon.push_envelope(envelope(ANA, 1, attachments=[voice]))
        rig.daemon.push_envelope(envelope(ANA, 2, attachments=[song]))
        await rig.step(times=3)
        assert [e.kind for e in rig.events] == [MessageKind.VOICE, MessageKind.AUDIO]

    run(main)


def test_sniffing() -> None:
    assert sniff_family(b"\x89PNG\r\n\x1a\n") == "image"
    assert sniff_family(b"OggS\x00") == "av"
    assert sniff_family(b"\xff\xf1\x50\x80") == "av"  # ADTS AAC (Android voice notes)
    assert sniff_family(b"%PDF-1.7") == "pdf"
    assert sniff_family(b"MZ\x90\x00") == "executable"
    assert sniff_family(b"  <!DOCTYPE html>") == "html"
    assert sniff_family(b"hello") == "unknown"


# -- outbound --------------------------------------------------------------------------------------


def test_send_ids_are_timestamps_and_long_text_is_split_by_utf16_units() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        text = ("😀" * 3999 + " ") * 3
        result = await rig.adapter.send(ANA, text)
        assert result.ok and len(result.ids) >= 3
        for call in rig.daemon.calls("send"):
            assert sum(2 if ord(c) > 0xFFFF else 1 for c in call.params["message"]) <= 8000
            assert call.params["recipient"] == [ANA]

    run(main)


def test_group_send_counts_as_sent_when_any_member_got_it() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        stamp = {"n": 900}

        async def handler(request: Any) -> Any:
            import httpx

            if request.url.path != "/api/v1/rpc":
                return await SignalDaemon.handle(rig.daemon, request)
            stamp["n"] += 1
            results = [{"type": "SUCCESS"}, {"type": "UNREGISTERED_FAILURE"}]
            if b"recipient" in request.content:
                results = [{"type": "UNREGISTERED_FAILURE"}]
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": "1",
                    "result": {"timestamp": stamp["n"], "results": results},
                },
            )

        rig.daemon.transport.handler = handler  # pyright: ignore[reportAttributeAccessIssue]
        group = await rig.adapter.send(GROUP, "to the team")
        assert group.ok and group.message_id == "901"
        dm = await rig.adapter.send(ANA, "to ana")
        assert not dm.ok and dm.kind is ErrorKind.NOT_FOUND and dm.chat_unreachable

    run(main)


def test_text_rate_limit_is_reported_with_its_wait_and_not_retried_here() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        rig.daemon.platform.fail(FailureKind.RATE_LIMIT, op="send", retry_after=9)
        result = await rig.adapter.send(ANA, "hello")
        assert result.kind is ErrorKind.RATE_LIMITED and result.retry_after == 9
        assert len(rig.daemon.calls("send")) == 1

    run(main)


def _files(tmp: Path, count: int) -> list[str]:
    paths: list[str] = []
    for n in range(count):
        path = tmp / f"f{n}.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n")
        paths.append(str(path))
    return paths


def test_33_attachments_go_out_in_two_sends(tmp_path: Path) -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        result = await rig.adapter.send_files(ANA, _files(tmp_path, 33), caption="pictures")
        sends = rig.daemon.calls("send")
        assert [len(c.params["attachments"]) for c in sends] == [32, 1]
        assert [c.params["message"] for c in sends] == ["pictures", ""]
        assert result.ok and len(result.ids) == 2

    run(main)


def test_oversized_or_missing_files_are_refused_without_a_call(tmp_path: Path) -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        big = tmp_path / "big.bin"
        with big.open("wb") as handle:
            handle.truncate(MAX_ATTACHMENT_BYTES + 1)
        result = await rig.adapter.send_document(ANA, str(big))
        missing = await rig.adapter.send_image(ANA, str(tmp_path / "nope.png"))
        assert not result.ok and not missing.ok
        assert rig.daemon.calls("send") == []

    run(main)


def test_an_attachment_rate_limit_is_retried_exactly_once_after_its_refill(
    tmp_path: Path,
) -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        limit = {"code": -5, "message": "Failed to send message due to rate limiting"}
        rig.daemon.script_error("send", limit, times=1)
        task = asyncio.ensure_future(rig.adapter.send_image(ANA, _files(tmp_path, 1)[0]))
        await rig.step(1, times=3)
        assert not task.done()  # sleeping the default 4 s refill
        await rig.step(1, times=2)
        result = await task
        sends = rig.daemon.calls("send")
        assert result.ok and len(sends) == 2
        assert sends[1].at - sends[0].at == pytest.approx(4.0, abs=1.0)
        # Two refusals in a row: one retry only, then the typed failure.
        rig.daemon.script_error("send", limit, times=2)
        task = asyncio.ensure_future(rig.adapter.send_image(ANA, _files(tmp_path, 1)[0]))
        await rig.step(1, times=6)
        result = await task
        assert result.kind is ErrorKind.RATE_LIMITED and len(rig.daemon.calls("send")) == 4

    run(main)


def test_a_long_attachment_wait_is_handed_over_without_sleeping(tmp_path: Path) -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        limit = {
            "code": -5,
            "message": "rate limited",
            "data": {"response": {"results": [{"retryAfterSeconds": 600}]}},
        }
        rig.daemon.script_error("send", limit)
        result = await rig.adapter.send_image(ANA, _files(tmp_path, 1)[0])
        assert result.is_flood and result.retry_after == 600
        assert len(rig.daemon.calls("send")) == 1

    run(main)


def test_numbers_are_upgraded_to_uuids_from_the_contact_list() -> None:
    async def main(rig: Rig) -> None:
        rig.daemon.contacts = [{"number": BEN, "uuid": uuid_for(BEN), "name": "Ben"}]
        await rig.adapter.connect()
        await rig.adapter.send(BEN, "one")
        await rig.adapter.send(ANA, "two")
        await rig.adapter.send(ANA, "three")
        recipients = [c.params["recipient"][0] for c in rig.daemon.calls("send")]
        assert recipients == [uuid_for(BEN), ANA, ANA]
        assert len(rig.daemon.calls("listContacts")) == 1
        info = await rig.adapter.chat_info(BEN)
        assert info.name == "Ben" and info.type == "dm"
        assert (await rig.adapter.chat_info(GROUP)).type == "group"

    run(main)


def test_typing_pauses_after_three_failures_in_a_row() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        rig.daemon.platform.fail(FailureKind.PARSE_ERROR, op="typing", times=10)
        for _ in range(5):
            await rig.adapter.typing_start(ANA)
        assert len(rig.daemon.calls("sendTyping")) == 3
        await rig.step(17)
        await rig.adapter.typing_start(ANA)
        assert len(rig.daemon.calls("sendTyping")) == 4
        await rig.adapter.typing_stop(ANA)
        assert rig.daemon.calls("sendTyping")[-1].params.get("stop") is True

    run(main)


def test_reactions() -> None:
    async def main(rig: Rig) -> None:
        await rig.adapter.connect()
        assert await rig.adapter.send_reaction(ANA, ANA, 1000, "👀")
        assert await rig.adapter.send_reaction(ANA, ANA, 1000, "👀", remove=True)
        calls = rig.daemon.calls("sendReaction")
        assert calls[0].params["targetTimestamp"] == 1000 and "remove" not in calls[0].params
        assert calls[1].params["remove"] is True

    run(main)


def test_every_rpc_method_is_served_by_the_fake() -> None:
    assert set(RPC_METHODS) <= set(RPC_SURFACE)


# -- secrets and config ----------------------------------------------------------------------------


def test_the_account_is_masked_in_reprs_logs_and_errors(caplog: pytest.LogCaptureFixture) -> None:
    async def main(rig: Rig) -> None:
        assert ACCOUNT not in repr(rig.adapter) and mask_number(ACCOUNT) in repr(rig.adapter)
        await rig.adapter.connect()
        await rig.step()
        assert rig.daemon.stream_opens  # the stream request carried the account to the daemon
        rig.daemon.script_error("send", {"code": -1, "message": f"bad account {ACCOUNT}"})
        result = await rig.adapter.send(ANA, "x")
        assert not result.ok and ACCOUNT not in (result.error or "")

    # httpx's own request log line too (the log setup pins it at WARNING; here it is on).
    for name in ("httpx", "httpcore"):
        caplog.set_level(logging.DEBUG, logger=name)
    with caplog.at_level(logging.DEBUG):
        run(main)
    assert "HTTP Request" in caplog.text and "/api/v1/events" in caplog.text
    assert ACCOUNT not in caplog.text and ACCOUNT[1:] not in caplog.text


def test_create_adapter_reads_the_secret_names_then_config(tmp_path: Path) -> None:
    env = {
        "SIGNAL_HTTP_URL": "http://127.0.0.1:9090",
        "SIGNAL_ACCOUNT": ACCOUNT,
        "INDUS_SIGNAL_GROUP_ALLOWED_CHATS": "group.QQ",
    }
    scope = make_scope(tmp_path / "home", env=env, clock=_clock())
    section = PlatformSection.model_validate(
        {"extra": {"group_allowed_chats": ["group.Qg"], "ignore_stories": False}}
    )
    settings = PlatformSettings("signal", True, "enabled in config", section, True)
    adapter = create_adapter(AdapterConfig("signal", settings, scope, scope.clock))
    assert isinstance(adapter, SignalAdapter)
    assert adapter.http_url == "http://127.0.0.1:9090" and adapter.account == ACCOUNT
    assert adapter.group_chats == {"group.QQ", "group.Qg"}
    assert adapter.ignore_stories is False
    from_config = PlatformSection.model_validate(
        {"extra": {"http_url": "http://10.0.0.5:8080", "account": BEN}}
    )
    bare = make_scope(tmp_path / "other", env={}, clock=_clock())
    other = create_adapter(
        AdapterConfig(
            "signal",
            PlatformSettings("signal", True, "enabled in config", from_config, True),
            bare,
            bare.clock,
        )
    )
    assert other.http_url == "http://10.0.0.5:8080" and other.account == BEN
