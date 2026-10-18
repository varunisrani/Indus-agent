# pyright: strict
"""Every Docker setting of a profile, read once from its scope (P11-01..05).

``terminal.docker_*`` and ``terminal.container_*`` come from the scope's config with the schema
defaults behind them (one defaults table). Odd values are normalised here, so the argument
builders below work on plain, typed data and stay pure.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, cast

from indus.backends.contract import BackendError
from indus.foundation.config.schema import default_for
from indus.foundation.scope import Scope

__all__ = ["DockerConfigError", "DockerSettings", "settings_for"]

log = logging.getLogger(__name__)

DIGEST_MARK = "@sha256:"


class DockerConfigError(BackendError):
    """The Docker settings contradict each other; nothing was started."""

    default_code = "backend.docker.config"


@dataclass(frozen=True, slots=True)
class DockerSettings:
    image: str
    image_pinned: bool = False
    binary: str = ""
    forward_env: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=lambda: {})
    volumes: tuple[str, ...] = ()
    extra_args: tuple[str, ...] = ()
    shm_size: str = "1g"
    network: bool = True
    run_as_host_user: bool = False
    snap_compat: bool = False
    mount_cwd_to_workspace: bool = False
    persist_across_processes: bool = True
    shared_key: str = ""
    orphan_reaper: bool = True
    cpu: float = 1.0
    memory_mb: int = 5120
    disk_mb: int = 51200
    persistent: bool = True


def _value(scope: Scope, key: str) -> Any:
    raw = scope.config.get(key) if scope.config is not None else None
    return default_for(key) if raw is None else raw


def _strings(raw: object, key: str) -> tuple[str, ...]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, Iterable):
        log.warning("%s should be a list; ignored", key)
        return ()
    out: list[str] = []
    for item in cast(Iterable[object], raw):
        if isinstance(item, str):
            out.append(item)
        else:
            log.warning("%s: ignoring a non-text entry (%s)", key, type(item).__name__)
    return tuple(out)


def _mapping(raw: object, key: str) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        if raw not in (None, ""):
            log.warning("%s should be a mapping; ignored", key)
        return {}
    out: dict[str, str] = {}
    for name, value in cast(Mapping[object, object], raw).items():
        if not isinstance(name, str):
            continue
        if isinstance(value, bool):
            out[name] = "true" if value else "false"
        elif isinstance(value, str | int | float):
            out[name] = str(value)
        else:
            log.warning("%s.%s: only text and numbers can be set; ignored", key, name)
    return out


def _number(raw: object, fallback: float) -> float:
    try:
        return max(0.0, float(cast(Any, raw)))
    except (TypeError, ValueError):
        return fallback


def _flag(raw: object) -> bool:
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return bool(raw)


def settings_for(scope: Scope) -> DockerSettings:
    """The profile's Docker settings; raises ``DockerConfigError`` for a pinned image without a
    digest."""
    t = "terminal."
    image = str(_value(scope, t + "docker_image") or "").strip() or str(
        default_for(t + "docker_image")
    )
    pinned = _flag(_value(scope, t + "docker_image_pinned"))
    if pinned and DIGEST_MARK not in image:
        raise DockerConfigError(
            f"terminal.docker_image_pinned is on but {image!r} names no digest",
            hint="use an image reference ending in @sha256:<digest>, or turn the pin off",
        )
    shm = _value(scope, t + "docker_shm_size")
    return DockerSettings(
        image=image,
        image_pinned=pinned,
        binary=str(_value(scope, t + "docker_binary") or "").strip(),
        forward_env=_strings(_value(scope, t + "docker_forward_env"), t + "docker_forward_env"),
        env=_mapping(_value(scope, t + "docker_env"), t + "docker_env"),
        volumes=_strings(_value(scope, t + "docker_volumes"), t + "docker_volumes"),
        extra_args=_strings(_value(scope, t + "docker_extra_args"), t + "docker_extra_args"),
        shm_size="" if shm is None else str(shm).strip(),
        network=_flag(_value(scope, t + "docker_network")),
        run_as_host_user=_flag(_value(scope, t + "docker_run_as_host_user")),
        snap_compat=_flag(_value(scope, t + "docker_snap_compat")),
        mount_cwd_to_workspace=_flag(_value(scope, t + "docker_mount_cwd_to_workspace")),
        persist_across_processes=_flag(_value(scope, t + "docker_persist_across_processes")),
        shared_key=str(_value(scope, t + "docker_shared_key") or "").strip(),
        orphan_reaper=_flag(_value(scope, t + "docker_orphan_reaper")),
        cpu=_number(_value(scope, t + "container_cpu"), 1.0),
        memory_mb=int(_number(_value(scope, t + "container_memory_mb"), 5120)),
        disk_mb=int(_number(_value(scope, t + "container_disk_mb"), 51200)),
        persistent=_flag(_value(scope, t + "container_persistent")),
    )
