"""The cron job record and what a create request carries (P14-03).

Fields are split in two: *authored* fields describe the job (what a person or an import sets) and
*scheduler* fields record what happened to it (run state, counters, claims). An import refreshes
authored fields and keeps scheduler state (``merge_authored``). ``enabled`` is the authoritative
switch; ``state == "paused"`` or a ``paused_at`` stamp is a second gate, so a half-paused record
never fires.

All datetimes are aware UTC. Unknown keys (a newer build's fields) are kept so a rewrite never
drops them.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from indus.cron.grammar import CronSchedule, IntervalSchedule, OnceSchedule, Schedule
from indus.cron.zones import UtcDatetime

__all__ = [
    "AUTHORED_FIELDS",
    "JOB_ID_PATTERN",
    "SCHEDULER_FIELDS",
    "CronJob",
    "UnreachableRetry",
    "DispatchStamp",
    "FireClaim",
    "JobSpec",
    "JobState",
    "MonitorState",
    "Origin",
    "Repeat",
    "derive_label",
    "merge_authored",
    "new_job_id",
]

JobState = Literal["scheduled", "paused", "completed", "error"]
LatenessClass = Literal["on_time", "late", "catch_up"]

# New ids are 12 lowercase hex characters; loaded ids only have to be safe as a path component.
JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
LABEL_CHARS = 50


def new_job_id() -> str:
    return secrets.token_hex(6)


class _Part(BaseModel):
    model_config = ConfigDict(extra="allow")


class Repeat(_Part):
    """``times`` None = forever; ``completed`` counts finished scheduled runs."""

    times: int | None = None
    completed: int = Field(default=0, ge=0)

    @field_validator("times")
    @classmethod
    def _forever_when_not_positive(cls, value: int | None) -> int | None:
        return None if value is None or value <= 0 else value

    @property
    def exhausted(self) -> bool:
        return self.times is not None and self.completed >= self.times


class Origin(_Part):
    platform: str
    chat_id: str
    thread_id: str | None = None


class FireClaim(_Part):
    owner: str
    claimed_at: UtcDatetime
    heartbeat_at: UtcDatetime


class MonitorState(_Part):
    hash: str | None = None
    last_output_path: str | None = None


class UnreachableRetry(_Part):
    """The unreachable-model re-run ladder (P14-14): rung ``attempt`` fires at ``at``; ``expr``
    fingerprints the schedule it was planned for (a changed schedule drops the ladder)."""

    attempt: int = Field(default=0, ge=0)
    at: UtcDatetime
    expr: str | None = None


class DispatchStamp(_Part):
    scheduled_for: UtcDatetime
    dispatched_at: UtcDatetime
    lateness_s: float
    lateness: LatenessClass


AUTHORED_FIELDS: frozenset[str] = frozenset(
    {
        "name",
        "prompt",
        "skills",
        "schedule",
        "schedule_display",
        "deliver",
        "failure_deliver",
        "origin",
        "enabled_toolsets",
        "workdir",
        "model",
        "provider",
        "base_url",
        "pinned",
        "reasoning_effort",
        "script",
        "interpreter",
        "no_agent",
        "monitor_script",
        "monitor_url",
        "context_from",
    }
)
# ``repeat`` is split: ``times`` is authored, ``completed`` is scheduler-owned.
SCHEDULER_FIELDS: frozenset[str] = frozenset(
    {
        "enabled",
        "state",
        "paused_at",
        "paused_reason",
        "next_run_at",
        "last_run_at",
        "last_status",
        "last_error",
        "last_delivery_error",
        "last_delivery_unverified",
        "failure_streak",
        "monitor_state",
        "quota_hold_until",
        "fire_claim",
        "last_dispatch",
        "schedule_error",
        "unreachable_retry",
    }
)


class CronJob(BaseModel):
    model_config = ConfigDict(extra="allow", validate_assignment=True)

    id: str
    created_at: UtcDatetime

    # -- authored --------------------------------------------------------------------------------
    name: str | None = None
    prompt: str | None = None
    skills: list[str] = Field(default_factory=list[str])
    schedule: Schedule
    schedule_display: str = ""
    repeat: Repeat = Field(default_factory=Repeat)
    deliver: str = "local"
    failure_deliver: str | None = None
    origin: Origin | None = None
    enabled_toolsets: list[str] | None = None
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
    context_from: list[str] = Field(default_factory=list[str])

    # -- scheduler-owned -------------------------------------------------------------------------
    enabled: bool = True
    state: JobState = "scheduled"
    paused_at: UtcDatetime | None = None
    paused_reason: str | None = None
    next_run_at: UtcDatetime | None = None
    last_run_at: UtcDatetime | None = None
    last_status: str | None = None
    last_error: str | None = None
    last_delivery_error: str | None = None
    last_delivery_unverified: list[str] | None = None
    failure_streak: int = Field(default=0, ge=0)
    monitor_state: MonitorState | None = None
    quota_hold_until: UtcDatetime | None = None
    fire_claim: FireClaim | None = None
    last_dispatch: DispatchStamp | None = None
    schedule_error: str | None = None
    unreachable_retry: UnreachableRetry | None = None

    @field_validator("id")
    @classmethod
    def _safe_id(cls, value: str) -> str:
        if not JOB_ID_PATTERN.match(value):
            raise ValueError("job id must be 1-64 letters, digits, '_' or '-'")
        return value

    # -- derived ---------------------------------------------------------------------------------

    @property
    def label(self) -> str:
        return self.name or derive_label(self.prompt, self.skills, self.script) or self.id

    @property
    def recurring(self) -> bool:
        return not isinstance(self.schedule, OnceSchedule)

    @property
    def has_pause_marker(self) -> bool:
        return self.state == "paused" or self.paused_at is not None

    @property
    def paused(self) -> bool:
        return not self.enabled or self.has_pause_marker

    @property
    def runnable(self) -> bool:
        """May the scheduler fire it (ignoring time)?"""
        finished = self.state in ("completed", "error")
        return self.enabled and not self.has_pause_marker and not finished

    @property
    def display_state(self) -> JobState:
        """What a person should see: never "scheduled" for a job that will not fire."""
        if self.state in ("completed", "error"):
            return self.state
        return "paused" if self.paused else "scheduled"

    def to_record(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def derive_label(prompt: str | None, skills: list[str], script: str | None) -> str:
    """A short label from the payload: first prompt line, else first skill, else the script."""
    if prompt and prompt.strip():
        line = prompt.strip().splitlines()[0].strip()
        return line[:LABEL_CHARS].rstrip()
    if skills:
        return skills[0][:LABEL_CHARS]
    if script:
        return script.strip()[:LABEL_CHARS]
    return ""


class JobSpec(BaseModel):
    """A create request: authored fields, the schedule as text or parsed, and pause options."""

    model_config = ConfigDict(extra="forbid")

    schedule: str | OnceSchedule | IntervalSchedule | CronSchedule
    prompt: str | None = None
    name: str | None = None
    skills: list[str] = Field(default_factory=list[str])
    repeat: int | None = None
    deliver: str | None = None
    failure_deliver: str | None = None
    origin: Origin | None = None
    enabled_toolsets: list[str] | None = None
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
    context_from: list[str] = Field(default_factory=list[str])
    paused: bool = False
    paused_reason: str | None = None


def merge_authored(local: CronJob, authored: Mapping[str, Any]) -> CronJob:
    """``local`` with the authored fields of ``authored``; scheduler state is kept.

    ``authored["repeat"]`` may be a mapping or a number; only its ``times`` is taken. The caller
    re-plans ``next_run_at`` when the schedule changed.
    """
    data = local.model_dump(mode="python")
    for key in AUTHORED_FIELDS:
        if key in authored:
            data[key] = authored[key]
    if "repeat" in authored:
        raw = authored["repeat"]
        times = raw.get("times") if isinstance(raw, Mapping) else raw
        data["repeat"] = {"times": times, "completed": local.repeat.completed}
    return CronJob.model_validate(data)
