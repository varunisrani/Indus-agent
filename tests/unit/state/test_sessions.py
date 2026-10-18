"""Session rows (P02-08): upsert, first end wins, child kinds, config, lineage flags, status."""

from __future__ import annotations

import json
from typing import Any

import pytest

from indus.foundation.errors import SessionTombstoned, StoreLocked
from indus.state import messages, sessions
from indus.state.registry import StoreHandle
from indus.state.sessions import AmbiguousSessionId, SessionNotFound
from indus.state.writer import OBSERVATION, Budget
from tests.unit.state._helpers_a import chain, column, rotate, seed, sql
from tests.unit.state.conftest import AutoClock

# -- create ---------------------------------------------------------------------------------------


def test_later_create_never_overwrites_an_earlier_value(store: StoreHandle) -> None:
    assert sessions.create(store, "s1", "cli", model="model-a", user_id="u-1") is True
    assert sessions.create(store, "s1", "cli") is False
    assert sessions.create(store, "s1", "tui", model="model-b", user_id="u-2") is False
    row = sessions.get(store, "s1")
    assert row is not None
    assert (row["model"], row["user_id"], row["source"]) == ("model-a", "u-1", "cli")
    assert row["created_source"] == "cli"


def test_placeholder_source_is_replaced_once(store: StoreHandle) -> None:
    seed(store, "s1", source="unknown")
    seed(store, "s1", source="telegram")
    seed(store, "s1", source="cli")
    assert column(store, "s1", "source") == "telegram"
    assert column(store, "s1", "created_source") == "unknown"


def test_a_later_create_fills_a_gap(store: StoreHandle) -> None:
    seed(store, "s1")
    seed(store, "s1", model="filled", cwd="/w")
    assert (column(store, "s1", "model"), column(store, "s1", "cwd")) == ("filled", "/w")


def test_child_inherits_workspace_and_profile(store: StoreHandle) -> None:
    seed(store, "p", cwd="/repo/sub", git_root="/repo", git_branch="main", profile_id="team")
    seed(store, "c", parent_session_id="p")
    seed(store, "own", parent_session_id="p", cwd="/elsewhere")
    child = sessions.get(store, "c")
    assert child is not None
    assert (child["cwd"], child["git_root"], child["git_branch"]) == ("/repo/sub", "/repo", "main")
    # ``create`` stamps the store's profile when the caller gives none; explicit wins over parent.
    assert child["profile_id"] == "work"
    assert column(store, "own", "cwd") == "/elsewhere"
    assert column(store, "own", "git_root") == "/repo"


def test_profile_inherited_in_transaction_form(store: StoreHandle) -> None:
    seed(store, "p", profile_id="team")
    store.write(lambda conn: sessions.create_in(conn, "c", "cli", now=5.0, parent_session_id="p"))
    assert column(store, "c", "profile_id") == "team"
    assert column(store, "c", "started_at") == 5.0


def test_model_config_dict_and_system_prompt(store: StoreHandle) -> None:
    seed(store, "s1", model_config={"temperature": 0.2}, system_prompt="be brief")
    row = sessions.get(store, "s1")
    assert row is not None
    assert json.loads(row["model_config"]) == {"temperature": 0.2}
    assert row["system_prompt"] == "be brief"
    assert len(row["system_prompt_hash"]) == 64


def test_tombstoned_id_cannot_be_created(store: StoreHandle) -> None:
    seed(store, "kept")
    sql(store, "INSERT INTO meta(key, value) VALUES (?, '1')", [sessions.TOMBSTONE_PREFIX + "gone"])
    sql(store, "INSERT INTO meta(key, value) VALUES (?, '1')", [sessions.TOMBSTONE_PREFIX + "kept"])
    with pytest.raises(SessionTombstoned) as caught:
        sessions.create(store, "gone", "cli")
    assert caught.value.code == "store.tombstoned"
    assert sessions.get(store, "gone") is None
    assert sessions.create(store, "kept", "cli") is False  # an existing row is never refused
    with store.read() as conn:
        assert sessions.is_tombstoned_in(conn, "gone")
        assert not sessions.is_tombstoned_in(conn, "other")


