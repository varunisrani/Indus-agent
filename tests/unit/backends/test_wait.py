"""Wait loop, hard backstop and exit fence with fake processes and a fake clock (P05-02)."""

from __future__ import annotations

import asyncio
import threading

import pytest

from indus.backends.capture import OutputCapture, unbounded_capture
from indus.backends.contract import ExecResult
from indus.backends.wait import (
    DEADLINE_KILL_BUDGET_S,
    FOREGROUND,
    ProcessHandle,
    run_bounded,
    wait_process,
)
from tests.fakes.backend.environment import FakeEnvironment, FakeProcess, Rule
from tests.fakes.clock import FakeClock
from tests.unit.backends.conftest import drive


class Killer:
    def __init__(self) -> None:
        self.calls: list[float | None] = []

    async def __call__(self, handle: ProcessHandle, within: float | None) -> None:
        assert isinstance(handle, FakeProcess)
        self.calls.append(within)
        handle.kill()


def test_deadline_kills_flags_and_keeps_partial_output(fake_clock: FakeClock) -> None:
    proc = FakeProcess(["partial"], 0, hang=True)
    kill = Killer()

    async def go() -> ExecResult:
        work = wait_process(proc, unbounded_capture(), timeout=5, clock=fake_clock, kill=kill)
        return await drive(fake_clock, work)

    result = asyncio.run(go())
    assert result.returncode == 124 and result.timed_out and not result.interrupted
    assert result.output == "partial\n[Command timed out after 5s]"
    assert kill.calls == [DEADLINE_KILL_BUDGET_S]
    assert result.pid == proc.pid
    assert 5.0 <= fake_clock.monotonic() < 5.5


def test_timeout_without_output_has_no_leading_blank(fake_clock: FakeClock) -> None:
    proc = FakeProcess([], 0, hang=True)
    result = asyncio.run(
        drive(
            fake_clock,
            wait_process(proc, unbounded_capture(), timeout=2, clock=fake_clock, kill=Killer()),
        )
    )
    assert result.output == "[Command timed out after 2s]"


def test_command_exiting_124_itself_is_not_flagged(fake_clock: FakeClock) -> None:
    proc = FakeProcess(["own timeout\n"], 124)
    result = asyncio.run(
        drive(
            fake_clock,
            wait_process(proc, unbounded_capture(), timeout=5, clock=fake_clock, kill=Killer()),
        )
    )
    assert result.returncode == 124 and not result.timed_out
    assert result.output == "own timeout\n"


def test_exit_wins_over_a_deadline_in_the_same_round(fake_clock: FakeClock) -> None:
    proc = FakeProcess(["done"], 0)
    kill = Killer()
    result = asyncio.run(
        drive(
            fake_clock,
            wait_process(proc, unbounded_capture(), timeout=0, clock=fake_clock, kill=kill),
        )
    )
    assert result.returncode == 0 and not result.timed_out and kill.calls == []


def test_interrupt_returns_130_with_output(fake_clock: FakeClock) -> None:
    proc = FakeProcess(["working\n"], 0, hang=True)
    stop = threading.Event()
    kill = Killer()

    async def go() -> ExecResult:
        asyncio.get_running_loop().call_later(0.05, stop.set)
        return await drive(
            fake_clock,
            wait_process(
                proc, unbounded_capture(), timeout=600, clock=fake_clock, kill=kill, cancel=stop
            ),
            step=0.01,
        )

    result = asyncio.run(go())
    assert result.returncode == 130 and result.interrupted and not result.timed_out
    assert result.output == "working\n\n[Command interrupted]"
    assert kill.calls == [None]


def test_interrupt_set_before_the_first_poll(fake_clock: FakeClock) -> None:
    stop = asyncio.Event()
    stop.set()
    proc = FakeProcess([], 0, hang=True)
    result = asyncio.run(
        drive(
            fake_clock,
            wait_process(
                proc, unbounded_capture(), timeout=1, clock=fake_clock, kill=Killer(), cancel=stop
            ),
        )
    )
    assert result.interrupted and result.returncode == 130 and fake_clock.monotonic() < 1


