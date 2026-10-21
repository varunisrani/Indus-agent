"""P24-31: the ``bots.*`` table — canonical chat identity (R08.11), metadata, avatars."""

from __future__ import annotations

import base64
import json
import sqlite3
import threading
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope, make_scope
from indus.service.bots_rpc import (
    BOT_FILE,
    MAX_AVATAR_BYTES,
    METHODS,
    BadAvatar,
    BotsRpc,
    BotsRpcError,
    StoreCanonicalChats,
    UnknownBot,
)
from indus.state import registry, sessions, titles
from indus.state.bot_chat import CANONICAL_TITLE
from indus.state.registry import StoreHandle


class FakeProfiles:
    def __init__(self, root: Path, names: Sequence[str]) -> None:
        self.root = root
        self._names = list(names)
        for name in self._names:
            (root / name).mkdir(parents=True, exist_ok=True)

    def names(self) -> Sequence[str]:
        return list(self._names)

    def home(self, profile: str) -> Path:
        return self.root / profile


class FakeRoutines:
    def __init__(self, jobs: Mapping[str, Sequence[Mapping[str, Any]]] | None = None) -> None:
        self._jobs = dict(jobs or {})

    def jobs(self, profile: str) -> Sequence[Mapping[str, Any]]:
        return list(self._jobs.get(profile, ()))


class CountingChats(StoreCanonicalChats):
    """The daemon's chats (state.bot_chat over each profile's store), counting the mints; with
    ``between_looks`` set, an ensure waits for it first (two opens race for the mint)."""

    def __init__(self, profiles: FakeProfiles, scope: Scope) -> None:
        super().__init__(profiles, scope, open_store=registry.open_unshared)
        self.mints = 0
        self.between_looks: threading.Event | None = None
        self._count = threading.Lock()

    def ensure(self, profile: str) -> tuple[str, bool]:
        if self.between_looks is not None:
            self.between_looks.wait(2.0)
        session_id, created = super().ensure(profile)
        if created:
            with self._count:
                self.mints += 1
        return session_id, created


type World = tuple[BotsRpc, CountingChats, dict[str, StoreHandle]]


@pytest.fixture
def world(scope: Scope, tmp_path: Path) -> Iterator[World]:
    root = tmp_path / "bots"
    profiles = FakeProfiles(root, ["alpha", "beta"])
    stores: dict[str, StoreHandle] = {}
    for name in profiles.names():
        other = make_scope(profiles.home(name), env=scope.env, clock=scope.clock)
        stores[name] = registry.open_unshared(other)
    chats = CountingChats(profiles, scope)
    routines = FakeRoutines(
        {"alpha": [{"id": "j1", "name": "daily digest", "schedule": "0 9 * * *", "enabled": True}]}
    )
    yield BotsRpc(profiles, chats, routines), chats, stores
    for handle in stores.values():
        handle.close()


def test_open_creates_the_forever_chat_once_and_hidden(world: World) -> None:
    rpc, chats, stores = world
    first = rpc.call("bots.open", {"profile": "alpha"})
    again = rpc.call("bots.open", {"profile": "alpha"})
    assert first["created"] is True and again == {
        "session_id": first["session_id"],
        "created": False,
    }
    assert chats.mints == 1
    with stores["alpha"].read() as conn:
        row = conn.execute(
            "SELECT title, hidden FROM sessions WHERE id = ?", (first["session_id"],)
        ).fetchone()
    assert row["title"] == CANONICAL_TITLE and row["hidden"] == 1


