"""The multiplex host: one gateway per OS user serving every profile under its root (P24-01).

Only with ``gateway.multiplex_profiles: true`` (default false, D-16.17).

**Host lock.** The serving process holds an exclusive ``flock`` on ``gateway-host.lock`` in the
user's state directory (``$XDG_STATE_HOME/indus``, else ``~/.local/state/indus``; created 0700 and
tightened when wider) for its whole life. The kernel drops it when the process dies, so a crash
never leaves a lock behind. The lock file is never deleted.

**Rendezvous record.** Beside the lock the owner publishes ``gateway-host.json`` (0600):
``{protocol, pid, start_time, root, home, served_profiles, updated_at}``. A reader believes a record
only when the file and the directory belong to this user. A record is *stale* when its process is
gone or recycled (pid + start time, ``ProcessIdentity``) or when the host lock is observed free —
the owner holds it for life, so a free lock falsifies a pid that merely looks alive.

**A second ``gateway run``** (``decide_second_run``) never starts a second gateway next to a live
owner. The record only proves an owner exists; what it serves comes from a live ``identify``
answer on the owner's control socket (from the recorded pid), polled for a bounded wait:

- profile served → ``attach``: exit 0 at a shell, 75 under a supervisor (retried, not parked);
- not served yet → the owner is asked to ``serve-profile`` it; attach once its answer includes it;
- the owner answers but will not serve it (unknown profile, a profile still running its own
  gateway, another root) → permanent refusal: 78 under a supervisor, 1 at a shell;
- an owner (lock held) that does not answer within the wait → transient refusal, always 75
  (R06.17: never 78 for "another process serves this");
- no record, or a stale one → ``start``.

**Serving** (``MultiplexHost``). The launch profile is always served and can never be unserved;
a profile whose own per-profile gateway is still running is not taken on (``own_gateway``: two
gateways would poll the same bots).
``serve(name)``, ``unserve(name)`` and ``rescan()`` change the served set under one lock, call the
daemon's hooks (start or stop that profile's adapters) and republish the record. The control verbs
``serve-profile``, ``unserve-profile`` and ``rescan-profiles`` answer within 5 s (on the host's
clock); past that they answer ``{"pending": true, "served_profiles": [...]}`` and the work goes on.

**Tickets.** ``served_ticket_ids`` is the served set as ticket profile ids; a ``TicketBook`` built
with ``served=host.served_ticket_ids`` refuses to mint or redeem for any other profile, and
``mint_routed_ticket`` mints only for a served profile (``wrong_profile`` otherwise).
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import json
import logging
import os
import stat
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, cast

from indus.foundation.clock import Clock, SystemClock
from indus.foundation.errors import InvalidProfileId
from indus.foundation.fsio import atomic_write_text
from indus.foundation.profiles import (
    DEFAULT_PROFILE,
    is_live,
    list_profiles,
    normalize_id,
    profile_home,
)
from indus.foundation.scope import Scope
from indus.gateway.control_socket import VerbRefused
from indus.service import gateway_control as wire
from indus.service.ws_auth import MintedTicket, TicketBook, TicketRefused, stable_profile_id
from indus.state.gateway_runtime import (
    EXIT_CONFIG,
    EXIT_OK,
    EXIT_RESTART,
    ProcessIdentity,
    live_gateway,
    process_start_time,
    supervised,
)

__all__ = [
    "ATTACH_WAIT_S",
    "EXIT_REFUSED_SHELL",
    "HOST_PROTOCOL",
    "LOCK_FILE",
    "PROFILE_VERBS",
    "RECORD_FILE",
    "SERVE_TIMEOUT_S",
    "VERB_BUDGET_S",
    "HostLock",
    "HostRecord",
    "HostStateError",
    "MultiplexHost",
    "RunVerdict",
    "ServeRefused",
    "clear_record",
    "decide_second_run",
    "ensure_state_dir",
    "host_state_dir",
    "mint_routed_ticket",
    "publish_record",
    "read_record",
    "record_alive",
]

log = logging.getLogger(__name__)

HOST_PROTOCOL: Final = 1
LOCK_FILE: Final = "gateway-host.lock"
RECORD_FILE: Final = "gateway-host.json"
ATTACH_WAIT_S: Final = 5.0
"""How long a second run waits for the owner's ``identify`` after seeing its record."""
VERB_BUDGET_S: Final = 5.0
"""The serve/unserve/rescan answer bound; the work goes on past it (``pending``)."""
SERVE_TIMEOUT_S: Final = 8.0
"""Client timeout for the serve verbs: the budget plus room for the answer."""
EXIT_REFUSED_SHELL: Final = 1
PROFILE_VERBS: Final = ("serve-profile", "unserve-profile", "rescan-profiles")
_POLL_S = 0.25
_SYSTEM = SystemClock()


