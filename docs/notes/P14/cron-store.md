# Task: Job model and store (P14-03) Phase: 14 Owner: wave-17 builder C1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `cron/jobs.py` (record fields,
`create_job`, `resolve_job_ref`, pause/resume/remove, load repair, effective state, ownership
restore), `cron/job_definition.py` (authored vs scheduler-owned split), `cron/jobs_invariants.py`
(mode invariants). Closed before writing.
Evidence: none.

## Model / store API (shared with slices C2 and C3 — read this first)

Everything below lives in `src/indus/cron/`; C2/C3 stand-ins should copy these names and shapes.
All datetimes are timezone-aware and stored in UTC (a naive value read from a hand-edited file is
taken as UTC). Times come from `scope.clock`; the configured zone from
`foundation.clock.resolve_zone(scope)` (None = system zone; `next_run.effective_zone(None)` turns
that into a DST-aware zone).

`errors.py` — every cron error is an `IndusError` (codes for the lead to list in `ERROR_CODES`):
`ScheduleError` `cron.bad_schedule` (also a `ValueError`), `OneShotInPast` `cron.oneshot_past`
(subclass of `ScheduleError`), `NextRunError` `cron.next_run_failed`, `InvalidJob`
`cron.invalid_job` (also a `ValueError`), `JobNotFound` `cron.job_not_found`, `AmbiguousJobRef`
`cron.ambiguous_job` (`.candidates: tuple[str, ...]` of ids), `CronStoreBusy` `cron.store_busy`,
`SlotTaken` `cron.slot_taken`, `AttemptFinal` `cron.attempt_final`, `NotepadLimit`
`cron.notepad_limit` (also a `ValueError`).

`grammar.py` (P14-01) — schedules are frozen pydantic models, discriminated by `kind`:
- `OnceSchedule(kind="once", run_at: datetime, display: str)`
- `IntervalSchedule(kind="interval", minutes: int, anchor: datetime | None, display: str)` —
  `anchor` set only by `every Nd at HH:MM` (N > 1, not 7): runs fall on `anchor + k·N days`.
- `CronSchedule(kind="cron", expr: str, display: str)` — 5 fields, croniter syntax.
- `Schedule` = the annotated union; `parse_schedule(text, tz, *, now=None) -> Schedule`;
  `parse_duration(text) -> minutes`; `is_recurring(schedule) -> bool`.

`next_run.py` (P14-02):
- `compute_next_run(schedule, *, now, tz, last_run_at=None) -> datetime | None` — once: `run_at`
  until it has run (then None); interval: `(last_run_at or now) + minutes` (anchored: next lattice
  point after that base); cron: next wall-clock match strictly after `last_run_at or now`.
  Raises `NextRunError` (bad expression, croniter missing).
- `next_after(schedule, base, tz) -> datetime` — the recurring primitive (strictly after `base`).
- `check_oneshot_time(schedule, now)` — raises `OneShotInPast` when a once is > 120 s past.
- `plan_next(job, *, now, tz, slot=None) -> NextRunPlan(next_run_at, state, schedule_error)` —
  what the record should hold after a run; a recurring job whose next run cannot be computed gets
  `state="error"` and the reason, never `completed`. Bookkeeping (C3) calls this after each run,
  **after** setting `last_run_at` / `repeat.completed`; cron steps from the run's slot (`slot`, else
  `job.last_dispatch.scheduled_for`) so a late run in the repeated fall-back hour is not doubled.
- `following_slot(schedule, slot, *, now, tz)` — next slot after `slot`, or after `now` if passed.
- `period_seconds(schedule, *, tz, now) -> float | None`.