def test_bad_create_arguments(store: StoreHandle) -> None:
    with pytest.raises(TypeError, match="colour"):
        sessions.create(store, "s1", "cli", colour="red")
    with pytest.raises(ValueError):
        sessions.create(store, "", "cli")


# -- end / reopen ---------------------------------------------------------------------------------


def test_first_end_reason_wins(store: StoreHandle, fake_clock: AutoClock) -> None:
    seed(store, "s1")
    assert sessions.end(store, "s1", sessions.END_COMPRESSION) is True
    first = column(store, "s1", "ended_at")
    fake_clock.advance(30)
    assert sessions.end(store, "s1", "user_exit") is False
    assert column(store, "s1", "end_reason") == "compression"
    assert column(store, "s1", "ended_at") == first


def test_reopen_then_end_again(store: StoreHandle) -> None:
    seed(store, "s1")
    sessions.end(store, "s1", "cli_close")
    assert sessions.reopen(store, "s1") is True
    assert column(store, "s1", "ended_at") is None and column(store, "s1", "end_reason") is None
    assert sessions.end(store, "s1", "user_exit") is True
    assert column(store, "s1", "end_reason") == "user_exit"


def test_end_and_reopen_of_a_missing_row(store: StoreHandle) -> None:
    assert sessions.end(store, "nope", "user_exit") is False
    assert sessions.reopen(store, "nope") is False


def test_reason_families() -> None:
    assert sorted(sessions.COMPRESSION_REASONS) == ["compression", "orphaned_compression"]
    assert "new_session" in sessions.RESET_REASONS and "daily" in sessions.RESET_REASONS
    assert sessions.END_STARTUP_ORPHAN_REAP in sessions.RECOVERABLE_REASONS
    assert sessions.END_BRANCHED in sessions.EXPLICIT_REASONS
    assert sessions.is_automatic_end("ws_disconnect") and sessions.is_automatic_end("agent_close")
    assert not sessions.is_automatic_end("user_exit") and not sessions.is_automatic_end(None)
    families = [
        sessions.COMPRESSION_REASONS,
        sessions.RESET_REASONS,
        sessions.RECOVERABLE_REASONS,
        sessions.AUTOMATIC_REASONS,
        sessions.EXPLICIT_REASONS,
    ]
    assert sum(len(f) for f in families) == len(frozenset().union(*families))


# -- child kinds ----------------------------------------------------------------------------------


def _config(**markers: str) -> str:
    return json.dumps(markers)


@pytest.mark.parametrize(
    ("row", "parent", "kind"),
    [
        ({"parent_session_id": None}, None, "root"),
        ({"parent_session_id": "p"}, None, "continuation"),
        ({"parent_session_id": "p", "model_config": _config(_branched_from="p")}, None, "branch"),
        ({"parent_session_id": "p", "model_config": _config(_delegate_from="p")}, None, "delegate"),
        ({"parent_session_id": "p", "model_config": _config(_reset_from="p")}, None, "reset"),
        # inherited from an earlier fork: the marker names someone else, so not a fork of p
        (
            {"parent_session_id": "p", "model_config": _config(_branched_from="q")},
            None,
            "continuation",
        ),
        (
            {"parent_session_id": "p", "model_config": _config(_delegate_from="q")},
            None,
            "continuation",
        ),
        ({"parent_session_id": "p", "source": "tool"}, None, "tool"),
        ({"parent_session_id": "p"}, {"end_reason": "session_reset"}, "reset"),
        ({"parent_session_id": "p"}, {"end_reason": "branched"}, "branch"),
        ({"parent_session_id": "p"}, {"end_reason": "compression"}, "continuation"),
        ({"parent_session_id": "p", "model_config": "{broken"}, None, "continuation"),
    ],
)
def test_child_kind(row: dict[str, Any], parent: dict[str, Any] | None, kind: str) -> None:
    assert sessions.child_kind(row, parent) == kind


def test_child_kind_on_stored_rows(store: StoreHandle) -> None:
    seed(store, "a")
    rotate(store, "a", "b", model_config={sessions.MARKER_BRANCHED: "older"})
    with store.read() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = 'b'").fetchone()
        parent = conn.execute("SELECT * FROM sessions WHERE id = 'a'").fetchone()
    assert sessions.child_kind(row, parent) == "continuation"


