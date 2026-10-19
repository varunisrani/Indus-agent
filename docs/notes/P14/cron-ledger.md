# Task: Execution ledger (P14-04) Phase: 14 Owner: wave-17 builder C1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `cron/executions.py` (states,
ownership, reaping, pruning), `cron/occurrences.py` (slot identity), `cron/execution_identity.py`
(skimmed). Closed before writing. Trap R07.13. Evidence: none.

What it does (own words): a SQLite audit of every run attempt, in `<home>/cron/executions.db`,
opened through the P02 store plumbing (test-isolation guard, owner-only file, journal policy,
single `Writer` with deadline retries). One row per attempt: `attempt_id, job_id, scheduled_for`
(the exact UTC slot; null for manual runs), `kind` (`scheduled | manual | retry`), `state`
(`claimed → running → completed | failed | unknown`), `owner_pid`, `owner_start_fingerprint`,
`claimed_at`, `heartbeat_at`, `started_at`, `finished_at`, `result_status`, `delivery_status`,
`error`.

- The attempt is written **before** dispatch (`claim`). A scheduled slot can be claimed once: a
  second claim of the same (job, slot) raises `SlotTaken`, and `slot_attempted` lets the due pass
  skip a slot that a stale `jobs.json` still points at (R07.13).
- Terminal states never change (`AttemptFinal`); only an unset `delivery_status` may be filled
  in once afterwards.
- Reaping (every tick and before every manual run): a non-terminal attempt becomes `unknown` when
  its owner is proved dead — the PID is gone, or the PID now belongs to a process with another
  start fingerprint — or when a live owner has been silent (no heartbeat) for longer than
  max(3 × inactivity timeout, script timeout, 2 h). Attempts of the calling process are left to
  it. `unknown` attempts are never re-run (their slot stays taken).
- History is bounded to the newest 500 finished attempts per job; active attempts are never pruned.

The same database holds the notepad table (P14-17); `ExecutionsDB` owns the connection and
schema for both.

Inputs → outputs: ledger calls → rows; `Attempt` records back.

Files: `src/indus/cron/ledger.py`, `tests/unit/cron/test_ledger.py`, `tests/fakes/pids.py`.

Edge cases / failure modes:
- the start fingerprint cannot be read (non-Linux, permissions) → death is proved only by a
  missing PID; a live-but-unreadable owner is reaped by the silence rule only;
- a heartbeat or finish on someone else's terminal row is refused, never rewrites it;
- clock skew: silence is measured from the last heartbeat (or claim) on the injected clock;
- inactivity timeout 0 (unlimited) → the silence rule still applies with the 2 h / script floor.

Our design differences / decisions:
- process fingerprint = Linux `/proc/<pid>/stat` start ticks plus the boot id; a mismatch is
  proof of PID reuse (exact value, not a drifting wall time);
- the slot uniqueness is a partial unique index (`kind = 'scheduled'`), so retries and manual runs
  never collide with it.

Tests we will write: record-before-dispatch (dispatcher fake asserts the row); second claim of a
slot refused; completed slot reported attempted after an old jobs file comes back; dead owner
(fake PID table) → unknown; PID reused (other fingerprint) → unknown; live silent owner past 2 h
(fake clock) → unknown, at 1 h 59 m kept; own attempts untouched; terminal update refused;
pruning keeps 500 and active rows; delivery status fill-once.

## Decisions for the lead
- Reaping never touches the calling process's own attempts (the ticker's in-flight guard owns
  them).
- Inactivity 0 keeps the floor rule (the reference never reaps live owners then); ours bounds it.

Compare checklist (filled after the rewrite; reopened `cron/executions.py`, `cron/occurrences.py`):
- [x] states, terminal immutability (`tests/unit/cron/test_ledger.py::test_terminal_states_are_final`)
- [x] record before dispatch, completed slot not replayed (`::test_record_exists_before_dispatch`,
  `::test_completed_slot_not_replayed_after_old_jobs_file`, `::test_slot_claimed_once`)
- [x] reaping rule max(3×, script, 2 h), unknown never re-run (`::test_dead_owner_becomes_unknown`,
  `::test_reused_pid_is_proof_of_death`, `::test_silent_live_owner_after_two_hours`,
  `::test_unknown_never_rerun`)
- [x] bounded history, active never pruned (`::test_history_bounded_active_kept`)
- Deliberate differences: the reference keeps 1,000 finished rows overall, we keep 500 per job
  (plan); its slot proof counts only `completed` rows, ours every attempt of the slot (stricter
  at-most-once: a failed or unknown slot is not re-run by the scheduler — retries use
  `kind="retry"`); worker hand-off columns are left to P15 (H14).
- Not taken: the receipt-recovery path (needs the gateway's worker processes, P15).

## Status

Done (2026-10-19). Files: `src/indus/cron/ledger.py`, `tests/unit/cron/test_ledger.py`,
`tests/fakes/pids.py`.
