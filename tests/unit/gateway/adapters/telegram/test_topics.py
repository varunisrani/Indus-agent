"""P16-34: Telegram DM topics and sticker descriptions over the wire fake."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.gateway.adapters.telegram.adapter import TelegramOptions
from indus.gateway.adapters.telegram.bot_api import Chat, FileRef, Message, Update, User
from indus.gateway.adapters.telegram.stickers import (
    STICKER_CACHE_MAX,
    StickerCache,
    StickerEntry,
    StickerNotes,
)
from indus.gateway.adapters.telegram.topics import (
    COOLDOWN_S,
    DEFAULT_TOPIC_NAME,
    GREETING,
    NO_THREADED_MODE,
    TOPIC_NAME_MAX,
)
from indus.gateway.core.events import MessageKind
from indus.gateway.core.session_keys import build_session_key
from indus.gateway.extras import attach_fetcher
from indus.gateway.intake.media import MediaIntake
from tests.fakes.clock import FakeClock, checkpoint
from tests.fakes.platform.core import FailureKind
from tests.unit.gateway.adapters.telegram.test_adapter import Rig

START = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
USER = "4242"
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 24


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def topic_rig(*, configured: bool = True, threaded: bool = True, **kw: Any) -> Rig:
    rig = Rig(options=TelegramOptions(dm_topics=configured), **kw)
    rig.bot.has_topics_enabled = threaded
    return rig


_updates = iter(range(1, 10_000))


async def say(rig: Rig, text: str, thread: str | None = None, *, chat: Chat | None = None) -> None:
    """One message to the running adapter, handled before this returns."""
    n = next(_updates)
    message = Message(
        500 + n,
        chat or Chat(int(USER), "private"),
        text=text,
        from_user=User(int(USER), False, "Ann"),
        message_thread_id=int(thread) if thread else None,
        date=START,
    )
    assert rig.adapter.admit(Update(n, message=message))
    await rig.settle(rig.adapter.idle())


async def up(rig: Rig) -> None:
    await rig.settle(rig.adapter.connect())


async def down(rig: Rig) -> None:
    await rig.adapter.idle()
    await rig.adapter.disconnect()


# -- the phase row's Verify column -----------------------------------------------------------------


def test_two_dm_topics_are_two_sessions() -> None:
    rig = topic_rig()

    async def main() -> None:
        await up(rig)
        for text, thread in (("in eleven", "11"), ("in twelve", "12"), ("at the root", None)):
            await say(rig, text, thread)
        await say(rig, "eleven again", "11")
        # a reply goes back into its topic
        await rig.settle(rig.adapter.send(USER, "answer", meta={"thread_id": "12"}))
        await down(rig)

    run(main())
    keys = {e.text: build_session_key(e.source) for e in rig.events}
    assert keys["in eleven"] == keys["eleven again"]
    assert len({keys["in eleven"], keys["in twelve"], keys["at the root"]}) == 3
    assert keys["in twelve"].endswith(":12")
    assert rig.called("send_message")[-1]["message_thread_id"] == 12


@pytest.mark.parametrize(("configured", "threaded"), [(False, True), (True, False)])
def test_without_the_mode_a_private_chat_stays_one_session(
    configured: bool, threaded: bool
) -> None:
    rig = topic_rig(configured=configured, threaded=threaded)

    async def main() -> None:
        await up(rig)
        await say(rig, "in eleven", "11")
        await say(rig, "in twelve", "12")
        await down(rig)

    run(main())
    assert len({build_session_key(e.source) for e in rig.events}) == 1
    assert all(e.source.thread_id is None for e in rig.events)


# -- /topic ----------------------------------------------------------------------------------------


def test_the_topic_command_opens_a_topic_and_greets_inside_it() -> None:
    rig = topic_rig()

    async def main() -> None:
        await up(rig)
        await say(rig, "/topic Trip plans")
        await say(rig, "/topic")  # again at once: refused, no second topic
        rig.clock.advance(COOLDOWN_S)
        await say(rig, "/topic " + "x" * 300)
        await down(rig)

    run(main())
    assert rig.events == []  # the adapter's own command never reaches the core
    names = [name for _, _, name in rig.bot.topics]
    assert names == ["Trip plans", "x" * TOPIC_NAME_MAX]
    sends = rig.called("send_message")
    greeting = next(s for s in sends if s["message_thread_id"] == 101)
    assert "new conversation" in greeting["text"]
    assert any("Trip plans" in s["text"] and s["message_thread_id"] is None for s in sends)
    assert any("a moment ago" in s["text"] for s in sends)


def test_an_unnamed_topic_gets_the_default_name() -> None:
    rig = topic_rig()

    async def main() -> None:
        await up(rig)
        await say(rig, "/topic")
        await down(rig)

    run(main())
    assert rig.bot.topics == [(USER, 101, DEFAULT_TOPIC_NAME)]
    assert GREETING.split(".")[0] in rig.called("send_message")[0]["text"].replace("\\", "")


def test_without_threaded_mode_the_command_says_how_to_turn_it_on() -> None:
    rig = topic_rig(threaded=False)

    async def main() -> None:
        await up(rig)
        await say(rig, "/topic Trip")
        await down(rig)

    run(main())
    assert rig.called("create_forum_topic") == [] and rig.events == []
    (sent,) = rig.called("send_message")
    assert "Threads Settings" in sent["text"].replace("\\", "")
    assert "BotFather" in NO_THREADED_MODE


def test_a_refused_topic_creation_is_answered() -> None:
    rig = topic_rig()

    async def main() -> None:
        await up(rig)
        rig.bot.has_topics_enabled = False  # Threaded Mode switched off since the connect
        await say(rig, "/topic Trip")
        await down(rig)

    run(main())
    assert len(rig.called("create_forum_topic")) == 1
    (sent,) = rig.called("send_message")
    assert "Threads Settings" in sent["text"].replace("\\", "")


def test_another_refusal_is_reported_without_the_threaded_mode_hint() -> None:
    rig = topic_rig()
    rig.platform.fail(FailureKind.PARSE_ERROR, op="topic")  # any other Bad Request

    async def main() -> None:
        await up(rig)
        await say(rig, "/topic Trip")
        await down(rig)

    run(main())
    (sent,) = rig.called("send_message")
    assert "could not open a topic" in sent["text"] and "Threads" not in sent["text"]


def test_the_command_is_not_ours_with_the_mode_off_or_in_a_group() -> None:
    off = topic_rig(configured=False)
    on = topic_rig()

    async def main() -> None:
        await up(off)
        await say(off, "/topic Trip")
        await down(off)
        await up(on)
        await say(on, "/topic Trip", chat=Chat(-100, "supergroup", "G"))
        await down(on)

    run(main())
    assert [e.text for e in off.events + on.events] == ["/topic Trip", "/topic Trip"]
    assert off.called("create_forum_topic") == on.called("create_forum_topic") == []


def test_the_private_menu_lists_topic_only_while_the_mode_is_active() -> None:
    def menus(rig: Rig) -> dict[str, list[str]]:
        async def main() -> None:
            await up(rig)
            await rig.settle(rig.adapter.idle())
            await rig.pump()
            await down(rig)

        run(main())
        return {
            kw["scope"]["type"]: [c.command for c in kw["commands"]]
            for kw in rig.called("set_my_commands")
        }

    active = menus(topic_rig())
    assert "topic" in active["all_private_chats"] and "topic" not in active["default"]
    assert "topic" not in menus(topic_rig(threaded=False))["all_private_chats"]


# -- stickers --------------------------------------------------------------------------------------


class Describer:
    def __init__(self, answer: str = "A cat waving a paw") -> None:
        self.answer = answer
        self.paths: list[Path] = []
        self.fail = False

    async def __call__(self, path: Path) -> str:
        self.paths.append(path)
        assert path.read_bytes().startswith(b"RIFF")
        if self.fail:
            raise RuntimeError("the side model is down")
        return self.answer


def sticker(unique: str = "stk1", **kw: Any) -> FileRef:
    return FileRef(f"f-{unique}", f"u-{unique}", emoji="\N{CAT FACE}", set_name="Cats", **kw)


def sticker_update(update_id: int, ref: FileRef, *, chat: Chat | None = None) -> Update:
    where = chat or Chat(int(USER), "private")
    message = Message(
        update_id + 1000,
        where,
        from_user=User(int(USER), False, "Ann"),
        sticker=ref,
        date=START,
    )
    return Update(update_id, message=message)


class StickerRig(Rig):
    def __init__(self, scope: Scope, describer: Describer, **notes: Any) -> None:
        super().__init__()
        self.path = scope.home / "gateway" / "telegram_stickers.json"
        self.adapter.stickers = StickerNotes(StickerCache(self.path), self.clock, **notes)
        for unique in ("stk1", "stk2", "stk3", "anim"):
            self.bot.files[unique] = WEBP
        self.intake = MediaIntake(scope, describe=describer)


def sticker_rig(scope: Scope, describer: Describer, **notes: Any) -> tuple[StickerRig, Path]:
    rig = StickerRig(scope, describer, **notes)
    return rig, rig.path


async def with_intake(rig: StickerRig, updates: list[Update]) -> None:
    await up(rig)
    undo = attach_fetcher(rig.intake, rig.adapter)
    for update in updates:
        assert rig.adapter.admit(update)
        await rig.adapter.idle()
    assert undo is not None
    undo()
    await down(rig)


def test_the_same_sticker_twice_costs_one_vision_call(scope: Scope) -> None:
    describer = Describer()
    rig, path = sticker_rig(scope, describer)
    run(with_intake(rig, [sticker_update(1, sticker()), sticker_update(2, sticker())]))
    assert len(describer.paths) == 1
    assert [e.kind for e in rig.events] == [MessageKind.STICKER] * 2
    first, second = (e.text for e in rig.events)
    assert first == second and "It shows: A cat waving a paw" in first
    assert '"Cats"' in first and "\N{CAT FACE}" in first
    assert not describer.paths[0].exists()  # the downloaded copy is removed
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["stickers"][0]["id"] == "u-stk1"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    # a restart remembers it: still no second call
    again = Describer()
    rig2, _ = sticker_rig(scope, again)
    run(with_intake(rig2, [sticker_update(3, sticker())]))
    assert again.paths == [] and "A cat waving a paw" in rig2.events[0].text


def test_animated_stickers_are_never_downloaded(scope: Scope) -> None:
    describer = Describer()
    rig, _ = sticker_rig(scope, describer)
    run(with_intake(rig, [sticker_update(1, sticker("anim", is_animated=True))]))
    assert describer.paths == [] and rig.called("get_file") == []
    assert "animated sticker" in rig.events[0].text


def test_a_failed_description_is_not_remembered(scope: Scope) -> None:
    describer = Describer()
    describer.fail = True
    rig, path = sticker_rig(scope, describer)

    async def main() -> None:
        await with_intake(rig, [sticker_update(1, sticker())])
        describer.fail = False
        rig.events.clear()

    run(main())
    assert not path.exists()
    rig2, _ = sticker_rig(scope, describer)
    run(with_intake(rig2, [sticker_update(2, sticker())]))
    assert len(describer.paths) == 2 and "It shows" in rig2.events[0].text


def test_fresh_descriptions_are_budgeted_per_sender(scope: Scope) -> None:
    describer = Describer()
    rig, _ = sticker_rig(scope, describer, fresh_per_hour=2)
    updates = [sticker_update(n, sticker(u)) for n, u in enumerate(("stk1", "stk2", "stk3"), 1)]
    run(with_intake(rig, updates))
    assert len(describer.paths) == 2
    assert "could not be described" in rig.events[2].text


def test_group_stickers_not_addressed_to_the_bot_are_not_described(scope: Scope) -> None:
    describer = Describer()
    rig, _ = sticker_rig(scope, describer)
    run(with_intake(rig, [sticker_update(1, sticker(), chat=Chat(-100, "supergroup", "G"))]))
    assert describer.paths == []


def test_without_an_intake_the_sticker_gets_the_emoji_note() -> None:
    rig = Rig()

    async def main() -> None:
        await up(rig)
        assert rig.adapter.admit(sticker_update(1, sticker()))
        await down(rig)

    run(main())
    assert "could not be described" in rig.events[0].text
    assert rig.called("get_file") == []


def test_concurrent_arrivals_share_one_description() -> None:
    clock = FakeClock(START)
    calls: list[Path] = []

    async def main() -> list[str]:
        release = asyncio.Event()

        async def describe(path: Path) -> str:
            calls.append(path)
            await release.wait()
            return "A dog"

        async def save(stream: Any) -> Path:
            return Path("/nonexistent/sticker.webp")

        notes = StickerNotes(StickerCache(None), clock)
        notes.describe, notes.save = describe, save

        async def download(ref: FileRef) -> AsyncIterator[bytes]:
            yield b""

        tasks = [
            asyncio.ensure_future(
                notes.note_for(sticker(), sender=USER, addressed=True, download=download)
            )
            for _ in range(3)
        ]
        await checkpoint()
        await checkpoint()
        release.set()
        return list(await asyncio.gather(*tasks))

    texts = run(main())
    assert len(calls) == 1 and all("A dog" in t for t in texts)


def test_the_cache_drops_the_least_recently_used_and_survives_bad_files(tmp_path: Path) -> None:
    path = tmp_path / "stickers.json"

    async def main() -> None:
        cache = StickerCache(path, cap=3)
        for n in range(3):
            await cache.put(f"u{n}", StickerEntry(f"d{n}"))
        assert await cache.get("u0") is not None  # u0 used again: u1 is now the oldest
        await cache.put("u3", StickerEntry("d3"))
        assert "u1" not in cache and len(cache) == 3
        reloaded = StickerCache(path, cap=3)
        assert (await reloaded.get("u0")) == StickerEntry("d0")
        assert "u1" not in reloaded
        path.write_text("{not json", encoding="utf-8")
        broken = StickerCache(path)
        assert await broken.get("u0") is None and len(broken) == 0

    run(main())
    assert STICKER_CACHE_MAX == 2000
