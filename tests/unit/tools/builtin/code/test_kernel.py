"""Persistent session kernels on this machine (P10-10)."""

from __future__ import annotations

import asyncio
import os
import signal
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from indus.foundation.clock import SystemClock
from indus.tools.builtin.code import ExecuteCodeTool, kernel
from indus.tools.builtin.code.kernel import KernelKey, KernelPool, child_owner
from tests.fakes.clock import FakeClock, checkpoint
from tests.unit.tools.builtin.code._rig import call, code_scope, fake_tools, run, shutdown_pools

KERNEL = {"code_execution": {"kernel": "auto"}}


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def _kernel_pid(session: str = "s1") -> int:
    (found,) = [k for k in kernel.POOL.live_keys() if k.owner == session]
    live = kernel.POOL.get(found)
    assert live is not None
    return live.child.pid


def test_state_survives_between_calls_and_loops(tmp_path: Path) -> None:
    scope = code_scope(tmp_path, KERNEL)
    registry, seen = fake_tools(tmp_path)
    tool = ExecuteCodeTool()
    first = run(
        call(tool, scope, "import math\ntotal = 40\nprint('set')", registry=registry, cwd=tmp_path)
    )
    second = run(
        call(
            tool,
            scope,
            "from indus_tools import terminal\nprint(total + 2, math.pi > 3, "
            "terminal('ls')['output'])",
            registry=registry,
            cwd=tmp_path,
        )
    )
    assert first["kernel"] == {"mode": "session", "reused": False, "execution_count": 1}
    assert second["output"] == "42 True ran ls\n"
    assert second["kernel"] == {"mode": "session", "reused": True, "execution_count": 2}
    assert second["tool_calls_made"] == 1 and seen == [("terminal", {"command": "ls"})]


def test_budget_is_per_call(tmp_path: Path) -> None:
    scope = code_scope(
        tmp_path, {**KERNEL, "code_execution": {"kernel": "auto", "max_tool_calls": 2}}
    )
    registry, _ = fake_tools(tmp_path)
    tool = ExecuteCodeTool()
    code = (
        "from indus_tools import terminal, ToolCallRefused\n"
        "done = 0\n"
        "for i in range(3):\n"
        "    try:\n"
        "        terminal('x'); done += 1\n"
        "    except ToolCallRefused:\n"
        "        pass\n"
        "print(done)\n"
    )
    assert run(call(tool, scope, code, registry=registry, cwd=tmp_path))["output"] == "2\n"
    assert run(call(tool, scope, code, registry=registry, cwd=tmp_path))["output"] == "2\n"


def test_reset_discards_state(tmp_path: Path) -> None:
    scope = code_scope(tmp_path, KERNEL)
    registry, _ = fake_tools(tmp_path)
    tool = ExecuteCodeTool()
    run(call(tool, scope, "kept = 1", registry=registry, cwd=tmp_path))
    old_pid = _kernel_pid()
    after = run(
        call(tool, scope, "print('kept' in globals())", registry=registry, cwd=tmp_path, reset=True)
    )
    assert after["output"] == "False\n", repr(after)
    assert after["kernel"]["state_reset"] is True and after["kernel"]["reused"] is False
    assert not _alive(old_pid)


def test_error_cell_keeps_the_kernel_and_shows_the_traceback(tmp_path: Path) -> None:
    scope = code_scope(tmp_path, KERNEL)
    registry, _ = fake_tools(tmp_path)
    tool = ExecuteCodeTool()
    failed = run(call(tool, scope, "value = 7\nprint('a')\n1 / 0", registry=registry, cwd=tmp_path))
    assert failed["status"] == "error" and failed["exit_code"] == 1
    assert failed["error"] == "ZeroDivisionError: division by zero"
    assert failed["output"].startswith("a\n\n--- stderr ---\n") and "<cell 1>" in failed["output"]
    assert "1 / 0" in failed["output"]  # the cell's source line is in the traceback
    again = run(call(tool, scope, "print(value)", registry=registry, cwd=tmp_path))
    assert again["output"] == "7\n" and again["kernel"]["reused"] is True


def test_sys_exit_ends_the_kernel(tmp_path: Path) -> None:
    scope = code_scope(tmp_path, KERNEL)
    registry, _ = fake_tools(tmp_path)
    tool = ExecuteCodeTool()
    ended = run(call(tool, scope, "x = 1\nraise SystemExit(0)", registry=registry, cwd=tmp_path))
    assert ended["status"] == "success" and ended["kernel"]["ended"] is True
    assert len(kernel.POOL) == 0
    fresh = run(call(tool, scope, "print('x' in globals())", registry=registry, cwd=tmp_path))
    assert fresh["output"] == "False\n" and "state_lost" not in fresh["kernel"]


