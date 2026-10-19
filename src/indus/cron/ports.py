"""The seams between the cron modules and their host — every call across them goes through here.

Two views of the same jobs:

- ``CronPorts`` (the ticker and the runner, P14-07..12): ``JobSpec`` is the read-only view of a job
  record a run uses (``spec_of`` is the one adapter over the store's record type); the bundle holds
  the job store (locked read-modify-write), the execution ledger, the store health probe, the due
  rules, next-run computation, the notepad block, the monitor gate and the after-run ``finish``
  (delivery, bookkeeping, incidents).
- ``CronBackend`` (the surfaces, P14-13..19: ``delivery``, ``bookkeeping``, ``tool``, the CLI and
  ``/cron``): create, edit, pause, run and inspect jobs.

There is one way in: the host sets ``BACKEND_FACTORY`` (``service/cron_host.py``, an adapter over
the store, ledger, notepad and health of ``cron.context`` and the ticker); that backend also
builds the ``CronPorts`` its ticker runs on. Tests set their own factory or pass ``CronPorts``
straight to a ``RunEnv``.

Jobs are read with ``field(job, name)``, which accepts the store's model objects and plain
mappings alike, using the field names of the job model (``id``, ``name``, ``prompt``, ``skills``,
``schedule``, ``schedule_display``, ``repeat {times, completed}``, ``enabled``, ``state``,
``next_run_at``, ``last_run_at``, ``last_status``, ``last_error``, ``last_delivery_error``,
``last_delivery_unverified``, ``failure_streak``, ``deliver``, ``failure_deliver``, ``origin``,
``script``, ``no_agent``, ``monitor_script``, ``monitor_url``, ``monitor_state``, ``workdir``,
``context_from``, ``quota_hold_until``, ``unreachable_retry``, ``last_dispatch``, ``created_at`` …).

``cron_setting(scope, "wrap_response")`` reads ``cron.<key>`` from the profile's config with the
schema default behind it.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from indus.foundation.config.schema import default_for
from indus.foundation.scope import Scope

if TYPE_CHECKING:
    from indus.cron.classify import RunOutcome

__all__ = [
    "AttemptKind",
    "BACKEND_FACTORY",
    "CronBackend",
    "CronPorts",
    "DueSlot",
    "JobAmbiguous",
    "JobNotFound",
    "JobSpec",
    "JobStorePort",
    "LedgerPort",
    "MonitorCheck",
    "NotepadPort",
    "RunReport",
    "StoreHealthView",
    "StoreUnavailable",
    "backend_for",
    "cron_dir",
    "cron_setting",
    "field",
    "flag_setting",
    "is_recurring",
    "iso",
    "job_output_dir",
    "number_setting",
    "origin_of",
    "output_dir",
    "parse_instant",
    "repeat_of",
    "schedule_kind",
    "scripts_dir",
    "spec_of",
]

AttemptKind = Literal["scheduled", "manual", "retry"]
ScheduleKind = Literal["once", "interval", "cron"]


# -- the job view ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class JobSpec:
    """What the runner reads from one job record (authored fields plus its schedule kind)."""

    id: str
    name: str | None = None
    prompt: str | None = None
    skills: tuple[str, ...] = ()
    schedule_kind: ScheduleKind = "interval"
    schedule_display: str = ""
    deliver: str = "local"
    failure_deliver: str | None = None
    origin: Mapping[str, Any] | None = None
    enabled_toolsets: tuple[str, ...] | None = None
    workdir: str | None = None
    model: str | None = None
    provider: str | None = None
    base_url: str | None = None
    pinned: bool = False
    reasoning_effort: str | None = None
    script: str | None = None
    interpreter: str | None = None
    no_agent: bool = False
    monitor_script: str | None = None
    monitor_url: str | None = None
    context_from: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return self.name or self.id

    @property
    def recurring(self) -> bool:
        return self.schedule_kind != "once"

    @property
    def route_pinned(self) -> bool:
        """The job carries its own model, provider or endpoint (or was pinned explicitly)."""
        return self.pinned or any(
            isinstance(v, str) and v.strip() for v in (self.model, self.provider, self.base_url)
        )

    @property
    def has_monitor(self) -> bool:
        return bool(self.monitor_script or self.monitor_url)


def _field(record: Any, name: str) -> Any:
    if isinstance(record, Mapping):
        return record.get(name)  # pyright: ignore[reportUnknownMemberType]
    return getattr(record, name, None)


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _names(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, Sequence):
        items: Sequence[object] = value  # pyright: ignore[reportUnknownVariableType]
        return tuple(str(v).strip() for v in items if str(v).strip())
    return ()


def _schedule_kind(schedule: object) -> ScheduleKind:
    kind = _field(schedule, "kind") if schedule is not None else None
    if not isinstance(kind, str):
        kind = type(schedule).__name__
    lowered = kind.lower()
    if lowered in ("once", "interval", "cron"):
        return lowered  # type: ignore[return-value]
    return "interval"


def spec_of(record: Any) -> JobSpec:
    """The ``JobSpec`` of a store record (an object with attributes, or a mapping)."""
    toolsets = _field(record, "enabled_toolsets")
    origin = _field(record, "origin")
    if origin is not None and not isinstance(origin, Mapping):
        dump = getattr(origin, "model_dump", None)
        origin = dump() if callable(dump) else None
    return JobSpec(
        id=str(_field(record, "id") or ""),
        name=_text(_field(record, "name")),
        prompt=_field(record, "prompt") if isinstance(_field(record, "prompt"), str) else None,
        skills=_names(_field(record, "skills")),
        schedule_kind=_schedule_kind(_field(record, "schedule")),
        schedule_display=str(_field(record, "schedule_display") or ""),
        deliver=_text(_field(record, "deliver")) or "local",
        failure_deliver=_text(_field(record, "failure_deliver")),
        origin=origin,  # pyright: ignore[reportArgumentType]
        enabled_toolsets=None if toolsets is None else _names(toolsets),
        workdir=_text(_field(record, "workdir")),
        model=_text(_field(record, "model")),
        provider=_text(_field(record, "provider")),
        base_url=_text(_field(record, "base_url")),
        pinned=_field(record, "pinned") is True,
        reasoning_effort=_text(_field(record, "reasoning_effort")),
        script=_text(_field(record, "script")),
        interpreter=_text(_field(record, "interpreter")),
        no_agent=_field(record, "no_agent") is True,
        monitor_script=_text(_field(record, "monitor_script")),
        monitor_url=_text(_field(record, "monitor_url")),
        context_from=_names(_field(record, "context_from")),
    )


# -- the seams ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DueSlot:
    """One job the due rules want fired now (C1, P14-06)."""

    job: Any
    scheduled_for: datetime | None
    lateness: Literal["on_time", "late", "catch_up"] = "on_time"
    # Scheduler fields the due rules want written with the claim (next run, dispatch stamp).
    updates: Mapping[str, Any] = dc_field(default_factory=dict[str, Any])


@dataclass(frozen=True)
class MonitorCheck:
    """The monitor gate's answer (C3, P14-16): ``no_change`` suppresses the run."""

    state: Literal["changed", "no_change", "error"]
    block: str = ""
    error: str = ""


