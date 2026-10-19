# Task: Incidents (P14-15) Phase: 14 Owner: wave-17 builder C3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `cron/incidents.py` (signature,
states, upsert/resolve/ack), `cron/scheduler.py` (`_repeat_alert_withheld`,
`_upsert_incident_for_failure`, `_mark_incident_alerted`, `_resolve_incidents_for_recovered_job`).
Research §3.15.7. Closed before writing.

What it does (own words): failures of the same job with "the same" error are one incident, so
the person is pinged once, reminded after a quiet period, and can acknowledge it to silence it
for good. "Same" ignores case, runs of whitespace and measured durations ("timeout after 31.2s"
equals "Timeout after 45s") and looks only at the first 200 characters. The stored error is
secret-masked and cut to 500 characters. States: `detected → alerted → resolved | closed`
(`closed` = acknowledged). First failure of a signature alerts; repeats are silent while alerted
until `cron.failure_repeat_alert_hours` (6; 0 = alert every run) have passed since the last ping,
then one reminder; a different error is a new incident and alerts at once; a success resolves the
job's open incidents and a later repeat re-opens them (alerts again); acknowledged ones stay silent.
If the incident table cannot be read or written, the alert goes out anyway.

Inputs → outputs: `IncidentLedger(path, clock)`; `ledger.on_failure(job_id, error, output_path=…,
repeat_hours=…)` → `AlertDecision{alert, incident_id, reason}`; `mark_alerted(id)`;
`resolve_job(job_id)`; `ack(id)`; `list(state=…)`; `get(id)`; `forget_job(job_id)`;
`decide_failure_alert(scope, job_id, error, …)` (the fail-open wrapper).

Files: `src/indus/cron/incidents.py`, `tests/unit/cron/test_incidents.py`. Shared: config row
`cron.failure_repeat_alert_hours`.

Storage: table `cron_incidents` (ours) in `<home>/cron/executions.db` (C1's ledger file; dir 0700,
file 0600 when we create it); id = first 6 chars of the job id + `_` + the 12-hex signature.

Edge cases / failure modes: empty error → signature of the empty string (still one incident);
`alerted_at` missing on an alerted row → alert (never swallow); corrupt file → `sqlite3.DatabaseError`
caught by the wrapper → alert delivered, logged once; ack of an unknown or already closed id → False.

Our design differences / decisions: no failure-type keyword classification column (not in the
row); timestamps stored as UTC ISO; the wrapper decides and marks in one call so the caller cannot
forget the mark (the caller passes `delivered=False` to undo when the send failed).

Tests we will write: shared signature for the duration example; repeat silent until the 6 h
reminder (fake clock), then silent again; hours 0 → every run; different error alerts; success
resolves and a repeat re-opens; ack silences; corrupt ledger → alert; stored error redacted and
≤ 500; job removal forgets its incidents.

## Decisions for the lead
- The incident table opens its own connection on `executions.db`; once C1's ledger exposes a
  connection helper (journal mode, busy timeout), `IncidentLedger.for_scope` should use it.

Compare checklist (reopened `cron/incidents.py`, `cron/scheduler.py` incident helpers):
- [x] signature: lower case, whitespace, measured durations, first 200 chars, with the job id (`tests/unit/cron/test_incidents.py::test_signature_normalisation`)
- [x] stored error masked (forced) and cut to 500 (`::test_stored_error_is_masked_and_bounded`)
- [x] first failure alerts, repeats silent while alerted, reminder after the window (restarting it) (`::test_first_alerts_repeats_silent_until_the_six_hour_reminder`, `::test_reminder_window_counts_from_the_last_alert`)
- [x] 0 hours → every run (`::test_zero_hours_alerts_every_run`); missing `alerted_at` → alert (`::test_alerted_row_without_a_stamp_alerts`)
- [x] success resolves detected/alerted, never closed; a repeat re-opens a resolved row (`::test_success_resolves_and_a_repeat_reopens`, `::test_ack_silences_for_good`)
- [x] unreadable ledger → alert delivered (`::test_corrupt_ledger_still_alerts`)
- Deliberate differences: no keyword failure-type column; an `occurrences` counter instead; the
  decision helper also marks the row alerted; reading never creates the file.
- Not taken: job-name column, count helpers (the CLI lists rows).

## Status

Done (2026-10-19). `src/indus/cron/incidents.py`, `tests/unit/cron/test_incidents.py`; config row
`cron.failure_repeat_alert_hours`. Integration: the runner calls `decide_failure_alert(scope,
job_id, error, output_path=…)` for failed runs (skip the notice when `alert` is false) and
`resolve_after_success(scope, job_id)` after a good run; job removal calls `forget_job` (the tool
and CLI already do).
