"""Each served profile's scope, and the one fail-closed reader for its secrets (P24-02).

**Scopes.** A ``Scope`` carries a profile's home, env view, config, secret store and terminal
policy together (R05.14). The launch profile's scope is the process scope. A secondary's scope
(``build_served_scope``) is built from a short list of operating-system names taken from the launch
env (``SECONDARY_OS_NAMES``: PATH, locale, terminal, user, temp and XDG dirs, CA bundle paths —
no proxies, no credentials, nothing ``INDUS_``), plus ``INDUS_HOME`` pinned to its home, plus its
own ``.env`` and config bridges. The process environment belongs to the launch profile and is
never consulted for a secondary (R06.26): a launch-profile token or allowlist is not even present
in a secondary's view, and nothing here writes ``os.environ`` (adapter settings are never bridged
into the process env). ``ProfileScopeCache`` keeps one scope per served profile.

**The reader.** ``ScopedReader(scope).get(name, default)`` is the one way to read a credential or
an allowlist: the profile's secret store first, then its env view; a miss is ``default``. For a
routed (secondary) scope the env rung reads only what the profile's own ``.env`` and config put in
the view (the overlay), never inherited values, so even a scope built the wrong way fails closed.

**Binding activities.** ``ScopeBinder`` runs every kind of work for a served profile under its
scope (R01.16, R03.23): turns, agent eviction and shutdown finalisation (memory flushes land in
that profile's stores), media delivery, deferred callbacks (on the scope's clock), cron ticks,
background threads, child process environments (``safety.child_env`` over the profile's view) and
availability probes.

**Probes before any scope** (R02.29): ``ProbeGate.run`` outside a scope logs at debug, answers
None and remembers the probe; ``on_scoped_turn(scope)`` runs the owed probes once per profile,
under that profile's scope.

**Inbound media.** A shared bot downloads into its own profile's cache; ``adopt_inbound_media``
moves files from that cache into the routed profile's cache (same relative place, a fresh name on
a clash, owner-only) and repoints the event's attachment refs. Anything else (URLs, files outside
the cache, symlinks) is left as it is.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import os
import shutil
import threading
from collections.abc import Awaitable, Callable, Coroutine, Mapping, MutableMapping
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from indus.foundation.bootstrap import attach_profile_state
from indus.foundation.clock import Clock
from indus.foundation.envload import build_env
from indus.foundation.home import HOME_ENV
from indus.foundation.profiles import profile_home, profile_id_of
from indus.foundation.scope import (
    EnvView,
    Scope,
    bind_scope,
    scope_or_none,
    spawn_with_scope,
    thread_with_scope,
    use_scope,
)
from indus.gateway.core.events import InboundEvent
from indus.safety import child_env as safety_child_env

__all__ = [
    "ACTIVITIES",
    "SECONDARY_OS_NAMES",
    "ProbeGate",
    "ProfileScopeCache",
    "ScopeBinder",
    "ScopedReader",
    "adopt_inbound_media",
    "build_served_scope",
    "scoped_reader_for",
    "secondary_base_env",
]

log = logging.getLogger(__name__)

SECONDARY_OS_NAMES: Final = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "LC_MESSAGES",
        "LC_NUMERIC",
        "LC_TIME",
        "TZ",
        "TERM",
        "COLORTERM",
        "TMPDIR",
        "TMP",
        "TEMP",
        "XDG_RUNTIME_DIR",
        "XDG_STATE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
    }
)
"""Process names a secondary profile inherits from the launch env: where things are and how text
is shown, never a credential, a proxy or a product setting."""

ACTIVITIES: Final = (
    "turn",
    "eviction",
    "finalisation",
    "media_delivery",
    "deferred_callback",
    "cron_tick",
    "child_process",
    "background_thread",
    "probe",
)
"""Every kind of per-profile work ``ScopeBinder`` binds (research 06 §3.26)."""


# -- building scopes ------------------------------------------------------------------------------


def secondary_base_env(launch_env: Mapping[str, str]) -> dict[str, str]:
    """The names a secondary inherits (``SECONDARY_OS_NAMES``), nothing else."""
    return {name: value for name, value in launch_env.items() if name in SECONDARY_OS_NAMES}


def _same_dir(a: Path, b: Path) -> bool:
    if a == b:
        return True
    try:
        return a.resolve(strict=False) == b.resolve(strict=False)
    except OSError:
        return False


type StateAttacher = Callable[[Scope, MutableMapping[str, str]], Scope]


def build_served_scope(
    launch: Scope,
    home: Path,
    *,
    clock: Clock | None = None,
    attach: StateAttacher = attach_profile_state,
) -> Scope:
    """The scope of the profile at ``home`` as served by the process whose scope is ``launch``.

    The launch home gives back ``launch`` itself. Any other home gets a routed scope whose env is
    ``SECONDARY_OS_NAMES`` from the launch env + ``INDUS_HOME`` + that profile's ``.env`` and
    config bridges, with its own config view and secret store (``attach``). ``clock`` replaces the
    profile's zone-aware system clock (tests pass a fake one)."""
    if _same_dir(home, launch.home):
        return launch
    inherited = secondary_base_env(launch.env.base)
    inherited[HOME_ENV] = str(home)
    draft = Scope(
        profile_id=profile_id_of(home),
        home=home,
        root=launch.root,
        env=build_env(home, inherited),
        clock=clock if clock is not None else launch.clock,
        routed=True,
    )
    scope = attach(draft, dict(inherited))
    if clock is not None:
        scope = replace(scope, clock=clock)
    return replace(scope, terminal_policy=_terminal_policy(scope), routed=True)


