"""Browser sessions: one Chromium per task id, kept alive between calls and reaped when idle.

``SessionManager.session(task_id, scope)`` returns the task's session, starting one on first use.
Which browser a session gets is the profile's *choice*: ``/browser connect <url>`` (kept in
``BrowserChoices`` per profile home), else ``INDUS_BROWSER_CDP_URL`` in the scope's env, else
``browser.cdp_url``; with none of them (or after ``/browser use local``) a local headless Chromium
is launched in its own profile folder ``<home>/cache/browser/profiles/<name>/`` (an ``owner.json``
there names the Indus process that owns it).

The local Chromium gets an environment built from the session's ``Scope``: ``child_env``'s
``code`` variant (nothing credential-named, no provider keys, R02.22), a few display and font
names, and the names listed in ``browser.env_passthrough`` read from the scope's secrets, then
its env (blocked names refused). Chromium's sandbox stays on unless it cannot work here (root, a
container, or a kernel that forbids unprivileged user namespaces); then it is off and
``--disable-dev-shm-usage`` is added.

``run(session, op, timeout_s=…)`` runs one page command under a deadline. When a JavaScript dialog
opens while the command waits, the call ends with ``DialogBlocking`` (the page is not broken; the
command finishes in the background once the dialog is answered). When the deadline passes the
session is marked suspect and replaced on its next use. Deadlines: ``browser.command_timeout``
(30 s, floor 5 s); navigation at least 60 s, the first navigation of a session at least 120 s.

The janitor runs every ``JANITOR_INTERVAL_S`` on the event loop that owns the sessions. It closes
sessions idle longer than ``browser.inactivity_timeout`` (120 s, floor 30 s), each under its own
scope; a session whose close fails ``MAX_CLOSE_FAILURES`` times in a row is force-reaped (its
Chromium processes killed, its folder removed). At its first pass and every ``REAP_INTERVAL_S`` it
also reaps orphaned profile folders: those of a dead owner at once, those of a live owner that
this manager does not track once idle for max(3,600 s, 20 × inactivity).

Every session gets its request guard before first use (``request_gate(scope)``: the P05 URL check
under the session's scope and the manager's ``resolver``; see ``guard``). A browser whose guard
cannot be installed is closed and the session is not started (fail closed).

P24 backends (``cloud``, ``real_profile``, ``engines``, ``routing``): after a CDP endpoint, the
choice is the real profile (``browser.real_profile``), then a cloud provider (``browser.provider``),
then the local engine (``browser.engine``). A backend hands back an ``Opened``: the handle, an
``engine`` label (what served the pages), cleanups that run exactly once on every way out (close,
idle close, force reap, shutdown), and for the real profile the user's folder, which is never
removed. A cloud provider that fails falls back to a local Chromium once for the session; the
session keeps the cloud choice (so it is not replaced on every call) and a one-time ``notice``.
A task may also have a *sidecar* session (``routing``) that serves private addresses locally
while a cloud provider serves the rest; ``routes`` remembers which one the task's last navigation
used, and ``session()`` hands out that one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import shutil
import signal
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from indus.foundation.clock import Clock, SystemClock
from indus.foundation.config.schema import default_for
from indus.foundation.fsio import atomic_write_text
from indus.foundation.scope import Scope, use_scope
from indus.platform import facts
from indus.safety.child_env import child_env, is_blocked
from indus.safety.url_policy import Blocked, Resolver, check_url_async
from indus.tools.builtin.browser import routing
from indus.tools.builtin.browser.cdp import Supervisor
from indus.tools.builtin.browser.cloud import (
    PROVIDER_NAMES,
    PROVIDERS,
    CloudBrowsers,
    CloudFailure,
)
from indus.tools.builtin.browser.driver import (
    BrowserHandle,
    Driver,
    LaunchSpec,
    PageDriver,
    PlaywrightDriver,
)
from indus.tools.builtin.browser.engines import Camofox, Lightpanda, Opened
from indus.tools.builtin.browser.guard import RequestGate
from indus.tools.builtin.browser.real_profile import RealProfiles
from indus.tools.builtin.browser.real_profile import enabled as real_profile_enabled
from indus.tools.registry import scope_key

__all__ = [
    "CDP_URL_ENV",
    "BrowserChoices",
    "BrowserSession",
    "Choice",
    "CommandTimeout",
    "DialogBlocking",
    "SIDECAR_CHOICE",
    "SessionManager",
    "Timeouts",
    "browser_env",
    "choose",
    "engine_for",
    "inactivity_for",
    "needs_sandbox_bypass",
    "orphan_grace_s",
    "setting",
    "timeouts_for",
]

log = logging.getLogger(__name__)

CDP_URL_ENV = "INDUS_BROWSER_CDP_URL"
DEFAULT_INACTIVITY_S = 120.0
MIN_INACTIVITY_S = 30.0
JANITOR_INTERVAL_S = 30.0
REAP_INTERVAL_S = 300.0
MIN_ORPHAN_GRACE_S = 3600.0
ORPHAN_GRACE_FACTOR = 20.0
MAX_CLOSE_FAILURES = 3
CLOSE_TIMEOUT_S = 10.0
DEFAULT_COMMAND_TIMEOUT_S = 30.0
MIN_COMMAND_TIMEOUT_S = 5.0
MIN_OPEN_TIMEOUT_S = 60.0
MIN_FIRST_OPEN_TIMEOUT_S = 120.0
PROFILES_DIR = Path("cache") / "browser" / "profiles"
OWNER_FILE = "owner.json"
OWNER_TOUCH_EVERY_S = 60.0
NO_SHM_ARG = "--disable-dev-shm-usage"
# Host names a headless or headed Chromium may need besides the ``code`` variant's.
HOST_NAMES = (
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XAUTHORITY",
    "DBUS_SESSION_BUS_ADDRESS",
    "FONTCONFIG_PATH",
    "FONTCONFIG_FILE",
    "TZ",
)
USERNS_SWITCH = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")


# -- settings -------------------------------------------------------------------------------------


def setting(scope: Scope | None, key: str) -> Any:
    """``key`` from the scope's config, else the schema default."""
    value: Any = None
    if scope is not None and scope.config is not None:
        try:
            value = scope.config.get(key)
        except Exception:
            log.debug("config %s unreadable; using the default", key, exc_info=True)
    return default_for(key) if value is None else value


