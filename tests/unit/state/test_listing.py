"""Session listings: previews, activity, visibility, filters, chain projection, recent browse."""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from indus.state import listing
from indus.state.handoff import HANDOFF_FINISH
from indus.state.listing import ListFilters, SessionSummary
from indus.state.registry import StoreHandle
from tests.unit.state._helpers_b import add_message, add_session, now, update_session
from tests.unit.state.conftest import AutoClock


def ids(rows: list[SessionSummary]) -> list[str]:
    return [r.id for r in rows]


def listed(store: StoreHandle, **filters: Any) -> list[str]:
    return ids(listing.list_sessions(store, ListFilters(**filters)))


def by_id(store: StoreHandle, sid: str, **filters: Any) -> SessionSummary:
    rows = {r.id: r for r in listing.list_sessions(store, ListFilters(**filters), limit=100)}
    return rows[sid]


# -- previews ----------------------------------------------------------------------------------

CJK = "会话记录的预览应当按字符截断而不是按字节截断" * 4


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("first line\nsecond line\r\nthird", "first line second line  third"),
        ("x" * 200, "x" * 60 + "..."),
        ("y" * 60, "y" * 60),
        ("z" * 61, "z" * 60 + "..."),
        ("  padded  ", "padded"),
        (CJK, CJK[:60] + "..."),
        ("", ""),
        (None, ""),
    ],
)
def test_preview_table(text: str | None, expected: str) -> None:
    assert listing.shape_preview(text) == expected


def test_preview_skips_hidden_rows_and_handoffs(store: StoreHandle) -> None:
    add_session(store, "s1")
    add_message(store, "s1", "system", "be brief")
    add_message(store, "s1", "user", "taken back", active=0)
    add_message(store, "s1", "user", "summary of before", finish_reason=HANDOFF_FINISH)
    add_message(store, "s1", "user", "")
    add_message(store, "s1", "user", "real\nask")
    add_message(store, "s1", "user", "later ask")
    assert by_id(store, "s1").preview == "real ask"


def test_preview_of_multimodal_and_empty_sessions(store: StoreHandle) -> None:
    add_session(store, "img")
    add_message(store, "img", "user", [{"type": "image_url"}, {"type": "text", "text": "what is"}])
    add_session(store, "none")
    assert by_id(store, "img").preview == "what is"
    assert by_id(store, "none").preview == ""


def test_preview_follows_id_order_not_timestamps(store: StoreHandle) -> None:
    add_session(store, "s1")
    t = now(store)
    add_message(store, "s1", "user", "first written", timestamp=t)
    add_message(store, "s1", "user", "older stamp", timestamp=t - 500)
    assert by_id(store, "s1").preview == "first written"


# -- activity ----------------------------------------------------------------------------------


def test_far_future_activity_is_ignored(store: StoreHandle) -> None:
    t = now(store)
    far = datetime(2099, 1, 1, tzinfo=UTC).timestamp()
    add_session(store, "s1", started_at=t - 100, last_activity_at=far)
    assert by_id(store, "s1").last_active == t - 100
    add_message(store, "s1", "user", "hi", timestamp=t - 10)
    add_message(store, "s1", "user", "bad clock", timestamp=far)
    assert by_id(store, "s1").last_active == t - 10


def test_freshest_of_activity_messages_and_start(store: StoreHandle) -> None:
    t = now(store)
    add_session(store, "s1", started_at=t - 300, last_activity_at=t - 5)
    add_message(store, "s1", "user", "hi", timestamp=t - 50)
    assert by_id(store, "s1").last_active == t - 5
    add_session(store, "old", started_at=datetime(2010, 1, 1, tzinfo=UTC).timestamp())
    assert by_id(store, "old").last_active is None


def test_unread_flag(store: StoreHandle) -> None:
    t = now(store)
    add_session(store, "read", started_at=t - 100)
    add_session(store, "seen", started_at=t - 100, last_read_at=t - 50)
    add_session(store, "fresh", started_at=t - 100, last_read_at=0)
    rows = {r.id: r.unread for r in listing.list_sessions(store)}
    assert rows == {"read": False, "seen": False, "fresh": True}
    add_message(store, "seen", "user", "new", timestamp=t - 10)
    assert by_id(store, "seen").unread


