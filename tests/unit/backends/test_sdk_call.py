"""Deadlines, retries and the shell guard around vendor SDK calls (P21-02, R02.30)."""

from __future__ import annotations

import asyncio
import shlex
import shutil
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from indus.backends.contract import BackendError
from indus.backends.sdk_call import (
    CREATE,
    CREATE_BY_NAME,
    EXEC_GRACE_S,
    GET,
    STOP,
    BackendDeadline,
    backoff_delay,
    exec_operation,
    guarded_argv,
    guarded_command,
    is_transient,
    sdk_call,
    status_of,
)
from indus.foundation.blocking import BlockingPool
from tests.fakes.backend.fake_modal import FakeGrpcStatus
from tests.fakes.clock import FakeClock, checkpoint
from tests.unit.backends.conftest import drive

START = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)


class VendorError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"vendor said {status_code}")
        self.status_code = status_code


class ResponseShaped(Exception):
    class _Response:
        def __init__(self, code: int) -> None:
            self.status_code = code

    def __init__(self, code: int) -> None:
        super().__init__("http error")
        self.response = self._Response(code)


class GrpcShaped(Exception):
    def __init__(self, status: FakeGrpcStatus) -> None:
        super().__init__(status.name)
        self.status = status


class SandboxConnectionError(Exception):
    """Named like vendor connection errors (daytona, modal)."""


@pytest.fixture
def pool() -> Iterator[BlockingPool]:
    own = BlockingPool("sdk-test", 4, queue_limit=4)
    yield own
    own.shutdown()


class Script:
    """A fake vendor call: raises the queued errors in order, then returns ``value``."""

    def __init__(self, *errors: BaseException, value: str = "ok") -> None:
        self.errors = list(errors)
        self.value = value
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.value


def test_never_returning_call_hits_the_deadline_and_cancels_once(pool: BlockingPool) -> None:
    clock = FakeClock(START)
    release = threading.Event()
    cancelled: list[str] = []
    cancel_seen = threading.Event()

    def stuck() -> str:
        release.wait(30)
        return "late"

    def cancel() -> None:
        cancelled.append("terminate")
        cancel_seen.set()

    op = exec_operation(20)
    assert op.deadline_s == 20 + EXEC_GRACE_S and not op.idempotent

    async def go() -> BaseException:
        task = asyncio.ensure_future(sdk_call(op, stuck, cancel=cancel, clock=clock, pool=pool))
        await asyncio.wait({task}, timeout=0.05)
        clock.advance(29.9)
        await asyncio.wait({task}, timeout=0.05)
        assert not task.done()
        clock.advance(0.1)
        await asyncio.wait({task}, timeout=2)
        assert task.done()
        error = task.exception()
        assert error is not None
        return error

    try:
        error = asyncio.run(go())
        assert isinstance(error, BackendDeadline)
        assert error.operation == "exec" and error.deadline_s == 30
        assert error.code == "backend.deadline"
        assert cancel_seen.wait(5)
        assert cancelled == ["terminate"]
    finally:
        release.set()


def test_cancel_that_raises_is_swallowed(pool: BlockingPool) -> None:
    clock = FakeClock(START)
    release = threading.Event()
    tried = threading.Event()

    def cancel() -> None:
        tried.set()
        raise RuntimeError("vendor down")

    async def go() -> None:
        with pytest.raises(BackendDeadline):
            await drive(
                clock,
                sdk_call(STOP, lambda: release.wait(30), cancel=cancel, clock=clock, pool=pool),
                step=1.0,
            )

    try:
        asyncio.run(go())
        assert tried.wait(5)
    finally:
        release.set()


def test_transient_twice_then_success(pool: BlockingPool) -> None:
    clock = FakeClock(START)
    script = Script(VendorError(503), VendorError(503), value="sandbox-1")
    slept: list[float] = []

    async def sleeper(seconds: float) -> None:
        slept.append(seconds)

    result = asyncio.run(
        sdk_call(
            CREATE_BY_NAME, script, clock=clock, sleeper=sleeper, jitter=lambda: 1.0, pool=pool
        )
    )
    assert result == "sandbox-1" and script.calls == 3
    assert slept == [0.5, 1.0]


def test_retries_stop_after_three_attempts(pool: BlockingPool) -> None:
    script = Script(VendorError(429), VendorError(502), VendorError(500))

    async def sleeper(seconds: float) -> None:
        return None

    with pytest.raises(VendorError) as caught:
        asyncio.run(sdk_call(GET, script, sleeper=sleeper, jitter=lambda: 0.0, pool=pool))
    assert caught.value.status_code == 500 and script.calls == 3


def test_exec_never_retried(pool: BlockingPool) -> None:
    script = Script(VendorError(503))
    with pytest.raises(VendorError):
        asyncio.run(sdk_call(exec_operation(5), script, pool=pool))
    assert script.calls == 1


