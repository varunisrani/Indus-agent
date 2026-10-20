"""The Matrix adapter over the fake homeserver (P16-06): the row's Verify cases and the rules the
shared contract suite does not reach."""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Awaitable, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.config.schema import PlatformSection
from indus.foundation.scope import Scope
from indus.gateway.adapters.matrix import create_adapter, split_target
from indus.gateway.adapters.matrix.adapter import (
    CHUNK_BYTES,
    MAX_CONTENT_BYTES,
    MatrixAdapter,
    MatrixOptions,
    utf8_bytes,
)
from indus.gateway.adapters.matrix.client import ROUTES as CLIENT_ROUTES
from indus.gateway.adapters.matrix.client import MatrixError, MediaTooLarge, compact_json
from indus.gateway.adapters.matrix.html import mentioned_users, to_html
from indus.gateway.config import AdapterConfig, PlatformSettings
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind
from indus.gateway.core.send import ErrorKind
from indus.gateway.core.session_keys import build_session_key
from indus.gateway.registry import PlatformRegistry
from tests.fakes.clock import FakeClock
from tests.fakes.platform.core import FailureKind
from tests.fakes.platform.matrix_wire import BOT, ROUTES, MatrixWire

ROOM = "!ops:example.org"
ANA = "@ana:example.org"
BEN = "@ben:example.org"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("real network used in a Matrix adapter test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield


def _clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))


async def drive[T](clock: FakeClock, awaitable: Awaitable[T], *, step: float = 0.5) -> T:
    task = asyncio.ensure_future(awaitable)
    for _ in range(4000):
        await clock.run_until_idle()
        if task.done():
            return task.result()
        clock.advance(step)
    task.cancel()
    raise AssertionError("never finished on the fake clock")


async def pump(clock: FakeClock, rounds: int = 4) -> None:
    for _ in range(rounds):
        await clock.advance_and_settle(1.0)


class Rig:
    def __init__(self, *, options: MatrixOptions | None = None, listen: bool = True) -> None:
        self.clock = _clock()
        self.wire = MatrixWire(clock=self.clock)
        self.wire.platform.add_chat(ANA, "dm", "Ana")
        self.adapter = self.wire.adapter(options=options)
        self.events: list[InboundEvent] = []
        if listen:
            self.adapter.set_sink(self._sink)

    async def _sink(self, event: InboundEvent) -> None:
        self.events.append(event)

    async def connect(self) -> None:
        await drive(self.clock, self.adapter.connect())
        await pump(self.clock, 1)

    async def close(self) -> None:
        await drive(self.clock, self.adapter.disconnect())


def run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(coro)  # pyright: ignore[reportArgumentType]


# -- the row's Verify ------------------------------------------------------------------------------


def test_backlog_event_at_startup_is_ignored() -> None:
    async def main() -> None:
        rig = Rig()
        rig.wire.platform.group(ROOM, ANA, "old news", mention=True)
        rig.clock.advance(10.0)  # sent ten seconds before the listener starts
        await rig.connect()
        rig.wire.platform.group(ROOM, ANA, "fresh", mention=True)
        await pump(rig.clock)
        assert [e.text for e in rig.events] == ["fresh"]
        await rig.close()

    run(main())


def test_an_event_inside_the_grace_window_is_kept() -> None:
    async def main() -> None:
        rig = Rig()
        rig.wire.platform.group(ROOM, ANA, "just before", mention=True)
        rig.clock.advance(4.0)
        await rig.connect()
        await pump(rig.clock)
        assert [e.text for e in rig.events] == ["just before"]
        await rig.close()

    run(main())


