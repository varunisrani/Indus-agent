"""Fakes for ``indus update`` tests (P23-17..19): uv, exec, the gateway, and a whole wiring.

``FakeUv`` stands in for every child the apply step runs: ``uv venv`` makes the directory,
``uv sync`` copies the checkout's ``uv.lock`` into the new venv (or fails on a lock carrying
``BROKEN_LOCK_MARK``, or raises ``KeyboardInterrupt`` when asked), and the venv's ``python -m indus
--version`` answers. ``ExecSpy`` records the hand-off instead of replacing the process.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from indus.cli.commands import update as update_mod
from indus.cli.update.apply import ApplyHooks
from indus.cli.update.finish import DistBuilder, RestartResult
from indus.foundation.scope import EnvView, Scope, make_scope
from indus.foundation.secrets import open_secret_store
from tests.fakes.clock import FakeClock
from tests.fixtures.update.make_repo import BROKEN_LOCK_MARK, env


def done(code: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, out, err)


@dataclass
class FakeUv:
    interrupt_on: str | None = None
    calls: list[list[str]] = field(default_factory=list[list[str]])

    def __call__(self, argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        args = list(argv)
        self.calls.append(args)
        run_env: Mapping[str, str] = kwargs.get("env") or {}
        cwd = Path(str(kwargs.get("cwd", ".")))
        verb = args[1] if len(args) > 1 else ""
        if self.interrupt_on is not None and verb == self.interrupt_on:
            raise KeyboardInterrupt
        if verb == "venv":
            venv = Path(args[-1])
            (venv / "bin").mkdir(parents=True, exist_ok=True)
            (venv / "bin" / "python").write_text("new interpreter\n", encoding="utf-8")
            return done()
        if verb == "sync":
            lock = (cwd / "uv.lock").read_text(encoding="utf-8")
            if BROKEN_LOCK_MARK in lock:
                return done(1, err="No solution found when resolving dependencies")
            venv = Path(run_env["UV_PROJECT_ENVIRONMENT"])
            (venv / "lock-of").write_text(lock, encoding="utf-8")
            return done()
        if args[1:] == ["-m", "indus", "--version"]:
            return done(0, "Indus Agent 0.0.0\n")
        return done(127, err=f"unexpected child {args}")


@dataclass
class ExecSpy:
    calls: list[tuple[str, list[str], dict[str, str]]] = field(
        default_factory=list[tuple[str, list[str], dict[str, str]]]
    )

    def __call__(self, path: str, argv: Sequence[str], env_map: Mapping[str, str]) -> None:
        self.calls.append((path, list(argv), dict(env_map)))


@dataclass
class FakeGateway:
    result: RestartResult = field(default_factory=lambda: RestartResult("skipped", "not running"))
    restarts: int = 0

    def describe(self) -> dict[str, Any]:
        return {"kind": "gateway", "running": False, "managed_by": "none", "restart": "not running"}

    def restart(self) -> RestartResult:
        self.restarts += 1
        return self.result


@dataclass
class Snapshots:
    fail: bool = False
    modes: list[str] = field(default_factory=list[str])

    def __call__(self, mode: str) -> str:
        self.modes.append(mode)
        if self.fail:
            raise RuntimeError("disk full")
        return f"{mode} snapshot"


def scope_for(root: Path, git_home: Path, clock: FakeClock) -> Scope:
    """The default profile at ``root`` with a git-capable env that reads no user config."""
    root.mkdir(parents=True, exist_ok=True)
    overlay: dict[str, str | None] = {**env(git_home), "INDUS_HOME": str(root)}
    view = EnvView.from_process().with_overlay(overlay)
    scope = make_scope(root, env=view, clock=clock)
    return scope.with_secrets(open_secret_store(root, scope.profile_id, mode="file"))


@dataclass
class World:
    checkout: Path
    uv: FakeUv = field(default_factory=FakeUv)
    execs: ExecSpy = field(default_factory=ExecSpy)
    gateway: FakeGateway = field(default_factory=FakeGateway)
    snapshots: Snapshots = field(default_factory=Snapshots)
    builders: list[DistBuilder] = field(default_factory=list[DistBuilder])
    now: Callable[[], datetime] | None = None
    uv_path: str | None = "uv"
    runs: list[list[str]] = field(default_factory=list[list[str]])

    def wiring(self, scope: Scope, _state: object) -> update_mod.Wiring:
        def run(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            self.runs.append(list(argv))
            return self.uv(argv, **kwargs)

        return update_mod.Wiring(
            package_dir=self.checkout / "src" / "indus",
            run=run,
            hooks=ApplyHooks(snapshot=self.snapshots, uv=self.uv_path, run=run, execv=self.execs),
            gateway=self.gateway,
            builders=self.builders,
            pnpm="pnpm",
            now=self.now or scope.clock.now,
            exists=lambda _p: False,
        )
