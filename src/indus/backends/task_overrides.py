# pyright: strict
"""Per-task image / backend / cwd overrides (P21-14).

One task (a research dataset row, a delegated task) can ask for its own sandbox: another container
image, another backend or another working directory. ``TaskOverrides`` holds those per profile
home and task id; a session that runs the task is bound to it. The environment registry asks
``TASK_OVERRIDES`` on every call that names no overrides itself: a call of an overridden task (or
of a session bound to one) gets its own environment key, ``task:<task id>``, so it never reuses the
default sandbox, and its factory is handed the override through ``plan``:

* docker: the image as ``terminal.docker_image`` in a config overlay on the factory's scope,
  unless the profile pins its image (``terminal.docker_image_pinned``): a pin is the profile's
  rule, so the pinned image stays and the task's image is ignored with one warning;
* singularity: ``terminal.singularity_image`` = ``docker://<image>`` (a ref with a scheme is kept);
* modal, daytona: the call override ``image``;
* vercel_sandbox, local, ssh: the image is ignored with one warning per backend and process.

``cwd`` and ``backend`` become call overrides of the same names. Every planned mapping carries
``task`` so the call is keyed per task even when nothing else is left.

A session started in a folder of its own (a desktop chat's project folder or default directory,
W47-b) uses the same kind of overlay: ``with_workdir`` answers ``terminal.cwd`` with that folder on
the scope its tool calls get, so relative paths and new shells start there rather than in the
folder the serving process happens to run in. When that folder has been deleted since,
``workdir_gone`` says so: the tools fall back to ``terminal.cwd`` and the turn shows a notice.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from indus.foundation.config.schema import default_for
from indus.foundation.scope import ConfigSource, Scope

__all__ = [
    "IMAGE_IN_CALL",
    "IMAGE_IN_CONFIG",
    "TASK_OVERRIDES",
    "LayeredConfig",
    "TaskOverride",
    "TaskOverrides",
    "TaskPlan",
    "backend_for",
    "plan",
    "register_row",
    "reset_warnings",
    "with_workdir",
    "workdir_gone",
]

log = logging.getLogger(__name__)

TASK_KEY = "task"
# Backends whose image comes from config (overlaid per task) or from the call override ``image``.
IMAGE_IN_CONFIG: Mapping[str, str] = {
    "docker": "terminal.docker_image",
    "singularity": "terminal.singularity_image",
}
IMAGE_IN_CALL = frozenset({"modal", "daytona"})
PINNED_WARNING = "docker:pinned"


@dataclass(frozen=True, slots=True)
class TaskOverride:
    """What one task asks for; None leaves the profile's setting."""

    image: str | None = None
    backend: str | None = None
    cwd: str | None = None

    @property
    def empty(self) -> bool:
        return not (self.image or self.backend or self.cwd)


@dataclass
class _Table:
    overrides: dict[tuple[str, str], TaskOverride] = field(
        default_factory=dict[tuple[str, str], TaskOverride]
    )
    sessions: dict[tuple[str, str], str] = field(default_factory=dict[tuple[str, str], str])


class TaskOverrides:
    """Overrides per (profile home, task id) and the sessions bound to a task."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._table = _Table()

    def __len__(self) -> int:
        with self._lock:
            return len(self._table.overrides)

    def set(self, scope: Scope, task_id: str, override: TaskOverride) -> None:
        if not task_id:
            raise ValueError("a task override needs a task id")
        with self._lock:
            self._table.overrides[(str(scope.home), task_id)] = override

    def bind(self, scope: Scope, session_id: str, task_id: str) -> None:
        """Calls of ``session_id`` run as ``task_id`` (until the task is cleared)."""
        with self._lock:
            self._table.sessions[(str(scope.home), session_id)] = task_id

    def get(self, scope: Scope, task_id: str) -> TaskOverride | None:
        with self._lock:
            return self._table.overrides.get((str(scope.home), task_id))

    def resolve(self, scope: Scope, ident: str | None) -> tuple[str, TaskOverride] | None:
        """``ident`` as a task id with an override, or as a session bound to one."""
        if not ident:
            return None
        home = str(scope.home)
        with self._lock:
            found = self._table.overrides.get((home, ident))
            if found is not None:
                return ident, found
            task = self._table.sessions.get((home, ident))
            if task is None:
                return None
            found = self._table.overrides.get((home, task))
            return (task, found) if found is not None else None

    def clear(self, scope: Scope, task_id: str) -> bool:
        """Drop ``task_id``'s override and every session bound to it."""
        home = str(scope.home)
        with self._lock:
            dropped = self._table.overrides.pop((home, task_id), None) is not None
            for key in [
                k for k, t in self._table.sessions.items() if k[0] == home and t == task_id
            ]:
                del self._table.sessions[key]
            return dropped

    def reset(self) -> None:
        with self._lock:
            self._table = _Table()


