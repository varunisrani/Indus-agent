"""Compression lineage walker (P02-08 / P02-12 chain rules): root, tip, lineage, members."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from typing import Any

import pytest

from indus.state import lineage, sessions
from indus.state.lineage import AmbiguousChain
from indus.state.registry import StoreHandle
from tests.unit.state._helpers_a import chain, rotate, seed, sql


def _walk(store: StoreHandle, fn: Callable[[sqlite3.Connection, str], Any], sid: str) -> Any:
    with store.read() as conn:
        return fn(conn, sid)


def test_three_segments_resolve_from_any_member(store: StoreHandle) -> None:
    ids = chain(store, ["s1", "s2", "s3"])
    for member in ids:
        assert _walk(store, lineage.root_in, member) == "s1"
        assert _walk(store, lineage.tip_in, member) == "s3"
        assert _walk(store, lineage.lineage_ids_in, member) == ids
    assert _walk(store, lineage.ancestors_in, "s2") == ["s1", "s2"]
    assert _walk(store, lineage.forward_in, "s2") == ["s2", "s3"]


def test_plain_session_is_its_own_lineage(store: StoreHandle) -> None:
    seed(store, "alone")
    assert _walk(store, lineage.lineage_ids_in, "alone") == ["alone"]
    assert _walk(store, lineage.root_in, "missing") == "missing"
    assert _walk(store, lineage.tip_in, "missing") == "missing"


def test_branch_child_of_a_compressed_parent_is_not_a_segment(store: StoreHandle) -> None:
    chain(store, ["p", "p2"])
    marker = json.dumps({sessions.MARKER_BRANCHED: "p"})
    seed(store, "fork", parent_session_id="p", model_config=marker)
    assert _walk(store, lineage.tip_in, "p") == "p2"
    assert _walk(store, lineage.root_in, "fork") == "fork"


def test_inherited_marker_naming_another_id_still_continues(store: StoreHandle) -> None:
    seed(store, "a")
    rotate(store, "a", "b", model_config={sessions.MARKER_BRANCHED: "elsewhere"})
    assert _walk(store, lineage.lineage_ids_in, "b") == ["a", "b"]


def test_child_of_an_ordinary_end_is_not_a_continuation(store: StoreHandle) -> None:
    seed(store, "a")
    sessions.end(store, "a", "user_exit")
    seed(store, "b", parent_session_id="a")
    assert _walk(store, lineage.root_in, "b") == "b"
    assert _walk(store, lineage.tip_in, "a") == "a"


def test_two_live_continuations_fail_closed(store: StoreHandle) -> None:
    seed(store, "p")
    rotate(store, "p", "c1")
    seed(store, "c2", parent_session_id="p")
    with pytest.raises(AmbiguousChain) as caught:
        _walk(store, lineage.tip_in, "p")
    assert caught.value.code == "store.ambiguous_chain"
    assert sorted(caught.value.candidates) == ["c1", "c2"]
    # members_in never raises: every segment is listed once, root first.
    assert _walk(store, lineage.members_in, "c2") == ["p", "c1", "c2"]


def test_a_live_continuation_beats_a_closed_sibling(store: StoreHandle) -> None:
    seed(store, "p")
    rotate(store, "p", "stale")
    sessions.end(store, "stale", "ws_disconnect")
    seed(store, "live", parent_session_id="p")
    assert _walk(store, lineage.tip_in, "p") == "live"


def test_rotated_continuation_counts_as_live(store: StoreHandle) -> None:
    chain(store, ["p", "mid", "end"])
    seed(store, "closed", parent_session_id="p")
    sessions.end(store, "closed", "user_exit")
    assert _walk(store, lineage.tip_in, "p") == "end"


def test_only_closed_continuations(store: StoreHandle) -> None:
    seed(store, "p")
    rotate(store, "p", "only")
    sessions.end(store, "only", "user_exit")
    assert _walk(store, lineage.tip_in, "p") == "only"
    seed(store, "other", parent_session_id="p")
    sessions.end(store, "other", "tui_close")
    with pytest.raises(AmbiguousChain):
        _walk(store, lineage.tip_in, "p")


def test_twelve_hundred_segments_have_no_step_cap(store: StoreHandle) -> None:
    ids = [f"seg{i:04d}" for i in range(1200)]

    def build(conn: sqlite3.Connection) -> None:
        for index, sid in enumerate(ids):
            parent = ids[index - 1] if index else None
            reason = None if index == len(ids) - 1 else sessions.END_COMPRESSION
            conn.execute(
                "INSERT INTO sessions (id, source, started_at, parent_session_id, ended_at, "
                "end_reason) VALUES (?, 'cli', ?, ?, ?, ?)",
                (sid, 1_700_000_000.0 + index, parent, 1.0 if reason else None, reason),
            )

    store.write(build)
    assert _walk(store, lineage.tip_in, ids[0]) == ids[-1]
    assert _walk(store, lineage.root_in, ids[-1]) == ids[0]
    assert len(_walk(store, lineage.lineage_ids_in, ids[600])) == 1200


def test_parent_cycle_stops_at_the_repeat(store: StoreHandle) -> None:
    chain(store, ["x", "y"])
    sessions.end(store, "y", sessions.END_COMPRESSION)
    sql(store, "UPDATE sessions SET parent_session_id = 'y' WHERE id = 'x'")
    found = _walk(store, lineage.lineage_ids_in, "x")
    assert sorted(found) == ["x", "y"] and len(found) == 2
    assert sorted(_walk(store, lineage.members_in, "y")) == ["x", "y"]


def test_tool_and_delegate_children_are_not_segments(store: StoreHandle) -> None:
    seed(store, "p")
    sessions.end(store, "p", sessions.END_COMPRESSION)
    seed(store, "t", source="tool", parent_session_id="p")
    seed(store, "d", parent_session_id="p", model_config={sessions.MARKER_DELEGATE: "p"})
    assert _walk(store, lineage.continuations_in, "p") == []
    assert _walk(store, lineage.tip_in, "p") == "p"