class HostStateError(OSError):
    """The host state directory cannot be used (not ours, not a directory)."""


# -- the state directory --------------------------------------------------------------------------


def host_state_dir(env: Mapping[str, str]) -> Path:
    """``$XDG_STATE_HOME/indus`` when that is an absolute path, else ``$HOME/.local/state/indus``
    (read from the launch profile's env view, never the process env)."""
    state = env.get("XDG_STATE_HOME", "").strip()
    if state and os.path.isabs(state):
        return Path(state) / "indus"
    user_home = env.get("HOME", "").strip()
    base = Path(user_home) if user_home and os.path.isabs(user_home) else Path.home()
    return base / ".local" / "state" / "indus"


def ensure_state_dir(directory: Path) -> Path:
    """Create ``directory`` owner-only, tighten it when wider, refuse one we do not own."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise HostStateError(f"{directory} is not a directory")
    if info.st_uid != os.getuid():
        raise HostStateError(f"{directory} belongs to another user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        os.chmod(directory, 0o700)
    return directory


# -- the lock -------------------------------------------------------------------------------------


class HostLock:
    """The per-user host ``flock`` (see the module notes). Not inherited by children."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.path = directory / LOCK_FILE
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def try_acquire(self) -> bool:
        """Take the lock without waiting; True when it is ours."""
        if self._fd is not None:
            return True
        ensure_state_dir(self.directory)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)

    def observed_free(self) -> bool | None:
        """Does nobody hold the lock? None when it cannot be probed. Never keeps the lock and
        never creates the file (a missing lock file means nobody holds it)."""
        if self._fd is not None:
            return False
        try:
            fd = os.open(self.path, os.O_RDWR)
        except FileNotFoundError:
            return True
        except OSError:
            return None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            return False if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES) else None
        else:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            return True
        finally:
            with contextlib.suppress(OSError):
                os.close(fd)


