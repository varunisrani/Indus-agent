"""P17-21: the maintenance gate — bulk store changes go through the daemon that owns the profile.

A "daemon" here is a real control socket (``gateway.control_socket.ControlServer``) on its own
loop thread in the profile home, holding the profile ownership lock like the real daemon, with
the ``maintenance-run`` verb registered over its own store handle. The CLI verbs run through the
app's dispatch, as a person would type them.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from indus.cli.commands import sessions, sessions_maint  # noqa: F401  (sessions loads the verbs)
from indus.foundation.scope import Scope, make_scope
from indus.gateway.control_socket import ControlServer
from indus.service import maintenance_gate as gate_mod
from indus.service.maintenance_gate import (
    MAINTENANCE_VERB,
    MAX_IDS,
    MaintenanceGate,
    MaintenanceRefused,
    MaintenanceVerb,
    OwnershipLock,
)
from indus.state import connection, deletion, fts, maintenance, portability, registry
from indus.state.registry import StoreHandle
from indus.state.schema import meta_set
from tests.unit.cli.commands._helpers_d import (  # noqa: F401
    aged,
    fake_clock,
    run,
    seeded,
    store_globals,
)


def _ids(scope: Scope) -> set[str]:
    with seeded(scope) as store, store.read() as conn:
        return {str(r[0]) for r in conn.execute("SELECT id FROM sessions")}


def _old_and_new(scope: Scope) -> None:
    with seeded(scope) as store:
        aged(store, "old", 100, title="old talk")
        aged(store, "recent", 10, title="recent talk")


class Spy:
    """The daemon's ``maintenance-run`` with every request recorded."""

    def __init__(self, verb: MaintenanceVerb) -> None:
        self.verb = verb
        self.calls: list[dict[str, Any]] = []
        self.threads: list[str] = []

    async def handle(self, params: dict[str, Any]) -> Any:
        self.calls.append(dict(params))
        self.threads.append(threading.current_thread().name)
        return await self.verb.handle(params)


@contextmanager
def daemon(
    scope: Scope, *, serve_maintenance: bool = True, hold_lock: bool = True
) -> Iterator[Spy]:
    """A control socket answering for ``scope.home`` on its own loop thread (see the notes)."""
    lock = OwnershipLock(scope.home)
    if hold_lock:
        assert lock.try_acquire()
    store = registry.open_unshared(scope)
    spy = Spy(MaintenanceVerb(scope, store=lambda: store, release=lambda _h: None))
    ready = threading.Event()
    stopping: list[asyncio.AbstractEventLoop] = []
    stop_event: list[asyncio.Event] = []
    failure: list[BaseException] = []

    async def serve() -> None:
        server = ControlServer(scope.home, status=lambda: {"state": "running"}, env={})
        if serve_maintenance:
            server.register(MAINTENANCE_VERB, spy.handle, timeout=60.0)
        await server.start()
        stopping.append(asyncio.get_running_loop())
        stop_event.append(asyncio.Event())
        ready.set()
        try:
            await stop_event[0].wait()
        finally:
            await server.stop()

    def main() -> None:
        try:
            asyncio.run(serve())
        except BaseException as exc:  # reported to the test
            failure.append(exc)
            ready.set()

    thread = threading.Thread(target=main, name="fake-daemon", daemon=True)
    thread.start()
    assert ready.wait(10.0)
    assert not failure, failure
    try:
        yield spy
    finally:
        stopping[0].call_soon_threadsafe(stop_event[0].set)
        thread.join(10.0)
        store.close()
        lock.release()


