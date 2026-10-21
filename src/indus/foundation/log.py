"""Process logging: one queue, one writer thread, files routed to each record's own home.

``setup(scope, mode)`` is called once per process by ``foundation.bootstrap`` (protocol servers
call it before importing anything that might log). After that every module just uses
``logging.getLogger(__name__)``.

How a record travels:

1. The record factory (installed by ``setup``) stamps it in the emitting thread with
   ``session_id`` / ``turn_id`` (from :func:`log_tags`) and ``home``, the home of the scope that
   is current there (``current_scope()``), or the process scope when none is bound.
2. A queue handler on the root logger puts a shallow copy on an in-process queue. Nothing is
   formatted yet, so the emitting thread never touches a file or a rotation.
3. The listener thread hands it to the router, which picks the file set of the record's home
   (created on first use) and writes it through a redacting formatter built for that home's
   scope. A home that was deleted or tombstoned is never re-created: its records fall back to
   the process home.

Files under ``<home>/logs/`` (dir 0700, files 0600): ``agent.log`` at the configured level,
``errors.log`` for WARNING and up, plus ``gateway.log`` / ``gui.log`` in those modes for their own
loggers. In ``stdio`` mode nothing writes to stdout; diagnostics go to stderr.
"""

from __future__ import annotations

import atexit
import contextlib
import copy
import logging
import os
import queue
import sys
import threading
import time
from collections.abc import Callable, Generator
from contextvars import ContextVar
from dataclasses import dataclass
from io import TextIOWrapper
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path
from typing import Any, Literal

from indus.foundation.errors import ScopeUnresolved
from indus.foundation.home import secure_dir
from indus.foundation.layout import profile_home_problem
from indus.foundation.redact import Redactor
from indus.foundation.scope import Scope, current_scope

__all__ = [
    "MODES",
    "Mode",
    "drain",
    "flush",
    "log_tags",
    "logs_dir",
    "release_home",
    "setup",
    "shutdown",
]

Mode = Literal["cli", "gateway", "gui", "stdio"]
MODES: tuple[Mode, ...] = ("cli", "gateway", "gui", "stdio")

MIB = 1024 * 1024
DEFAULT_LEVEL = "INFO"
DEFAULT_MAX_SIZE_MB = 5
DEFAULT_BACKUP_COUNT = 3

LINE_FORMAT = "%(asctime)s %(levelname)s %(name)s%(indus_tags)s: %(message)s"
CONSOLE_FORMAT = "%(asctime)s %(levelname)s %(name)s%(indus_tags)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
CONSOLE_DATE_FORMAT = "%H:%M:%S"

# Third-party loggers that flood at INFO/DEBUG; pinned at WARNING by setup.
NOISY_LOGGERS = ("httpx", "httpcore", "httpx2", "httpcore2", "urllib3", "asyncio")
"""Libraries whose INFO lines carry full request URLs (query strings may hold a single-use ticket
or an OAuth code): held at WARNING. ``httpx2`` / ``httpcore2`` are the fork the MCP SDK and the
ASGI test client use (P24 integration finding)."""


@dataclass(frozen=True)
class FileSpec:
    """One log file kept under ``<home>/logs/``."""

    name: str
    level: int | None  # None: the configured ``logging.level``
    max_bytes: int | None  # None: ``logging.max_size_mb``
    backups: int | None  # None: ``logging.backup_count``
    prefixes: tuple[str, ...] = ()  # logger-name prefixes kept; empty keeps every logger


ALWAYS_FILES = (
    FileSpec("agent.log", None, None, None),
    FileSpec("errors.log", logging.WARNING, 2 * MIB, 2),
)
MODE_FILES: dict[str, FileSpec] = {
    "gateway": FileSpec("gateway.log", logging.INFO, 5 * MIB, 3, ("indus.gateway",)),
    "gui": FileSpec(
        "gui.log", logging.INFO, 10 * MIB, 5, ("indus.web", "indus.tui_bridge", "uvicorn")
    ),
}

_session_id: ContextVar[str | None] = ContextVar("indus_log_session_id", default=None)
_turn_id: ContextVar[str | None] = ContextVar("indus_log_turn_id", default=None)

# Attribute names the factory adds to every record (besides the private scope reference).
RECORD_FIELDS = ("session_id", "turn_id", "home", "indus_tags")
_SCOPE_ATTR = "_indus_scope"


@contextlib.contextmanager
def log_tags(*, session_id: str | None = None, turn_id: str | None = None) -> Generator[None]:
    """Tag every record logged inside the block (this task/thread only) with these ids."""
    tokens = [_session_id.set(session_id), _turn_id.set(turn_id)]
    try:
        yield
    finally:
        _turn_id.reset(tokens[1])
        _session_id.reset(tokens[0])