def _terminal_policy(scope: Scope) -> Mapping[str, Any]:
    """The profile's own ``terminal`` config section (read-only), else the scope's current one."""
    config = scope.config
    if config is None:
        return scope.terminal_policy
    try:
        section = config.get("terminal")
    except Exception:
        return scope.terminal_policy
    if isinstance(section, Mapping):
        items: Mapping[Any, Any] = section  # pyright: ignore[reportUnknownVariableType]
        return MappingProxyType({str(k): v for k, v in items.items()})
    return scope.terminal_policy


class ProfileScopeCache:
    """One scope per served profile of the process whose scope is ``launch``."""

    def __init__(
        self,
        launch: Scope,
        *,
        build: Callable[[Scope, Path], Scope] = build_served_scope,
    ) -> None:
        self.launch = launch
        self._build = build
        self._scopes: dict[str, Scope] = {launch.profile_id: launch}
        self._lock = threading.Lock()

    def get(self, profile: str) -> Scope:
        """The scope of ``profile`` (built on first use)."""
        with self._lock:
            found = self._scopes.get(profile)
            if found is not None:
                return found
            home = profile_home(self.launch.root, profile)
            scope = self._build(self.launch, home)
            self._scopes[profile] = scope
            return scope

    def drop(self, profile: str) -> None:
        """Forget ``profile``'s scope (unserved); the launch scope is kept."""
        if profile == self.launch.profile_id:
            return
        with self._lock:
            self._scopes.pop(profile, None)

    def profiles(self) -> list[str]:
        with self._lock:
            return sorted(self._scopes)


# -- reading --------------------------------------------------------------------------------------


class ScopedReader:
    """Credentials and allowlists of one profile (see the module notes)."""

    def __init__(self, scope: Scope) -> None:
        self.scope = scope

    def _env_value(self, name: str) -> str | None:
        env = self.scope.env
        if not self.scope.routed:
            return env.get(name)
        if isinstance(env, EnvView):
            return env.overlay.get(name)
        return None

    def get(self, name: str, default: str | None = None) -> str | None:
        """The profile's value for ``name``; ``default`` when its scope has none. Blank = none."""
        if not name:
            return default
        store = self.scope.secrets
        if store is not None:
            try:
                value = store.get(name)
            except Exception as exc:
                log.warning("secret store could not be read for %s: %s", name, type(exc).__name__)
                value = None
            if value and value.strip():
                return value.strip()
        value = self._env_value(name)
        if value and value.strip():
            return value.strip()
        return default

    def flag(self, name: str, *, default: bool = False) -> bool:
        """A true/false switch; anything but a known word is ``default``."""
        raw = self.get(name)
        if raw is None:
            return default
        word = raw.strip().lower()
        if word in {"1", "true", "yes", "on"}:
            return True
        if word in {"0", "false", "no", "off"}:
            return False
        return default


def scoped_reader_for(scope: Scope) -> Callable[[str], str | None]:
    """``ScopedReader(scope).get`` as the policy reader ``Authorizer(env=)`` takes."""
    reader = ScopedReader(scope)
    return lambda name: reader.get(name)


# -- binding activities ---------------------------------------------------------------------------


