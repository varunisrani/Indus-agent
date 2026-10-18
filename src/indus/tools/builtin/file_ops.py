# pyright: strict
"""The byte layer under the file tools: one ``FileOps`` per task environment (P05-15).

Two implementations behind one async interface:

- ``HostFileOps`` works on the Indus host's own file system (the local backend). Reads open with
  ``O_NONBLOCK`` and check ``fstat`` before reading, so a FIFO or device never blocks a worker;
  writes go through ``foundation.fsio`` (temp file in the same directory, then rename). The
  blocking calls run on the shared ``io`` pool.
- ``ShellFileOps`` speaks a small shell protocol through ``Environment.execute`` for every other
  backend (containers, SSH, sandboxes). Every call passes ``bounded=False``: a capped capture would
  cut a file without saying so (R02.2).

The shell protocol. Each call prints its answer between lines holding a fresh random sentinel
(``__INDUS_RF_<32 hex>__``) and works in a subshell, so nothing it sets leaks into a persistent
shell. Whatever the backend prints around the fence (login banners, ``TERM`` notices) is ignored.
File bytes travel base64-encoded, and base64 has no ``_``, so no file content can forge a
sentinel; the file's own byte count travels in its own segment and a payload whose decoded length
differs is refused as garbled, never handed to a writer. A backend without ``base64`` (exit 127)
falls back to ``od`` hex. Writes send the bytes on stdin (base64 text, decoded by the remote
shell) into a temp file next to the target, copy an existing file's mode, and rename over it;
a symlink target is written through, never replaced.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import os
import secrets
import shlex
import stat as stat_mod
import weakref
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from indus.backends.contract import CancelSignal, Environment, ExecResult
from indus.foundation import fsio
from indus.foundation.blocking import run_blocking
from indus.foundation.errors import IndusError
from indus.foundation.scope import Scope
from indus.tools.builtin import paths

__all__ = [
    "Environment",
    "is_backend_error",
    "SHELL_TIMEOUT_S",
    "FileOpError",
    "FileOps",
    "FileStat",
    "HostFileOps",
    "ShellFileOps",
    "environment_of",
    "file_ops_for",
    "forget",
    "ops_for_call",
]

SHELL_TIMEOUT_S = 120.0
HOST_DEADLINE_S = 120.0
READ_SENTINEL = "__INDUS_RF_"
WRITE_SENTINEL = "__INDUS_WF_"
PROBE_SENTINEL = "__INDUS_FP_"
_FALLBACK_UMASK = 0o022

type ErrorKind = Literal["missing", "not_regular", "is_dir", "failed", "garbled"]
type StatKind = Literal["file", "dir", "other", "missing"]


def is_backend_error(exc: BaseException) -> bool:
    """True for the backend's own failures (``backend.*`` codes: unreachable, unavailable)."""
    return isinstance(exc, IndusError) and exc.code.startswith("backend.")


class FileOpError(Exception):
    """A file operation that did not happen; ``kind`` says why."""

    def __init__(self, kind: ErrorKind, path: str, detail: str = "") -> None:
        self.kind: ErrorKind = kind
        self.path = path
        self.detail = detail
        super().__init__(detail or f"{path}: {kind}")


@dataclass(frozen=True, slots=True)
class FileStat:
    kind: StatKind
    size: int = 0


class FileOps(ABC):
    """Read, write and inspect files where the task's commands run."""

    host: bool = False

    @property
    @abstractmethod
    def cwd(self) -> str:
        """The task's current working directory (relative paths anchor here)."""

    @abstractmethod
    async def read_bytes(self, path: str) -> bytes:
        """The file's exact bytes; ``FileOpError`` when it is missing, not regular or unreadable."""

    @abstractmethod
    async def write_bytes(self, path: str, data: bytes) -> bool:
        """Replace the file atomically (parents created); True when a directory was created."""

    @abstractmethod
    async def stat(self, path: str) -> FileStat: ...

    @abstractmethod
    async def sha256(self, path: str) -> str | None:
        """Hex digest of the file, or None when it is missing or not a regular file."""

    @abstractmethod
    async def list_dir(self, path: str) -> list[str]:
        """Entry names in a directory (empty when it cannot be listed)."""

    @abstractmethod
    async def delete(self, path: str) -> None:
        """Remove the directory entry ``path`` (a symlink itself, never its target);
        ``FileOpError`` when it is missing or a directory."""


