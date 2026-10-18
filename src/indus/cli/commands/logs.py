"""``indus logs``: read, filter and follow the profile's log files (P07-20).

Lines follow P01's format: ``YYYY-MM-DD HH:MM:SS LEVEL logger [session:turn]: message``. Filters
AND together: ``--level`` (that level and up), ``--session`` (text anywhere in the record),
``--since`` (``30s``, ``5m``, ``2h``, ``1d`` back from now on the scope's clock; line times are read
in the clock's zone) and ``--component`` (logger-name prefixes). A line without a timestamp — a
traceback frame, the rest of a multi-line message — belongs to the record above it and gets that
record's verdict; such lines before the first stamped one pass unless a time or level filter is
on. ``-f`` prints new matching lines as they arrive, polling every 0.3 s, and starts over at the
top of the file when it is rotated or truncated.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import BinaryIO

import click

from indus.cli.cmdkit import age_label, cli_state, err, human_size
from indus.foundation.log import logs_dir
from indus.foundation.scope import Scope
from indus.state.writer import blocking_sleep

__all__ = [
    "COMPONENTS",
    "LEVELS",
    "LOG_FILES",
    "POLL_SECONDS",
    "LineFilter",
    "follow",
    "logs_command",
    "parse_since",
    "tail",
]

LOG_FILES = {
    "agent": "agent.log",
    "errors": "errors.log",
    "gateway": "gateway.log",
    "gui": "gui.log",
    "update": "update.log",
}
ROOT_LOGS = frozenset({"update"})
COMPONENTS: dict[str, tuple[str, ...]] = {
    "agent": ("indus.core", "indus.providers", "indus.learning"),
    "tools": ("indus.tools", "indus.backends", "indus.safety"),
    "cli": ("indus.cli", "indus.service"),
    "gateway": ("indus.gateway",),
    "cron": ("indus.cron",),
}
LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
POLL_SECONDS = 0.3
TAIL_BYTES = 8 * 1024 * 1024
_SINCE = re.compile(r"^(\d+)\s*([smhd])$")
_UNIT = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})")
_HEAD = re.compile(
    r"^\S+[ T]\S+\s+(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+(?P<name>[^\s:\[]+)"
)


def parse_since(text: str, now: datetime) -> datetime:
    found = _SINCE.match(text.strip().lower())
    if found is None:
        raise click.BadParameter(f"{text!r} is not like 30s, 5m, 2h or 1d", param_hint="--since")
    return now - timedelta(**{_UNIT[found.group(2)]: int(found.group(1))})


@dataclass(frozen=True)
class Filters:
    min_level: int | None = None
    session: str | None = None
    since: datetime | None = None
    prefixes: tuple[str, ...] | None = None
    zone: tzinfo | None = None

    @property
    def active(self) -> bool:
        return any(v is not None for v in (self.min_level, self.session, self.since, self.prefixes))

    @property
    def timed(self) -> bool:
        return self.min_level is not None or self.since is not None


def _stamp(line: str, zone: tzinfo | None) -> datetime | None:
    found = _STAMP.match(line)
    if found is None:
        return None
    try:
        moment = datetime.fromisoformat(f"{found.group(1)} {found.group(2)}")
    except ValueError:
        return None
    return moment.replace(tzinfo=zone)


def _record_ok(line: str, when: datetime | None, f: Filters) -> bool:
    if f.since is not None and when is not None and when < f.since:
        return False
    head = _HEAD.match(line)
    if f.min_level is not None and head is not None and LEVELS[head.group("level")] < f.min_level:
        return False
    if f.session is not None and f.session not in line:
        return False
    if f.prefixes is not None:
        name = head.group("name") if head is not None else None
        if name is None or not any(name == p or name.startswith(p + ".") for p in f.prefixes):
            return False
    return True


class LineFilter:
    """Filters one file's lines in order; continuation lines share their record's verdict."""

    def __init__(self, filters: Filters) -> None:
        self.filters = filters
        self._carry: bool | None = None

    def __call__(self, line: str) -> bool:
        when = _stamp(line, self.filters.zone)
        if when is not None:
            self._carry = _record_ok(line, when, self.filters)
            return self._carry
        if self._carry is None:
            return not self.filters.timed and _record_ok_loose(line, self.filters)
        return self._carry


def _record_ok_loose(line: str, f: Filters) -> bool:
    if f.session is not None and f.session not in line:
        return False
    return f.prefixes is None


def _read_lines(path: Path) -> list[str]:
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > TAIL_BYTES:
            handle.seek(size - TAIL_BYTES)
            handle.readline()  # drop the partial first line
        data = handle.read()
    return data.decode("utf-8", errors="replace").splitlines(keepends=True)


def tail(path: Path, count: int, filters: Filters) -> list[str]:
    """The last ``count`` lines of ``path`` that pass ``filters``."""
    lines = _read_lines(path)
    if filters.active:
        keep = LineFilter(filters)
        lines = [line for line in lines if keep(line)]
    return lines[-count:] if count > 0 else []


def _identity(path: Path) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_dev, st.st_ino


def follow(
    path: Path,
    filters: Filters,
    write: Callable[[str], None],
    pause: Callable[[float], None],
    *,
    from_end: bool = True,
) -> None:
    """Print new matching lines until ``pause`` raises (Ctrl+C); survives rotation."""
    keep = LineFilter(filters)
    handle: BinaryIO | None = None
    ident: tuple[int, int] | None = None
    pending = b""
    try:
        while True:
            if handle is None:
                try:
                    handle = path.open("rb")
                except FileNotFoundError:
                    pause(POLL_SECONDS)
                    continue
                ident = _identity(path)
                if from_end:
                    handle.seek(0, os.SEEK_END)
                    from_end = False
                pending = b""
            chunk = handle.read()
            if chunk:
                pending += chunk
                *complete, pending = pending.split(b"\n")
                for raw in complete:
                    line = raw.decode("utf-8", errors="replace") + "\n"
                    if keep(line):
                        write(line)
                continue
            now_ident = _identity(path)
            try:
                shrunk = now_ident == ident and os.stat(path).st_size < handle.tell()
            except OSError:
                shrunk = False
            if now_ident is not None and (now_ident != ident or shrunk):
                handle.close()
                handle = None  # rotated or truncated: read the new file from its start
                continue
            pause(POLL_SECONDS)
    finally:
        if handle is not None:
            handle.close()


def _path_for(scope: Scope, name: str) -> Path:
    home = scope.root if name in ROOT_LOGS else scope.home
    return logs_dir(home) / LOG_FILES[name]


def _list(scope: Scope) -> int:
    folder = logs_dir(scope.home)
    entries: list[tuple[str, Path]] = []
    if folder.is_dir():
        entries = [
            (p.name, p) for p in sorted(folder.iterdir()) if p.is_file() and ".log" in p.name
        ]
    root_update = _path_for(scope, "update")
    if root_update.parent != folder and root_update.is_file():
        entries.append((f"{root_update.name} (root)", root_update))
    if not entries:
        click.echo(f"No log files yet in {folder}.")
        return 0
    now = scope.clock.now().timestamp()
    click.echo(f"Log files in {folder}:")
    for label, path in entries:
        st = path.stat()
        click.echo(f"  {label:<28} {human_size(st.st_size):>9}   {age_label(now - st.st_mtime)}")
    return 0


@click.command("logs", short_help="Show and follow the log files.")
@click.argument("name", required=False, default="agent", type=click.Choice([*LOG_FILES, "list"]))
@click.option("-n", "--lines", "count", type=click.IntRange(min=0), default=50, show_default=True)
@click.option("-f", "--follow", "follow_flag", is_flag=True, help="Keep printing new lines.")
@click.option(
    "--level",
    type=click.Choice(list(LEVELS), case_sensitive=False),
    default=None,
    help="Only this level and above.",
)
@click.option("--session", default=None, help="Only records mentioning this session id.")
@click.option("--since", default=None, help="Only records newer than 30s, 5m, 2h, 1d ago.")
@click.option(
    "--component",
    type=click.Choice(list(COMPONENTS), case_sensitive=False),
    default=None,
    help="Only records from one part of the program.",
)
def logs_command(
    name: str,
    count: int,
    follow_flag: bool,
    level: str | None,
    session: str | None,
    since: str | None,
    component: str | None,
) -> int:
    """Show the end of a log file (agent, errors, gateway, gui, update) or `list` them."""
    scope = cli_state().scope
    if name == "list":
        return _list(scope)
    now = scope.clock.now()
    filters = Filters(
        min_level=LEVELS[level.upper()] if level else None,
        session=session or None,
        since=parse_since(since, now) if since else None,
        prefixes=COMPONENTS[component.lower()] if component else None,
        zone=now.tzinfo,
    )
    path = _path_for(scope, name)
    if not path.is_file():
        err(f"No {name} log yet ({path}). Logs appear once Indus Agent has run.")
        return 1
    try:
        lines = tail(path, count, filters)
    except OSError as exc:
        err(f"Cannot read {path}: {exc}")
        return 1
    shown = [
        f"{label}={value}"
        for label, value in (
            ("level>=", level),
            ("session", session),
            ("since", since),
            ("component", component),
        )
        if value
    ]
    extra = f" [{', '.join(shown)}]" if shown else ""
    mode = "following, Ctrl+C to stop" if follow_flag else f"last {count}"
    click.echo(f"--- {path}{extra} ({mode}) ---")
    for line in lines:
        click.echo(line, nl=False)
    if not follow_flag:
        return 0
    try:
        follow(path, filters, lambda line: click.echo(line, nl=False), blocking_sleep(scope.clock))
    except KeyboardInterrupt:
        click.echo("\n--- stopped ---")
    return 0