# -- visibility --------------------------------------------------------------------------------


def chain(store: StoreHandle, *, tip_title: str | None = "tip title") -> None:
    t = now(store)
    add_session(
        store,
        "root",
        title="root title",
        started_at=t - 1000,
        ended_at=t - 500,
        end_reason="compression",
        message_count=7,
    )
    add_message(store, "root", "user", "opening ask", timestamp=t - 990)
    add_session(
        store,
        "tip",
        title=tip_title,
        parent_session_id="root",
        started_at=t - 500,
        model="model-b",
        model_config={"_branched_from": "someone-else"},
    )
    add_message(
        store, "tip", "user", "the summary", finish_reason=HANDOFF_FINISH, timestamp=t - 500
    )
    add_message(store, "tip", "user", "follow-up ask", timestamp=t - 20)


def test_compression_chain_is_one_row_with_tip_fields(store: StoreHandle) -> None:
    chain(store)
    add_session(store, "newer", started_at=now(store) - 10)
    rows = listing.list_sessions(store)
    assert ids(rows) == ["newer", "tip"]
    tip = rows[1]
    assert tip.title == "tip title"
    assert tip.started_at == now(store) - 1000  # the root's start keeps the order stable
    assert (tip.root_id, tip.lineage_ids) == ("root", ("root", "tip"))
    assert (tip.model, tip.message_count, tip.end_reason) == ("model-b", 2, None)
    assert tip.preview == "follow-up ask"
    assert tip.last_active == now(store) - 20
    assert tip.kind == "root"


def test_untitled_tip_shows_the_root_title(store: StoreHandle) -> None:
    chain(store, tip_title=None)
    row = listing.list_sessions(store)[0]
    assert (row.id, row.title) == ("tip", "root title")


def test_competing_continuations_keep_the_root(store: StoreHandle) -> None:
    chain(store)
    add_session(store, "rival", parent_session_id="root")
    row = listing.list_sessions(store)[0]
    assert (row.id, row.root_id, row.title) == ("root", None, "root title")


def test_internal_and_subagent_sources(store: StoreHandle) -> None:
    for source in ("cli", "kanban", "tool", "oneshot", "subagent"):
        add_session(store, f"s-{source}", source)
    assert listed(store) == ["s-cli"]
    assert sorted(listed(store, include_subagents=True)) == ["s-cli", "s-subagent"]
    assert listed(store, sources=["kanban"]) == ["s-kanban"]
    assert len(listed(store, include_internal=True)) == 5
    assert listed(store, exclude_sources=["cli"]) == []


def test_children_by_kind(store: StoreHandle) -> None:
    t = now(store)
    add_session(store, "p", started_at=t - 100)
    add_session(store, "br", parent_session_id="p", model_config={"_branched_from": "p"})
    add_session(store, "rs", parent_session_id="p", model_config={"_reset_from": "p"})
    add_session(store, "dg", parent_session_id="p", model_config={"_delegate_from": "p"})
    add_session(store, "run", parent_session_id="p")
    rows = {r.id: r.kind for r in listing.list_sessions(store)}
    assert rows == {"p": "root", "br": "branch", "rs": "reset"}
    wide = {r.id: r.kind for r in listing.list_sessions(store, ListFilters(include_subagents=True))}
    assert wide == {"p": "root", "br": "branch", "rs": "reset", "dg": "delegate", "run": "delegate"}


# -- filters -----------------------------------------------------------------------------------


def test_cwd_prefix_and_workspace(store: StoreHandle) -> None:
    add_session(store, "exact", cwd="/work/proj")
    add_session(store, "below", cwd="/work/proj/src")
    add_session(store, "sibling", cwd="/work/project2")
    add_session(store, "wild", cwd="/work/pxoj")
    add_session(store, "win", cwd="C:\\w\\p\\sub")
    add_session(store, "repo", cwd="/elsewhere", git_root="/work/proj")
    assert sorted(listed(store, cwd_prefix="/work/proj/")) == ["below", "exact"]
    assert listed(store, cwd_prefix="/work/p_oj") == []
    assert listed(store, cwd_prefix="C:\\w\\p") == ["win"]
    assert sorted(listed(store, workspace="/work/proj")) == ["below", "exact", "repo"]


