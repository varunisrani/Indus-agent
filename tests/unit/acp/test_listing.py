"""P19-07: ``session/list`` — paging, cursors, folder filter, ACP rows only (R09.16)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.acp import store_view
from indus.acp.agent import AcpAgent
from indus.acp.backend import HostSelector, ListedSession
from indus.acp.listing import PAGE_SIZE, page, title_of
from indus.foundation.scope import Scope
from indus.state import messages, registry, sessions, titles
from indus.state.registry import StoreHandle
from tests.fakes.acp_client import AcpPair
from tests.unit.backends._aio import aio


@pytest.fixture
def store(scope: Scope) -> Iterator[StoreHandle]:
    handle = registry.open_unshared(scope)
    yield handle
    handle.close()


def rows(count: int, cwd: str = "/w") -> list[ListedSession]:
    return [
        ListedSession(f"s{i:03d}", cwd, None, f"ask {i}", float(1000 - i)) for i in range(count)
    ]


def test_pages_hold_fifty_and_the_cursor_is_the_last_id() -> None:
    every = rows(120)
    first, cursor = page(every)
    assert len(first) == PAGE_SIZE == 50 and cursor == "s049"
    second, cursor = page(every, cursor=cursor)
    assert [r.session_id for r in second][:1] == ["s050"] and cursor == "s099"
    third, cursor = page(every, cursor=cursor)
    assert len(third) == 20 and cursor is None


def test_an_unknown_cursor_gives_an_empty_page_never_the_first() -> None:
    assert page(rows(120), cursor="nope") == ([], None)
    assert page(rows(10), cursor="s009") == ([], None)


def test_next_cursor_only_when_more_remain() -> None:
    assert page(rows(50))[1] is None
    assert page(rows(51))[1] == "s049"


def test_the_folder_filter_resolves_symlinked_spellings(tmp_path: Path) -> None:
    real = tmp_path / "private" / "var"
    (real / "x").mkdir(parents=True)
    os.symlink(real, tmp_path / "var")
    listed = [
        ListedSession("a", str(real / "x"), None, "", 2.0),
        ListedSession("b", str(tmp_path / "elsewhere"), None, "", 1.0),
    ]
    shown, _ = page(listed, cwd=str(tmp_path / "var" / "x"))
    assert [r.session_id for r in shown] == ["a"]
    shown, _ = page([ListedSession("w", "/mnt/e/proj", None, "", 1.0)], cwd="E:\\proj")
    assert [r.session_id for r in shown] == ["w"]


def test_titles_fall_back_to_the_ask_then_the_folder() -> None:
    assert title_of(ListedSession("a", "/w/proj", "Named", "ask", None)) == "Named"
    assert title_of(ListedSession("a", "/w/proj", None, " ask ", None)) == "ask"
    assert title_of(ListedSession("a", "/w/proj/", None, "", None)) == "proj"
    assert title_of(ListedSession("a", None, None, "", None)) == "New conversation"


def _seed(store: StoreHandle, sid: str, source: str, cwd: str, *, rows_: int = 1) -> None:
    sessions.create(store, sid, source, cwd=cwd)
    for _ in range(rows_):
        messages.append(store, sid, {"role": "user", "content": f"hello from {sid}"})


def test_only_acp_conversations_with_messages_are_listed(scope: Scope, store: StoreHandle) -> None:
    _seed(store, "acp-1", "acp", "/w")
    _seed(store, "cli-1", "cli", "/w")
    _seed(store, "acp-empty", "acp", "/w", rows_=0)
    titles.set_title(store, "acp-1", "My chat")
    found = store_view.listed(scope)
    assert [(r.session_id, r.title) for r in found] == [("acp-1", "My chat")]


@aio
async def test_the_handler_pages_through_the_store(scope: Scope, store: StoreHandle) -> None:
    for i in range(120):
        _seed(store, f"s{i:03d}", "acp", "/w")

    async def never() -> Any:
        raise AssertionError("listing needs no host")

    agent = AcpAgent(
        scope,
        HostSelector(local_only=False, allow_fallback=False, attached=never, local=never),
        env={},
    )
    pair = AcpPair(agent)
    try:
        sizes: list[int] = []
        seen: list[str] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"cwd": "/w"}
            if cursor:
                params["cursor"] = cursor
            result = await pair.call("session/list", params)
            sizes.append(len(result["sessions"]))
            seen += [s["sessionId"] for s in result["sessions"]]
            cursor = result.get("nextCursor")
            if cursor is None:
                break
        assert sizes == [50, 50, 20]
        assert len(set(seen)) == 120
        first = (await pair.call("session/list", {}))["sessions"][0]
        assert set(first) == {"sessionId", "cwd", "title", "updatedAt"}
        assert first["updatedAt"].endswith("+00:00")
        assert (await pair.call("session/list", {"cursor": "unknown"}))["sessions"] == []
        assert (await pair.call("session/list", {"cwd": "/elsewhere"}))["sessions"] == []
    finally:
        await pair.close()