# -- model config ---------------------------------------------------------------------------------


def test_patch_merges_deletes_and_keeps_markers(store: StoreHandle) -> None:
    seed(store, "p")
    seed(store, "c", parent_session_id="p", model_config={"_branched_from": "p", "a": 1, "b": 2})
    assert sessions.patch_model_config(store, "c", {"a": None, "c": 3, "_branched_from": None})
    assert json.loads(column(store, "c", "model_config")) == {"_branched_from": "p", "b": 2, "c": 3}
    assert sessions.patch_model_config(store, "c", {"_branched_from": "x", "b": None, "c": None})
    assert json.loads(column(store, "c", "model_config")) == {"_branched_from": "p"}
    seed(store, "plain", model_config={"only": 1})
    sessions.patch_model_config(store, "plain", {"only": None})
    assert column(store, "plain", "model_config") is None
    assert sessions.patch_model_config(store, "nope", {"a": 1}) is False
    assert sessions.patch_model_config(store, "plain", {}) is True


def test_patch_over_a_corrupt_config_starts_fresh(store: StoreHandle) -> None:
    seed(store, "s1")
    sql(store, "UPDATE sessions SET model_config = '[1, 2' WHERE id = 's1'")
    sessions.patch_model_config(store, "s1", {"k": "v"})
    assert json.loads(column(store, "s1", "model_config")) == {"k": "v"}


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ('{"yolo": true}', True),
        ('{"yolo": false}', False),
        ('{"yolo": "yes"}', False),
        ('{"yolo": 1}', False),
        ("{not json", False),
        ("[true]", False),
        ('"yolo"', False),
        ("", False),
        (None, False),
    ],
)
def test_yolo_flag_reads_false_on_any_parse_problem(stored: str | None, expected: bool) -> None:
    assert sessions.yolo_enabled({"model_config": stored}) is expected


def test_yolo_round_trip(store: StoreHandle) -> None:
    seed(store, "s1", model_config={"keep": 1})
    assert sessions.set_yolo(store, "s1", True)
    assert sessions.yolo_enabled(sessions.get(store, "s1"))
    sessions.set_yolo(store, "s1", False)
    assert not sessions.yolo_enabled(sessions.get(store, "s1"))
    assert sessions.yolo_enabled(None) is False
    assert json.loads(column(store, "s1", "model_config"))["keep"] == 1


# -- lineage-wide flags ---------------------------------------------------------------------------


def _flags(store: StoreHandle, name: str, ids: list[str]) -> list[Any]:
    return [column(store, sid, name) for sid in ids]


def test_archive_on_the_tip_archives_the_root_and_back(store: StoreHandle) -> None:
    ids = chain(store, ["r", "m", "t"])
    seed(store, "unrelated")
    assert sessions.set_archived(store, "t", True)
    assert _flags(store, "archived", ids) == [1, 1, 1]
    assert column(store, "unrelated", "archived") == 0
    assert sessions.set_archived(store, "r", False)
    assert _flags(store, "archived", ids) == [0, 0, 0]
    # a listing-style read of root and tip agrees
    with store.read() as conn:
        rows = conn.execute("SELECT DISTINCT archived FROM sessions WHERE id IN ('r', 't')")
        assert [r[0] for r in rows.fetchall()] == [0]


def test_pin_hide_and_unknown_ids(store: StoreHandle) -> None:
    ids = chain(store, ["r", "t"])
    assert sessions.set_hidden(store, "r", True)
    assert _flags(store, "hidden", ids) == [1, 1]
    assert sessions.set_pinned(store, "t", True)
    assert _flags(store, "pinned", ids) == [1, 1]
    assert _flags(store, "hidden", ids) == [0, 0]
    sessions.set_hidden(store, "t", True)
    sessions.set_pinned(store, "r", False)
    assert _flags(store, "pinned", ids) == [0, 0]
    assert _flags(store, "hidden", ids) == [1, 1]  # unpinning does not unhide
    assert sessions.set_pinned(store, "nope", True) is False
    assert sessions.mark_read(store, "nope") is False


