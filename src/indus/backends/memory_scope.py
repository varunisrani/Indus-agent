# pyright: strict
"""Memory caps for local background processes (P24-43).

On Linux, when a systemd user manager answers, a background job starts as
``systemd-run --user --scope --quiet --collect --unit indus-bg-<id> --property MemoryMax=<bytes>
[--expand-environment=no] -- <argv>``. The scope is the job's own cgroup: a job that eats memory is
killed alone, and a gateway running under a unit with ``MemoryMax`` no longer shares its cgroup
with the job (the OOM killer would otherwise take the gateway with it). With ``--scope``
systemd-run registers the scope and then becomes the command itself, so pid, process group and
output pipe stay what the registry expects.

The cap is ``terminal.background_memory_max``: a size (``1G``, ``512M``, ``768MiB``; a bare
number is MiB) or ``off``. Default 1 GiB, clamped to 64 MiB … 4 GiB.

Availability is probed once per (binary, bus) and remembered for ``PROBE_TTL_S`` either way: the
user bus can appear (lingering turned on) or vanish (logout). The bus is the job env's
``DBUS_SESSION_BUS_ADDRESS``, else ``$XDG_RUNTIME_DIR/bus``, else ``/run/user/<uid>/bus`` when that
folder is ours; a derived address is added to the job env. Systemd older than 254 does not know
``--expand-environment``; the probe then drops it. Without systemd (other systems, containers, no
user bus) the job runs unscoped and one debug line per reason says so. Never on Windows.

A scoped job whose leader died of a SIGKILL that Indus did not send is reported as killed by the
memory cap (``is_memory_kill``).
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from indus.foundation.clock import Clock, SystemClock
from indus.foundation.config.schema import default_for
from indus.foundation.scope import Scope
from indus.platform.locate import which

__all__ = [
    "DEFAULT_CAP",
    "MAX_CAP",
    "MEMORY_CAP_SOURCE",
    "MIN_CAP",
    "PROBE_TTL_S",
    "ScopeProbe",
    "Wrapped",
    "cap_for",
    "is_memory_kill",
    "parse_cap",
    "reset_probe",
    "wrap",
]

log = logging.getLogger(__name__)

MIB = 1024 * 1024
MIN_CAP = 64 * MIB
DEFAULT_CAP = 1024 * MIB
MAX_CAP = 4096 * MIB
PROBE_TTL_S = 60.0
PROBE_TIMEOUT_S = 5.0
MEMORY_CAP_SOURCE = "memory cap"
NO_EXPAND = "--expand-environment=no"
UNIT_PREFIX = "indus-bg-"
SETTING = "terminal.background_memory_max"

_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?)(?:i?b)?\s*$", re.IGNORECASE)
_FACTORS = {"": MIB, "k": 1024, "m": MIB, "g": 1024 * MIB, "t": 1024 * 1024 * MIB}
_OFF = frozenset({"off", "none", "no", "false", "0", "disabled"})

type Runner = Callable[..., subprocess.CompletedProcess[bytes]]

_said_lock = threading.Lock()
_said: set[str] = set()


def _once(key: str, level: int, message: str, *args: object) -> None:
    with _said_lock:
        if key in _said:
            return
        _said.add(key)
    log.log(level, message, *args)


def parse_cap(raw: object) -> int | None:
    """Bytes for a setting value, clamped to ``MIN_CAP`` … ``MAX_CAP``; None = no cap."""
    if raw is None:
        return DEFAULT_CAP
    if isinstance(raw, bool):
        return DEFAULT_CAP if raw else None
    if isinstance(raw, int | float):
        if raw <= 0:
            return None
        value = int(raw * MIB)
    else:
        text = str(raw).strip().lower()
        if not text:
            return DEFAULT_CAP
        if text in _OFF:
            return None
        match = _SIZE.match(text)
        if match is None:
            _once(f"bad:{text}", logging.WARNING, "%s=%r is not a size; using 1G", SETTING, raw)
            return DEFAULT_CAP
        value = int(float(match.group(1)) * _FACTORS[match.group(2).lower()])
        if value <= 0:
            return None
    clamped = min(max(value, MIN_CAP), MAX_CAP)
    if clamped != value:
        _once(
            f"clamp:{value}",
            logging.WARNING,
            "%s is outside 64M..4G; using %d MiB",
            SETTING,
            clamped // MIB,
        )
    return clamped


def cap_for(scope: Scope) -> int | None:
    raw = scope.config.get(SETTING) if scope.config is not None else None
    return parse_cap(default_for(SETTING) if raw is None else raw)


# -- the user bus ---------------------------------------------------------------------------------


def _owned_dir(path: Path) -> bool:
    get_uid = getattr(os, "getuid", None)
    try:
        info = path.stat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and (get_uid is None or info.st_uid == get_uid())


def bus_env(env: Mapping[str, str]) -> dict[str, str] | None:
    """The variables ``systemd-run --user`` needs to find the user manager, or None when there is
    no user bus to talk to."""
    address = env.get("DBUS_SESSION_BUS_ADDRESS", "").strip()
    if address:
        return {}
    runtime = env.get("XDG_RUNTIME_DIR", "").strip()
    extra: dict[str, str] = {}
    if not runtime:
        get_uid = getattr(os, "getuid", None)
        if get_uid is None:
            return None
        guess = Path(f"/run/user/{get_uid()}")
        if not _owned_dir(guess):
            return None
        runtime = str(guess)
        extra["XDG_RUNTIME_DIR"] = runtime
    bus = Path(runtime) / "bus"
    try:
        if not stat.S_ISSOCK(bus.stat().st_mode):
            return None
    except OSError:
        return None
    extra["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    return extra


# -- the probe ------------------------------------------------------------------------------------


def scope_argv(
    binary: str, unit: str, cap: int, argv: Sequence[str], *, no_expand: bool
) -> list[str]:
    return [
        binary,
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        "--unit",
        unit,
        "--property",
        f"MemoryMax={cap}",
        *([NO_EXPAND] if no_expand else []),
        "--",
        *argv,
    ]


@dataclass(frozen=True, slots=True)
class _Verdict:
    ok: bool
    no_expand: bool
    at: float
    why: str = ""


class ScopeProbe:
    """Whether ``systemd-run --user --scope`` works, remembered for ``PROBE_TTL_S``."""

    def __init__(self, *, runner: Runner | None = None, clock: Clock | None = None) -> None:
        self.runner: Runner = runner if runner is not None else subprocess.run
        self.clock: Clock = clock if clock is not None else SystemClock()
        self._lock = threading.Lock()
        self._verdicts: dict[tuple[str, str], _Verdict] = {}

    def _run(self, argv: list[str], env: Mapping[str, str]) -> tuple[int, str]:
        try:
            done = self.runner(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=dict(env),
                timeout=PROBE_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return -1, "the probe timed out"
        except OSError as exc:
            return -1, f"could not run systemd-run ({exc.strerror or exc})"
        text = (done.stderr or b"").decode("utf-8", "replace").strip()
        return done.returncode, text

    def check(self, binary: str, env: Mapping[str, str]) -> _Verdict:
        key = (binary, env.get("DBUS_SESSION_BUS_ADDRESS", ""))
        now = self.clock.monotonic()
        with self._lock:
            found = self._verdicts.get(key)
            if found is not None and now - found.at < PROBE_TTL_S:
                return found
        unit = f"{UNIT_PREFIX}probe-{secrets.token_hex(4)}"
        true = "/bin/true" if os.path.exists("/bin/true") else "true"
        code, text = self._run(scope_argv(binary, unit, MIN_CAP, [true], no_expand=True), env)
        no_expand = True
        if code != 0 and "expand-environment" in text:
            no_expand = False
            code, text = self._run(scope_argv(binary, unit, MIN_CAP, [true], no_expand=False), env)
        verdict = _Verdict(code == 0, no_expand, now, "" if code == 0 else (text or f"exit {code}"))
        with self._lock:
            self._verdicts[key] = verdict
        return verdict

    def reset(self) -> None:
        with self._lock:
            self._verdicts.clear()


_PROBE = ScopeProbe()


def reset_probe(probe: ScopeProbe | None = None) -> None:
    """Forget cached verdicts and the once-only log lines (tests)."""
    (probe or _PROBE).reset()
    with _said_lock:
        _said.clear()


# -- wrapping -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Wrapped:
    """``argv`` to run, ``env`` to run it with, ``cap`` in bytes (0 = not scoped), ``unit``."""

    argv: tuple[str, ...]
    env: Mapping[str, str]
    cap: int = 0
    unit: str = ""


def wrap(
    scope: Scope,
    argv: Sequence[str],
    env: Mapping[str, str],
    *,
    job_id: str,
    platform: str = sys.platform,
    probe: ScopeProbe | None = None,
) -> Wrapped:
    """``argv`` inside a capped transient scope when the host allows it (see the module notes)."""
    plain = Wrapped(tuple(argv), env)
    if not platform.startswith("linux"):
        return plain
    cap = cap_for(scope)
    if cap is None:
        return plain
    binary = which("systemd-run", path=env.get("PATH") or None)
    if binary is None:
        _once("no-binary", logging.DEBUG, "background memory cap skipped: no systemd-run on PATH")
        return plain
    extra = bus_env(env)
    if extra is None:
        _once("no-bus", logging.DEBUG, "background memory cap skipped: no systemd user bus")
        return plain
    run_env = {**env, **extra}
    verdict = (probe or _PROBE).check(binary, run_env)
    if not verdict.ok:
        _once(
            f"probe:{verdict.why}",
            logging.DEBUG,
            "background memory cap skipped: systemd-run --user --scope failed (%s)",
            verdict.why[:200],
        )
        return plain
    unit = f"{UNIT_PREFIX}{job_id.replace('_', '-')}-{secrets.token_hex(3)}"
    wrapped = scope_argv(binary, unit, cap, argv, no_expand=verdict.no_expand)
    return Wrapped(tuple(wrapped), run_env, cap, unit)


def is_memory_kill(returncode: int | None, *, scoped: bool, killed_by_us: bool) -> bool:
    """A scoped job ended by a SIGKILL nobody in Indus sent: the cgroup's OOM killer."""
    if not scoped or killed_by_us or returncode is None:
        return False
    sigkill = int(getattr(signal, "SIGKILL", 9))
    return returncode in (-sigkill, 128 + sigkill)
