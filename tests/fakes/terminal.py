"""Test helpers for the terminal tool and background processes (P05-11..14).

- ``terminal_scope``: a profile scope with a dict config, fake secrets and the real process env.
- ``ScriptedEnv``: an ``Environment`` answering ``execute`` from a script (no shell, no wait loop).
- ``FakeJob``: a background-job handle the test drives by hand (output, exit, stdin).
- ``Inbox``: a delivery sink that records events and can signal when one arrives.
- ``forge_token``: a credential-shaped value built at run time (never a literal in the source).
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from indus.backends.contract import (
    ActivityCallback,
    BackendFlags,
    CancelSignal,
    ExecResult,
)
from indus.backends.process_notify import ProcessEvent
from indus.foundation.clock import Clock
from indus.foundation.scope import EnvView, Scope, make_scope
from tests.fakes.agent import DictConfig

_RNG = random.Random(1311)
_ALNUM = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def body(n: int) -> str:
    return "".join(_RNG.choice(_ALNUM) for _ in range(n))


def forge_token() -> str:
    return "gh" + "p_" + body(36)


class FakeSecrets:
    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self._values = dict(values or {})

    def get(self, name: str, /) -> str | None:
        return self._values.get(name)

    def known_values(self) -> list[str]:
        return list(self._values.values())

    @property
    def generation(self) -> int:
        return 1


def terminal_scope(
    tmp_path: Path,
    config: Mapping[str, Any] | None = None,
    *,
    secrets: Mapping[str, str] | None = None,
    clock: Clock | None = None,
    name: str = "home",
) -> Scope:
    home = tmp_path / name
    home.mkdir(parents=True, exist_ok=True)
    env = EnvView.from_process().with_overlay({"INDUS_HOME": str(home)})
    scope = make_scope(home, env=env, clock=clock)
    return scope.with_config(DictConfig(dict(config or {}))).with_secrets(FakeSecrets(secrets))


type Answer = ExecResult | BaseException | Callable[[str, str | None], ExecResult]


@dataclass
class ExecCall:
    command: str
    timeout: float | None
    cwd: str | None
    bounded: bool


class ScriptedEnv:
    """An ``Environment`` whose ``execute`` pops answers from ``answers`` (or one default)."""

    def __init__(
        self,
        answers: list[Answer] | None = None,
        *,
        cwd: str = "/work",
        name: str = "scripted",
        flags: BackendFlags | None = None,
    ) -> None:
        self.answers: list[Answer] = list(answers or [])
        self._cwd = cwd
        self._name = name
        self._flags = flags or BackendFlags(host_local=True)
        self.calls: list[ExecCall] = []
        self.cleaned = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def flags(self) -> BackendFlags:
        return self._flags

    @property
    def cwd(self) -> str:
        return self._cwd

    async def execute(
        self,
        command: str,
        *,
        timeout: float | None = None,
        cwd: str | None = None,
        stdin: str | None = None,
        bounded: bool = True,
        on_activity: ActivityCallback | None = None,
        cancel: CancelSignal | None = None,
    ) -> ExecResult:
        self.calls.append(ExecCall(command, timeout, cwd, bounded))
        answer: Answer = (
            self.answers.pop(0) if self.answers else ExecResult(output="", returncode=0)
        )
        if isinstance(answer, BaseException):
            raise answer
        result = answer(command, cwd) if callable(answer) else answer
        if result.cwd_observed and not cwd:
            self._cwd = result.cwd_observed
        return result

    async def cleanup(self) -> None:
        self.cleaned += 1

    def describe(self) -> str:
        return "scripted test environment"

    def run_env(self) -> dict[str, str]:
        """What a background job started here would get (no snapshot)."""
        return {"PATH": "/usr/bin:/bin"}


class FakeJob:
    """A background-job handle the test drives (meets ``processes.JobHandle``)."""

    pty = False
    fd = None

    def __init__(self, pid: int | None = 4242) -> None:
        self._pid = pid
        self.code: int | None = None
        self.written: list[str] = []
        self.input_closed = False
        self.stopped = 0
        self.forced = 0
        self.released = 0

    @property
    def pid(self) -> int | None:
        return self._pid

    def poll(self) -> int | None:
        return self.code

    async def write(self, data: str) -> str | None:
        if self.input_closed:
            return "the process input is closed"
        self.written.append(data)
        return None

    def close_input(self) -> str | None:
        self.input_closed = True
        return None

    async def stop(self, clock: Clock, grace: float) -> None:
        self.stopped += 1
        self.code = -15

    def force_stop(self) -> None:
        self.forced += 1
        self.code = -9

    def release(self) -> None:
        self.released += 1


@dataclass
class Inbox:
    """Records delivered events; ``arrived`` is set on each one (reset it to wait again)."""

    events: list[ProcessEvent] = field(default_factory=list[ProcessEvent])
    arrived: asyncio.Event = field(default_factory=asyncio.Event)

    def __call__(self, event: ProcessEvent) -> None:
        self.events.append(event)
        self.arrived.set()

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]