def _tags_text(session_id: str, turn_id: str) -> str:
    if session_id and turn_id:
        return f" [{session_id}:{turn_id}]"
    if session_id:
        return f" [{session_id}]"
    if turn_id:
        return f" [:{turn_id}]"
    return ""


def logs_dir(home: Path) -> Path:
    return home / "logs"


def _secure_mkdir(path: Path, scope: Scope, *, parents: bool) -> None:
    """Create ``path`` with the home dir mode (``INDUS_HOME_MODE``; managed installs keep theirs).

    Without ``parents`` a vanished home is never re-created: the plain mkdir fails first.
    """
    if not parents:
        path.mkdir(mode=0o700, exist_ok=True)
    secure_dir(path, home=scope.home, env=scope.env)


# -- handlers -----------------------------------------------------------------------------------


class _OwnerOnlyRotatingFile(RotatingFileHandler):
    """A size-rotated file created 0600 that notices when someone else rotated it.

    Before each write the path's identity is compared with the open stream's; when ``mv``,
    logrotate or a delete swapped the file out, the stream is reopened at the path. When the
    whole ``logs`` dir is gone the write is skipped and ``dir_missing`` is set so the router can
    decide where the record goes instead.
    """

    def __init__(self, path: Path, *, level: int, max_bytes: int, backups: int) -> None:
        self._identity: tuple[int, int] | None = None
        self.dir_missing = False
        super().__init__(
            path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8", delay=True
        )
        self.setLevel(level)

    def _open(self) -> TextIOWrapper:
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            info = os.fstat(fd)
            self._identity = (info.st_dev, info.st_ino)
            return open(fd, "a", encoding=self.encoding, errors=self.errors)  # noqa: SIM115
        except BaseException:
            os.close(fd)
            raise

    def _drop_stream(self) -> None:
        stream = self.stream
        self.stream = None  # type: ignore[assignment]
        if stream is not None:
            with contextlib.suppress(OSError, ValueError):
                stream.close()

    def emit(self, record: logging.LogRecord) -> None:
        self.dir_missing = False
        if self.stream is not None:
            try:
                info = os.stat(self.baseFilename)
                if (info.st_dev, info.st_ino) != self._identity:
                    self._drop_stream()
            except FileNotFoundError:
                self._drop_stream()
            except OSError:
                pass  # transient; keep writing to the open stream
        if self.stream is None and not os.path.isdir(os.path.dirname(self.baseFilename)):
            self.dir_missing = True
            return
        super().emit(record)


class _PrefixFilter(logging.Filter):
    def __init__(self, prefixes: tuple[str, ...]) -> None:
        super().__init__()
        self._prefixes = prefixes

    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name
        return any(name == p or name.startswith(p + ".") for p in self._prefixes)


class _RedactingFormatter(logging.Formatter):
    """Formats a record, then scrubs the whole line (message, args and traceback) for secrets."""

    def __init__(self, redactor: Redactor | None, fmt: str, datefmt: str) -> None:
        super().__init__(fmt, datefmt)
        self.redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        for name in RECORD_FIELDS:
            if not hasattr(record, name):
                setattr(record, name, "")
        text = super().format(record)
        redactor = self.redactor
        return redactor.redact(text) if redactor is not None else text


class _NoFormatQueueHandler(QueueHandler):
    """Queue a shallow copy, unformatted: the listener formats with the record's own scope."""

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        return copy.copy(record)


@dataclass
class _HomeFiles:
    scope: Scope | None
    formatter: _RedactingFormatter
    handlers: list[_OwnerOnlyRotatingFile]