def test_deliberate_archive_clears_the_automatic_stamp(store: StoreHandle) -> None:
    ids = chain(store, ["r", "t"])
    store.write(lambda conn: sessions.auto_archive_in(conn, "t"))
    assert _flags(store, "auto_archived", ids) == [1, 1]
    sessions.set_archived(store, "r", True)
    assert _flags(store, "auto_archived", ids) == [0, 0]
    assert _flags(store, "archived", ids) == [1, 1]


def test_reopen_lifts_only_an_automatic_archive(store: StoreHandle) -> None:
    ids = chain(store, ["r", "t"])
    sessions.end(store, "t", "idle_timeout")
    store.write(lambda conn: sessions.auto_archive_in(conn, "t"))
    sessions.reopen(store, "t")
    assert _flags(store, "archived", ids) == [0, 0]

    sessions.end(store, "t", "user_exit")
    sessions.set_archived(store, "t", True)
    sessions.reopen(store, "t")
    assert _flags(store, "archived", ids) == [1, 1]


def test_auto_archive_keeps_a_deliberate_archive_deliberate(store: StoreHandle) -> None:
    ids = chain(store, ["r", "t"])
    sql(store, "UPDATE sessions SET archived = 1 WHERE id = 'r'")
    store.write(lambda conn: sessions.auto_archive_in(conn, "t"))
    assert _flags(store, "auto_archived", ids) == [0, 1]


def test_read_watermark(store: StoreHandle, fake_clock: AutoClock) -> None:
    ids = chain(store, ["r", "t"])
    assert _flags(store, "last_read_at", ids) == [None, None]
    assert not sessions.is_unread(sessions.get(store, "t") or {})
    sessions.mark_unread(store, "t")
    assert _flags(store, "last_read_at", ids) == [0.0, 0.0]
    assert sessions.is_unread(sessions.get(store, "r") or {})
    fake_clock.advance(10)
    sessions.mark_read(store, "r")
    now = fake_clock.now().timestamp()
    assert _flags(store, "last_read_at", ids) == [now, now]
    assert not sessions.is_unread(sessions.get(store, "t") or {})
    assert sessions.is_unread({"last_read_at": now, "last_activity_at": now + 5})
    assert sessions.is_unread({"last_read_at": 100.0, "last_active": 101.0})


def test_flags_on_a_forked_chain_reach_every_segment(store: StoreHandle) -> None:
    seed(store, "p")
    rotate(store, "p", "c1")
    seed(store, "c2", parent_session_id="p")
    assert sessions.set_archived(store, "c1", True)  # c1's own path: root and itself
    assert _flags(store, "archived", ["p", "c1", "c2"]) == [1, 1, 0]
    assert sessions.set_archived(store, "p", True)  # the fork below p: every segment
    assert _flags(store, "archived", ["p", "c1", "c2"]) == [1, 1, 1]


# -- status ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "tools", "finish", "expected"),
    [
        ("assistant", False, "stop", "complete"),
        ("assistant", True, None, "interrupted"),
        ("assistant", False, "error", "error"),
        ("assistant", False, " Content_Filter ", "error"),
        ("tool", False, "agent_error", "error"),
        ("user", False, None, "interrupted"),
        ("tool", False, None, "interrupted"),
        ("system", False, None, "complete"),
        (None, False, None, "complete"),
    ],
)
def test_classify_status(role: str | None, tools: bool, finish: str | None, expected: str) -> None:
    assert sessions.classify_status(role, tools, finish) == expected


def test_status_from_the_last_live_message(store: StoreHandle) -> None:
    for sid in ("done", "pending", "fresh", "failed"):
        seed(store, sid)
    messages.append(store, "done", {"role": "user", "content": "hi"})
    messages.append(store, "done", {"role": "assistant", "content": "hello"})
    call = [{"id": "c1", "type": "function", "function": {"name": "ls", "arguments": "{}"}}]
    messages.append(store, "pending", {"role": "assistant", "content": "", "tool_calls": call})
    messages.append(
        store, "failed", {"role": "assistant", "content": "x", "finish_reason": "error"}
    )
    assert sessions.status(store, "done") == "complete"
    assert sessions.status(store, "pending") == "interrupted"
    assert sessions.status(store, "fresh") == "empty"
    found = sessions.statuses(store, ["done", "pending", "fresh", "failed", "done", ""])
    assert found == {
        "done": "complete",
        "pending": "interrupted",
        "fresh": "empty",
        "failed": "error",
    }
    # a soft-replaced (inactive) tail does not decide the status
    messages.replace(store, "done", [{"role": "user", "content": "hi"}], soft=True)
    assert sessions.status(store, "done") == "interrupted"