# -- host ------------------------------------------------------------------------------------------


def _process_umask() -> int:
    try:
        text = Path("/proc/self/status").read_text(encoding="ascii", errors="replace")
    except OSError:
        return _FALLBACK_UMASK
    for line in text.splitlines():
        if line.startswith("Umask:"):
            with contextlib.suppress(ValueError):
                return int(line.split()[1], 8)
    return _FALLBACK_UMASK


def _host_read(path: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except (FileNotFoundError, NotADirectoryError):
        if os.path.islink(path):
            raise FileOpError("not_regular", path, f"{path} is a dangling symlink") from None
        raise FileOpError("missing", path, f"{path} does not exist") from None
    except IsADirectoryError:
        raise FileOpError("is_dir", path, f"{path} is a directory") from None
    except OSError as exc:
        raise FileOpError("failed", path, f"{path}: {exc.strerror or exc}") from None
    try:
        mode = os.fstat(fd).st_mode
        if stat_mod.S_ISDIR(mode):
            raise FileOpError("is_dir", path, f"{path} is a directory")
        if not stat_mod.S_ISREG(mode):
            raise FileOpError("not_regular", path, f"{path} is not a regular file")
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "rb") as handle:
        return handle.read()


def _host_write(path: str, data: bytes) -> bool:
    target = os.path.realpath(path)
    parent = os.path.dirname(target) or "."
    created = not os.path.isdir(parent)
    if created:
        os.makedirs(parent, exist_ok=True)
    mode: int | None = None
    try:
        existing = os.stat(target)
    except FileNotFoundError:
        existing = None
    if existing is not None and not stat_mod.S_ISREG(existing.st_mode):
        raise FileOpError("not_regular", path, f"{path} exists and is not a regular file")
    if existing is None:
        mode = 0o666 & ~_process_umask()
    fsio.atomic_write_bytes(target, data, mode=mode)
    return created


def _host_stat(path: str) -> FileStat:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return FileStat("other") if os.path.islink(path) else FileStat("missing")
    except OSError:
        return FileStat("missing")
    if stat_mod.S_ISREG(st.st_mode):
        return FileStat("file", st.st_size)
    if stat_mod.S_ISDIR(st.st_mode):
        return FileStat("dir")
    return FileStat("other")


def _host_sha256(path: str) -> str | None:
    try:
        data = _host_read(path)
    except FileOpError:
        return None
    return hashlib.sha256(data).hexdigest()


def _host_delete(path: str) -> None:
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            raise FileOpError("is_dir", path, f"{path} is a directory")
        os.unlink(path)
    except FileNotFoundError:
        raise FileOpError("missing", path, f"{path} does not exist") from None
    except OSError as exc:
        raise FileOpError("failed", path, f"{path}: {exc.strerror or exc}") from None


def _host_list(path: str) -> list[str]:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


class HostFileOps(FileOps):
    """Direct file system access on the Indus host."""

    host = True

    def __init__(self, cwd: str | Callable[[], str], *, env: Environment | None = None) -> None:
        self._cwd = cwd
        self.host_env = env

    @property
    def cwd(self) -> str:
        return self._cwd() if callable(self._cwd) else self._cwd

    async def _run[T](self, fn: Callable[[str], T], path: str, label: str) -> T:
        return await run_blocking(fn, path, pool="io", deadline=HOST_DEADLINE_S, label=label)

    async def read_bytes(self, path: str) -> bytes:
        return await self._run(_host_read, path, "file read")

    async def write_bytes(self, path: str, data: bytes) -> bool:
        def write(target: str) -> bool:
            return _host_write(target, data)

        try:
            return await self._run(write, path, "file write")
        except FileOpError:
            raise
        except OSError as exc:
            raise FileOpError("failed", path, f"{path}: {exc.strerror or exc}") from None

    async def stat(self, path: str) -> FileStat:
        return await self._run(_host_stat, path, "file stat")

    async def sha256(self, path: str) -> str | None:
        return await self._run(_host_sha256, path, "file hash")

    async def list_dir(self, path: str) -> list[str]:
        return await self._run(_host_list, path, "list dir")

    async def delete(self, path: str) -> None:
        await self._run(_host_delete, path, "file delete")


