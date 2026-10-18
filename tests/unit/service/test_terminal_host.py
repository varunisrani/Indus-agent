"""Host answers for the backends: receipt lineage and orphan-sweep liveness (P11 integration)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from indus.backends import process_receipts
from indus.backends.docker import reaper
from indus.backends.processes import ProcessRegistry, SpawnRequest
from indus.backends.wait import FOREGROUND
from indus.foundation.scope import Scope
from indus.service import terminal_host
from indus.state import compression, registry, sessions
from indus.state.registry import StoreHandle
from indus.tools.builtin.terminal import TerminalRuntime
from tests.fakes.terminal import FakeJob

HANDOFF = [
    {"role": "user", "content": "summary so far"},
    {"role": "assistant", "content": "carrying on"},
]


@pytest.fixture
def store(scope: Scope) -> Iterator[StoreHandle]:
    handle = registry.open_unshared(scope)
    yield handle
    handle.close()
    process_receipts.set_lineage_resolver(None)
    reaper.reset_for_tests()


def _continue(store: StoreHandle, parent: str, child: str) -> None:
    assert compression.acquire(store, parent, "rotator")
    mark = compression.watermark(store, parent)
    compression.publish_continuation(store, parent, {"id": child}, HANDOFF, mark, "rotator")


def test_continuation_reads_the_owners_receipts(store: StoreHandle) -> None:
    sessions.create(store, "s1", "cli")
    sessions.create(store, "x9", "cli")
    _continue(store, "s1", "s2")
    assert terminal_host.is_continuation(store, "s1", "s2")
    assert not terminal_host.is_continuation(store, "s1", "x9")
    assert not terminal_host.is_continuation(store, "s1", "")
    assert not terminal_host.is_continuation(store, "s1", "never-seen")


def test_session_liveness_fails_safe(store: StoreHandle) -> None:
    sessions.create(store, "open1", "cli")
    sessions.create(store, "done1", "cli")
    sessions.end(store, "done1", "user_exit")
    sessions.create(store, "p1", "cli")
    _continue(store, "p1", "p2")  # the parent ended, its continuation is open
    assert terminal_host.session_alive(store, "open1")
    assert not terminal_host.session_alive(store, "done1")
    assert terminal_host.session_alive(store, "p1")
    assert terminal_host.session_alive(store, "unknown")  # not ours to judge: kept


def test_jobs_alive_matches_session_and_key(scope: Scope) -> None:
    def launch(spec: object) -> FakeJob:
        return FakeJob()

    async def go() -> tuple[bool, bool, bool]:
        processes = ProcessRegistry(launcher=launch, watch_exits=False)
        key = f"{scope.home}::session:sess1@docker"
        await processes.spawn(SpawnRequest("sleep 9", scope, "sess1", key, "/", {}))
        answers = (
            terminal_host.jobs_alive(processes, "sess1"),
            terminal_host.jobs_alive(processes, "other"),
            terminal_host.jobs_alive(processes, "shared"),
        )
        await processes.shutdown()
        return answers

    assert asyncio.run(go()) == (True, False, False)


def test_install_points_receipts_and_sweep_at_the_store(store: StoreHandle) -> None:
    sessions.create(store, "s1", "cli")
    _continue(store, "s1", "s2")
    terminal_host.install(store, ProcessRegistry())
    assert process_receipts._owns("s1", "s2")  # pyright: ignore[reportPrivateUsage]
    assert not process_receipts._owns("s1", "zz")  # pyright: ignore[reportPrivateUsage]
    sessions.end(store, "s2", "user_exit")
    report = reaper._liveness  # pyright: ignore[reportPrivateUsage]
    assert report["session"]("s1") is False and report["session"]("nobody") is True
    assert report["jobs"]("s1") is False


def test_runtime_registers_and_drops_the_lost_handler() -> None:
    from indus.backends.docker import recovery

    async def go() -> None:
        runtime = TerminalRuntime.create()
        handler = runtime.lost_handler
        assert handler is not None
        with recovery._handlers_lock:  # pyright: ignore[reportPrivateUsage]
            assert handler in recovery._handlers  # pyright: ignore[reportPrivateUsage]
        await runtime.shutdown()
        with recovery._handlers_lock:  # pyright: ignore[reportPrivateUsage]
            assert handler not in recovery._handlers  # pyright: ignore[reportPrivateUsage]

    try:
        asyncio.run(go())
    finally:
        FOREGROUND.reset_for_tests()  # shutdown fenced the process-wide foreground gate