def test_thread_reply_keys_match_the_thread() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        p = rig.wire.platform
        p.thread(ROOM, "$root", ANA, "first in thread")
        p.thread(ROOM, "$root", BEN, "second in thread")
        p.group(ROOM, ANA, "top level", mention=True)
        await pump(rig.clock)
        first, second, top = rig.events
        for event in (first, second):
            assert event.source.chat_type == "thread"
            assert event.source.thread_id == "$root"
            assert event.source.parent_chat_id == ROOM
            assert event.reply_to_id is None  # the thread fallback is not a chosen reply
        assert build_session_key(first.source) == build_session_key(second.source)
        assert build_session_key(top.source) != build_session_key(first.source)
        # The answer goes back into the thread.
        result = await drive(
            rig.clock, rig.adapter.send(ROOM, "on it", meta={"thread_id": "$root"})
        )
        assert result.ok
        relates = rig.wire.sent[-1].content["m.relates_to"]
        assert relates == {
            "rel_type": "m.thread",
            "event_id": "$root",
            "is_falling_back": True,
            "m.in_reply_to": {"event_id": "$root"},
        }
        assert p.message(result.message_id or "").thread_id == "$root"  # pyright: ignore[reportOptionalMemberAccess]
        await rig.close()

    run(main())


def test_a_reply_inside_a_thread_is_not_a_fallback() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        await drive(
            rig.clock, rig.adapter.send(ROOM, "ok", reply_to="$7", meta={"thread_id": "$r"})
        )
        relates = rig.wire.sent[-1].content["m.relates_to"]
        assert relates["is_falling_back"] is False
        assert relates["m.in_reply_to"] == {"event_id": "$7"}
        await rig.close()

    run(main())


def test_a_46000_byte_reply_is_split_by_bytes_as_well_as_chars() -> None:
    async def main() -> None:
        rig = Rig(listen=False)
        await rig.connect()
        words: list[str] = []
        n = 0
        while utf8_bytes(" ".join(words)) < 46_000:
            n += 1
            words.append(f"wört{n}€😀")  # 2-, 3- and 4-byte characters
        text = " ".join(words)
        assert len(text) < CHUNK_BYTES * 3  # by characters alone it would be fewer pieces
        result = await drive(rig.clock, rig.adapter.send(ROOM, text))
        assert result.ok
        bodies = [s.content["body"] for s in rig.wire.sent]
        assert len(bodies) == len(result.ids) >= 4
        for sent in rig.wire.sent:
            assert utf8_bytes(sent.content["body"]) <= CHUNK_BYTES
            assert len(sent.content["body"]) <= CHUNK_BYTES
            assert utf8_bytes(compact_json(sent.content)) <= MAX_CONTENT_BYTES
        kept = [w for b in bodies for w in b.split() if not w.startswith("(")]
        assert kept == words
        await rig.close()

    run(main())


def test_html_that_would_burst_the_event_is_left_out() -> None:
    async def main() -> None:
        rig = Rig(listen=False)
        await rig.connect()
        heavy = "**" + "&<" * 7000 + "**"  # escaping makes the HTML far larger than the text
        assert utf8_bytes(heavy) <= CHUNK_BYTES
        result = await drive(rig.clock, rig.adapter.send(ROOM, heavy))
        assert result.ok
        content = rig.wire.sent[-1].content
        assert "formatted_body" not in content and content["body"] == heavy
        assert utf8_bytes(compact_json(content)) <= MAX_CONTENT_BYTES
        await rig.close()

    run(main())


# -- outbound --------------------------------------------------------------------------------------


def test_text_goes_with_html_and_intentional_mentions() -> None:
    async def main() -> None:
        rig = Rig(listen=False)
        await rig.connect()
        await drive(rig.clock, rig.adapter.send(ROOM, f"**done**, {BEN} — see `{ANA}` @room"))
        content = rig.wire.sent[-1].content
        assert content["msgtype"] == "m.text"
        assert content["format"] == "org.matrix.custom.html"
        assert "<strong>done</strong>" in content["formatted_body"]
        assert f'href="https://matrix.to/#/{BEN}"' in content["formatted_body"]
        # Only the user named outside code is notified, and never the whole room.
        assert content["m.mentions"] == {"user_ids": [BEN]}
        plain = await drive(rig.clock, rig.adapter.send(ROOM, "*x*", meta={"plain": True}))
        assert plain.ok and "formatted_body" not in rig.wire.sent[-1].content
        await rig.close()

    run(main())


