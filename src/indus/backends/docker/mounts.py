# pyright: strict
"""What the container sees of the host (P11-03).

Order of the ``-v``/``--tmpfs`` flags: the host working directory (when bound), the user's
volumes, the read-only mounts, and the writable space (persistent bind dirs or tmpfs) — the last
two are returned apart because only the first part is the user's choice.

- User volumes ``host:container[:options]``: a malformed entry is skipped with a warning; ``~`` in
  the host part is the scope's HOME. A host part without a slash is a named Docker volume.
- The host working directory is bound only with ``docker_mount_cwd_to_workspace`` (or when it is a
  Windows drive path, which can never be a container path): at ``/workspace``, or ``/host-cwd``,
  ``-2``, ``-3`` when a volume owns ``/workspace``; a volume that already binds that directory is
  reused instead of a second bind.
- Skills dirs and credential files are mounted read-only. Sources come from the caller and from
  ``register_readonly_source`` (the credential-file registry registers there); a source that is
  missing or of the wrong kind is skipped (Docker would create a directory in its place).
- Writable space: persistent → owner-only dirs ``<home>/sandboxes/docker/<task>/{home,workspace}``
  bound at ``/root`` and ``/workspace``; otherwise tmpfs ``/workspace`` 10 GB, ``/home`` and
  ``/root`` 1 GB. A task key with colons (a session key) is turned into a safe dir name with a
  digest, because ``-v`` reads every colon as a field separator (R02.20).

``host_paths_mounted`` is true when a writable bind exposes host files the user owns (a volume
not marked ``ro``, or the working directory). Then commands can destroy host files and the
backend is not isolated: approval prompts stay on (R02.6).
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from indus.backends.credential_files import mount_specs, resolve
from indus.backends.docker.settings import DockerSettings
from indus.foundation.scope import Scope

__all__ = [
    "HOST_CWD_TARGETS",
    "credential_mounts",
    "MountPlan",
    "ReadonlyMount",
    "plan_mounts",
    "readonly_from_sources",
    "register_readonly_source",
    "sandbox_dir_name",
    "split_volume",
    "unregister_readonly_source",
]

log = logging.getLogger(__name__)

WORKSPACE = "/workspace"
HOST_CWD_TARGETS = ("/host-cwd", "/host-cwd-2", "/host-cwd-3")
NON_PERSISTENT_TMPFS = (
    "/workspace:rw,exec,size=10g",
    "/home:rw,exec,size=1g",
    "/root:rw,exec,size=1g",
)
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")
_MAX_DIR_NAME = 64
_DIGEST_LEN = 12
_VOLUME = re.compile(r"^(?P<host>.+?):(?P<container>/[^:]*)(?::(?P<options>[^:]*))?$")
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


@dataclass(frozen=True, slots=True)
class ReadonlyMount:
    host_path: str
    container_path: str
    kind: Literal["file", "dir"] = "dir"


type ReadonlySource = Callable[[Scope], Iterable[ReadonlyMount]]

_sources_lock = threading.Lock()
_sources: list[ReadonlySource] = []


def register_readonly_source(source: ReadonlySource) -> None:
    """Add a provider of read-only mounts (skills, credential files); registering twice is a
    no-op."""
    with _sources_lock:
        if source not in _sources:
            _sources.append(source)


def unregister_readonly_source(source: ReadonlySource) -> None:
    with _sources_lock:
        if source in _sources:
            _sources.remove(source)


CONTAINER_HOME = "/root"


def _attr(item: object, name: str) -> object:
    return cast(object, getattr(item, name, None))


def _as_mount(spec: object) -> ReadonlyMount | None:
    """One read-only mount from the credential registry's ``mount_specs`` output: a
    ``host:container[:ro]`` text, a (host, container) pair, or an object with ``host_path`` and
    ``container_path``/``remote_rel``. A relative container path is under the container home."""
    host: object = None
    target: object = None
    if isinstance(spec, str):
        parsed = split_volume(spec)
        if parsed is not None:
            host, target = parsed[0], parsed[1]
        elif spec.count(":") >= 1:  # a relative container path: host:rel[:ro]
            host, _, rest = spec.partition(":")
            target = rest.split(":", 1)[0]
    elif isinstance(spec, tuple | list) and len(cast(Sequence[object], spec)) >= 2:
        pair = cast(Sequence[object], spec)
        host, target = pair[0], pair[1]
    else:
        item: object = cast(object, spec)
        host = _attr(item, "host_path")
        target = _attr(item, "container_path") or _attr(item, "remote_rel")
    if not isinstance(host, str | Path) or not isinstance(target, str | Path):
        return None
    host_text, target_text = str(host), str(target)
    if not target_text.startswith("/"):
        target_text = f"{CONTAINER_HOME}/{target_text.removeprefix('./')}"
    return ReadonlyMount(host_text, target_text, "file")


def credential_mounts(scope: Scope, session_id: str | None = None) -> list[ReadonlyMount]:
    """Credential files (P11-11) as read-only mounts below the container's ``~/.indus``, from the
    registry's ``resolve`` and ``mount_specs``."""
    try:
        specs = mount_specs(resolve(scope, session_id), f"{CONTAINER_HOME}/.indus")
    except Exception:
        log.warning("credential files could not be listed; none are mounted", exc_info=True)
        return []
    found: list[ReadonlyMount] = []
    for spec in specs:
        mount = _as_mount(spec)
        if mount is None:
            log.warning("credential mount %r is not understood; skipped", spec)
        else:
            found.append(mount)
    return found