def test_statuses_over_more_ids_than_one_chunk(store: StoreHandle) -> None:
    seed(store, "real")
    messages.append(store, "real", {"role": "assistant", "content": "ok"})
    ids = [f"ghost{i}" for i in range(1000)] + ["real"]
    found = sessions.statuses(store, ids)
    assert len(found) == 1001 and found["real"] == "complete" and found["ghost999"] == "empty"


# -- lookups --------------------------------------------------------------------------------------


def test_prefix_lookup(store: StoreHandle, fake_clock: AutoClock) -> None:
    seed(store, "20260101_090000_aaaa11")
    fake_clock.advance(5)
    seed(store, "20260101_090000_aaaa22")
    seed(store, "20260101_100000_bbbb33")
    assert sessions.resolve_id(store, "20260101_090000_aaaa11") == "20260101_090000_aaaa11"
    assert sessions.resolve_id(store, "20260101_1") == "20260101_100000_bbbb33"
    with pytest.raises(AmbiguousSessionId) as caught:
        sessions.resolve_id(store, "20260101_090000_aaaa")
    assert caught.value.code == "store.ambiguous_id"
    assert caught.value.candidates == ["20260101_090000_aaaa22", "20260101_090000_aaaa11"]
    assert "aaaa11" in str(caught.value) and "aaaa22" in str(caught.value)
    with pytest.raises(SessionNotFound) as missing:
        sessions.resolve_id(store, "2027")
    assert missing.value.code == "store.session_not_found"
    with pytest.raises(SessionNotFound):
        sessions.resolve_id(store, "  ")


def test_prefix_lookup_treats_wildcards_literally(store: StoreHandle) -> None:
    seed(store, "abc_one")
    seed(store, "abcXtwo")
    assert sessions.resolve_id(store, "abc_") == "abc_one"
    with pytest.raises(SessionNotFound):
        sessions.resolve_id(store, "%")


def test_ambiguous_lookup_caps_the_candidate_list(store: StoreHandle) -> None:
    for index in range(14):
        seed(store, f"dup{index:02d}")
    with pytest.raises(AmbiguousSessionId) as caught:
        sessions.resolve_id(store, "dup")
    assert len(caught.value.candidates) == 10 and "and more" in str(caught.value)


# -- activity -------------------------------------------------------------------------------------


def test_activity_never_moves_backwards(store: StoreHandle) -> None:
    seed(store, "s1")
    assert sessions.touch_activity(store, "s1", "tool: ls", "tool", at=2_000_000_000.0)
    assert not sessions.touch_activity(store, "s1", "older", "model", at=1_999_999_000.0)
    assert column(store, "s1", "last_activity_at") == 2_000_000_000.0
    assert column(store, "s1", "last_activity_note") == "tool: ls"
    assert sessions.touch_activity(store, "s1", "x" * 500, at=2_000_000_001.0)
    assert len(column(store, "s1", "last_activity_note")) == 200
    assert column(store, "s1", "last_activity_provenance") is None


def test_activity_uses_the_observation_budget_and_skips_when_busy(
    store: StoreHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(store, "s1")
    budgets: list[Budget] = []

    def busy(fn: Any, *, budget: Budget) -> Any:
        budgets.append(budget)
        raise StoreLocked("busy")

    monkeypatch.setattr(store, "write", busy)
    assert sessions.touch_activity(store, "s1") is False
    assert budgets == [OBSERVATION]


def test_activity_defaults_to_the_clock(store: StoreHandle, fake_clock: AutoClock) -> None:
    seed(store, "s1")
    sessions.touch_activity(store, "s1")
    assert column(store, "s1", "last_activity_at") == fake_clock.now().timestamp()


def test_chunks_helper() -> None:
    assert [list(c) for c in sessions.chunks([1, 2, 3, 4, 5], 2)] == [[1, 2], [3, 4], [5]]
    assert list(sessions.chunks([], 3)) == []