def test_counts_and_tristate_flags(store: StoreHandle) -> None:
    add_session(store, "plain", message_count=3)
    add_session(store, "arch", archived=1)
    add_session(store, "hid", hidden=1)
    add_session(store, "pin", pinned=1)
    assert sorted(listed(store)) == ["pin", "plain"]
    assert listed(store, min_messages=2) == ["plain"]
    assert listed(store, archived=True) == ["arch"]
    add_session(store, "arch-hid", archived=1, hidden=1)
    assert sorted(listed(store, archived=True)) == ["arch", "arch-hid"]  # recovery view
    assert listed(store, archived=True, hidden=True) == ["arch-hid"]
    assert sorted(listed(store, archived=None)) == ["arch", "pin", "plain"]
    assert listed(store, hidden=True) == ["hid"]
    assert listed(store, pinned=True) == ["pin"]
    assert listed(store, pinned=False) == ["plain"]


def test_query_by_id_title_and_first_ask_across_the_chain(store: StoreHandle) -> None:
    chain(store)
    update_session(store, "tip", title="AN-94 rifle notes")
    add_session(store, "other", title="groceries")
    add_message(store, "other", "user", "Buy MILK and eggs")
    assert listed(store, query="an94") == ["tip"]  # punctuation-free title match on the tip
    assert listed(store, query="TIP") == ["tip"]  # id of a chain member
    assert listed(store, query="opening") == ["tip"]  # the root's first ask
    assert listed(store, query="milk") == ["other"]
    assert listed(store, query="100%") == []
    assert listed(store, query="nothing like this") == []


def test_order_limit_offset(store: StoreHandle) -> None:
    t = now(store)
    for i in range(5):
        add_session(store, f"s{i}", started_at=t - i)
    page = listing.list_sessions(store, limit=2, offset=1)
    assert ids(page) == ["s1", "s2"]
    page = listing.list_sessions(store, ListFilters(query="s"), limit=2, offset=3)
    assert ids(page) == ["s3", "s4"]


# -- recent ------------------------------------------------------------------------------------


def test_recent_orders_by_activity_and_resolves_chains(store: StoreHandle) -> None:
    chain(store)
    t = now(store)
    add_session(store, "quiet", started_at=t - 50)
    add_session(store, "busy", started_at=t - 2000, last_activity_at=t - 1)
    add_session(store, "gone", started_at=t - 3, archived=1)
    add_session(store, "bot", "kanban", started_at=t - 2)
    rows = listing.recent(store, limit=10)
    assert ids(rows) == ["busy", "tip", "quiet"]
    assert listing.recent(store, limit=1)[0].id == "busy"


class SlowConnection:
    """A read connection whose every statement costs fake time."""

    def __init__(self, real: sqlite3.Connection, clock: AutoClock, cost: float) -> None:
        self.real = real
        self.clock = clock
        self.cost = cost

    def execute(self, sql: str, parameters: Any = (), /) -> Any:
        self.clock.advance(self.cost)
        return self.real.execute(sql, parameters)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.real, name)


def test_recent_stops_at_the_budget(
    store: StoreHandle, fake_clock: AutoClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = now(store)
    for i in range(40):
        add_session(store, f"s{i:02d}", started_at=t - i)
    real_read = store.read

    @contextlib.contextmanager
    def slow_read() -> Iterator[Any]:
        with real_read() as conn:
            yield SlowConnection(conn, fake_clock, 0.2)

    monkeypatch.setattr(store, "read", slow_read)
    start = fake_clock.monotonic()
    rows = listing.recent(store, limit=40, budget=3.0)
    spent = fake_clock.monotonic() - start
    assert 0 < len(rows) < 40
    assert ids(rows) == [f"s{i:02d}" for i in range(len(rows))]  # the freshest come first
    assert spent < 3.0 + 2.0