def _number(value: object, default: float) -> float:
    if value is None or isinstance(value, bool):
        return default
    try:
        number = float(cast(Any, value))
    except (TypeError, ValueError):
        return default
    return number if number == number else default


@dataclass(frozen=True)
class Timeouts:
    command: float
    open: float
    first_open: float

    def navigation(self, first: bool) -> float:
        return self.first_open if first else self.open


def timeouts_for(scope: Scope | None) -> Timeouts:
    command = max(
        MIN_COMMAND_TIMEOUT_S,
        _number(setting(scope, "browser.command_timeout"), DEFAULT_COMMAND_TIMEOUT_S),
    )
    return Timeouts(
        command, max(command, MIN_OPEN_TIMEOUT_S), max(command, MIN_FIRST_OPEN_TIMEOUT_S)
    )


def inactivity_for(scope: Scope | None) -> float:
    value = _number(setting(scope, "browser.inactivity_timeout"), DEFAULT_INACTIVITY_S)
    return max(MIN_INACTIVITY_S, value)


def orphan_grace_s(inactivity_s: float) -> float:
    return max(MIN_ORPHAN_GRACE_S, ORPHAN_GRACE_FACTOR * inactivity_s)


# -- which browser ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Choice:
    mode: Literal["local", "cdp", "cloud", "real-profile"]
    cdp_url: str | None = None
    source: str = "default"  # default | connect | env | config | use-local | sidecar
    provider: str | None = None  # the cloud provider of a "cloud" choice
    engine: str = "chromium"  # the local engine of a "local" choice

    @property
    def key(self) -> tuple[str, str | None, str | None, str]:
        return (self.mode, self.cdp_url, self.provider, self.engine)


SIDECAR_CHOICE = Choice("local", source="sidecar")
ENGINES = frozenset({"chromium", "lightpanda", "camofox"})


class BrowserChoices:
    """``/browser`` decisions per profile home (one profile's connect never moves another's)."""

    def __init__(self) -> None:
        self._by_profile: dict[str, Choice] = {}

    def get(self, scope: Scope) -> Choice | None:
        return self._by_profile.get(scope_key(scope))

    def set(self, scope: Scope, choice: Choice | None) -> None:
        key = scope_key(scope)
        if choice is None:
            self._by_profile.pop(key, None)
        else:
            self._by_profile[key] = choice


def configured_endpoint(scope: Scope | None) -> tuple[str, str] | None:
    """``(url, source)`` from the scope's env, then config; ``None`` when neither sets one."""
    if scope is not None:
        from_env = (scope.env.get(CDP_URL_ENV) or "").strip()
        if from_env:
            return from_env, "env"
    from_config = setting(scope, "browser.cdp_url")
    if isinstance(from_config, str) and from_config.strip():
        return from_config.strip(), "config"
    return None


def engine_for(scope: Scope | None) -> str:
    engine = setting(scope, "browser.engine")
    return engine if isinstance(engine, str) and engine in ENGINES else "chromium"