class JobStorePort(Protocol):
    """The job store (C1, P14-03)."""

    def load(self) -> list[Any]:
        """Every job record."""
        ...

    def find(self, ref: str) -> Any | None:
        """The record ``ref`` (id or name) names, or None."""
        ...

    def edit[T](self, job_id: str, change: Callable[[Any], T]) -> T | None:
        """Run ``change(record)`` under the store lock and save the record it may have changed.
        None when the job does not exist."""
        ...


class LedgerPort(Protocol):
    """The execution ledger (C1, P14-04)."""

    def reap(self, now: datetime) -> int: ...

    def begin(
        self, job_id: str, scheduled_for: datetime | None, kind: AttemptKind, now: datetime
    ) -> str | None:
        """Record a claimed attempt before dispatch; None when that slot already completed."""
        ...

    def running(self, attempt_id: str, now: datetime) -> None: ...

    def heartbeat(self, attempt_id: str, now: datetime) -> None: ...

    def finish(
        self,
        attempt_id: str,
        *,
        state: Literal["completed", "failed"],
        result_status: str,
        now: datetime,
        error: str | None = None,
    ) -> None: ...

    def note_delivery(self, attempt_id: str, status: str) -> None:
        """Fill in the attempt's delivery status once it is known."""
        ...


@dataclass
class CronPorts:
    """Everything the ticker needs from the other slices (see the module notes)."""

    store: JobStorePort
    ledger: LedgerPort
    # C1 P14-05: True when the jobs file and the ledger can be written right now.
    store_writable: Callable[[datetime], bool]
    # C1 P14-06: the jobs to fire now (lateness and catch-up already decided).
    due: Callable[[datetime], list[DueSlot]]
    # C1 P14-02: the following slot of a recurring job after ``now`` (None: cannot compute).
    next_run: Callable[[Any, datetime], datetime | None]
    # C1 P14-17: the notepad block for a job's prompt ("" when empty).
    notepad: Callable[[str], str] = lambda _job_id: ""
    # C3 P14-16: the monitor gate; None when the slice is not wired.
    monitor: Callable[[JobSpec], Awaitable[MonitorCheck]] | None = None
    # C3 P14-13/14/15: delivery, bookkeeping and incidents after a run; the delivery status.
    finish: Callable[[JobSpec, RunOutcome], Awaitable[str | None]] | None = None
    # C3 P14-13: platforms with a registered deliverer (``local`` is always known).
    deliverers: Callable[[], frozenset[str]] = dc_field(default=lambda: frozenset({"local"}))