def test_kernel_killed_between_calls_reports_lost_state(tmp_path: Path) -> None:
    scope = code_scope(tmp_path, KERNEL)
    registry, _ = fake_tools(tmp_path)
    tool = ExecuteCodeTool()
    run(call(tool, scope, "x = 1", registry=registry, cwd=tmp_path))
    pid = _kernel_pid()
    (key,) = kernel.POOL.live_keys()
    live = kernel.POOL.get(key)
    assert live is not None
    os.killpg(pid, signal.SIGKILL)
    # Wait for the reapable exit, not the zombie leader: a multi-threaded leader shows as "Z" in
    # /proc while its other threads are still exiting, and waitpid cannot reap it yet.
    for _ in range(500):
        if not live.child.alive():
            break
        threading.Event().wait(0.01)
    after = run(call(tool, scope, "print('x' in globals())", registry=registry, cwd=tmp_path))
    assert after["output"] == "False\n", repr(after)
    assert after["kernel"]["state_lost"] is True and after["kernel"]["reused"] is False


def test_child_output_written_to_fd_is_captured(tmp_path: Path) -> None:
    scope = code_scope(tmp_path, KERNEL)
    registry, _ = fake_tools(tmp_path)
    code = "import subprocess, sys\nprint('py', flush=True)\nsubprocess.run(['echo', 'from-child'])"
    result = run(call(ExecuteCodeTool(), scope, code, registry=registry, cwd=tmp_path))
    assert "py" in result["output"] and "from-child" in result["output"]


def test_per_cell_capture_is_bounded(tmp_path: Path) -> None:
    scope = code_scope(tmp_path, KERNEL)
    registry, _ = fake_tools(tmp_path)
    result = run(
        call(ExecuteCodeTool(), scope, "print('y' * 1_200_000)", registry=registry, cwd=tmp_path)
    )
    assert "1,000,000" in result["kernel"]["capture_cut"]
    assert result["stdout_truncated"] is True and result["stdout_bytes_total"] == 1_000_000
    assert Path(result["full_output_path"]).stat().st_size == 1_000_000


def test_fifth_kernel_evicts_the_least_recently_used(tmp_path: Path) -> None:
    scope = code_scope(tmp_path, KERNEL)
    registry, _ = fake_tools(tmp_path)
    tool = ExecuteCodeTool()
    for session in ("s1", "s2", "s3", "s4"):
        run(
            call(
                tool,
                scope,
                f"name = {session!r}",
                registry=registry,
                cwd=tmp_path,
                session_id=session,
            )
        )
    run(
        call(tool, scope, "print(name)", registry=registry, cwd=tmp_path, session_id="s1")
    )  # s2 is LRU now
    s2_pid = _kernel_pid("s2")
    run(call(tool, scope, "name = 's5'", registry=registry, cwd=tmp_path, session_id="s5"))
    assert sorted(k.owner for k in kernel.POOL.live_keys()) == ["s1", "s3", "s4", "s5"]
    assert not _alive(s2_pid)
    back = run(
        call(
            tool,
            scope,
            "print('name' in globals())",
            registry=registry,
            cwd=tmp_path,
            session_id="s2",
        )
    )
    assert back["output"] == "False\n"


def test_child_kernels_are_pinned_and_outside_the_cap(tmp_path: Path) -> None:
    scope = code_scope(tmp_path, KERNEL)
    registry, _ = fake_tools(tmp_path)
    tool = ExecuteCodeTool()
    child = child_owner("s1", "c1")
    run(
        call(
            tool, scope, "mine = 'child'", registry=registry, cwd=tmp_path, code_kernel_owner=child
        )
    )
    for session in ("s1", "s2", "s3", "s4", "s5"):
        run(call(tool, scope, "pass", registry=registry, cwd=tmp_path, session_id=session))
    owners = {k.owner for k in kernel.POOL.live_keys()}
    assert child in owners and len(owners) == 5 and kernel.POOL.counted() == 4
    kept = run(
        call(tool, scope, "print(mine)", registry=registry, cwd=tmp_path, code_kernel_owner=child)
    )
    assert kept["output"] == "child\n"
    assert asyncio.run(kernel.dispose_child("c1")) == 1
    assert child not in {k.owner for k in kernel.POOL.live_keys()}


