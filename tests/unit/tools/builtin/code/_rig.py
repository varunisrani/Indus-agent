"""Shared pieces for the ``execute_code`` tests: scopes, a host-local shell env, fake tools.

Every run stages its files under the test's own ``tmp_path`` (``TMPDIR`` in the scope's env), and
``no_stray_processes`` checks that no child the test started is still alive afterwards.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

from indus.backends.contract import BackendFlags
from indus.foundation.clock import Clock
from indus.foundation.scope import Scope
from indus.tools.builtin.code import ExecuteCodeTool, kernel, remote
from indus.tools.registry import ToolRegistry
from tests.fakes.terminal import terminal_scope
from tests.unit.tools.builtin._file_env import FakeRegistry, ShellEnv


class HostEnv(ShellEnv):
    """A shell environment that says it is this machine (the local execute_code path)."""

    name = "local"

    @property
    def flags(self) -> BackendFlags:
        return BackendFlags(host_local=True)


def merged(base: Mapping[str, Any], extra: Mapping[str, Any] | None) -> dict[str, Any]:
    out = {k: dict(v) if isinstance(v, Mapping) else v for k, v in base.items()}
    for key, value in (extra or {}).items():
        if isinstance(value, Mapping) and isinstance(out.get(key), dict):
            out[key] = {**out[key], **value}
        else:
            out[key] = value
    return out


BASE_CONFIG: dict[str, Any] = {
    "approvals": {"mode": "off"},
    "code_execution": {"kernel": "off"},
}


def code_scope(
    tmp_path: Path,
    config: Mapping[str, Any] | None = None,
    *,
    clock: Clock | None = None,
    env: Mapping[str, str] | None = None,
    secrets: Mapping[str, str] | None = None,
) -> Scope:
    scope = terminal_scope(tmp_path, merged(BASE_CONFIG, config), clock=clock, secrets=secrets)
    staging = tmp_path / "t"
    staging.mkdir(exist_ok=True)
    overlay = {"TMPDIR": str(staging), **(env or {})}
    return scope.with_env(scope.env.with_overlay(overlay))


def fake_tools(tmp_path: Path) -> tuple[ToolRegistry, list[tuple[str, dict[str, Any]]]]:
    """A registry with stand-in file/web/terminal tools; returns it and the call log."""
    registry = ToolRegistry()
    seen: list[tuple[str, dict[str, Any]]] = []

    def read_file(args: Mapping[str, Any]) -> str:
        seen.append(("read_file", dict(args)))
        path = Path(str(args.get("path")))
        if not path.is_file():
            return json.dumps({"error": f"no such file: {path}"})
        return json.dumps({"content": path.read_text(), "total_lines": 1})

    def write_file(args: Mapping[str, Any]) -> str:
        seen.append(("write_file", dict(args)))
        Path(str(args["path"])).write_text(str(args["content"]))
        return json.dumps({"ok": True})

    def terminal(args: Mapping[str, Any]) -> str:
        seen.append(("terminal", dict(args)))
        return json.dumps({"output": f"ran {args.get('command')}", "exit_code": 0})

    def web_search(args: Mapping[str, Any]) -> str:
        seen.append(("web_search", dict(args)))
        return json.dumps({"results": []})

    def big(args: Mapping[str, Any]) -> str:
        return json.dumps({"content": "x" * int(args.get("size", 0))})

    schema = {"description": "stand-in", "parameters": {"type": "object", "properties": {}}}
    registry.register("read_file", "file", schema, read_file)
    registry.register("write_file", "file", schema, write_file)
    registry.register("terminal", "terminal", schema, terminal)
    registry.register("web_search", "web", schema, web_search)
    registry.register("delegate_task", "delegation", schema, big)
    return registry, seen


def host_registry(cwd: Path) -> FakeRegistry:
    return FakeRegistry(HostEnv(str(cwd)), backend="local")


async def call(
    tool: ExecuteCodeTool,
    scope: Scope,
    code: str,
    *,
    registry: ToolRegistry,
    cwd: Path,
    environments: Any = None,
    **extras: Any,
) -> dict[str, Any]:
    args: dict[str, Any] = {"code": code}
    if extras.pop("reset", False):
        args["reset"] = True
    serving: Any = environments if environments is not None else host_registry(cwd)
    text = await tool(
        args,
        scope=scope,
        session_id=extras.pop("session_id", "s1"),
        turn_id="t1",
        environments=serving,
        tool_registry=registry,
        **extras,
    )
    return json.loads(text)


def run(work: Awaitable[Any]) -> Any:
    return asyncio.run(_with_timeout(work))


async def _with_timeout(work: Awaitable[Any]) -> Any:
    return await asyncio.wait_for(work, 120)


def live_children() -> set[int]:
    """Processes whose parent is this test process (the kernel and script children)."""
    me = os.getpid()
    found: set[int] = set()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as handle:
                fields = handle.read().rsplit(")", 1)[1].split()
        except OSError:
            continue
        if int(fields[1]) == me and fields[0] != "Z":
            found.add(int(entry))
    return found


def shutdown_pools() -> None:
    asyncio.run(kernel.POOL.shutdown())
    asyncio.run(remote.POOL.shutdown())


def no_stray(before: set[int]) -> Iterator[None]:
    yield
    shutdown_pools()
    leftover = live_children() - before
    assert not leftover, f"processes left running: {sorted(leftover)}"


Maker = Callable[..., Awaitable[dict[str, Any]]]
