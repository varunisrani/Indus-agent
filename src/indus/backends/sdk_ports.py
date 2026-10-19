# pyright: strict
"""The SDK backends' two ports, served by the shared helpers (P21-12 wiring).

``sdk_shell`` (Modal, Daytona, Vercel Sandbox) talks to two small ports so it never depends on how
vendor calls are bounded or where restore points live. This module is the one implementation of
each:

- ``VendorSupport`` (``sdk_shell.SdkSupport``): every vendor call goes through ``sdk_call`` (our
  own deadline on the SDK pool, retries of idempotent calls on transient failures, a cancel fired
  once on a missed deadline; R02.30), the in-sandbox guard is ``guarded_command``, and stdin is
  staged or chunked by ``stdin_payload`` (P21-03).
- ``ProfileRecords`` (``sdk_shell.SandboxRecords``): one profile's records of one backend kind
  (P21-01). The store takes a file lock and fsyncs, so every call runs on the ``io`` pool through
  ``run_blocking``, never on the event loop.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from indus.backends import sandbox_records as records_store
from indus.backends import sdk_call as calls
from indus.backends import stdin_payload as payload
from indus.backends.sdk_shell import FileSize, StagedStdin, WriteFile
from indus.foundation.blocking import BlockingPool, run_blocking
from indus.foundation.clock import Clock, SystemClock
from indus.foundation.scope import Scope

__all__ = ["RECORD_FIELDS", "RECORDS_DEADLINE_S", "ProfileRecords", "VendorSupport"]

# Fields a record may carry (the store's own set).
RECORD_FIELDS = frozenset({"snapshot_id", "overlay_path", "sandbox_name"})
# The store waits up to ``LOCK_TIMEOUT_S`` for its lock; past this (real time) the call is
# abandoned and the caller gets ``DeadlineMissed``.
RECORDS_DEADLINE_S = records_store.LOCK_TIMEOUT_S + 20.0

type RemoveFile = Callable[[str], Awaitable[object]]


class VendorSupport:
    """``SdkSupport`` over ``sdk_call`` and ``stdin_payload`` (see the module notes)."""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        sleeper: Callable[[float], Awaitable[object]] | None = None,
        jitter: Callable[[], float] | None = None,
        pool: BlockingPool = calls.SDK_POOL,
    ) -> None:
        self.clock: Clock = clock if clock is not None else SystemClock()
        self.sleeper = sleeper
        self.jitter = jitter
        self.pool = pool

    async def call[T](
        self,
        op: str,
        fn: Callable[[], T],
        *,
        timeout: float,
        idempotent: bool = False,
        on_deadline: Callable[[], None] | None = None,
    ) -> T:
        """``fn`` under a deadline of ``timeout`` seconds; ``BackendDeadline`` when missed."""
        operation = calls.Operation(op, max(0.0, float(timeout)), idempotent=idempotent)
        return await calls.sdk_call(
            operation,
            fn,
            cancel=on_deadline,
            clock=self.clock,
            sleeper=self.sleeper,
            jitter=self.jitter,
            pool=self.pool,
        )

    def guard(self, script: str, seconds: float) -> str:
        return calls.guarded_command(script, seconds)

    async def stage_stdin(
        self,
        data: str,
        *,
        temp_dir: str,
        write: WriteFile,
        size: FileSize,
        remove: RemoveFile | None = None,
    ) -> StagedStdin:
        staged = await payload.stage_stdin(
            data, temp_dir=temp_dir, write=write, size_of=size, remove=remove
        )
        return StagedStdin(prefix=staged.prefix(), error=staged.error, path=staged.path or "")

    def stdin_chunks(self, data: str) -> list[bytes]:
        raw = payload.encode_stdin(data)
        if isinstance(raw, payload.StdinFailure):
            raise ValueError(raw.message)
        return list(payload.stdin_chunks(raw))


class ProfileRecords:
    """``SandboxRecords`` over the profile's records store, off the event loop."""

    def __init__(self, store: records_store.SandboxRecords) -> None:
        self.store = store

    @classmethod
    def for_scope(cls, scope: Scope, kind: str) -> ProfileRecords:
        return cls(records_store.SandboxRecords.for_scope(scope, kind))

    async def _run[T](self, fn: Callable[[], T], what: str) -> T:
        return await run_blocking(
            fn,
            pool="io",
            deadline=RECORDS_DEADLINE_S,
            label=f"sandbox records {self.store.kind} {what}",
        )

    async def load(self, task_key: str) -> Mapping[str, Any] | None:
        store = self.store
        record = await self._run(lambda: store.get(task_key), "read")
        return None if record is None else record.to_json()

    async def save(self, task_key: str, fields: Mapping[str, Any]) -> None:
        unknown = set(fields) - RECORD_FIELDS
        if unknown:
            raise ValueError(f"sandbox records have no field {', '.join(sorted(unknown))}")
        values = {name: (None if value is None else str(value)) for name, value in fields.items()}
        store = self.store
        await self._run(lambda: store.put(task_key, **values), "write")

    async def drop(self, task_key: str, *, expect: Mapping[str, Any] | None = None) -> None:
        unknown = set(expect or {}) - {"snapshot_id"}
        if unknown:
            raise ValueError("a sandbox record can be dropped only on its snapshot id")
        snapshot = (expect or {}).get("snapshot_id")
        store = self.store
        await self._run(
            lambda: store.delete(
                task_key, only_if_snapshot=None if snapshot is None else str(snapshot)
            ),
            "drop",
        )
