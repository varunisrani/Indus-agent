# pyright: strict
"""The Docker (and Podman) terminal backend (P11-01..07).

``docker_entry()`` is the registry row: its factory opens a ``DockerEnvironment``
(``environment.py``), its probe checks the client and daemon with the flake grace (``probe.py``),
and ``profile_scoped`` gives a persistent container one key per profile. The row's flags are the
defaults of a container without host binds; each environment reports its own flags, which turn
``isolated`` off when a host path is mounted (R02.6).

Modules: ``settings`` (config), ``probe`` (client, daemon, host facts, doctor), ``args`` (security
and resources), ``mounts``, ``env`` (names only), ``environment`` (lifecycle), ``recovery``
(vanished containers, isolation), ``reaper`` (old containers).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from indus.backends.contract import BackendFlags, Environment
from indus.foundation.scope import Scope

if TYPE_CHECKING:
    from indus.backends.registry import BackendEntry, CreateRequest

__all__ = ["DOCKER_FLAGS", "docker_entry", "persistent_per_profile"]

DOCKER_FLAGS = BackendFlags(
    isolated=True, container_paths=True, stdin_mode="pipe", host_local=False
)


def persistent_per_profile(scope: Scope) -> bool:
    """A persistent container serves the whole profile (one environment key)."""
    raw = scope.config.get("terminal.container_persistent") if scope.config is not None else None
    if raw is None:
        from indus.foundation.config.schema import default_for

        raw = default_for("terminal.container_persistent")
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return bool(raw)


async def _create(request: CreateRequest) -> Environment:
    from indus.backends.docker.environment import open_docker

    return await open_docker(request)


def _probe(scope: Scope) -> str | None:
    from indus.backends.docker.probe import docker_probe

    return docker_probe(scope)


def docker_entry() -> BackendEntry:
    from indus.backends.registry import BackendEntry

    return BackendEntry(
        "docker", _create, DOCKER_FLAGS, probe=_probe, profile_scoped=persistent_per_profile
    )
