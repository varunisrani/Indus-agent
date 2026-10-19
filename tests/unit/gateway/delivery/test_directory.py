"""P15-20: the channel directory (build, persistence, refresh, name lookup) and mirror lookup."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.gateway.adapters.base import ChannelEntry
from indus.gateway.delivery import mirror
from indus.gateway.delivery.directory import REFRESH_SECONDS, ChannelDirectory, stop
from indus.state import gateway_routes as routes
from indus.state import registry as store_registry
from indus.state.registry import StoreHandle
from tests.fakes.clock import FakeClock
from tests.unit.gateway.delivery.kit import settle, until
from tests.unit.gateway.outbound.recording import RecordingAdapter, recording_adapter


@pytest.fixture
def store(scope: Scope) -> Iterator[StoreHandle]:
    handle = store_registry.open_unshared(scope)
    yield handle
    handle.close()


def _route(
    store: StoreHandle, key: str, sid: str, chat: str, *, thread: str | None = None, **origin: Any
) -> None:
    full = {"platform": "telegram", "chat_id": chat, "chat_type": "group", **origin}
    if thread:
        full["thread_id"] = thread
    routes.get_or_create(
        store,
        key,
        new_session_id=lambda: sid,
        platform="telegram",
        chat_type="group",
        origin=full,
    )


def _lister(adapter: RecordingAdapter, entries: list[ChannelEntry]) -> list[int]:
    calls: list[int] = []

    async def listed() -> list[ChannelEntry]:
        calls.append(1)
        return entries

    adapter.list_channels = listed  # type: ignore[method-assign]
    return calls


def test_build_from_adapter_and_routes_and_persist(scope: Scope, store: StoreHandle) -> None:
    discord = recording_adapter(name="discord")
    _lister(
        discord,
        [
            ChannelEntry("111", "general", guild="Indus"),
            ChannelEntry("111", "general", guild="Indus"),  # duplicates collapse
            ChannelEntry("222", "general-chat", guild="Other"),
        ],
    )
    telegram = recording_adapter(name="telegram")  # cannot list: routes give its chats
    _route(store, "k1", "s1", "-100", chat_name="Team")
    _route(store, "k2", "s2", "-100", thread="9", chat_name="Team")
    webhook = recording_adapter(name="webhook")
    directory = ChannelDirectory(scope)
    asyncio.run(
        directory.build({"discord": discord, "telegram": telegram, "webhook": webhook}, store)
    )
    assert directory.platforms() == ["discord", "telegram"]
    assert [e.id for e in directory.entries("discord")] == ["111", "222"]
    tg = directory.entries("telegram")
    assert [(e.id, e.thread_id, e.name) for e in tg] == [
        ("-100", None, "Team"),
        ("-100", "9", "Team / 9"),
    ]
    assert oct(directory.path.stat().st_mode & 0o777) == "0o600"
    data = json.loads(directory.path.read_text(encoding="utf-8"))
    assert data["platforms"]["discord"][0] == {
        "id": "111",
        "name": "general",
        "type": "channel",
        "guild": "Indus",
    }
    # another process (indus send) reads the same file
    loaded = ChannelDirectory(scope).load()
    assert loaded.entries("telegram") == tg and loaded.updated_at == directory.updated_at


def test_resolve_rules(scope: Scope) -> None:
    directory = ChannelDirectory(scope)
    adapter = recording_adapter(name="discord")
    _lister(
        adapter,
        [
            ChannelEntry("111", "General", guild="Indus"),
            ChannelEntry("222", "general", guild="Other"),
            ChannelEntry("333", "ops-alerts", guild="Indus"),
            ChannelEntry("444", "ops-builds", guild="Indus"),
            ChannelEntry("555", "random", guild="Indus", thread_id="77"),
        ],
    )
    asyncio.run(directory.build({"discord": adapter}))
    assert directory.resolve("discord", "222") == ("222", None)  # exact id first
    assert directory.resolve("discord", "#general") == ("111", None)  # first name match
    assert directory.resolve("discord", "other/general") == ("222", None)
    assert directory.resolve("discord", "#ops") is None  # ambiguous prefix
    assert directory.resolve("discord", "ops-a") == ("333", None)
    assert directory.resolve("discord", "#RANDOM") == ("555", "77")
    assert directory.resolve("discord", "#nowhere") is None
    assert directory.resolve("slack", "#general") is None
    assert "discord:#General  [111]" in directory.render()


def test_corrupt_or_unwritable_file_never_breaks(scope: Scope, tmp_path: Any) -> None:
    path = scope.home / "gateway" / "channel_directory.json"
    path.parent.mkdir(parents=True)
    path.write_text("{nope", encoding="utf-8")
    assert ChannelDirectory(scope).load().platforms() == []
    blocker = tmp_path / "file"
    blocker.write_text("")
    directory = ChannelDirectory(scope, blocker / "sub" / "dir.json")
    adapter = recording_adapter(name="discord")
    _lister(adapter, [ChannelEntry("1", "a")])
    asyncio.run(directory.build({"discord": adapter}))
    assert directory.resolve("discord", "a") == ("1", None)  # kept in memory


def test_refresh_every_five_minutes(scope: Scope, fake_clock: FakeClock) -> None:
    adapter = recording_adapter(name="discord")
    entries = [ChannelEntry("1", "a")]
    calls = _lister(adapter, entries)

    async def main() -> None:
        directory = ChannelDirectory(scope)
        task = directory.start(lambda: {"discord": adapter})
        try:
            await until(lambda: len(calls) == 1)
            await settle()
            assert len(calls) == 1
            entries.append(ChannelEntry("2", "b"))
            fake_clock.advance(REFRESH_SECONDS)
            await until(lambda: len(calls) == 2)
            await until(lambda: directory.resolve("discord", "b") == ("2", None))
        finally:
            await stop(task)
        assert task.cancelled()

    asyncio.run(main())


def test_mirror_finds_one_conversation_or_none(store: StoreHandle) -> None:
    _route(store, "dm", "s-dm", "42", chat_type="dm")
    assert mirror.find_session(store, "telegram", "42") == "s-dm"
    assert mirror.find_session(store, "Telegram", "43") is None
    # a group with one conversation per person: no guessing
    _route(store, "g-a", "s-a", "-5", user_id="a")
    _route(store, "g-b", "s-b", "-5", user_id="b")
    assert mirror.find_session(store, "telegram", "-5") is None
    # a thread is its own conversation
    _route(store, "t", "s-t", "-6", thread="3")
    _route(store, "t0", "s-t0", "-6")
    assert mirror.find_session(store, "telegram", "-6", "3") == "s-t"
    assert mirror.find_session(store, "telegram", "-6") == "s-t0"


def test_mirror_notes_are_capped_and_taken_once(store: StoreHandle) -> None:
    from tests.unit.state._helpers_d import add_session

    add_session(store, "s1")
    clock = store.scope.clock
    assert isinstance(clock, FakeClock)
    for i in range(mirror.MAX_NOTES + 3):
        mirror.store_note(store, "s1", f"note {i}")
        clock.advance(1)
    taken = mirror.take_notes(store, "s1")
    assert len(taken) == mirror.MAX_NOTES and taken[0] == "note 3"
    assert mirror.take_notes(store, "s1") == []
    long = mirror.note_text("indus send", "x" * 5000)
    assert len(long) < 2200 and long.endswith("[…]")


def test_mirror_record_without_a_conversation_is_a_quiet_no(store: StoreHandle) -> None:
    recorder = mirror.Mirror(store)
    assert not asyncio.run(recorder.record("telegram", "999", None, "hi", "indus send"))
