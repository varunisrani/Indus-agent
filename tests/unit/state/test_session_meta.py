"""Per-session metadata API (P02-23)."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from indus.foundation.scope import Scope
from indus.state import deletion, portability, registry, session_meta
from indus.state.registry import StoreHandle
from indus.state.session_meta import DueEntry, MetaValueTooLarge
from tests.unit.state._helpers_d import add_session, count, now_of


@pytest.fixture(autouse=True)
def _fresh_branch_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_meta, "_branch_safe", set())


def test_put_get_round_trip(store: StoreHandle) -> None:
    add_session(store, "s")
    value = {"goal": "ship it", "steps": [1, 2.5, None, True], "note": "日本語 ✓"}
    session_meta.put(store, "s", "goals", "main", value)
    assert session_meta.get(store, "s", "goals", "main") == value
    assert session_meta.get(store, "s", "goals", "other") is None
    assert session_meta.get(store, "s", "goals", "other", default=0) == 0
    session_meta.put(store, "s", "goals", "main", "replaced")
    assert session_meta.get(store, "s", "goals", "main") == "replaced"
    session_meta.put(store, "s", "goals", "aux", [])
    assert session_meta.list_entries(store, "s", "goals") == {"aux": [], "main": "replaced"}
    assert session_meta.delete(store, "s", "goals", "aux") is True
    assert session_meta.delete(store, "s", "goals", "aux") is False
    assert session_meta.list_entries(store, "s", "loops") == {}


def test_value_over_64_kib_is_refused(store: StoreHandle) -> None:
    add_session(store, "s")
    with pytest.raises(MetaValueTooLarge) as caught:
        session_meta.put(store, "s", "loops", "big", "x" * (65 * 1024))
    assert caught.value.code == "store.meta_too_large"
    assert count(store, "SELECT COUNT(*) FROM session_meta") == 0
    session_meta.put(store, "s", "loops", "fits", "x" * (63 * 1024))
    with pytest.raises(MetaValueTooLarge):
        session_meta.patch(store, "s", "loops", "grow", {"blob": "y" * (65 * 1024)})
    assert session_meta.get(store, "s", "loops", "grow") is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), {"k": object()}, {1, 2}])
def test_non_json_values_are_refused(store: StoreHandle, bad: object) -> None:
    add_session(store, "s")
    with pytest.raises(ValueError):
        session_meta.put(store, "s", "goals", "main", bad)


@pytest.mark.parametrize(("namespace", "key"), [("", "k"), ("ns", ""), ("n" * 201, "k")])
def test_names_are_checked(store: StoreHandle, namespace: str, key: str) -> None:
    add_session(store, "s")
    with pytest.raises(ValueError):
        session_meta.put(store, "s", namespace, key, 1)


def test_unknown_session_is_a_foreign_key_error(store: StoreHandle) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        session_meta.put(store, "ghost", "goals", "main", 1)


def test_patch_is_a_merge_patch(store: StoreHandle) -> None:
    add_session(store, "s")
    assert session_meta.patch(store, "s", "loops", "l", {"a": 1, "nested": {"x": 1}}) == {
        "a": 1,
        "nested": {"x": 1},
    }
    merged = session_meta.patch(store, "s", "loops", "l", {"a": None, "nested": {"y": 2}, "b": [3]})
    assert merged == {"nested": {"x": 1, "y": 2}, "b": [3]}
    assert session_meta.get(store, "s", "loops", "l") == merged
    session_meta.put(store, "s", "loops", "plain", 5)
    assert session_meta.patch(store, "s", "loops", "plain", {"now": "object"}) == {"now": "object"}


def test_patch_keeps_or_sets_due_at(store: StoreHandle) -> None:
    add_session(store, "s")
    session_meta.put(store, "s", "heartbeat", "next", {"n": 1}, due_at=100.0)
    session_meta.patch(store, "s", "heartbeat", "next", {"n": 2})
    assert session_meta.due(store, "heartbeat", 200.0) == [DueEntry("s", "next", 100.0, {"n": 2})]
    session_meta.patch(store, "s", "heartbeat", "next", {"n": 3}, due_at=None)
    assert session_meta.due(store, "heartbeat", 200.0) == []
    with pytest.raises(ValueError):
        session_meta.put(store, "s", "heartbeat", "next", 1, due_at=float("inf"))


def test_two_writers_patching_different_fields(store: StoreHandle) -> None:
    add_session(store, "s")
    start = threading.Barrier(2)
    errors: list[BaseException] = []

    def writer(field: str) -> None:
        try:
            start.wait()
            for i in range(25):
                session_meta.patch(store, "s", "goals", "shared", {field: i})
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(name,)) for name in ("left", "right")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []
    assert session_meta.get(store, "s", "goals", "shared") == {"left": 24, "right": 24}


def test_rows_go_with_the_session(store: StoreHandle) -> None:
    add_session(store, "s")
    add_session(store, "other")
    session_meta.put(store, "s", "goals", "main", 1)
    session_meta.put(store, "other", "goals", "main", 2)
    deletion.delete_session(store, "s")
    assert count(store, "SELECT COUNT(*) FROM session_meta WHERE session_id = 's'") == 0
    assert session_meta.get(store, "other", "goals", "main") == 2


def test_export_import_keeps_rows(store: StoreHandle, scope: Scope, tmp_path: Path) -> None:
    add_session(store, "s")
    session_meta.put(store, "s", "goals", "main", {"text": "learn"})
    session_meta.put(store, "s", "heartbeat", "next", {"every": 60}, due_at=1234.5)
    lines = [portability.dump_line(o) for o in portability.export_session(store, "s")]
    target = registry.open_unshared(scope, tmp_path / "copy" / "state.db")
    try:
        report = portability.import_jsonl(target, lines)
        assert report.ok and report.imported == ["s"]
        assert session_meta.list_entries(target, "s", "goals") == {"main": {"text": "learn"}}
        assert session_meta.due(target, "heartbeat", 2000.0) == [
            DueEntry("s", "next", 1234.5, {"every": 60})
        ]
    finally:
        target.close()


def test_branch_copies_only_branch_safe_namespaces(store: StoreHandle) -> None:
    add_session(store, "parent")
    add_session(store, "child")
    session_meta.register_branch_safe("goals")
    session_meta.put(store, "parent", "goals", "main", {"g": 1}, due_at=50.0)
    session_meta.put(store, "parent", "loops", "l", {"x": 1})
    now = now_of(store)
    copied = store.write(lambda c: session_meta.copy_branch_safe_in(c, "parent", "child", now=now))
    assert copied == 1
    assert session_meta.list_entries(store, "child", "goals") == {"main": {"g": 1}}
    assert session_meta.list_entries(store, "child", "loops") == {}
    assert session_meta.branch_safe_namespaces() == frozenset({"goals"})


def test_branch_copy_with_nothing_registered(store: StoreHandle) -> None:
    add_session(store, "parent")
    add_session(store, "child")
    session_meta.put(store, "parent", "goals", "main", 1)
    now = now_of(store)
    assert (
        store.write(lambda c: session_meta.copy_branch_safe_in(c, "parent", "child", now=now)) == 0
    )


def test_due_returns_rows_in_time_order(store: StoreHandle) -> None:
    for sid in ("a", "b"):
        add_session(store, sid)
    session_meta.put(store, "a", "heartbeat", "k1", 1, due_at=30.0)
    session_meta.put(store, "b", "heartbeat", "k2", 2, due_at=10.0)
    session_meta.put(store, "a", "heartbeat", "k3", 3, due_at=20.0)
    session_meta.put(store, "b", "heartbeat", "never", 4)
    session_meta.put(store, "b", "loops", "elsewhere", 5, due_at=1.0)
    found = session_meta.due(store, "heartbeat", 25.0)
    assert [(e.session_id, e.key, e.due_at) for e in found] == [
        ("b", "k2", 10.0),
        ("a", "k3", 20.0),
    ]
    assert [e.key for e in session_meta.due(store, "heartbeat", 100.0, limit=1)] == ["k2"]


def test_import_rows_are_all_checked_first(store: StoreHandle) -> None:
    add_session(store, "s")
    rows = [{"namespace": "goals", "key": "ok", "value": 1}, {"namespace": "", "key": "k"}]
    now = now_of(store)
    with pytest.raises(ValueError):
        store.write(lambda c: session_meta.import_rows_in(c, "s", rows, now=now))
    assert count(store, "SELECT COUNT(*) FROM session_meta") == 0