def test_room_mentions_only_when_allowed() -> None:
    async def main() -> None:
        rig = Rig(listen=False, options=MatrixOptions(allow_room_mentions=True))
        await rig.connect()
        await drive(rig.clock, rig.adapter.send(ROOM, "@room the build is red"))
        assert rig.wire.sent[-1].content["m.mentions"] == {"room": True}
        await rig.close()

    run(main())


def test_edits_use_m_replace_and_deletes_redact() -> None:
    async def main() -> None:
        rig = Rig(listen=False)
        await rig.connect()
        sent = await drive(rig.clock, rig.adapter.send(ROOM, "draft"))
        assert sent.message_id
        edited = await drive(rig.clock, rig.adapter.edit(ROOM, sent.message_id, "**final**"))
        assert edited.ok and edited.message_id == sent.message_id
        content = rig.wire.sent[-1].content
        assert content["m.relates_to"] == {"rel_type": "m.replace", "event_id": sent.message_id}
        assert content["m.new_content"]["body"] == "**final**"
        assert content["body"] == "* **final**" and content["m.mentions"] == {}
        assert rig.wire.platform.message(sent.message_id).text == "**final**"  # pyright: ignore[reportOptionalMemberAccess]
        assert await drive(rig.clock, rig.adapter.delete(ROOM, sent.message_id))
        assert rig.wire.platform.message(sent.message_id).deleted  # pyright: ignore[reportOptionalMemberAccess]
        await rig.close()

    run(main())


def test_typing_turns_on_and_off() -> None:
    async def main() -> None:
        rig = Rig(listen=False)
        await rig.connect()
        await drive(rig.clock, rig.adapter.typing_start(ROOM))
        await drive(rig.clock, rig.adapter.typing_stop(ROOM))
        assert rig.wire.typing == [(ROOM, True), (ROOM, False)]
        await rig.close()

    run(main())


def test_targets_resolve_aliases_and_users() -> None:
    async def main() -> None:
        rig = Rig(listen=False)
        rig.wire.aliases["#ops:example.org"] = ROOM
        await rig.connect()
        by_alias = await drive(rig.clock, rig.adapter.send("#ops:example.org", "via alias"))
        assert by_alias.ok and rig.wire.sent[-1].room == ROOM
        by_user = await drive(rig.clock, rig.adapter.send(ANA, "via m.direct"))
        assert by_user.ok and rig.wire.sent[-1].room == "!dm.ana:example.org"
        assert rig.wire.platform.texts(ANA) == ["via m.direct"]
        nobody = await drive(rig.clock, rig.adapter.send("@nobody:example.org", "x"))
        assert not nobody.ok and nobody.kind is ErrorKind.NOT_FOUND and nobody.chat_unreachable
        unknown = await drive(rig.clock, rig.adapter.send("#gone:example.org", "x"))
        assert not unknown.ok and unknown.kind is ErrorKind.NOT_FOUND
        await rig.close()

    run(main())


def test_split_target_keeps_room_colons() -> None:
    assert split_target("!ops:example.org") == ("!ops:example.org", None)
    assert split_target("#ops:example.org:$root") == ("#ops:example.org", "$root")
    assert split_target(" @ana:example.org ") == ("@ana:example.org", None)


def test_media_upload_then_event(tmp_path: Path) -> None:
    async def main() -> None:
        rig = Rig(listen=False)
        await rig.connect()
        path = tmp_path / "report.pdf"
        path.write_bytes(b"%PDF-1.7 fake")
        result = await drive(
            rig.clock, rig.adapter.send_document(ROOM, str(path), caption="the report")
        )
        voice = await drive(rig.clock, rig.adapter.send_voice(ROOM, str(path)))
        assert result.ok and voice.ok
        doc, note = rig.wire.sent[-2].content, rig.wire.sent[-1].content
        assert doc["msgtype"] == "m.file" and doc["body"] == "the report"
        assert doc["filename"] == "report.pdf" and doc["url"].startswith("mxc://example.org/")
        assert doc["info"] == {"mimetype": "application/pdf", "size": 13}
        uploaded = doc["url"].rsplit("/", 1)[1]
        assert rig.wire.media[uploaded][0] == b"%PDF-1.7 fake"
        assert note["msgtype"] == "m.audio" and "org.matrix.msc3245.voice" in note
        await rig.close()

    run(main())


