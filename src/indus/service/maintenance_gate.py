"""Who may bulk-edit the session store right now: the maintenance gate (P17-21, R09.21).

While a gateway daemon owns the profile it is the one writer of the store. A bulk change made by
another process at the same time (a prune of many sessions, a search-index rebuild, an import,
a restore over the live file) would race the daemon's turns. Every bulk maintenance verb therefore
asks the gate, which decides where it runs:

1. **A daemon answers the control socket** (an answer is liveness, R06.18): the operation is sent
   to it as the owner-only ``maintenance-run`` verb and runs inside the daemon on its own store
   handle, through the writer executor its turns use, so the two never interleave. A daemon that
   does not serve the verb, refuses it, or cannot be asked (an operation that needs it stopped)
   means **refused**: nothing is written by this process.
2. **No daemon answers**: this process takes the profile ownership lock (the daemon's
   single-instance ``flock`` on ``<home>/gateway.lock``) without waiting and runs the operation
   while holding it, so no daemon can claim the profile halfway. A held lock (a daemon starting,
   one whose socket is slow, another maintenance run) means **refused**.

Read-only browsing never comes here: it opens ``mode=ro`` connections that create, migrate and
checkpoint nothing (R03.16).

The daemon half is ``MaintenanceVerb``: the daemon registers ``handlers()`` on its control socket;
each operation runs on a worker thread with the daemon's shared store handle.

Operations (``op`` and params): ``prune`` (``ids``: the sessions the caller showed, at most
``MAX_IDS`` per request; the gate batches), ``delete`` (``session_id``: the session and its
continuations), ``repair`` (rebuild the search index), ``import`` (``path``: a JSON-lines export
the daemon reads).
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, cast

from indus.foundation.blocking import run_blocking
from indus.foundation.errors import StoreError
from indus.foundation.scope import Scope
from indus.service import gateway_control
from indus.state import deletion, fts, portability, registry
from indus.state.gateway_runtime import lock_path
from indus.state.registry import StoreHandle

__all__ = [
    "MAINTENANCE_VERB",
    "MAX_IDS",
    "OPS",
    "VERB_TIMEOUT_S",
    "GateResult",
    "MaintenanceGate",
    "MaintenanceRefused",
    "MaintenanceVerb",
    "OwnershipLock",
    "Owner",
]

log = logging.getLogger(__name__)

MAINTENANCE_VERB: Final = "maintenance-run"
"""The owner-only control verb a daemon serves bulk maintenance on."""
OPS: Final[tuple[str, ...]] = ("prune", "delete", "repair", "import")
MAX_IDS: Final = 1000
"""Session ids per ``prune`` request (a request line is at most 64 KiB)."""
VERB_TIMEOUT_S: Final = 600.0
"""How long the daemon gives one operation, and the client waits for its answer."""
_MAX_REPORTED: Final = 20

type Where = Literal["daemon", "local"]
type RefusalReason = Literal["daemon_cannot", "daemon_refused", "needs_stopped", "busy"]


class MaintenanceRefused(Exception):
    """The operation cannot run now; nothing was written. ``message`` is for the person."""

    def __init__(self, reason: RefusalReason, message: str) -> None:
        super().__init__(message)
        self.reason: RefusalReason = reason
        self.message = message


@dataclass(frozen=True)
class Owner:
    """Who owns the profile: a daemon that answered (its pid), or nobody."""

    daemon: bool
    pid: int | None = None


@dataclass(frozen=True)
class GateResult:
    """Where the operation ran and what it answered: the daemon's (merged) JSON answer, or the
    value ``local`` returned."""

    where: Where
    value: Any
    answers: Sequence[Mapping[str, Any]] = field(default_factory=tuple[Mapping[str, Any], ...])


class OwnershipLock:
    """The profile's ownership lock taken by a process that is not the daemon (non-blocking)."""

    def __init__(self, home: Path) -> None:
        self.path = lock_path(home)
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def try_acquire(self) -> bool:
        if self._fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)  # never inherited (PEP 446)
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


type Identify = Callable[[Path], Mapping[str, Any] | None]
type Ask = Callable[..., gateway_control.ControlReply]