class _HomeRouter(logging.Handler):
    """Sends each record to the file set of the record's home (listener thread only)."""

    def __init__(self, process_scope: Scope, specs: list[tuple[FileSpec, int, int, int]]) -> None:
        super().__init__()
        self.process_home = process_scope.home
        self._process_scope = process_scope
        self._specs = specs
        self._homes: dict[Path, _HomeFiles] = {}
        self._homes_lock = threading.RLock()

    # The files of one home, built on first use.
    def files_for(self, home: Path, scope: Scope | None) -> _HomeFiles | None:
        with self._homes_lock:
            files = self._homes.get(home)
            if files is not None:
                if scope is not None and scope is not files.scope:
                    files.scope = scope
                    files.formatter.redactor = Redactor(scope)
                return files
            owner = scope if scope is not None else self._process_scope
            if home != self.process_home:
                if not home.is_dir() or profile_home_problem(home) is not None:
                    return None
                _secure_mkdir(logs_dir(home), owner, parents=False)
            else:
                _secure_mkdir(logs_dir(home), owner, parents=True)
            formatter = _RedactingFormatter(Redactor(owner), LINE_FORMAT, DATE_FORMAT)
            handlers: list[_OwnerOnlyRotatingFile] = []
            for spec, level, max_bytes, backups in self._specs:
                handler = _OwnerOnlyRotatingFile(
                    logs_dir(home) / spec.name, level=level, max_bytes=max_bytes, backups=backups
                )
                handler.setFormatter(formatter)
                if spec.prefixes:
                    handler.addFilter(_PrefixFilter(spec.prefixes))
                handlers.append(handler)
            files = _HomeFiles(owner, formatter, handlers)
            self._homes[home] = files
            return files

    def _write(self, files: _HomeFiles, record: logging.LogRecord) -> bool:
        """Write to every matching file; False when the home's logs dir has vanished."""
        for handler in files.handlers:
            if record.levelno < handler.level:
                continue
            handler.handle(record)
            if handler.dir_missing:
                return False
        return True

    def emit(self, record: logging.LogRecord) -> None:
        try:
            raw = getattr(record, "home", "")
            home = Path(raw) if raw else self.process_home
            if home != self.process_home:
                scope: Scope | None = getattr(record, _SCOPE_ATTR, None)
                files = self.files_for(home, scope)
                if files is not None:
                    if self._write(files, record):
                        return
                    # Its logs dir vanished (profile deleted under us): stop serving it.
                    self.release_home(home)
            own = self.files_for(self.process_home, None)
            if own is not None and not self._write(own, record):
                _secure_mkdir(logs_dir(self.process_home), self._process_scope, parents=True)
                self._write(own, record)
        except Exception:
            self.handleError(record)

    def homes(self) -> list[Path]:
        with self._homes_lock:
            return list(self._homes)

    def release_home(self, home: Path) -> bool:
        with self._homes_lock:
            files = self._homes.pop(home, None)
        if files is None:
            return False
        for handler in files.handlers:
            with contextlib.suppress(Exception):
                handler.close()
        return True

    def close(self) -> None:
        for home in self.homes():
            self.release_home(home)
        super().close()


# -- process state ------------------------------------------------------------------------------


@dataclass
class _Live:
    scope: Scope
    mode: Mode
    queue: queue.Queue[logging.LogRecord | None]
    queue_handler: _NoFormatQueueHandler
    listener: QueueListener
    router: _HomeRouter
    console: logging.Handler | None
    factory: Callable[..., logging.LogRecord]
    previous_factory: Callable[..., logging.LogRecord]


_LOCK = threading.RLock()
_live: _Live | None = None
_atexit_registered = False
_level_warnings: set[str] = set()


def _scope_for_record() -> Scope | None:
    try:
        return current_scope()
    except ScopeUnresolved:
        live = _live
        return live.scope if live is not None else None


def _make_factory(
    base: Callable[..., logging.LogRecord],
) -> Callable[..., logging.LogRecord]:
    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = base(*args, **kwargs)
        session_id = _session_id.get() or ""
        turn_id = _turn_id.get() or ""
        record.session_id = session_id
        record.turn_id = turn_id
        record.indus_tags = _tags_text(session_id, turn_id)
        scope = _scope_for_record()
        record.home = str(scope.home) if scope is not None else ""
        setattr(record, _SCOPE_ATTR, scope)
        return record

    return factory


def _config_value(scope: Scope, path: str) -> object:
    config: Any = scope.config
    if config is None:
        return None
    try:
        return config.get(path)
    except (KeyError, LookupError, AttributeError, TypeError, ValueError):
        return None


def _settings(scope: Scope) -> tuple[int, int, int]:
    """(level, max bytes, backup count) for ``agent.log`` from the scope's config."""
    raw_level = _config_value(scope, "logging.level")
    level_name = raw_level.strip().upper() if isinstance(raw_level, str) else DEFAULT_LEVEL
    level = logging.getLevelNamesMapping().get(level_name)
    if level is None:
        level = logging.INFO
        if level_name not in _level_warnings:
            _level_warnings.add(level_name)
            logging.getLogger(__name__).warning(
                "unknown logging.level %r in config; using INFO", raw_level
            )
    raw_size = _config_value(scope, "logging.max_size_mb")
    size_mb = (
        raw_size
        if isinstance(raw_size, int | float) and not isinstance(raw_size, bool) and raw_size > 0
        else DEFAULT_MAX_SIZE_MB
    )
    raw_count = _config_value(scope, "logging.backup_count")
    backups = (
        raw_count
        if isinstance(raw_count, int) and not isinstance(raw_count, bool) and raw_count >= 0
        else DEFAULT_BACKUP_COUNT
    )
    return level, max(1, int(size_mb * MIB)), backups


def _is_stdout_handler(handler: logging.Handler) -> bool:
    stream: object = getattr(handler, "stream", None)
    return stream is sys.stdout or stream is sys.__stdout__


