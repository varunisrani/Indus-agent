# Task: Store health (P14-05) Phase: 14 Owner: wave-17 builder C1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `cron/store_health.py` (all).
Closed before writing. Trap R07.13. Evidence: none.

What it does (own words): before any job fires, the ticker asks `StoreHealth.gate()`. The gate
probes the cron directory the way a save would: create a temp file beside `jobs.json`, write as
many bytes as `jobs.json` holds plus a page, fsync, unlink; and check that `jobs.json`,
`executions.db` (and its journal) and the tick lock are writable. ENOSPC, EDQUOT, EROFS, EACCES or
EPERM → the store is unwritable and **no job fires** (a run that cannot be recorded is not run).
The ticker keeps running; while unwritable the gate re-probes at most once a minute (in between
it answers "blocked" without touching the disk).

Notices: one `unwritable` event when it turns bad (store, error, since, fix hint), and one
`recovered` event once it has stayed writable for an hour (a flapping disk does not spam). On
recovery the jobs simply are still due: each fires once under the normal catch-up rules, and
`outage_since` lets the due pass fire a one-shot that came due during the outage even though it
is past its 120 s grace.

`probe_report(cron_dir)` is the cross-process view for `cron status`, `cron list` and `doctor`.

Inputs → outputs: cron dir + clock → `gate() -> bool`, `condition` (`HealthCondition(store,
error, since)` or None), `outage_since`, events through `notify(HealthEvent)`.

Files: `src/indus/cron/store_health.py`, `tests/unit/cron/test_store_health.py`.

Edge cases / failure modes:
- cron dir missing → writable (the first save creates it);
- an error other than the listed errnos (EIO, …) is also treated as unwritable — we never fire
  without a durable record — but logged at error level;
- a listener that raises never breaks the tick;
- state is per process (the daemon); a separate `indus cron tick` process starts clean.

Our design differences / decisions:
- the recovery notice waits for an hour of continuous writability (plan) rather than the first
  landed save;
- the probe also covers the ledger files, since the ledger is what makes a run recordable.

Tests we will write: injected ENOSPC → gate false, zero dispatches over several ticks, one
notice; re-probe only after 60 s (fake clock, probe call count); recovery → gate true, each due
job fires once (with the due pass and a ledger); recovery notice after 1 h, not before, not
twice; flapping inside the hour → no recovery notice yet; probe_report text; real read-only dir
(skipped as root).

## Decisions for the lead
- none.

Compare checklist (filled after the rewrite; reopened `cron/store_health.py`):
- [x] errno set, re-probe 60 s, never fire while unwritable
  (`tests/unit/cron/test_store_health.py::test_enospc_blocks_every_fire`,
  `::test_reprobe_at_most_once_a_minute`)
- [x] one notice in, one out; each due job fires once on recovery (`::test_notices_once_each_way`,
  `::test_recovery_fires_each_due_job_once`, `::test_oneshot_due_in_outage_keeps_grace_after`)
- Deliberate differences: recovery is confirmed by a passing probe (the reference waits for a
  landed save); the recovery notice waits an hour (plan); the probe also covers the ledger files.
- Not taken: per-site skip accounting and host-wide gauges (P15 status).

## Status

Done (2026-10-19). Files: `src/indus/cron/store_health.py`, `tests/unit/cron/test_store_health.py`.
