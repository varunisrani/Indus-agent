"""``indus uninstall``: remove what the installer added, and nothing else (P23-20).

Levels (the table in ``docs/internals/uninstall-levels.md``, which the desktop's Settings page,
P24-34, drives through these flags):

- **keep data** (default, ``-y``): the gateway service of this home, the marked PATH block in every
  shell rc file that holds one, the Windows user PATH entry, the ``indus`` launcher, the Start
  menu entry, the code checkout and the installer's tools (``<root>/tools``). The home stays.
- **full** (``--full``): keep data, then this home's keychain entries, the desktop app's user data
  and the home itself. From the default profile, named profiles go only when the person says so
  in the interactive run; ``-y`` never removes them (their folder ``<root>/profiles`` is kept).
- **gui** (``--gui``): the desktop app only: its built output in the checkout, the Start menu
  entry, its user data. The agent stays.
- **data** (``--data``): this home's data only (keychain entries too); code, the installer's tools
  and other profiles stay. Allowed when another manager owns the install.

``--gui-summary`` prints what the desktop app has installed as JSON; ``--dry-run`` prints the plan
of the chosen level and changes nothing.

Ownership: code is removed only from a git checkout. A Nix store path, a container, the desktop
app's bundled runtime, or a tree without ``.git`` belongs to another manager: every level that
removes code is refused with that manager's instruction (``--data`` still works).

Interactive (a terminal on stdin and stdout, no ``-y``): the current install is shown, then
1 keep data (default) / 2 full / 3 cancel; full from the default profile asks about the named
profiles (default no); the last question needs ``yes`` typed. Ctrl-C or end of input prints
"Cancelled." and changes nothing. Without a terminal and without ``-y`` the command says how to run
it and exits 1.

Rules that keep it from removing what is not ours: the PATH block goes only between the exact
marker lines (an opening marker without its closing one leaves the file alone); a launcher goes
only when it points into the checkout; a Start menu entry only when it names the checkout; a
running gateway is stopped through its service, or by SIGTERM to the pid its control socket or
status file names (after the planned-stop record), never by a name pattern.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import shutil
import signal
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import click

__all__ = [
    "LEVELS",
    "PATH_BLOCK_END",
    "PATH_BLOCK_START",
    "Machine",
    "Options",
    "Plan",
    "build_plan",
    "desktop_userdata_dir",
    "gui_summary",
    "ownership_refusal",
    "remove_path_block",
    "run",
    "uninstall_command",
]

LEVELS = ("keep-data", "full", "gui", "data")
PATH_BLOCK_START = "# >>> indus >>>"
PATH_BLOCK_END = "# <<< indus <<<"
DESKTOP_APP_NAME = "Indus"
LAUNCHER_NAME = "indus"
LAUNCHER_MARK = "# indus launcher"
"""The comment line both installers write into the launcher they publish."""
WINDOWS_LAUNCHER = "indus.cmd"
SHORTCUT_NAME = "Indus.lnk"
TOOLS_DIR = "tools"
GATEWAY_STOP_WAIT_S = 30.0
_POLL_S = 0.2

MANAGED_KINDS: Mapping[str, str] = {
    "nix": "Nix owns this install: remove it from your Nix profile or configuration instead.",
    "docker": "This copy runs in a container: remove the container and its image instead.",
    "desktop": (
        "The desktop app owns this runtime: uninstall it from the app (Settings, About) or from "
        "the system's app list instead."
    ),
}
"""Install kinds another manager owns (``indus.cli.update.detect_kind`` words, the detector
``indus update`` uses: it also knows the desktop's bundled runtime and a clone in a dev
container)."""

DATA_KEEP = ("profiles", TOOLS_DIR, "bin", ".indus-bootstrap-complete")
"""Entries ``--data`` leaves in the home: other profiles and what the installer put there."""

_CANCEL_WORDS = frozenset({"3", "c", "cancel", "q", "quit", "n", "no"})
_SETTINGS = {"help_option_names": ["-h", "--help"], "max_content_width": 100}


# -- the outside world ----------------------------------------------------------------------------


class ServiceLike(Protocol):
    """The part of ``indus.service.gateway_manager.ServiceManager`` the uninstaller uses."""

    @property
    def path(self) -> Path: ...

    def installed(self) -> bool: ...

    def stop(self) -> None: ...

    def uninstall(self) -> None: ...

    def state(self) -> Any: ...


class SecretsLike(Protocol):
    def names(self) -> list[str]: ...

    def delete(self, name: str) -> bool: ...


class WindowsUserPath(Protocol):
    """The user-scope PATH in the Windows registry (``HKCU\\Environment``)."""

    def read(self) -> str | None: ...

    def write(self, value: str) -> None: ...


@dataclass(frozen=True)
class LiveGateway:
    pid: int
    start_time: int | None


@dataclass
class Machine:
    """Everything the uninstaller reads or changes outside its own logic. ``from_scope`` builds the
    real one; tests build their own with temp folders and fakes."""

    env: Mapping[str, str]
    platform: str
    home: Path
    root: Path
    profile_id: str
    code_dir: Path
    install_kind: str
    user_home: Path
    service_for: Callable[[Path], ServiceLike | None]
    secrets_for: Callable[[Path, str], SecretsLike | None]
    gateway_live: Callable[[Path], LiveGateway | None]
    stop_pid: Callable[[LiveGateway, float], bool]
    windows_path: WindowsUserPath | None = None
    start_menu: Path | None = None
    profiles: Callable[[], list[tuple[str, Path]]] | None = None
    aliases_for: Callable[[str], list[Path]] | None = None
    """The alias commands (``indus profile alias``) that start a named profile."""

    @property
    def windows(self) -> bool:
        return self.platform == "win32"

    @property
    def is_default_profile(self) -> bool:
        return self.profile_id == "default"

    def named_profiles(self) -> list[tuple[str, Path]]:
        if not self.is_default_profile:
            return []
        if self.profiles is not None:
            return self.profiles()
        from indus.foundation.profiles import list_profiles, profile_home

        return [(pid, profile_home(self.root, pid)) for pid in list_profiles(self.root)[1:]]

    @classmethod
    def from_scope(cls, scope: Any) -> Machine:
        from indus.cli.update import detect_kind
        from indus.cli.version import install_dir

        env: Mapping[str, str] = scope.env
        user = _user_home(env)
        return cls(
            env=env,
            platform=sys.platform,
            home=Path(scope.home),
            root=Path(scope.root),
            profile_id=str(scope.profile_id),
            code_dir=install_dir(),
            install_kind=detect_kind(env=env),
            user_home=user,
            service_for=lambda home: _service_for(home, env),
            secrets_for=lambda home, pid: _secrets_for(scope, home, pid),
            gateway_live=_gateway_live,
            stop_pid=_stop_pid,
            windows_path=_RegistryUserPath() if sys.platform == "win32" else None,
            start_menu=_start_menu(env) if sys.platform == "win32" else None,
            aliases_for=lambda pid: _aliases_for(env, pid),
        )


def _user_home(env: Mapping[str, str]) -> Path:
    for name in ("HOME", "USERPROFILE"):
        raw = env.get(name, "")
        if raw and os.path.isabs(raw):
            return Path(raw)
    return Path.home()


def _aliases_for(env: Mapping[str, str], profile_id: str) -> list[Path]:
    from indus.cli.commands.profile import aliases_for

    return aliases_for(env, profile_id)


def _start_menu(env: Mapping[str, str]) -> Path | None:
    appdata = env.get("APPDATA", "")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def _service_for(home: Path, env: Mapping[str, str]) -> ServiceLike | None:
    from indus.service.gateway_manager import manager_for
    from indus.service.gateway_units import UnitSpec

    spec = UnitSpec.build(home, env=env)
    manager = manager_for(spec, env, platform=sys.platform)
    if manager is None or not manager.installed():
        return None
    return manager


def _secrets_for(scope: Any, home: Path, profile_id: str) -> SecretsLike | None:
    from indus.foundation.errors import IndusError
    from indus.foundation.secrets import SecretStore, open_secret_store

    if Path(scope.home) == home and isinstance(scope.secrets, SecretStore):
        return scope.secrets
    try:
        return open_secret_store(home, profile_id, mode="auto", clock=scope.clock)
    except IndusError:
        return None


def _gateway_live(home: Path) -> LiveGateway | None:
    from indus.service import gateway_control
    from indus.state import gateway_runtime

    found = gateway_control.identify(home)
    if found is not None:
        start = found.get("start_time")
        return LiveGateway(int(found["pid"]), start if isinstance(start, int) else None)
    view = gateway_runtime.runtime_view(home)
    if view.liveness == "running" and view.pid is not None:
        return LiveGateway(view.pid, view.start_time)
    return None


def _stop_pid(live: LiveGateway, timeout: float) -> bool:
    """SIGTERM to that one pid, then wait; True when it is gone. Never a pattern, never SIGKILL."""
    from indus.state import gateway_runtime

    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(live.pid, signal.SIGTERM)
    waited = 0.0
    pause = threading.Event()
    while gateway_runtime.alive(live.pid, live.start_time):
        if waited >= timeout:
            return False
        pause.wait(_POLL_S)
        waited += _POLL_S
    return True


class _RegistryUserPath:
    """``HKCU\\Environment`` ``Path``, keeping its value kind (unexpanded ``%VARS%`` survive)."""

    def __init__(self) -> None:
        self._reg: Any = importlib.import_module("winreg")

    def read(self) -> str | None:
        reg = self._reg
        try:
            with reg.OpenKey(reg.HKEY_CURRENT_USER, "Environment") as key:
                value, _kind = reg.QueryValueEx(key, "Path")
        except OSError:
            return None
        return str(value)

    def write(self, value: str) -> None:
        reg = self._reg
        access = reg.KEY_READ | reg.KEY_WRITE
        with reg.OpenKey(reg.HKEY_CURRENT_USER, "Environment", 0, access) as key:
            try:
                kind = reg.QueryValueEx(key, "Path")[1]
            except OSError:
                kind = reg.REG_EXPAND_SZ
            reg.SetValueEx(key, "Path", 0, kind, value)


# -- small helpers ---------------------------------------------------------------------------------


def desktop_userdata_dir(env: Mapping[str, str], platform: str, user_home: Path) -> Path:
    """Where the desktop app keeps its own data (Electron's userData for the app name)."""
    override = env.get("INDUS_DESKTOP_USER_DATA_DIR", "").strip()
    if override and os.path.isabs(override):
        return Path(override)
    if platform == "darwin":
        return user_home / "Library" / "Application Support" / DESKTOP_APP_NAME
    if platform == "win32":
        appdata = env.get("APPDATA", "")
        base = Path(appdata) if appdata else user_home / "AppData" / "Roaming"
        return base / DESKTOP_APP_NAME
    config = env.get("XDG_CONFIG_HOME", "")
    base = Path(config) if config and os.path.isabs(config) else user_home / ".config"
    return base / DESKTOP_APP_NAME


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
    except (ValueError, OSError):
        return False
    return True


def rc_candidates(env: Mapping[str, str], user_home: Path) -> list[Path]:
    """Every shell rc file the installers may have touched."""
    zdot = env.get("ZDOTDIR", "")
    zsh_dir = Path(zdot) if zdot and os.path.isabs(zdot) else user_home
    config = env.get("XDG_CONFIG_HOME", "")
    config_dir = Path(config) if config and os.path.isabs(config) else user_home / ".config"
    found: list[Path] = []
    for path in (
        user_home / ".bashrc",
        user_home / ".bash_profile",
        user_home / ".bash_login",
        user_home / ".profile",
        zsh_dir / ".zshrc",
        zsh_dir / ".zprofile",
        config_dir / "fish" / "config.fish",
    ):
        if path not in found:
            found.append(path)
    return found


def remove_path_block(text: str) -> tuple[str, int] | None:
    """``text`` without its marked PATH blocks, and how many went; None when an opening marker has
    no closing one (the file is then left alone). Only lines equal to the markers count."""
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    removed = 0
    inside = False
    for line in lines:
        bare = line.rstrip("\r\n").rstrip()
        if not inside and bare == PATH_BLOCK_START:
            inside = True
            continue
        if inside:
            if bare == PATH_BLOCK_END:
                inside = False
                removed += 1
            continue
        kept.append(line)
    if inside:
        return None
    return "".join(kept), removed


def _rewrite_rc(path: Path, text: str) -> None:
    from indus.foundation.fsio import atomic_write_text

    target = path.resolve() if path.is_symlink() else path
    atomic_write_text(target, text, preserve_mode=True)


def _launcher_is_ours(path: Path, code_dir: Path) -> bool:
    try:
        if path.is_symlink():
            return _inside(Path(os.path.realpath(path)), code_dir)
        if not path.is_file() or path.stat().st_size > 64 * 1024:
            return False
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if LAUNCHER_MARK not in text and "rem indus launcher" not in text:
        return False
    return str(code_dir) in text or str(code_dir.resolve()) in text


def _shortcut_is_ours(path: Path, code_dir: Path) -> bool:
    try:
        data = path.read_bytes()
    except OSError:
        return False
    needle = str(code_dir)
    return needle.encode("utf-16-le") in data or needle.encode("utf-8", errors="ignore") in data


def _windows_path_entry(root: Path) -> str:
    return str(root / "bin")


def _same_windows_path(left: str, right: str) -> bool:
    return left.rstrip("\\/").lower() == right.rstrip("\\/").lower()


# -- refusal -------------------------------------------------------------------------------------


def ownership_refusal(machine: Machine) -> str | None:
    """Why this command must not remove the code, or None."""
    kind = machine.install_kind
    message = MANAGED_KINDS.get(kind)
    if message is None and not (machine.code_dir / ".git").exists():
        message = (
            f"This copy was not installed by the Indus Agent installer (install kind: {kind}); "
            "remove it the way it was installed."
        )
    if message is None:
        return None
    return f"{message}\nTo remove only this home's data: indus uninstall --data"


def _code_dir_problem(machine: Machine) -> str | None:
    code = machine.code_dir
    if code.parent == code or code.resolve() == machine.user_home.resolve():
        return f"refusing to remove {code}: it is not an install directory"
    if _inside(machine.home, code) or _inside(machine.root, code):
        return (
            f"the Indus home ({machine.home}) lives inside the code directory ({code}); "
            "removing the code would delete your data. Move the home out first, or remove the "
            "folder yourself."
        )
    return None


# -- the plan ------------------------------------------------------------------------------------


@dataclass
class Options:
    level: str = "keep-data"
    yes: bool = False
    dry_run: bool = False
    remove_profiles: bool = False


@dataclass
class Plan:
    level: str
    services: list[Path] = field(default_factory=list[Path])
    """Homes whose gateway (service or process) is stopped and removed."""
    rc_files: list[Path] = field(default_factory=list[Path])
    rc_unterminated: list[Path] = field(default_factory=list[Path])
    windows_path: list[str] = field(default_factory=list[str])
    launchers: list[Path] = field(default_factory=list[Path])
    foreign_launchers: list[Path] = field(default_factory=list[Path])
    shortcuts: list[Path] = field(default_factory=list[Path])
    keychain: list[tuple[Path, str]] = field(default_factory=list[tuple[Path, str]])
    remove: list[Path] = field(default_factory=list[Path])
    """Whole files or folders that go."""
    empty: list[tuple[Path, tuple[str, ...]]] = field(
        default_factory=list[tuple[Path, tuple[str, ...]]]
    )
    """Folders whose entries go, except the named ones."""
    keep: list[Path] = field(default_factory=list[Path])

    def lines(self) -> list[str]:
        out: list[str] = []
        for home in self.services:
            out.append(f"gateway service or process for {home}")
        for path in self.rc_files:
            out.append(f"PATH block in {path}")
        for entry in self.windows_path:
            out.append(f"user PATH entry {entry}")
        for path in [*self.launchers, *self.shortcuts]:
            out.append(str(path))
        for home, pid in self.keychain:
            out.append(f"stored keys of profile {pid} ({home})")
        for path in self.remove:
            out.append(str(path))
        for folder, kept in self.empty:
            out.append(f"everything in {folder} except {', '.join(kept)}")
        return out


def _desktop_built(code_dir: Path) -> Path:
    return code_dir / "apps" / "desktop" / "release"


def _shortcuts(machine: Machine) -> list[Path]:
    if machine.start_menu is None:
        return []
    link = machine.start_menu / SHORTCUT_NAME
    return [link] if link.is_file() and _shortcut_is_ours(link, machine.code_dir) else []


def _launchers(machine: Machine) -> tuple[list[Path], list[Path]]:
    candidates = [machine.user_home / ".local" / "bin" / LAUNCHER_NAME]
    if machine.windows:
        candidates.append(machine.root / "bin" / WINDOWS_LAUNCHER)
    ours: list[Path] = []
    foreign: list[Path] = []
    for path in candidates:
        if not (path.is_file() or path.is_symlink()):
            continue
        (ours if _launcher_is_ours(path, machine.code_dir) else foreign).append(path)
    return ours, foreign


def _rc_files(machine: Machine) -> tuple[list[Path], list[Path]]:
    found: list[Path] = []
    broken: list[Path] = []
    for path in rc_candidates(machine.env, machine.user_home):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if PATH_BLOCK_START not in text:
            continue
        result = remove_path_block(text)
        if result is None:
            broken.append(path)
        elif result[1]:
            found.append(path)
    return found, broken


def _windows_entries(machine: Machine) -> list[str]:
    if machine.windows_path is None:
        return []
    current = machine.windows_path.read() or ""
    wanted = _windows_path_entry(machine.root)
    return [e for e in current.split(";") if e and _same_windows_path(e, wanted)]


def _code_steps(machine: Machine, plan: Plan) -> None:
    plan.services.append(machine.home)
    plan.rc_files, plan.rc_unterminated = _rc_files(machine)
    plan.windows_path = _windows_entries(machine)
    plan.launchers, plan.foreign_launchers = _launchers(machine)
    plan.shortcuts = _shortcuts(machine)
    plan.remove.append(machine.code_dir)
    tools = machine.root / TOOLS_DIR
    if tools.is_dir():
        plan.remove.append(tools)


def build_plan(machine: Machine, options: Options) -> Plan:
    plan = Plan(options.level)
    userdata = desktop_userdata_dir(machine.env, machine.platform, machine.user_home)
    if options.level == "gui":
        built = _desktop_built(machine.code_dir)
        if built.exists():
            plan.remove.append(built)
        plan.shortcuts = _shortcuts(machine)
        if userdata.exists():
            plan.remove.append(userdata)
        plan.keep += [machine.code_dir, machine.home]
        return plan
    if options.level == "data":
        plan.keychain.append((machine.home, machine.profile_id))
        extra: list[str] = []
        if _inside(machine.code_dir, machine.home) and machine.code_dir != machine.home:
            extra.append(machine.code_dir.resolve().relative_to(machine.home.resolve()).parts[0])
        plan.empty.append((machine.home, (*DATA_KEEP, *extra)))
        plan.keep += [machine.code_dir]
        return plan
    _code_steps(machine, plan)
    if options.level == "keep-data":
        plan.keep.append(machine.home)
        if userdata.exists():
            plan.keep.append(userdata)
        return plan
    # full
    if userdata.exists():
        plan.remove.append(userdata)
    plan.keychain.append((machine.home, machine.profile_id))
    named = machine.named_profiles()
    if machine.is_default_profile and named and not options.remove_profiles:
        plan.empty.append((machine.home, ("profiles",)))
        plan.keep += [path for _pid, path in named]
    else:
        for pid, path in named:
            plan.services.append(path)
            plan.keychain.append((path, pid))
            plan.launchers += _aliases(machine, pid)
        if not machine.is_default_profile:
            plan.launchers += _aliases(machine, machine.profile_id)
        plan.remove.append(machine.home)
    return plan


def _aliases(machine: Machine, profile_id: str) -> list[Path]:
    if machine.aliases_for is None:
        return []
    try:
        return machine.aliases_for(profile_id)
    except OSError:
        return []


# -- running the plan ----------------------------------------------------------------------------


@dataclass
class Outcome:
    done: list[str] = field(default_factory=list[str])
    notes: list[str] = field(default_factory=list[str])
    """Things left alone on purpose (not ours, or not safe to touch); they do not fail the run."""
    warnings: list[str] = field(default_factory=list[str])
    """Steps that failed; any of them makes the exit code 1."""

    @property
    def failed(self) -> bool:
        return bool(self.warnings)


def _stop_gateway(machine: Machine, home: Path, outcome: Outcome) -> None:
    from indus.state import gateway_runtime

    live = None
    try:
        live = machine.gateway_live(home)
    except Exception as exc:  # noqa: BLE001 - a broken probe must not stop the uninstall
        outcome.warnings.append(f"could not check the gateway for {home}: {exc}")
    if live is not None:
        with contextlib.suppress(OSError):
            gateway_runtime.record_planned_stop(
                home, live.pid, "uninstall", start_time=live.start_time, by="cli"
            )
    service = None
    try:
        service = machine.service_for(home)
    except Exception as exc:  # noqa: BLE001
        outcome.warnings.append(f"could not look up the gateway service for {home}: {exc}")
    if service is not None:
        try:
            with contextlib.suppress(Exception):
                if service.state().running:
                    service.stop()
            service.uninstall()
            outcome.done.append(f"Removed the gateway service ({service.path})")
        except Exception as exc:  # noqa: BLE001
            outcome.warnings.append(f"could not remove the gateway service {service.path}: {exc}")
        return
    if live is not None:
        if machine.stop_pid(live, GATEWAY_STOP_WAIT_S):
            outcome.done.append(f"Stopped the gateway (pid {live.pid})")
        else:
            outcome.warnings.append(
                f"the gateway (pid {live.pid}) is still running; stop it with `indus gateway stop`"
            )


def _remove_tree(path: Path, outcome: Outcome) -> None:
    if not os.path.lexists(path):
        return
    real_dir = path.is_dir() and not path.is_symlink()
    try:
        shutil.rmtree(path) if real_dir else os.unlink(path)
    except OSError as exc:
        outcome.warnings.append(f"could not remove {path}: {exc}")
        return
    outcome.done.append(f"Removed {path}")


def _empty_folder(folder: Path, keep: Sequence[str], outcome: Outcome) -> None:
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name in keep:
            continue
        _remove_tree(entry, outcome)


def _drop_keychain(machine: Machine, home: Path, pid: str, outcome: Outcome) -> None:
    store = machine.secrets_for(home, pid)
    if store is None:
        return
    count = 0
    try:
        for name in store.names():
            if store.delete(name):
                count += 1
    except Exception as exc:  # noqa: BLE001
        outcome.warnings.append(f"stored keys of profile {pid} could not all be removed: {exc}")
    if count:
        outcome.done.append(f"Removed {count} stored key(s) of profile {pid}")


def execute(machine: Machine, plan: Plan) -> Outcome:
    outcome = Outcome()
    for home in plan.services:
        _stop_gateway(machine, home, outcome)
    for path in plan.rc_files:
        try:
            result = remove_path_block(path.read_text(encoding="utf-8"))
            if result is not None and result[1]:
                _rewrite_rc(path, result[0])
                outcome.done.append(f"Removed the PATH block from {path}")
        except OSError as exc:
            outcome.warnings.append(f"could not update {path}: {exc}")
    for path in plan.rc_unterminated:
        outcome.notes.append(
            f"{path} has `{PATH_BLOCK_START}` without `{PATH_BLOCK_END}`; left it alone"
        )
    if plan.windows_path and machine.windows_path is not None:
        try:
            current = machine.windows_path.read() or ""
            kept = [e for e in current.split(";") if e and e not in plan.windows_path]
            machine.windows_path.write(";".join(kept))
            outcome.done.append("Removed " + ", ".join(plan.windows_path) + " from the user PATH")
        except OSError as exc:
            outcome.warnings.append(f"could not edit the user PATH: {exc}")
    for path in [*plan.launchers, *plan.shortcuts]:
        _remove_tree(path, outcome)
    for path in plan.foreign_launchers:
        outcome.notes.append(f"kept {path}: it does not point into this install")
    for home, pid in plan.keychain:
        _drop_keychain(machine, home, pid, outcome)
    for path in plan.remove:
        _remove_tree(path, outcome)
    for folder, kept in plan.empty:
        _empty_folder(folder, kept, outcome)
    return outcome


# -- the summary for the desktop ------------------------------------------------------------------


def gui_summary(machine: Machine) -> dict[str, Any]:
    built = _desktop_built(machine.code_dir)
    artefacts = sorted(str(p) for p in built.iterdir()) if built.is_dir() else []
    userdata = desktop_userdata_dir(machine.env, machine.platform, machine.user_home)
    shortcuts = [str(p) for p in _shortcuts(machine)]
    return {
        "gui_installed": bool(artefacts or shortcuts or userdata.exists()),
        "built_artifacts": artefacts,
        "shortcuts": shortcuts,
        "userdata_dir": str(userdata),
        "userdata_exists": userdata.exists(),
        "agent_installed": (machine.code_dir / ".git").exists(),
        "install_kind": machine.install_kind,
        "code_dir": str(machine.code_dir),
        "home": str(machine.home),
    }


# -- conversation --------------------------------------------------------------------------------


class Asker(Protocol):
    def __call__(self, prompt: str) -> str | None: ...


def _click_ask(prompt: str) -> str | None:
    try:
        answer = click.prompt(prompt, default="", show_default=False, prompt_suffix="")
    except (click.Abort, EOFError, KeyboardInterrupt):
        click.echo("")
        return None
    return str(answer).strip()


def _cancelled() -> int:
    click.echo("Cancelled.")
    return 0


def _show_install(machine: Machine) -> None:
    click.echo("Current install:")
    click.echo(f"  Code:     {machine.code_dir}")
    click.echo(f"  Config:   {machine.home / 'config.yaml'}")
    store = None
    with contextlib.suppress(Exception):
        store = machine.secrets_for(machine.home, machine.profile_id)
    names: list[str] = []
    if store is not None:
        with contextlib.suppress(Exception):
            names = store.names()
    click.echo(f"  Secrets:  {machine.home / '.env'} and {len(names)} stored key(s)")
    data_dirs = ", ".join(str(machine.home / name) for name in ("sessions", "logs", "cron"))
    click.echo(f"  Data:     {data_dirs}")
    userdata = desktop_userdata_dir(machine.env, machine.platform, machine.user_home)
    if userdata.exists():
        click.echo(f"  Desktop:  {userdata} (kept by 'keep data', removed by 'full')")
    named = machine.named_profiles()
    if named:
        click.echo("Other profiles:")
        for pid, path in named:
            running = ""
            with contextlib.suppress(Exception):
                running = " (gateway running)" if machine.gateway_live(path) is not None else ""
            click.echo(f"  {pid}{running}: {path}")


def _interactive_options(machine: Machine, options: Options, ask: Asker) -> Options | None:
    _show_install(machine)
    click.echo("")
    level = options.level
    if level == "keep-data":
        click.echo("  1) Keep data: remove the code only; settings, keys and sessions stay")
        click.echo("     (default)")
        click.echo("  2) Full: remove everything, including all data")
        click.echo("  3) Cancel")
        while True:
            answer = ask("Choose [1/2/3]: ")
            if answer is None:
                return None
            word = answer.lower()
            if word in _CANCEL_WORDS:
                return None
            if word in ("", "1"):
                level = "keep-data"
                break
            if word == "2":
                level = "full"
                break
            click.echo("Please answer 1, 2 or 3.")
    remove_profiles = False
    named = machine.named_profiles()
    if level == "full" and named:
        names = ", ".join(pid for pid, _path in named)
        click.echo(f"Named profiles are kept unless you say so: {names}")
        answer = ask(f"Also stop and remove these {len(named)} profile(s)? [y/N]: ")
        if answer is None:
            return None
        remove_profiles = answer.lower() in ("y", "yes")
    click.echo("")
    if level == "full":
        click.echo("This permanently deletes the settings, stored keys, sessions, jobs and logs.")
        if remove_profiles:
            click.echo(f"It also removes the profiles: {', '.join(pid for pid, _ in named)}.")
    else:
        click.echo("This removes the Indus Agent code and keeps your settings and data.")
    answer = ask("Type 'yes' to confirm: ")
    if answer is None:
        return None
    if answer.lower() != "yes":
        return None
    return Options(level=level, yes=True, remove_profiles=remove_profiles)


def _confirm_typed(what: str, ask: Asker) -> bool:
    answer = ask(f"Type 'yes' to {what}: ")
    return answer is not None and answer.lower() == "yes"


def _print_outcome(outcome: Outcome) -> None:
    for line in outcome.done:
        click.echo(f"  {line}")
    for line in outcome.notes:
        click.echo(f"  note: {line}")
    for line in outcome.warnings:
        click.echo(f"  warning: {line}", err=True)


def _closing(machine: Machine, plan: Plan) -> None:
    if plan.level == "keep-data":
        click.echo(f"Your settings and data are kept in {machine.home}.")
    if plan.level in ("keep-data", "full"):
        if machine.windows:
            click.echo("Open a new terminal to pick up the changed user PATH.")
        else:
            click.echo("Open a new terminal (or run `exec $SHELL`) to pick up the changed PATH.")


def run(
    machine: Machine,
    options: Options,
    *,
    interactive: bool,
    ask: Asker = _click_ask,
) -> int:
    """The command's logic; returns the exit code."""
    level = options.level
    if level != "data":
        refusal = ownership_refusal(machine)
        if refusal is not None:
            click.echo(refusal, err=True)
            return 1
    if level in ("keep-data", "full"):
        problem = _code_dir_problem(machine)
        if problem is not None:
            click.echo(problem, err=True)
            return 1
    if options.dry_run:
        plan = build_plan(machine, options)
        click.echo(f"Dry run ({plan.level}): nothing is changed. Would remove:")
        for line in plan.lines() or ["nothing"]:
            click.echo(f"  {line}")
        if plan.keep:
            click.echo("Would keep:")
            for path in plan.keep:
                click.echo(f"  {path}")
        if level == "full" and machine.named_profiles() and not options.remove_profiles:
            click.echo("Named profiles are removed only when you say so in the interactive run.")
        return 0
    if not options.yes and not interactive:
        click.echo(
            "No terminal to confirm on. Run `indus uninstall -y` (keep data), "
            "`indus uninstall -y --full`, or `indus uninstall --dry-run` to see the plan.",
            err=True,
        )
        return 1
    if level == "data":
        live = machine.gateway_live(machine.home)
        if live is not None:
            click.echo(
                f"This profile's gateway is running (pid {live.pid}); stop it first: "
                "indus gateway stop",
                err=True,
            )
            return 1
    if not options.yes:
        if level in ("keep-data", "full"):
            chosen = _interactive_options(machine, options, ask)
            if chosen is None:
                return _cancelled()
            options = chosen
        else:
            plan = build_plan(machine, options)
            click.echo("Will remove:")
            for line in plan.lines() or ["nothing"]:
                click.echo(f"  {line}")
            if not _confirm_typed("remove these", ask):
                return _cancelled()
    plan = build_plan(machine, options)
    click.echo("Uninstalling...")
    outcome = execute(machine, plan)
    _print_outcome(outcome)
    _closing(machine, plan)
    return 1 if outcome.failed else 0


# -- the click command ---------------------------------------------------------------------------


def _level(full: bool, gui: bool, data: bool) -> str:
    chosen = [name for name, on in (("full", full), ("gui", gui), ("data", data)) if on]
    if len(chosen) > 1:
        raise click.UsageError("--full, --gui and --data cannot be combined")
    return chosen[0] if chosen else "keep-data"


MACHINE_FACTORY: Callable[[Any], Machine] = Machine.from_scope
"""How the command builds its ``Machine`` from the scope (tests replace it)."""


@click.command(
    "uninstall",
    context_settings=_SETTINGS,
    help="Remove Indus Agent: keep data (default), --full, --gui (desktop app) or --data.",
)
@click.option("-y", "--yes", is_flag=True, help="No questions (keeps data unless --full).")
@click.option(
    "--full", is_flag=True, help="Also remove this home, its stored keys and desktop data."
)
@click.option("--gui", is_flag=True, help="Remove only the desktop app.")
@click.option("--data", is_flag=True, help="Remove only this home's data (code and profiles stay).")
@click.option(
    "--gui-summary", "summary", is_flag=True, help="Print what the desktop app installed, as JSON."
)
@click.option("--dry-run", is_flag=True, help="Print what would be removed and change nothing.")
@click.pass_context
def uninstall_command(
    ctx: click.Context,
    yes: bool,
    full: bool,
    gui: bool,
    data: bool,
    summary: bool,
    dry_run: bool,
) -> int:
    from indus.cli.commands._support import interactive, state_of

    state = state_of(ctx)
    machine = MACHINE_FACTORY(state.scope)
    if summary:
        if full or gui or data or dry_run:
            raise click.UsageError("--gui-summary cannot be combined with other levels")
        click.echo(json.dumps(gui_summary(machine), indent=2))
        return 0
    options = Options(level=_level(full, gui, data), yes=yes, dry_run=dry_run)
    code = run(machine, options, interactive=interactive(state))
    if code:
        raise click.exceptions.Exit(code)
    return 0
