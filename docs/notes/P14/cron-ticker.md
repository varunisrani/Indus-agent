# Task: ticker, `indus cron tick` / `indus cron daemon` (P14-07) Phase: 14 Owner: wave builder C2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `cron/scheduler_tick.py` (tick flow),
`cron/jobs.py` (`claim_job_for_fire`, `heartbeat_fire_claim`, `_claim_is_live`, the one-shot claim
TTL), `cron/scheduler.py` (tick lock, parallel pool size, `_process_due_job`, the claim heartbeat
loop and its grace). Closed before writing. Evidence: none.

What it does (own words): one tick pass looks for due jobs and starts each at most once. It takes a
process-wide tick lock first (a second tick, in this or another process, sees the lock held and
exits 0 saying another tick runs), checks that the cron store can be written (a run that cannot be
recorded is not run), reaps ledger attempts whose owner died, asks the due rules (C1) which jobs
fire now, and for each one: skips it when this process already runs it, claims it on its job record
(`fire_claim = {owner, claimed_at, heartbeat_at}`; a claim younger than 300 s belongs to someone
else; a recurring job's `next_run_at` moves to the following slot in the same locked write so a
crash can never fire the slot again), records the attempt in the ledger (refused when that slot
already completed) and hands it to the per-profile pool (`cron.max_parallel_jobs`, env
`INDUS_CRON_MAX_PARALLEL`; unset or 0 = unbounded, 1 = one at a time). While a job runs its claim
and its ledger attempt are refreshed every 60 s; when the claim cannot be refreshed for 180 s
(3 x heartbeat), or another owner took it, the run is stopped. A pass writes two marker files:
`<home>/cron/ticker_heartbeat` (every pass) and `ticker_last_success` (a pass that finished
without an internal error).

`indus cron tick` = one pass that waits for its jobs. `indus cron daemon` = a pass every 60 s on
asyncio with the injectable clock, until SIGINT/SIGTERM; then no new pass starts and in-flight runs
get up to 30 s to finish before they are cancelled (their children are killed on the way out).

Global emergency pause: `pause_all(reason)` writes `<home>/cron/paused_all.json`; while it exists
no scheduled fire starts (the pass returns after the housekeeping), runs in flight continue, and a
manual `run` still executes. `resume_all()` removes it.

Inputs → outputs: scope (home, config, clock), ports to the store / ledger / due rules / bookkeeping
(C1, C3), the agent runner (service layer) → started runs, ledger rows, markers, exit code.

Files: `src/indus/cron/ticker.py`, `src/indus/cron/ports.py` (the seams to C1/C3, one place),
`src/indus/service/cron_host.py` (process wiring: agent runner, skill loader, tick/daemon/run-now
entry points the CLI calls), `tests/unit/cron/test_ticker.py`, `tests/integration/cron/test_tick_e2e.py`,
`tests/unit/cron/_stubs_c2.py` (stand-in for C1's store, ledger and due rules; removed at
integration). Shared: `foundation/config/schema.py` (`cron` rows), its test.

Edge cases / failure modes:
- tick lock held → exit 0 with "another tick is running"; lock file cannot be created (read-only
  store) → the store is unwritable: no job fires.