def choose(scope: Scope, choices: BrowserChoices) -> Choice:
    """``/browser`` choice, CDP endpoint, real profile, cloud provider, then the local engine."""
    picked = choices.get(scope)
    if picked is not None:
        if picked.mode == "local":
            return Choice("local", source=picked.source, engine=engine_for(scope))
        return picked
    endpoint = configured_endpoint(scope)
    if endpoint is not None:
        return Choice("cdp", endpoint[0], endpoint[1])
    if real_profile_enabled(scope):
        return Choice("real-profile", source="config")
    provider = setting(scope, "browser.provider")
    if isinstance(provider, str) and provider in PROVIDER_NAMES:
        return Choice("cloud", source="config", provider=provider)
    return Choice("local", engine=engine_for(scope))


# -- launch details ---------------------------------------------------------------------------


def _userns_restricted() -> bool:
    try:
        return USERNS_SWITCH.read_text(encoding="utf-8").strip() == "1"
    except OSError:
        return False


def needs_sandbox_bypass(
    scope: Scope | None,
    *,
    euid: int | None = None,
    container: bool | None = None,
    userns_restricted: bool | None = None,
) -> bool:
    """True when Chromium's own sandbox cannot start here (root, a container, or no user
    namespaces for unprivileged processes)."""
    if not sys.platform.startswith("linux"):
        return euid == 0
    if euid is None:
        euid = os.geteuid() if hasattr(os, "geteuid") else None
    if euid == 0:
        return True
    if container is None:
        container = facts.is_container(env=scope.env if scope is not None else {})
    if container:
        return True
    if userns_restricted is None:
        userns_restricted = _userns_restricted()
    return userns_restricted


def _passthrough_value(scope: Scope, name: str) -> str | None:
    if scope.secrets is not None:
        try:
            value = scope.secrets.get(name)
        except Exception:
            log.debug("secret lookup for %s failed", name, exc_info=True)
            value = None
        if value is not None:
            return value
    return scope.env.get(name)


def browser_env(scope: Scope) -> dict[str, str]:
    """The environment of a local Chromium for ``scope`` (see the module notes)."""
    env = child_env(scope, variant="code")
    for name in HOST_NAMES:
        value = scope.env.get(name)
        if value:
            env[name] = value
    names = setting(scope, "browser.env_passthrough")
    if isinstance(names, list | tuple):
        for raw in cast(list[Any], names):
            if not isinstance(raw, str) or not raw.strip():
                continue
            name = raw.strip()
            if is_blocked(name, scope):
                log.warning("browser env passthrough of %s refused: a managed credential", name)
                continue
            value = _passthrough_value(scope, name)
            if value is not None:
                env[name] = value
    return env


# -- processes (orphan reaping) -----------------------------------------------------------------


class ProcessTable(Protocol):
    def alive(self, pid: int) -> bool: ...

    def find(self, marker: str) -> list[int]: ...

    def kill(self, pid: int) -> None: ...