`models.py` (P14-03):
- `Repeat(times: int | None, completed: int = 0)` — `times` None = forever; `.exhausted`.
- `Origin(platform: str, chat_id: str, thread_id: str | None = None)`.
- `FireClaim(owner: str, claimed_at: datetime, heartbeat_at: datetime)` — C2 writes it.
- `MonitorState(hash: str | None = None, last_output_path: str | None = None)` — C3 writes it.
- `DispatchStamp(scheduled_for, dispatched_at, lateness_s: float, lateness: "on_time" | "late" |
  "catch_up")` — the ticker stores the due decision here; doctor reads it.
- `CronJob` — authored: `name, prompt, skills, schedule, schedule_display, repeat.times, deliver,
  failure_deliver, origin, enabled_toolsets, workdir, model, provider, base_url, pinned,
  reasoning_effort, script, interpreter, no_agent, monitor_script, monitor_url, context_from`;
  scheduler-owned: `enabled, state ("scheduled" | "paused" | "completed" | "error"), paused_at,
  paused_reason, next_run_at, last_run_at, last_status, last_error, last_delivery_error,
  last_delivery_unverified, failure_streak, repeat.completed, monitor_state, quota_hold_until,
  fire_claim, last_dispatch, schedule_error`; identity: `id` (12 hex), `created_at`.
  `AUTHORED_FIELDS` / `SCHEDULER_FIELDS` list them. Unknown keys from a newer build are kept
  (`extra="allow"`) so a rewrite never drops them.
  Properties: `label` (name or a derived label), `recurring`, `paused` (not enabled, or any pause
  marker), `runnable` (enabled, no pause marker, state not `completed`/`error`).
- `JobSpec` — what `create` takes: the authored fields with `schedule: str | Schedule`, plus
  `paused: bool`, `paused_reason: str | None`, `repeat: int | None`.
- `merge_authored(local, authored: Mapping) -> CronJob` — refresh authored fields, keep state.
- `new_job_id() -> str`.

`store.py` (P14-03): `CronPaths(home)` (`cron_dir`, `jobs_file`, `lock_file`, `output_dir`,
`executions_db`, `tick_lock`); `CronStore(scope, *, lock_timeout=30.0, lock_wait=None,
script_check=None)`:
- reads: `load() -> list[CronJob]`, `get(job_id) -> CronJob | None`, `resolve(ref) -> CronJob`
  (exact id, else case-insensitive name; `JobNotFound` / `AmbiguousJobRef`).
- authored writes: `create(spec) -> CronJob`, `update(ref, **changes) -> CronJob`,
  `pause(ref, reason=None)`, `resume(ref)`, `remove(ref) -> CronJob` (runs remove hooks:
  notepad clear; C3 adds the output dir), `import_definitions(authored_by_id, paused_reason)`.
- scheduler writes: `patch(job_id, **fields) -> CronJob | None` (None when the job is gone),
  `transaction()` (context manager yielding the mutable `list[CronJob]`, saved on clean exit).
- skills: `referenced_skills()`, `rewrite_skills(renames, dropped) -> tuple[str, ...]` (change
  lines), `rename_skill(old, new)`. The curator `SkillReferenceSource` adapter lives in
  `indus/service/cron_refs.py` (`CronSkillReferences`, `register_cron_references(store) -> undo`)
  because `indus.cron` and `indus.learning` are sibling layers that may not import each other.
- `add_remove_hook(fn: Callable[[str], None])`.

`ledger.py` (P14-04): `ExecutionsDB(scope, path=None)` opens `<home>/cron/executions.db` (P02
`open_store` + journal policy + `Writer`); `ExecutionLedger(db, *, clock, procs=None)`:
`claim(job_id, *, scheduled_for, kind) -> Attempt` (call **before** dispatch; `SlotTaken` when the
slot already has an attempt), `slot_attempted(job_id, instant) -> bool`, `start(id)`,
`heartbeat(id)`, `finish(id, *, ok, result_status=None, delivery_status=None, error=None)`,
`note_delivery(id, status)`, `reap(*, inactivity_timeout_s, script_timeout_s) -> list[Attempt]`,
`get(id)`, `history(job_id, limit)`, `active()`. `Attempt` is a frozen dataclass of the row.

