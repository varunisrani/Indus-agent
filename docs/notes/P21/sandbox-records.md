# Task: Sandbox records store (P21-01) Phase: 21 Owner: P21 slice B1 builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/environments/modal.py`
(`_snapshot_store`, `_load_snapshots`, `_update_snapshots`, `_store_direct_snapshot`,
`_delete_direct_snapshot`), `tools/environments/singularity.py` (`_snapshot_store`,
`_update_snapshots`), `tools/environments/base.py` (`_update_json_store`). Closed before writing.
Evidence: none.

What it does (own words): a persistent cloud sandbox or Singularity overlay outlives the process
that made it, so the next process needs to know what to restore from: a vendor snapshot id, an
overlay directory or a sandbox name. Each backend kind keeps one small JSON file of such records in
the profile home. Several processes (CLI, gateway, delegated children, the idle reaper) may write
the same file at the same moment; every read and write therefore goes through one locked
read-modify-write, and a broken file never stops a sandbox from starting.

Inputs → outputs:
- `SandboxRecords(home, kind, profile_id, *, clock=None, lock_timeout=10.0, wait=None)` or
  `SandboxRecords.for_scope(scope, kind, *, clock=None)`.
- `get(task_key) -> SandboxRecord | None`, `put(task_key, *, snapshot_id=..., overlay_path=...,
  sandbox_name=...) -> SandboxRecord` (fields not passed keep their value; `None` clears one),
  `delete(task_key, *, only_if_snapshot=None) -> bool` (with `only_if_snapshot`, only when the
  stored snapshot id still equals it), `keys() -> list[str]` (this profile's task keys).
- `SandboxRecord(snapshot_id, overlay_path, sandbox_name, created_at, updated_at)`; timestamps are
  ISO 8601 UTC from the injected clock.
- Low level: `mutate_records(path, mutation, *, lock_timeout, clock, wait) -> T` — the one function
  that locks, re-reads, applies `mutation(data) -> T` to the raw dict and writes only when the dict
  changed.
- File: `<home>/sandboxes/records/<kind>.json` (0600, dir 0700), lock `<kind>.json.lock`, keys
  `<profile id>:<task key>`.
- `SandboxRecordBusy` (a `BackendError`, code `backend.sandbox_records_busy`) when the lock is not
  had within 10 s.

Files: `src/indus/backends/sandbox_records.py`, `tests/unit/backends/test_sandbox_records.py`.

Edge cases / failure modes:
- Two processes mutating together: the flock serialises them; the re-read inside the lock makes
  sure neither overwrites the other's entry (200 × 2 processes test).
- Corrupt JSON or a non-object top level: moved aside to `<kind>.json.corrupt-<YYYYmmddTHHMMSSZ>`
  (from the injected clock), one warning, treated as empty; the next write starts a clean file.
  A single malformed entry (not an object) reads as absent.
- Lock held by a wedged process: `SandboxRecordBusy` after 10 s (injected clock + wait). The
  reference degrades to an unlocked write; we refuse instead (a lost record means a lost snapshot).
- Kind names are `[a-z0-9_]+`; anything else is a programming error (`ValueError`).
- Profile isolation: keys are always `<profile id>:<task key>`; `get`/`keys` only see the scope's
  own prefix, so profile B never reads profile A's snapshot (R02.8). No legacy un-prefixed keys
  are read or migrated (D-16.3).
- Nothing reads the home at import; the scope is passed in.

Our design differences / decisions:
- One file per kind under `sandboxes/records/` instead of one file per backend at the home root.
- Records hold three optional fields plus timestamps instead of a bare snapshot id string.
- Lock timeout raises instead of writing unlocked.

Tests we will write:
- round trip, partial update keeps fields, `None` clears, delete with and without a snapshot guard
- two processes × 200 puts each, all 400 keys present
- profile isolation (A writes, B sees nothing; same task key, different records)
- corrupt file quarantined with the clock stamp, one warning, store usable after
- lock held by another thread → `SandboxRecordBusy` after 10 s on a fake clock
- file mode 0600, dir mode 0700 (POSIX only)

## Decisions for the lead
- Lock contention raises `SandboxRecordBusy` (no unlocked fallback). Callers in B2's lifecycles
  should treat it like "no record" on restore and log on save.

Compare checklist (filled after the rewrite; reopened `tools/environments/modal.py`,
`tools/environments/base.py`):
- [x] Read-modify-write under a cross-process lock with a 10 s acquire deadline (test:
  `test_sandbox_records.py::test_two_processes_lose_no_update`, `::test_lock_timeout_raises_busy`)
- [x] Write only when the data changed (test: `::test_get_does_not_write`)
- [x] Delete only when the stored snapshot still matches (test: `::test_delete_with_snapshot_guard`)
- Deliberate differences: the reference falls back to an unlocked update when the lock cannot be
  had; we raise `SandboxRecordBusy`. The reference keeps a legacy bare-task-id key and migrates it;
  we have no legacy keys (D-16.3). Corrupt files are quarantined, not silently overwritten.
- Not taken: the reference's per-backend store file names at the home root.

## Status

Done (2026-10-19). Consumed by P21-04 (overlay path) here and by B2's Modal/Daytona/Vercel tasks.
