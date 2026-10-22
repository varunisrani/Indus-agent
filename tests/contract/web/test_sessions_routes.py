"""P18-11: the Sessions REST routes against a real store (R03.16, R03.22, R09.21)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.errors import SessionTombstoned
from indus.foundation.scope import Scope
from indus.service.maintenance_gate import MaintenanceGate, OwnershipLock
from indus.state import browse, connection, messages, registry, sessions
from indus.web.routes import sessions as sessions_routes
from tests.contract.web.rest_app import RestApp, build_rest_app, route_table, unscoped_writes
from tests.fixtures.store.build_dashboard import (
    AUTOMATION_SOURCES,
    CHAT_SOURCES,
    DashboardStore,
    build_dashboard_store,
)


@pytest.fixture
def store(scope: Scope) -> DashboardStore:
    return build_dashboard_store(scope)


@pytest.fixture
def rest(scope: Scope) -> Iterator[RestApp]:
    built = build_rest_app(scope)
    yield built
    built.close()


@pytest.fixture
def write(scope: Scope) -> dict[str, str]:
    """Writes name their profile (the isolated root holds more than one)."""
    return {"profile": scope.profile_id}


def _signature(path: Path) -> list[tuple[str, int, int]]:
    """Every file of the store family: name, mtime and size."""
    out: list[tuple[str, int, int]] = []
    for item in sorted(path.parent.iterdir()):
        if item.name.startswith(path.name):
            st = item.stat()
            out.append((item.name, st.st_mtime_ns, st.st_size))
    return out


def _snapshot(rest: RestApp, sid: str) -> dict[str, Any]:
    answer = rest.client.get(f"/api/sessions/{sid}/mutation-snapshot")
    assert answer.status_code == 200
    return answer.json()


def _identity(snap: Mapping[str, Any], request_id: str = "req-1") -> dict[str, Any]:
    out: dict[str, Any] = {"request_id": request_id, "expected_revision": snap["revision"]}
    if snap["generation"] is not None:
        out["expected_generation"] = snap["generation"]
    return out


def _code(answer: Any) -> str:
    return answer.json()["detail"]["code"]


class _Daemon:
    """A gate whose control socket answers (a daemon owns the profile)."""

    def __init__(self) -> None:
        self.asked: list[Mapping[str, Any]] = []

    def gate(self, scope: Scope) -> MaintenanceGate:
        def ask(home: Path, verb: str, params: Mapping[str, Any], *, timeout: float) -> Any:
            self.asked.append(params)
            raise AssertionError("bulk maintenance must not be sent to the daemon")

        return MaintenanceGate(scope, identify=lambda home: {"pid": 4242}, ask=ask)


# -- reads ----------------------------------------------------------------------------------------


def test_list_tabs_sources_order_and_paging(rest: RestApp, store: DashboardStore) -> None:
    every = rest.client.get("/api/sessions", params={"limit": 100}).json()
    ids = {row["id"] for row in every["sessions"]}
    assert every["total"] == len(every["sessions"])
    assert store.archived not in ids  # archived rows stay out by default
    assert set(store.chats) <= ids
    assert {store.by_source[s] for s in ("cron", "api_server", "acp")} <= ids

    chats = rest.client.get("/api/sessions", params={"tab": "chats", "limit": 100}).json()
    chat_sources = {row["source"] for row in chats["sessions"]}
    assert set(CHAT_SOURCES) <= chat_sources
    assert not chat_sources & set(AUTOMATION_SOURCES)

    auto = rest.client.get("/api/sessions", params={"tab": "automation", "limit": 100}).json()
    assert {row["source"] for row in auto["sessions"]} == set(AUTOMATION_SOURCES)
    assert auto["total"] == len(AUTOMATION_SOURCES)

    exact = rest.client.get("/api/sessions", params={"source": "cron"}).json()
    assert [row["id"] for row in exact["sessions"]] == [store.by_source["cron"]]
    picked = rest.client.get("/api/sessions", params={"sources": "cron,acp"}).json()
    assert {row["source"] for row in picked["sessions"]} == {"cron", "acp"}
    without = rest.client.get(
        "/api/sessions", params={"exclude_sources": "cli", "limit": 100}
    ).json()
    assert "cli" not in {row["source"] for row in without["sessions"]}

    page = rest.client.get("/api/sessions", params={"limit": 2, "offset": 1}).json()
    assert page["limit"] == 2 and page["offset"] == 1 and len(page["sessions"]) == 2
    assert page["total"] == every["total"]
    created = [r["id"] for r in every["sessions"]]
    assert [r["id"] for r in page["sessions"]] == created[1:3]

    only_archived = rest.client.get("/api/sessions", params={"archived": "only"}).json()
    assert [r["id"] for r in only_archived["sessions"]] == [store.archived]


def test_list_row_shape_and_recent_order(rest: RestApp, store: DashboardStore) -> None:
    rows = rest.client.get("/api/sessions", params={"order": "recent", "limit": 100}).json()
    first = rows["sessions"][0]
    for key in (
        "id",
        "title",
        "preview",
        "source",
        "model",
        "started_at",
        "last_active",
        "ended_at",
        "is_active",
        "input_tokens",
        "output_tokens",
        "tool_call_count",
        "message_count",
    ):
        assert key in first
    stamps = [r["last_active"] for r in rows["sessions"]]
    assert stamps == sorted(stamps, reverse=True)
    cli = next(r for r in rows["sessions"] if r["id"] == store.by_source["cli"])
    assert cli["input_tokens"] == 100 and cli["output_tokens"] == 10
    assert cli["message_count"] == 2 and cli["is_active"] is False
    assert cli["preview"].startswith("Please summarise the cli report")


def test_list_limit_bounds(rest: RestApp, store: DashboardStore) -> None:
    assert rest.client.get("/api/sessions", params={"limit": 101}).status_code == 422
    assert rest.client.get("/api/sessions", params={"order": "sideways"}).status_code == 422


def test_search_marks_matches(rest: RestApp, store: DashboardStore) -> None:
    answer = rest.client.get("/api/sessions/search", params={"q": "herons"})
    assert answer.status_code == 200
    results = answer.json()["results"]
    assert {r["session_id"] for r in results} >= set(store.chats)
    assert len({r["session_id"] for r in results}) == len(results)  # one hit per session
    hit = next(r for r in results if r["session_id"] == store.by_source["cli"])
    assert "<mark>herons</mark>" in hit["snippet"]
    assert "[[" not in hit["snippet"]
    assert hit["title"] == "Cli session" and hit["source"] == "cli"
    assert rest.client.get("/api/sessions/search", params={"q": "  "}).json() == {"results": []}


def test_search_falls_back_to_titles(rest: RestApp, store: DashboardStore) -> None:
    results = rest.client.get("/api/sessions/search", params={"q": "Acp session"}).json()
    hit = next(r for r in results["results"] if r["session_id"] == store.by_source["acp"])
    assert "<mark>" in hit["snippet"]


def test_stats(rest: RestApp, store: DashboardStore) -> None:
    stats = rest.client.get("/api/sessions/stats").json()
    assert stats["archived"] == 1
    assert stats["by_source"]["cron"] == 1 and stats["by_source"]["cli"] >= 6
    assert stats["total"] == sum(stats["by_source"].values())
    assert stats["messages"] >= 2 * (len(CHAT_SOURCES) + len(AUTOMATION_SOURCES))
    assert stats["active"] == 1  # only the session running a turn; the rest went quiet
    rows = rest.client.get("/api/sessions", params={"limit": 100}).json()["sessions"]
    assert [r["id"] for r in rows if r["is_active"]] == [store.busy]


def test_one_session_messages_and_export(rest: RestApp, store: DashboardStore) -> None:
    sid = store.by_source["cli"]
    one = rest.client.get(f"/api/sessions/{sid}").json()
    assert one["id"] == sid and one["title"] == "Cli session" and one["profile"] == "work"
    assert rest.client.get("/api/sessions/nope").status_code == 404

    page = rest.client.get(f"/api/sessions/{sid}/messages").json()
    assert [m["role"] for m in page["messages"]] == ["user", "assistant"]
    assert page["pagination"]["order"] == "latest" and page["pagination"]["limit"] == 500
    latest = rest.client.get(
        f"/api/sessions/{sid}/messages", params={"limit": 1, "order": "latest"}
    ).json()
    assert [m["role"] for m in latest["messages"]] == ["assistant"]
    oldest = rest.client.get(f"/api/sessions/{sid}/messages", params={"limit": 1}).json()
    assert [m["role"] for m in oldest["messages"]] == ["user"]
    assert oldest["pagination"]["order"] == "oldest"
    second = rest.client.get(
        f"/api/sessions/{sid}/messages", params={"limit": 1, "offset": 1, "order": "oldest"}
    ).json()
    assert [m["role"] for m in second["messages"]] == ["assistant"]
    assert second["pagination"]["total"] == 2
    too_many = rest.client.get(f"/api/sessions/{sid}/messages", params={"limit": 501})
    assert too_many.status_code == 422
    assert rest.client.get("/api/sessions/nope/messages").status_code == 404

    exported = rest.client.get(f"/api/sessions/{sid}/export").json()
    kinds = [o["kind"] for o in exported["objects"]]
    assert exported["session_id"] == sid and "session" in kinds and kinds.count("message") == 2
    assert rest.client.get("/api/sessions/nope/export").status_code == 404


def test_a_compressed_conversation_reads_as_one(
    rest: RestApp, store: DashboardStore, scope: Scope
) -> None:
    with registry.open_unshared(scope) as handle:
        sessions.create(handle, "s-root", "cli", started_at=store.now - 500)
        messages.append(handle, "s-root", {"role": "user", "content": "first half"})
        sessions.end(handle, "s-root", sessions.END_COMPRESSION)
        sessions.create(
            handle, "s-tip", "cli", started_at=store.now - 400, parent_session_id="s-root"
        )
        messages.append(handle, "s-tip", {"role": "user", "content": "second half"})
    rows = rest.client.get("/api/sessions", params={"limit": 100}).json()["sessions"]
    ids = [r["id"] for r in rows]
    assert "s-tip" in ids and "s-root" not in ids  # one row, carrying the tip's id
    tip = next(r for r in rows if r["id"] == "s-tip")
    assert tip["lineage_root"] == "s-root"  # the durable id the desktop pins and PATCHes
    plain = next(r for r in rows if r["id"] not in {"s-tip"})
    assert plain["lineage_root"] is None
    snap = _snapshot(rest, "s-root")
    pinned = rest.client.patch(
        "/api/sessions/s-root",
        params={"profile": scope.profile_id},
        json={"pinned": True, **_identity(snap)},
    )
    assert pinned.status_code == 200
    rows = rest.client.get("/api/sessions", params={"limit": 100}).json()["sessions"]
    assert next(r for r in rows if r["id"] == "s-tip")["pinned"] is True
    page = rest.client.get("/api/sessions/s-tip/messages").json()
    assert [m["content"] for m in page["messages"]] == ["first half", "second half"]


def test_tool_call_arguments_are_masked_for_display(
    rest: RestApp, store: DashboardStore, scope: Scope
) -> None:
    """W47-a: the desktop rebuilds tool rows (summary and Arguments) from this route after a
    reload; the arguments are masked as the live ``tool.started`` masks them."""
    secret = "sk-" + "live" + "B" * 30
    calls = [
        {
            "id": "c1",
            "type": "function",
            "function": {"name": "terminal", "arguments": f'{{"command": "echo {secret}"}}'},
        },
        {"id": "c2", "type": "function", "function": {"name": "odd", "arguments": f"x {secret}"}},
    ]
    with registry.open_unshared(scope) as handle:
        sessions.create(handle, "s-tools", "cli", started_at=store.now - 300)
        messages.append(
            handle, "s-tools", {"role": "assistant", "content": "", "tool_calls": calls}
        )
    page = rest.client.get("/api/sessions/s-tools/messages").json()
    shown = page["messages"][0]["tool_calls"]
    assert [call["name"] for call in shown] == ["terminal", "odd"]
    assert all(secret not in call["arguments"] for call in shown)
    assert json.loads(shown[0]["arguments"])["command"].startswith("echo ")


def test_reads_never_write_the_store(rest: RestApp, store: DashboardStore) -> None:
    before = _signature(store.path)
    sid = store.by_source["cli"]
    for path in (
        "/api/sessions",
        "/api/sessions?order=recent",
        "/api/sessions/search?q=herons",
        "/api/sessions/stats",
        f"/api/sessions/{sid}",
        f"/api/sessions/{sid}/messages",
        f"/api/sessions/{sid}/export",
        f"/api/sessions/{sid}/mutation-snapshot",
    ):
        assert rest.client.get(path).status_code == 200, path
    answer = rest.client.post(
        "/api/sessions/prune",
        params={"profile": "work"},
        json={"older_than_days": 30, "dry_run": True},
    )
    assert answer.status_code == 200
    assert _signature(store.path) == before


def test_a_missing_store_is_empty_and_never_created(rest: RestApp, scope: Scope) -> None:
    before = sorted(p.name for p in scope.home.iterdir())
    assert rest.client.get("/api/sessions").json()["sessions"] == []
    assert rest.client.get("/api/sessions/stats").json()["total"] == 0
    assert rest.client.get("/api/sessions/search", params={"q": "x"}).json() == {"results": []}
    assert rest.client.get("/api/sessions/s1").status_code == 404
    snap = _snapshot(rest, "s1")
    assert snap == {"session_id": "s1", "exists": False, "revision": 0, "generation": None}
    assert not connection.default_store_path(scope).exists()
    assert sorted(p.name for p in scope.home.iterdir()) == before


def test_an_uninitialised_store_is_503(rest: RestApp, scope: Scope) -> None:
    path = connection.default_store_path(scope)
    sqlite3.connect(path).close()  # a file that was never set up
    answer = rest.client.get("/api/sessions")
    assert answer.status_code == 503
    assert _code(answer) == "store_unavailable"


def test_a_busy_store_is_503(
    rest: RestApp, store: DashboardStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def locked(path: Path) -> sqlite3.Connection:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(browse, "_connect", locked)
    answer = rest.client.get("/api/sessions")
    assert answer.status_code == 503 and _code(answer) == "store_busy"
    assert rest.client.get("/api/sessions/stats").status_code == 503


# -- one-session writes ---------------------------------------------------------------------------


def test_rename_and_archive_under_the_identity(
    rest: RestApp, store: DashboardStore, write: dict[str, str]
) -> None:
    sid = store.by_source["web"]
    url = f"/api/sessions/{sid}"
    bare = rest.client.patch(url, params=write, json={"title": "Renamed"})
    assert bare.status_code == 409 and _code(bare) == "client_upgrade_required"
    partial = rest.client.patch(url, params=write, json={"title": "x", "request_id": "r"})
    assert partial.status_code == 409 and _code(partial) == "mutation_identity_required"

    snap = _snapshot(rest, sid)
    assert snap["exists"] is True and snap["revision"] > 0 and snap["generation"] == 0
    done = rest.client.patch(url, params=write, json={"title": "Renamed", **_identity(snap)})
    assert done.status_code == 200 and done.json()["title"] == "Renamed"

    stale = rest.client.patch(url, params=write, json={"archived": True, **_identity(snap)})
    assert stale.status_code == 409 and _code(stale) == "stale_revision"
    fresh = _snapshot(rest, sid)
    assert fresh["revision"] != snap["revision"]
    archived = rest.client.patch(url, params=write, json={"archived": True, **_identity(fresh)})
    assert archived.status_code == 200 and archived.json()["archived"] is True

    taken = _snapshot(rest, store.by_source["tui"])
    clash = rest.client.patch(
        f"/api/sessions/{store.by_source['tui']}",
        params=write,
        json={"title": "Renamed", **_identity(taken)},
    )
    assert clash.status_code == 409 and _code(clash) == "title_taken"


def test_delete_fence_codes(rest: RestApp, store: DashboardStore, write: dict[str, str]) -> None:
    sid = store.by_source["tui"]
    url = f"/api/sessions/{sid}"
    none = rest.client.delete(url, params=write)
    assert none.status_code == 409 and _code(none) == "client_upgrade_required"
    snap = _snapshot(rest, sid)
    partial = rest.client.delete(url, params={**write, "request_id": "r1"})
    assert partial.status_code == 409 and _code(partial) == "mutation_identity_required"
    no_generation = rest.client.delete(
        url, params={**write, "request_id": "r1", "expected_revision": snap["revision"]}
    )
    assert _code(no_generation) == "mutation_identity_required"
    wrong = rest.client.delete(url, params={**write, **_identity({**snap, "revision": 7})})
    assert wrong.status_code == 409 and _code(wrong) == "stale_revision"

    busy = _snapshot(rest, store.busy)
    refused = rest.client.delete(f"/api/sessions/{store.busy}", params={**write, **_identity(busy)})
    assert refused.status_code == 409 and _code(refused) == "session_busy"
    lost = _snapshot(rest, store.unknown)
    unknown = rest.client.delete(
        f"/api/sessions/{store.unknown}", params={**write, **_identity(lost)}
    )
    assert unknown.status_code == 409 and _code(unknown) == "unknown_execution"
    assert rest.client.get(f"/api/sessions/{store.busy}").status_code == 200


def test_delete_then_retry_is_idempotent(
    rest: RestApp, store: DashboardStore, write: dict[str, str], scope: Scope
) -> None:
    sid = store.by_source["telegram"]
    identity = _identity(_snapshot(rest, sid))
    first = rest.client.delete(f"/api/sessions/{sid}", params={**write, **identity})
    assert first.status_code == 200
    assert first.json() == {"ok": True, "session_id": sid, "deleted": True, "already_absent": False}
    assert rest.client.get(f"/api/sessions/{sid}").status_code == 404

    again = rest.client.delete(f"/api/sessions/{sid}", params={**write, **identity})
    assert again.status_code == 200 and again.json()["already_absent"] is True
    gone = _snapshot(rest, sid)
    assert gone == {"session_id": sid, "exists": False, "revision": 0, "generation": None}
    retry = rest.client.delete(f"/api/sessions/{sid}", params={**write, **_identity(gone)})
    assert retry.status_code == 200
    with registry.open_unshared(scope) as handle, pytest.raises(SessionTombstoned):
        sessions.create(handle, sid, "telegram")  # the tombstone keeps the id dead (R03.22)


def test_delete_runs_inside_a_live_daemon(scope: Scope, store: DashboardStore) -> None:
    asked: list[tuple[str, Mapping[str, Any]]] = []

    def ask(home: Path, verb: str, params: Mapping[str, Any], *, timeout: float) -> Any:
        from indus.service.gateway_control import ControlReply

        asked.append((verb, params))
        return ControlReply(ok=True, result={"removed": True})

    def gate(s: Scope) -> MaintenanceGate:
        return MaintenanceGate(s, identify=lambda home: {"pid": 7}, ask=ask)

    rest = build_rest_app(scope, extras={sessions_routes.GATE_KEY: gate})
    try:
        sid = store.by_source["cli"]
        identity = _identity(_snapshot(rest, sid))
        answer = rest.client.delete(f"/api/sessions/{sid}", params={"profile": "work", **identity})
        assert answer.status_code == 200 and answer.json()["deleted"] is True
        assert asked == [("maintenance-run", {"op": "delete", "session_id": sid})]
    finally:
        rest.close()


# -- bulk maintenance -----------------------------------------------------------------------------


def test_prune_dry_run_then_real(
    rest: RestApp, store: DashboardStore, write: dict[str, str]
) -> None:
    dry = rest.client.post(
        "/api/sessions/prune", params=write, json={"older_than_days": 30, "dry_run": True}
    )
    assert dry.status_code == 200
    assert dry.json() == {"dry_run": True, "count": 1, "skipped_open": 1}
    assert rest.client.get(f"/api/sessions/{store.old}").status_code == 200
    bad = rest.client.post("/api/sessions/prune", params=write, json={"older_than_days": 0})
    assert bad.status_code == 422

    real = rest.client.post(
        "/api/sessions/prune", params=write, json={"older_than_days": 30, "dry_run": False}
    )
    assert real.status_code == 200
    assert real.json() == {"dry_run": False, "count": 1, "skipped_open": 1}
    assert rest.client.get(f"/api/sessions/{store.old}").status_code == 404


def test_bulk_maintenance_refuses_while_the_daemon_holds_the_lock(
    rest: RestApp, store: DashboardStore, write: dict[str, str], scope: Scope
) -> None:
    lock = OwnershipLock(scope.home)
    assert lock.try_acquire()
    try:
        prune = rest.client.post(
            "/api/sessions/prune", params=write, json={"older_than_days": 30, "dry_run": False}
        )
        assert prune.status_code == 409 and _code(prune) == "daemon_running"
        dry = rest.client.post(
            "/api/sessions/prune", params=write, json={"older_than_days": 30, "dry_run": True}
        )
        assert dry.status_code == 200
        bulk = rest.client.post(
            "/api/sessions/bulk-delete", params=write, json={"ids": [store.by_source["cli"]]}
        )
        assert bulk.status_code == 409 and _code(bulk) == "daemon_running"
        empty = rest.client.delete("/api/sessions/empty", params=write)
        assert empty.status_code == 409 and _code(empty) == "daemon_running"
    finally:
        lock.release()
    assert rest.client.get(f"/api/sessions/{store.old}").status_code == 200
    assert rest.client.get(f"/api/sessions/{store.empty}").status_code == 200


def test_bulk_maintenance_refuses_while_a_daemon_answers(
    scope: Scope, store: DashboardStore
) -> None:
    daemon = _Daemon()
    rest = build_rest_app(scope, extras={sessions_routes.GATE_KEY: daemon.gate})
    try:
        write = {"profile": "work"}
        prune = rest.client.post(
            "/api/sessions/prune", params=write, json={"older_than_days": 30, "dry_run": False}
        )
        assert prune.status_code == 409 and _code(prune) == "daemon_running"
        bulk = rest.client.post("/api/sessions/bulk-delete", params=write, json={"ids": ["x"]})
        assert _code(bulk) == "daemon_running"
        assert _code(rest.client.delete("/api/sessions/empty", params=write)) == "daemon_running"
        assert daemon.asked == []
    finally:
        rest.close()


def test_bulk_and_empty_delete_when_stopped(
    rest: RestApp, store: DashboardStore, write: dict[str, str]
) -> None:
    wanted = [store.by_source["cron"], store.busy, "missing"]
    bulk = rest.client.post("/api/sessions/bulk-delete", params=write, json={"ids": wanted})
    assert bulk.status_code == 200
    assert bulk.json() == {"ok": True, "deleted": 1, "skipped_active": [store.busy]}
    assert rest.client.get(f"/api/sessions/{store.by_source['cron']}").status_code == 404
    too_many = rest.client.post(
        "/api/sessions/bulk-delete", params=write, json={"ids": [f"s{i}" for i in range(501)]}
    )
    assert too_many.status_code == 422

    empty = rest.client.delete("/api/sessions/empty", params=write)
    assert empty.status_code == 200 and empty.json() == {"ok": True, "deleted": 1}
    assert rest.client.get(f"/api/sessions/{store.empty}").status_code == 404


# -- wiring ---------------------------------------------------------------------------------------


def test_writes_on_a_multi_profile_host_need_a_profile(
    rest: RestApp, store: DashboardStore
) -> None:
    answer = rest.client.post("/api/sessions/prune", json={"older_than_days": 30})
    assert answer.status_code == 400


def test_every_write_route_is_scoped(rest: RestApp) -> None:
    assert unscoped_writes(rest.app, ("/api/sessions",)) == []
    writes = {m for m, _, _ in route_table(rest.app, ("/api/sessions",))} - {"GET"}
    assert writes == {"POST", "PATCH", "DELETE"}


def test_route_list_matches_the_app(rest: RestApp) -> None:
    table = route_table(rest.app, ("/api/sessions",))
    assert table == set(sessions_routes.ROUTE_LIST)


def test_routes_need_the_session_token(rest: RestApp, store: DashboardStore) -> None:
    del rest.client.headers["X-Indus-Session-Token"]
    assert rest.client.get("/api/sessions").status_code == 401