def _identify(home: Path) -> Mapping[str, Any] | None:
    return gateway_control.identify(home)


def _ask(home: Path, verb: str, params: Mapping[str, Any], *, timeout: float) -> Any:
    return gateway_control.query(home, verb, params, timeout=timeout)


def _stop_hint(pid: int | None) -> str:
    who = f" (pid {pid})" if pid else ""
    return f"The gateway daemon{who} owns this profile's sessions"


class MaintenanceGate:
    """Decides where a bulk maintenance operation runs (see the module notes)."""

    def __init__(
        self,
        scope: Scope,
        *,
        identify: Identify = _identify,
        ask: Ask = _ask,
        timeout: float = VERB_TIMEOUT_S,
    ) -> None:
        self.scope = scope
        self.home = scope.home
        self._identify = identify
        self._ask = ask
        self.timeout = timeout

    def owner(self) -> Owner:
        """A daemon answering the control socket owns the profile; nothing else counts."""
        answer = self._identify(self.home)
        if answer is None:
            return Owner(False)
        pid = answer.get("pid")
        return Owner(True, pid if isinstance(pid, int) and not isinstance(pid, bool) else None)

    @contextlib.contextmanager
    def locally(self, what: str) -> Generator[None]:
        """Hold the ownership lock for a local run, or refuse (``busy``)."""
        lock = OwnershipLock(self.home)
        if not lock.try_acquire():
            raise MaintenanceRefused(
                "busy",
                f"Not done: {what} needs this profile to itself, and a gateway daemon is starting "
                "or stopping, or another maintenance run is going on. Try again in a moment "
                "(`indus gateway status` shows the daemon).",
            )
        try:
            yield
        finally:
            lock.release()

    def run(
        self,
        op: str,
        params: Mapping[str, Any],
        local: Callable[[], Any],
        *,
        what: str,
        before_daemon: Callable[[], None] | None = None,
    ) -> GateResult:
        """Run ``op`` in the daemon when one owns the profile, else ``local()`` under the lock.
        ``before_daemon`` runs here first when the daemon will run it (a read-only backup)."""
        if op not in OPS:
            raise ValueError(f"unknown maintenance op {op!r}")
        owner = self.owner()
        if owner.daemon:
            if before_daemon is not None:
                before_daemon()
            return self._in_daemon(op, params, owner, what)
        with self.locally(what):
            return GateResult("local", local())

    def refuse_while_owned(self, what: str) -> None:
        """For operations that need the daemon stopped (restoring files under it)."""
        owner = self.owner()
        if owner.daemon:
            raise MaintenanceRefused(
                "needs_stopped",
                f"Not done: {_stop_hint(owner.pid)}; {what} would change files it has open. "
                "Stop it with `indus gateway stop`, then run this again.",
            )

    def _in_daemon(self, op: str, params: Mapping[str, Any], owner: Owner, what: str) -> GateResult:
        batches: list[Mapping[str, Any]] = [params]
        if op == "prune":
            ids = [str(i) for i in params.get("ids", ())]
            batches = [
                {**params, "ids": ids[i : i + MAX_IDS]} for i in range(0, max(len(ids), 1), MAX_IDS)
            ]
        answers: list[Mapping[str, Any]] = []
        for batch in batches:
            reply = self._ask(
                self.home,
                MAINTENANCE_VERB,
                {"op": op, **batch},
                timeout=self.timeout,
            )
            done = " (earlier batches were done)" if answers else ""
            if reply.ok and reply.result is not None:
                refused = reply.result.get("refused")
                if refused is None:
                    answers.append(reply.result)
                    continue
                detail = reply.result.get("detail")
                raise MaintenanceRefused(
                    "daemon_refused",
                    f"Not done{done}: {detail if isinstance(detail, str) else refused}.",
                )
            if reply.error in ("unsupported", "unknown_verb"):
                raise MaintenanceRefused(
                    "daemon_cannot",
                    f"Not done: {_stop_hint(owner.pid)} and cannot run {what} for you. Stop it "
                    "with `indus gateway stop`, then run this again.",
                )
            detail = f": {reply.detail}" if reply.detail else ""
            raise MaintenanceRefused(
                "daemon_refused",
                f"Not done{done}: {_stop_hint(owner.pid)} and answered {reply.error}{detail}.",
            )
        return GateResult("daemon", _merge(answers), tuple(answers))


