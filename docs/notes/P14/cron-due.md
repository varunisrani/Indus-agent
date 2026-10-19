# Task: Due detection, lateness and catch-up (P14-06) Phase: 14 Owner: wave-17 builder C1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `cron/jobs.py` (`_compute_grace_seconds`,
`_classify_dispatch_lateness`, `_fast_forward_missed_recurring`, the due evaluation around it).
Closed before writing. Evidence: none.

What it does (own words): pure functions that look at one job, the time and a ledger view and
decide whether it fires now.

- Due = enabled, no pause marker, not `completed`, not in a quota hold (`quota_hold_until > now`),
  and `next_run_at ≤ now`.
- Grace = half the schedule's period clamped to [120 s, 7,200 s]; one-shots 120 s.
- Lateness (now − slot): ≤ 300 s on time; ≤ grace late (noted); beyond grace catch-up: the missed
  slots collapse into **one** run, identified by the oldest missed slot. With
  `cron.catch_up_missed: false` a job past grace does not run; its next run moves to the next slot
  after now and the skip is noted.
- A slot the ledger already has an attempt for is never run again: the job's next run moves past
  now (recurring) or the one-shot is closed.
- A one-shot past its 120 s grace is closed as missed without running — unless it came due during
  a store outage (`outage_since`), then it fires once.
- Paused jobs never fire and never catch up while paused; resume keeps a past slot (store,
  P14-03), so a job paused for two days fires once after resume (or logs a skip when catch-up is
  off).
- A recurring job in the `error` state is re-planned every pass (self-heal, P14-02).
- Every firing decision carries a provisional `next_run_at` (the next slot after now) for the
  ticker to write with its claim, and a `DispatchStamp`.

Inputs → outputs: `(job, now, tz, slots, catch_up_missed, outage_since)` → `DueDecision(job_id,
fire, scheduled_for, lateness, lateness_s, updates, note)`.

Files: `src/indus/cron/due.py`, `tests/unit/cron/test_due.py`; shared:
`src/indus/foundation/config/schema.py` (`cron.catch_up_missed`, default true) and its test.

Edge cases / failure modes:
- `next_run_at` missing on an enabled job → repaired (computed) without firing this pass;
- the period of an irregular cron (`0 9 * * 1,5`) is the gap between the next two runs;
- a job whose period cannot be computed gets the 120 s floor;
- quota hold in the past is ignored (C3 clears it).

Our design differences / decisions:
- decisions are values (no store writes inside); the ticker applies `updates` with its claim.

Tests we will write: the plan's table on a fake clock (hourly 3 h behind → one run, next slot in
the future; 5-min grace 150 s; daily grace 7,200 s; catch-up off → skip; paused 2 days then
resumed → one run; 299 s on time, 301 s late), plus ledger-attempted slot skipped, one-shot
grace and outage cover, quota hold, error self-heal, ordering of `collect_due`.

## Decisions for the lead
- none.

Compare checklist (filled after the rewrite; reopened `cron/jobs.py` grace / lateness / fast-forward):
- [x] 300 s / half period [120, 7,200] / collapse to one / paused never catch up
  (`tests/unit/cron/test_due.py::test_grace_values`, `::test_hourly_three_hours_behind_runs_once`,
  `::test_hourly_job_lateness`)
- [x] catch-up off skips; resume keeps the slot (`::test_catch_up_off_skips`,
  `::test_paused_two_days_then_resumed_runs_once`)
- Deliberate differences: decisions are values; the stale-cron re-anchor and timezone-migration
  classes are not needed (UTC instants); the reference's pending-slot stamp is replaced by the
  ledger claim (the slot is durable before dispatch).
- Not taken: nothing in scope.

## Status

Done (2026-10-19). Shared: `src/indus/foundation/config/schema.py` (`cron.catch_up_missed`) and
`tests/unit/foundation/config/test_schema.py`.
