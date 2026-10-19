"""P15-33: Telegram specifics over the wire fake (the shared contract runs in tests/contract)."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from indus.foundation.scope import Scope
from indus.gateway.adapters.base import AdapterConnectError, ApprovalAction, ApprovalPrompt
from indus.gateway.adapters.telegram import BOT_FACTORY, TelegramAdapter
from indus.gateway.adapters.telegram import __name__ as telegram_package
from indus.gateway.adapters.telegram.adapter import LOBBY_TEXT, TelegramOptions
from indus.gateway.adapters.telegram.bot_api import (
    BadRequest,
    BotApi,
    BotCommand,
    Chat,
    Entity,
    Message,
    NetworkError,
    Unauthorized,
    Update,
    User,
)
from indus.gateway.adapters.telegram.inbound import (
    Normaliser,
    UpdateDispatcher,
    UpdateReceipts,
    _canonical_command_text,  # pyright: ignore[reportPrivateUsage]
    menu_commands,
)
from indus.gateway.adapters.telegram.webhook import (
    SECRET_HEADER,
    UvicornServer,
    WebhookOptions,
    webhook_app,
)
from indus.gateway.config import AdapterConfig, PlatformSettings, load_gateway_settings
from indus.gateway.core.busy import canonical_command
from indus.gateway.core.events import Attachment, InboundEvent, MessageKind
from indus.gateway.core.send import ErrorKind
from indus.gateway.core.session_keys import build_session_key
from indus.gateway.delivery import send_command
from indus.gateway.extras import attach_fetcher
from indus.gateway.intake.media import MediaIntake
from indus.gateway.outbound.chunker import utf16_units
from indus.gateway.registry import PlatformRegistry
from indus.service.commands.registry import CommandDef, CommandRegistry
from tests.fakes.clock import FakeClock, checkpoint
from tests.fakes.model import text_turn, tool_batch
from tests.fakes.platform.core import FailureKind, FakeChatPlatform
from tests.fakes.platform.telegram_wire import FakeBot, TelegramWire
from tests.unit.cli.commands.cmdrun import run_cli, write_settings
from tests.unit.gateway.core import _kit
from tests.unit.gateway.core._kit import ALLOWED, build_kit
from tests.unit.gateway.core.test_prompts import Outcomes, asking, finish

GOLDEN = Path(__file__).resolve().parents[4] / "fixtures" / "gateway" / "session_keys.golden.json"
START = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
# Built at runtime: the Telegram token shape never appears literally in the source.
TOKEN = "".join(["55501", "234", ":", "Ab", "c-" * 16, "d"])
EMOJI = "\N{GRINNING FACE}"  # two UTF-16 units


class Rig:
    """The wire fake, a Telegram adapter on it and the events it emitted."""

    def __init__(self, options: TelegramOptions | None = None, **kw: Any) -> None:
        self.clock = FakeClock(START)
        self.platform = FakeChatPlatform(
            "telegram", clock=self.clock, max_len=4096, len_fn=utf16_units, bot_user_id="1000"
        )
        self.wire = TelegramWire(self.platform)
        self.bot = self.wire.bot
        self.events: list[InboundEvent] = []
        self.adapter = TelegramAdapter(
            cast(BotApi, self.bot), clock=self.clock, options=options, **kw
        )

        async def sink(event: InboundEvent) -> None:
            self.events.append(event)

        self.adapter.set_sink(sink)

    async def settle[T](self, awaitable: Awaitable[T], *, step: float = 0.5) -> T:
        """Await while moving the fake clock past any sleeps (and letting threads finish)."""
        task = asyncio.ensure_future(awaitable)
        for _ in range(4000):
            await self.clock.run_until_idle()
            if task.done():
                return task.result()
            pause = asyncio.get_running_loop().create_future()
            await asyncio.wait({pause}, timeout=0.001)  # threads (file I/O) get a moment
            pause.cancel()
            self.clock.advance(step)
        task.cancel()
        raise AssertionError("never finished on the fake clock")

    async def pump(self, rounds: int = 3) -> None:
        for _ in range(rounds):
            await self.clock.advance_and_settle(1.0)
            await self.adapter.idle()

    def called(self, name: str) -> list[dict[str, Any]]:
        return [kw for n, kw in self.bot.calls if n == name]


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# -- the phase row's Verify column -----------------------------------------------------------------


def test_4096_utf16_units_of_emoji_fit_one_message_and_one_more_unit_splits() -> None:
    rig = Rig()

    async def main() -> None:
        await rig.settle(rig.adapter.connect())
        whole = await rig.settle(rig.adapter.send("7", EMOJI * 2048, meta={"plain": True}))
        assert whole.ok and whole.continuation_ids == ()
        split = await rig.settle(rig.adapter.send("8", EMOJI * 2048 + "x", meta={"plain": True}))
        assert split.ok and len(split.ids) == 2
        texts = rig.platform.texts("8")
        assert all(utf16_units(t) <= 4096 for t in texts)
        body = "".join(re.sub(r"\s*\(\d+/\d+\)$", "", t) for t in texts)
        assert body == EMOJI * 2048 + "x"  # nothing lost, no surrogate pair cut in half
        await rig.adapter.disconnect()

    run(main())
    assert rig.platform.texts("7") == [EMOJI * 2048]


def test_a_97_minute_flood_wait_comes_back_typed_without_sleeping() -> None:
    rig = Rig()

    async def main() -> None:
        await rig.settle(rig.adapter.connect())
        before = rig.clock.monotonic()
        sends = len(rig.called("send_message"))
        rig.platform.fail(FailureKind.RATE_LIMIT, op="send", retry_after=97 * 60, times=2)
        direct = await rig.adapter.send("7", "hello")
        retried = await rig.adapter.send_with_retry("8", "hello", rng=lambda: 0.0)
        for result in (direct, retried):
            assert not result.ok and result.is_flood and result.kind is ErrorKind.RATE_LIMITED
            assert result.retry_after == 97 * 60
        assert rig.clock.monotonic() == before  # nothing slept, not even the retry policy
        assert len(rig.called("send_message")) == sends + 2  # one call each, no notice sent
        await rig.adapter.disconnect()

    run(main())


def test_a_duplicate_update_id_is_handled_once_also_after_a_restart(tmp_path: Path) -> None:
    def receipts(clock: FakeClock) -> UpdateReceipts:
        return UpdateReceipts(clock, path_for=lambda bot_id: tmp_path / f"receipts_{bot_id}.json")

    first = Rig()
    first.adapter.receipts = receipts(first.clock)
    first.platform.dm("200", "hello")

    async def main(rig: Rig, again: bool) -> None:
        await rig.settle(rig.adapter.connect())
        await rig.pump()
        if again:
            for update in rig.bot._update_log:  # pyright: ignore[reportAttributeAccessIssue]
                assert rig.adapter.admit(update) is False
            await rig.pump()
        await rig.adapter.disconnect()

    original = FakeBot.get_updates

    async def remembering(self: FakeBot, *args: Any, **kw: Any) -> list[Update]:
        found = await original(self, *args, **kw)
        log: list[Update] = self.__dict__.setdefault("_update_log", [])
        log.extend(found)
        return found

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(FakeBot, "get_updates", remembering)
        run(main(first, again=True))
        assert [e.text for e in first.events] == ["hello"]
        saved = json.loads((tmp_path / "receipts_1000.json").read_text())
        assert list(saved["update_ids"]) == ["1"]

        # a new process: Telegram re-sends the update it never saw acknowledged
        second = Rig()
        second.adapter.receipts = receipts(second.clock)
        redelivered = first.bot._update_log[0]  # pyright: ignore[reportAttributeAccessIssue]

        async def restart() -> None:
            await second.settle(second.adapter.connect())
            assert second.adapter.admit(redelivered) is False
            await second.pump()
            await second.adapter.disconnect()

        run(restart())
        assert second.events == []


def test_receipts_are_bounded_and_expire_after_a_day() -> None:
    clock = FakeClock(START)
    receipts = UpdateReceipts(clock, cap=3)

    async def main() -> None:
        for update_id in range(1, 6):
            assert receipts.claim(update_id)
            receipts.done(update_id)
        assert len(receipts) == 3 and 1 not in receipts and 5 in receipts
        assert not receipts.claim(5)
        clock.advance(24 * 3600 + 1)
        assert receipts.claim(5)  # a day later the id may be Telegram's again

    run(main())


def test_a_button_press_from_an_unauthorized_user_is_refused(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[TelegramAdapter] = []

    def telegram_adapter(fake: FakeChatPlatform, *, clock: FakeClock) -> TelegramAdapter:
        fake.bot_user_id = "1000"
        adapter = TelegramAdapter(cast(BotApi, FakeBot(fake)), clock=clock)
        built.append(adapter)
        return adapter

    monkeypatch.setattr(_kit, "FakeChatAdapter", telegram_adapter)
    stranger = "999"

    async def main() -> None:
        seen = Outcomes()
        kit = await build_kit(
            scope,
            fake_clock,
            tmp_path,
            tool_batch(("ask", {})),
            text_turn("all done"),
            inline={"ask": seen.approval()},
        )
        from indus.gateway.core.prompts import ChatPrompts

        prompts = ChatPrompts(kit.pipeline)
        prompts.install()
        try:
            await asking(kit, prompts)
            card = prompts.open_cards()[0]
            assert card.buttons and card.message_id
            once = next(b.data for b in kit.fake.message(card.message_id).buttons)  # type: ignore[union-attr]
            kit.fake.button_press(ALLOWED, stranger, once, message_id=card.message_id)
            await kit.until(lambda: len(kit.fake.answered) == 1)  # the spinner stops anyway
            await kit.settle()
            assert seen.seen == [] and prompts.open_cards()  # nothing was approved
            kit.fake.button_press(ALLOWED, ALLOWED, once, message_id=card.message_id)
            await kit.until(lambda: seen.seen == ["once:"])
            assert len(kit.fake.answered) == 2
        finally:
            await finish(kit, prompts)

    run(main())
    assert built and built[0].me is not None


def test_a_forum_topic_message_gets_the_golden_topic_key() -> None:
    golden = json.loads(GOLDEN.read_text())
    normaliser = Normaliser()
    normaliser.identify(1000, "indus_bot")
    message = Message(
        41,
        Chat(-200, "supergroup", "Ops", is_forum=True),
        text="deploy status?",
        from_user=User(7, False, "Ana"),
        message_thread_id=12,
        is_topic_message=True,
    )
    event = normaliser.message_event(5, message)
    assert event is not None
    assert event.source.chat_type == "forum" and event.source.thread_id == "12"
    assert build_session_key(event.source) == golden["forum_topic_shared"]


# -- inbound ---------------------------------------------------------------------------------------


def _message(text: str, **kw: Any) -> Message:
    chat = kw.pop("chat", Chat(-300, "supergroup", "Team"))
    return Message(
        kw.pop("message_id", 9), chat, text=text, from_user=kw.pop("user", User(7)), **kw
    )


def test_normalising_mentions_replies_and_foreign_commands() -> None:
    n = Normaliser()
    n.identify(1000, "Indus_Bot")
    mention = _message(
        f"{EMOJI} @indus_bot hi", entities=(Entity("mention", 3, 10),)
    )  # offsets in UTF-16 units
    event = n.message_event(1, mention)
    assert event is not None and event.metadata.get("mentioned") is True
    plain_text_mention = n.message_event(2, _message("hey @INDUS_BOT"))
    assert plain_text_mention is not None and plain_text_mention.metadata.get("mentioned")
    assert n.message_event(3, _message("/help@other_bot")) is None
    own = n.message_event(4, _message("/help@indus_bot"))
    assert own is not None and own.command == "help" and own.metadata.get("mentioned")
    reply = _message(
        "and this?",
        reply_to_message=_message("earlier answer", user=User(1000, True), message_id=8),
    )
    event = n.message_event(5, reply)
    assert event is not None and event.reply_to_is_own and event.reply_to_id == "8"
    assert event.reply_to_text == "earlier answer"
    assert n.message_event(6, _message("echo", user=User(1000, True))) is None  # our own message


def test_topics_general_and_reply_anchors() -> None:
    n = Normaliser()
    n.identify(1000, "indus_bot")
    forum = Chat(-200, "supergroup", "Ops", is_forum=True)
    general = n.message_event(1, _message("hi", chat=forum))
    assert general is not None and (general.source.chat_type, general.source.thread_id) == (
        "forum",
        "1",
    )
    anchored = n.message_event(2, _message("hi", message_thread_id=33))  # plain group
    assert anchored is not None and anchored.source.chat_type == "group"
    assert anchored.source.thread_id is None  # a reply anchor is not a thread
    channel = n.message_event(
        3, Message(4, Chat(-500, "channel", "News"), text="post", from_user=None)
    )
    assert channel is not None and channel.source.chat_type == "channel"
    assert channel.source.user_id == "-500"


def test_underscore_commands_reach_their_hyphenated_registry_names() -> None:
    registry = CommandRegistry([CommandDef("do-thing", "Do the thing", "tools")])
    assert _canonical_command_text("/do_thing@indus_bot now", registry) == (
        "/do-thing@indus_bot now"
    )
    assert _canonical_command_text("/unknown_word x", registry) == "/unknown_word x"
    assert menu_commands(registry) == [BotCommand("do_thing", "Do the thing")]
    n = Normaliser()
    n.identify(1000, "indus_bot")
    for word in ("reload_mcp", "reload_skills"):
        event = n.message_event(1, _message(f"/{word} now", chat=Chat(7, "private")))
        assert event is not None and event.command_args == "now"
        assert canonical_command(event) == word.replace("_", "-")


def test_inbound_media_kinds_and_refs() -> None:
    rig = Rig()
    rig.platform.voice("200", "200", "note.ogg", duration=4)
    rig.platform.photo("200", "200", "pic.jpg", caption="look")
    rig.platform.document("200", "200", "notes.txt")

    async def main() -> None:
        await rig.settle(rig.adapter.connect())
        await rig.pump()
        await rig.adapter.disconnect()

    run(main())
    voice, photo, document = rig.events
    assert voice.kind is MessageKind.VOICE and voice.metadata["duration"] == 4
    assert voice.media[0].ref == "tg:f-note.ogg" and voice.media[0].mime == "audio/ogg"
    assert photo.kind is MessageKind.PHOTO and photo.text == "look"
    assert document.media[0].ref == "tg:f-notes.txt:notes.txt"
    assert all("/" not in e.media[0].ref for e in rig.events)


def test_updates_of_one_chat_run_in_order_and_at_most_the_limit_at_once() -> None:
    running = 0
    peak = 0
    order: list[tuple[int, int]] = []

    async def handle(update: Update) -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        for _ in range(5 if update.update_id % 2 else 1):
            await checkpoint()
        order.append((update.message.chat.id, update.update_id))  # type: ignore[union-attr]
        running -= 1

    def update(update_id: int, chat: int) -> Update:
        return Update(update_id, message=Message(update_id, Chat(chat, "private")))

    async def main() -> None:
        dispatcher = UpdateDispatcher(handle, limit=2)
        for update_id in range(1, 9):
            dispatcher.submit(update(update_id, chat=update_id % 3))
        await dispatcher.idle()

    run(main())
    assert peak <= 2
    for chat in range(3):
        ids = [u for c, u in order if c == chat]
        assert ids == sorted(ids)


def test_polling_backs_off_on_network_errors_and_stops_on_a_revoked_token() -> None:
    rig = Rig()
    failures: list[Exception] = [NetworkError("ConnectError: down"), NetworkError("again")]
    original = FakeBot.get_updates

    async def flaky(self: FakeBot, *args: Any, **kw: Any) -> list[Update]:
        if failures:
            raise failures.pop(0)
        return await original(self, *args, **kw)

    fatal: list[tuple[str, str]] = []
    rig.adapter.set_fatal_listener(lambda code, message: fatal.append((code, message)))

    async def main() -> None:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(FakeBot, "get_updates", flaky)
            await rig.settle(rig.adapter.connect())
            rig.platform.dm("200", "after the outage")
            await rig.pump(5)
            assert [e.text for e in rig.events] == ["after the outage"]
            failures.append(Unauthorized("Unauthorized"))
            assert fatal == []  # network errors are retried, never fatal
            await rig.pump(3)
            assert rig.adapter.polling_stopped == "auth"
            await rig.adapter.disconnect()
        # the revoked token is reported once as a fatal platform state (the daemon parks it)
        assert [code for code, _ in fatal] == ["telegram_auth"]
        assert "TELEGRAM_BOT_TOKEN" in fatal[0][1]

    run(main())


def test_require_topics_reminds_in_general_once_per_window() -> None:
    rig = Rig(TelegramOptions(require_topics=True))
    rig.platform.add_chat("-200", "forum", "Ops")

    async def main() -> None:
        await rig.settle(rig.adapter.connect())
        for text in ("one", "two", "/help"):
            rig.platform._inbound("-200", "forum", "7", text)  # pyright: ignore[reportPrivateUsage]
        rig.platform.thread("-200", "12", "7", "in a topic")
        await rig.pump()
        rig.clock.advance(31)
        rig.platform._inbound("-200", "forum", "7", "three")  # pyright: ignore[reportPrivateUsage]
        await rig.pump()
        await rig.adapter.disconnect()

    run(main())
    assert [e.text for e in rig.events] == ["/help", "in a topic"]
    reminders = [t for t in rig.platform.texts("-200") if "topics" in t]
    assert len(reminders) == 2
    assert rig.adapter.format(LOBBY_TEXT) in reminders


# -- outbound --------------------------------------------------------------------------------------


def test_topic_replies_threads_and_a_deleted_topic() -> None:
    rig = Rig()

    async def main() -> None:
        await rig.settle(rig.adapter.connect())
        await rig.settle(
            rig.adapter.send("-200", "in topic", reply_to="5", meta={"thread_id": "12"})
        )
        await rig.settle(rig.adapter.send("-200", "in general", meta={"thread_id": "1"}))
        rig.platform.fail(FailureKind.THREAD_NOT_FOUND, op="send")
        moved = await rig.settle(rig.adapter.send("-200", "topic gone", meta={"thread_id": "13"}))
        assert moved.ok
        await rig.adapter.disconnect()

    run(main())
    sends = rig.called("send_message")
    assert sends[0]["message_thread_id"] == 12 and sends[0]["reply_to_message_id"] == 5
    assert sends[0]["parse_mode"] == "MarkdownV2"
    assert sends[1]["message_thread_id"] is None  # General is addressed by leaving it out
    assert [s["message_thread_id"] for s in sends[2:]] == [13, None]


def test_edits_not_modified_markup_fallback_and_overflow() -> None:
    rig = Rig()

    async def main() -> None:
        await rig.settle(rig.adapter.connect())
        sent = await rig.adapter.send("7", "draft")
        assert sent.message_id is not None
        rig.platform.fail(FailureKind.PARSE_ERROR, op="edit")
        fixed = await rig.adapter.edit("7", sent.message_id, "Approved.", final=True)
        assert fixed.ok
        long = " ".join(f"w{i:05d}" for i in range(1200))  # about 8,400 units
        rig.clock.advance(2.0)  # past the chat's send slot: interim edits are not skipped
        interim = await rig.adapter.edit("7", sent.message_id, long)
        assert interim.ok and interim.message_id == sent.message_id
        final = await rig.settle(rig.adapter.edit("7", sent.message_id, long, final=True))
        assert final.ok and final.continuation_ids[0] == sent.message_id
        assert len(final.ids) >= 3
        await rig.adapter.disconnect()

    run(main())
    edits = rig.called("edit_message_text")
    assert edits[0]["parse_mode"] == "MarkdownV2" and edits[1]["parse_mode"] is None
    texts = rig.platform.texts("7")
    words = [w for t in texts for w in t.split() if not re.match(r"^\\?\(\d+/\d+\\?\)$", w)]
    assert words == [f"w{i:05d}" for i in range(1200)]


def test_approval_cards_have_inline_keyboards_and_fall_back_to_plain_markup() -> None:
    rig = Rig()
    prompt = ApprovalPrompt(
        request_id="r1",
        session_key="k",
        text="Run this? It deletes files.\n```\nrm -rf build\n```",
        fallback_text="Run this? Reply /approve or /deny.",
        actions=(ApprovalAction("Allow once", "once"), ApprovalAction("Deny", "deny")),
    )

    async def main() -> None:
        await rig.settle(rig.adapter.connect())
        card = await rig.settle(rig.adapter.send_approval("7", prompt))
        assert card.ok
        rig.platform.fail(FailureKind.PARSE_ERROR, op="send")
        again = await rig.settle(rig.adapter.send_approval("7", prompt))
        assert again.ok
        await rig.adapter.disconnect()

    run(main())
    first, retry_md, retry_plain = rig.called("send_message")
    assert first["parse_mode"] == "MarkdownV2" and "deletes files\\." in first["text"]
    assert retry_md["parse_mode"] == "MarkdownV2" and retry_plain["parse_mode"] is None
    message = rig.platform.messages("7")[0]
    assert [b.data for b in message.buttons] == ["apv:r1:once", "apv:r1:deny"]


def test_media_sends_per_kind_with_caption_reply_and_topic(tmp_path: Path) -> None:
    rig = Rig()
    files = {name: tmp_path / name for name in ("a.png", "b.ogg", "c.mp3", "d.mp4", "e.pdf")}
    for path in files.values():
        path.write_bytes(b"data")

    async def main() -> None:
        await rig.settle(rig.adapter.connect())
        meta = {"thread_id": "12"}
        a = rig.adapter
        assert (await rig.settle(a.send_image("-200", str(files["a.png"]), caption="chart"))).ok
        assert (await rig.settle(a.send_voice("-200", str(files["b.ogg"]), meta=meta))).ok
        assert (await rig.settle(a.send_voice("-200", str(files["c.mp3"]), reply_to="3"))).ok
        assert (await rig.settle(a.send_video("-200", str(files["d.mp4"])))).ok
        assert (await rig.settle(a.send_document("-200", str(files["e.pdf"])))).ok
        assert (await rig.settle(a.send_file("-200", "https://files.example/x.zip"))).ok
        missing = await rig.settle(a.send_image("-200", str(tmp_path / "nope.png")))
        assert not missing.ok
        await rig.adapter.disconnect()

    run(main())
    names = [n for n, _ in rig.bot.calls if n.startswith("send_") and n != "send_message"]
    assert names == [
        "send_photo",
        "send_voice",
        "send_audio",
        "send_video",
        "send_document",
        "send_document",
    ]
    calls = [kw for n, kw in rig.bot.calls if n in names]
    assert calls[0]["caption"] == "chart"
    assert calls[1]["message_thread_id"] == 12 and calls[2]["reply_to_message_id"] == 3
    assert calls[5]["file"] == "https://files.example/x.zip"


def test_typing_reactions_and_the_command_menu() -> None:
    rig = Rig(TelegramOptions(reactions=True))
    quiet = Rig()

    async def main() -> None:
        await rig.settle(rig.adapter.connect())
        await rig.adapter.typing_start("-200", meta={"thread_id": "12"})
        assert await rig.adapter.processing_started("-200", "5")
        assert await rig.adapter.processing_finished("-200", "5", ok=True)
        assert await rig.adapter.processing_finished("-200", "5", ok=None)
        await rig.pump(1)
        await rig.adapter.disconnect()
        await quiet.settle(quiet.adapter.connect())
        assert not await quiet.adapter.processing_started("-200", "5")
        await quiet.adapter.disconnect()

    run(main())
    assert rig.called("send_chat_action")[0]["message_thread_id"] == 12
    assert [kw["reaction"] for kw in rig.called("set_message_reaction")] == [
        "\N{EYES}",
        "\N{THUMBS UP SIGN}",
        None,
    ]
    assert quiet.called("set_message_reaction") == []
    menus = rig.called("set_my_commands")
    assert [m["scope"]["type"] for m in menus] == [
        "default",
        "all_private_chats",
        "all_group_chats",
    ]
    commands = menus[0]["commands"]
    assert commands == menu_commands() and 0 < len(commands) <= 100
    assert all(re.fullmatch(r"[a-z0-9_]{1,32}", c.command) for c in commands)
    assert all(0 < len(c.description) <= 256 for c in commands)
    assert "reload_mcp" in [c.command for c in commands]


def test_connect_drops_the_backlog_on_a_cold_boot_and_keeps_it_on_a_reconnect() -> None:
    rig = Rig()

    async def main() -> None:
        await rig.settle(rig.adapter.connect())
        await rig.adapter.disconnect()
        await rig.settle(rig.adapter.connect(is_reconnect=True))
        await rig.adapter.disconnect()

    run(main())
    assert [kw["drop_pending_updates"] for kw in rig.called("delete_webhook")] == [True, None]
    assert rig.adapter.me is not None and rig.adapter.me.username == "indus_test_bot"


def test_a_refused_token_is_a_fatal_connect_error() -> None:
    rig = Rig()

    async def refuse(self: FakeBot) -> User:
        raise Unauthorized("Unauthorized")

    async def main() -> None:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(FakeBot, "get_me", refuse)
            with pytest.raises(AdapterConnectError) as caught:
                await rig.adapter.connect()
        assert not caught.value.retryable and caught.value.code == "telegram_auth"

    run(main())


# -- webhook mode ----------------------------------------------------------------------------------


class RecordingServer:
    def __init__(self) -> None:
        self.app: Any = None
        self.bound: tuple[str, int] | None = None
        self.stopped = False

    async def start(self, app: Any, host: str, port: int) -> None:
        self.app, self.bound = app, (host, port)

    async def stop(self) -> None:
        self.stopped = True


def test_webhook_mode_needs_its_secret() -> None:
    rig = Rig(TelegramOptions(webhook=WebhookOptions("https://bot.example/tg/hook")))

    async def main() -> None:
        with pytest.raises(AdapterConnectError) as caught:
            await rig.adapter.connect()
        assert not caught.value.retryable and caught.value.code == "webhook_secret_missing"

    run(main())
    assert rig.bot.calls == []  # refused before any call


def test_webhook_mode_accepts_only_pushes_with_the_secret() -> None:
    secret = "s3cret-" + "x" * 10
    server = RecordingServer()
    options = TelegramOptions(
        webhook=WebhookOptions("https://bot.example/tg/hook", secret=secret, port=8443)
    )
    rig = Rig(options, server_factory=lambda: server)
    push = {
        "update_id": 77,
        "message": {
            "message_id": 1,
            "chat": {"id": 200, "type": "private"},
            "from": {"id": 200, "first_name": "Ana"},
            "text": "pushed",
        },
    }

    async def post(headers: dict[str, str], body: Any) -> int:
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://hook") as client:
            response = await client.post("/tg/hook", json=body, headers=headers)
        return response.status_code

    async def main() -> None:
        await rig.settle(rig.adapter.connect())
        assert server.bound == ("127.0.0.1", 8443)
        assert await post({}, push) == 401
        assert await post({SECRET_HEADER: "wrong"}, push) == 401
        await rig.adapter.idle()
        assert rig.events == []
        assert await post({SECRET_HEADER: secret}, {"nope": 1}) == 400
        assert await post({SECRET_HEADER: secret}, push) == 200
        assert await post({SECRET_HEADER: secret}, push) == 200  # a re-push: handled once
        await rig.adapter.idle()
        await rig.adapter.disconnect()

    run(main())
    assert [e.text for e in rig.events] == ["pushed"]
    (registered,) = rig.called("set_webhook")
    assert registered["secret_token"] == secret and registered["url"].endswith("/tg/hook")
    assert rig.called("get_updates") == [] and server.stopped
    assert secret not in repr(options)


# -- wiring ----------------------------------------------------------------------------------------


def _settings(scope: Scope) -> PlatformSettings:
    settings = load_gateway_settings(scope).get("telegram")
    assert settings is not None and settings.enabled
    return settings


def test_create_adapter_reads_credentials_and_options(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets: Any = scope.secrets
    secrets.set("TELEGRAM_BOT_TOKEN", TOKEN)
    write_settings(scope, "platforms:\n  telegram:\n    reactions: true\n    poll_timeout: 20\n")
    adapter = PlatformRegistry().create_adapter(
        "telegram", AdapterConfig("telegram", _settings(scope), scope, scope.clock, None)
    )
    assert isinstance(adapter, TelegramAdapter)
    assert isinstance(adapter.bot, BotApi) and BOT_FACTORY.__module__ == telegram_package
    assert adapter.options.reactions and adapter.options.poll_timeout == 20
    assert TOKEN not in repr(adapter) and TOKEN not in repr(adapter.bot)
    assert adapter.scrub(f"failed: /bot{TOKEN}/getMe") == "failed: /bot****/getMe" or (
        TOKEN not in adapter.scrub(f"failed: /bot{TOKEN}/getMe")
    )
    assert PlatformRegistry().missing_deps("telegram") is None  # no extra to install


def test_indus_send_opens_telegram_send_only(
    capsys: pytest.CaptureFixture[str], scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets: Any = scope.secrets
    secrets.set("TELEGRAM_BOT_TOKEN", TOKEN)
    write_settings(scope, "platforms:\n  telegram:\n    home_channel:\n      chat_id: '-100'\n")
    wire = TelegramWire()
    wire.platform.dm("200", "an unread message nobody should consume")
    seen: list[tuple[str, AdapterConfig]] = []

    def factory(token: str, config: AdapterConfig) -> BotApi:
        seen.append((token, config))
        return cast(BotApi, wire.bot)

    monkeypatch.setattr(f"{telegram_package}.BOT_FACTORY", factory)
    assert send_command.ADAPTER_BUILDER is send_command.build_adapter
    ran = run_cli(capsys, scope, "send", "telegram:42", "deploy done")
    assert ran.code == 0, ran.err
    assert seen and seen[0][0] == TOKEN and seen[0][1].send_only
    names = wire.bot.names()
    assert names[0] == "get_me" and "send_message" in names and names[-1] == "aclose"
    for inbound_work in ("get_updates", "delete_webhook", "set_webhook", "set_my_commands"):
        assert inbound_work not in names
    assert wire.platform.texts("42") == ["deploy done"]
    assert wire.platform.next_inbound() is not None  # still unread
    assert TOKEN not in ran.out + ran.err


def test_the_media_fetcher_feeds_the_intake(scope: Scope) -> None:
    rig = Rig()
    rig.bot.files["notes.txt"] = b"line one\nline two\n"
    rig.platform.document("200", "200", "notes.txt", caption="summarise")

    async def main() -> str:
        await rig.settle(rig.adapter.connect())
        await rig.pump()
        intake = MediaIntake(scope)
        undo = attach_fetcher(intake, rig.adapter)
        assert undo is not None
        text = await rig.settle(intake.enrich(rig.events[0]))
        undo()
        await rig.adapter.disconnect()
        return text

    text = run(main())
    assert "line one\nline two" in text and text.endswith("summarise")
    assert [n for n, _ in rig.bot.calls if n in ("get_file", "download_file")] == [
        "get_file",
        "download_file",
    ]


def test_fetch_media_refuses_files_over_the_download_limit_and_foreign_refs() -> None:
    rig = Rig()
    rig.bot.files["big.bin"] = b"x" * (20 * 1024 * 1024 + 1)

    async def drain(ref: str) -> bytes:
        return b"".join([c async for c in rig.adapter.fetch_media(Attachment(ref=ref))])

    async def main() -> None:
        with pytest.raises(BadRequest, match="too big"):
            await drain("tg:f-big.bin")
        with pytest.raises(ValueError):
            await drain("/etc/passwd")

    run(main())
    assert "download_file" not in rig.bot.names()  # refused from getFile's size alone


def test_the_webhook_listener_binds_loopback_unless_a_host_is_configured() -> None:
    url = "https://bot.example/tg/hook"
    plain = TelegramOptions.from_extra({"webhook_url": url}, webhook_secret="k" * 20)
    assert plain.webhook is not None and plain.webhook.host == "127.0.0.1"
    assert WebhookOptions(url).host == "127.0.0.1"
    public = TelegramOptions.from_extra({"webhook_url": url, "webhook_host": "0.0.0.0"})
    assert public.webhook is not None and public.webhook.host == "0.0.0.0"


def test_the_webhook_listener_serves_on_loopback() -> None:
    received: list[int] = []

    async def on_update(update: Update) -> None:
        received.append(update.update_id)

    async def main() -> None:
        server = UvicornServer(FakeClock(START))
        await server.start(webhook_app("k" * 20, on_update, "/hook"), "127.0.0.1", 0)
        try:
            address = server.address
            assert address is not None
            url = f"http://{address[0]}:{address[1]}/hook"
            async with httpx.AsyncClient(trust_env=False) as client:
                denied = await client.post(url, json={"update_id": 1})
                taken = await client.post(
                    url, json={"update_id": 2}, headers={SECRET_HEADER: "k" * 20}
                )
            assert (denied.status_code, taken.status_code) == (401, 200)
        finally:
            await server.stop()

    run(main())
    assert received == [2]


def test_streaming_edits_are_on_by_default_for_telegram(scope: Scope) -> None:
    secrets: Any = scope.secrets
    secrets.set("TELEGRAM_BOT_TOKEN", TOKEN)
    assert _settings(scope).streaming is True


def test_general_topic_typing_saturated_previews_and_forum_menus(tmp_path: Path) -> None:
    rig = Rig()
    wav = tmp_path / "speech.wav"
    wav.write_bytes(b"RIFF")

    async def main() -> None:
        await rig.settle(rig.adapter.connect())
        await rig.adapter.typing_start("-200", meta={"thread_id": "1"})
        sent = await rig.adapter.send("7", "draft")
        assert sent.message_id is not None
        long = " ".join(f"w{i:05d}" for i in range(1200))
        for extra in ("", " more", " and more"):  # every frame past the cap shows piece one
            rig.clock.advance(2.0)
            assert (await rig.adapter.edit("7", sent.message_id, long + extra)).ok
        assert (await rig.settle(rig.adapter.send_voice("-200", str(wav)))).ok
        for text in ("first", "second"):
            rig.platform.thread("-200", "12", "7", text)
        await rig.pump()
        await rig.adapter.disconnect()

    run(main())
    assert rig.called("send_chat_action")[0]["message_thread_id"] == 1
    assert len(rig.called("edit_message_text")) == 1
    assert "send_document" in rig.bot.names() and "send_voice" not in rig.bot.names()
    scopes = [kw["scope"] for kw in rig.called("set_my_commands")]
    assert scopes.count({"type": "chat", "chat_id": -200}) == 1


def test_the_command_menu_stays_within_its_text_budget() -> None:
    rows = menu_commands(budget=200)
    assert rows and sum(len(r.command) + len(r.description) for r in rows) <= 200
    assert len(rows) < len(menu_commands())


def test_a_failed_connect_closes_the_client() -> None:
    rig = Rig()
    rig.platform.fail_connect(FailureKind.CONNECT_TIMEOUT)

    async def main() -> None:
        with pytest.raises(AdapterConnectError) as caught:
            await rig.adapter.connect()
        assert caught.value.retryable

    run(main())
    assert rig.bot.names() == ["get_me", "aclose"]
