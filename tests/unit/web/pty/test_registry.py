"""P18-08: keep-alive PTYs — supersession, ring replay + redraw, exit, cap, reaper, orphans
(R09.6). A fake bridge and a fake clock stand in for the child and time."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest

from indus.web.pty.registry import (
    CLOSE_EXITED,
    CLOSE_SUPERSEDED,
    MAX_SESSIONS,
    REDRAW,
    RING_BYTES,
    TTL_S,
    PtyKey,
    PtyRegistry,
    RegistryFull,
    RingBuffer,
    process_ancestors,
)
from indus.web.pty.resize import TermSize
from tests.fakes.clock import FakeClock
from tests.fakes.pty.fake_bridge import FakeBridge, FakeViewer
from tests.unit.backends._aio import aio


def _spawner(made: list[FakeBridge]) -> Callable[[], Awaitable[FakeBridge]]:
    async def spawn() -> FakeBridge:
        bridge = FakeBridge()
        made.append(bridge)
        return bridge

    return spawn


def _registry(clock: FakeClock, **kw: object) -> PtyRegistry:
    return PtyRegistry(clock=clock, **kw)  # pyright: ignore[reportArgumentType]


def test_ring_keeps_the_last_bytes() -> None:
    ring = RingBuffer(8)
    ring.append(b"abcdef")
    ring.append(b"ghij")
    assert ring.snapshot() == b"cdefghij"
    assert ring.truncated
    assert RING_BYTES == 1024 * 1024
    with pytest.raises(ValueError):
        RingBuffer(0)


@aio
async def test_second_attach_supersedes_and_replays_then_redraws(fake_clock: FakeClock) -> None:
    made: list[FakeBridge] = []
    registry = _registry(fake_clock)
    key = PtyKey("tab-token", "default", "")
    first = FakeViewer("first")
    session, created = await registry.attach_or_spawn(key, _spawner(made))
    assert created
    assert await session.attach(first, redraw=not created)
    made[0].emit(b"hello ")
    made[0].emit(b"world")
    await fake_clock.run_until_idle()
    assert first.text == b"hello world"
    assert made[0].written == []  # a new PTY gets no redraw byte

    second = FakeViewer("second")
    again, created = await registry.attach_or_spawn(key, _spawner(made))
    assert again is session and not created
    assert await again.attach(second, redraw=not created)
    assert first.closes == [CLOSE_SUPERSEDED]
    assert second.received == [b"hello world"]
    assert made[0].written == [REDRAW] and REDRAW == b"\x0c"
    assert len(made) == 1

    # Input from the superseded socket is dropped; its late detach changes nothing.
    assert await session.write(first, b"stale")
    registry.detach(key, first)
    assert session.attached and session.viewer is second
    assert await session.write(second, b"ls\r")
    assert made[0].written == [REDRAW, b"ls\r"]
    made[0].emit(b"!")
    await fake_clock.run_until_idle()
    assert second.received[-1] == b"!"
    await registry.close_all()


@aio
async def test_child_exit_closes_the_viewer_4410(fake_clock: FakeClock) -> None:
    made: list[FakeBridge] = []
    registry = _registry(fake_clock)
    viewer = FakeViewer()
    session, _ = await registry.attach_or_spawn(PtyKey("t"), _spawner(made))
    await session.attach(viewer, redraw=False)
    made[0].exit()
    await fake_clock.run_until_idle()
    assert viewer.closes == [CLOSE_EXITED]
    assert not session.alive
    assert await registry.reap() == 1
    assert len(registry) == 0
    assert made[0].closed


@aio
async def test_output_while_detached_is_kept_for_the_next_tab(fake_clock: FakeClock) -> None:
    made: list[FakeBridge] = []
    registry = _registry(fake_clock)
    key = PtyKey("t")
    viewer = FakeViewer()
    session, _ = await registry.attach_or_spawn(key, _spawner(made))
    await session.attach(viewer, redraw=False)
    registry.detach(key, viewer)
    made[0].emit(b"while away")
    await fake_clock.run_until_idle()
    assert viewer.received == []
    later = FakeViewer("later")
    again, created = await registry.attach_or_spawn(key, _spawner(made))
    await again.attach(later, redraw=not created)
    assert later.received == [b"while away"]
    await registry.close_all()


@aio
async def test_a_viewer_gone_mid_replay_is_detached(fake_clock: FakeClock) -> None:
    made: list[FakeBridge] = []
    registry = _registry(fake_clock)
    key = PtyKey("t")
    session, _ = await registry.attach_or_spawn(key, _spawner(made))
    made[0].emit(b"x")
    await fake_clock.run_until_idle()
    gone = FakeViewer(fail_sends=True)
    assert not await session.attach(gone, redraw=True)
    assert not session.attached
    assert session.detached_at == fake_clock.monotonic()
    await registry.close_all()


@aio
async def test_stalled_input_marks_the_pty_dead(fake_clock: FakeClock) -> None:
    made: list[FakeBridge] = []
    registry = _registry(fake_clock)
    viewer = FakeViewer()
    session, _ = await registry.attach_or_spawn(PtyKey("t"), _spawner(made))
    await session.attach(viewer, redraw=False)
    made[0].stall()
    assert not await session.write(viewer, b"x")
    assert not session.alive
    _, created = await registry.attach_or_spawn(PtyKey("t"), _spawner(made))
    assert created and len(made) == 2
    await registry.close_all()


@aio
async def test_resize_only_from_the_viewer(fake_clock: FakeClock) -> None:
    made: list[FakeBridge] = []
    registry = _registry(fake_clock)
    viewer, other = FakeViewer(), FakeViewer("other")
    session, _ = await registry.attach_or_spawn(PtyKey("t"), _spawner(made))
    await session.attach(viewer, redraw=False)
    session.resize(other, TermSize(10, 10))
    session.resize(viewer, TermSize(100, 30))
    assert made[0].sizes == [TermSize(100, 30)]
    await registry.close_all()


@aio
async def test_seventeenth_pty_evicts_the_oldest_detached(fake_clock: FakeClock) -> None:
    made: list[FakeBridge] = []
    registry = _registry(fake_clock)
    viewers: list[FakeViewer] = []
    for number in range(MAX_SESSIONS):
        session, _ = await registry.attach_or_spawn(PtyKey(f"t{number}"), _spawner(made))
        viewer = FakeViewer(f"v{number}")
        viewers.append(viewer)
        await session.attach(viewer, redraw=False)
    assert len(registry) == MAX_SESSIONS == 16
    with pytest.raises(RegistryFull) as full:
        await registry.attach_or_spawn(PtyKey("t16"), _spawner(made))
    assert "close one" in str(full.value)
    assert len(made) == MAX_SESSIONS  # nothing was spawned

    registry.detach(PtyKey("t3"), viewers[3])
    fake_clock.advance(5)
    registry.detach(PtyKey("t7"), viewers[7])
    _, created = await registry.attach_or_spawn(PtyKey("t16"), _spawner(made))
    assert created
    await fake_clock.run_until_idle()
    assert registry.get(PtyKey("t3")) is None  # detached longest
    assert made[3].closed
    assert registry.get(PtyKey("t7")) is not None
    assert len(registry) == MAX_SESSIONS
    await registry.close_all()
    assert all(bridge.closed for bridge in made)


@aio
async def test_reaper_ends_a_pty_thirty_minutes_after_detach(fake_clock: FakeClock) -> None:
    made: list[FakeBridge] = []
    registry = _registry(fake_clock)
    key = PtyKey("t")
    viewer = FakeViewer()
    session, _ = await registry.attach_or_spawn(key, _spawner(made))
    await session.attach(viewer, redraw=False)
    fake_clock.advance(TTL_S * 2)  # attached time does not count
    assert await registry.reap() == 0
    registry.detach(key, viewer)
    fake_clock.advance(TTL_S - 1)
    assert await registry.reap() == 0
    fake_clock.advance(1)
    assert TTL_S == 1800
    assert await registry.reap() == 1
    assert made[0].closed


@aio
async def test_run_reaper_ticks_on_the_clock(fake_clock: FakeClock) -> None:
    import asyncio

    made: list[FakeBridge] = []
    registry = _registry(fake_clock, ttl=10.0)
    key = PtyKey("t")
    viewer = FakeViewer()
    session, _ = await registry.attach_or_spawn(key, _spawner(made))
    await session.attach(viewer, redraw=False)
    registry.detach(key, viewer)
    task = asyncio.get_running_loop().create_task(registry.run_reaper(interval=60.0))
    await fake_clock.run_until_idle()
    await fake_clock.advance_and_settle(59)
    assert not made[0].closed
    await fake_clock.advance_and_settle(1)
    assert made[0].closed
    task.cancel()
    await registry.close_all()


@aio
async def test_close_others_ends_the_token_s_other_ptys(fake_clock: FakeClock) -> None:
    made: list[FakeBridge] = []
    registry = _registry(fake_clock)
    old_key = PtyKey("tab", "default", "")
    other_tab = PtyKey("tab2", "default", "")
    viewer = FakeViewer()
    old, _ = await registry.attach_or_spawn(old_key, _spawner(made))
    await old.attach(viewer, redraw=False)
    await registry.attach_or_spawn(other_tab, _spawner(made))
    new_key = PtyKey("tab", "work", "")
    await registry.close_others("tab", keep=new_key)
    assert viewer.closes == [CLOSE_SUPERSEDED]
    assert made[0].closed and not made[1].closed
    assert registry.get(old_key) is None and registry.get(other_tab) is not None
    await registry.close_all()


@aio
async def test_orphans_holding_the_resumed_chat_are_ended(fake_clock: FakeClock) -> None:
    made: list[FakeBridge] = []
    lease_pid = 77_001
    leader_pid = 66_001
    registry = _registry(
        fake_clock, ancestors=lambda pid: {leader_pid, 1} if pid == lease_pid else set()
    )

    async def spawn_with(pid: int) -> FakeBridge:
        bridge = FakeBridge(pid)
        made.append(bridge)
        return bridge

    same = PtyKey("old-tab", "default", "s1")
    other_profile = PtyKey("old-tab2", "work", "s1")
    holder = PtyKey("fresh-tab", "default", "")
    watched = PtyKey("busy-tab", "default", "s1")
    await registry.attach_or_spawn(same, lambda: spawn_with(1))
    await registry.attach_or_spawn(other_profile, lambda: spawn_with(2))
    await registry.attach_or_spawn(holder, lambda: spawn_with(leader_pid))
    busy, _ = await registry.attach_or_spawn(watched, lambda: spawn_with(3))
    await busy.attach(FakeViewer(), redraw=False)

    keep = PtyKey("new-tab", "default", "s1")
    await registry.close_orphans(keep, holder_pid=lease_pid)
    assert registry.get(same) is None  # same profile + resume id
    assert registry.get(holder) is None  # holds the lease through a descendant
    assert registry.get(other_profile) is not None  # another profile's s1 is another chat
    assert registry.get(watched) is not None  # someone is looking at it
    await registry.close_orphans(PtyKey("x", "default", ""), holder_pid=lease_pid)  # no resume
    assert registry.get(other_profile) is not None
    await registry.close_all()


class FakeDaemon:
    """Records what reaches the daemon from a TUI viewer: a reap may detach, never interrupt."""

    def __init__(self) -> None:
        self.turn_running = True
        self.interrupts = 0
        self.viewers = 1

    def detach(self) -> None:
        self.viewers -= 1

    def interrupt(self) -> None:
        self.interrupts += 1
        self.turn_running = False


class ViewerTui(FakeBridge):
    """A TUI attached to the daemon: ending it only detaches its viewer."""

    def __init__(self, daemon: FakeDaemon) -> None:
        super().__init__()
        self.daemon = daemon

    async def close(self) -> None:
        if not self.closed:
            self.daemon.detach()
        await super().close()


@aio
async def test_reaping_a_tui_leaves_the_daemon_turn_running(fake_clock: FakeClock) -> None:
    daemon = FakeDaemon()
    registry = _registry(fake_clock)
    key = PtyKey("t", "default", "s1")

    async def spawn() -> FakeBridge:
        return ViewerTui(daemon)

    viewer = FakeViewer()
    session, _ = await registry.attach_or_spawn(key, spawn)
    await session.attach(viewer, redraw=False)
    registry.detach(key, viewer)
    fake_clock.advance(TTL_S)
    assert await registry.reap() == 1
    assert daemon.viewers == 0
    assert daemon.interrupts == 0 and daemon.turn_running


@aio
async def test_one_pty_per_key_under_racing_attaches(fake_clock: FakeClock) -> None:
    import asyncio

    made: list[FakeBridge] = []
    registry = _registry(fake_clock)
    key = PtyKey("t")
    results = await asyncio.gather(
        *(registry.attach_or_spawn(key, _spawner(made)) for _ in range(5))
    )
    assert len(made) == 1
    assert len({id(session) for session, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    await registry.close_all()


def test_process_ancestors_reads_the_parent_chain() -> None:
    import os
    import sys

    if not sys.platform.startswith("linux"):
        assert process_ancestors(os.getpid()) == set()
        return
    found = process_ancestors(os.getpid())
    assert os.getppid() in found
    assert process_ancestors(2**31 - 1) == set()
