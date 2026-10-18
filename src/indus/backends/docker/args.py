# pyright: strict
"""The security and resource half of ``docker run`` (P11-02): a pure function of the settings and
the host facts, so every combination is pinned by an argv golden.

Security (always): every capability dropped, ``DAC_OVERRIDE``, ``CHOWN`` and ``FOWNER`` added back
(root inside must still work on files it does not own), plus ``SETUID``/``SETGID`` so an image's
init can drop privileges — not when the container already runs as the host user; privilege gain
off (``no-new-privileges``) except under snap-packaged Docker, whose AppArmor profile breaks every
exec with it; size-capped tmpfs for ``/tmp`` (512 MB, exec allowed: builds run there),
``/var/tmp`` (256 MB) and ``/run`` (64 MB, exec only for an image that boots its own s6 ``/init``).

Resources: ``--cpus``, ``--memory`` and ``--pids-limit 2048`` only when the host's cgroups allow
them (an unprivileged LXC would refuse every start); ``--shm-size`` unless set in the extra args
or configured empty / "0"; a disk quota only when the storage probe passed (else one warning).
The PID limit follows the code, not the docs (2048, R02.23): browsers inside need the room.

Network: ``docker_network: false`` adds ``--network=none``. Docker refuses a repeated
``--network`` (exit 125), so an extra-args network flag asking for anything else is refused here,
before anything starts (R02.20).
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Sequence
from dataclasses import dataclass

from indus.backends.docker.probe import HostFacts
from indus.backends.docker.settings import DockerConfigError, DockerSettings

__all__ = ["PIDS_LIMIT", "RunArgs", "host_user_spec", "network_from_extra", "run_args"]

log = logging.getLogger(__name__)

PIDS_LIMIT = 2048
BASE_CAPS = ("DAC_OVERRIDE", "CHOWN", "FOWNER")
PRIVDROP_CAPS = ("SETUID", "SETGID")
TMP_TMPFS = "/tmp:rw,nosuid,exec,size=512m"  # noqa: S108 - a container mount spec
VAR_TMP_TMPFS = "/var/tmp:rw,noexec,nosuid,size=256m"  # noqa: S108 - a container mount spec
RUN_TMPFS_NOEXEC = "/run:rw,noexec,nosuid,size=64m"
RUN_TMPFS_EXEC = "/run:rw,exec,nosuid,size=64m"
NETWORK_FLAGS = ("--network", "--net")

_warned_lock = threading.Lock()
_warned_storage = False


@dataclass(frozen=True, slots=True)
class RunArgs:
    """``args``: our flags in order; ``extra``: the user's extra args, which go after every other
    flag (mounts and env names included) so they can still override a default; ``init``: whether
    ``docker run`` gets ``--init``."""

    args: tuple[str, ...]
    extra: tuple[str, ...]
    init: bool


def host_user_spec() -> str | None:
    """``uid:gid`` of the user running Indus, when the platform has POSIX ids."""
    get_uid = getattr(os, "getuid", None)
    get_gid = getattr(os, "getgid", None)
    if get_uid is None or get_gid is None:
        return None
    return f"{get_uid()}:{get_gid()}"


def network_from_extra(extra: Sequence[str]) -> str | None:
    """The network mode the extra args ask for, or None when they set none."""
    for i, arg in enumerate(extra):
        for flag in NETWORK_FLAGS:
            if arg == flag:
                return extra[i + 1] if i + 1 < len(extra) else ""
            if arg.startswith(flag + "="):
                return arg.split("=", 1)[1]
    return None


def _sets_shm(extra: Sequence[str]) -> bool:
    return any(a == "--shm-size" or a.startswith("--shm-size=") for a in extra)


def _warn_no_quota_once() -> None:
    global _warned_storage
    with _warned_lock:
        if _warned_storage:
            return
        _warned_storage = True
    log.warning(
        "this host's container storage cannot cap disk use per container (that needs overlay2 "
        "on XFS with project quotas); sandboxes run without a disk limit"
    )


def _number(value: float) -> str:
    return f"{value:g}"


def run_args(
    settings: DockerSettings, facts: HostFacts, *, host_user: str | None = None
) -> RunArgs:
    """The flags for ``settings`` on a host with ``facts`` (see the module notes).

    ``host_user`` is the ``uid:gid`` used with ``docker_run_as_host_user`` (default: this
    process's ids). Raises ``DockerConfigError`` when the extra args contradict the network
    setting."""
    extra = list(settings.extra_args)
    user: str | None = None
    if settings.run_as_host_user:
        user = host_user if host_user is not None else host_user_spec()
        if user is None:
            log.warning("docker_run_as_host_user is on but this platform has no POSIX ids")

    args: list[str] = ["--cap-drop", "ALL"]
    for cap in BASE_CAPS:
        args += ["--cap-add", cap]
    if user is None:
        for cap in PRIVDROP_CAPS:
            args += ["--cap-add", cap]
    if not settings.snap_compat:
        args += ["--security-opt", "no-new-privileges"]
    args += ["--tmpfs", TMP_TMPFS, "--tmpfs", VAR_TMP_TMPFS]
    args += ["--tmpfs", RUN_TMPFS_EXEC if facts.s6_init else RUN_TMPFS_NOEXEC]
    if user is not None:
        args += ["--user", user]

    if facts.cgroups:
        if settings.cpu > 0:
            args += ["--cpus", _number(settings.cpu)]
        if settings.memory_mb > 0:
            args += ["--memory", f"{settings.memory_mb}m"]
        args += ["--pids-limit", str(PIDS_LIMIT)]
    shm = settings.shm_size.strip()
    if shm and shm != "0" and not _sets_shm(extra):
        args += ["--shm-size", shm]
    if settings.disk_mb > 0:
        if facts.storage_quota:
            args += ["--storage-opt", f"size={settings.disk_mb}m"]
        else:
            _warn_no_quota_once()

    if not settings.network:
        asked = network_from_extra(extra)
        if asked is None:
            args.append("--network=none")
        elif asked != "none":
            raise DockerConfigError(
                f"terminal.docker_network is false (no network) but terminal.docker_extra_args "
                f"asks for --network {asked!r}",
                hint="remove the --network entry from docker_extra_args, or set docker_network "
                "to true",
            )
    return RunArgs(tuple(args), tuple(extra), init=not (facts.s6_init or settings.snap_compat))
