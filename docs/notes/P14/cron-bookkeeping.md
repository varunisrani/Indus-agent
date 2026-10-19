# Task: Bookkeeping, failure streak, unreachable re-runs, quota hold (P14-14) Phase: 14 Owner: wave-17 builder C3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `cron/jobs.py` (`_record_run_outcome`,
`_advance_after_run`, the `mark_job_run` order of ladder/hold), `cron/unreachable_retry.py` (all),
`cron/quota_hold.py` (all), `cron/scheduler.py` (`_failure_streak_nudge` only). Research
§3.15.7. Closed before writing.

What it does (own words): after every run the job record is brought up to date in one step —
counts, streak, status fields, the next run — and two narrow recovery rules can pull the next run
earlier or push it later:
- a recurring job whose run never reached the model because the network or DNS was down gets up
  to three quiet re-runs (+5, +15, +30 min), never past its next natural slot; the interim failure
  notices are held back; a run that reaches the model ends the ladder;
- a recurring job whose provider says its usage window is closed (retry-after known) while the
  whole fallback chain is unusable is parked until the window reopens: one alert, the slots inside
  the window collapse into one, and a sparse cron schedule may take one recovery run at the
  reopening boundary when that is at least half a period before its next slot.

Inputs → outputs: `settle_run(job, outcome, now=…, settings=…, next_run=…, period=…)` →
`Settlement{changes: dict, completed: bool, retry_at, held_until, nudge}`; pure, so the caller (C2's
runner, inside C1's locked update) writes `changes` onto the record. Helpers:
`failure_nudge(job, settings)`, `will_retry(...)` (hold the interim notice?),
`is_model_unreachable(error, model_calls)`, `usage_window_seconds(error, now, model_calls)`,
`hold_notice(...)`.

Fields written (phase-file names): `last_run_at`, `last_status`, `last_error`,
`last_delivery_error`, `last_delivery_unverified`, `failure_streak`, `repeat.completed` (as a
`repeat` mapping `{times, completed}`), `next_run_at`, `state`, `fire_claim` (cleared),
`quota_hold_until`, plus one field not in the phase list: `unreachable_retry {attempt, at}` (see
decisions).

Rules:
- `repeat.completed += 1` unless the run was a ladder re-run; a finite repeat that reaches its
  count → `state=completed`, `next_run_at=None` (record kept);
- next run from C1's `next_run(schedule, base)`; `None` on a recurring job → `state=error` with a
  reason (recoverable, R07.12); `None` on a one-shot → `completed`;
- `failure_streak`: +1 for a failed run (pre-agent crashes included), reset to 0 on success;
  a delivery failure never touches it (R07.17);
- nudge (fix / pause / remove, our wording) when a recurring job's streak including this run
  reaches `cron.failure_nudge_threshold` (3; 0 off); never for one-shots;
- ladder off with `cron.retry_unreachable: false`; one-shots and paused jobs excluded;
- quota hold never for one-shots or when the run made a model call before the 429 (mid-run).

Edge cases / failure modes: ladder exhausted → back to the natural schedule and the state cleared
so the next slot starts a fresh ladder; next natural slot at or before the rung → no re-run;
schedule edited between rung and fire → the rung is not treated as a re-run (expression
fingerprint); retry-after missing → no hold; a hold that already holds (`quota_hold_until` set)
does not take a second recovery run.

Our design differences / decisions:
- one pure function returning a change set instead of mutating a dict under the store lock;
- the "reached the model" fact comes from the runner as `model_calls` (count of answered model
  calls), and "transient network" from the P03 classifier (`connection` reason); timeouts are not
  counted as unreachable (a timeout may have reached the model);
- the window length comes from the P03 classifier (`reset_at` preferred, else `retry_after`),
  reasons `rate_limit` / `billing` only, with 60 s slack.

Tests we will write: repeat 3 completes after 3 runs (record kept); streak 3 adds the nudge, success
resets, one-shot never nudges; delivery failure leaves the streak; DNS failure → re-runs at +5,
+15, +30 min (fake clock) then the natural slot; rung beyond the next slot skipped; one-shot not
retried; re-run does not count toward repeat; quota hold parks until retry-after and coalesces cron
slots; sparse recovery fire; mid-run 429 not held; one-shot not held; recurring next-run failure →
`error`, not `completed`.

## Decisions for the lead
- The ladder needs one persisted field C1's model does not list: `unreachable_retry: {attempt:
  int, at: ISO, expr: str|None} | None` (scheduler-owned). Add it to `CronJob`.
- The ticker must treat a fire at `unreachable_retry.at` as a retry (`kind=retry` in the ledger)
  and pass `retry_run=True` in the outcome; `is_retry_fire(job, scheduled_for)` answers that.

Compare checklist (reopened `cron/jobs.py` `_record_run_outcome`/`_advance_after_run`/`mark_job_run`,
`cron/unreachable_retry.py`, `cron/quota_hold.py`, `_failure_streak_nudge`):
- [x] repeat counted once per slot, re-runs not counted, finite repeat → completed with the record kept (`tests/unit/cron/test_bookkeeping.py::test_repeat_three_completes_after_three_runs_and_keeps_the_record`, `::test_dns_failure_reruns_after_5_15_30_minutes_then_the_natural_slot`)
- [x] recurring job with no computable next run → `error`, recovers when fixed (`::test_recurring_job_whose_next_run_fails_goes_to_error_never_completed`)
- [x] streak +1 on failure, reset on success, delivery failure not counted (`::test_streak_three_adds_the_nudge_and_success_resets`, `test_delivery.py::test_delivery_failure_leaves_the_failure_streak`)
- [x] nudge at the threshold counting this run; 0 off; one-shots never (`::test_nudge_threshold_zero_and_one_shots`)
- [x] ladder 5/15/30 min, never at or past the natural slot, exhausted → natural slot and reset, paused/one-shots/off excluded, reached-model clears (`::test_rung_never_passes_the_next_natural_slot`, `::test_ladder_off_one_shots_and_model_reached`)
- [x] rung identified with the schedule fingerprint (`::test_retry_fire_needs_the_same_schedule`)
- [x] quota hold: window + 60 s slack, cron slots coalesced to the first after the window, interval parked at the boundary, one recovery fire for sparse cron when the next slot is ≥ half a period later, not for manual runs or an already-held job (`::test_quota_hold_parks_until_retry_after_and_coalesces_cron_slots`, `::test_sparse_cron_takes_one_recovery_run_at_the_boundary`)
- [x] mid-run 429 and one-shots never held (`::test_mid_run_429_and_one_shots_are_not_held`)
- [x] decision: an already-held job that is parked again at the boundary sends no second alert (`notify=False`), so a closed window costs one alert as the row asks (`::test_quota_hold_parks_until_retry_after_and_coalesces_cron_slots`).
- Deliberate differences: the reference anchors the quota hold on one provider's auth-error type
  and a "retry after Ns" text; we use the P03 classifier (`rate_limit`/`billing` with `reset_at`
  or `retry_after`) and require zero answered model calls. The "unreachable" test is the P03
  `connection` reason with no HTTP status anywhere in the cause chain. We return a change set
  instead of mutating the record under the lock; the sticky `last_failure` stamp and
  `canonical_completions` bookkeeping are not taken (not in the row; the ledger owns replay).

## Status

Done (2026-10-19). `src/indus/cron/bookkeeping.py`, `tests/unit/cron/test_bookkeeping.py`. Shared
config rows `cron.failure_nudge_threshold`, `cron.retry_unreachable`. Integration (C1/C2): add
`unreachable_retry` to `CronJob`; the runner builds `RunOutcome` (`model_unreachable =
is_model_unreachable(exc, calls)`, `quota_hold_s = usage_window_seconds(exc, now=…, model_calls=…)`
only when the fallback chain is exhausted, `retry_run = is_retry_fire(job, scheduled_for)`), calls
`settle_run` inside the store's locked update, appends `nudge` / `hold_notice(held_until)` to the
failure notice and sends it only when `notify` is true.
