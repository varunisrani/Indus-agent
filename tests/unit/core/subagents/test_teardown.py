"""Child teardown and resource hand-off (P10-06)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from indus.backends.processes import MAX_HANDOFFS, ProcessRecord
from indus.core.subagents.teardown import (
    KERNEL_RELEASER,
    ChildResources,
    set_lifecycle_hooks,
)
from indus.core.subagents.teardown import (
    _releasers as releasers,  # pyright: ignore[reportPrivateUsage]
)
from indus.tools.builtin.code import kernel
from tests.unit.tools.builtin._terminal_rig import Rig

PARENT, CHILD = "parent-session", "child-session"


class FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any], asyncio.Task[Any] | None]] = []

    def emit(self, event: str, payload: Mapping[str, Any]) -> None:
        self.events.append((event, dict(payload), asyncio.current_task()))


@pytest.fixture
def bus() -> Iterator[FakeBus]:
    fake = FakeBus()
    set_lifecycle_hooks(fake)
    yield fake
    set_lifecycle_hooks(None)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def resources(rig: Rig) -> ChildResources:
    return ChildResources(
        scope=rig.scope,
        parent_session_id=PARENT,
        child_session_id=CHILD,
        subagent_id="sa-0-abcd",
        goal="run the servers",
        processes=rig.shared.processes,
    )


def start(rig: Rig, command: str, session_id: str = CHILD) -> ProcessRecord:
    body = rig.run(rig.call(session_id, command=command, background=True, notify=True))
    record = rig.shared.processes.get(body["session_id"], str(rig.scope.home))
    assert record is not None
    return record


def handoff(rig: Rig, record: ProcessRecord, session_id: str = CHILD, **args: Any) -> Any:
    args.setdefault("purpose", "The parent needs the dev server for its checks.")
    return rig.run(rig.manage_call(session_id, action="handoff", session_id=record.id, **args))


def test_handed_off_job_survives_and_reports_to_the_parent(rig: Rig, bus: FakeBus) -> None:
    rig.deliverable(PARENT)
    child = resources(rig)
    rig.run(child.open())
    kept = start(rig, "dev-server")
    left = start(rig, "watcher")
    assert not kept.notify_on_exit  # a child cannot be told later (R02.17)
    body = handoff(rig, kept)
    assert body["status"] == "handed_off" and body["handoffs_left"] == MAX_HANDOFFS - 1
    assert kept.session_id == PARENT and kept.notify_on_exit
    assert "dev server" in kept.handoff_note

    async def finish() -> tuple[list[dict[str, Any]], asyncio.Task[Any] | None]:
        return await child.close(status="completed"), asyncio.current_task()

    orphans, closing_task = rig.run(finish())
    assert [o["id"] for o in orphans] == [left.id]
    assert orphans[0]["action"] == "killed" and "watcher" in orphans[0]["command"]
    assert left.exited and not kept.exited
    # The handed-off job belongs to the parent: its completion goes there.
    assert kept.handle is not None
    kept.handle.code = 0  # type: ignore[attr-defined]
    rig.shared.processes.notice_exit(kept)
    assert rig.inbox.kinds() == ["completion"]
    assert rig.inbox.events[0].session_id == PARENT
    # The grant is gone once the child ended.
    late = start(rig, "late")
    assert "not a subagent" in handoff(rig, late)["error"]
    names = [name for name, _, _ in bus.events]
    assert names == ["subagent_start", "subagent_stop"]
    stop_payload, stop_task = bus.events[1][1], bus.events[1][2]
    assert stop_payload["child_session_id"] == CHILD and stop_payload["status"] == "completed"
    assert stop_task is closing_task  # emitted on the caller's task


def test_fourth_handoff_is_refused(rig: Rig) -> None:
    child = resources(rig)
    rig.run(child.open())
    jobs = [start(rig, f"job-{i}") for i in range(MAX_HANDOFFS + 1)]
    for record in jobs[:MAX_HANDOFFS]:
        assert handoff(rig, record)["status"] == "handed_off"
    refused = handoff(rig, jobs[-1])
    assert "already handed off 3" in refused["error"]
    assert jobs[-1].session_id == CHILD
    orphans = rig.run(child.close())
    assert [o["id"] for o in orphans] == [jobs[-1].id]


def test_handoff_rules(rig: Rig) -> None:
    child = resources(rig)
    rig.run(child.open())
    record = start(rig, "server")
    assert "needs purpose" in handoff(rig, record, purpose="  ")["error"]
    parents_own = start(rig, "parent-job", session_id=PARENT)
    assert "not a subagent" in handoff(rig, parents_own, session_id=PARENT)["error"]
    other = start(rig, "someone-else", session_id="other")
    assert handoff(rig, other)["status"] == "not_found"  # not the child's job
    assert (
        rig.shared.processes.transfer_ownership(record, from_session="nobody", to_session=PARENT)
        is False
    )
    rig.run(child.close())


class StubKernel:
    """A pooled kernel without a process (the pool only needs these fields and calls)."""

    def __init__(self) -> None:
        self.last_used = 0.0
        self.busy = False
        self.pinned = False
        self.closed = False

    async def alive(self) -> bool:
        return not self.closed

    async def close(self) -> None:
        self.closed = True

    def force_close(self) -> None:
        self.closed = True


async def _stub() -> StubKernel:
    return StubKernel()


def test_child_kernel_is_disposed_and_never_in_the_lru(rig: Rig) -> None:
    assert KERNEL_RELEASER in releasers  # the real pool is wired in, no stand-in
    pool = cast(Any, kernel.POOL)  # the process-wide local pool, holding stub kernels
    owner = kernel.child_owner(PARENT, CHILD)
    assert owner == f"{PARENT}::child::{CHILD}"

    async def take(who: str, cap: int = 4) -> StubKernel:
        key = kernel.KernelKey(who, "local", "project", "py", "/w", ())
        taken = await pool.take(key, clock=rig.clock, reset=False, idle_s=1800, cap=cap, make=_stub)
        taken.kernel.busy = False
        return cast(StubKernel, taken.kernel)

    async def scenario() -> None:
        mine = await take(owner)
        tops = [await take(f"top-{i}", cap=2) for i in range(4)]
        assert mine.pinned and not mine.closed
        assert owner in {k.owner for k in pool.live_keys()}
        assert pool.counted() == 2  # the child's kernel is outside the top-level LRU
        assert [t.closed for t in tops] == [True, True, False, False]
        child = resources(rig)
        await child.open()
        await child.close()
        assert mine.closed
        assert owner not in {k.owner for k in pool.live_keys()}
        assert [t.closed for t in tops] == [True, True, False, False]  # others untouched

    try:
        rig.run(scenario())
    finally:
        asyncio.run(kernel.POOL.shutdown())


def test_close_runs_once_and_a_failing_releaser_does_not_stop_it(rig: Rig) -> None:
    from indus.core.subagents.teardown import register_releaser

    calls: list[str] = []

    def broken(parent: str, child: str) -> None:
        raise RuntimeError("browser gone")

    async def fine(parent: str, child: str) -> None:
        calls.append(child)

    register_releaser("broken", broken)
    register_releaser("fine", fine)
    try:
        child = resources(rig)
        rig.run(child.open())
        rig.run(child.close())
        rig.run(child.close())
        assert calls == [CHILD]
    finally:
        register_releaser("broken", None)
        register_releaser("fine", None)
