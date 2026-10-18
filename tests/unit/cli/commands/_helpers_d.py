"""Shared bits for the slice-D command tests: run a command like the process does, seed a store.

The fixtures ``fake_clock`` (a self-advancing fake clock) and ``store_globals`` (SQLite version
pinned outside the WAL-reset bug range, every handle closed afterwards) are imported by each test
module that needs them, so other command tests keep the plain fixtures.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from click.testing import CliRunner

from indus.cli import app as app_mod
from indus.cli import prompts
from indus.cli.args import CliState
from indus.foundation.scope import Scope
from indus.state import guards, journal, readpool, registry
from indus.state.registry import StoreHandle
from tests.conftest import FAKE_CLOCK_START
from tests.fakes.prompts import ScriptedDriver
from tests.unit.state._helpers_d import DAY, add_message, add_session
from tests.unit.state.conftest import PINNED_SQLITE, AutoClock

__all__ = ["DAY", "Result", "aged", "fake_clock", "run", "seeded", "store_globals"]


@pytest.fixture
def fake_clock() -> AutoClock:
    return AutoClock(FAKE_CLOCK_START)


@pytest.fixture(autouse=True)
def store_globals(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(journal, "linked_sqlite_version", lambda: PINNED_SQLITE)
    journal.reset_once_logs()
    guards.reset_health()
    readpool.reset_fd_cache()
    yield
    registry.close_all()
    guards.reset_health()
    journal.reset_once_logs()
    readpool.reset_fd_cache()


@dataclass
class Result:
    code: int
    out: str
    err: str


def run(scope: Scope, *argv: str, tty: bool = False, input: str | None = None) -> Result:
    """``indus ARGV`` through the app's dispatch, with stdin/stdout terminals as ``tty`` says."""
    state = CliState(scope=scope, stdin_isatty=lambda: tty, stdout_isatty=lambda: tty)
    runner = CliRunner()
    saved = prompts.io_factory
    if tty and input is not None:
        lines = input.split("\n")[:-1] if input.endswith("\n") else input.split("\n")

        def scripted_io(_state: CliState, guidance: str) -> prompts.Interactive:
            return prompts.Interactive(
                tty=True, guidance=guidance, driver=ScriptedDriver(list(lines))
            )

        prompts.io_factory = scripted_io
    try:
        with runner.isolation(input=input) as (out, err, _mixed):
            code = app_mod.dispatch(list(argv), state)
    finally:
        prompts.io_factory = saved
    return Result(code, out.getvalue().decode(), err.getvalue().decode())


@contextmanager
def seeded(scope: Scope) -> Any:
    """A private writable handle for seeding; closed afterwards so the command opens its own."""
    handle = registry.open_unshared(scope)
    try:
        yield handle
    finally:
        handle.close()


def aged(
    store: StoreHandle,
    sid: str,
    days: float,
    *,
    messages: int = 1,
    content: object = "hello there",
    ended: bool = True,
    **columns: Any,
) -> None:
    """A session started ``days`` ago, its messages that old too; ended unless ``ended=False``."""
    now = store.scope.clock.now().timestamp()
    at = now - days * DAY
    if ended:
        columns.setdefault("end_reason", "cli_close")
        columns.setdefault("ended_at", at)
    columns.setdefault("message_count", messages)
    add_session(store, sid, started_at=at, **columns)
    for _ in range(messages):
        add_message(store, sid, content, timestamp=at)


Seeder = Callable[[StoreHandle], None]
