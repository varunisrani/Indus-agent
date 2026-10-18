"""Export and import as JSON lines (P02-17)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.errors import ResumeTooLarge
from indus.foundation.scope import Scope
from indus.state import deletion, messages, portability, registry, session_meta
from indus.state.registry import StoreHandle
from tests.unit.state._helpers_d import (
    Config,
    add_message,
    add_session,
    count,
    now_of,
    session_row,
)

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "state" / "export_small.jsonl"


def seed_small(store: StoreHandle) -> None:
    """Two conversations: a compressed one (root + continuation, tool calls, an image part) and
    a short one with metadata and a rewound row."""
    t0 = now_of(store) - 3600
    uid = iter(f"{n:032x}" for n in range(1, 100))
    add_session(
        store,
        "trip-root",
        started_at=t0,
        ended_at=t0 + 600,
        end_reason="compression",
        title="Trip plan",
        title_source="user",
        model="model-a",
        model_config={"temperature": 0.2},
        prompt="You plan trips.",
        message_count=5,
        tool_call_count=1,
        input_tokens=1200,
        output_tokens=340,
    )
    call = {"id": "call_1", "type": "function", "function": {"name": "search", "arguments": "{}"}}
    rows: list[tuple[str, object, dict[str, Any]]] = [
        ("user", "Plan a weekend in Lisbon", {}),
        ("assistant", None, {"tool_calls": [call], "finish_reason": "tool_calls"}),
        ("tool", "3 results", {"tool_call_id": "call_1", "tool_name": "search"}),
        (
            "user",
            [
                {"type": "text", "text": "What about this place?"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
            {},
        ),
        ("assistant", "It looks lovely.", {"finish_reason": "stop", "token_count": 12}),
    ]
    for i, (role, content, extra) in enumerate(rows):
        add_message(
            store, "trip-root", content, role=role, timestamp=t0 + i, message_uid=next(uid), **extra
        )
    add_session(
        store,
        "trip-2",
        parent="trip-root",
        started_at=t0 + 600,
        model="model-a",
        message_count=2,
    )
    add_message(
        store,
        "trip-2",
        "Summary of the earlier turns.",
        role="assistant",
        timestamp=t0 + 600,
        message_uid=next(uid),
    )
    add_message(store, "trip-2", "Book the hotel", timestamp=t0 + 601, message_uid=next(uid))
    add_session(
        store,
        "notes",
        source="cron",
        started_at=t0 + 10,
        ended_at=t0 + 20,
        end_reason="cli_close",
        title="Notes",
        pinned=1,
        message_count=1,
    )
    add_message(store, "notes", "remember milk", timestamp=t0 + 11, message_uid=next(uid))
    add_message(store, "notes", "never mind", timestamp=t0 + 12, active=0, message_uid=next(uid))
    store.write(
        lambda c: session_meta.put_in(
            c, "notes", "goals", "main", {"text": "buy milk", "done": False}, now=t0 + 15
        )
    )


@pytest.fixture
def other(scope: Scope, tmp_path: Path) -> Iterator[StoreHandle]:
    handle = registry.open_unshared(scope, tmp_path / "other" / "state.db")
    yield handle
    handle.close()


def _lines(objs: Iterator[dict[str, Any]] | list[dict[str, Any]]) -> list[str]:
    return [portability.dump_line(o) for o in objs]


def _export(store: StoreHandle, tmp_path: Path, name: str) -> list[dict[str, Any]]:
    path = tmp_path / name
    portability.export_all(store, path)
    return [json.loads(line) for line in path.read_text().splitlines()]


def _session(sid: str, **fields: object) -> dict[str, object]:
    return {"kind": "session", "id": sid, "source": "cli", "started_at": 1_767_000_000.0, **fields}


def _message(sid: str, content: object = "hi", **fields: object) -> dict[str, object]:
    return {"kind": "message", "session_id": sid, "role": "user", "content": content, **fields}


def _import(store: StoreHandle, objs: list[dict[str, object]]) -> portability.ImportReport:
    return portability.import_jsonl(store, [json.dumps(o) for o in objs])


# -- export ---------------------------------------------------------------------------------------


def test_export_shape(store: StoreHandle) -> None:
    seed_small(store)
    out = list(portability.export_session(store, "trip-root"))
    header, *rows = out
    assert header["kind"] == "session" and header["id"] == "trip-root"
    assert header["format"] == portability.FORMAT
    assert header["system_prompt"] == "You plan trips." and "system_prompt_hash" not in header
    assert header["model_config"] == {"temperature": 0.2}
    assert [r["role"] for r in rows] == ["user", "assistant", "tool", "user", "assistant"]
    assert rows[1]["tool_calls"][0]["id"] == "call_1"
    assert rows[3]["content"][1]["type"] == "image_url"
    assert all("id" not in r and r["session_id"] == "trip-root" for r in rows)


def test_lineage_export(store: StoreHandle) -> None:
    seed_small(store)
    out = list(portability.export_session(store, "trip-2", lineage=True))
    assert out[0] == {
        "kind": "lineage",
        "format": portability.FORMAT,
        "segments": ["trip-root", "trip-2"],
    }
    headers = [o["id"] for o in out if o["kind"] == "session"]
    assert headers == ["trip-root", "trip-2"]
    assert sum(1 for o in out if o["kind"] == "message") == 7


def test_inactive_rows_only_when_asked(store: StoreHandle, other: StoreHandle) -> None:
    seed_small(store)
    live = list(portability.export_session(store, "notes"))
    every = list(portability.export_session(store, "notes", include_inactive=True))
    assert len(live) == 2 and len(every) == 3 and every[2]["active"] == 0
    assert portability.import_jsonl(other, _lines(every)).ok
    assert count(other, "SELECT COUNT(*) FROM messages WHERE active = 0") == 1
    row = session_row(other, "notes")
    assert row is not None and row["message_count"] == 1


def test_unknown_session_exports_nothing(store: StoreHandle) -> None:
    assert list(portability.export_session(store, "ghost")) == []
    assert list(portability.export_session(store, "ghost", lineage=True)) == []


def test_export_guard_4131(scope: Scope, tmp_path: Path) -> None:
    limited = scope.with_config(Config({"sessions.resume_message_limit": 3}))
    store = registry.open_unshared(limited, tmp_path / "guard" / "state.db")
    try:
        add_session(store, "big")
        for _ in range(4):
            add_message(store, "big")
        add_message(store, "big", active=0)
        with pytest.raises(ResumeTooLarge) as caught:
            portability.export_session(store, "big", guard=True)
        assert (caught.value.numeric, caught.value.count) == (4131, 4)
        assert len(list(portability.export_session(store, "big"))) == 5
    finally:
        store.close()


# -- round trips ----------------------------------------------------------------------------------


def test_round_trip_through_a_fresh_store(
    store: StoreHandle, other: StoreHandle, tmp_path: Path
) -> None:
    seed_small(store)
    store.write(
        lambda c: c.execute(
            "UPDATE sessions SET last_activity_at = 1, last_activity_note = 'typing', "
            "rewind_count = 2, compression_fallback_streak = 3, auto_archived = 1 "
            "WHERE id = 'notes'"
        )
    )
    report = portability.import_jsonl(other, tmp_path / "missing.jsonl")
    assert not report.ok and "could not be read" in report.errors[0].message

    path = tmp_path / "all.jsonl"
    written = portability.export_all(store, path)
    assert (written.sessions, written.messages) == (3, 9)
    report = portability.import_jsonl(other, path)
    assert report.ok and sorted(report.imported) == ["notes", "trip-2", "trip-root"]
    assert report.skipped == [] and report.detached == []

    before = _export(store, tmp_path, "before.jsonl")
    after = _export(other, tmp_path, "after.jsonl")
    reset = {
        "last_activity_at": None,
        "last_activity_note": None,
        "rewind_count": 0,
        "compression_fallback_streak": 0,
        "auto_archived": 0,
    }
    for obj in before:
        if obj.get("id") == "notes":
            obj.update(reset)
    assert after == before
    child = session_row(other, "trip-2")
    assert child is not None and child["parent_session_id"] == "trip-root"
    assert session_meta.get(other, "notes", "goals", "main") == {"text": "buy milk", "done": False}


def test_fixture_imports_and_reexports(store: StoreHandle, tmp_path: Path) -> None:
    fixture = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
    kinds = [o["kind"] for o in fixture]
    assert kinds.count("session") == 3
    report = portability.import_jsonl(store, FIXTURE)
    assert report.ok, report.errors
    again = _export(store, tmp_path, "again.jsonl")
    assert len(again) == len(fixture)
    for ours, theirs in zip(again, fixture, strict=True):
        assert {k: ours.get(k) for k in theirs} == theirs


def test_existing_id_is_skipped(store: StoreHandle) -> None:
    seed_small(store)
    lines = _lines(portability.export_session(store, "notes"))
    report = portability.import_jsonl(store, lines)
    assert report.ok and report.imported == [] and report.skipped == ["notes"]
    assert count(store, "SELECT COUNT(*) FROM messages WHERE session_id = 'notes'") == 2


def test_tombstoned_id_is_refused(store: StoreHandle, other: StoreHandle) -> None:
    seed_small(store)
    lines = _lines(portability.export_session(store, "notes"))
    add_session(other, "notes")
    deletion.delete_session(other, "notes")
    report = portability.import_jsonl(other, [json.dumps(_session("fresh")), *lines])
    assert not report.ok and report.imported == []
    assert [e.session_id for e in report.errors] == ["notes"]
    assert "deleted" in report.errors[0].message
    assert session_row(other, "fresh") is None and session_row(other, "notes") is None


def test_cycle_is_cut_once(store: StoreHandle) -> None:
    report = _import(
        store,
        [
            _session("a", parent_session_id="b"),
            _session("b", parent_session_id="a"),
            _session("self", parent_session_id="self"),
        ],
    )
    assert report.ok and report.detached == ["b", "self"]
    a, b = session_row(store, "a"), session_row(store, "b")
    assert a is not None and a["parent_session_id"] == "b"
    assert b is not None and b["parent_session_id"] is None


def test_parents_attach_only_when_present(store: StoreHandle) -> None:
    add_session(store, "in-store")
    report = _import(
        store,
        [_session("x", parent_session_id="ghost"), _session("y", parent_session_id="in-store")],
    )
    assert report.detached == ["x"]
    x, y = session_row(store, "x"), session_row(store, "y")
    assert x is not None and x["parent_session_id"] is None
    assert y is not None and y["parent_session_id"] == "in-store"


def test_title_taken_is_renamed(store: StoreHandle) -> None:
    add_session(store, "mine", title="Plan")
    report = _import(store, [_session("theirs", title="Plan")])
    assert report.retitled == ["theirs"]
    row = session_row(store, "theirs")
    assert row is not None and row["title"] == "Plan #2"


def test_uids_and_counters(store: StoreHandle) -> None:
    uid = "ab" * 16
    calls = [{"id": "c1"}, {"id": "c2"}]
    report = _import(
        store,
        [
            _session("s", message_count=99),
            _message("s", "q", message_uid=uid, timestamp=1_767_000_001),
            _message("s", None, role="assistant", tool_calls=calls),
            _message("s", "old", active=0, tool_calls=[{"id": "c0"}]),
        ],
    )
    assert report.ok
    row = session_row(store, "s")
    assert row is not None and (row["message_count"], row["tool_call_count"]) == (2, 2)
    with store.read() as conn:
        first = conn.execute("SELECT * FROM messages ORDER BY id LIMIT 1").fetchone()
    assert first["message_uid"] == uid and first["timestamp"] == 1_767_000_001.0


def test_prefixed_literal_stays_a_string(store: StoreHandle) -> None:
    literal = messages.PARTS_PREFIX + '[{"type": "text", "text": "injected"}]'
    assert _import(store, [_session("s"), _message("s", literal)]).ok
    out = list(portability.export_session(store, "s"))
    assert out[1]["content"] == literal


def test_lone_surrogate_is_scrubbed(store: StoreHandle) -> None:
    text = '{"kind": "message", "session_id": "s", "role": "user", "content": "a\\ud800b"}'
    report = portability.import_jsonl(store, [json.dumps(_session("s")), text])
    assert report.ok
    out = list(portability.export_session(store, "s"))
    assert out[1]["content"] == "a�b"


# -- validation -----------------------------------------------------------------------------------


def test_too_many_sessions_is_one_error(store: StoreHandle) -> None:
    report = _import(store, [_session(f"s{i}") for i in range(501)])
    assert not report.ok and len(report.errors) == 1
    assert report.errors[0].line == 501 and "500" in report.errors[0].message
    assert count(store, "SELECT COUNT(*) FROM sessions") == 0


def test_nan_in_a_nested_field_names_the_line(store: StoreHandle) -> None:
    lines = [
        json.dumps(_session("ok")),
        '{"kind": "session", "id": "bad", "source": "cli", "model_config": {"a": {"b": NaN}}}',
        '{"kind": "message", "session_id": "ok", "role": "user", "content": [{"w": -Infinity}]}',
    ]
    report = portability.import_jsonl(store, lines)
    assert not report.ok
    assert [(e.line, e.session_id) for e in report.errors] == [(2, "bad"), (3, "ok")]
    assert (
        "non-finite" in report.errors[0].message
        and "$.model_config.a.b" in report.errors[0].message
    )
    assert count(store, "SELECT COUNT(*) FROM sessions") == 0


@pytest.mark.parametrize(
    "line",
    [
        '{"kind": "session", "id": "s", "source": "cli", "started_at": 1e309}',
        '{"kind": "session", "id": "s", "source": "cli", "actual_cost_usd": Infinity}',
        '{"kind": "session", "id": "s", "source": "cli", "model_config": "{\\"t\\": 1e999}"}',
    ],
)
def test_overflowing_float_is_refused(store: StoreHandle, line: str) -> None:
    report = portability.import_jsonl(store, [line])
    assert not report.ok and "non-finite" in report.errors[0].message


def test_huge_int_is_clamped(store: StoreHandle) -> None:
    report = _import(
        store,
        [
            _session("s", input_tokens=2**70, output_tokens=-(2**70)),
            _message("s", token_count=2**80),
        ],
    )
    assert report.ok
    row = session_row(store, "s")
    assert row is not None
    assert (row["input_tokens"], row["output_tokens"]) == (portability.INT_MAX, portability.INT_MIN)
    with store.read() as conn:
        assert conn.execute("SELECT token_count FROM messages").fetchone()[0] == portability.INT_MAX


def test_every_error_is_reported_with_its_line(store: StoreHandle) -> None:
    lines = [
        json.dumps(_session("a")),
        "not json at all",
        json.dumps(_message("a", role="")),
        json.dumps(_message("a", 42)),
        json.dumps(_message("nobody")),
        json.dumps(_session("a")),
        json.dumps({"kind": "mystery"}),
        "[1, 2]",
        json.dumps(_session("b", title=7)),
        json.dumps(_message("a", timestamp="yesterday")),
        "",
    ]
    report = portability.import_jsonl(store, lines)
    assert not report.ok
    assert [e.line for e in report.errors] == [2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert all(str(e).startswith(f"line {e.line}") for e in report.errors)
    assert count(store, "SELECT COUNT(*) FROM sessions") == 0


def test_per_session_message_limit(store: StoreHandle) -> None:
    lines = [json.dumps(_session("s"))] + [json.dumps(_message("s"))] * 10_001
    report = portability.import_jsonl(store, lines)
    assert not report.ok and len(report.errors) == 1
    assert "10,000" in report.errors[0].message


def test_total_limits(store: StoreHandle, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(portability, "MAX_MESSAGES_TOTAL", 3)
    report = _import(
        store, [_session("a"), _session("b"), *[_message("a")] * 2, *[_message("b")] * 2]
    )
    assert [e.session_id for e in report.errors] == ["b"] and "50,000" in report.errors[0].message
    monkeypatch.setattr(portability, "MAX_SESSION_BYTES", 200)
    report = _import(store, [_session("a"), _message("a", "x" * 300)])
    assert "5 MiB" in report.errors[0].message
    monkeypatch.setattr(portability, "MAX_TOTAL_BYTES", 500)
    report = _import(store, [_session(f"s{i}") for i in range(10)])
    assert len(report.errors) == 1 and "25 MiB" in report.errors[0].message
    assert count(store, "SELECT COUNT(*) FROM sessions") == 0


def test_late_failure_writes_nothing(store: StoreHandle, monkeypatch: pytest.MonkeyPatch) -> None:
    objs = [_session("a"), _message("a"), _session("b"), _session("c"), _message("c", 1.5)]
    assert not _import(store, objs).ok
    assert count(store, "SELECT COUNT(*) FROM sessions") == 0

    real = session_meta.import_rows_in

    def fail_on_c(conn: sqlite3.Connection, sid: str, rows: Any, *, now: float) -> int:
        if sid == "c":
            raise sqlite3.OperationalError("disk trouble")
        return real(conn, sid, rows, now=now)

    monkeypatch.setattr(session_meta, "import_rows_in", fail_on_c)
    with pytest.raises(sqlite3.OperationalError):
        _import(store, objs[:4])
    assert count(store, "SELECT COUNT(*) FROM sessions") == 0
    assert count(store, "SELECT COUNT(*) FROM messages") == 0


def test_bad_metadata_rows_are_reported(store: StoreHandle) -> None:
    report = _import(store, [_session("s", meta=[{"namespace": "", "key": "k", "value": 1}])])
    assert not report.ok and report.errors[0].session_id == "s"
    report = _import(store, [_session("s", meta="nope")])
    assert not report.ok


def test_defaults_for_missing_fields(store: StoreHandle) -> None:
    report = _import(
        store,
        [
            {"kind": "session", "id": "bare"},
            {"kind": "message", "session_id": "bare", "role": "user"},
        ],
    )
    assert report.ok
    row = session_row(store, "bare")
    assert row is not None and row["source"] == "import" and row["started_at"] == now_of(store)