# -- shell ---------------------------------------------------------------------------------------


def _sentinel(prefix: str) -> str:
    return f"{prefix}{secrets.token_hex(16)}__"


def _segments(output: str, sentinel: str) -> list[str]:
    """The text between sentinel lines (noise before the first and after the last dropped)."""
    parts = output.replace("\r\n", "\n").split(sentinel + "\n")
    if output.rstrip("\n").endswith(sentinel):
        tail = parts[-1]
        parts[-1:] = tail.rsplit(sentinel, 1)
    return parts[1:-1] if len(parts) >= 3 else []


def _decode_b64(text: str) -> bytes | None:
    compact = "".join(text.split())
    try:
        return base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        return None


def _decode_hex(text: str) -> bytes | None:
    try:
        return bytes.fromhex("".join(text.split()))
    except ValueError:
        return None


def _first_word(text: str) -> str:
    words = text.split()
    return words[0] if words else ""


class ShellFileOps(FileOps):
    """File access through the environment's shell (see the module notes)."""

    host = False

    def __init__(
        self,
        env: Environment,
        *,
        timeout: float = SHELL_TIMEOUT_S,
        cancel: CancelSignal | None = None,
    ) -> None:
        self.cancel = cancel
        self.env = env
        self.timeout = timeout

    @property
    def cwd(self) -> str:
        return self.env.cwd

    async def _exec(self, script: str, *, stdin: str | None = None) -> ExecResult:
        return await self.env.execute(
            script, timeout=self.timeout, stdin=stdin, bounded=False, cwd=None, cancel=self.cancel
        )

    async def _read_with(self, path: str, dump: str) -> tuple[str, str, list[str]]:
        mark = _sentinel(READ_SENTINEL)
        q = shlex.quote(path)
        script = (
            f"( {{ set +x; }} 2>/dev/null; S={mark}; p={q}; "
            'if [ -d "$p" ]; then echo "$S"; echo dir; '
            'elif [ -f "$p" ]; then echo "$S"; echo file; '
            f'{dump} < "$p" 2>&1; rc=$?; echo "$S"; wc -c < "$p"; echo "$S"; echo "$rc"; '
            'elif [ -e "$p" ] || [ -L "$p" ]; then echo "$S"; echo other; '
            'else echo "$S"; echo missing; fi; echo "$S" )'
        )
        result = await self._exec(script)
        parts = _segments(result.output, mark)
        if not parts:
            raise FileOpError("failed", path, self._failure_text(path, result))
        head, _, payload = parts[0].partition("\n")
        return head.strip(), payload, parts[1:]

    @staticmethod
    def _failure_text(path: str, result: ExecResult) -> str:
        text = result.output.strip()
        if len(text) > 400:
            text = text[:400] + " …"
        return (
            f"{path}: the backend gave no usable answer (exit {result.returncode}) {text}".strip()
        )

    async def read_bytes(self, path: str) -> bytes:
        data = await self._read_transport(path, "base64", _decode_b64)
        if data is None:
            data = await self._read_transport(path, "od -An -v -tx1", _decode_hex)
        if data is None:
            raise FileOpError("failed", path, f"{path}: the backend has neither base64 nor od")
        return data

    async def _read_transport(
        self, path: str, dump: str, decode: Callable[[str], bytes | None]
    ) -> bytes | None:
        kind, payload, rest = await self._read_with(path, dump)
        if kind == "missing":
            raise FileOpError("missing", path, f"{path} does not exist")
        if kind == "dir":
            raise FileOpError("is_dir", path, f"{path} is a directory")
        if kind != "file":
            raise FileOpError("not_regular", path, f"{path} is not a regular file")
        if len(rest) != 2:
            raise FileOpError("garbled", path, f"{path}: the backend returned a garbled read")
        size_text, rc_text = _first_word(rest[0]), _first_word(rest[1])
        if rc_text == "127":
            return None
        if rc_text != "0":
            detail = payload.strip() or f"exit {rc_text}"
            raise FileOpError("failed", path, f"{path}: {detail}")
        data = decode(payload)
        if data is None or not size_text.isdigit() or int(size_text) != len(data):
            raise FileOpError("garbled", path, f"{path}: the backend returned a garbled read")
        return data

    async def write_bytes(self, path: str, data: bytes) -> bool:
        mark = _sentinel(WRITE_SENTINEL)
        q = shlex.quote(path)
        script = (
            f"( {{ set +x; }} 2>/dev/null; set -e; S={mark}; t={q}; "
            'if [ -L "$t" ]; then r="$(readlink -f -- "$t" 2>/dev/null || true)"; '
            '[ -n "$r" ] && t="$r"; fi; '
            'd="$(dirname -- "$t")"; made=0; '
            'if [ ! -d "$d" ]; then mkdir -p -- "$d"; made=1; fi; '
            'tmp="$(mktemp "$d/.indus-write.XXXXXX")"; '
            "trap 'rm -f -- \"$tmp\"' EXIT; "
            'if [ -e "$t" ]; then '
            'm="$(stat -c %a -- "$t" 2>/dev/null || stat -f %Lp -- "$t" 2>/dev/null || true)"; '
            'if [ -n "$m" ]; then chmod "$m" "$tmp" 2>/dev/null || true; fi; fi; '
            'base64 -d > "$tmp"; '
            'if [ ! -e "$t" ]; then chmod =rw "$tmp" 2>/dev/null || true; fi; '
            'mv -f -- "$tmp" "$t"; trap - EXIT; echo "$S"; echo "written $made"; echo "$S" )'
        )
        payload = base64.b64encode(data).decode("ascii")
        wrapped = "\n".join(payload[i : i + 76] for i in range(0, len(payload), 76)) + "\n"
        result = await self._exec(script, stdin=wrapped)
        parts = _segments(result.output, mark)
        words = parts[0].split() if parts else []
        if result.returncode != 0 or len(words) != 2 or words[0] != "written":
            raise FileOpError("failed", path, self._failure_text(path, result))
        return words[1] == "1"

    async def stat(self, path: str) -> FileStat:
        mark = _sentinel(PROBE_SENTINEL)
        script = (
            f'( S={mark}; p={shlex.quote(path)}; echo "$S"; '
            'if [ -d "$p" ]; then echo dir 0; '
            'elif [ -f "$p" ]; then echo file $(wc -c < "$p"); '
            'elif [ -e "$p" ] || [ -L "$p" ]; then echo other 0; '
            'else echo missing 0; fi; echo "$S" )'
        )
        result = await self._exec(script)
        parts = _segments(result.output, mark)
        words = parts[0].split() if parts else []
        if len(words) != 2 or words[0] not in ("dir", "file", "other", "missing"):
            raise FileOpError("failed", path, self._failure_text(path, result))
        kind: StatKind = words[0]  # pyright: ignore[reportAssignmentType]
        return FileStat(kind, int(words[1]) if words[1].isdigit() else 0)

    async def sha256(self, path: str) -> str | None:
        mark = _sentinel(PROBE_SENTINEL)
        script = (
            f'( S={mark}; p={shlex.quote(path)}; echo "$S"; '
            'if [ -f "$p" ]; then '
            '(sha256sum -- "$p" 2>/dev/null || shasum -a 256 -- "$p" 2>/dev/null) | cut -c1-64; '
            'else echo none; fi; echo "$S" )'
        )
        result = await self._exec(script)
        parts = _segments(result.output, mark)
        word = _first_word(parts[0]) if parts else ""
        if word == "none":
            return None
        if len(word) == 64 and all(c in "0123456789abcdef" for c in word):
            return word
        try:
            return hashlib.sha256(await self.read_bytes(path)).hexdigest()
        except FileOpError:
            return None

    async def list_dir(self, path: str) -> list[str]:
        mark = _sentinel(PROBE_SENTINEL)
        script = f'( S={mark}; echo "$S"; ls -1A -- {shlex.quote(path)} 2>/dev/null; echo "$S" )'
        result = await self._exec(script)
        parts = _segments(result.output, mark)
        return sorted(line for line in parts[0].splitlines() if line) if parts else []

    async def delete(self, path: str) -> None:
        mark = _sentinel(PROBE_SENTINEL)
        script = (
            f'( S={mark}; p={shlex.quote(path)}; echo "$S"; '
            'if [ -d "$p" ] && [ ! -L "$p" ]; then echo dir; '
            'elif [ -e "$p" ] || [ -L "$p" ]; then rm -f -- "$p" && echo removed || echo failed; '
            'else echo missing; fi; echo "$S" )'
        )
        result = await self._exec(script)
        parts = _segments(result.output, mark)
        word = _first_word(parts[0]) if parts else ""
        if word == "removed":
            return
        if word == "missing":
            raise FileOpError("missing", path, f"{path} does not exist")
        if word == "dir":
            raise FileOpError("is_dir", path, f"{path} is a directory")
        raise FileOpError("failed", path, self._failure_text(path, result))


