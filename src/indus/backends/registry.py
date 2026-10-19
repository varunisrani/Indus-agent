# pyright: strict
"""Which environment a command runs in, and how long it lives (P05-04).

Backends are rows (``BackendEntry``: name, async factory, flags, optional probe); container and
cloud backends (P11, P21) only add rows. The backend is chosen by ``terminal.backend`` in the
call's ``Scope`` config, never by process environment variables (D-16.75).

Keys (``EnvKey``) always start with the profile home, so two profiles never share an
environment (R02.8): calls with per-task overrides (passed in, or registered in
``task_overrides.TASK_OVERRIDES`` for the task or the session's bound task, P21-14) get their
task's own key and their factory the planned scope and overrides; a backend whose row
says it is ``profile_scoped`` for the scope (a persistent container, P11) gets one ``profile`` key
that a session's release leaves alone; everything else the session's key (or ``default``), and
host-local control calls a separate ``host`` key. Creation
happens under one asyncio lock per key with a re-check; every use stamps the activity time.

The idle reaper wakes every 60 s and cleans environments unused for ``terminal.lifetime_s``
(300 s) unless a background process of that key is still alive (then the time is refreshed).

A connection-class failure (``EnvironmentConnectionError``) never reaches the model as a
traceback: the environment is evicted and the call answers ``status: "degraded"`` with a retry
hint (``terminal.degraded_mode: fail`` answers ``status: "error"`` instead); the next call builds
a fresh environment. A backend that cannot be created here answers ``status: "disabled"``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from indus.backends.contract import (
    ActivityCallback,
    BackendFlags,
    BackendUnavailable,
    CancelSignal,
    Environment,
    EnvironmentConnectionError,
    ExecResult,
)
from indus.backends.local import LOCAL_FLAGS, open_local
from indus.backends.task_overrides import TASK_OVERRIDES, plan
from indus.backends.wait import FOREGROUND
from indus.foundation.clock import Clock, SystemClock
from indus.foundation.config.schema import default_for
from indus.foundation.scope import Scope, current_scope

__all__ = [
    "CLEANUP_BOUND_S",
    "REAP_EVERY_S",
    "BackendEntry",
    "BackendStatus",
    "CreateRequest",
    "EnvKey",
    "EnvironmentRegistry",
    "default_entries",
]

log = logging.getLogger(__name__)

REAP_EVERY_S = 60.0
CLEANUP_BOUND_S = 15.0

type KeyKind = Literal["task", "session", "profile", "host"]


@dataclass(frozen=True, slots=True)
class EnvKey:
    """Where one environment is cached: profile home + what it serves."""

    home: str
    kind: KeyKind
    ident: str
    backend: str

    def __str__(self) -> str:
        return f"{self.home}::{self.kind}:{self.ident}@{self.backend}"


@dataclass(frozen=True, slots=True)
class CreateRequest:
    """What a factory gets to build one environment."""

    scope: Scope
    key: EnvKey
    task_id: str | None
    session_id: str | None
    overrides: Mapping[str, Any]


type Factory = Callable[[CreateRequest], Awaitable[Environment]]


@dataclass(frozen=True, slots=True)
class BackendEntry:
    """One backend row. ``probe`` returns why the backend cannot run here, or None;
    ``profile_scoped`` says whether one environment serves the whole profile;
    ``cleanup_bound_s`` replaces the registry's cleanup bound for this backend's environments
    (SDK sandboxes sync back and snapshot when they end, P21-12)."""

    name: str
    factory: Factory
    flags: BackendFlags
    probe: Callable[[Scope], str | None] | None = None
    profile_scoped: Callable[[Scope], bool] | None = None
    cleanup_bound_s: float | None = None


@dataclass(frozen=True, slots=True)
class BackendStatus:
    """A call that did not run a command: the backend is degraded, disabled or failed."""

    status: Literal["degraded", "disabled", "error"]
    error: str
    reason: str = ""
    retry_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        if self.status == "degraded":
            return {
                "output": "",
                "exit_code": -1,
                "status": "degraded",
                "reason": self.reason,
                "retry_hint": self.retry_hint,
                "error": self.error,
            }
        return {"output": "", "exit_code": -1, "status": self.status, "error": self.error}


class BackgroundProbe(Protocol):
    def __call__(self, key: EnvKey, /) -> bool: ...


def _no_background(key: EnvKey, /) -> bool:
    return False


async def _create_local(request: CreateRequest) -> Environment:
    cwd = request.overrides.get("cwd")
    return await open_local(
        request.scope,
        session_id=request.session_id,
        cwd=str(cwd) if cwd else None,
    )


async def _create_ssh(request: CreateRequest) -> Environment:
    from indus.backends.ssh.environment import open_ssh

    return await open_ssh(request.scope, session_id=request.session_id, overrides=request.overrides)


# Flags of rows whose module loads only when the backend is used (kept in sync by a test).
SSH_ROW_FLAGS = BackendFlags(
    isolated=False, container_paths=True, stdin_mode="pipe", host_local=False
)


def default_entries() -> list[BackendEntry]:
    from indus.backends.docker import docker_entry
    from indus.backends.remote_probes import guard_entry
    from indus.backends.sdk_entries import sdk_entries
    from indus.backends.singularity import singularity_entry

    return [
        BackendEntry("local", _create_local, LOCAL_FLAGS),
        docker_entry(),
        BackendEntry("ssh", _create_ssh, SSH_ROW_FLAGS),
        # The cloud rows' probes sit behind the flapping guard (P21-12, R02.13 / R02.29).
        *(guard_entry(entry) for entry in (singularity_entry(), *sdk_entries())),
    ]


def _resolved(
    scope: Scope,
    task_id: str | None,
    session_id: str | None,
    overrides: Mapping[str, Any] | None,
) -> tuple[str | None, Mapping[str, Any] | None, Scope]:
    """(task id, call overrides, factory scope) for one call. Explicit overrides win; else an
    override registered for the task, or for the task the session is bound to (P21-14)."""
    if overrides:
        return task_id, overrides, scope
    found = TASK_OVERRIDES.resolve(scope, task_id) or TASK_OVERRIDES.resolve(scope, session_id)
    if found is None:
        return task_id, overrides, scope
    task, override = found
    planned = plan(scope, task, override)
    return task, planned.overrides, planned.scope


def _config(scope: Scope, key: str) -> Any:
    value = scope.config.get(key) if scope.config is not None else None
    return default_for(key) if value is None else value


@dataclass
class _Slot:
    env: Environment
    lifetime_s: float
    last_used: float
    owner: EnvKey = field(repr=False)


class EnvironmentRegistry:
    """The process's table of live environments (see the module notes)."""

    def __init__(
        self,
        entries: list[BackendEntry] | None = None,
        *,
        clock: Clock | None = None,
        background_alive: BackgroundProbe = _no_background,
        cleanup_bound_s: float = CLEANUP_BOUND_S,
    ) -> None:
        self._entries: dict[str, BackendEntry] = {}
        for entry in entries if entries is not None else default_entries():
            self.register(entry)
        self.clock: Clock = clock if clock is not None else SystemClock()
        self.background_alive = background_alive
        self.cleanup_bound_s = cleanup_bound_s
        self._slots: dict[EnvKey, _Slot] = {}
        self._locks: dict[EnvKey, asyncio.Lock] = {}
        self._reaper: asyncio.Task[None] | None = None
        self._closed = False

    # -- rows --------------------------------------------------------------------------------------

    def register(self, entry: BackendEntry) -> None:
        name = entry.name.strip().lower()
        if not name:
            raise ValueError("a backend needs a name")
        if name in self._entries:
            raise ValueError(f"terminal backend {name!r} is already registered")
        self._entries[name] = entry

    def names(self) -> list[str]:
        return sorted(self._entries)

    def entry(self, name: str) -> BackendEntry | None:
        return self._entries.get(name.strip().lower())

    # -- keys --------------------------------------------------------------------------------------

    def key_for(
        self,
        scope: Scope,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
        overrides: Mapping[str, Any] | None = None,
        host_local: bool = False,
    ) -> EnvKey:
        home = str(scope.home)
        if host_local:
            return EnvKey(home, "host", session_id or task_id or "default", "local")
        task_id, overrides, _ = _resolved(scope, task_id, session_id, overrides)
        backend = self.backend_name(scope, overrides)
        if overrides and task_id:
            return EnvKey(home, "task", task_id, backend)
        entry = self._entries.get(backend)
        if entry is not None and entry.profile_scoped is not None and entry.profile_scoped(scope):
            return EnvKey(home, "profile", "shared", backend)
        return EnvKey(home, "session", session_id or "default", backend)

    @staticmethod
    def backend_name(scope: Scope, overrides: Mapping[str, Any] | None = None) -> str:
        chosen = (overrides or {}).get("backend") or _config(scope, "terminal.backend") or "local"
        return str(chosen).strip().lower()

    # -- use -------------------------------------------------------------------------------------

    def live(self) -> dict[EnvKey, Environment]:
        return {key: slot.env for key, slot in self._slots.items()}

    async def acquire(
        self,
        scope: Scope | None = None,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
        overrides: Mapping[str, Any] | None = None,
        host_local: bool = False,
    ) -> tuple[EnvKey, Environment]:
        """The environment for this call, created on first use. Raises ``BackendUnavailable``
        (unknown or unusable backend) or ``EnvironmentConnectionError`` (creation could not
        reach it); an unresolved scope raises ``ScopeUnresolved``."""
        if self._closed:
            raise BackendUnavailable("terminal environments are shut down")
        scope = scope if scope is not None else current_scope()
        if getattr(scope.config, "failed", False):
            # Defaults after a failed read could swap a configured sandbox for the host.
            raise BackendUnavailable(
                "this profile's config could not be read, so its terminal settings are unknown; "
                "fix config.yaml and try again"
            )
        create_scope = scope
        if not host_local:
            task_id, overrides, create_scope = _resolved(scope, task_id, session_id, overrides)
        key = self.key_for(
            scope,
            task_id=task_id,
            session_id=session_id,
            overrides=overrides,
            host_local=host_local,
        )
        slot = self._slots.get(key)
        if slot is not None:
            slot.last_used = self.clock.monotonic()
            return key, slot.env
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            slot = self._slots.get(key)
            if slot is not None:
                slot.last_used = self.clock.monotonic()
                return key, slot.env
            entry = self._entries.get(key.backend)
            if entry is None:
                raise BackendUnavailable(
                    f"unknown terminal backend {key.backend!r} (available: "
                    f"{', '.join(self.names()) or 'none'})"
                )
            if entry.probe is not None:
                problem = entry.probe(create_scope)
                if problem:
                    raise BackendUnavailable(problem)
            request = CreateRequest(create_scope, key, task_id, session_id, dict(overrides or {}))
            try:
                env = await entry.factory(request)
            except ImportError as exc:
                raise BackendUnavailable(
                    f"the {key.backend} backend needs a package that is not installed ({exc})"
                ) from exc
            lifetime = float(_config(scope, "terminal.lifetime_s"))
            self._slots[key] = _Slot(env, lifetime, self.clock.monotonic(), key)
            log.info("%s environment ready for %s", key.backend, key.kind)
            self._ensure_reaper()
            return key, env

    async def run(
        self,
        command: str,
        *,
        scope: Scope | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        overrides: Mapping[str, Any] | None = None,
        host_local: bool = False,
        timeout: float | None = None,
        cwd: str | None = None,
        stdin: str | None = None,
        bounded: bool = True,
        on_activity: ActivityCallback | None = None,
        cancel: CancelSignal | None = None,
    ) -> ExecResult | BackendStatus:
        """Acquire and run; backend trouble becomes a ``BackendStatus`` (see module notes)."""
        scope = scope if scope is not None else current_scope()
        key: EnvKey | None = None
        try:
            key, env = await self.acquire(
                scope,
                task_id=task_id,
                session_id=session_id,
                overrides=overrides,
                host_local=host_local,
            )
            result = await env.execute(
                command,
                timeout=timeout,
                cwd=cwd,
                stdin=stdin,
                bounded=bounded,
                on_activity=on_activity,
                cancel=cancel,
            )
        except BackendUnavailable as exc:
            return BackendStatus("disabled", f"Terminal backend disabled: {exc}")
        except EnvironmentConnectionError as exc:
            if key is not None:
                await self.evict(key)
            log.warning("terminal backend degraded: %s", exc.reason)
            if str(_config(scope, "terminal.degraded_mode")).strip().lower() == "fail":
                return BackendStatus("error", f"Terminal backend failed: {exc.reason}")
            return BackendStatus(
                "degraded",
                f"Terminal backend degraded: {exc.reason}",
                reason=exc.reason,
                retry_hint=exc.retry_hint,
            )
        self.touch(key)
        return result

    def touch(self, key: EnvKey) -> None:
        slot = self._slots.get(key)
        if slot is not None:
            slot.last_used = self.clock.monotonic()

    # -- ending ------------------------------------------------------------------------------------

    async def evict(self, key: EnvKey) -> bool:
        """Drop ``key``'s environment (after a failure, or on release) and clean it up."""
        slot = self._slots.pop(key, None)
        self._locks.pop(key, None)
        if slot is None:
            return False
        await self._cleanup(slot.env, key)
        return True

    async def release(
        self,
        scope: Scope,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
        overrides: Mapping[str, Any] | None = None,
    ) -> bool:
        """End the environment a call with these ids would use (session end, ``/new``). A
        profile-wide environment serves other sessions too and is left to the idle reaper."""
        key = self.key_for(scope, task_id=task_id, session_id=session_id, overrides=overrides)
        if key.kind == "profile":
            return False
        return await self.evict(key)

    async def reap_idle(self) -> list[EnvKey]:
        """One reaper round: clean environments idle past their lifetime; returns their keys."""
        now = self.clock.monotonic()
        stale: list[tuple[EnvKey, _Slot]] = []
        for key, slot in list(self._slots.items()):
            if now - slot.last_used <= slot.lifetime_s:
                continue
            try:
                busy = self.background_alive(key)
            except Exception:
                log.debug("background probe failed for %s", key, exc_info=True)
                busy = False
            if busy:
                slot.last_used = now
                continue
            stale.append((key, slot))
        for key, _ in stale:  # unregister first, clean up outside the table
            self._slots.pop(key, None)
            self._locks.pop(key, None)
        for key, slot in stale:
            await self._cleanup(slot.env, key)
        return [key for key, _ in stale]

    async def _reap_forever(self) -> None:
        while True:
            await self.clock.sleep(REAP_EVERY_S)
            try:
                await self.reap_idle()
            except Exception:
                log.warning("terminal reaper round failed", exc_info=True)

    def _ensure_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.ensure_future(self._reap_forever())

    def cleanup_bound_for(self, key: EnvKey) -> float:
        """How long the cleanup of ``key``'s environment may take (its row's own bound, if any)."""
        entry = self._entries.get(key.backend)
        if entry is not None and entry.cleanup_bound_s is not None:
            return entry.cleanup_bound_s
        return self.cleanup_bound_s

    async def _cleanup(self, env: Environment, key: EnvKey) -> None:
        bound = self.cleanup_bound_for(key)
        task = asyncio.ensure_future(env.cleanup())
        timer = asyncio.ensure_future(self.clock.sleep(bound))
        try:
            await asyncio.wait({task, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            timer.cancel()
        if not task.done():
            task.cancel()
            log.warning("cleanup of %s did not finish in %gs; abandoned", key, bound)
            return
        if not task.cancelled() and task.exception() is not None:
            log.warning("cleanup of %s failed", key, exc_info=task.exception())

    async def shutdown(self, *, fence: bool = True) -> None:
        """Stop the reaper, fence foreground commands (process exit; ``fence=False`` for a
        registry that is not the host's), clean every environment (bounded)."""
        self._closed = True
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reaper
            self._reaper = None
        if fence:
            FOREGROUND.fence_and_kill()
        slots = list(self._slots.items())
        self._slots.clear()
        self._locks.clear()
        await asyncio.gather(*(self._cleanup(slot.env, key) for key, slot in slots))
