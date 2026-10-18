# pyright: strict
"""Crash recovery for background jobs: the running-process checkpoint (P11-14).

Host jobs run in their own session, so they outlive a crash of the Indus process that started
them. ``<home>/processes.json`` (atomic, 0600) lists the running jobs of the profile, one entry
each: ``id, pid, pid_start_time, backend, env_key, log_path, owner_session, notify_on_exit,
persist_on_release, command`` (redacted), ``cwd, started_at`` and ``holder`` — the Indus process
that tracks the entry (its pid and start time).

Saving (``save``) is read-merge-write under a lock file: entries held by another Indus process
that is still alive (a CLI and a gateway on one profile) are kept as they are; this process's
entries are replaced by its current list.

Recovery (``classify``): entries of a live other holder are left alone. Every other entry is
``adopt``ed only when it is a host job whose PID exists with the recorded start time — a recycled
PID is never adopted or signalled; everything else (gone, recycled, unknown start time,
in-sandbox PIDs whose sandbox handle died with the old process) is ``lost``.

``DetachedJob`` is the handle of an adopted job: no output pipe, an exit noticed through a pidfd
or the registry's poll, an unknown exit code, and the ordered kill (SIGTERM to its group, grace,
SIGKILL) guarded by the start time.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, cast

from indus.backends.local import process_start_time
from indus.foundation.clock import Clock
from indus.foundation.fsio import FileLock, atomic_write_json

__all__ = [
    "CHECKPOINT_NAME",
    "DetachedJob",
    "Entry",
    "Recovery",
    "checkpoint_path",
    "classify",
    "holder_identity",
    "read_entries",
    "save",
]

log = logging.getLogger(__name__)

CHECKPOINT_NAME = "processes.json"
LOCK_TIMEOUT_S = 2.0
KILL_POLL_S = 0.05

type Fate = Literal["adopt", "lost", "foreign"]


@dataclass(frozen=True, slots=True)
class Entry:
    id: str
    pid: int | None
    pid_start_time: int | None
    backend: str
    env_key: str
    log_path: str
    owner_session: str | None
    notify_on_exit: bool
    persist_on_release: bool
    command: str
    cwd: str
    started_at: str
    holder: Mapping[str, int | None] = field(default_factory=lambda: dict[str, int | None]())

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "pid": self.pid,
            "pid_start_time": self.pid_start_time,
            "backend": self.backend,
            "env_key": self.env_key,
            "log_path": self.log_path,
            "owner_session": self.owner_session,
            "notify_on_exit": self.notify_on_exit,
            "persist_on_release": self.persist_on_release,
            "command": self.command,
            "cwd": self.cwd,
            "started_at": self.started_at,
            "holder": dict(self.holder),
        }


def checkpoint_path(home: Path) -> Path:
    return home / CHECKPOINT_NAME


def holder_identity() -> dict[str, int | None]:
    pid = os.getpid()
    return {"pid": pid, "start": process_start_time(pid)}


def _opt_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return None


def _entry(raw: Mapping[str, Any]) -> Entry | None:
    ident = raw.get("id")
    if not isinstance(ident, str) or not ident.startswith("proc_"):
        return None
    holder_raw = raw.get("holder")
    holder: dict[str, int | None] = {}
    if isinstance(holder_raw, Mapping):
        values = cast(Mapping[str, Any], holder_raw)
        holder = {"pid": _opt_int(values.get("pid")), "start": _opt_int(values.get("start"))}
    session = raw.get("owner_session")
    return Entry(
        id=ident,
        pid=_opt_int(raw.get("pid")),
        pid_start_time=_opt_int(raw.get("pid_start_time")),
        backend=str(raw.get("backend") or "local"),
        env_key=str(raw.get("env_key") or ""),
        log_path=str(raw.get("log_path") or ""),
        owner_session=session if isinstance(session, str) else None,
        notify_on_exit=bool(raw.get("notify_on_exit")),
        persist_on_release=bool(raw.get("persist_on_release")),
        command=str(raw.get("command") or ""),
        cwd=str(raw.get("cwd") or ""),
        started_at=str(raw.get("started_at") or ""),
        holder=holder,
    )


def read_entries(home: Path) -> list[Entry]:
    """The checkpoint's entries; a missing file is empty, a corrupt one is ignored (warning)."""
    path = checkpoint_path(home)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        log.warning("cannot read %s (%s); running processes are not recovered", path, exc)
        return []
    try:
        raw: Any = json.loads(text)
    except ValueError:
        log.warning("%s is not valid JSON; ignored (rewritten on the next change)", path)
        return []
    if not isinstance(raw, list):
        log.warning("%s does not hold a list; ignored (rewritten on the next change)", path)
        return []
    found: list[Entry] = []
    for item in cast(list[Any], raw):
        if isinstance(item, Mapping):
            entry = _entry(cast(Mapping[str, Any], item))
            if entry is not None:
                found.append(entry)
    return found