# -- choosing and caching ----------------------------------------------------------------------

_CACHE: weakref.WeakKeyDictionary[Any, FileOps] = weakref.WeakKeyDictionary()


def file_ops_for(env: Environment, *, cancel: CancelSignal | None = None) -> FileOps:
    """The cached ``FileOps`` for ``env``: host access for a host-local backend, else the shell.
    A call's ``cancel`` signal gets its own (uncached) shell object so it stops that call only."""
    if cancel is not None and not (env.flags.host_local and not env.flags.container_paths):
        return ShellFileOps(env, cancel=cancel)
    try:
        cached = _CACHE.get(env)
    except TypeError:  # not weak-referenceable: no caching
        cached = None
    if cached is not None:
        return cached
    flags = env.flags
    ops: FileOps
    if flags.host_local and not flags.container_paths:
        ops = HostFileOps(lambda: env.cwd, env=env)
    else:
        ops = ShellFileOps(env)
    with contextlib.suppress(TypeError):
        _CACHE[env] = ops
    return ops


def environment_of(ops: FileOps) -> Environment | None:
    """The live terminal environment behind ``ops``, if any: helper commands (the shell
    lint-after-edit checkers) run there. Host access without a live environment has none."""
    if isinstance(ops, ShellFileOps):
        return ops.env
    if isinstance(ops, HostFileOps):
        return ops.host_env
    return None