def test_dispose_session(tmp_path: Path) -> None:
    scope = code_scope(tmp_path, KERNEL)
    registry, _ = fake_tools(tmp_path)
    run(call(ExecuteCodeTool(), scope, "pass", registry=registry, cwd=tmp_path, session_id="gone"))
    pid = _kernel_pid("gone")
    assert asyncio.run(kernel.dispose_session("gone")) == 1
    assert not _alive(pid) and len(kernel.POOL) == 0
    assert not list((tmp_path / "t").iterdir())  # staging dir removed


def test_runner_exits_when_indus_closes_its_pipe(tmp_path: Path) -> None:
    scope = code_scope(tmp_path, KERNEL)
    registry, _ = fake_tools(tmp_path)
    run(call(ExecuteCodeTool(), scope, "pass", registry=registry, cwd=tmp_path))
    live = kernel.POOL.get(kernel.POOL.live_keys()[0])
    assert live is not None
    live._close_pipe()  # pyright: ignore[reportPrivateUsage]
    for _ in range(300):
        if live.child.poll() is not None:
            break
        threading.Event().wait(0.01)
    assert live.child.poll() == 0


# -- fake clock: idle and timeout ------------------------------------------------------------------


def _clock() -> FakeClock:
    return FakeClock(datetime(2026, 10, 19, tzinfo=UTC))


async def _drive(
    clock: FakeClock, work: Any, *, ready: Path | None = None, then: float = 0.0
) -> Any:
    """Run ``work``, moving the fake clock in small steps while it waits; once ``ready`` exists,
    jump ``then`` seconds."""
    task = asyncio.ensure_future(work)
    jumped = ready is None
    for _ in range(200_000):
        await checkpoint()
        await asyncio.wait({task}, timeout=0.002)
        if task.done():
            return task.result()
        if not jumped and ready is not None and ready.exists():
            clock.advance(then)
            jumped = True
        elif clock.pending:
            clock.advance(0.01 if not jumped else 0.25)
    raise AssertionError("work did not finish")


def test_idle_kernel_is_disposed_after_1800_seconds(tmp_path: Path) -> None:
    clock = _clock()
    scope = code_scope(tmp_path, KERNEL, clock=clock)
    registry, _ = fake_tools(tmp_path)

    async def scenario() -> tuple[int, int]:
        await _drive(
            clock, call(ExecuteCodeTool(), scope, "x = 1", registry=registry, cwd=tmp_path)
        )
        pid = _kernel_pid()
        clock.advance(1799)
        kept = await kernel.POOL.reap_idle(clock, 1800)
        clock.advance(2)
        closed = await _drive(clock, kernel.POOL.reap_idle(clock, 1800))
        assert kept == 0 and closed == 1
        return pid, len(kernel.POOL)

    pid, left = asyncio.run(scenario())
    assert left == 0 and not _alive(pid)


def test_timeout_kills_the_kernel_and_the_next_call_starts_fresh(tmp_path: Path) -> None:
    clock = _clock()
    scope = code_scope(tmp_path, KERNEL, clock=clock)
    registry, _ = fake_tools(tmp_path)
    ready = tmp_path / "ready"
    tool = ExecuteCodeTool()

    async def scenario() -> tuple[dict[str, Any], dict[str, Any], int]:
        await _drive(clock, call(tool, scope, "kept = 1", registry=registry, cwd=tmp_path))
        pid = _kernel_pid()
        slow = f"import time\nopen({str(ready)!r}, 'w').write('1')\ntime.sleep(600)"
        timed_out = await _drive(
            clock, call(tool, scope, slow, registry=registry, cwd=tmp_path), ready=ready, then=301
        )
        fresh = await _drive(
            clock, call(tool, scope, "print('kept' in globals())", registry=registry, cwd=tmp_path)
        )
        return timed_out, fresh, pid

    timed_out, fresh, pid = asyncio.run(scenario())
    assert timed_out["status"] == "timeout" and timed_out["kernel"]["ended"] is True
    assert "session kernel was stopped and its state is gone" in timed_out["error"]
    assert not _alive(pid)
    assert fresh["output"] == "False\n"
    assert fresh["kernel"]["state_lost"] is True and "not here" in fresh["kernel"]["note"]


