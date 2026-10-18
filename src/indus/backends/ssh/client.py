# pyright: strict
"""OpenSSH client argv and control-socket paths for the SSH backend (P11-08).

Pure functions: nothing here starts a process or touches the network.

Multiplexing: every connection to one target shares a master connection through a control socket
(``ControlMaster=auto``, kept ``CONTROL_PERSIST_S`` after its last client). A master forwards only
the ``SendEnv`` names it was started with, so each distinct set of names gets its own master and
its own socket. Socket paths must stay under the 104-byte ``sun_path`` limit of macOS (Linux
allows 108), and ssh adds a random suffix of up to ``SSH_TEMP_SUFFIX`` bytes while it creates the
socket, so names are hashes and the directory falls back to a short one when the runtime dir is
long. The Windows client has no Unix-socket multiplexing: no ``Control*`` options there.

Secrets (R02.7): the argv carries only ``SendEnv=NAME``; the value lives in the client's own
environment, which the caller builds.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "CONNECT_TIMEOUT_S",
    "CONTROL_PERSIST_S",
    "SOCKET_PATH_LIMIT",
    "SSH_TEMP_SUFFIX",
    "SshTarget",
    "base_options",
    "control_dir",
    "control_path",
    "exit_argv",
    "multiplex_supported",
    "scp_argv",
    "send_env_names",
    "ssh_argv",
]

CONTROL_PERSIST_S = 300
CONNECT_TIMEOUT_S = 10
SOCKET_PATH_LIMIT = 104
SSH_TEMP_SUFFIX = 17  # "." + 16 random characters while the master binds
RUNTIME_DIR_BUDGET = 50
SOCKET_SUBDIR = "indus-ssh"


@dataclass(frozen=True, slots=True)
class SshTarget:
    """Where commands go: ``user@host:port``, optionally with an identity file."""

    host: str
    user: str = ""
    port: int = 22
    key_path: str = ""

    @property
    def destination(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    @property
    def label(self) -> str:
        return f"{self.destination}:{self.port}"


def multiplex_supported(os_name: str) -> bool:
    """Control sockets work everywhere but the Windows client (``os_name`` as ``os.name``)."""
    return os_name != "nt"


def send_env_names(names: Iterable[str]) -> tuple[str, ...]:
    """Sorted, de-duplicated names that are safe to put in a ``SendEnv`` option."""
    return tuple(sorted({n for n in names if n and n.isascii() and n.isidentifier()}))


def _short_hash(text: str, size: int) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:size]


def socket_name(target: SshTarget, send_env: Sequence[str] = ()) -> str:
    """``<16 hex of the target>`` plus ``-<8 hex of the names>`` when there are SendEnv names."""
    name = _short_hash(f"{target.destination}:{target.port}", 16)
    names = send_env_names(send_env)
    if names:
        name += "-" + _short_hash(" ".join(names), 8)
    return name


def _fits(directory: str) -> bool:
    longest = len(directory) + 1 + len(SOCKET_SUBDIR) + 1 + 16 + 9 + SSH_TEMP_SUFFIX
    return longest < SOCKET_PATH_LIMIT


def control_dir(runtime_dir: str | None, fallback: str) -> str:
    """``<runtime dir>/indus-ssh``: ``runtime_dir`` (``XDG_RUNTIME_DIR``) when it is absolute and
    short, else ``fallback`` (the profile's short socket dir), else ``/tmp``."""
    for candidate in (runtime_dir, fallback, "/tmp"):
        if not candidate or not os.path.isabs(candidate):
            continue
        candidate = candidate.rstrip("/") or "/"
        if len(candidate) <= RUNTIME_DIR_BUDGET and _fits(candidate):
            return os.path.join(candidate, SOCKET_SUBDIR)
    return os.path.join("/tmp", SOCKET_SUBDIR)


def control_path(directory: str, target: SshTarget, send_env: Sequence[str] = ()) -> str:
    return os.path.join(directory, socket_name(target, send_env))


def socket_dir_usable(directory: Path, *, uid: int | None = None) -> bool:
    """Create ``directory`` owner-only when missing; refuse one that someone else owns or that
    others may write to (a planted socket would capture our connection)."""
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
    except OSError:
        return False
    if not stat.S_ISDIR(info.st_mode):
        return False
    owner = os.getuid() if uid is None and hasattr(os, "getuid") else uid
    if owner is not None and info.st_uid != owner:
        return False
    if info.st_mode & 0o022:
        try:
            directory.chmod(0o700)
        except OSError:
            return False
    return True


def base_options(
    target: SshTarget,
    *,
    control: str | None,
    send_env: Sequence[str] = (),
) -> list[str]:
    """The options every ssh call shares (multiplexing only when ``control`` is given)."""
    argv: list[str] = []
    if control:
        argv += [
            "-o",
            f"ControlPath={control}",
            "-o",
            "ControlMaster=auto",
            "-o",
            f"ControlPersist={CONTROL_PERSIST_S}",
        ]
    argv += [
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={CONNECT_TIMEOUT_S}",
    ]
    for name in send_env_names(send_env):
        argv += ["-o", f"SendEnv={name}"]
    argv += ["-p", str(target.port)]
    if target.key_path:
        argv += ["-i", target.key_path]
    return argv


def ssh_argv(
    ssh: str,
    target: SshTarget,
    remote_command: str,
    *,
    control: str | None,
    send_env: Sequence[str] = (),
) -> list[str]:
    """``ssh <options> <destination> <remote command>``; the command is one already-quoted
    string (ssh joins its trailing words with spaces for the remote shell)."""
    return [
        ssh,
        *base_options(target, control=control, send_env=send_env),
        target.destination,
        remote_command,
    ]


def scp_argv(
    scp: str, target: SshTarget, local_path: str, remote_path: str, *, control: str | None
) -> list[str]:
    """``scp`` of one file over the target's plain master (port flag ``-P``)."""
    argv = [scp]
    if control:
        argv += ["-o", f"ControlPath={control}"]
    argv += [
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={CONNECT_TIMEOUT_S}",
        "-P",
        str(target.port),
    ]
    if target.key_path:
        argv += ["-i", target.key_path]
    argv += ["-q", local_path, f"{target.destination}:{remote_path}"]
    return argv


def exit_argv(ssh: str, target: SshTarget, control: str) -> list[str]:
    """Ask the master behind ``control`` to exit."""
    return [ssh, "-o", f"ControlPath={control}", "-O", "exit", target.destination]