TASK_OVERRIDES = TaskOverrides()


# -- planning -------------------------------------------------------------------------------------


class LayeredConfig:
    """A config view with a few dotted keys answered from ``overlay`` first."""

    def __init__(self, base: ConfigSource | None, overlay: Mapping[str, object]) -> None:
        self.base = base
        self.overlay = dict(overlay)

    def get(self, path: str, /) -> object:
        if path in self.overlay:
            return self.overlay[path]
        return self.base.get(path) if self.base is not None else None

    def __getattr__(self, name: str) -> Any:  # e.g. ``failed`` on a config that could not be read
        if name.startswith("__") or self.base is None:
            raise AttributeError(name)
        return getattr(self.base, name)


@dataclass(frozen=True, slots=True)
class TaskPlan:
    """The scope a factory is built with and the call overrides for one task."""

    scope: Scope
    overrides: Mapping[str, Any]


_warned: set[str] = set()
_warned_lock = threading.Lock()


def reset_warnings() -> None:
    with _warned_lock:
        _warned.clear()


def _warn_once(backend: str, image: str) -> None:
    with _warned_lock:
        if backend in _warned:
            return
        _warned.add(backend)
    log.warning(
        "the %s backend runs no container image of yours; the per-task image %s is ignored",
        backend,
        image,
    )


def _pinned(scope: Scope) -> bool:
    value = scope.config.get("terminal.docker_image_pinned") if scope.config is not None else None
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _warn_pinned(image: str) -> None:
    with _warned_lock:
        if PINNED_WARNING in _warned:
            return
        _warned.add(PINNED_WARNING)
    log.warning(
        "this profile pins its Docker image (terminal.docker_image_pinned); the per-task image %s "
        "is ignored",
        image,
    )


def backend_for(scope: Scope, override: TaskOverride) -> str:
    """The backend a task runs on: its own choice, else the profile's ``terminal.backend``."""
    if override.backend:
        return override.backend.strip().lower()
    value = scope.config.get("terminal.backend") if scope.config is not None else None
    if value is None:
        value = default_for("terminal.backend")
    return str(value or "local").strip().lower()


def _singularity_ref(image: str) -> str:
    return image if "://" in image else f"docker://{image}"


def plan(scope: Scope, task_id: str, override: TaskOverride) -> TaskPlan:
    """How ``override`` reaches the factory of ``task_id``'s environment (module notes)."""
    calls: dict[str, Any] = {TASK_KEY: task_id}
    if override.backend:
        calls["backend"] = override.backend.strip().lower()
    if override.cwd:
        calls["cwd"] = override.cwd
    image = (override.image or "").strip()
    if not image:
        return TaskPlan(scope, calls)
    backend = backend_for(scope, override)
    if backend in IMAGE_IN_CALL:
        calls["image"] = image
        return TaskPlan(scope, calls)
    key = IMAGE_IN_CONFIG.get(backend)
    if key is None:
        _warn_once(backend, image)
        return TaskPlan(scope, calls)
    if backend == "docker" and _pinned(scope):
        _warn_pinned(image)
        return TaskPlan(scope, calls)
    value = _singularity_ref(image) if backend == "singularity" else image
    layered = LayeredConfig(scope.config, {key: value})
    return TaskPlan(scope.with_config(layered), calls)


def with_workdir(scope: Scope, folder: str | None) -> Scope:
    """``scope`` with ``terminal.cwd`` answered by a session's own ``folder`` (module notes).

    Only the local backend reads ``terminal.cwd`` as a host folder; a sandbox's cwd is a path
    inside the sandbox, so other backends keep their own. A folder that is not an existing
    absolute directory leaves the scope unchanged (the profile's ``terminal.cwd`` still applies)."""
    if not folder or not os.path.isabs(folder) or not os.path.isdir(folder):
        return scope
    if backend_for(scope, TaskOverride()) != "local":
        return scope
    return scope.with_config(LayeredConfig(scope.config, {"terminal.cwd": folder}))


def workdir_gone(scope: Scope, folder: str | None) -> bool:
    """True when ``with_workdir`` would use ``folder`` (an absolute folder on the local backend)
    but it no longer exists, so the tools fall back to ``terminal.cwd`` (W48-c: a notice says
    so)."""
    if not folder or not os.path.isabs(folder) or os.path.isdir(folder):
        return False
    return backend_for(scope, TaskOverride()) == "local"


def register_row(scope: Scope, task_id: str, image: str | None, cwd: str | None) -> str | None:
    """Register a dataset row's image and cwd for ``task_id`` (the batch runner's seam);
    returns why it cannot, or None."""
    override = TaskOverride(image=(image or "").strip() or None, cwd=(cwd or "").strip() or None)
    if override.empty:
        return None
    TASK_OVERRIDES.set(scope, task_id, override)
    return None