def test_media_over_the_cap_is_refused_before_upload(tmp_path: Path) -> None:
    async def main() -> None:
        rig = Rig(listen=False, options=MatrixOptions(max_media_bytes=4))
        await rig.connect()
        big = tmp_path / "big.bin"
        big.write_bytes(b"12345")
        result = await drive(rig.clock, rig.adapter.send_file(ROOM, str(big)))
        assert not result.ok and "too large" in (result.error or "")
        assert not any(path.endswith("/upload") for _, path in rig.wire.requests)
        await rig.close()

    run(main())


# -- inbound ---------------------------------------------------------------------------------------


def test_dm_detection_by_m_direct_and_member_count() -> None:
    async def main() -> None:
        rig = Rig()
        rig.wire.member_counts["!dm.ana:example.org"] = 5  # m.direct alone still makes it a DM
        rig.wire.member_counts["!pair:example.org"] = 2
        await rig.connect()
        rig.wire.platform.dm(ANA, "hi")
        rig.wire.push_event(
            "!pair:example.org", rig.wire.event(BEN, {"msgtype": "m.text", "body": "yo"})
        )
        rig.wire.platform.group(ROOM, BEN, "group talk")
        await pump(rig.clock)
        kinds = {e.text: e.source.chat_type for e in rig.events}
        assert kinds == {"hi": "dm", "yo": "dm", "group talk": "group"}
        await rig.close()

    run(main())


def test_mentions_are_detected_and_removed() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        w = rig.wire
        w.push_event(ROOM, w.event(ANA, {"msgtype": "m.text", "body": f"{BOT}: status?"}))
        w.push_event(ROOM, w.event(ANA, {"msgtype": "m.text", "body": "hey @indus deploy"}))
        w.push_event(
            ROOM,
            w.event(
                ANA,
                {
                    "msgtype": "m.text",
                    "body": "Indus Bot: ping",
                    "formatted_body": '<a href="https://matrix.to/#/@indus:example.org">x</a>',
                },
            ),
        )
        w.push_event(ROOM, w.event(ANA, {"msgtype": "m.text", "body": "indusbot is a word"}))
        await pump(rig.clock)
        mentioned = [(e.text, e.metadata.get("mentioned", False)) for e in rig.events]
        assert mentioned == [
            ("status?", True),
            ("hey deploy", True),
            ("Indus Bot: ping", True),
            ("indusbot is a word", False),
        ]
        await rig.close()

    run(main())


def test_reply_fallback_is_cut_and_a_reply_to_us_counts_as_a_mention() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        ours = await drive(rig.clock, rig.adapter.send(ROOM, "the answer"))
        assert ours.message_id
        w = rig.wire
        body = f"> <{BOT}> the answer\n> second line\n\nthanks!"
        w.push_event(
            ROOM,
            w.event(
                ANA,
                {
                    "msgtype": "m.text",
                    "body": body,
                    "m.relates_to": {"m.in_reply_to": {"event_id": ours.message_id}},
                },
            ),
        )
        await pump(rig.clock)
        (event,) = rig.events
        assert event.text == "thanks!"
        assert event.reply_to_id == ours.message_id and event.reply_to_is_own
        assert event.reply_to_text == "the answer\nsecond line"
        assert event.reply_to_author_id == BOT
        assert event.reply_to_author_name == "indus"
        assert event.metadata.get("mentioned") is True
        await rig.close()

    run(main())


