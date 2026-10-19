"""P15-29: the reconnect watcher (backoff, fatal, needs-attention, pause/resume, supervision),
``/platform`` over it, and the side services wired onto a stand-in daemon."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from indus.foundation.scope import Scope
from indus.gateway.daemon import reconnect as module
from indus.gateway.daemon.reconnect import (
    ConnectOutcome,
    PlatformHealth,
    ReconnectWatcher,
    backoff_s,
)
from indus.gateway.daemon.side_services import attach, attention_after
from indus.service import gateway_control
from tests.fakes.clock import FakeClock
from tests.unit.cli.commands.cmdrun import write_settings
from tests.unit.gateway.core._kit import build_kit, dm
from tests.unit.gateway.daemon.ports import PortsDouble


def run(coro: Awaitable[None]) -> None:
    async def main() -> None:
        await coro

    asyncio.run(main())


def scripted(
    clock: FakeClock, *outcomes: ConnectOutcome | Exception
) -> tuple[Callable[[str], Awaitable[ConnectOutcome]], list[float]]:
    """A connect function answering ``outcomes`` in order (then retryable), recording when."""
    times: list[float] = []
    queue = list(outcomes)

    async def connect(_name: str) -> ConnectOutcome:
        times.append(clock.monotonic())
        answer = queue.pop(0) if queue else ConnectOutcome.retryable("still down")
        if isinstance(answer, Exception):
            raise answer
        return answer

    return connect, times


async def drive(clock: FakeClock, seconds: float, step: float = 5.0) -> None:
    for _ in range(int(seconds / step)):
        await clock.advance_and_settle(step)


def test_backoff_formula() -> None:
    assert [backoff_s(n) for n in range(1, 8)] == [30, 60, 120, 240, 300, 300, 300]
    assert backoff_s(0) == 30


def test_backoff_sequence(fake_clock: FakeClock) -> None:
    async def main() -> None:
        connect, times = scripted(fake_clock)
        watcher = ReconnectWatcher(connect, clock=fake_clock, attention_after=lambda: 0)
        start = fake_clock.monotonic()
        watcher.enqueue("telegram", error="boom", error_code="connect_timeout")
        watcher.start()
        try:
            await drive(fake_clock, 1060)
        finally:
            await watcher.stop()
        gaps = [round(b - a) for a, b in zip([start, *times], times, strict=False)]
        assert gaps == [30, 60, 120, 240, 300, 300]
        health = watcher.get("telegram")
        assert health is not None and health.state == "retrying" and health.attempts == 6
        assert not health.needs_attention  # threshold 0 = never

    run(main())


def test_connected_leaves_the_queue_and_reports(fake_clock: FakeClock) -> None:
    async def main() -> None:
        changes: list[tuple[str, PlatformHealth | None]] = []
        connect, times = scripted(
            fake_clock, ConnectOutcome.retryable("x"), ConnectOutcome.connected()
        )
        watcher = ReconnectWatcher(
            connect, clock=fake_clock, on_change=lambda n, h: changes.append((n, h))
        )
        watcher.enqueue("slack")
        watcher.start()
        try:
            await drive(fake_clock, 100)
        finally:
            await watcher.stop()
        assert len(times) == 2 and watcher.snapshot() == {}
        assert changes[-1] == ("slack", None)
        assert [h.attempts for _, h in changes if h is not None] == [0, 1]

    run(main())


def test_fatal_failure_drops_out(fake_clock: FakeClock) -> None:
    async def main() -> None:
        connect, times = scripted(fake_clock, ConnectOutcome.fatal("token revoked", "unauthorized"))
        watcher = ReconnectWatcher(connect, clock=fake_clock)
        watcher.enqueue("discord")
        watcher.start()
        try:
            await drive(fake_clock, 400)
        finally:
            await watcher.stop()
        assert len(times) == 1
        health = watcher.get("discord")
        assert health is not None and health.state == "fatal"
        assert health.as_status() == {"state": "fatal", "error_code": "unauthorized"}
        assert watcher.queued() == []

    run(main())


def test_non_retryable_at_enqueue_is_never_tried(fake_clock: FakeClock) -> None:
    async def main() -> None:
        connect, times = scripted(fake_clock)
        watcher = ReconnectWatcher(connect, clock=fake_clock)
        watcher.enqueue("telegram", error="token held elsewhere", retryable=False)
        await fake_clock.advance_and_settle(100)
        await watcher.tick()
        assert times == [] and watcher.get("telegram").state == "fatal"  # type: ignore[union-attr]

    run(main())


def test_exceptions_count_as_retryable(fake_clock: FakeClock) -> None:
    async def main() -> None:
        connect, times = scripted(fake_clock, RuntimeError("socket closed"))
        watcher = ReconnectWatcher(connect, clock=fake_clock)
        watcher.enqueue("telegram")
        await fake_clock.advance_and_settle(30)
        await watcher.tick()
        health = watcher.get("telegram")
        assert len(times) == 1 and health is not None
        assert health.state == "retrying" and "socket closed" in health.error
        assert health.next_retry == fake_clock.monotonic() + 60

    run(main())


def test_needs_attention_after_threshold(fake_clock: FakeClock) -> None:
    async def main() -> None:
        threshold = [1800.0]
        changes: list[PlatformHealth | None] = []
        connect, times = scripted(fake_clock)
        watcher = ReconnectWatcher(
            connect,
            clock=fake_clock,
            attention_after=lambda: threshold[0],
            on_change=lambda _n, h: changes.append(h),
        )
        watcher.enqueue("telegram")
        watcher.start()
        try:
            await drive(fake_clock, 1790)
            assert not watcher.get("telegram").needs_attention  # type: ignore[union-attr]
            await drive(fake_clock, 320)
        finally:
            await watcher.stop()
        health = watcher.get("telegram")
        assert health is not None and health.needs_attention and health.state == "retrying"
        assert health.as_status()["needs_attention"] is True
        flags = [h.needs_attention for h in changes if h is not None]
        assert sum(1 for a, b in zip(flags, flags[1:], strict=False) if b and not a) == 1
        assert times[-1] > 1800  # still retrying after the flag

    run(main())


def test_a_platform_lost_at_runtime_is_tried_at_once(fake_clock: FakeClock) -> None:
    async def main() -> None:
        connect, times = scripted(fake_clock, ConnectOutcome.connected())
        watcher = ReconnectWatcher(connect, clock=fake_clock)
        watcher.start()
        try:
            await fake_clock.run_until_idle()
            start = fake_clock.monotonic()
            watcher.enqueue("telegram", error="connection lost", immediate=True)
            await fake_clock.run_until_idle()
            assert times == [start] and watcher.snapshot() == {}
        finally:
            await watcher.stop()

    run(main())


def test_enqueue_revives_a_watcher_that_gave_up(
    fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def main() -> None:
        connect, times = scripted(fake_clock)
        watcher = ReconnectWatcher(connect, clock=fake_clock)
        real_tick = watcher.tick
        broken = [True]

        async def tick() -> None:
            if broken[0]:
                raise RuntimeError("broken")
            await real_tick()

        monkeypatch.setattr(watcher, "tick", tick)
        watcher.start()
        try:
            await fake_clock.run_until_idle()
            await drive(fake_clock, 200, step=1)
            assert watcher.gave_up and not watcher.running
            broken[0] = False
            watcher.enqueue("slack", immediate=True)
            await fake_clock.run_until_idle()
            assert watcher.running and not watcher.gave_up and len(times) == 1
        finally:
            await watcher.stop()

    run(main())


def test_requeue_keeps_the_first_failure_time(fake_clock: FakeClock) -> None:
    connect, _times = scripted(fake_clock)
    watcher = ReconnectWatcher(connect, clock=fake_clock)
    first = watcher.enqueue("telegram", error="a")
    fake_clock.advance(10)
    second = watcher.enqueue("telegram", error="b")
    assert second.queued_at == first.queued_at and second.error == "b"
    watcher.forget("telegram")
    third = watcher.enqueue("telegram")
    assert third.queued_at == fake_clock.monotonic()


def test_pause_holds_and_resume_retries_now(fake_clock: FakeClock) -> None:
    async def main() -> None:
        connect, times = scripted(fake_clock)
        watcher = ReconnectWatcher(connect, clock=fake_clock)
        watcher.enqueue("telegram")
        assert watcher.pause("telegram") and not watcher.pause("telegram")
        watcher.start()
        try:
            await drive(fake_clock, 600)
            assert times == []
            assert watcher.get("telegram").state == "paused"  # type: ignore[union-attr]
            assert watcher.resume("telegram") and not watcher.resume("telegram")
            await fake_clock.advance_and_settle(0)
            await drive(fake_clock, 5)
            assert len(times) == 1
        finally:
            await watcher.stop()
        assert not watcher.pause("unknown") and not watcher.resume("unknown")

    run(main())


def test_watcher_is_respawned_after_a_crash(
    fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def main() -> None:
        connect, _times = scripted(fake_clock)
        watcher = ReconnectWatcher(connect, clock=fake_clock)
        crashes = [0]
        real_tick = watcher.tick

        async def flaky_tick() -> None:
            if crashes[0] < 2:
                crashes[0] += 1
                raise RuntimeError("bug in a pass")
            await real_tick()

        monkeypatch.setattr(watcher, "tick", flaky_tick)
        watcher.start()
        try:
            await fake_clock.run_until_idle()
            await drive(fake_clock, 10, step=1)
            assert watcher.respawns == 2 and watcher.running and not watcher.gave_up
        finally:
            await watcher.stop()

    run(main())


def test_supervision_gives_up_after_five_fast_crashes(
    fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def main() -> None:
        connect, _times = scripted(fake_clock)
        watcher = ReconnectWatcher(connect, clock=fake_clock)

        async def always() -> None:
            raise RuntimeError("broken")

        monkeypatch.setattr(watcher, "tick", always)
        watcher.start()
        try:
            await fake_clock.run_until_idle()
            await drive(fake_clock, 200, step=1)
        finally:
            await watcher.stop()
        assert watcher.gave_up and watcher.respawns == module.RESPAWN_LIMIT

    run(main())


def test_attention_after_reads_the_config_each_time(scope: Scope) -> None:
    assert attention_after(scope) == 1800.0
    write_settings(scope, "agent:\n  reconnect_attention_after: 60\n")
    assert attention_after(scope) == 60.0
    write_settings(scope, "agent:\n  reconnect_attention_after: 0\n")
    assert attention_after(scope) == 0.0


# -- /platform and the side services on the pipeline ----------------------------------------------


def test_platform_command_shows_and_holds_queued_platforms(
    scope: Scope, fake_clock: FakeClock, tmp_path: Path
) -> None:
    async def main() -> None:
        kit = await build_kit(scope, fake_clock, tmp_path)
        daemon = PortsDouble(scope, pipeline=kit.pipeline)
        services = await attach(daemon)
        try:
            assert kit.pipeline.reconnect is services.watcher

            async def say(text: str) -> Any:
                return await kit.pipeline.commands.dispatch_idle(dm(text), kit.key())

            services.platform_failed("slack", "connect timeout", error_code="connect_timeout")
            services.platform_failed("discord", "bad token", retryable=False)
            listed = await say("/platform")
            assert "Connected: telegram" in listed
            assert "Reconnecting: slack (next in 30 s)" in listed
            assert "Down (needs a config fix): discord (bad token)" in listed
            assert (await say("/platform pause slack")).startswith("Stopped trying")
            assert "Reconnect paused: slack" in await say("/platform list")
            assert await say("/platform resume slack") == "Trying to reconnect slack now."
            await kit.until(lambda: daemon.connects == ["slack"])
            name, first = daemon.statuses[0]
            assert name == "slack" and first is not None
            assert first["state"] == "retrying" and first["error_code"] == "connect_timeout"
            assert "retrying_since" in first
            assert ("discord", {"state": "fatal"}) in daemon.statuses
            assert (await say("/platform pause telegram")).startswith("Paused telegram")
            assert (await say("/platform pause nowhere")) == (
                "No platform called nowhere is connected."
            )
            # The control socket answers for the daemon meanwhile.
            status = await asyncio.to_thread(gateway_control.query, scope.home, "status")
            assert status.ok and status.result is not None
            assert status.result["state"] == "running"
        finally:
            await services.stop()
            await kit.close()
        assert gateway_control.socket_path_for(scope.home) is None

    run(main())


def test_daemon_verbs_are_registered(scope: Scope) -> None:
    async def main() -> None:
        async def drain(params: dict[str, Any]) -> dict[str, Any]:
            return {"draining": True, "timeout": params.get("timeout")}

        daemon = PortsDouble(scope, handlers={"drain": (drain, 5.0)})
        services = await attach(daemon)
        try:
            reply = await asyncio.to_thread(
                gateway_control.query, scope.home, "drain", {"timeout": 3}
            )
            assert reply.result == {"draining": True, "timeout": 3}
            missing = await asyncio.to_thread(gateway_control.query, scope.home, "reload-plugins")
            assert missing.error == "unsupported" and "drain" in missing.verbs
        finally:
            await services.stop()

    run(main())