def _merge(answers: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Batched answers as one: counts added, lists joined."""
    if len(answers) == 1:
        return answers[0]
    out: dict[str, Any] = {}
    for answer in answers:
        for key, value in answer.items():
            if isinstance(value, int) and not isinstance(value, bool):
                out[key] = int(out.get(key, 0)) + value
            elif isinstance(value, list):
                out[key] = [*out.get(key, []), *value]
            else:
                out[key] = value
    return out


# -- the daemon half ------------------------------------------------------------------------------


class _BadParams(Exception):
    pass


def _ids(params: Mapping[str, Any]) -> list[str]:
    raw: object = params.get("ids")
    if not isinstance(raw, list):
        raise _BadParams("ids must be a list of session ids")
    items = cast(list[object], raw)
    if len(items) > MAX_IDS:
        raise _BadParams("ids must be a list of at most 1000 session ids")
    out: list[str] = []
    for item in items:
        if not isinstance(item, str) or not item or len(item) > 200:
            raise _BadParams("ids must be session ids")
        out.append(item)
    return out


def _text(params: Mapping[str, Any], name: str) -> str:
    value = params.get(name)
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise _BadParams(f"{name} is missing")
    return value


class MaintenanceVerb:
    """``maintenance-run`` inside the daemon: each operation on the daemon's own store handle."""

    def __init__(
        self,
        scope: Scope,
        *,
        store: Callable[[], StoreHandle] | None = None,
        release: Callable[[StoreHandle], None] | None = None,
    ) -> None:
        self.scope = scope
        self._store = store or (lambda: registry.acquire(scope))
        self._release = release or registry.release

    def handlers(self) -> dict[str, tuple[Callable[[dict[str, Any]], Any], float]]:
        return {MAINTENANCE_VERB: (self.handle, VERB_TIMEOUT_S)}

    async def handle(self, params: dict[str, Any]) -> Mapping[str, Any]:
        """Run one operation; a refusal is answered as ``{"refused": code, "detail": ...}`` (the
        control wire's error answers belong to the gateway layer)."""
        op = params.get("op")
        if op not in OPS:
            return {"refused": "bad_op", "detail": "unknown maintenance operation"}
        try:
            return await run_blocking(self._run, str(op), params, label=f"maintenance {op}")
        except _BadParams as exc:
            return {"refused": "bad_params", "detail": str(exc)}
        except StoreError as exc:
            log.warning("maintenance %s failed: %s", op, exc.code)
            return {"refused": "store_error", "detail": exc.message}

    def _run(self, op: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        store = self._store()
        try:
            log.info("maintenance %s runs in the daemon", op)
            if op == "prune":
                report = deletion.delete_many(store, _ids(params))
                return {
                    "deleted": len(report.deleted),
                    "removed": len(report.removed),
                    "guarded": len(report.guarded),
                    "unknown": len(report.unknown),
                }
            if op == "delete":
                sid = _text(params, "session_id")
                try:
                    removed = deletion.delete_session(store, sid, whole_chain=True)
                except deletion.SessionGuarded as exc:
                    return {"refused": "guarded", "detail": exc.message}
                return {"removed": bool(removed)}
            if op == "repair":
                return {"rebuilt": fts.rebuild(store)}
            path = Path(_text(params, "path"))
            if not path.is_absolute() or not path.is_file():
                raise _BadParams("path must name an existing file by its absolute path")
            imported = portability.import_jsonl(store, path)
            return {
                "ok": imported.ok,
                "imported": len(imported.imported),
                "skipped": len(imported.skipped),
                "detached": len(imported.detached),
                "retitled": len(imported.retitled),
                "errors": [str(issue) for issue in imported.errors[:_MAX_REPORTED]],
                "error_count": len(imported.errors),
            }
        finally:
            self._release(store)