def job_output_dir(home: Path, job_id: str) -> Path:
    return home / "cron" / "output" / job_id


# -- the surfaces' port --------------------------------------------------------------------------

log = logging.getLogger(__name__)

RECURRING_KINDS = frozenset({"interval", "cron"})


class StoreUnavailable(RuntimeError):
    """The cron store cannot be used here (not wired in this build, or not writable)."""


class JobNotFound(LookupError):
    def __init__(self, ref: str) -> None:
        super().__init__(f"no cron job matches {ref!r}")
        self.ref = ref


class JobAmbiguous(LookupError):
    def __init__(self, ref: str, candidates: Sequence[str]) -> None:
        listed = ", ".join(candidates)
        super().__init__(f"{ref!r} names more than one job; use an id: {listed}")
        self.ref = ref
        self.candidates = tuple(candidates)


@dataclass(frozen=True)
class RunReport:
    """The end of one run, as the surfaces show it.

    ``status``: ``ok``, ``failed``, ``silent``, ``no_change``, ``blocked`` (preflight) or
    ``skipped`` (wake gate, claim lost). ``delivery``: the delivery status, or None when nothing
    was due to be sent.
    """

    job_id: str
    status: str
    delivery: str | None = None
    output_path: Path | None = None
    error: str | None = None
    excerpt: str = ""


@dataclass(frozen=True)
class StoreHealthView:
    writable: bool
    reason: str | None = None
    since: datetime | None = None


class NotepadPort(Protocol):
    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str) -> None: ...

    def unset(self, key: str) -> bool: ...

    def items(self) -> list[tuple[str, str]]: ...


class CronBackend(Protocol):
    """The job store, ledger and ticker as one port (see the module notes).

    ``resolve`` takes an id or a case-insensitive name (exact id wins) and raises ``JobNotFound``
    or ``JobAmbiguous``. ``create`` takes the authored fields (``schedule`` as the text the person
    typed) and raises ``ValueError`` with a message for a bad schedule or spec. ``update`` takes
    the changed authored fields; ``None`` clears an optional one.
    """

    def list_jobs(self, *, include_disabled: bool = True) -> list[Any]: ...

    def resolve(self, ref: str) -> Any: ...

    def get(self, job_id: str) -> Any | None: ...

    def create(self, spec: Mapping[str, Any]) -> Any: ...

    def update(self, job_id: str, changes: Mapping[str, Any]) -> Any: ...

    def pause(self, job_id: str, reason: str | None = None) -> Any: ...

    def resume(self, job_id: str) -> Any: ...

    def remove(self, job_id: str) -> bool: ...

    def is_running(self, job_id: str) -> bool: ...

    def run_now(
        self, job_id: str, *, context: str | None = None
    ) -> Coroutine[Any, Any, RunReport]: ...

    def runs(self, job_id: str | None = None, *, limit: int = 20) -> list[Mapping[str, Any]]: ...

    def store_health(self) -> StoreHealthView: ...

    def global_pause(self) -> tuple[bool, str | None]: ...

    def set_global_pause(self, paused: bool, reason: str | None = None) -> None: ...

    def notepad(self, job_id: str) -> NotepadPort: ...

    def tick(self, out: Callable[[str], object] | None = None) -> int:
        """One scheduler pass that waits for its runs; ``out`` gets a line per started job."""
        ...

    def daemon(self) -> int: ...


