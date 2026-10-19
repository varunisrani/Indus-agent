# pyright: strict
"""Our own deadline around every vendor SDK call, and the in-sandbox timeout guard (P21-02).

Vendor SDKs block, and some ignore their own timeouts (R02.30). Every call a cloud backend makes
goes through ``sdk_call``: the blocking callable runs on ``SDK_POOL`` with ``Operation.deadline_s``
on the caller's clock. Past the deadline the caller gets ``BackendDeadline`` at once; the stuck
thread is abandoned (Python cannot stop it, ``foundation.blocking``), and the operation's
``cancel`` (the vendor's terminate or stop) is fired once on its own daemon thread, never awaited.

Retries: an idempotent operation (create-by-name, get, stop, snapshot read, …) is tried up to three
times when the failure is transient (``is_transient``: HTTP 408, 425, 429, 5xx anywhere in the
exception chain, gRPC-style UNAVAILABLE / RESOURCE_EXHAUSTED / INTERNAL / DEADLINE_EXCEEDED, or a
dropped connection), sleeping ``min(4, 0.5 · 2^(n-1))`` seconds scaled by a jitter of 0.5–1.0
between tries. An exec is never retried, and neither is a deadline miss (the first call may still
land).

The shell guard: an exec's command runs as ``timeout -k 2 <N> bash -c <command>`` inside the
sandbox, so the process stops even when the vendor never enforces a timeout.
"""

from __future__ import annotations

import logging
import math
import random
import shlex
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from indus.backends.contract import BackendError
from indus.foundation.blocking import BlockingPool, DeadlineMissed, PoolClosed, PoolFull
from indus.foundation.clock import Clock, SystemClock

__all__ = [
    "BACKOFF_BASE_S",
    "BACKOFF_CAP_S",
    "BULK_FILE_DEADLINE_S",
    "CREATE",
    "CREATE_BY_NAME",
    "CREATE_DEADLINE_S",
    "DELETE",
    "EXEC_GRACE_S",
    "FILE",
    "FILE_BULK",
    "FILE_DEADLINE_S",
    "GET",
    "MAX_ATTEMPTS",
    "SDK_POOL",
    "SNAPSHOT",
    "SNAPSHOT_DEADLINE_S",
    "SNAPSHOT_READ",
    "STOP",
    "STOP_DEADLINE_S",
    "TERMINATE",
    "TRANSIENT_STATUS",
    "BackendDeadline",
    "Operation",
    "backoff_delay",
    "exec_operation",
    "guarded_argv",
    "guarded_command",
    "is_transient",
    "sdk_call",
    "status_of",
]

log = logging.getLogger(__name__)

EXEC_GRACE_S = 10.0
CREATE_DEADLINE_S = 300.0
SNAPSHOT_DEADLINE_S = 60.0
STOP_DEADLINE_S = 15.0
GET_DEADLINE_S = 30.0
FILE_DEADLINE_S = 30.0
BULK_FILE_DEADLINE_S = 120.0

MAX_ATTEMPTS = 3
BACKOFF_BASE_S = 0.5
BACKOFF_FACTOR = 2.0
BACKOFF_CAP_S = 4.0
GUARD_KILL_AFTER_S = 2

TRANSIENT_STATUS: frozenset[int] = frozenset({408, 425, 429, *range(500, 600)})
# gRPC status names (Modal raises gRPC-shaped errors) as the HTTP code they stand for.
_GRPC_AS_HTTP: dict[str, int] = {
    "UNAVAILABLE": 503,
    "RESOURCE_EXHAUSTED": 429,
    "INTERNAL": 500,
    "DEADLINE_EXCEEDED": 504,
    "NOT_FOUND": 404,
    "INVALID_ARGUMENT": 400,
    "UNAUTHENTICATED": 401,
    "PERMISSION_DENIED": 403,
    "ALREADY_EXISTS": 409,
    "FAILED_PRECONDITION": 400,
}
_NETWORK_CLASS_NAMES = ("NetworkError", "RemoteProtocolError")

# Vendor calls hold a worker for at most their deadline, then are abandoned; their own pool keeps a
# stuck vendor from starving file reads or tool handlers.
SDK_POOL = BlockingPool("sdk", 8, queue_limit=64)


class BackendDeadline(BackendError):
    """A vendor call did not come back within our deadline; it was abandoned (R02.30)."""

    default_code = "backend.deadline"

    def __init__(self, operation: str, deadline_s: float) -> None:
        super().__init__(
            f"the sandbox vendor did not answer {operation} within {deadline_s:g}s",
            hint="the vendor may be slow or down; try again, or check its status page",
        )
        self.operation = operation
        self.deadline_s = deadline_s


@dataclass(frozen=True, slots=True)
class Operation:
    """One kind of vendor call: its name (for logs and errors), deadline and retry safety."""

    name: str
    deadline_s: float
    idempotent: bool = False


CREATE = Operation("create", CREATE_DEADLINE_S)
CREATE_BY_NAME = Operation("create_by_name", CREATE_DEADLINE_S, idempotent=True)
GET = Operation("get", GET_DEADLINE_S, idempotent=True)
SNAPSHOT = Operation("snapshot", SNAPSHOT_DEADLINE_S)
SNAPSHOT_READ = Operation("snapshot_read", SNAPSHOT_DEADLINE_S, idempotent=True)
STOP = Operation("stop", STOP_DEADLINE_S, idempotent=True)
TERMINATE = Operation("terminate", STOP_DEADLINE_S, idempotent=True)
DELETE = Operation("delete", STOP_DEADLINE_S, idempotent=True)
FILE = Operation("file", FILE_DEADLINE_S)
FILE_BULK = Operation("file_bulk", BULK_FILE_DEADLINE_S)