def readonly_from_sources(scope: Scope) -> list[ReadonlyMount]:
    with _sources_lock:
        sources = list(_sources)
    found: list[ReadonlyMount] = []
    for source in sources:
        try:
            found.extend(source(scope))
        except Exception:
            log.warning("a read-only mount source failed; its mounts are skipped", exc_info=True)
    return found


@dataclass(frozen=True, slots=True)
class MountPlan:
    volume_args: tuple[str, ...]
    writable_args: tuple[str, ...]
    readonly_args: tuple[str, ...]
    cwd_target: str | None
    host_cwd: str | None
    host_paths_mounted: bool
    home_dir: str | None = None
    workspace_dir: str | None = None

    @property
    def args(self) -> tuple[str, ...]:
        return (*self.writable_args, *self.volume_args, *self.readonly_args)

    @property
    def default_cwd(self) -> str:
        return self.cwd_target or WORKSPACE


def split_volume(spec: str) -> tuple[str, str, str] | None:
    """``host:container[:options]`` → (host, container, options); a drive-letter host keeps its
    colon. None when the spec has no absolute container path."""
    found = _VOLUME.match(spec.strip())
    if found is None or not found.group("container"):
        return None
    return found.group("host"), found.group("container"), found.group("options") or ""


def is_windows_drive(path: str) -> bool:
    return bool(_DRIVE.match(path))


def sandbox_dir_name(task_key: str) -> str:
    """A bind-mountable dir name for ``task_key``: unchanged when already safe, otherwise cleaned
    and suffixed with a digest so two keys never share a dir (``a:b`` vs ``a_b``)."""
    if not task_key:
        return "default"
    cleaned = _SAFE_NAME.sub("_", task_key)
    if (
        cleaned == task_key
        and len(task_key) <= _MAX_DIR_NAME
        and task_key not in (".", "..")
        and not task_key.endswith(".")
    ):
        return task_key
    digest = hashlib.sha256(task_key.encode("utf-8")).hexdigest()[:_DIGEST_LEN]
    stem = cleaned[: _MAX_DIR_NAME - _DIGEST_LEN - 1].strip("._") or "task"
    return f"{stem}-{digest}"


def _expand(host: str, user_home: str | None) -> str:
    if user_home and (host == "~" or host.startswith("~/")):
        return user_home + host[1:]
    return host


def _is_host_path(host: str) -> bool:
    return host.startswith(("/", ".", "~")) or is_windows_drive(host)


def _key(path: str) -> str:
    if is_windows_drive(path):
        return path.replace("\\", "/").rstrip("/").lower()
    try:
        return os.path.realpath(path)
    except OSError:
        return os.path.normpath(path)


