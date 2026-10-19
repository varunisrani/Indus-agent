# Task: Next-run computation (P14-02) Phase: 14 Owner: wave-17 builder C1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `cron/jobs.py` (`compute_next_run`,
the one-shot grace helpers, `_oneshot_past_grace_error`, recoverable error state). Closed before
writing. Traps R07.11, R07.12. Evidence: none.

What it does (own words): given a schedule, the configured zone and a base time, says when the
job runs next.
- Interval: `(last_run_at or now) + minutes`, added in UTC (a run that started late shifts the
  following ones by the same amount — kept and documented, R07.11). An anchored interval
  (`every 3d at 09:00`) returns the first `anchor + k·period` strictly after the base, so it
  never drifts.
- Cron: matched on the configured zone's wall clock, then converted to UTC. A wall time in the
  repeated fall-back hour has two instants; both are tried and the earliest one strictly after
  the base wins, so a job that ran in the first pass is not run again in the second. A wall time
  in the spring-forward gap does not exist; it maps to the first valid instant after the gap
  (02:30 on the change night → 03:00 local) and only once.
- Once: its instant until it has run, then nothing. At create/update a one-shot more than 120 s in
  the past is refused (`OneShotInPast`); up to 120 s late is accepted and runs at once.
- A recurring job whose next run cannot be computed (broken expression, croniter missing) is put
  in the recoverable `error` state with the reason (`schedule_error`), never `completed`; the due
  pass (P14-06) retries every tick and returns it to `scheduled` once the cause is fixed.

Inputs → outputs: `(schedule, now, tz, last_run_at)` → `datetime | None`; `plan_next(job, now, tz)`
→ `NextRunPlan(next_run_at, state, schedule_error)`.

Files: `src/indus/cron/next_run.py`, `tests/unit/cron/test_next_run.py`.

Edge cases / failure modes:
- `tz` None means the system zone: we load it as a DST-aware zone (`TZ`, then `/etc/localtime`),
  falling back to the fixed current offset only when neither is readable;
- base exactly on a slot → the following slot (strictly after);
- an expression that never matches (`0 0 30 2 *`) → `NextRunError` after a bounded search;
- spring-forward with a 15-minute cron: all slots inside the gap collapse into the one instant
  after the gap;
- once with `last_run_at` set → None (completed by bookkeeping).

Our design differences / decisions:
- the gap rule fires at the gap's end (03:00), as the plan's verify column asks, rather than at the
  pre-transition offset reading (03:30);
- `schedule_error` is a field of its own so a later run error does not overwrite the reason.

Tests we will write: fall-back `30 1 * * *` America/New_York fires once (bases in both folds);
spring-forward `30 2 * * *` → 03:00 local once, next day back to 02:30; 15-minute cron through
the gap; interval anchored on last run; anchored interval lattice; Europe/London and
Australia/Lord_Howe (30-minute shift) rows; leap day `0 9 29 2 *`; one-shot 121 s past refused,
119 s accepted; broken expr → `error`, fixed → `scheduled`; seeded property: next run strictly
after the base for many expressions, zones and bases.

## Decisions for the lead
- none beyond the gap rule above.

Compare checklist (filled after the rewrite; reopened `cron/jobs.py` `compute_next_run`):
- [x] interval anchored on last run in UTC (`tests/unit/cron/test_next_run.py::test_interval_anchored_on_last_run`)
- [x] both-folds guard, strictly after base (`::test_fall_back_fires_once_from_first_fold`,
  `::test_fall_back_base_in_second_fold`, `::test_next_run_strictly_after_base`)
- [x] 120 s one-shot grace (`::test_oneshot_grace`); recurring never silently completes
  (`::test_broken_expression_goes_to_error_then_heals`)
- [x] gap found while testing: a run that started late inside the repeated hour, re-planned from
  its start time, picked the second 01:30 — fixed by stepping cron from the slot
  (`following_slot`, `plan_next(slot=…)`, the due pass uses it too), pinned by
  `::test_fall_back_fires_once_from_first_fold` and
  `tests/unit/cron/test_due.py::test_late_run_in_repeated_hour_not_doubled`
- Deliberate differences: a wall time in a spring-forward gap fires at the end of the gap (03:00)
  where the reference's reading lands at 03:30; a never-matching expression is an error after a
  bounded search; the system zone is loaded DST-aware from `/etc/localtime`.
- Not taken: the reference's stale-offset / timezone-migration repairs (we store UTC instants and
  re-plan on the wall clock, so there is no stored offset to repair).

## Status

Done (2026-10-19). Files: `src/indus/cron/next_run.py`, `src/indus/cron/zones.py`,
`tests/unit/cron/test_next_run.py`.