# -- the record -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class HostRecord:
    """The published owner. ``served_profiles`` is what it serves, launch profile first."""

    pid: int
    start_time: int | None
    root: str
    home: str
    served_profiles: tuple[str, ...] = ()
    updated_at: str = ""
    protocol: int = HOST_PROTOCOL

    def to_json(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "pid": self.pid,
            "start_time": self.start_time,
            "root": self.root,
            "home": self.home,
            "served_profiles": list(self.served_profiles),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_json(cls, data: object) -> HostRecord | None:
        if not isinstance(data, dict):
            return None
        raw = cast(dict[str, Any], data)
        pid, start, protocol = raw.get("pid"), raw.get("start_time"), raw.get("protocol")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1:
            return None
        if not isinstance(protocol, int) or isinstance(protocol, bool):
            return None
        if start is not None and (not isinstance(start, int) or isinstance(start, bool)):
            return None
        root, home = raw.get("root"), raw.get("home")
        if not isinstance(root, str) or not root or not isinstance(home, str) or not home:
            return None
        served_raw: object = raw.get("served_profiles")
        served: tuple[str, ...] = ()
        if isinstance(served_raw, list):
            items = cast(list[object], served_raw)
            served = tuple(p for p in items if isinstance(p, str) and p)
        updated = raw.get("updated_at")
        return cls(
            pid=pid,
            start_time=start,
            root=root,
            home=home,
            served_profiles=served,
            updated_at=updated if isinstance(updated, str) else "",
            protocol=protocol,
        )


def _record_path(directory: Path) -> Path:
    return directory / RECORD_FILE


def publish_record(directory: Path, record: HostRecord) -> None:
    """Write ``record`` atomically, 0600, into the (owner-only) state directory."""
    ensure_state_dir(directory)
    text = json.dumps(record.to_json(), sort_keys=True)
    atomic_write_text(_record_path(directory), text + "\n", mode=0o600)


def _ours(path: Path) -> bool:
    """The file and its directory belong to this user, and the file is a plain file."""
    try:
        info = path.lstat()
        parent = path.parent.lstat()
    except OSError:
        return False
    uid = os.getuid()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or parent.st_uid != uid:
        if stat.S_ISREG(info.st_mode):
            log.warning("ignoring the host record %s: it is not owned by this user", path)
        return False
    return True


def read_record(directory: Path) -> HostRecord | None:
    """The published record; None when absent, foreign, unreadable or malformed."""
    path = _record_path(directory)
    if not _ours(path):
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return HostRecord.from_json(data)


def clear_record(directory: Path, pid: int | None = None) -> bool:
    """Remove the record when it names ``pid`` (default: this process); never another owner's."""
    record = read_record(directory)
    if record is None or record.pid != (os.getpid() if pid is None else pid):
        return False
    with contextlib.suppress(OSError):
        _record_path(directory).unlink()
    return True


def record_alive(
    record: HostRecord,
    lock: HostLock,
    *,
    probes: Mapping[str, Any] | None = None,
) -> bool | None:
    """Does ``record`` name a live owner? False when stale; None when it cannot be proven either
    way (no start time recorded) while the lock is held."""
    if record.protocol != HOST_PROTOCOL:
        return False
    if lock.observed_free() is True:
        return False
    alive = ProcessIdentity(record.pid, record.start_time).is_alive(**dict(probes or {}))
    if not alive:
        return False
    return True if record.start_time is not None else None


# -- the second run -------------------------------------------------------------------------------


type Outcome = Literal["start", "attach", "refuse"]


@dataclass(frozen=True)
class RunVerdict:
    """What a ``gateway run`` does about an existing host. ``exit_code`` None = start."""

    outcome: Outcome
    exit_code: int | None = None
    message: str = ""
    transient: bool = False
    owner_pid: int | None = None


type Identify = Callable[[Path, float], Mapping[str, Any] | None]
type Serve = Callable[[Path, str, float], wire.ControlReply]


def _identify(home: Path, timeout: float) -> Mapping[str, Any] | None:
    return wire.identify(home, timeout=timeout)


def _serve(home: Path, profile: str, timeout: float) -> wire.ControlReply:
    return wire.query(home, "serve-profile", {"name": profile}, timeout=timeout)


def _is_owner(answer: Mapping[str, Any], record: HostRecord) -> bool:
    """The answer comes from the record's process: same pid and, when it names one, the same
    launch home (a record naming some other home must not make us trust whoever answers there)."""
    if answer.get("pid") != record.pid:
        return False
    home = answer.get("home")
    if not isinstance(home, str) or not home:
        return True
    try:
        return Path(home).resolve(strict=False) == Path(record.home).resolve(strict=False)
    except OSError:
        return home == record.home


def _served_from(answer: Mapping[str, Any]) -> list[str]:
    raw: object = answer.get("served_profiles")
    if isinstance(raw, list):
        return [p for p in cast(list[object], raw) if isinstance(p, str)]
    profile = answer.get("profile")
    return [profile] if isinstance(profile, str) and profile else []


def decide_second_run(
    profile: str,
    *,
    root: Path,
    record_dir: Path,
    env: Mapping[str, str],
    identify: Identify = _identify,
    serve: Serve = _serve,
    wait: float = ATTACH_WAIT_S,
    sleep: Callable[[float], None] = _SYSTEM.sleep_sync,
    monotonic: Callable[[], float] = _SYSTEM.monotonic,
    probes: Mapping[str, Any] | None = None,
) -> RunVerdict:
    """Attach, start or refuse (see the module notes). Never raises."""
    try:
        wanted = normalize_id(profile)
    except InvalidProfileId:
        wanted = profile
    supervised_run = supervised(env)
    attach_code = EXIT_RESTART if supervised_run else EXIT_OK
    refuse_code = EXIT_CONFIG if supervised_run else EXIT_REFUSED_SHELL
    lock = HostLock(record_dir)
    try:
        record = read_record(record_dir)
        held = lock.observed_free() is False
        if record is None:
            if held:
                return RunVerdict(
                    "refuse",
                    EXIT_RESTART,
                    "a gateway holds this user's host lock but has not published its record "
                    "yet; nothing was started (a supervisor retries)",
                    transient=True,
                )
            return RunVerdict("start")
        if record_alive(record, lock, probes=probes) is False:
            log.info("the host record names pid %d, which is gone; starting", record.pid)
            return RunVerdict("start")
        if Path(record.root) != root:
            return RunVerdict(
                "refuse",
                refuse_code,
                f"a gateway for another Indus root ({record.root}) owns this user's host lock "
                f"(pid {record.pid}); it cannot serve profile '{wanted}' of this root",
                owner_pid=record.pid,
            )
        owner_home = Path(record.home)
        answer: Mapping[str, Any] | None = None
        deadline = monotonic() + max(0.0, wait)
        while True:
            found = identify(owner_home, wire.DEFAULT_TIMEOUT_S)
            if found is not None and _is_owner(found, record):
                answer = found
                break
            if monotonic() >= deadline:
                break
            sleep(_POLL_S)
        if answer is None:
            return RunVerdict(
                "refuse",
                EXIT_RESTART,
                f"a gateway (pid {record.pid}) owns this user's host but its control socket did "
                f"not answer; whether it serves profile '{wanted}' is unknown, so nothing was "
                "started (a supervisor retries)",
                transient=True,
                owner_pid=record.pid,
            )
        if wanted in _served_from(answer):
            return _attached(record.pid, wanted, attach_code)
        reply = serve(owner_home, wanted, SERVE_TIMEOUT_S)
        if reply.ok and reply.result is not None and wanted in _served_from(reply.result):
            return _attached(record.pid, wanted, attach_code)
        if (reply.ok and reply.result is not None and reply.result.get("pending")) or (
            not reply.answered
        ):
            return RunVerdict(
                "refuse",
                EXIT_RESTART,
                f"the host gateway (pid {record.pid}) is still taking on profile '{wanted}'; "
                "nothing was started (a supervisor retries)",
                transient=True,
                owner_pid=record.pid,
            )
        why = reply.detail or reply.error or "refused"
        return RunVerdict(
            "refuse",
            refuse_code,
            f"the host gateway (pid {record.pid}) will not serve profile '{wanted}': {why}",
            owner_pid=record.pid,
        )
    except Exception as exc:
        log.warning("the host gateway check failed (%s); starting", type(exc).__name__)
        return RunVerdict("start")


def _attached(pid: int, profile: str, code: int) -> RunVerdict:
    return RunVerdict(
        "attach",
        code,
        f"the host gateway (pid {pid}) already serves profile '{profile}'; nothing to start",
        transient=True,
        owner_pid=pid,
    )


# -- serving --------------------------------------------------------------------------------------


class ServeRefused(Exception):
    """A serve/unserve request that cannot be applied; ``code`` goes on the control wire."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


type ProfileHook = Callable[[str, Path], Awaitable[object]]
type VerbHandler = Callable[[dict[str, Any]], Awaitable[Mapping[str, Any]]]
"""A profile verb handler (always async); it fits ``control_socket.Handler``."""


async def _no_hook(_name: str, _home: Path) -> None:
    return None


@dataclass
class _Work:
    tasks: set[asyncio.Task[Any]] = field(default_factory=set[asyncio.Task[Any]])


class MultiplexHost:
    """The served-profile set of the one host gateway (see the module notes)."""

    def __init__(
        self,
        launch: Scope,
        *,
        record_dir: Path | None = None,
        clock: Clock | None = None,
        on_serve: ProfileHook = _no_hook,
        on_unserve: ProfileHook = _no_hook,
        budget: float = VERB_BUDGET_S,
    ) -> None:
        self.launch = launch
        self.root = launch.root
        self.launch_profile = launch.profile_id
        self.record_dir = record_dir if record_dir is not None else host_state_dir(launch.env)
        self.clock: Clock = clock if clock is not None else launch.clock
        self.on_serve = on_serve
        self.on_unserve = on_unserve
        self.budget = budget
        self.lock = HostLock(self.record_dir)
        self._served: dict[str, Path] = {self.launch_profile: launch.home}
        self._mutex: asyncio.Lock | None = None
        self._work = _Work()
        self._start_time = process_start_time(os.getpid())

    # -- claim and record ---------------------------------------------------------------------

    def claim(self) -> bool:
        """Take the host lock and publish the record; False when another process holds it."""
        if not self.lock.try_acquire():
            return False
        self.publish()
        return True

    def record(self) -> HostRecord:
        return HostRecord(
            pid=os.getpid(),
            start_time=self._start_time,
            root=str(self.root),
            home=str(self.launch.home),
            served_profiles=tuple(self.served_profiles()),
            updated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )

    def publish(self) -> None:
        if not self.lock.held:
            return
        try:
            publish_record(self.record_dir, self.record())
        except OSError as exc:
            log.warning("the host record could not be written: %s", exc)

    def close(self) -> None:
        """Remove our record and drop the lock (process exit)."""
        if self.lock.held:
            clear_record(self.record_dir)
        self.lock.release()

    # -- the served set -----------------------------------------------------------------------

    def served_profiles(self) -> list[str]:
        """Served profile ids, the launch profile first."""
        others = sorted(name for name in self._served if name != self.launch_profile)
        return [self.launch_profile, *others]

    def serves(self, profile: str) -> bool:
        return profile in self._served

    def home_of(self, profile: str) -> Path | None:
        return self._served.get(profile)

    def served_ticket_ids(self) -> frozenset[str]:
        """The served set as ticket profile ids (``ws_auth.stable_profile_id`` of each home)."""
        return frozenset(stable_profile_id(home) for home in list(self._served.values()))

    def identify_fields(self) -> dict[str, Any]:
        """What the owner adds to its ``identify`` answer."""
        return {
            "multiplex": True,
            "root": str(self.root),
            "served_profiles": self.served_profiles(),
        }

    def _lock_for_changes(self) -> asyncio.Lock:
        if self._mutex is None:
            self._mutex = asyncio.Lock()
        return self._mutex

    def _answer(self, **fields: Any) -> dict[str, Any]:
        return {**fields, "served_profiles": self.served_profiles()}

    def _checked(self, name: object) -> str:
        if not isinstance(name, str) or not name.strip():
            raise ServeRefused("bad_request", "name the profile")
        try:
            return normalize_id(name)
        except InvalidProfileId:
            raise ServeRefused("invalid_profile", "not a profile id") from None

    async def serve(self, name: object) -> dict[str, Any]:
        profile = self._checked(name)
        async with self._lock_for_changes():
            if profile in self._served:
                return self._answer(served=profile, already=True)
            if not self._live(profile):
                raise ServeRefused("unknown_profile", f"no live profile '{profile}' in this root")
            await self._start(profile)
            self.publish()
            return self._answer(served=profile)

    async def unserve(self, name: object) -> dict[str, Any]:
        profile = self._checked(name)
        if profile == self.launch_profile:
            raise ServeRefused("launch_profile", "the launch profile is always served")
        async with self._lock_for_changes():
            if profile not in self._served:
                raise ServeRefused("not_served", f"profile '{profile}' is not served")
            await self._stop(profile)
            self.publish()
            return self._answer(unserved=profile)

    async def rescan(self) -> dict[str, Any]:
        """Serve every live profile not served yet; unserve served ones that are gone."""
        async with self._lock_for_changes():
            live = set(await asyncio.to_thread(self._live_profiles))
            added: list[str] = []
            failed: list[str] = []
            removed: list[str] = []
            for profile in sorted(name for name in self._served if name not in live):
                if profile == self.launch_profile:
                    continue
                await self._stop(profile)
                removed.append(profile)
            for profile in sorted(live - set(self._served)):
                try:
                    await self._start(profile)
                except ServeRefused:
                    failed.append(profile)
                    continue
                added.append(profile)
            if added or removed:
                self.publish()
            return self._answer(added=added, removed=removed, failed=failed)

    def _live(self, profile: str) -> bool:
        if profile == DEFAULT_PROFILE:
            return True
        try:
            return is_live(self.root, profile)
        except (InvalidProfileId, OSError):
            return False

    def _live_profiles(self) -> list[str]:
        return [p for p in list_profiles(self.root) if self._live(p)]

    async def _start(self, profile: str) -> None:
        home = profile_home(self.root, profile)
        other = await asyncio.to_thread(live_gateway, home)
        if other is not None and other.pid != os.getpid():
            log.warning(
                "profile %s still runs its own gateway (pid %d); stop it before the host serves it",
                profile,
                other.pid,
            )
            raise ServeRefused("own_gateway", f"profile '{profile}' runs its own gateway")
        try:
            await self.on_serve(profile, home)
        except Exception as exc:
            log.error("profile %s could not be served: %s", profile, type(exc).__name__)
            raise ServeRefused("serve_failed", type(exc).__name__) from exc
        self._served[profile] = home
        log.info("now serving profile %s", profile)

    async def _stop(self, profile: str) -> None:
        home = self._served.pop(profile)
        try:
            await self.on_unserve(profile, home)
        except Exception as exc:
            log.error("profile %s did not stop cleanly: %s", profile, type(exc).__name__)
        log.info("stopped serving profile %s", profile)

    # -- control verbs ------------------------------------------------------------------------

    def control_verbs(self) -> dict[str, tuple[VerbHandler, float]]:
        """``{verb: (handler, socket timeout)}`` for ``ControlServer.register`` (owner-only)."""
        timeout = self.budget + 3.0

        async def serve_verb(params: dict[str, Any]) -> Mapping[str, Any]:
            return await self._bounded(lambda: self.serve(params.get("name")))

        async def unserve_verb(params: dict[str, Any]) -> Mapping[str, Any]:
            return await self._bounded(lambda: self.unserve(params.get("name")))

        async def rescan_verb(params: dict[str, Any]) -> Mapping[str, Any]:
            return await self._bounded(self.rescan)

        return {
            "serve-profile": (serve_verb, timeout),
            "unserve-profile": (unserve_verb, timeout),
            "rescan-profiles": (rescan_verb, timeout),
        }

    async def _bounded(self, work: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        """Run ``work``; answer it within the budget, else ``pending`` while it goes on."""
        task: asyncio.Task[dict[str, Any]] = asyncio.ensure_future(work())
        self._work.tasks.add(task)
        task.add_done_callback(self._finished)
        timer: asyncio.Task[None] = asyncio.ensure_future(self.clock.sleep(self.budget))
        try:
            await asyncio.wait({task, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            timer.cancel()
        if not task.done():
            return self._answer(pending=True)
        try:
            return task.result()
        except ServeRefused as refused:
            raise VerbRefused(refused.code, refused.detail) from None

    def _finished(self, task: asyncio.Task[Any]) -> None:
        self._work.tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None and not isinstance(exc, ServeRefused):
            log.warning("a profile change failed: %s", type(exc).__name__)

    async def aclose(self) -> None:
        """Wait for profile changes still running past their answer, then ``close``."""
        for task in list(self._work.tasks):
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await task
        self.close()


def mint_routed_ticket(
    book: TicketBook, host: MultiplexHost, profile: str, purpose: str
) -> MintedTicket:
    """A ticket bound to ``profile``'s home; ``wrong_profile`` when the host does not serve it."""
    home = host.home_of(profile)
    if home is None:
        raise TicketRefused("wrong_profile", "this gateway does not serve that profile")
    return book.mint(purpose, profile_id=stable_profile_id(home))