def test_dropped_senders_and_events() -> None:
    async def main() -> None:
        options = MatrixOptions.read({"ignore_user_patterns": ["^@spam"]}, {})
        rig = Rig(options=options)
        await rig.connect()
        w = rig.wire
        text = {"msgtype": "m.text", "body": "x"}
        w.push_event(ROOM, w.event(BOT.upper(), text))  # our own echo, any case
        w.push_event(ROOM, w.event("@_telegram_42:example.org", text))  # a bridge ghost
        w.push_event(ROOM, w.event("@spammer:example.org", text))
        w.push_event(ROOM, w.event(ANA, {"msgtype": "m.location", "body": "geo"}))
        repeated = w.event(ANA, {"msgtype": "m.text", "body": "once"})
        w.push_event(ROOM, repeated)
        w.push_event(ROOM, repeated)
        await pump(rig.clock)
        assert [e.text for e in rig.events] == ["once"]
        await rig.close()

    run(main())


def test_notices_are_bots_and_bang_commands_become_slash_commands() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        w = rig.wire
        w.push_event(ROOM, w.event(BEN, {"msgtype": "m.notice", "body": "build passed"}))
        w.push_event(ROOM, w.event(ANA, {"msgtype": "m.text", "body": "!help me"}))
        w.push_event(ROOM, w.event(ANA, {"msgtype": "m.text", "body": "!wow nice"}))
        await pump(rig.clock)
        notice, command, plain = rig.events
        assert notice.source.is_bot and not command.source.is_bot
        assert command.command == "help" and command.text == "/help me"
        assert plain.text == "!wow nice" and plain.command is None
        await rig.close()

    run(main())


def test_allowed_rooms_drop_other_rooms_but_not_dms() -> None:
    async def main() -> None:
        rig = Rig(options=MatrixOptions(allowed_rooms=frozenset({"!yes:example.org"})))
        await rig.connect()
        p = rig.wire.platform
        p.group("!yes:example.org", ANA, "allowed")
        p.group(ROOM, ANA, "not allowed")
        p.dm(ANA, "dm is exempt")
        await pump(rig.clock)
        assert sorted(e.text for e in rig.events) == ["allowed", "dm is exempt"]
        await rig.close()

    run(main())


def test_auto_thread_makes_room_messages_thread_roots() -> None:
    async def main() -> None:
        rig = Rig(options=MatrixOptions(auto_thread=True))
        await rig.connect()
        rig.wire.platform.group(ROOM, ANA, "start", mention=True)
        rig.wire.platform.dm(ANA, "dm stays flat")
        await pump(rig.clock)
        room, dm = rig.events
        assert room.source.chat_type == "thread" and room.source.thread_id == room.message_id
        assert dm.source.chat_type == "dm" and dm.source.thread_id is None
        await rig.close()

    run(main())


def test_media_events_and_the_fetcher() -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        p = rig.wire.platform
        p.photo(ANA, ANA, "cat.png", caption="look")
        p.voice(ANA, ANA, "note.ogg", duration=2.5)
        w = rig.wire
        w.push_event(
            ROOM,
            w.event(ANA, {"msgtype": "m.image", "body": "x.png", "url": "https://evil.example/x"}),
        )
        w.push_event(
            ROOM,
            w.event(
                ANA,
                {
                    "msgtype": "m.file",
                    "body": "huge.bin",
                    "url": "mxc://example.org/huge",
                    "info": {"size": 10**10},
                },
            ),
        )
        await pump(rig.clock)
        photo, voice = rig.events
        assert photo.kind is MessageKind.PHOTO and photo.text == "look"
        assert voice.kind is MessageKind.VOICE and voice.text == ""
        assert voice.metadata["duration"] == 2.5
        chunks = [c async for c in rig.adapter.fetch_media(photo.media[0])]
        assert b"".join(chunks) == b"bytes of cat.png"
        registered: dict[str, Any] = {}

        class Intake:
            def register_fetcher(self, platform: str, fetcher: Any) -> Any:
                registered[platform] = fetcher
                return lambda: None

        undo = rig.adapter.register_fetchers(Intake())
        assert undo is not None and set(registered) == {"matrix"}
        await rig.close()

    run(main())