def test_non_idempotent_create_not_retried(pool: BlockingPool) -> None:
    script = Script(VendorError(503))
    with pytest.raises(VendorError):
        asyncio.run(sdk_call(CREATE, script, pool=pool))
    assert script.calls == 1


def test_client_error_not_retried(pool: BlockingPool) -> None:
    script = Script(VendorError(400))
    with pytest.raises(VendorError):
        asyncio.run(sdk_call(GET, script, pool=pool))
    assert script.calls == 1


def test_deadline_miss_is_not_retried(pool: BlockingPool) -> None:
    clock = FakeClock(START)
    release = threading.Event()
    calls: list[int] = []

    def stuck() -> None:
        calls.append(1)
        release.wait(30)

    async def go() -> None:
        with pytest.raises(BackendDeadline):
            await drive(clock, sdk_call(replace(GET, deadline_s=2), stuck, clock=clock, pool=pool))

    try:
        asyncio.run(go())
        assert calls == [1]
    finally:
        release.set()


def test_pool_full_is_a_backend_error() -> None:
    tiny = BlockingPool("sdk-tiny", 1, queue_limit=0)
    release = threading.Event()

    async def go() -> None:
        first = asyncio.ensure_future(sdk_call(GET, lambda: release.wait(30), pool=tiny))
        await checkpoint()  # let it take the worker
        with pytest.raises(BackendError) as caught:
            await sdk_call(GET, lambda: "x", pool=tiny)
        assert "could not start" in str(caught.value)
        release.set()
        await first

    try:
        asyncio.run(go())
    finally:
        release.set()
        tiny.shutdown()


@pytest.mark.parametrize(
    ("status", "transient"),
    [(408, True), (425, True), (429, True), (500, True), (503, True), (599, True)]
    + [(400, False), (401, False), (404, False), (409, False), (499, False)],
)
def test_transient_status_table(status: int, transient: bool) -> None:
    assert is_transient(VendorError(status)) is transient


def test_status_in_chain() -> None:
    try:
        try:
            raise ResponseShaped(504)
        except ResponseShaped as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        assert status_of(outer) == 504 and is_transient(outer)
    try:
        try:
            raise VendorError(429)
        except VendorError:
            raise ValueError("context only")  # noqa: B904 - implicit context on purpose
    except ValueError as outer:
        assert status_of(outer) == 429 and is_transient(outer)


def test_grpc_status_names() -> None:
    assert status_of(GrpcShaped(FakeGrpcStatus.UNAVAILABLE)) == 503
    assert is_transient(GrpcShaped(FakeGrpcStatus.RESOURCE_EXHAUSTED))
    assert not is_transient(GrpcShaped(FakeGrpcStatus.NOT_FOUND))


def test_connection_failures_are_transient() -> None:
    assert is_transient(ConnectionResetError("reset by peer"))
    assert is_transient(SandboxConnectionError("socket closed"))
    assert is_transient(httpx.ReadError("eof"))
    assert is_transient(httpx.RemoteProtocolError("bad frame"))
    assert not is_transient(ValueError("bad argument"))
    assert status_of(ValueError("none")) is None


def test_cyclic_chain_terminates() -> None:
    first = ValueError("a")
    second = ValueError("b")
    first.__context__ = second
    second.__context__ = first
    assert not is_transient(first) and status_of(first) is None


def test_backoff_shape() -> None:
    assert [backoff_delay(n, 1.0) for n in (1, 2, 3, 4, 5)] == [0.5, 1.0, 2.0, 4.0, 4.0]
    assert backoff_delay(1, 0.0) == 0.25


AWKWARD = "echo 'it''s' \"$(printf %s inner)\" $HOME\nprintf '%s\\n' second line; exit 3"


def test_guard_quotes_awkward_commands() -> None:
    argv = guarded_argv(AWKWARD, 12.2)
    assert argv == ["timeout", "-k", "2", "13", "bash", "-c", AWKWARD]
    assert shlex.split(guarded_command(AWKWARD, 12.2)) == argv
    assert guarded_argv("true", 0)[3] == "1"
    assert guarded_argv("true", 5, login=True)[4:6] == ["bash", "-l"]


@pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("timeout") is None, reason="needs bash, timeout"
)
def test_guard_runs_the_command_unchanged() -> None:
    line = guarded_command(AWKWARD, 10)
    done = subprocess.run(  # noqa: S603 - fixed argv
        ["bash", "-c", line],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        env={"PATH": "/usr/bin:/bin", "HOME": "/nowhere"},
    )
    assert done.returncode == 3
    assert done.stdout == "its inner /nowhere\nsecond\nline\n"


@pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("timeout") is None, reason="needs bash, timeout"
)
def test_guard_stops_a_runaway_command() -> None:
    done = subprocess.run(  # noqa: S603 - fixed argv
        ["bash", "-c", guarded_command("sleep 30", 1)],
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert done.returncode == 124