`store_health.py` (P14-05): `probe_dir(cron_dir) -> OSError | None`, `probe_report(cron_dir)`,
`StoreHealth(cron_dir, *, clock, notify=None, probe=probe_dir)` with `gate() -> bool` (call before
any fire), `condition`, `outage_since`.

`due.py` (P14-06): `evaluate(job, *, now, tz, slots, catch_up_missed=True, outage_since=None) ->
DueDecision(job_id, fire, scheduled_for, lateness, lateness_s, updates, note)`;
`evaluate_all(jobs, ...)` → every decision (non-firing ones may carry repairs in `updates` to
`store.patch`); `collect_due(decisions)` → the firing ones, oldest slot first. A firing decision's
`updates` hold the provisional `next_run_at` and `last_dispatch`: write them with the claim.
The ticker order (C2): `health.gate()` → `ledger.reap(...)` → `evaluate_all` → patch repairs →
for each `collect_due`: `ledger.claim(job_id, scheduled_for=d.scheduled_for)` (skip on
`SlotTaken`) + `store.patch(job_id, **d.updates)` → dispatch.

`notepad.py` (P14-17): `Notepad(db, *, clock)`: `get/set/unset/list/clear/render_block`.

`context.py`: `CronContext.open(scope)` builds all of the above for one profile (store, db,
ledger, notepad, health; notepad cleared on job removal) and `close()`.

## What it does (own words)