def exec_operation(timeout: float) -> Operation:
    """An exec: the command's own timeout plus ``EXEC_GRACE_S``; never retried."""
    return Operation("exec", max(0.0, float(timeout)) + EXEC_GRACE_S, idempotent=False)


# -- classification -------------------------------------------------------------------------------


def _chain(exc: BaseException) -> list[BaseException]:
    out: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        out.append(current)
        current = current.__cause__ or current.__context__
    return out


def _own_status(exc: BaseException) -> int | None:
    response: object = cast(object, getattr(exc, "response", None))
    for value in (
        cast(object, getattr(exc, "status_code", None)),
        cast(object, getattr(response, "status_code", None)),
        cast(object, getattr(exc, "status", None)),
    ):
        if value is None or isinstance(value, bool):
            continue
        name: object = cast(object, getattr(value, "name", None))
        if isinstance(name, str):  # an enum: gRPC-style status names
            if name in _GRPC_AS_HTTP:
                return _GRPC_AS_HTTP[name]
            continue
        if isinstance(value, int):
            return value
    return None


def status_of(exc: BaseException) -> int | None:
    """The first HTTP-like status found along ``exc``'s cause/context chain, or None."""
    for item in _chain(exc):
        found = _own_status(item)
        if found is not None:
            return found
    return None


def _network_failure(exc: BaseException) -> bool:
    if isinstance(exc, ConnectionError):
        return True
    for cls in type(exc).__mro__:
        if cls.__name__ in _NETWORK_CLASS_NAMES and cls.__module__.startswith("httpx"):
            return True
    return type(exc).__name__.endswith("ConnectionError")


def is_transient(exc: BaseException) -> bool:
    """Whether a retry may help: a transient status or a dropped connection in the chain."""
    for item in _chain(exc):
        status = _own_status(item)
        if status is not None and status in TRANSIENT_STATUS:
            return True
        if _network_failure(item):
            return True
    return False


def backoff_delay(attempt: int, jitter: float) -> float:
    """Seconds to wait after failed attempt ``attempt`` (1-based); ``jitter`` in [0, 1]."""
    raw = min(BACKOFF_CAP_S, BACKOFF_BASE_S * BACKOFF_FACTOR ** max(0, attempt - 1))
    return raw * (0.5 + 0.5 * min(1.0, max(0.0, jitter)))


# -- the call -------------------------------------------------------------------------------------


def _fire_cancel(cancel: Callable[[], object], operation: str) -> None:
    def run() -> None:
        try:
            cancel()
        except Exception as exc:
            log.debug("vendor cancel after a missed %s deadline failed: %s", operation, exc)

    threading.Thread(target=run, name=f"indus-sdk-cancel-{operation}", daemon=True).start()


async def sdk_call[T](
    op: Operation,
    fn: Callable[[], T],
    *,
    cancel: Callable[[], object] | None = None,
    clock: Clock | None = None,
    sleeper: Callable[[float], Awaitable[object]] | None = None,
    jitter: Callable[[], float] | None = None,
    pool: BlockingPool = SDK_POOL,
    label: str = "",
) -> T:
    """Run the blocking vendor call ``fn`` under ``op`` (see the module notes)."""
    clock = clock if clock is not None else SystemClock()
    sleep: Callable[[float], Awaitable[Any]] = sleeper if sleeper is not None else clock.sleep
    draw: Callable[[], float] = jitter if jitter is not None else random.random
    what = label or op.name
    attempts = MAX_ATTEMPTS if op.idempotent else 1
    attempt = 0
    while True:
        attempt += 1
        try:
            return await pool.run(fn, deadline=op.deadline_s, clock=clock, label=f"sdk {what}")
        except DeadlineMissed as exc:
            if cancel is not None:
                _fire_cancel(cancel, op.name)
            log.warning("vendor call %s missed its %gs deadline; abandoned", what, op.deadline_s)
            raise BackendDeadline(op.name, op.deadline_s) from exc
        except (PoolFull, PoolClosed) as exc:
            raise BackendError(
                f"vendor call {what} could not start: {exc}",
                hint="earlier vendor calls are stuck; wait a little, then try again",
            ) from exc
        except Exception as exc:
            if attempt >= attempts or not is_transient(exc):
                raise
            delay = backoff_delay(attempt, draw())
            log.warning(
                "vendor call %s failed (%s, status %s); retry %d of %d in %.2fs",
                what,
                type(exc).__name__,
                status_of(exc),
                attempt,
                attempts - 1,
                delay,
            )
            await sleep(delay)


# -- the shell guard ------------------------------------------------------------------------------


def _guard_seconds(timeout: float) -> int:
    return max(1, math.ceil(timeout)) if timeout > 0 else 1


def guarded_argv(command: str, timeout: float, *, login: bool = False) -> list[str]:
    """``timeout -k 2 <N> bash [-l] -c <command>`` as argv (``N`` = ``timeout`` rounded up)."""
    shell = ["bash", "-l", "-c"] if login else ["bash", "-c"]
    return ["timeout", "-k", str(GUARD_KILL_AFTER_S), str(_guard_seconds(timeout)), *shell, command]


def guarded_command(command: str, timeout: float, *, login: bool = False) -> str:
    """``guarded_argv`` as one shell-quoted string, for SDKs that take a command line."""
    return shlex.join(guarded_argv(command, timeout, login=login))
