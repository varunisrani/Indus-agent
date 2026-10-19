"""Typed errors of the cron package (codes ``cron.*``).

Validation errors (bad schedule, invalid job, notepad limits) are also ``ValueError`` so callers
that only know the standard library still catch them.
"""

from __future__ import annotations

from indus.foundation.errors import ERROR_CODES, IndusError

__all__ = [
    "AmbiguousJobRef",
    "AttemptFinal",
    "CronError",
    "CronStoreBusy",
    "InvalidJob",
    "JobNotFound",
    "NextRunError",
    "NotepadLimit",
    "OneShotInPast",
    "ScheduleError",
    "SlotTaken",
]

CRON_ERROR_CODES: dict[str, str] = {
    code: text for code, text in ERROR_CODES.items() if code.startswith("cron.")
}
"""The ``cron.*`` rows of the one error-code table (``foundation.errors.ERROR_CODES``)."""


class CronError(IndusError):
    default_code = "cron.error"


class ScheduleError(CronError, ValueError):
    default_code = "cron.bad_schedule"


class OneShotInPast(ScheduleError):
    default_code = "cron.oneshot_past"


class NextRunError(CronError):
    default_code = "cron.next_run_failed"


class InvalidJob(CronError, ValueError):
    default_code = "cron.invalid_job"


class JobNotFound(CronError, LookupError):
    default_code = "cron.job_not_found"


class AmbiguousJobRef(CronError, LookupError):
    default_code = "cron.ambiguous_job"

    def __init__(self, ref: str, candidates: tuple[str, ...]) -> None:
        super().__init__(
            f"{len(candidates)} jobs are named {ref!r} ({', '.join(candidates)}); use the job id"
        )
        self.ref = ref
        self.candidates = candidates


class CronStoreBusy(CronError):
    default_code = "cron.store_busy"


class SlotTaken(CronError):
    default_code = "cron.slot_taken"


class AttemptFinal(CronError):
    default_code = "cron.attempt_final"


class NotepadLimit(CronError, ValueError):
    default_code = "cron.notepad_limit"