BACKEND_FACTORY: Callable[[Scope], CronBackend] | None = None
"""Set by the host (and tests); ``backend_for`` raises ``StoreUnavailable`` while it is None."""


def backend_for(scope: Scope) -> CronBackend:
    factory = BACKEND_FACTORY
    if factory is None:
        raise StoreUnavailable("the cron job store is not available in this build")
    return factory(scope)


# -- reading jobs ---------------------------------------------------------------------------------


def field(job: Any, name: str, default: Any = None) -> Any:
    """``job.name`` for model objects, ``job[name]`` for mappings; ``default`` when absent."""
    if isinstance(job, Mapping):
        value = cast(Mapping[str, Any], job).get(name, default)
    else:
        value = getattr(job, name, default)
    return default if value is None else value


def repeat_of(job: Any) -> tuple[int | None, int]:
    """``(times, completed)``; ``times`` None or ≤ 0 means forever (None is returned for both)."""
    repeat = field(job, "repeat")
    times = field(repeat, "times") if repeat is not None else None
    completed = field(repeat, "completed", 0) if repeat is not None else 0
    try:
        count = int(times) if times is not None else None
    except (TypeError, ValueError):
        count = None
    try:
        done = max(0, int(completed))
    except (TypeError, ValueError):
        done = 0
    return (count if count is not None and count > 0 else None), done


def schedule_kind(job: Any) -> str:
    """``once``, ``interval``, ``cron`` or ``""`` when the record has no readable schedule."""
    schedule = field(job, "schedule")
    if schedule is None:
        return ""
    kind = field(schedule, "kind", "")
    if kind:
        return str(kind).lower()
    name = type(schedule).__name__.lower()  # tagged-union members without a ``kind`` field
    return next((k for k in ("once", "interval", "cron") if k in name), "")


def is_recurring(job: Any) -> bool:
    return schedule_kind(job) in RECURRING_KINDS


def origin_of(job: Any) -> dict[str, str] | None:
    """The stored origin as ``{platform, chat_id, thread_id?}``, or None."""
    origin = field(job, "origin")
    if origin is None:
        return None
    platform = str(field(origin, "platform", "") or "").strip()
    chat = str(field(origin, "chat_id", "") or "").strip()
    if not platform or not chat:
        return None
    out = {"platform": platform, "chat_id": chat}
    thread = field(origin, "thread_id")
    if thread is not None and str(thread).strip():
        out["thread_id"] = str(thread).strip()
    return out


def parse_instant(value: object) -> datetime | None:
    """A stored timestamp as an aware datetime (naive text is taken as UTC); None if unreadable."""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


# -- paths and settings ---------------------------------------------------------------------------


def cron_dir(scope: Scope) -> Path:
    return scope.home / "cron"


def output_dir(scope: Scope, job_id: str) -> Path:
    return cron_dir(scope) / "output" / job_id


def scripts_dir(scope: Scope) -> Path:
    return scope.home / "scripts"


def cron_setting(scope: Scope, key: str) -> Any:
    """``cron.<key>`` from the profile config, else the schema default (never raises)."""
    path = f"cron.{key}"
    value: Any = None
    try:
        config: Any = scope.config
        if config is None:
            from indus.foundation import config as config_mod

            config = config_mod.load(scope)
        value = config.get(path)
    except Exception:
        log.debug("config read failed for %s", path, exc_info=True)
    return default_for(path) if value is None else value


def number_setting(scope: Scope, key: str, fallback: float) -> float:
    value = cron_setting(scope, key)
    if isinstance(value, bool):
        return fallback
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def flag_setting(scope: Scope, key: str, fallback: bool) -> bool:
    value = cron_setting(scope, key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "on", "1"):
            return True
        if lowered in ("false", "no", "off", "0"):
            return False
    return fallback