def _private_dir(path: Path) -> str:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.chmod(0o700)
    return str(path)


def _readonly_args(mounts: Sequence[ReadonlyMount]) -> list[str]:
    args: list[str] = []
    seen: set[str] = set()
    for mount in mounts:
        source = Path(mount.host_path)
        if mount.kind == "file":
            problem = None if source.is_file() else "it is not a file"
        else:
            problem = None if source.is_dir() else "it is not a directory"
        if problem:
            log.warning("read-only mount of %s skipped: %s", mount.host_path, problem)
            continue
        if mount.container_path in seen:
            continue
        seen.add(mount.container_path)
        args += ["-v", f"{mount.host_path}:{mount.container_path}:ro"]
    return args


def plan_mounts(
    settings: DockerSettings,
    *,
    home: Path,
    task_key: str,
    host_cwd: str | None = None,
    user_home: str | None = None,
    readonly: Sequence[ReadonlyMount] = (),
) -> MountPlan:
    """The mounts for one container (see the module notes)."""
    volumes: list[tuple[str, str, str]] = []
    for raw in settings.volumes:
        spec = raw.strip()
        if not spec:
            continue
        parsed = split_volume(spec)
        if parsed is None:
            log.warning("docker volume %r is not host:/container[:options]; skipped", spec)
            continue
        host, container, options = parsed
        volumes.append((_expand(host, user_home), container, options))

    def taken(target: str) -> bool:
        want = target.rstrip("/") or "/"
        return any((c.rstrip("/") or "/") == want for _, c, _ in volumes)

    host_paths = any(_is_host_path(h) and "ro" not in o.split(",") for h, _, o in volumes)
    cwd_target: str | None = None
    bound_cwd: str | None = None
    bind_cwd: list[str] = []
    if host_cwd:
        expanded = _expand(host_cwd, user_home)
        drive = is_windows_drive(expanded)
        wanted = settings.mount_cwd_to_workspace or drive
        absolute = expanded if drive else os.path.abspath(expanded)
        if wanted and not drive and not os.path.isdir(absolute):
            log.debug("host cwd %s is not a directory; not mounted", absolute)
            wanted = False
        if wanted:
            existing = next((c for h, c, _ in volumes if _key(h) == _key(absolute)), None)
            if existing is not None:
                cwd_target = existing
            else:
                cwd_target = WORKSPACE
                if taken(WORKSPACE):
                    cwd_target = next(
                        (t for t in HOST_CWD_TARGETS if not taken(t)), HOST_CWD_TARGETS[-1]
                    )
                bind_cwd = ["-v", f"{absolute}:{cwd_target}"]
            bound_cwd = absolute
            host_paths = True

    workspace_free = not taken(WORKSPACE) and cwd_target != WORKSPACE
    writable: list[str] = []
    home_dir: str | None = None
    workspace_dir: str | None = None
    if settings.persistent:
        sandbox = home / "sandboxes" / "docker" / sandbox_dir_name(task_key)
        home_dir = _private_dir(sandbox / "home")
        writable += ["-v", f"{home_dir}:/root"]
        if workspace_free:
            workspace_dir = _private_dir(sandbox / "workspace")
            writable += ["-v", f"{workspace_dir}:{WORKSPACE}"]
    else:
        for spec in NON_PERSISTENT_TMPFS:
            if spec.startswith(WORKSPACE + ":") and not workspace_free:
                continue
            writable += ["--tmpfs", spec]

    volume_args = list(bind_cwd)
    for host, container, options in volumes:
        volume_args += ["-v", f"{host}:{container}" + (f":{options}" if options else "")]
    return MountPlan(
        volume_args=tuple(volume_args),
        writable_args=tuple(writable),
        readonly_args=tuple(_readonly_args(readonly)),
        cwd_target=cwd_target,
        host_cwd=bound_cwd,
        host_paths_mounted=host_paths,
        home_dir=home_dir,
        workspace_dir=workspace_dir,
    )