@pytest.fixture
def no_local_writes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Fail the test if the CLI opens a writable handle of its own."""
    opened: list[str] = []

    @contextmanager
    def refuse(_scope: Scope) -> Iterator[Any]:
        opened.append("private handle")
        raise AssertionError("the CLI opened a writable store handle while a daemon owns it")
        yield  # pragma: no cover

    monkeypatch.setattr(sessions_maint, "_private_handle", refuse)
    return opened


# -- with a live daemon ---------------------------------------------------------------------------


def test_prune_with_a_live_daemon_runs_inside_the_daemon(
    scope: Scope, no_local_writes: list[str]
) -> None:
    _old_and_new(scope)
    with daemon(scope) as spy:
        result = run(scope, "sessions", "prune", "-y")
    assert result.code == 0, result.err
    assert "Deleted 1 session(s)" in result.out
    assert [call["op"] for call in spy.calls] == ["prune"]
    assert spy.calls[0]["ids"] == ["old"]
    assert _ids(scope) == {"recent"}
    assert no_local_writes == []


def test_a_dry_run_never_asks_the_daemon(scope: Scope) -> None:
    _old_and_new(scope)
    with daemon(scope) as spy:
        result = run(scope, "sessions", "prune", "--dry-run")
    assert result.code == 0 and "Would delete 1 session(s)" in result.out
    assert spy.calls == []
    assert _ids(scope) == {"old", "recent"}


def test_a_daemon_without_the_verb_refuses_and_nothing_is_deleted(
    scope: Scope, no_local_writes: list[str]
) -> None:
    _old_and_new(scope)
    with daemon(scope, serve_maintenance=False):
        result = run(scope, "sessions", "prune", "-y")
    assert result.code == 1
    assert "owns this profile" in result.err and "indus gateway stop" in result.err
    assert _ids(scope) == {"old", "recent"}
    assert no_local_writes == []


def test_delete_with_a_live_daemon_runs_inside_the_daemon(scope: Scope) -> None:
    _old_and_new(scope)
    with daemon(scope) as spy:
        result = run(scope, "sessions", "delete", "old", "-y")
    assert result.code == 0, result.err
    assert "Deleted old" in result.out
    assert spy.calls == [{"op": "delete", "session_id": "old"}]
    assert _ids(scope) == {"recent"}


def test_a_guarded_session_is_reported_by_the_daemon(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    _old_and_new(scope)

    def guarded(*_a: Any, **_k: Any) -> bool:
        raise deletion.SessionGuarded("store.session_guarded", "the session is in use")

    monkeypatch.setattr(deletion, "delete_session", guarded)
    with daemon(scope):
        result = run(scope, "sessions", "delete", "old", "-y")
    assert result.code == 1
    assert "in use" in result.err
    assert _ids(scope) == {"old", "recent"}


def test_repair_and_import_run_inside_the_daemon(scope: Scope, tmp_path: Path) -> None:
    with seeded(scope) as store:
        aged(store, "s1", 1, content="searchable words")
        path = store.path
    donor = make_scope(tmp_path / "donor", clock=scope.clock)
    with registry.open_unshared(donor) as store:
        aged(store, "carried", 2, title="carried over", messages=2)
        lines = list(portability.export_session(store, "carried"))
    payload = tmp_path / "in.jsonl"
    payload.write_text("".join(portability.dump_line(o) + "\n" for o in lines))
    with daemon(scope) as spy:
        # Marked stale after the daemon opened the store (an open rebuilds a stale index).
        with sqlite3.connect(path) as raw:
            meta_set(raw, fts.STALE_KEY, "test")
        repaired = run(scope, "sessions", "repair")
        imported = run(scope, "sessions", "import", str(payload))
    assert repaired.code == 0, repaired.err
    assert "Backup:" in repaired.out and "healthy" in repaired.out
    assert imported.code == 0, imported.err
    assert "Imported 1 session(s)" in imported.out
    assert [call["op"] for call in spy.calls] == ["repair", "import"]
    assert spy.calls[1]["path"] == str(payload.resolve())
    assert "carried" in _ids(scope)


def test_restoring_a_backup_is_refused_while_a_daemon_owns_the_profile(
    scope: Scope, tmp_path: Path
) -> None:
    import zipfile

    archive = tmp_path / "b.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("config.yaml", "model: theirs\n")
    with daemon(scope):
        result = run(scope, "import", str(archive), "-f")
    assert result.code == 1
    assert "indus gateway stop" in result.err
    assert not (scope.home / "config.yaml").exists()


def test_big_prunes_go_to_the_daemon_in_batches(scope: Scope) -> None:
    asked: list[dict[str, Any]] = []

    def ask(_home: Path, verb: str, params: dict[str, Any], *, timeout: float) -> Any:
        from indus.service.gateway_control import ControlReply

        asked.append(params)
        return ControlReply(True, result={"deleted": len(params["ids"]), "guarded": 0})

    gate = MaintenanceGate(scope, identify=lambda _h: {"pid": 4242}, ask=ask)
    ids = [f"s{i}" for i in range(MAX_IDS * 2 + 5)]
    done = gate.run("prune", {"ids": ids}, lambda: pytest.fail("ran locally"), what="pruning")
    assert done.where == "daemon"
    assert [len(p["ids"]) for p in asked] == [MAX_IDS, MAX_IDS, 5]
    assert done.value["deleted"] == len(ids)


def test_a_refusal_in_a_later_batch_says_earlier_ones_were_done(scope: Scope) -> None:
    from indus.service.gateway_control import ControlReply

    replies = iter(
        [ControlReply(True, result={"deleted": 1}), ControlReply(False, error="timeout")]
    )
    gate = MaintenanceGate(
        scope, identify=lambda _h: {"pid": 7}, ask=lambda *_a, **_k: next(replies)
    )
    with pytest.raises(MaintenanceRefused) as caught:
        gate.run("prune", {"ids": ["a"] * (MAX_IDS + 1)}, lambda: None, what="pruning")
    assert caught.value.reason == "daemon_refused"
    assert "earlier batches were done" in caught.value.message and "pid 7" in caught.value.message


# -- without a daemon -----------------------------------------------------------------------------


def test_without_a_daemon_prune_runs_here_holding_the_ownership_lock(
    scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    _old_and_new(scope)
    seen: list[bool] = []
    real = maintenance.prune

    def watched(*args: Any, **kwargs: Any) -> Any:
        if not kwargs.get("dry_run"):  # the preview reads without the lock
            probe = OwnershipLock(scope.home)
            seen.append(probe.try_acquire())  # False: this process holds the profile lock
            probe.release()
        return real(*args, **kwargs)

    monkeypatch.setattr(maintenance, "prune", watched)
    result = run(scope, "sessions", "prune", "-y")
    assert result.code == 0, result.err
    assert seen == [False]
    assert _ids(scope) == {"recent"}
    probe = OwnershipLock(scope.home)
    assert probe.try_acquire()  # released afterwards
    probe.release()


def test_a_held_ownership_lock_refuses_and_nothing_is_deleted(scope: Scope) -> None:
    _old_and_new(scope)
    lock = OwnershipLock(scope.home)
    assert lock.try_acquire()  # a daemon that is starting (its socket is not up yet)
    try:
        result = run(scope, "sessions", "prune", "-y")
        deleted = run(scope, "sessions", "delete", "old", "-y")
    finally:
        lock.release()
    assert result.code == 1 and "Try again in a moment" in result.err
    assert deleted.code == 1
    assert _ids(scope) == {"old", "recent"}


def test_an_unknown_op_is_a_programming_error(scope: Scope) -> None:
    with pytest.raises(ValueError):
        MaintenanceGate(scope).run("vacuum", {}, lambda: None, what="x")


# -- the daemon half on its own -------------------------------------------------------------------


def test_the_verb_refuses_bad_requests(scope: Scope) -> None:
    store: StoreHandle = registry.open_unshared(scope)
    try:
        verb = MaintenanceVerb(scope, store=lambda: store, release=lambda _h: None)

        async def ask(params: dict[str, Any]) -> Any:
            return await verb.handle(params)

        assert asyncio.run(ask({"op": "drop-all"}))["refused"] == "bad_op"
        assert asyncio.run(ask({"op": "prune", "ids": "old"}))["refused"] == "bad_params"
        too_many = {"op": "prune", "ids": ["x"] * (MAX_IDS + 1)}
        assert asyncio.run(ask(too_many))["refused"] == "bad_params"
        assert asyncio.run(ask({"op": "import", "path": "rel.jsonl"}))["refused"] == "bad_params"
        assert asyncio.run(ask({"op": "delete"}))["refused"] == "bad_params"
    finally:
        store.close()


def test_the_verb_is_owner_only() -> None:
    from indus.gateway.control_socket import KNOWN_VERBS, OWNER_ONLY_VERBS

    assert MAINTENANCE_VERB in KNOWN_VERBS and MAINTENANCE_VERB in OWNER_ONLY_VERBS


def test_an_unverified_peer_cannot_run_maintenance(scope: Scope) -> None:
    import json

    server = ControlServer(scope.home, env={})
    for verb, (handler, seconds) in MaintenanceVerb(scope).handlers().items():
        server.register(verb, handler, timeout=seconds)  # the daemon's path: no owner flag
    line = json.dumps(
        {"protocol": 1, "verb": MAINTENANCE_VERB, "id": 5, "params": {"op": "nothing"}}
    ).encode()
    refused = json.loads(asyncio.run(server.handle_line(line)))
    assert (refused["ok"], refused["error"]) == (False, "forbidden")
    served = json.loads(asyncio.run(server.handle_line(line, peer_verified=True)))
    assert served["ok"] is True and served["result"]["refused"] == "bad_op"


# -- read-only browsing (R03.16) ------------------------------------------------------------------


def _tree(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


@pytest.mark.parametrize(
    "argv",
    [
        ("sessions", "list"),
        ("sessions", "pinned"),
        ("sessions", "browse"),
        ("sessions", "stats"),
        ("sessions", "export", "-"),
        ("sessions", "prune", "--dry-run"),
        ("sessions", "archive", "--older-than", "1d", "--dry-run"),
    ],
)
def test_read_only_commands_create_nothing_in_an_empty_home(
    scope: Scope, argv: tuple[str, ...]
) -> None:
    before = _tree(scope.home)
    result = run(scope, *argv)
    assert result.code == 0, result.err
    assert _tree(scope.home) == before
    assert not connection.default_store_path(scope).exists()
    assert not (scope.home / "gateway.lock").exists()


def test_the_gate_answer_names_no_daemon_when_none_answers(scope: Scope) -> None:
    owner = MaintenanceGate(scope).owner()
    assert owner.daemon is False and owner.pid is None


def test_a_live_daemon_is_the_owner(scope: Scope) -> None:
    with daemon(scope):
        owner = MaintenanceGate(scope).owner()
    assert owner.daemon is True and owner.pid == os.getpid()


def test_gate_for_is_the_seam(monkeypatch: pytest.MonkeyPatch, scope: Scope) -> None:
    made: list[Scope] = []
    real: Callable[[Scope], MaintenanceGate] = sessions_maint.gate_for

    def fake(s: Scope) -> MaintenanceGate:
        made.append(s)
        return real(s)

    monkeypatch.setattr(sessions_maint, "gate_for", fake)
    _old_and_new(scope)
    assert run(scope, "sessions", "prune", "-y").code == 0
    assert made and made[0].home == scope.home
    assert gate_mod.MAINTENANCE_VERB == "maintenance-run"