class ScopeBinder:
    """Runs each kind of work for one served profile under that profile's scope."""

    def __init__(self, scope: Scope) -> None:
        self.scope = scope

    def run_turn[T](
        self, coro: Coroutine[Any, Any, T], *, name: str | None = None
    ) -> asyncio.Task[T]:
        """A turn as a task bound to the profile."""
        return spawn_with_scope(coro, scope=self.scope, name=name)

    def deliver_media[T](self, coro: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        """A media delivery (reads the profile's caches) as a task bound to the profile."""
        return spawn_with_scope(coro, scope=self.scope, name="media delivery")

    def cron_tick[T](self, coro: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        """One cron tick of the profile's jobs."""
        return spawn_with_scope(coro, scope=self.scope, name=f"cron tick {self.scope.profile_id}")

    async def evict[T](self, flush: Callable[[], T]) -> T:
        """Flush an evicted agent's memory (blocking I/O) on a thread, under the profile."""
        return await asyncio.to_thread(bind_scope(flush, scope=self.scope))

    async def finalise[T](self, flush: Callable[[], T]) -> T:
        """Shutdown finalisation of the profile's sessions, like ``evict``."""
        return await asyncio.to_thread(bind_scope(flush, scope=self.scope))

    def defer(
        self, seconds: float, callback: Callable[[], Awaitable[object] | object]
    ) -> asyncio.Task[None]:
        """Run ``callback`` after ``seconds`` on the profile's clock, under the profile."""

        async def later() -> None:
            await self.scope.clock.sleep(seconds)
            produced = callback()
            if asyncio.iscoroutine(produced) or isinstance(produced, asyncio.Future):
                await produced

        return spawn_with_scope(later(), scope=self.scope, name="deferred callback")

    def thread(
        self, fn: Callable[..., object], *args: object, name: str | None = None
    ) -> threading.Thread:
        """An unstarted daemon thread that runs ``fn(*args)`` under the profile."""
        return thread_with_scope(fn, *args, scope=self.scope, name=name, daemon=True)

    def child_env(
        self, *, extra: Mapping[str, str] | None = None, session_id: str | None = None
    ) -> dict[str, str]:
        """The environment of a child process acting for the profile (R02.22, R03.23)."""
        return safety_child_env.child_env(self.scope, extra=extra, session_id=session_id)

    def call[T](self, fn: Callable[..., T], *args: object) -> T:
        """Call ``fn`` now under the profile (a probe, a synchronous callback)."""
        with use_scope(self.scope):
            return fn(*args)


class ProbeGate:
    """Availability probes that may first run before any profile scope exists (R02.29)."""

    def __init__(self) -> None:
        self._owed: dict[str, Callable[[], object]] = {}
        self._ran: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    def run(self, name: str, check: Callable[[], object]) -> object | None:
        """``check()`` under the current scope; None (quietly, remembered) when there is none."""
        if scope_or_none() is None:
            log.debug("probe %s ran before any profile scope; it runs again later", name)
            with self._lock:
                self._owed[name] = check
            return None
        try:
            return check()
        except Exception as exc:
            log.warning("probe %s failed: %s", name, type(exc).__name__)
            return None

    def owed(self) -> list[str]:
        with self._lock:
            return sorted(self._owed)

    def on_scoped_turn(self, scope: Scope) -> dict[str, object]:
        """Run each owed probe once for ``scope``'s profile, under its scope."""
        with self._lock:
            due = [
                (name, check)
                for name, check in self._owed.items()
                if (name, scope.profile_id) not in self._ran
            ]
            self._ran.update((name, scope.profile_id) for name, _ in due)
        results: dict[str, object] = {}
        for name, check in due:
            with use_scope(scope):
                try:
                    results[name] = check()
                except Exception as exc:
                    log.warning(
                        "probe %s failed for profile %s: %s",
                        name,
                        scope.profile_id,
                        type(exc).__name__,
                    )
        return results


# -- inbound media --------------------------------------------------------------------------------


def _free_name(target: Path) -> Path:
    if not target.exists():
        return target
    for n in range(1, 10_000):
        candidate = target.with_name(f"{target.stem}-{n}{target.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"no free name for {target.name}")


def _move(source: Path, target: Path) -> None:
    try:
        os.replace(source, target)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        shutil.copy2(source, target)
        source.unlink()
    with contextlib.suppress(OSError):
        os.chmod(target, 0o600)


def _adopt_one(ref: str, caches: Path, own_caches: Path) -> str:
    if "://" in ref or not ref:
        return ref
    path = Path(ref)
    try:
        if path.is_symlink() or not path.is_file():
            return ref
        relative = path.resolve(strict=True).relative_to(caches.resolve(strict=True))
    except (OSError, ValueError):
        return ref
    folder = own_caches / relative.parent
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = _free_name(folder / relative.name)
    _move(path, target)
    return str(target)


def adopt_inbound_media(event: InboundEvent, *, from_home: Path, scope: Scope) -> InboundEvent:
    """``event`` with its cached attachments moved from ``from_home``'s caches into ``scope``'s
    (see the module notes). Blocking file I/O: run it off the loop."""
    if _same_dir(from_home, scope.home) or not event.media:
        return event
    caches = from_home / "cache"
    own_caches = scope.home / "cache"
    moved = []
    for item in event.media:
        try:
            ref = _adopt_one(item.ref, caches, own_caches)
        except OSError as exc:
            log.warning("an inbound file could not be moved to its profile: %s", exc.strerror)
            ref = item.ref
        moved.append(item if ref == item.ref else item.model_copy(update={"ref": ref}))
    return event.model_copy(update={"media": tuple(moved)})
