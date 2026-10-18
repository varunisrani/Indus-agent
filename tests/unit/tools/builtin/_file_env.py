"""Test helpers for the file tools: a shell environment that is not the host, and a tiny registry.

``ShellEnv`` meets ``indus.backends.contract.Environment`` by running each command with a real
``bash -c`` (stdout and stderr merged, like a remote shell), but its flags say the commands run
somewhere else, so the file tools take the shell protocol instead of the host fast path. ``noise``
is printed before and after every command to stand in for login banners.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from indus.backends.contract import (
    ActivityCallback,
    BackendFlags,
    CancelSignal,
    Environment,
    ExecResult,
)
from indus.foundation.scope import Scope


class ShellEnv:
    name = "testshell"

    def __init__(
        self, cwd: str, *, noise: str = "", inner_noise: str = "", prefix: str = ""
    ) -> None:
        self._cwd = cwd
        self.prefix = prefix
        self.noise = noise
        self.inner_noise = inner_noise
        self.commands: list[str] = []
        self.bounded: list[bool] = []

    @property
    def flags(self) -> BackendFlags:
        return BackendFlags(isolated=True, container_paths=True, host_local=False)

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
        self.commands.append(command)
        self.bounded.append(bounded)
        script = self.prefix + command
        if self.inner_noise:
            script = command.replace("base64 < ", f"printf %s {self.inner_noise!r}; base64 < ", 1)
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            script,
            cwd=cwd or self._cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate(stdin.encode() if stdin is not None else b"")
        text = out.decode("utf-8", errors="replace")
        return ExecResult(output=f"{self.noise}{text}{self.noise}", returncode=proc.returncode or 0)

    async def cleanup(self) -> None:
        return None

    def describe(self) -> str:
        return f"test shell in {self._cwd}"


class FakeRegistry:
    """The slice of ``EnvironmentRegistry`` the file tools use, serving one environment."""

    def __init__(self, env: Environment, backend: str = "testshell") -> None:
        self.env = env
        self.backend = backend
        self.acquired = 0

    def backend_name(self, scope: Scope, overrides: Mapping[str, Any] | None = None) -> str:
        return self.backend

    def key_for(self, scope: Scope, *, task_id: str | None = None, session_id: str | None = None):
        return ("key", session_id or "default")

    def live(self) -> dict[Any, Environment]:
        return {}

    async def acquire(self, scope: Scope | None = None, **_: Any) -> tuple[object, Environment]:
        self.acquired += 1
        return ("key", "default"), self.env