def test_fetcher_caps_the_size_and_falls_back_to_the_legacy_route() -> None:
    async def main() -> None:
        rig = Rig(listen=False, options=MatrixOptions(max_media_bytes=8))
        rig.wire.media["small"] = (b"tiny", "text/plain")
        rig.wire.media["large"] = (b"far too large", "text/plain")
        rig.wire.legacy_media_only = True
        await rig.connect()
        small = [
            c async for c in rig.adapter.fetch_media(Attachment(ref="mxc://example.org/small"))
        ]
        assert small == [b"tiny"]
        assert any(p.startswith("/_matrix/media/v3/download/") for _, p in rig.wire.requests)
        with pytest.raises(MediaTooLarge):
            _ = [
                c async for c in rig.adapter.fetch_media(Attachment(ref="mxc://example.org/large"))
            ]
        with pytest.raises(MatrixError):
            _ = [c async for c in rig.adapter.fetch_media(Attachment(ref="https://x.example/y"))]
        await rig.close()

    run(main())


def test_encrypted_rooms_are_skipped_with_one_warning(caplog: pytest.LogCaptureFixture) -> None:
    async def main() -> None:
        rig = Rig()
        await rig.connect()
        w = rig.wire
        for _ in range(3):
            w.push_event(
                ROOM,
                w.event(ANA, {"algorithm": "m.megolm.v1.aes-sha2"}, event_type="m.room.encrypted"),
            )
        await pump(rig.clock)
        assert rig.events == []
        await rig.close()

    with caplog.at_level(logging.WARNING, logger="indus.gateway.adapters.matrix.adapter"):
        run(main())
    assert sum("encrypted" in r.getMessage() for r in caplog.records) == 1


def test_invites_join_only_from_allowed_users() -> None:
    async def main() -> None:
        options = MatrixOptions.read({}, {"INDUS_MATRIX_ALLOWED_USERS": ANA})
        rig = Rig(options=options)
        await rig.connect()
        rig.wire.invite("!new:example.org", ANA, direct=True)
        rig.wire.invite("!spam:example.org", "@stranger:example.org")
        await pump(rig.clock)
        assert rig.wire.joined == ["!new:example.org"]
        assert "!new:example.org" in rig.wire.account_data["m.direct"][ANA]
        await rig.close()

    run(main())


# -- lifecycle, errors, secrets --------------------------------------------------------------------


def test_send_only_open_never_syncs_and_the_sink_starts_the_loop() -> None:
    async def main() -> None:
        rig = Rig(listen=False)
        await rig.connect()
        result = await drive(rig.clock, rig.adapter.send(ROOM, "deploy done"))
        assert result.ok and rig.wire.sync_paths() == [] and not rig.adapter.listening
        rig.adapter.set_sink(rig._sink)
        await pump(rig.clock, 1)
        assert rig.adapter.listening and rig.wire.sync_paths()
        rig.adapter.set_sink(None)
        await pump(rig.clock, 1)
        assert not rig.adapter.listening
        await rig.close()

    run(main())


def test_a_send_only_adapter_never_syncs_even_with_a_sink() -> None:
    async def main() -> None:
        clock = _clock()
        wire = MatrixWire(clock=clock)
        adapter = wire.adapter(send_only=True)
        events: list[InboundEvent] = []

        async def sink(event: InboundEvent) -> None:
            events.append(event)

        adapter.set_sink(sink)
        await drive(clock, adapter.connect())
        adapter.set_sink(sink)
        await pump(clock, 3)
        result = await drive(clock, adapter.send(ROOM, "deploy done"))
        assert result.ok and wire.sync_paths() == [] and not adapter.listening
        assert events == []
        await drive(clock, adapter.disconnect())

    run(main())