def test_teardown_closes_a_fake_clock_kernel_whose_child_has_not_exited(tmp_path: Path) -> None:
    # A kernel made under a test's fake clock is closed by the teardown (`shutdown_pools`) with
    # that clock: `stop_tree` waits on it while the child is still running. Nothing advanced it
    # there, so a child slower to exit than the pipe close (a loaded machine) hung the whole
    # suite. Held stopped here, the child cannot exit until it is killed.
    clock = _clock()
    scope = code_scope(tmp_path, KERNEL, clock=clock)
    registry, _ = fake_tools(tmp_path)

    async def scenario() -> None:
        await _drive(
            clock, call(ExecuteCodeTool(), scope, "x = 1", registry=registry, cwd=tmp_path)
        )

    asyncio.run(scenario())
    pid = _kernel_pid()
    os.kill(pid, signal.SIGSTOP)
    done = threading.Event()

    def teardown() -> None:
        shutdown_pools()
        done.set()

    threading.Thread(target=teardown, daemon=True).start()
    assert done.wait(30), "the pools never closed"
    assert len(kernel.POOL) == 0 and not _alive(pid)


def test_interrupt_kills_the_kernel(tmp_path: Path) -> None:
    scope = code_scope(tmp_path, KERNEL)
    registry, _ = fake_tools(tmp_path)
    ready = tmp_path / "ready"
    cancel = threading.Event()

    async def scenario() -> dict[str, Any]:
        code = f"import time\nopen({str(ready)!r}, 'w').write('1')\ntime.sleep(600)"
        task = asyncio.ensure_future(
            call(ExecuteCodeTool(), scope, code, registry=registry, cwd=tmp_path, cancel=cancel)
        )
        while not ready.exists():
            await asyncio.wait({task}, timeout=0.01)
        cancel.set()
        return await task

    result = run(scenario())
    assert result["status"] == "interrupted" and result["kernel"]["ended"] is True
    assert len(kernel.POOL) == 0


# -- the pool rules with stand-in kernels ----------------------------------------------------------


class Stub:
    def __init__(self) -> None:
        self.last_used = 0.0
        self.busy = False
        self.pinned = False
        self.closed = False
        self.living = True

    async def alive(self) -> bool:
        return self.living

    async def close(self) -> None:
        self.closed = True

    def force_close(self) -> None:
        self.closed = True


def _key(owner: str) -> KernelKey:
    return KernelKey(owner, "local", "project", "py", "/w", ())


async def _take(pool: KernelPool[Stub], owner: str, clock: Any, **kw: Any) -> Stub:
    taken = await pool.take(
        _key(owner),
        clock=clock,
        reset=kw.get("reset", False),
        idle_s=1800,
        cap=kw.get("cap", 4),
        make=lambda: _make(),
    )
    if kw.get("release", True):
        pool.give_back(taken.kernel, clock)
    return taken.kernel


async def _make() -> Stub:
    return Stub()


def test_pool_never_evicts_a_busy_or_pinned_kernel() -> None:
    clock = _clock()
    pool: KernelPool[Stub] = KernelPool("test")

    async def scenario() -> None:
        busy = await _take(pool, "a", clock, release=False)
        clock.advance(1)
        pinned = await _take(pool, child_owner("p", "c"), clock)
        clock.advance(1)
        others = []
        for owner in ("b", "c", "d", "e"):
            others.append(await _take(pool, owner, clock, cap=2))
            clock.advance(1)
        assert not busy.closed and not pinned.closed and pinned.pinned
        assert {k.owner for k in pool.live_keys()} == {"a", child_owner("p", "c"), "e"}
        clock.advance(4000)
        assert await pool.reap_idle(clock, 1800) == 1  # only "e": "a" is busy, the child is pinned
        assert not busy.closed and not pinned.closed

    asyncio.run(scenario())


def test_pool_reports_reset_and_lost() -> None:
    clock = SystemClock()
    pool: KernelPool[Stub] = KernelPool("test")

    async def scenario() -> None:
        first = await pool.take(_key("a"), clock=clock, reset=False, idle_s=60, cap=4, make=_make)
        assert (first.reused, first.state_reset, first.state_lost) == (False, False, False)
        pool.give_back(first.kernel, clock)
        again = await pool.take(_key("a"), clock=clock, reset=True, idle_s=60, cap=4, make=_make)
        assert (again.reused, again.state_reset) == (False, True) and first.kernel.closed
        again.kernel.living = False
        pool.give_back(again.kernel, clock)
        third = await pool.take(_key("a"), clock=clock, reset=False, idle_s=60, cap=4, make=_make)
        assert third.state_lost and not third.reused
        await pool.discard(_key("a"), third.kernel)
        fourth = await pool.take(_key("a"), clock=clock, reset=False, idle_s=60, cap=4, make=_make)
        assert fourth.state_lost
        pool.give_back(fourth.kernel, clock)
        pool.force_shutdown()
        assert fourth.kernel.closed and len(pool) == 0

    asyncio.run(scenario())