The job file `<home>/cron/jobs.json` (`{"version": 1, "jobs": [...]}`) holds every scheduled job of
a profile. All writes go through one cross-process lock file `<home>/cron/jobs.lock`: the writer
re-reads the file under the lock, changes it and replaces it atomically, so two processes (CLI
and ticker) never lose each other's jobs. Waiting for the lock is bounded at 30 s; after that a
typed `CronStoreBusy` error says another process holds it. The directory is 0700, the file 0600,
and when root rewrites a file owned by another user the old owner is put back (a root CLI must not
lock the user's ticker out).

A hand-edited file is repaired on load: non-object entries and objects that do not validate are
dropped (the dropped objects are kept in `jobs.rejected.json` beside it), negative or non-integer
counters are reset, and one warning lists only the field names and value types — never content.

`enabled` is the authoritative switch; `paused_at` / `state == "paused"` are a second gate, so a
half-paused record never fires and is shown as paused.

Create defaults: `deliver` is `origin` when an origin is given, else `local`; one-shots repeat
once, recurring jobs forever (≤ 0 = forever); a job needs a prompt, a script or skills;
`no_agent` needs a script; `monitor_script` and `monitor_url` exclude each other and `no_agent`.
`paused=True` writes `enabled=false, state=paused, next_run_at=null`, the time and the reason
("created paused, waiting for approval" unless given) in the same locked write.

The store is a curator `SkillReferenceSource`: every job's skills (paused or not) are protected;
consolidation renames are applied to job skill lists.

Inputs → outputs: `JobSpec` / ref strings / field changes → `CronJob` records in `jobs.json`.

Files: `src/indus/cron/{errors,models,store,context}.py`, `tests/unit/cron/test_store.py`,
`tests/fixtures/cron/jobs_*.json`.

Edge cases / failure modes:
- missing file → empty list; non-JSON file → `InvalidJob` naming the file (never overwritten);
- a bare list (no wrapper object) is accepted and rewritten wrapped;
- two processes appending at once → both jobs kept (lock + re-read);
- lock held past 30 s → `CronStoreBusy` (fake clock in tests);
- name lookup: exact id first, then case-insensitive name; two jobs with the same name → refused
  with both ids;
- `update(skills=[])` clears skills; updating authored fields never touches run state;
- a recurring schedule change recomputes `next_run_at` (unless paused); a one-shot more than
  120 s in the past is refused at create/update;
- resume keeps a past `next_run_at` of a recurring job (the due rules decide catch-up or skip);
- removing a job runs remove hooks (notepad) after the write landed.

Our design differences / decisions:
- file wrapper `{"version": 1, "jobs": [...]}`; schedule stored as a tagged object with `display`;
- dropped invalid records are kept in `jobs.rejected.json` instead of being lost;
- `schedule_error` and `last_dispatch` are our scheduler fields (not in the plan's list);
- `script_check` hook: C2's script-path rule (P14-09, "command line, not a file") plugs in here.

Tests we will write: create defaults table; paused create; payload/mode invariants; resolve by
id / name / ambiguous; update authored keeps state; skills clear; pause/resume (past slot kept);
remove + hook; repair fixture (types only in the warning, rejected file); bare list; two-process
writers; lock timeout on fake clock; permissions 0700/0600; curator protection with a real P09
`referenced_skills()` over this store; rename rewrites jobs; import merge keeps state.

## Decisions for the lead
- The curator adapter is a new file in the service layer (`service/cron_refs.py`, layer rule).
- Error codes `cron.*` (listed above) are not yet in `foundation/errors.ERROR_CODES` (shared file,
  left alone): add the rows at integration.
- Service wiring (`service/host_inprocess.py` registering the store with the curator) is left to
  integration: `register_cron_references(CronStore(scope))` is the one call (that file is shared
  with C3's P14-18).
- `last_delivery_unverified` is typed `str | None` (a description of the unverified targets);
  C3 may narrow it.

Compare checklist (filled after the rewrite; reopened `cron/jobs.py`, `cron/job_definition.py`,
`cron/jobs_invariants.py`):
- [x] lock 30 s, atomic replace, permissions, ownership restore (`tests/unit/cron/test_store.py::
  test_lock_wait_then_busy`, `::test_permissions`, `::test_root_rewrite_restores_owner`,
  `::test_two_processes_never_lose_jobs`)
- [x] repair on load (types only), invalid counters reset (`::test_repair_drops_junk_and_warns_types_only`)
- [x] authored vs scheduler fields; import merge keeps run state (`::test_field_split_is_complete`,
  `::test_import_merge_keeps_run_state`, `::test_update_keeps_run_state`)
- [x] create defaults, paused create, invariants (`::test_create_recurring_defaults`,
  `::test_created_paused`, `::test_create_invariants`)
- [x] resolve: exact id wins, ambiguous refused (`::test_exact_id_wins_over_name`,
  `::test_ambiguous_name_refused_with_ids`)
- [x] curator protection and renames (`::test_curator_skips_skill_of_paused_job`,
  `::test_rename_rewrites_job_skills`)
- Deliberate differences: an id-keyed or otherwise unknown top-level shape is refused and left on
  disk (the reference rewrites it, possibly empty); dropped records go to `jobs.rejected.json`;
  the reference's merge-on-save against a racing degraded lock is not needed (we always re-read
  under the lock); the gateway-lifecycle guard belongs to P14-18.
- Not taken: the reference's legacy single `skill` field (we store `skills` only).

## Status

Done (2026-10-19). Shared: `docs/internals/traps-ledger.md` (R07.8–R07.13 rows),
`docs/internals/acceptance-map.md` (ACC-H1-1, H2-1, new H2-2, H3-1, H13-1). New in the service
layer: `src/indus/service/cron_refs.py`. Wiring left for integration: `register_cron_references`
at host start; `ERROR_CODES` rows.

## Integration (lead, P14 landing)
Landed with C1/C2/C3 together: one backend `service.cron_host.CronHost` behind `ports.BACKEND_FACTORY` (C2's `set_ports_factory`/`ports_for` were folded into it), stand-ins deleted, tests on the real store (`tests/unit/cron/world.py`). Details and open seams: `docs/plan/phases/LEAD-NOTES.md`, "P14 landing notes (lead)"; differences D-16.220 … D-16.237.