def forget(env: Environment) -> None:
    """Drop the cached object for ``env`` (call on environment teardown)."""
    with contextlib.suppress(TypeError, KeyError):
        del _CACHE[env]


async def ops_for_call(
    scope: Scope | None,
    *,
    task_id: str | None = None,
    session_id: str | None = None,
    environments: Any = None,
    cancel: CancelSignal | None = None,
) -> FileOps:
    """The ``FileOps`` one tool call works through.

    ``environments`` is the host's ``EnvironmentRegistry`` and ``cancel`` the turn's stop signal
    (handler keywords ``environments`` / ``cancel``, offered through ``ToolContext.extras``).
    Without it, or for a ``local`` backend with no live environment yet, the host is used
    directly with the configured ``terminal.cwd`` as anchor, so a read never has to start a shell.
    """
    if environments is None or scope is None:
        return HostFileOps(paths.base_dir(scope))
    backend = str(environments.backend_name(scope))
    key = environments.key_for(scope, task_id=task_id, session_id=session_id)
    live: Mapping[Any, Environment] = environments.live()
    env = live.get(key)
    if env is None and backend == "local":
        return HostFileOps(paths.base_dir(scope))
    if env is None:
        _, env = await environments.acquire(scope, task_id=task_id, session_id=session_id)
    return file_ops_for(env, cancel=cancel)