def _line_buffer_piped_stdout() -> None:
    """A piped stdout is block-buffered; make it line-buffered so a supervisor reading the
    pipe sees progress as it happens (a terminal already is)."""
    stream = sys.stdout
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        if stream.isatty():
            return
        reconfigure(line_buffering=True)
    except (OSError, ValueError):
        pass


def _console_level(mode: Mode, verbose: bool) -> int | None:
    if verbose:
        return logging.DEBUG
    if mode == "stdio":
        return logging.WARNING
    return None


def _teardown_locked() -> None:
    global _live
    live = _live
    if live is None:
        return
    _live = None
    root = logging.getLogger()
    root.removeHandler(live.queue_handler)
    if live.console is not None:
        root.removeHandler(live.console)
        with contextlib.suppress(Exception):
            live.console.close()
    with contextlib.suppress(Exception):
        live.listener.stop()
    live.router.close()
    if logging.getLogRecordFactory() is live.factory:
        logging.setLogRecordFactory(live.previous_factory)


def setup(scope: Scope, mode: Mode = "cli", verbose: bool = False) -> Path:
    """Configure process logging for ``scope`` (the process profile); returns its logs dir.

    Calling it again (a re-home after ``-p``, a test) replaces the previous setup.
    """
    global _live, _atexit_registered
    if mode not in MODES:
        raise ValueError(f"unknown logging mode {mode!r}; expected one of {', '.join(MODES)}")
    with _LOCK:
        _teardown_locked()
        root = logging.getLogger()
        if mode == "stdio":
            # Protocol frames own stdout: drop anything a library attached there.
            for handler in [h for h in root.handlers if _is_stdout_handler(h)]:
                root.removeHandler(handler)
        else:
            _line_buffer_piped_stdout()

        level, max_bytes, backups = _settings(scope)
        specs: list[tuple[FileSpec, int, int, int]] = []
        for spec in (*ALWAYS_FILES, *([MODE_FILES[mode]] if mode in MODE_FILES else [])):
            specs.append(
                (
                    spec,
                    spec.level if spec.level is not None else level,
                    spec.max_bytes if spec.max_bytes is not None else max_bytes,
                    spec.backups if spec.backups is not None else backups,
                )
            )

        router = _HomeRouter(scope, specs)
        record_queue: queue.Queue[logging.LogRecord | None] = queue.Queue()
        queue_handler = _NoFormatQueueHandler(record_queue)
        listener = QueueListener(record_queue, router, respect_handler_level=False)

        console: logging.Handler | None = None
        console_level = _console_level(mode, verbose)
        if console_level is not None:
            console = logging.StreamHandler(sys.stderr)
            console.setLevel(console_level)
            console.setFormatter(
                _RedactingFormatter(Redactor(scope), CONSOLE_FORMAT, CONSOLE_DATE_FORMAT)
            )

        previous_factory = logging.getLogRecordFactory()
        factory = _make_factory(previous_factory)
        _live = _Live(
            scope=scope,
            mode=mode,
            queue=record_queue,
            queue_handler=queue_handler,
            listener=listener,
            router=router,
            console=console,
            factory=factory,
            previous_factory=previous_factory,
        )
        logging.setLogRecordFactory(factory)
        # Create the process home's files now so permission problems surface at startup.
        router.files_for(scope.home, None)
        listener.start()
        root.addHandler(queue_handler)
        if console is not None:
            root.addHandler(console)

        file_levels = [spec_level for _, spec_level, _, _ in specs]
        root.setLevel(min(*file_levels, console_level or logging.CRITICAL))
        for name in NOISY_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)

        if not _atexit_registered:
            atexit.register(shutdown)
            _atexit_registered = True
        return logs_dir(scope.home)


def _wait_empty(record_queue: queue.Queue[Any], timeout: float | None) -> bool:
    """Wait until every queued record was handled; False when ``timeout`` ran out first."""
    done = record_queue.all_tasks_done
    deadline = None if timeout is None else time.monotonic() + timeout
    with done:
        while record_queue.unfinished_tasks:
            if deadline is None:
                done.wait()
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            done.wait(remaining)
    return True


def flush() -> None:
    """Block until every record logged so far is in its file (normal exits)."""
    live = _live
    if live is not None:
        _wait_empty(live.queue, None)


def drain(timeout: float = 1.0) -> bool:
    """Best effort for hard exits: wait at most ``timeout`` seconds; True when fully written."""
    live = _live
    if live is None:
        return True
    return _wait_empty(live.queue, max(0.0, timeout))


def release_home(home: Path) -> bool:
    """Close the files kept open for ``home`` (call before deleting a profile)."""
    live = _live
    if live is None:
        return False
    return live.router.release_home(home)


def shutdown() -> None:
    """Stop the writer thread after it has written everything queued, and detach our handlers."""
    with _LOCK:
        _teardown_locked()
