"""Service definitions for the gateway: a systemd user unit and a launchd agent (P15-28).

Names: ``indus-gateway.service`` / ``ai.indus.gateway`` for the platform-default home
(``~/.indus``); every other home gets ``-<hash>`` / ``.<hash>`` with the first 8 hex digits of the
sha256 of its absolute path, so two profiles (or a test's temp home) never share a unit.

Exit codes (R06.17): 75 asks the supervisor for a restart, 78 is a fatal config error that must
not restart. systemd: ``Restart=on-failure`` restarts 75 and crashes,
``RestartPreventExitStatus=78`` holds 78, a clean 0 stays stopped. launchd cannot gate on a
status, so the agent runs the gateway under a small ``/bin/sh`` wrapper that forwards
TERM/INT/USR1 to it and maps 78 to 0, and ``KeepAlive.SuccessfulExit=false`` leaves a 0 stopped.

The stop budget (``TimeoutStopSec``) covers the longest drain the daemon may do: the chat-turn
drain or the cron/API drain plus its 10 s cleanup reserve, plus 30 s headroom, never under 60 s.
Nothing here kills unconditionally on stop; ``ExecStop`` only records that the stop was planned.

Everything is a pure function of ``UnitSpec`` so the text can be compared with golden files.
"""

from __future__ import annotations

import hashlib
import os
import plistlib
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from indus.foundation.home import default_home
from indus.foundation.profiles import profile_id_of
from indus.platform import known_dirs
from indus.state.gateway_runtime import stdio_log_path

__all__ = [
    "BASE_LABEL",
    "BASE_NAME",
    "CRON_CLEANUP_RESERVE_S",
    "LAUNCHD_EXIT_TIMEOUT_S",
    "STOP_FLOOR_S",
    "STOP_HEADROOM_S",
    "UnitSpec",
    "default_program",
    "home_hash",
    "launch_agents_dir",
    "launchd_label",
    "launchd_plist",
    "launchd_wrapper",
    "service_name",
    "systemd_quote",
    "systemd_unit",
    "timeout_stop_seconds",
    "user_unit_dir",
]

BASE_NAME = "indus-gateway"
BASE_LABEL = "ai.indus.gateway"
STOP_HEADROOM_S = 30
STOP_FLOOR_S = 60
CRON_CLEANUP_RESERVE_S = 10
LAUNCHD_EXIT_TIMEOUT_S = 60
LAUNCHD_THROTTLE_S = 30
RESTART_SEC = 5
EXIT_FATAL_CONFIG = 78

_DEFAULT_RESTART_DRAIN_S = 0.0
_DEFAULT_CRON_DRAIN_S = 30.0


def home_hash(home: Path) -> str:
    return hashlib.sha256(os.path.abspath(home).encode("utf-8")).hexdigest()[:8]


def _is_default(home: Path, default: Path) -> bool:
    return os.path.realpath(home) == os.path.realpath(default)


def service_name(home: Path, default: Path) -> str:
    """The systemd unit name (without ``.service``)."""
    return BASE_NAME if _is_default(home, default) else f"{BASE_NAME}-{home_hash(home)}"


def launchd_label(home: Path, default: Path) -> str:
    return BASE_LABEL if _is_default(home, default) else f"{BASE_LABEL}.{home_hash(home)}"


def timeout_stop_seconds(restart_drain_s: float, cron_drain_s: float) -> int:
    """See the module notes; a cron drain of 0 opts out of its reserve."""
    restart = max(0.0, restart_drain_s)
    cron = max(0.0, cron_drain_s)
    cron_budget = cron + CRON_CLEANUP_RESERVE_S if cron > 0 else 0.0
    return int(max(float(STOP_FLOOR_S), max(restart, cron_budget) + STOP_HEADROOM_S))


def _account_home(env: Mapping[str, str]) -> Path:
    raw = env.get("HOME", "")
    if raw and os.path.isabs(raw):
        return Path(raw)
    return Path.home()


def user_unit_dir(env: Mapping[str, str]) -> Path:
    """``$XDG_CONFIG_HOME/systemd/user`` (``~/.config`` when unset or relative)."""
    config = env.get("XDG_CONFIG_HOME", "")
    base = Path(config) if config and os.path.isabs(config) else _account_home(env) / ".config"
    return base / "systemd" / "user"


def launch_agents_dir(env: Mapping[str, str]) -> Path:
    return _account_home(env) / "Library" / "LaunchAgents"


def default_program(executable: str | None = None) -> tuple[str, ...]:
    """How the service starts ``indus``: the ``indus`` script next to this interpreter when it
    exists, else ``<python> -m indus``."""
    python = executable or sys.executable
    script = Path(python).parent / "indus"
    if script.is_file() and os.access(script, os.X_OK):
        return (str(script),)
    return (python, "-m", "indus")


