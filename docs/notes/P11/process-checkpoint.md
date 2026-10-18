# Task: Process crash recovery (P11-14) Phase: 11 Owner: P11 builder B

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/process_registry_checkpoint.py`
(all), `tools/process_registry.py` (`_detached_host_fate`, `_checkpoint_path`, the detached branch
of kill). Spec: research `02-tools.md` §3.12 (crash recovery); phase file P11-14. Closed before
writing.
Evidence: none.

What it does (own words): background jobs on the host run in their own session, so they survive a
crash of the Indus process that started them. A checkpoint `<home>/processes.json` (atomic, 0600)
lists the running jobs of each profile: `{id, pid, pid_start_time, backend, env_key, log_path,
owner_session, notify_on_exit, persist_on_release, command (redacted), cwd, started_at}` and the
Indus process that owns the entry (its pid and start time). It is rewritten when a job starts or
ends. When a registry first touches a profile, it reads the checkpoint: entries whose owner Indus
process is still alive belong to someone else and are kept untouched; the rest are adopted only
when the PID exists and its start time matches the recorded one (a recycled PID is never adopted
or signalled). Adopted jobs are `detached`: the registry tracks their exit (pidfd or a poll), can
kill them (same ordered kill, guarded by the start time), and notify-on-exit still fires; there
is no in-memory output history, so poll says so (and reads the log file when the entry has one).
Entries that cannot be adopted become `lost` records. `persist_on_release` entries survive
session end (they are not killed by `release`), not a clean host exit (shutdown kills every job
and empties the checkpoint).

Inputs → outputs: registry events → checkpoint file; checkpoint at first touch → detached or
lost records.

Files: `src/indus/backends/process_checkpoint.py`, `tests/unit/backends/test_process_checkpoint.py`.
Shared: `src/indus/backends/processes.py` (checkpoint writes, recovery at first touch, detached
records), `src/indus/tools/builtin/process_manage.py` (`detached` note on poll/list).

Edge cases / failure modes:
- corrupt or non-list JSON → ignored with one warning, rewritten on the next change.
- entries of another live Indus process of the same profile (CLI + gateway) → never adopted,
  preserved on rewrite (read-merge-write under a file lock).
- non-local entries (in-sandbox PIDs) → `lost`: the sandbox handle is gone.
- no `/proc` (macOS): no start time → no adoption (fail safe toward lost).
- exit code of an adopted job is unknown (not our child) → `null`.

Our design differences / decisions:
- ownership of entries by Indus process (pid + start time) instead of the reference's
  foreign-handoff rule.
- recovery runs lazily on the first registry use per profile home, so every host (CLI resume,
  gateway) gets it without a separate call; `recover(scope)` can be called directly too.

Tests we will write:
- real sleeping child: write checkpoint, new registry adopts it as detached; poll says detached;
  kill works; notify-on-complete fires for the adopted process when it exits.
- recycled PID (different start time) → lost; corrupt checkpoint → ignored with a warning;
  entry of a live foreign owner kept; checkpoint mode 0600 and contents redacted.

Compare checklist (filled after the rewrite; reopened `tools/process_registry_checkpoint.py`,
`tools/process_registry.py` detached paths):
- [x] atomic write, 0600, command redacted (test: `tests/unit/backends/test_process_checkpoint.py::test_new_registry_adopts_a_live_job_as_detached`)
- [x] PID + start time; recycled pid never adopted or signalled; non-host entries not adopted (test: `::test_recycled_pid_is_lost_and_never_signalled`)
- [x] detached: poll/list say so; exit noticed; notify-on-exit fires with no exit code (test: `::test_new_registry_adopts_a_live_job_as_detached`); kill works (`::test_adopted_job_can_be_killed`)
- [x] read-merge-write keeps a live other holder's entries (test: `::test_entries_of_a_live_other_holder_are_kept`)
- [x] corrupt file ignored with a warning (test: `::test_corrupt_checkpoint_is_ignored_with_a_warning`)
- [x] persist_on_release survives release, not shutdown (test: `::test_persist_on_release_survives_session_end_not_shutdown`); per profile (`::test_each_profile_has_its_own_checkpoint`)
- Deliberate differences: an unreadable start time means lost (the reference adopts a live pid whose start time cannot be read); non-host entries become `lost` records (the reference drops them silently); entries are owned by an Indus process (pid + start time) instead of the reference's worker hand-off logs.
- Not taken: systemd scope stop for dead wrappers (no scopes, D16), watcher re-enqueue for the gateway (watch patterns are not persisted; notify-on-exit is).

## Status

Done (2026-10-18). `src/indus/backends/process_checkpoint.py`, `tests/unit/backends/test_process_checkpoint.py`.
Shared: `src/indus/backends/processes.py` (`recover(scope)` on first spawn / `process_manage` call per
profile, checkpoint rewritten on start and exit, `DetachedJob` records), `src/indus/tools/builtin/process_manage.py`
(`detached` in poll/list). Recovery runs lazily; the gateway (P15) may call `recover(scope)` at start.

## Integration (lead)

Merged in the P11 integration (2026-10-18, lead's verify tree on main with P08 wave 1; slices applied A, B, C).
- `processes.json` is written under the profile home of the job's scope; tests use temp homes (checked at landing: nothing new under `/root`). D-16.131.
