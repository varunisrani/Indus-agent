# pyright: strict
"""When the container goes away, and how isolated the backend is (P11-06).

A container can vanish under a running session (``docker rm``, a prune, an OOM kill, a daemon
restart). The client then answers before the command ever starts, with an error that opens its
output: "No such container" or "container … is not running" (Podman: "no container with name or
ID"). ``container_gone`` matches only there, so a command that merely prints those words is not
mistaken for it. With ``docker_persist_across_processes`` the environment recovers: a reusable
labelled container (another process may have made one) or a fresh one, a new snapshot, and the
command once more; that result carries ``environment_recreated`` so the tool warns that
background processes and unsaved files may be gone (R02.19). Jobs the old container ran are
reported to the lost handlers (``register_lost_handler``), which mark them ``lost``.

``daemon_gone`` spots a client that cannot reach its daemon at all; the environment turns that
into a connection error, so the registry answers "degraded" and evicts it.

Isolation (R02.6): the environment is ``isolated`` only when no host path is bound
(``MountPlan.host_paths_mounted``). Approval prompts are skipped only then; the hardline floor
and the user's deny rules still apply (R02.4).
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

from indus.backends.contract import BackendFlags

if TYPE_CHECKING:
    from indus.backends.docker.environment import DockerEnvironment
    from indus.backends.processes import ProcessRegistry

__all__ = [
    "LOST_SOURCE",
    "container_gone",
    "daemon_gone",
    "isolated_flags",
    "mark_lost",
    "notify_lost",
    "recover",
    "register_lost_handler",
    "unregister_lost_handler",
]

log = logging.getLogger(__name__)

CONTAINER_GONE = ("no such container", "is not running", "no container with name or id")
DAEMON_GONE = (
    "cannot connect to the docker daemon",
    "is the docker daemon running",
    "error during connect",
    "cannot connect to podman",
    "unable to connect to podman",
)
LOST_SOURCE = "the sandbox container was replaced"

type LostHandler = Callable[[str], object]

_handlers_lock = threading.Lock()
_handlers: list[LostHandler] = []


def _first_line(output: str) -> str:
    text = output.lstrip()
    return text.split("\n", 1)[0][:500].lower()


def container_gone(output: str, returncode: int) -> bool:
    """The client said the container does not exist or is stopped (see the module notes)."""
    if returncode == 0:
        return False
    line = _first_line(output)
    return line.startswith("error") and any(p in line for p in CONTAINER_GONE)


def daemon_gone(output: str, returncode: int) -> bool:
    """The client could not reach its daemon."""
    if returncode == 0:
        return False
    line = _first_line(output)
    return any(p in line for p in DAEMON_GONE)


def isolated_flags(host_paths_mounted: bool) -> BackendFlags:
    """The flags of a Docker environment: isolated only without host binds."""
    return BackendFlags(
        isolated=not host_paths_mounted,
        container_paths=True,
        stdin_mode="pipe",
        host_local=False,
    )


# -- lost background jobs -----------------------------------------------------------------------


def register_lost_handler(handler: LostHandler) -> None:
    """Called with the environment key whose container was replaced."""
    with _handlers_lock:
        if handler not in _handlers:
            _handlers.append(handler)


def unregister_lost_handler(handler: LostHandler) -> None:
    with _handlers_lock:
        if handler in _handlers:
            _handlers.remove(handler)


def notify_lost(env_key: str) -> None:
    with _handlers_lock:
        handlers = list(_handlers)
    for handler in handlers:
        try:
            handler(env_key)
        except Exception:
            log.warning("a lost-job handler failed for %s", env_key, exc_info=True)


def mark_lost(registry: ProcessRegistry, env_key: str, *, source: str = LOST_SOURCE) -> list[str]:
    """End every running job of ``env_key`` in ``registry`` as ``lost``; returns their ids.

    Their output so far is kept and a job that asked for a completion message still gets one."""
    ended: list[str] = []
    for record in registry.running():
        if record.env_key != env_key:
            continue
        handle = record.handle
        if handle is not None:
            with contextlib.suppress(Exception):
                handle.force_stop()
        record.reason = "lost"
        record.source = source
        registry.notice_exit(record)
        ended.append(record.id)
    return ended


# -- recovery -----------------------------------------------------------------------------------


async def recover(env: DockerEnvironment) -> bool:
    """Put a working container behind ``env`` (see the module notes); False when that failed,
    so the caller returns the original result."""
    old = env.container_id or ""
    log.warning("sandbox container %s is gone; recovering", old[:12] or "(none)")
    env.forget_container()
    try:
        found = await env.lifecycle.find_reusable()
        container: str | None = None
        if found is not None:
            cid, state = found
            if state == "running" or await env.lifecycle.start(cid):
                container = cid
        if container is None:
            container = await env.lifecycle.create()
    except Exception as exc:
        log.error("could not replace the sandbox container: %s", exc)
        return False
    env.adopt_container(container)
    try:
        await env.restart_snapshot()
    except Exception as exc:
        log.error("the new sandbox container did not take a snapshot: %s", exc)
        return False
    env.recreations += 1
    notify_lost(env.env_key)  # jobs are ended as lost here, not left to the one-shot flag
    env.mark_recreated()
    log.info("sandbox container replaced by %s", container[:12])
    return True