def test_indus_send_over_the_wire_does_no_inbound_work(
    capsys: pytest.CaptureFixture[str], scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    from indus.cron import targets
    from indus.gateway.delivery import send_command
    from tests.unit.cli.commands.cmdrun import run_cli, write_settings

    write_settings(scope, "platforms:\n  matrix:\n    enabled: true\n")
    wire = MatrixWire(clock=scope.clock)  # pyright: ignore[reportArgumentType]
    secrets: Any = scope.secrets
    secrets.set("MATRIX_ACCESS_TOKEN", wire.token)
    secrets.set("MATRIX_HOMESERVER", "https://matrix.example.org")
    built: list[MatrixAdapter] = []
    real_build = send_command.build_adapter

    def factory(config: AdapterConfig) -> MatrixAdapter:
        adapter = create_adapter(config, transport=wire.transport())
        built.append(adapter)
        return adapter

    def build(build_scope: Scope, name: str, settings: PlatformSettings, *_: Any) -> Any:
        # The real builder (``AdapterConfig(send_only=True)``) over the real registry row.
        row = PlatformRegistry().get("matrix")
        assert row is not None
        return real_build(
            build_scope, name, settings, PlatformRegistry([replace(row, factory=factory)])
        )

    monkeypatch.setattr(send_command, "ADAPTER_BUILDER", build)
    undo = targets.register_platform("matrix", splitter=split_target)
    try:
        ran = run_cli(capsys, scope, "send", "matrix:!ops:example.org", "deploy done")
    finally:
        undo()
    assert ran.code == 0, ran.err
    assert wire.platform.texts(ROOM) == ["deploy done"]
    assert wire.sync_paths() == []
    assert [op.kind for op in wire.platform.ops] == ["connect", "send", "disconnect"]
    assert built[0].send_only and not built[0].listening


def test_connect_failures_are_classified() -> None:
    async def main() -> None:
        clock = _clock()
        wire = MatrixWire(clock=clock)
        bad = MatrixAdapter(
            clock=clock,
            homeserver="https://matrix.example.org",
            token="wrong",
            transport=wire.transport(),
        )
        with pytest.raises(Exception) as refused:
            await drive(clock, bad.connect())
        assert getattr(refused.value, "retryable", True) is False
        assert getattr(refused.value, "code", "") == "auth_failed"
        unset = MatrixAdapter(clock=clock, homeserver="", token="", transport=wire.transport())
        with pytest.raises(Exception) as missing:
            await drive(clock, unset.connect())
        assert "MATRIX_HOMESERVER and MATRIX_ACCESS_TOKEN" in str(missing.value)
        assert getattr(missing.value, "retryable", True) is False

    run(main())


def test_required_e2ee_without_the_module_is_fatal_with_a_hint() -> None:
    async def main() -> None:
        rig = Rig(listen=False, options=MatrixOptions(e2ee="required"))
        with pytest.raises(Exception) as failed:
            await drive(rig.clock, rig.adapter.connect())
        assert getattr(failed.value, "code", "") == "e2ee_unavailable"
        assert "matrix-e2ee" in str(failed.value)
        optional = Rig(listen=False, options=MatrixOptions(e2ee="optional"))
        await optional.connect()
        assert optional.adapter.connected
        await optional.close()

    run(main())


def test_send_error_readings() -> None:
    async def main() -> None:
        rig = Rig(listen=False)
        await rig.connect()
        rig.wire.platform.fail(FailureKind.RATE_LIMIT, op="send", retry_after=3)
        limited = await drive(rig.clock, rig.adapter.send(ROOM, "x"))
        assert limited.kind is ErrorKind.RATE_LIMITED and limited.retry_after == 3
        rig.wire.token = "rotated"  # the server no longer knows our token
        refused = await drive(rig.clock, rig.adapter.send(ROOM, "x"))
        assert not refused.ok and not refused.chat_unreachable
        assert rig.adapter.is_final_failure(refused)
        await rig.close()

    run(main())


def test_the_sync_loop_backs_off_and_stops_on_a_refused_token() -> None:
    async def main() -> None:
        import httpx

        rig = Rig(listen=False)
        await rig.connect()
        lost: list[tuple[str, str]] = []
        rig.adapter.set_fatal_listener(lambda code, message: lost.append((code, message)))
        rig.wire.sync_errors = [
            httpx.ConnectError("down"),
            httpx.Response(502, json={}),
            httpx.Response(401, json={"errcode": "M_UNKNOWN_TOKEN", "error": "gone"}),
        ]
        start = rig.clock.monotonic()
        rig.adapter.set_sink(rig._sink)
        times: list[float] = []
        for _ in range(20):
            await rig.clock.advance_and_settle(0.5)
            count = len(rig.wire.sync_paths())
            while len(times) < count:
                times.append(rig.clock.monotonic() - start)
        assert len(times) == 3
        assert 1.9 <= times[1] - times[0] <= 2.6 and 3.9 <= times[2] - times[1] <= 4.6
        assert [code for code, _ in lost] == ["matrix_auth"]
        assert "refused the access token" in lost[0][1]
        assert not rig.adapter.listening
        await rig.close()

    run(main())


def test_the_token_never_shows_in_logs_errors_or_repr(caplog: pytest.LogCaptureFixture) -> None:
    async def main() -> str:
        rig = Rig(listen=False)
        await rig.connect()
        token = rig.wire.token
        assert token not in repr(rig.adapter) and token not in repr(rig.adapter._client)
        rig.wire.platform.fail(FailureKind.FORBIDDEN, op="send")
        failed = await drive(rig.clock, rig.adapter.send(ROOM, f"x {token}"))
        assert token not in (failed.error or "")
        await rig.close()
        return token

    with caplog.at_level(logging.DEBUG):
        token = run(main())
    assert all(token not in r.getMessage() for r in caplog.records)


def test_create_adapter_reads_settings_and_secrets(scope: Scope) -> None:
    secrets: Any = scope.secrets
    secrets.set("MATRIX_ACCESS_TOKEN", "t" * 12)
    section = PlatformSection.model_validate(
        {
            "homeserver": "https://hs.example.org/",
            "auto_thread": True,
            "allowed_rooms": "!a:x, !b:x",
        }
    )
    settings = PlatformSettings("matrix", True, "enabled in config", section)
    adapter = create_adapter(AdapterConfig("matrix", settings, scope, scope.clock, None))
    assert adapter.options.auto_thread and adapter.options.allowed_rooms == {"!a:x", "!b:x"}
    assert repr(adapter) == "<MatrixAdapter https://hs.example.org disconnected>"
    assert adapter.scrub("t" * 12) != "t" * 12


# -- the client and the HTML -----------------------------------------------------------------------


def test_the_wire_fake_serves_every_client_route() -> None:
    assert dict(CLIENT_ROUTES) == ROUTES
    assert set(
        MatrixAdapter.vendor_names["indus.gateway.adapters.matrix.client:MatrixClient"]
    ) <= set(CLIENT_ROUTES)


def test_html_escapes_and_keeps_only_safe_links() -> None:
    html = to_html("<script>x</script> [a](javascript:alert(1)) [b](https://ok.example/?q=1&r=2)")
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert 'href="javascript' not in html
    assert '<a href="https://ok.example/?q=1&amp;r=2">b</a>' in html
    block = to_html("# Title\n\n- one\n- two\n\n```py\nprint('<x>')\n```\n> quoted")
    assert block == (
        "<h1>Title</h1><ul><li>one</li><li>two</li></ul>"
        "<pre><code class=\"language-py\">print('&lt;x&gt;')</code></pre>"
        "<blockquote>quoted</blockquote>"
    )
    assert to_html("line one\nline two") == "line one<br>line two"
    assert mentioned_users(f"hi {ANA} and `{BEN}` and ```\n{BEN}\n``` {ANA}") == [ANA]


@pytest.mark.parametrize("strict", [False, True])
def test_a_thread_the_bot_takes_part_in_needs_no_new_mention(strict: bool) -> None:
    async def main() -> None:
        rig = Rig(options=MatrixOptions(thread_require_mention=strict))
        await rig.connect()
        p = rig.wire.platform
        p.thread(ROOM, "$other", ANA, "a thread without us")
        await drive(rig.clock, rig.adapter.send(ROOM, "joining", meta={"thread_id": "$root"}))
        p.thread(ROOM, "$root", ANA, "follow-up without a mention")
        await pump(rig.clock)
        other, ours = rig.events
        assert other.metadata.get("mentioned") is None
        assert ours.metadata.get("mentioned") is (None if strict else True)
        await rig.close()

    run(main())