def _holder_alive_elsewhere(entry: Entry) -> bool:
    """Is the entry tracked by another Indus process that is still running?"""
    pid = entry.holder.get("pid")
    if pid is None or pid == os.getpid():
        return False
    start = entry.holder.get("start")
    now = process_start_time(pid)
    if now is None:
        return False
    return start is None or now == start


def _fate(entry: Entry) -> Fate:
    if _holder_alive_elsewhere(entry):
        return "foreign"
    if entry.backend != "local" or entry.pid is None or entry.pid <= 1:
        return "lost"
    if entry.pid_start_time is None:
        return "lost"
    if process_start_time(entry.pid) != entry.pid_start_time:
        return "lost"
    return "adopt"


@dataclass
class Recovery:
    adopt: list[Entry] = field(default_factory=list[Entry])
    lost: list[Entry] = field(default_factory=list[Entry])
    foreign: list[Entry] = field(default_factory=list[Entry])


def classify(entries: Iterable[Entry]) -> Recovery:
    found = Recovery()
    for entry in entries:
        getattr(found, _fate(entry)).append(entry)
    return found


def save(home: Path, own: Iterable[Entry]) -> bool:
    """Replace this process's entries in ``home``'s checkpoint (module notes); False when the
    file could not be written (a warning is logged)."""
    path = checkpoint_path(home)
    me = holder_identity()
    mine = [replace(e, holder=me) for e in own]
    ids = {e.id for e in mine}
    try:
        with FileLock(path.with_name(path.name + ".lock"), timeout=LOCK_TIMEOUT_S):
            others = [
                e for e in read_entries(home) if e.id not in ids and _holder_alive_elsewhere(e)
            ]
            entries = [e.to_json() for e in [*others, *mine]]
            if not entries and not path.exists():
                return True
            atomic_write_json(path, entries, mode=0o600)
    except Exception as exc:
        log.warning("could not update %s: %s", path, exc)
        return False
    return True


# -- adopted jobs -------------------------------------------------------------------------------


def _state(pid: int) -> tuple[int | None, bool]:
    """(start time, zombie) of ``pid`` from ``/proc``."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None, False
    fields = raw.rsplit(")", 1)[-1].split()
    try:
        return int(fields[19]), fields[0] in ("Z", "X")
    except (IndexError, ValueError):
        return None, False


class DetachedJob:
    """Handle of a job adopted after a restart (meets ``processes.JobHandle``)."""

    pty = False
    fd: int | None = None
    unknown_exit = True  # not our child: its exit status cannot be collected

    def __init__(self, pid: int, start: int) -> None:
        self._pid = pid
        self.start = start
        self._gone = False

    @property
    def pid(self) -> int | None:
        return self._pid

    def alive(self) -> bool:
        if self._gone:
            return False
        start, zombie = _state(self._pid)
        if start != self.start or zombie:
            self._gone = True
        return not self._gone

    def poll(self) -> int | None:
        return None if self.alive() else -1

    async def write(self, data: str) -> str | None:
        return "a process recovered after a restart has no input connection"

    def close_input(self) -> str | None:
        return "a process recovered after a restart has no input connection"

    def _signal(self, sig: int) -> None:
        if not self.alive():
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self._pid, sig)  # started with its own session: pgid == pid
        if self.alive():
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(self._pid, sig)

    async def stop(self, clock: Clock, grace: float) -> None:
        self._signal(signal.SIGTERM)
        deadline = clock.monotonic() + max(0.0, grace)
        while self.alive() and clock.monotonic() < deadline:
            await clock.sleep(KILL_POLL_S)
        self._signal(signal.SIGKILL)

    def force_stop(self) -> None:
        self._signal(signal.SIGKILL)

    def release(self) -> None:
        return None