def _number(value: object, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return float(value)


@dataclass(frozen=True)
class UnitSpec:
    """What a gateway service definition needs to know."""

    home: Path
    name: str
    label: str
    profile: str
    program: tuple[str, ...]
    path_env: str
    watchdog_s: int = 0
    timeout_stop_s: int = STOP_FLOOR_S + 10

    @property
    def unit_file(self) -> str:
        return f"{self.name}.service"

    @property
    def run_argv(self) -> tuple[str, ...]:
        return (*self.program, "gateway", "run")

    @classmethod
    def build(
        cls,
        home: Path,
        *,
        env: Mapping[str, str],
        setting: Callable[[str, object], object] = lambda _key, default: default,
        program: tuple[str, ...] | None = None,
    ) -> UnitSpec:
        """The spec for ``home``; ``setting(key, default)`` reads the profile's config."""
        default = default_home(env, user_home=lambda: _account_home(env))
        argv = program if program is not None else default_program()
        restart = _number(setting("agent.restart_drain_timeout", None), _DEFAULT_RESTART_DRAIN_S)
        cron = _number(setting("agent.cron_drain_timeout", None), _DEFAULT_CRON_DRAIN_S)
        watchdog = int(_number(setting("gateway.systemd_watchdog_seconds", None), 0))
        bin_dir = os.path.dirname(argv[0])
        entries = [bin_dir] if bin_dir else []
        entries += [p for p in env.get("PATH", "").split(os.pathsep) if p]
        entries += list(known_dirs.service_bins("linux"))
        return cls(
            home=Path(os.path.abspath(home)),
            name=service_name(home, default),
            label=launchd_label(home, default),
            profile=profile_id_of(home),
            program=argv,
            path_env=os.pathsep.join(dict.fromkeys(entries)),
            watchdog_s=max(0, watchdog),
            timeout_stop_s=timeout_stop_seconds(restart, cron),
        )


# -- systemd --------------------------------------------------------------------------------------


def systemd_quote(arg: str) -> str:
    """One argument for ``ExecStart=`` / ``Environment=``: double-quoted, with backslash and quote
    escaped and systemd's ``%`` specifiers and ``$`` expansion neutralised."""
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


def _command(argv: tuple[str, ...] | list[str]) -> str:
    return " ".join(systemd_quote(a) for a in argv)


def systemd_unit(spec: UnitSpec, *, system_user: str | None = None) -> str:
    """The unit text. ``system_user`` renders the system-wide variant (``User=``, started at
    boot); ``indus`` itself only ever writes the user unit."""
    lines = [
        "[Unit]",
        f"Description=Indus Agent messaging gateway ({spec.profile})",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
    ]
    if spec.watchdog_s > 0:
        lines += ["Type=notify", "NotifyAccess=main", f"WatchdogSec={spec.watchdog_s}s"]
    else:
        lines.append("Type=simple")
    if system_user is not None:
        lines.append(f"User={system_user}")
    lines += [
        f"ExecStart={_command(spec.run_argv)}",
        "ExecReload=/bin/kill -USR1 $MAINPID",
        f"ExecStop=-{_command([*spec.program, 'gateway', 'mark-stop', '--pid'])} $MAINPID",
        f"WorkingDirectory={str(spec.home).replace('%', '%%')}",
        f"Environment={systemd_quote(f'INDUS_HOME={spec.home}')}",
        f"Environment={systemd_quote('INDUS_GATEWAY_SUPERVISED=1')}",
        f"Environment={systemd_quote(f'PATH={spec.path_env}')}",
        "Restart=on-failure",
        f"RestartSec={RESTART_SEC}",
        f"RestartPreventExitStatus={EXIT_FATAL_CONFIG}",
        "KillMode=mixed",
        "KillSignal=SIGTERM",
        f"TimeoutStopSec={spec.timeout_stop_s}",
        "",
        "[Install]",
        f"WantedBy={'multi-user.target' if system_user is not None else 'default.target'}",
        "",
    ]
    return "\n".join(lines)


# -- launchd --------------------------------------------------------------------------------------

_WRAPPER = (
    '"$@" & child=$!; '
    "trap 'kill -TERM \"$child\" 2>/dev/null' TERM; "
    "trap 'kill -INT \"$child\" 2>/dev/null' INT; "
    "trap 'kill -USR1 \"$child\" 2>/dev/null' USR1; "
    'while :; do wait "$child"; status=$?; kill -0 "$child" 2>/dev/null || break; done; '
    f'[ "$status" -eq {EXIT_FATAL_CONFIG} ] && exit 0; exit "$status"'
)


def launchd_wrapper() -> str:
    """The ``/bin/sh -c`` script: run the gateway, forward signals, map 78 to 0."""
    return _WRAPPER


def launchd_plist(spec: UnitSpec) -> str:
    log_file = str(stdio_log_path(spec.home))
    agent = {
        "Label": spec.label,
        "ProgramArguments": ["/bin/sh", "-c", _WRAPPER, BASE_NAME, *spec.run_argv],
        "WorkingDirectory": str(spec.home),
        "EnvironmentVariables": {
            "INDUS_HOME": str(spec.home),
            "INDUS_GATEWAY_SUPERVISED": "1",
            "PATH": spec.path_env,
        },
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": LAUNCHD_THROTTLE_S,
        "ExitTimeOut": LAUNCHD_EXIT_TIMEOUT_S,
        "StandardOutPath": log_file,
        "StandardErrorPath": log_file,
    }
    return plistlib.dumps(agent, sort_keys=False).decode("utf-8")
