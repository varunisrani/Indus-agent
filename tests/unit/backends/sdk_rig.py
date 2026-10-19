"""What the SDK backend tests (Modal, Daytona, Vercel Sandbox) share.

- ``RecordingSupport``: the real ports (``sdk_ports.VendorSupport`` over ``sdk_call``) that also
  note every vendor call (operation, deadline, retry safety), every missed deadline and every
  retry wait.
- ``records_for`` / ``seed_record`` / ``record_of``: the real records store of a test profile.
- ``new_cloud``: a fake vendor account (``tests/fakes/backend/sdk_kit.Cloud``) under the test's tmp
  dir, with ``tmp`` and ``work`` dirs inside it to hand the backend as its temp and work dirs.
- ``scripts``: the scripts the fake sandbox ran (the last argv word of every command).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from indus.backends.sandbox_records import SandboxRecords
from indus.backends.sdk_call import BackendDeadline
from indus.backends.sdk_ports import ProfileRecords, VendorSupport
from indus.foundation.clock import Clock
from indus.foundation.scope import Scope
from tests.fakes.backend.sdk_kit import Cloud
from tests.fakes.clock import FakeClock


@dataclass
class CallRecord:
    op: str
    timeout: float
    idempotent: bool


class RecordingSupport(VendorSupport):
    """``VendorSupport`` with a log of its calls (see the module notes)."""

    def __init__(self, clock: Clock | None = None) -> None:
        self.retry_sleeps: list[float] = []
        super().__init__(clock=clock, sleeper=self._sleep, jitter=lambda: 1.0)
        self.calls: list[CallRecord] = []
        self.deadlines = 0

    async def _sleep(self, seconds: float) -> None:
        self.retry_sleeps.append(seconds)
        await self.clock.sleep(seconds)

    async def call[T](
        self,
        op: str,
        fn: Callable[[], T],
        *,
        timeout: float,
        idempotent: bool = False,
        on_deadline: Callable[[], None] | None = None,
    ) -> T:
        self.calls.append(CallRecord(op, timeout, idempotent))
        try:
            return await super().call(
                op, fn, timeout=timeout, idempotent=idempotent, on_deadline=on_deadline
            )
        except BackendDeadline:
            self.deadlines += 1
            raise

    def ops(self) -> list[tuple[str, float]]:
        return [(c.op, c.timeout) for c in self.calls]


def records_for(scope: Scope, kind: str) -> ProfileRecords:
    return ProfileRecords.for_scope(scope, kind)


def seed_record(scope: Scope, kind: str, task_key: str = "default", **fields: str) -> None:
    SandboxRecords.for_scope(scope, kind).put(task_key, **fields)


def record_of(scope: Scope, kind: str, task_key: str = "default") -> dict[str, str] | None:
    found = SandboxRecords.for_scope(scope, kind).get(task_key)
    if found is None:
        return None
    data = found.to_json()
    data.pop("created_at", None)
    data.pop("updated_at", None)
    return data


@dataclass
class Rig:
    cloud: Cloud
    tmp: Path
    work: Path


def new_cloud(tmp_path: Path, clock: FakeClock | None = None) -> Rig:
    cloud = Cloud(tmp_path / "cloud", clock=clock)
    return Rig(cloud, cloud.root / "tmp", cloud.root / "work")


def scripts(cloud: Cloud) -> list[str]:
    return [argv[-1] for argv in cloud.ran if argv]


def kwargs_of(cloud: Cloud, method: str) -> list[dict[str, Any]]:
    return [dict(call.kwargs) for call in cloud.calls_of(method)]
