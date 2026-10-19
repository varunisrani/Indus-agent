"""Install, start, stop and ask about the gateway's service (P15-28).

Two managers, both per user and never needing root: ``SystemdUser`` (``systemctl --user``, the
unit file under ``$XDG_CONFIG_HOME/systemd/user``) and ``Launchd`` (``launchctl`` in the user's
``gui/<uid>`` domain, the agent under ``~/Library/LaunchAgents``). The tool is looked up on the
caller's ``PATH`` (the scope's env), so tests put a fake ``systemctl`` there.

``install`` writes the definition (refusing to overwrite a different one without ``force``) and
registers it; it does not start the gateway on systemd (``indus gateway start`` does), while
launchd starts an agent as soon as it is loaded (``RunAtLoad``). ``uninstall`` unregisters and
removes the file. Stopping through the manager is the CLI's job after it has recorded the
planned stop (``state.gateway_runtime.record_planned_stop``).
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from indus.foundation.errors import IndusError
from indus.foundation.fsio import atomic_write_text
from indus.platform.locate import which
from indus.service.gateway_units import (
    UnitSpec,
    launch_agents_dir,
    launchd_plist,
    systemd_unit,
    user_unit_dir,
)

__all__ = [
    "Completed",
    "InstallOutcome",
    "Launchd",
    "Runner",
    "ServiceError",
    "ServiceManager",
    "SystemdUser",
    "UnitState",
    "manager_for",
]

_TIMEOUT_S = 30.0


class ServiceError(IndusError):
    default_code = "gateway.service"


@dataclass(frozen=True)
class Completed:
    returncode: int
    stdout: str = ""
    stderr: str = ""


type Runner = Callable[[Sequence[str]], Completed]

type InstallOutcome = Literal["written", "unchanged", "conflict"]


@dataclass(frozen=True)
class UnitState:
    """``active``: ``active`` / ``activating`` / ``deactivating`` / ``inactive`` / ``failed`` /
    ``unknown``."""

    installed: bool
    active: str = "unknown"
    main_pid: int | None = None
    detail: str = ""

    @property
    def running(self) -> bool:
        return self.active in ("active", "activating", "reloading")


class ServiceManager(Protocol):
    kind: str
    tool: str
    spec: UnitSpec

    @property
    def path(self) -> Path: ...

    def available(self) -> bool: ...

    def installed(self) -> bool: ...

    def definition(self) -> str: ...

    def install(self, *, force: bool = False) -> InstallOutcome: ...

    def uninstall(self) -> None: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def restart(self) -> None: ...

    def state(self) -> UnitState: ...


def _default_runner(tool: str, env: Mapping[str, str]) -> Runner:
    def run(args: Sequence[str]) -> Completed:
        binary = which(tool, path=env.get("PATH", ""))
        if binary is None:
            raise ServiceError(f"`{tool}` was not found on PATH")
        try:
            done = subprocess.run(
                [binary, *args],
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_S,
                check=False,
                env=dict(env),
            )
        except subprocess.TimeoutExpired:
            raise ServiceError(f"`{tool} {' '.join(args)}` did not finish in 30 s") from None
        except OSError as exc:
            raise ServiceError(f"`{tool}` could not run: {exc.strerror or exc}") from exc
        return Completed(done.returncode, done.stdout or "", done.stderr or "")

    return run


def _write(path: Path, text: str, force: bool) -> InstallOutcome:
    try:
        current = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = None
    if current == text:
        return "unchanged"
    if current is not None and not force:
        return "conflict"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, text, mode=0o644)
    return "written"


class _Base:
    kind = ""
    tool = ""

    def __init__(
        self, spec: UnitSpec, env: Mapping[str, str], runner: Runner | None = None
    ) -> None:
        self.spec = spec
        self.env = env
        self._runner = runner or _default_runner(self.tool, env)

    def available(self) -> bool:
        return which(self.tool, path=self.env.get("PATH", "")) is not None

    def _run(self, *args: str, check: bool = True) -> Completed:
        done = self._runner(args)
        if check and done.returncode != 0:
            said = (done.stderr or done.stdout).strip()
            raise ServiceError(
                f"`{self.tool} {' '.join(args)}` failed ({done.returncode})"
                + (f": {said}" if said else "")
            )
        return done


class SystemdUser(_Base):
    kind = "systemd"
    tool = "systemctl"

    @property
    def path(self) -> Path:
        return user_unit_dir(self.env) / self.spec.unit_file

    def installed(self) -> bool:
        return self.path.is_file()

    def definition(self) -> str:
        return systemd_unit(self.spec)

    def _ctl(self, *args: str, check: bool = True) -> Completed:
        return self._run("--user", *args, check=check)

    def install(self, *, force: bool = False) -> InstallOutcome:
        outcome = _write(self.path, self.definition(), force)
        if outcome == "conflict":
            return outcome
        self._ctl("daemon-reload")
        self._ctl("enable", self.spec.unit_file)
        return outcome

    def uninstall(self) -> None:
        if self.installed():
            self._ctl("disable", self.spec.unit_file, check=False)
            self.path.unlink()
        self._ctl("daemon-reload", check=False)
        self._ctl("reset-failed", self.spec.unit_file, check=False)

    def start(self) -> None:
        self._ctl("start", self.spec.unit_file)

    def stop(self) -> None:
        self._ctl("stop", self.spec.unit_file)

    def restart(self) -> None:
        self._ctl("restart", self.spec.unit_file)

    def state(self) -> UnitState:
        if not self.installed():
            return UnitState(False)
        try:
            done = self._ctl(
                "show",
                self.spec.unit_file,
                "--no-pager",
                "--property",
                "ActiveState,SubState,MainPID,Result",
                check=False,
            )
        except ServiceError as exc:
            return UnitState(True, detail=exc.message)
        props = dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)
        pid = props.get("MainPID", "").strip()
        sub = props.get("SubState", "").strip()
        result = props.get("Result", "").strip()
        detail = ", ".join(p for p in (sub, result if result != "success" else "") if p)
        return UnitState(
            True,
            active=props.get("ActiveState", "").strip() or "unknown",
            main_pid=int(pid) if pid.isdigit() and int(pid) > 0 else None,
            detail=detail,
        )


class Launchd(_Base):
    kind = "launchd"
    tool = "launchctl"

    def __init__(
        self,
        spec: UnitSpec,
        env: Mapping[str, str],
        runner: Runner | None = None,
        *,
        uid: int | None = None,
    ) -> None:
        super().__init__(spec, env, runner)
        self.uid = os.getuid() if uid is None else uid

    @property
    def path(self) -> Path:
        return launch_agents_dir(self.env) / f"{self.spec.label}.plist"

    @property
    def domain(self) -> str:
        return f"gui/{self.uid}"

    @property
    def target(self) -> str:
        return f"{self.domain}/{self.spec.label}"

    def installed(self) -> bool:
        return self.path.is_file()

    def definition(self) -> str:
        return launchd_plist(self.spec)

    def _loaded(self) -> bool:
        return self._run("print", self.target, check=False).returncode == 0

    def install(self, *, force: bool = False) -> InstallOutcome:
        outcome = _write(self.path, self.definition(), force)
        if outcome == "conflict":
            return outcome
        if self._loaded():
            self._run("bootout", self.target, check=False)
        self._run("bootstrap", self.domain, str(self.path))
        return outcome

    def uninstall(self) -> None:
        if self._loaded():
            self._run("bootout", self.target, check=False)
        if self.installed():
            self.path.unlink()

    def start(self) -> None:
        if not self._loaded():
            self._run("bootstrap", self.domain, str(self.path))
        self._run("kickstart", self.target)

    def stop(self) -> None:
        # A clean exit (0) is not restarted (KeepAlive.SuccessfulExit=false).
        self._run("kill", "SIGTERM", self.target)

    def restart(self) -> None:
        self._run("kickstart", "-k", self.target)

    def state(self) -> UnitState:
        if not self.installed():
            return UnitState(False)
        done = self._run("print", self.target, check=False)
        if done.returncode != 0:
            return UnitState(True, active="inactive", detail="not loaded")
        state, pid = "unknown", None
        for raw in done.stdout.splitlines():
            key, _, value = raw.strip().partition(" = ")
            if key == "state":
                state = "active" if value.strip() == "running" else "inactive"
            elif key == "pid" and value.strip().isdigit():
                pid = int(value.strip())
        return UnitState(True, active=state, main_pid=pid)


def manager_for(
    spec: UnitSpec,
    env: Mapping[str, str],
    *,
    platform: str,
    runner: Runner | None = None,
) -> ServiceManager | None:
    """The user-level service manager for ``platform`` (``sys.platform``), or None (Windows and
    anything else: no native service in 1.0, D-16.55)."""
    if platform.startswith("linux"):
        return SystemdUser(spec, env, runner)
    if platform == "darwin":
        return Launchd(spec, env, runner)
    return None