class HostProcesses:
    """Processes of this host, read from ``/proc`` where it exists."""

    def alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        if Path("/proc").is_dir():
            return Path("/proc", str(pid)).exists()
        found = True
        try:
            os.kill(pid, 0)  # signal 0 only asks whether the process exists
        except PermissionError:
            found = True  # it exists but belongs to another user
        except OSError:
            found = False
        return found

    def find(self, marker: str) -> list[int]:
        proc = Path("/proc")
        if not proc.is_dir():
            return []
        wanted = marker.encode("utf-8", "surrogateescape")
        found: list[int] = []
        for entry in proc.iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                args = (entry / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if wanted in args:
                found.append(int(entry.name))
        return found

    def kill(self, pid: int) -> None:
        sig = getattr(signal, "SIGKILL", signal.SIGTERM)
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(pid, sig)


def _profile_marker(folder: Path) -> str:
    return f"--user-data-dir={folder}"


# -- sessions -----------------------------------------------------------------------------------


class CommandTimeout(TimeoutError):
    def __init__(self, label: str, seconds: float, *, restart: bool = True) -> None:
        then = " The session will be restarted on the next browser call." if restart else ""
        super().__init__(f"The browser did not finish '{label}' within {seconds:g} s.{then}")
        self.seconds = seconds


class DialogBlocking(RuntimeError):
    def __init__(self, dialogs: list[dict[str, Any]]) -> None:
        super().__init__(
            "A JavaScript dialog is open and holds up the page. Answer it with browser_dialog "
            "(accept or dismiss), then try again."
        )
        self.dialogs = dialogs


@dataclass(eq=False)
class BrowserSession:
    task_id: str
    scope: Scope
    choice: Choice
    handle: BrowserHandle
    supervisor: Supervisor
    ref_attr: str
    profile_dir: Path | None
    loop: asyncio.AbstractEventLoop
    first_open: bool = True
    suspect: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Commands left running behind an open dialog; cancelled when the session closes.
    background: set[asyncio.Future[Any]] = field(default_factory=set[asyncio.Future[Any]])
    # P24: what served the pages, a one-time notice, cleanups run once at the end, a folder in
    # use that is never removed (the real profile), the cloud fallback, the sidecar's relaxed
    # checks and the private addresses the guard refused (routing).
    engine: str = "local"
    notice: str | None = None
    cleanups: list[Callable[[], Awaitable[None]]] = field(
        default_factory=list[Callable[[], Awaitable[None]]]
    )
    user_dir: Path | None = None
    fell_back: bool = False
    relaxed: bool = False
    private_hits: list[str] = field(default_factory=list[str])

    def take_notice(self) -> str | None:
        notice, self.notice = self.notice, None
        return notice

    @property
    def check_scope(self) -> Scope:
        """The scope URL checks of this session run under (relaxed for a sidecar)."""
        return routing.relaxed_scope(self.scope) if self.relaxed else self.scope

    def cancel_background(self) -> None:
        for work in list(self.background):
            work.cancel()
        self.background.clear()

    @property
    def page(self) -> PageDriver:
        return self.handle.page

    @property
    def timeouts(self) -> Timeouts:
        return timeouts_for(self.scope)


def _fallback_notice(provider: str, exc: BaseException) -> str:
    label = PROVIDERS[provider].label if provider in PROVIDERS else provider
    # A provider's own text is safe (no keys); anything else (an attach error can quote the
    # connect address, which may carry a key) is named by its type only.
    why = str(exc) if isinstance(exc, CloudFailure) else type(exc).__name__
    return (
        f"The {label} cloud browser could not be used ({why}). A local browser serves this "
        "session instead; it closes with the session, and the next session tries the cloud again."
    )


def _swallow(task: asyncio.Future[Any]) -> None:
    if not task.cancelled():
        with contextlib.suppress(BaseException):
            task.exception()


class SessionManager:
    """Every browser session of this process (see the module notes)."""

    def __init__(
        self,
        *,
        driver_factory: Callable[[], Driver] = PlaywrightDriver,
        clock: Clock | None = None,
        processes: ProcessTable | None = None,
        choices: BrowserChoices | None = None,
        janitor: bool = True,
        wall_time: Callable[[], float] = time.time,
        sandbox_check: Callable[[Scope], bool] = needs_sandbox_bypass,
        resolver: Resolver | None = None,
        cloud: CloudBrowsers | None = None,
        lightpanda: Lightpanda | None = None,
        camofox: Camofox | None = None,
    ) -> None:
        self.driver_factory = driver_factory
        self.cloud = cloud if cloud is not None else CloudBrowsers()
        self.lightpanda = lightpanda if lightpanda is not None else Lightpanda()
        self.camofox = camofox if camofox is not None else Camofox()
        self.resolver = resolver
        self.clock = clock if clock is not None else SystemClock()
        self.processes = processes if processes is not None else HostProcesses()
        self.choices = choices if choices is not None else BrowserChoices()
        self.janitor_enabled = janitor
        self.wall_time = wall_time
        self.sandbox_check = sandbox_check
        self.sessions: dict[str, BrowserSession] = {}
        self.activity: dict[str, float] = {}
        self.failures: dict[str, int] = {}
        self._homes: dict[Path, float] = {}  # profile home -> its inactivity limit
        self._creating: dict[str, asyncio.Lock] = {}
        self._drivers: dict[asyncio.AbstractEventLoop, Driver] = {}
        self._janitors: dict[asyncio.AbstractEventLoop, asyncio.Task[None]] = {}
        self._touched: dict[str, float] = {}
        self._passes = 0
        self.pid = os.getpid()
        self.real_profiles = RealProfiles(self.clock)
        self.routes: dict[str, routing.Route] = {}  # task id -> route of its last navigation

    # -- lookup -----------------------------------------------------------------------------------

    def driver(self) -> Driver:
        loop = asyncio.get_running_loop()
        found = self._drivers.get(loop)
        if found is None:
            found = self._drivers[loop] = self.driver_factory()
        return found

    def get(self, task_id: str) -> BrowserSession | None:
        return self.sessions.get(task_id)

    def key_for(self, task_id: str, route: routing.Route | None = None) -> str:
        """The session key serving ``task_id`` on ``route`` (default: its current route)."""
        chosen = route or self.routes.get(task_id, routing.MAIN)
        return task_id if chosen == routing.MAIN else routing.sidecar_key(task_id)

    def current(self, task_id: str) -> BrowserSession | None:
        """The session the task's last navigation used."""
        return self.sessions.get(self.key_for(task_id))

    def set_route(self, task_id: str, route: routing.Route) -> None:
        self.routes[task_id] = route

    def choice_for(self, scope: Scope) -> Choice:
        return choose(scope, self.choices)

    def _stale(self, current: BrowserSession, wanted: Choice) -> str | None:
        if current.suspect:
            return current.suspect
        if current.supervisor.gave_up:
            return "the connection to the browser was lost"
        if current.choice.key != wanted.key:
            return "the browser choice changed"
        return None

    async def session(
        self, task_id: str, scope: Scope, *, route: routing.Route | None = None
    ) -> BrowserSession:
        """The task's session on ``route`` (default: its current route), started or replaced as
        needed."""
        self._ensure_janitor()
        chosen = route or self.routes.get(task_id, routing.MAIN)
        key = self.key_for(task_id, chosen)
        sidecar = chosen == routing.SIDECAR
        wanted = SIDECAR_CHOICE if sidecar else self.choice_for(scope)
        lock = self._creating.setdefault(key, asyncio.Lock())
        async with lock:
            current = self.sessions.get(key)
            if current is not None:
                reason = self._stale(current, wanted)
                if reason is None:
                    self.touch(key)
                    return current
                log.info("replacing the browser session of %s: %s", key, reason)
                try:
                    await self.close(key)
                except Exception as exc:
                    log.warning("closing the old browser session failed (%s); force reap", exc)
                    await self.force_reap(key)
            made = await self._create(key, scope, wanted, relaxed=sidecar)
            self.sessions[key] = made
            self.failures.pop(key, None)
            self.touch(key)
            return made

    # -- creation ---------------------------------------------------------------------------------

    def new_profile(self, scope: Scope, task_id: str) -> Path:
        root = scope.home / PROFILES_DIR
        folder = root / f"s-{secrets.token_hex(5)}"
        folder.mkdir(parents=True, exist_ok=False)
        owner = {"pid": self.pid, "task": task_id, "created": self.clock.now().isoformat()}
        atomic_write_text(folder / OWNER_FILE, json.dumps(owner))
        return folder

    def launch_spec(self, scope: Scope, folder: Path) -> LaunchSpec:
        timeouts = timeouts_for(scope)
        bypass = self.sandbox_check(scope)
        executable = setting(scope, "browser.executable_path")
        return LaunchSpec(
            profile_dir=folder,
            env=browser_env(scope),
            args=(NO_SHM_ARG,) if bypass else (),
            sandbox=not bypass,
            executable=executable.strip()
            if isinstance(executable, str) and executable.strip()
            else None,
            timeout_s=timeouts.first_open,
            command_timeout_s=timeouts.command,
        )

    def env_for(self, scope: Scope) -> dict[str, str]:
        """The environment a browser process of ``scope`` gets (R02.22)."""
        return browser_env(scope)

    def request_gate(self, scope: Scope, hits: list[str] | None = None) -> RequestGate:
        """The P05 URL check for every request of a session under ``scope``; private addresses
        it refuses are added to ``hits`` when given (hybrid routing)."""
        resolver = self.resolver

        async def gate(url: str) -> str | None:
            verdict = await check_url_async(url, scope, resolver=resolver)
            if not isinstance(verdict, Blocked):
                return None
            if hits is not None and verdict.kind == "private":
                hits.append(url)
            return verdict.reason

        return gate

    async def guarded(
        self, handle: BrowserHandle, scope: Scope, hits: list[str] | None = None
    ) -> BrowserHandle:
        """``handle`` with its request guard installed; closed (and the error raised) when the
        guard cannot be installed."""
        try:
            await handle.guard(self.request_gate(scope, hits))
        except BaseException:
            log.warning("the browser's request guard could not be installed; closing it")
            with contextlib.suppress(Exception):
                await handle.close()
            raise
        return handle

    _guarded = guarded

    def _supervisor(self, scope: Scope) -> Supervisor:
        return Supervisor(
            policy=str(setting(scope, "browser.dialog_policy") or ""),
            dialog_timeout_s=_number(setting(scope, "browser.dialog_timeout_s"), 300.0),
            clock=self.clock,
            scope=scope,
        )

    async def _launch_local(
        self, scope: Scope, task_id: str, check_scope: Scope
    ) -> tuple[BrowserHandle, Path]:
        self._homes[scope.home] = inactivity_for(scope)
        folder = self.new_profile(scope, task_id)
        try:
            handle = await self.driver().launch(self.launch_spec(scope, folder))
            handle = await self.guarded(handle, check_scope)
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        return handle, folder

    async def _open_cloud(
        self, scope: Scope, task_id: str, provider: str, hits: list[str]
    ) -> Opened:
        made = await self.cloud.open(provider, scope, task_id)

        async def give_back() -> None:
            await self.cloud.release(scope, made)

        timeouts = timeouts_for(scope)
        try:
            handle = await self.driver().attach(
                made.cdp_url, timeout_s=timeouts.open, command_timeout_s=timeouts.command
            )
            handle = await self.guarded(handle, scope, hits)
        except BaseException:
            await give_back()
            raise
        return Opened(handle, f"cloud:{provider}", cleanups=[give_back])

    async def _create(
        self, task_id: str, scope: Scope, choice: Choice, *, relaxed: bool = False
    ) -> BrowserSession:
        driver = self.driver()
        timeouts = timeouts_for(scope)
        supervisor = self._supervisor(scope)
        check_scope = routing.relaxed_scope(scope) if relaxed else scope
        hits: list[str] = []
        folder: Path | None = None
        opened: Opened | None = None
        notice: str | None = None
        engine = "local-sidecar" if relaxed else "local"
        with use_scope(scope):
            if choice.mode == "cdp" and choice.cdp_url:
                handle = await driver.attach(
                    choice.cdp_url, timeout_s=timeouts.open, command_timeout_s=timeouts.command
                )
                handle = await self.guarded(handle, check_scope)
                engine = "cdp"
            else:
                if choice.mode == "cloud" and choice.provider:
                    try:
                        opened = await self._open_cloud(scope, task_id, choice.provider, hits)
                    except Exception as exc:
                        notice = _fallback_notice(choice.provider, exc)
                        log.warning(
                            "cloud browser %s failed for %s; a local browser serves instead",
                            choice.provider,
                            task_id,
                        )
                else:
                    opened = await self._open_other(scope, task_id, choice)
                if opened is not None:
                    handle = opened.handle
                    engine = opened.engine
                else:
                    handle, folder = await self._launch_local(scope, task_id, check_scope)
        made = BrowserSession(
            task_id=task_id,
            scope=scope,
            choice=choice,
            handle=handle,
            supervisor=supervisor,
            ref_attr=f"data-indus-ref-{secrets.token_hex(4)}",
            profile_dir=folder,
            loop=asyncio.get_running_loop(),
            engine=engine,
            notice=notice or (opened.notice if opened is not None else None),
            cleanups=list(opened.cleanups) if opened is not None else [],
            user_dir=opened.user_dir if opened is not None else None,
            fell_back=notice is not None,
            relaxed=relaxed,
            private_hits=hits,
        )
        if choice.mode == "cdp" and choice.cdp_url:
            supervisor.reconnect = self._reattacher(made)
        handle.listen(supervisor)
        log.info("browser session started for %s (%s)", task_id, engine)
        return made

    async def _open_other(self, scope: Scope, task_id: str, choice: Choice) -> Opened | None:
        """The real profile or an alternative engine; None for a plain local Chromium."""
        if choice.mode == "real-profile":
            return await self.real_profiles.open(self, scope, task_id)
        if choice.mode == "local" and choice.engine == "lightpanda":
            return await self.lightpanda.open(self, scope, task_id)
        if choice.mode == "local" and choice.engine == "camofox":
            return await self.camofox.open(self, scope, task_id)
        return None

    def _reattacher(self, made: BrowserSession) -> Callable[[], Awaitable[None]]:
        async def reattach() -> None:
            url = made.choice.cdp_url or ""
            timeouts = timeouts_for(made.scope)
            with use_scope(made.scope):
                handle = await self.driver().attach(
                    url, timeout_s=timeouts.open, command_timeout_s=timeouts.command
                )
                handle = await self.guarded(handle, made.check_scope)
            made.handle = handle
            handle.listen(made.supervisor)

        return reattach

    # -- running ----------------------------------------------------------------------------------

    def touch(self, task_id: str) -> None:
        now = self.clock.monotonic()
        self.activity[task_id] = now
        current = self.sessions.get(task_id)
        if current is None or current.profile_dir is None:
            return
        last = self._touched.get(task_id)
        if last is None or now - last >= OWNER_TOUCH_EVERY_S:
            self._touched[task_id] = now
            with contextlib.suppress(OSError):
                os.utime(current.profile_dir / OWNER_FILE)

    async def run[R](
        self,
        current: BrowserSession,
        op: Callable[[PageDriver], Awaitable[R]],
        *,
        timeout_s: float | None = None,
        label: str = "command",
        dialog_aware: bool = True,
        recycle_on_timeout: bool = True,
    ) -> R:
        """One page command under its deadline (see the module notes)."""
        limit = timeout_s if timeout_s is not None else current.timeouts.command
        self.touch(current.task_id)
        async with current.lock:
            if dialog_aware and current.supervisor.pending:
                raise DialogBlocking(current.supervisor.pending_dialogs())
            work = asyncio.ensure_future(op(current.page))
            waiters: set[asyncio.Future[Any]] = {work}
            dialog: asyncio.Future[None] | None = None
            if dialog_aware:
                dialog = asyncio.ensure_future(current.supervisor.wait_dialog())
                waiters.add(dialog)
            try:
                done, _ = await asyncio.wait(
                    waiters, timeout=limit, return_when=asyncio.FIRST_COMPLETED
                )
            except BaseException:
                work.cancel()
                work.add_done_callback(_swallow)
                raise
            finally:
                if dialog is not None:
                    dialog.cancel()
                    dialog.add_done_callback(_swallow)
                self.touch(current.task_id)
            if work in done:
                return work.result()
            work.add_done_callback(_swallow)
            if dialog is not None and dialog in done:
                current.background.add(work)
                work.add_done_callback(current.background.discard)
                raise DialogBlocking(current.supervisor.pending_dialogs())
            work.cancel()
            if recycle_on_timeout:
                current.suspect = f"'{label}' timed out after {limit:g} s"
                log.warning(
                    "browser session of %s marked suspect: %s", current.task_id, current.suspect
                )
            raise CommandTimeout(label, limit, restart=recycle_on_timeout)

    # -- closing ----------------------------------------------------------------------------------

    @staticmethod
    async def _run_cleanups(current: BrowserSession) -> None:
        """Run the session's cleanups once each (release a cloud browser, the real profile, stop
        an engine's server); never raises."""
        while current.cleanups:
            cleanup = current.cleanups.pop(0)
            try:
                await asyncio.wait_for(cleanup(), CLOSE_TIMEOUT_S)
            except Exception:
                log.warning("a browser session cleanup failed", exc_info=True)

    def _forget(self, task_id: str) -> BrowserSession | None:
        found = self.sessions.pop(task_id, None)
        self.activity.pop(task_id, None)
        self.failures.pop(task_id, None)
        self._touched.pop(task_id, None)
        return found

    async def close(self, task_id: str) -> None:
        """Close the task's session politely; raises when the browser will not close (the
        session is then kept for the janitor's next try)."""
        current = self.sessions.get(task_id)
        if current is None:
            return
        current.cancel_background()
        await current.supervisor.stop()
        with use_scope(current.scope):
            await asyncio.wait_for(current.handle.close(), CLOSE_TIMEOUT_S)
        if self.sessions.get(task_id) is current:
            self._forget(task_id)
        with use_scope(current.scope):
            await self._run_cleanups(current)
        if current.profile_dir is not None:
            shutil.rmtree(current.profile_dir, ignore_errors=True)
        log.info("browser session of %s closed", task_id)

    async def force_reap(self, task_id: str) -> None:
        """Drop the session without asking the browser; kill what still uses its folder."""
        current = self._forget(task_id)
        if current is None:
            return
        current.cancel_background()
        with contextlib.suppress(Exception):
            await current.supervisor.stop()
        if current.profile_dir is not None:
            self._kill_folder(current.profile_dir)
        if current.user_dir is not None:
            self._kill_folder(current.user_dir, remove=False)  # the user's own folder stays
        with use_scope(current.scope):
            await self._run_cleanups(current)
        log.warning("browser session of %s force-reaped", task_id)

    def _kill_folder(self, folder: Path, *, remove: bool = True) -> int:
        pids = self.processes.find(_profile_marker(folder))
        for pid in pids:
            self.processes.kill(pid)
        if remove:
            shutil.rmtree(folder, ignore_errors=True)
        return len(pids)

    async def end(self, task_id: str) -> None:
        """Close the task's sessions (its main one and any sidecar) at the end of its owner (a
        finished subagent, a closed conversation). Never raises."""
        self.routes.pop(task_id, None)
        await self._end_one(routing.sidecar_key(task_id))
        await self._end_one(task_id)

    async def _end_one(self, task_id: str) -> None:
        """Close one session: awaited on the loop that owns it, else handed to that loop; a
        session that will not close is force-reaped."""
        current = self.sessions.get(task_id)
        if current is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not current.loop:
            self.release(task_id)
            return
        try:
            await self.close(task_id)
        except Exception:
            log.warning("closing the browser of %s failed; reaping it", task_id, exc_info=True)
            await self.force_reap(task_id)

    def release(self, task_id: str) -> None:
        """Close the task's sessions from any thread (child teardown, task end); never raises."""
        self.routes.pop(task_id, None)
        self._release_one(routing.sidecar_key(task_id))
        self._release_one(task_id)

    def _release_one(self, task_id: str) -> None:
        current = self.sessions.get(task_id)
        if current is None:
            return

        async def go() -> None:
            try:
                await self.close(task_id)
            except Exception:
                await self.force_reap(task_id)

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None and running is current.loop:
            task = running.create_task(go())
            task.add_done_callback(_swallow)
        elif not current.loop.is_closed():
            asyncio.run_coroutine_threadsafe(go(), current.loop)

    # -- janitor --------------------------------------------------------------------------------

    def _ensure_janitor(self) -> None:
        if not self.janitor_enabled:
            return
        loop = asyncio.get_running_loop()
        task = self._janitors.get(loop)
        if task is None or task.done():
            self._janitors[loop] = loop.create_task(self._janitor_loop())

    async def _janitor_loop(self) -> None:
        every = max(1, round(REAP_INTERVAL_S / JANITOR_INTERVAL_S))
        while True:
            try:
                if self._passes % every == 0:
                    self.reap_orphans()
                self._passes += 1
                await self.janitor_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("browser janitor pass failed", exc_info=True)
            await self.clock.sleep(JANITOR_INTERVAL_S)

    async def janitor_tick(self) -> list[str]:
        """Close sessions idle past their inactivity limit; returns the task ids handled."""
        loop = asyncio.get_running_loop()
        now = self.clock.monotonic()
        idle: list[str] = []
        for task_id, last in list(self.activity.items()):
            current = self.sessions.get(task_id)
            if current is None:
                self.activity.pop(task_id, None)
                continue
            if current.loop is not loop:
                continue
            if now - last > inactivity_for(current.scope):
                idle.append(task_id)
        for task_id in idle:
            try:
                await self.close(task_id)
            except Exception as exc:
                count = self.failures.get(task_id, 0) + 1
                self.failures[task_id] = count
                if count < MAX_CLOSE_FAILURES:
                    log.warning(
                        "closing the idle browser session of %s failed (%d of %d): %s",
                        task_id,
                        count,
                        MAX_CLOSE_FAILURES,
                        exc,
                    )
                    continue
                log.error("browser session of %s would not close %d times", task_id, count)
                with contextlib.suppress(Exception):
                    await self.force_reap(task_id)
        return idle

    def reap_orphans(self) -> list[Path]:
        """Remove profile folders nobody here owns (see the module notes)."""
        tracked = {s.profile_dir for s in self.sessions.values() if s.profile_dir is not None}
        reaped: list[Path] = []
        for home, inactivity in sorted(self._homes.items()):
            root = home / PROFILES_DIR
            try:
                folders = [p for p in root.iterdir() if p.is_dir()]
            except OSError:
                continue
            for folder in folders:
                if folder in tracked:
                    continue
                if self._orphaned(folder, orphan_grace_s(inactivity)):
                    count = self._kill_folder(folder)
                    log.info(
                        "reaped orphaned browser profile %s (%d processes)", folder.name, count
                    )
                    reaped.append(folder)
        return reaped

    def _orphaned(self, folder: Path, grace: float) -> bool:
        owner: Mapping[str, Any] = {}
        with contextlib.suppress(OSError, ValueError):
            loaded = json.loads((folder / OWNER_FILE).read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                owner = cast(Mapping[str, Any], loaded)
        pid = owner.get("pid")
        owner_alive = isinstance(pid, int) and self.processes.alive(pid)
        if isinstance(pid, int) and not owner_alive:
            return True
        return self._idle_seconds(folder) >= grace

    def _idle_seconds(self, folder: Path) -> float:
        newest = 0.0
        for path in (folder, folder / OWNER_FILE):
            with contextlib.suppress(OSError):
                newest = max(newest, path.stat().st_mtime)
        return max(0.0, self.wall_time() - newest) if newest else float("inf")

    # -- shutdown ---------------------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Close every session of this loop and stop its janitor and Playwright driver."""
        loop = asyncio.get_running_loop()
        janitor = self._janitors.pop(loop, None)
        if janitor is not None:
            janitor.cancel()
            with contextlib.suppress(BaseException):
                await janitor
        for task_id, current in list(self.sessions.items()):
            if current.loop is not loop:
                continue
            try:
                await self.close(task_id)
            except Exception:
                await self.force_reap(task_id)
        driver = self._drivers.pop(loop, None)
        if driver is not None:
            with contextlib.suppress(Exception):
                await driver.stop()
