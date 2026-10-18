# Task: Retained process receipts (P11-13) Phase: 11 Owner: P11 builder B

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/process_registry_results.py`
(all). Spec: research `02-tools.md` §3.12 (retained results), §3.14 (force redaction); phase file
P11-13. Closed before writing.
Evidence: none.

What it does (own words): when a background job ends, a small JSON receipt is written to
`<home>/logs/process-results/<proc_id>.json` before any completion event goes out, so the result
outlives the in-memory record (30 min) and the process. Fields: `id, command (redacted),
exit_code, completion_reason, started_at, exited_at, tail (≤ 200,000 chars, redacted),
owner_session` plus `cwd` and `termination_source`. Redaction is forced, even when live redaction
is off: a receipt is durable. Written atomically, 0600, dir 0700. After each write the dir is
pruned: receipts older than 7 days go, then all but the newest 64. `process_manage poll/log/
wait/kill` fall back to a receipt when the id is not in memory — only for the owning conversation
or a compression continuation of it (a bare id is not authority). A receipt is read-only: no
re-run, no replayed completion.

Inputs → outputs: an exited `ProcessRecord` → file; (home, id, caller session) → a read-only
record or nothing.

Files: `src/indus/backends/process_receipts.py`, `tests/unit/backends/test_process_receipts.py`.
Shared: `src/indus/backends/processes.py` (receipt written for every exit, before the event),
`src/indus/tools/builtin/process_manage.py` (receipt fallback).

Edge cases / failure modes:
- disk full / unwritable → warning; the completion still goes out (never claim durability).
- a receipt whose id does not match its file name, or malformed JSON → ignored.
- id prefixes: a receipt is found by full id or a unique prefix of ≥ 4 hex digits.
- a session without an id (one-shot runs) never owns a receipt.
- pruning by exit time (`exited_at`, falling back to the file mtime), not start time.

Our design differences / decisions:
- the lineage check (continuation of the owner) is a resolver the host installs
  (`set_lineage_resolver`); without one only the owner itself can read. Backends do not open the
  session store themselves.
- receipts for every exit (not only notify-on-exit jobs), so a poll after the 30 min window works.

Tests we will write:
- receipt exists when the completion sink runs (ordering); 65th receipt prunes the oldest;
  8-day-old pruned (fake clock); other session's poll refused; continuation allowed through the
  resolver; secret in output redacted with live redaction off; process_manage poll of a pruned
  in-memory record answers from the receipt.

## Decisions for the lead
- The host (service, P08 compression) should call `process_receipts.set_lineage_resolver` with a
  function backed by `state.compression.lineage_ids`.

Compare checklist (filled after the rewrite; reopened `tools/process_registry_results.py`):
- [x] receipt before the event, for every exit (test: `tests/unit/backends/test_process_receipts.py::test_receipt_exists_when_the_completion_handler_runs`)
- [x] newest 64, 7 days by exit time (tests: `::test_newest_64_are_kept`, `::test_receipts_older_than_seven_days_are_pruned`)
- [x] forced redaction of command and tail (test: `::test_secret_redacted_even_with_live_redaction_off`)
- [x] owner or continuation only; list shows kept results of this conversation (tests: `::test_owner_reads_the_receipt_others_do_not`, `::test_compression_continuation_may_read`)
- [x] gap found: the reference refuses to write into a deleted profile's home — we skip the receipt then (test: `::test_no_receipt_for_a_deleted_profile`)
- [x] ids never glob (test: `::test_bad_ids_never_glob`); per profile (test: `::test_receipts_are_per_profile`)
- Deliberate differences: fields per the plan (no task ids — we have none); lineage through a resolver the host installs instead of opening the session store here.

## Status

Done (2026-10-18). `src/indus/backends/process_receipts.py`, `tests/unit/backends/test_process_receipts.py`.
Shared: `src/indus/backends/processes.py` (receipt for every exit, before the event; `receipts="default"`),
`src/indus/tools/builtin/process_manage.py` (receipt fallback for poll/log/wait/kill, retained entries in
list). Follow-up for the host: `process_receipts.set_lineage_resolver` backed by
`state.compression.lineage_ids` (until then only the owning session itself reads its receipts).

## Integration (lead)

Merged in the P11 integration (2026-10-18, lead's verify tree on main with P08 wave 1; slices applied A, B, C).
- Host wiring: `cli/runtime.build_service` → `service.terminal_host.install(store, processes)` installs the lineage resolver: the caller may read when the owner is in `state.compression.lineage_ids(store, caller)`; any store error means no. Gateway hosts (P15) call the same `install`.
- Receipts and the crash checkpoint go to `scope.home`; every test scope lives under the test's temp dir (autouse `INDUS_HOME` isolation), and a full run left nothing under `/root` (checked before/after at landing).
- D-16.133.
