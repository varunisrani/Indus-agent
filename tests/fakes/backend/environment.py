"""A fake terminal ``Environment`` for the contract suite and tool tests (P05-04).

It runs no shell. ``FakeEnvironment`` keeps the shared execute flow of every shell backend
(``SpawnedEnvironment``: wait loop, backstop, capture, exit fence, marker parsing) and replaces
only the process: a ``FakeProcess`` that prints scripted output, optionally followed by the cwd
marker the real wrapper would print, and either exits or hangs until it is killed.

Commands are answered by explicit rules (``script``) first, then by a few built-in shapes that
mirror what bash prints: ``printf %s TEXT``, ``echo TEXT``, ``cd DIR``, ``exit N``, ``sleep N``
(hangs), ``cat`` (echoes stdin), ``yes-chars N`` (N times ``x``).
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from indus.backends.contract import BackendFlags, EnvironmentConnectionError
from indus.backends.spawned import SpawnedEnvironment
from indus.backends.wait import ProcessHandle
from indus.foundation.clock import Clock

KILLED_RC = -15


@dataclass
class Rule:
    output: str = ""
    returncode: int = 0
    hang: bool = False  # runs until killed
    marker: bool = True  # print the cwd marker after the output (a killed command never does)
    cwd: str | None = None  # the directory the command ends in (default: where it started)
    echo_stdin: bool = False
    connection_error: str | None = None
    wedge_reader: bool = False  # the output reader blocks for ever (ignores its stop flag)
    recreate: bool = False  # the sandbox was replaced while serving this command
    stdin_error: str | None = None


class FakeProcess:
    """A scripted process (meets ``indus.backends.wait.ProcessHandle``)."""

    _next_pid = 40000

    def __init__(self, chunks: list[str], returncode: int, *, hang: bool = False) -> None:
        FakeProcess._next_pid += 1
        self._pid = FakeProcess._next_pid
        self.chunks = chunks
        self._returncode = returncode
        self.hang = hang
        self.finished = not hang
        self.killed = 0
        self.force_killed = 0
        self.wedge_reader = False
        self.release_reader = threading.Event()
        self._stdin_task: asyncio.Future[str | None] | None = None

    @property
    def pid(self) -> int | None:
        return self._pid

    def poll(self) -> int | None:
        return self._returncode if self.finished else None

    def finish(self, returncode: int | None = None) -> None:
        if returncode is not None:
            self._returncode = returncode
        self.finished = True

    def kill(self) -> None:
        self.killed += 1
        if not self.finished:
            self._returncode = KILLED_RC
            self.finished = True

    def drain(self, sink: Callable[[str], None], stop: threading.Event) -> None:
        for chunk in self.chunks:
            sink(chunk)
        if self.wedge_reader:
            self.release_reader.wait(30)

    @property
    def stdin_task(self) -> asyncio.Future[str | None] | None:
        return self._stdin_task

    def set_stdin_outcome(self, error: str | None) -> None:
        future: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
        future.set_result(error)
        self._stdin_task = future


@dataclass
class SpawnCall:
    command: str
    login: bool
    stdin: str | None


_PRINTF = re.compile(r"^printf %s (?P<text>.*)$", re.S)
_ECHO = re.compile(r"^echo (?P<text>.*)$", re.S)
_CD = re.compile(r"^cd (?P<dir>.+)$")
_EXIT = re.compile(r"^exit (?P<code>\d+)$")
_SLEEP = re.compile(r"^sleep \d+(\.\d+)?$")
_CHARS = re.compile(r"^yes-chars (?P<count>\d+)$")


class FakeEnvironment(SpawnedEnvironment):
    """See the module notes. ``rules`` maps exact command text to a ``Rule``."""

    backend_name = "fake"

    def __init__(
        self,
        *,
        cwd: str = "/work",
        timeout: float = 60.0,
        clock: Clock | None = None,
        capture_home: Path | None = None,
        max_chars: int = 50_000,
        flags: BackendFlags | None = None,
    ) -> None:
        super().__init__(
            cwd=cwd,
            timeout=timeout,
            temp_dir="/fake-tmp",
            flags=flags or BackendFlags(isolated=True, container_paths=True),
            clock=clock,
            capture_home=capture_home,
            max_chars=max_chars,
        )
        self.rules: dict[str, Rule] = {}
        self.calls: list[SpawnCall] = []
        self.processes: list[FakeProcess] = []
        self.cleaned = 0
        self.snapshot_ready = True

    def script(self, command: str, rule: Rule) -> Rule:
        self.rules[command] = rule
        return rule

    async def start(self) -> None:  # no shell: nothing to snapshot
        self.snapshot_ready = True

    def _wrap(self, command: str, cwd: str) -> str:
        return f"{cwd}\0{command}"

    def _rule_for(self, command: str, cwd: str) -> Rule:
        if command in self.rules:
            return self.rules[command]
        if m := _PRINTF.match(command):
            return Rule(output=" ".join(shlex.split(m["text"])))
        if m := _ECHO.match(command):
            return Rule(output=" ".join(shlex.split(m["text"])) + "\n")
        if m := _CD.match(command):
            target = shlex.split(m["dir"])[0]
            return Rule(cwd=os.path.normpath(os.path.join(cwd, target)))
        if m := _EXIT.match(command):
            return Rule(returncode=int(m["code"]), marker=False)
        if _SLEEP.match(command):
            return Rule(hang=True)
        if command == "cat":
            return Rule(echo_stdin=True)
        if m := _CHARS.match(command):
            return Rule(output="x" * int(m["count"]))
        raise AssertionError(f"the fake environment has no rule for: {command!r}")

    async def _spawn(self, script: str, *, login: bool, stdin: str | None) -> ProcessHandle:
        cwd, _, command = script.partition("\0")
        self.calls.append(SpawnCall(command, login, stdin))
        rule = self._rule_for(command, cwd)
        if rule.connection_error is not None:
            raise EnvironmentConnectionError(rule.connection_error)
        if rule.recreate:
            self.mark_recreated()
        chunks = [rule.output]
        if rule.echo_stdin and stdin is not None:
            chunks.append(stdin)
        if rule.marker and not rule.hang:
            where = rule.cwd or cwd
            chunks.append(f"\n{self.paths.marker}{where}{self.paths.marker}\n")
        process = FakeProcess(chunks, rule.returncode, hang=rule.hang)
        process.wedge_reader = rule.wedge_reader
        if stdin is not None:
            process.set_stdin_outcome(rule.stdin_error)
        self.processes.append(process)
        return process

    async def _kill(self, handle: ProcessHandle, within: float | None = None) -> None:
        assert isinstance(handle, FakeProcess)
        handle.kill()

    def _force_kill(self, handle: ProcessHandle) -> None:
        assert isinstance(handle, FakeProcess)
        handle.force_killed += 1
        handle.kill()

    async def cleanup(self) -> None:
        self.cleaned += 1
        await super().cleanup()


__all__ = ["FakeEnvironment", "FakeProcess", "Rule", "SpawnCall"]