- store probe says unwritable → zero dispatches (C1's health module notices and re-probes).
- claim lost between due and claim (another process) → skipped silently; ledger refuses a slot that
  completed (stale jobs file) → skipped, recurring jobs still advance.
- job already in flight in this process → never dispatched again.
- a run raises outside the pipeline's own handling → recorded as failed, the claim released, the
  pass continues with the other jobs.
- daemon stop while runs are in flight → 30 s drain, then cancel (scripts and agents kill their
  children on cancel).

Our design differences / decisions:
- Claims, heartbeats and global pause live in `ticker.py`; the store write itself goes through the
  store port's locked `edit`. C1's due rules decide lateness and catch-up; the ticker only obeys.
- The ticker never imports C1/C3 modules: `ports.py` defines what it needs (`JobSpec`, `CronPorts`)
  and a factory registry (`set_ports_factory`), so the call sites stay in one file. Until
  integration only the test stand-in registers a factory.
- `cron` sits below `service` in the layer order, so the agent run itself (SessionService-level
  wiring) is passed in as a callable from `service/cron_host.py`; the CLI (C3) calls
  `cron_host.run_tick / run_daemon / run_now`.
- Global pause marker is a file in `<home>/cron/` (ours), not a job field.

Tests we will write:
- two concurrent ticks → second exits "another tick is running"; claim < 300 s blocks, stale claim
  reclaimed; claim advances a recurring job's next run.
- pool size 1 serialises two jobs; unbounded runs both together; in-flight job not re-dispatched.
- pause-all → no scheduled fire; manual run executes; resume-all → fires.
- unwritable store → zero dispatches; heartbeat lost for 180 s → run cancelled.
- daemon loop with a fake clock fires an every-30m job twice in 60 simulated minutes.
- integration: real subprocess tick with `INDUS_HOME=<tmp>` runs a script-only job and writes its
  output document.

## Decisions for the lead
- Stand-in contract (what C2 reads from C1's job record): `id, name, prompt, skills, schedule
  (kind once|interval|cron), schedule_display, deliver, failure_deliver, origin, enabled_toolsets,
  workdir, model, provider, base_url, pinned, reasoning_effort, script, interpreter, no_agent,
  monitor_script, monitor_url, context_from, next_run_at, fire_claim`. `ports.spec_of` is the one
  adapter; `ports.CronPorts` names the store/ledger/due/next-run/notepad/monitor/finish seams.
  At integration: register the real factory (`ports.set_ports_factory`) in `service/cron_host.py`
  (one line) and delete `tests/unit/cron/_stubs_c2.py`.
- The integration test runs the tick through `python -m tests.integration.cron.tick_entry`
  (installs the stand-in ports, then calls `cron_host.tick_main`); switch it to the real
  `indus cron tick` subprocess once C3's CLI and C1's store are merged (one helper,
  `_tick_command`).
- `INDUS_CRON_MAX_PARALLEL` non-numeric → warning and unbounded.
- Claim owner text: `<hostname>:<pid>:<random hex>` (fresh per claim).

Compare checklist (filled after the rewrite; reopened `cron/scheduler_tick.py`, `cron/jobs.py`
claim functions, `cron/scheduler.py` tick lock / pool / heartbeat loop):
- [x] tick lock taken without waiting; held → skipped quietly; lock file not creatable → no fire
  (test: `tests/unit/cron/test_ticker.py::test_second_concurrent_tick_exits`,
  `::test_ticks_inside_one_process_conflict_too`, `::test_unwritable_lock_folder_fires_nothing`)
- [x] store probe before any fire (`::test_unwritable_store_fires_nothing`)
- [x] claim live for 300 s, malformed / future-dated claims stale (`::test_claim_liveness`,
  `::test_young_claim_blocks_another_process_and_stale_claim_is_reclaimed`)
- [x] recurring job's next run advanced in the claim write (`::test_take_claim_advances_a_recurring_job`)
- [x] attempt recorded before dispatch; completed slot never replayed
  (`::test_markers_and_attempt_recorded_before_dispatch`, `::test_completed_slot_is_never_replayed`)
- [x] heartbeat every 60 s keeps a long run's claim; lost claim stops the run
  (`::test_claim_heartbeat_keeps_a_long_run_alive`, `::test_lost_claim_stops_the_run`)
- [x] pool: env > config > unbounded, 0 = unbounded (`::test_pool_size_one_serialises`,
  `::test_unbounded_pool_runs_together`, `::test_env_override_of_the_pool`)
- [x] in-flight job not dispatched twice (`::test_job_in_flight_here_is_not_dispatched_again`)
- [x] global pause: no scheduled fire, manual run executes; manual runs carry no slot
  (`::test_pause_all_stops_scheduled_fires_but_not_manual_runs`, `::test_manual_run_refusals`)
- [x] daemon loop and drain (`::test_daemon_fires_an_every_30m_job_twice_in_an_hour`,
  `::test_daemon_drains_then_cancels`); real subprocess tick
  (`tests/integration/cron/test_tick_e2e.py::test_tick_process_runs_a_script_only_job`)
- [x] gap found: a claim write that fails (store full / read-only) raised out of the pass — now the
  job is skipped and logged, the other jobs go on; pinned by `::test_claim_that_cannot_be_written_fires_nothing`
- Deliberate differences: the reference confirms a single heartbeat miss with a second read 1 s
  later; ours treats "another owner holds it" as final (that answer is not a transient error) and
  only transient write errors get the 180 s grace. No stale-code yield to a fresher gateway, no
  admission reconcile, no worktree/MCP-orphan sweeps in the tick (gateway concerns, P15).
- Not taken: "headless tick refuses agent jobs without a gateway" — before P15 the tick is the
  only runner, so agent jobs run here.

## Status

Done (2026-10-19). Ticker, claims, pool, global pause, markers, daemon and run-now in
`src/indus/cron/ticker.py`; seams in `src/indus/cron/ports.py`; process wiring and entry points
in `src/indus/service/cron_host.py` (`tick_main`, `daemon_main`, `run_now`). The CLI verbs
(`indus cron tick|daemon|pause-all|resume-all|run`) are C3's (P14-19): they call these functions.
Stand-in `tests/unit/cron/_stubs_c2.py` until C1's store/ledger/due and C3's finish land.

## Integration (lead, P14 landing)
Landed with C1/C2/C3 together: one backend `service.cron_host.CronHost` behind `ports.BACKEND_FACTORY` (C2's `set_ports_factory`/`ports_for` were folded into it), stand-ins deleted, tests on the real store (`tests/unit/cron/world.py`). Details and open seams: `docs/plan/phases/LEAD-NOTES.md`, "P14 landing notes (lead)"; differences D-16.220 … D-16.237.