def test_two_concurrent_opens_create_one_chat(world: World) -> None:
    rpc, chats, _ = world
    chats.between_looks = threading.Event()  # both callers pass the first look before minting
    answers: list[dict[str, object]] = []
    workers = [
        threading.Thread(target=lambda: answers.append(rpc.call("bots.open", {"profile": "beta"})))
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    chats.between_looks.set()
    for worker in workers:
        worker.join(5)
    assert len(answers) == 2 and chats.mints == 1
    assert answers[0]["session_id"] == answers[1]["session_id"]
    assert sorted(bool(a["created"]) for a in answers) == [False, True]


def test_open_after_compression_returns_the_live_tip(world: World) -> None:
    rpc, _, stores = world
    root = str(rpc.call("bots.open", {"profile": "alpha"})["session_id"])
    store = stores["alpha"]
    now = store.scope.clock.now().timestamp()

    def compress(conn: sqlite3.Connection) -> None:
        sessions.create_in(conn, "tip-1", "bots", now=now + 1, parent_session_id=root, hidden=1)
        conn.execute(
            "UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ?",
            (now + 1, sessions.END_COMPRESSION, root),
        )
        titles.set_title_in(conn, "tip-1", CANONICAL_TITLE, source="user")

    store.write(compress)
    opened = rpc.call("bots.open", {"profile": "alpha"})
    assert opened == {"session_id": "tip-1", "created": False}
    listed = {b["profile"]: b for b in rpc.call("bots.list")["bots"]}
    assert listed["alpha"]["canonical"] == {"session_id": "tip-1"}


def test_the_list_carries_the_canonical_row_routines_and_metadata(world: World) -> None:
    rpc, _, _ = world
    before = {b["profile"]: b for b in rpc.call("bots.list")["bots"]}
    assert before["alpha"]["canonical"] is None  # listing never creates
    assert before["alpha"]["routines"] == [
        {"id": "j1", "name": "daily digest", "schedule": "0 9 * * *", "enabled": True}
    ]
    assert before["beta"]["avatar"] == {"kind": "face", "seed": "beta"}
    opened = rpc.call("bots.open", {"profile": "alpha"})
    rows = rpc.profile_rows(
        [{"name": "alpha", "is_default": True}, {"name": "beta"}, {"name": "x"}]
    )
    assert rows[0]["canonical_session"] == {"session_id": opened["session_id"]}
    assert rows[0]["is_default"] is True
    assert rows[1]["canonical_session"] is None and rows[2]["canonical_session"] is None


def test_update_persists_hidden_sections_and_a_face(world: World, tmp_path: Path) -> None:
    rpc, _, _ = world
    done = rpc.call(
        "bots.update",
        {
            "profile": "beta",
            "hidden": True,
            "sections": ["Work", " Work ", "", "Home"],
            "avatar": {"kind": "face", "seed": "s-42"},
        },
    )["bot"]
    assert done["hidden"] is True and done["sections"] == ["Work", "Home"]
    stored = json.loads((tmp_path / "bots" / "beta" / BOT_FILE).read_text(encoding="utf-8"))
    assert stored["avatar"] == {"kind": "face", "seed": "s-42"} and stored["hidden"] is True
    listed = {b["profile"]: b for b in rpc.call("bots.list")["bots"]}
    assert listed["beta"]["hidden"] is True


def test_avatars_are_checked(world: World) -> None:
    rpc, _, _ = world
    small = "data:image/png;base64," + base64.b64encode(b"\x89PNG" + b"0" * 64).decode()
    ok = rpc.call("bots.update", {"profile": "alpha", "avatar": {"kind": "upload", "data": small}})
    assert ok["bot"]["avatar"]["kind"] == "upload"
    big = "data:image/png;base64," + base64.b64encode(b"0" * (MAX_AVATAR_BYTES + 1)).decode()
    for avatar in (
        {"kind": "upload", "data": "data:image/svg+xml;base64,PHN2Zz4="},
        {"kind": "portrait", "data": "not a data url"},
        {"kind": "face", "seed": "  "},
    ):
        with pytest.raises(BadAvatar):
            rpc.call("bots.update", {"profile": "alpha", "avatar": avatar})
    with pytest.raises((BadAvatar, BotsRpcError)):
        rpc.call("bots.update", {"profile": "alpha", "avatar": {"kind": "upload", "data": big}})
    pet = rpc.call("bots.update", {"profile": "alpha", "avatar": {"kind": "pet"}})
    assert pet["bot"]["avatar"] == {"kind": "pet"}


def test_bad_params_and_unknown_bots_are_refused(world: World) -> None:
    rpc, _, _ = world
    with pytest.raises(UnknownBot):
        rpc.call("bots.open", {"profile": "ghost"})
    with pytest.raises(BotsRpcError):
        rpc.call("bots.open", {"profile": "alpha", "extra": 1})
    with pytest.raises(BotsRpcError):
        rpc.call("bots.nope", {})
    assert sorted(rpc.handlers()) == sorted(METHODS)