def test_cancelling_the_wait_kills_the_tree(fake_clock: FakeClock) -> None:
    proc = FakeProcess([], 0, hang=True)
    kill = Killer()

    async def go() -> None:
        task = asyncio.ensure_future(
            wait_process(proc, unbounded_capture(), timeout=600, clock=fake_clock, kill=kill)
        )
        await asyncio.wait({task}, timeout=0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait({asyncio.ensure_future(fake_clock.run_until_idle())})

    asyncio.run(go())
    assert kill.calls == [None] and proc.killed == 1


def test_activity_callback_every_ten_seconds(fake_clock: FakeClock) -> None:
    proc = FakeProcess([], 0, hang=True)
    seen: list[str] = []
    asyncio.run(
        drive(
            fake_clock,
            wait_process(
                proc,
                unbounded_capture(),
                timeout=35,
                clock=fake_clock,
                kill=Killer(),
                on_activity=seen.append,
            ),
            step=0.2,
        )
    )
    assert seen == ["command running (10s)", "command running (20s)", "command running (30s)"]


class RecordingClock(FakeClock):
    def __init__(self, base: FakeClock) -> None:
        super().__init__(base.now())
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await super().sleep(seconds)


def test_poll_backs_off_from_5ms_to_200ms(fake_clock: FakeClock) -> None:
    clock = RecordingClock(fake_clock)
    proc = FakeProcess([], 0, hang=True)
    asyncio.run(
        drive(
            clock,
            wait_process(proc, unbounded_capture(), timeout=1.5, clock=clock, kill=Killer()),
            step=0.005,
        )
    )
    polls = [s for s in clock.sleeps if s <= 0.2]
    assert polls[:4] == pytest.approx([0.005, 0.0075, 0.01125, 0.016875])
    assert max(polls) == pytest.approx(0.2) and polls[-1] == pytest.approx(0.2)


def test_stdin_error_is_reported(fake_clock: FakeClock) -> None:
    async def go() -> ExecResult:
        proc = FakeProcess(["out"], 0)
        proc.set_stdin_outcome("stdin is not valid text")
        return await drive(
            fake_clock,
            wait_process(proc, unbounded_capture(), timeout=5, clock=fake_clock, kill=Killer()),
        )

    result = asyncio.run(go())
    assert result.stdin_error == "stdin is not valid text"
    assert result.output == "out\n[stdin write failed: stdin is not valid text]"


def test_wedged_reader_is_still_bounded(fake_clock: FakeClock) -> None:
    proc = FakeProcess(["head"], 0)
    proc.wedge_reader = True
    try:
        result = asyncio.run(
            drive(
                fake_clock,
                wait_process(proc, unbounded_capture(), timeout=5, clock=fake_clock, kill=Killer()),
            )
        )
    finally:
        proc.release_reader.set()
    assert result.output == "head" and result.returncode == 0
    assert fake_clock.monotonic() <= 5 + 2


def test_backstop_bounds_a_wait_that_never_returns(fake_clock: FakeClock) -> None:
    capture = OutputCapture(None)
    expired: list[bool] = []

    async def wedged() -> ExecResult:
        capture.append("before the wedge")
        await asyncio.Event().wait()  # never set: the inner wait never comes back
        raise AssertionError("unreachable")

    async def go() -> ExecResult:
        return await drive(
            fake_clock,
            run_bounded(
                wedged,
                timeout=3,
                clock=fake_clock,
                on_expiry=lambda: expired.append(True),
                capture=capture,
            ),
            step=0.1,
        )

    result = asyncio.run(go())
    assert result.returncode == 124 and result.timed_out
    assert result.output == "before the wedge\n[Command timed out after 3s]"
    assert expired == [True]
    assert 5.0 <= fake_clock.monotonic() < 5.2  # timeout + 2 s


def test_backstop_answers_after_the_killed_tree_is_reaped(fake_clock: FakeClock) -> None:
    order: list[str] = []

    async def wedged() -> ExecResult:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def reap() -> None:
        order.append("reap")
        await fake_clock.sleep(0.3)  # the killed child takes a while to be scheduled and die
        order.append("gone")

    async def go() -> ExecResult:
        return await drive(
            fake_clock,
            run_bounded(
                wedged,
                timeout=3,
                clock=fake_clock,
                on_expiry=lambda: order.append("kill"),
                capture=OutputCapture(None),
                reap=reap,
            ),
            step=0.1,
        )

    result = asyncio.run(go())
    assert result.returncode == 124 and result.timed_out
    assert order == ["kill", "reap", "gone"]
    assert 5.3 <= fake_clock.monotonic() < 5.5  # timeout + 2 s, then the reap


def test_backstop_passes_a_normal_result_through(fake_clock: FakeClock) -> None:
    async def quick() -> ExecResult:
        return ExecResult(output="ok", returncode=0)

    async def go() -> ExecResult:
        return await run_bounded(
            quick, timeout=3, clock=fake_clock, on_expiry=lambda: None, capture=OutputCapture(None)
        )

    assert asyncio.run(go()).output == "ok"


def test_fence_force_kills_live_commands_and_refuses_new_ones() -> None:
    proc = FakeProcess([], 0, hang=True)
    assert FOREGROUND.admit()
    fenced = FOREGROUND.publish(proc, lambda h: h.kill() if isinstance(h, FakeProcess) else None)
    assert not fenced and not FOREGROUND.fenced and len(FOREGROUND) == 1
    assert FOREGROUND.fence_and_kill(budget=0.1) == 1
    assert proc.killed == 1 and FOREGROUND.fenced
    assert not FOREGROUND.admit()


def test_no_spawn_after_the_fence(fake_clock: FakeClock) -> None:
    env = FakeEnvironment(clock=fake_clock)
    FOREGROUND.fence_and_kill(budget=0.0)
    result = asyncio.run(drive(fake_clock, env.execute("echo hi")))
    assert result.returncode == 130 and result.interrupted
    assert result.output == "[host is exiting: command not started]"
    assert env.calls == []


def test_a_spawn_that_raced_the_fence_is_force_killed(fake_clock: FakeClock) -> None:
    class Racing(FakeEnvironment):
        async def _spawn(self, script: str, *, login: bool, stdin: str | None) -> ProcessHandle:
            handle = await super()._spawn(script, login=login, stdin=stdin)
            FOREGROUND.fence_and_kill(budget=0.0)  # the host starts exiting mid-spawn
            return handle

    env = Racing(clock=fake_clock)
    env.script("serve", Rule(hang=True))
    result = asyncio.run(drive(fake_clock, env.execute("serve", timeout=30)))
    assert env.processes[0].force_killed == 1
    assert result.returncode != 0 and not result.timed_out
