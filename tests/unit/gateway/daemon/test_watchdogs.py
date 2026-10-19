"""P15-25: loop watchdog strikes, deadlines, stack dumps, the restart-loop guard, sd_notify."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from indus.gateway.daemon.watchdogs import (
    Deadline,
    LoopWatchdog,
    RestartLoopGuard,
    SystemdNotifier,
    dump_stacks,
    loop_probe,
    notify,
)
from tests.fakes.clock import FakeClock, checkpoint

START = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)


def test_three_unanswered_probes_in_a_row_trip_the_watchdog() -> None:
    answers = iter([False, False, True, False, False, False, False])
    tripped: list[int] = []
    dog = LoopWatchdog(lambda: next(answers), lambda: tripped.append(1), strikes=3)
    results = [dog.check() for _ in range(6)]
    # miss, miss, answered (reset), miss, miss, miss → trips on the sixth probe only
    assert results == [False, False, False, False, False, True]
    assert tripped == [1] and dog.tripped
    assert dog.check() is False and tripped == [1]  # never trips twice


def test_a_probe_that_raises_counts_as_a_miss() -> None:
    def boom() -> bool:
        raise RuntimeError("probe broke")

    dog = LoopWatchdog(boom, lambda: None, strikes=1)
    assert dog.check()


def test_the_watchdog_thread_probes_on_its_interval() -> None:
    fired = threading.Event()
    dog = LoopWatchdog(lambda: False, fired.set, interval=0.01, strikes=2)
    dog.start()
    assert fired.wait(5)
    dog.stop()
    dog.join(5)


def test_the_loop_probe_sees_a_wedged_loop() -> None:
    async def main() -> None:
        loop = asyncio.get_running_loop()
        answered = await asyncio.to_thread(loop_probe, loop, 5.0)
        assert answered
        wedged = threading.Event()
        result: list[bool] = []

        def probe_while_wedged() -> None:
            wedged.wait(5)
            result.append(loop_probe(loop, 0.05))

        thread = threading.Thread(target=probe_while_wedged)
        thread.start()
        wedged.set()
        thread.join(5)  # the loop is blocked in this join: the probe cannot be answered
        assert result == [False]

    asyncio.run(main())


def test_deadline_fires_unless_cancelled() -> None:
    fired = threading.Event()
    Deadline(0.01, fired.set, name="t").start()
    assert fired.wait(5)
    quiet = threading.Event()
    deadline = Deadline(0.2, quiet.set, name="t").start()
    deadline.cancel()
    assert not quiet.wait(0.4) and not deadline.fired


def test_stack_dump_has_threads_and_tasks(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "gateway_faulthandler.log"

    async def main() -> None:
        async def parked() -> None:
            await asyncio.get_running_loop().create_future()

        task = asyncio.ensure_future(parked())
        await checkpoint()
        dump_stacks(path, "SIGUSR2", loop=asyncio.get_running_loop())
        task.cancel()

    asyncio.run(main())
    text = path.read_text(encoding="utf-8")
    assert "stack dump: SIGUSR2" in text
    assert "Thread" in text or "thread" in text
    assert "asyncio task(s)" in text and "parked" in text
    dump_stacks(path, "second")
    assert path.read_text(encoding="utf-8").count("=== ") == 2


@pytest.mark.parametrize(
    ("earlier", "now", "tripped", "kept"),
    [
        ([], 1000.0, False, 1),
        ([990.0], 1000.0, False, 2),
        ([980.0, 990.0], 1000.0, True, 3),
        ([600.0, 800.0], 1000.0, True, 3),  # slow chain: every gap within 300 s
        ([500.0, 990.0], 1000.0, False, 2),  # a gap of 490 s ends the older run
        ([1500.0, 990.0], 1000.0, True, 3),  # clock stepped back: a "future" boot still counts
    ],
)
def test_restart_loop_guard_chains_boots(
    tmp_path: Path, earlier: list[float], now: float, tripped: bool, kept: int
) -> None:
    path = tmp_path / "gateway_boots.json"
    if earlier:
        path.write_text(json.dumps({"boots": earlier}), encoding="utf-8")
    guard = RestartLoopGuard(path)
    assert guard.check_and_record(now) is tripped
    assert len(json.loads(path.read_text(encoding="utf-8"))["boots"]) == kept


def test_restart_loop_guard_keeps_fifty_and_fails_open(tmp_path: Path) -> None:
    path = tmp_path / "gateway_boots.json"
    path.write_text(json.dumps({"boots": [float(i) for i in range(100)]}), encoding="utf-8")
    guard = RestartLoopGuard(path)
    assert guard.check_and_record(100.0)
    assert len(json.loads(path.read_text(encoding="utf-8"))["boots"]) == 50
    path.write_text("garbage", encoding="utf-8")
    assert not RestartLoopGuard(path).check_and_record(200.0)  # unreadable: fail open
    blocked = RestartLoopGuard(tmp_path / "missing-dir" / "x" / "boots.json")
    (tmp_path / "missing-dir").write_text("a file, not a folder", encoding="utf-8")
    assert not blocked.check_and_record(1.0)
    guard.clear()
    guard.clear()


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix sockets")
def test_sd_notify_datagrams(tmp_path: Path) -> None:
    address = str(tmp_path / "notify.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
        server.bind(address)
        server.settimeout(5)
        env = {"NOTIFY_SOCKET": address}
        assert notify("STATUS=hello", env)
        assert server.recv(256) == b"STATUS=hello"
        assert not notify("READY=1", {})
        assert not notify("READY=1", {"NOTIFY_SOCKET": str(tmp_path / "nobody.sock")})

        async def main() -> None:
            clock = FakeClock(START)
            notifier = SystemdNotifier(env, clock=clock, watchdog_seconds=10.0)
            assert notifier.ready("running")
            assert server.recv(256) == b"READY=1\nSTATUS=running"
            await checkpoint()
            await clock.advance_and_settle(5.0)
            assert server.recv(256) == b"WATCHDOG=1"
            await notifier.stopping()
            await notifier.stopping()
            assert server.recv(256) == b"STOPPING=1"

        asyncio.run(main())
    assert not SystemdNotifier({}, clock=FakeClock(START)).active
    os.unlink(address)
