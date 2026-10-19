# Task: Checkpoint bound to the dataset hash (P22-05) Phase: 22 Owner: wave-19 builder R1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `batch_runner.py` (empty / load /
save checkpoint, and how `run` uses the completed indices). Research 10 §3.1, trap R10.3. Closed
before writing.

What it does (own words): the run directory keeps one checkpoint saying which prompt indices are
done. Indices only mean something for the dataset they were counted on, so the checkpoint records
the dataset hash, and a run whose dataset hash differs is refused instead of silently skipping
rows of a different file (R10.3, D-16.31).

Inputs → outputs: `Checkpoint{format 1, run_name, dataset_hash, distribution, seed, completed
(sorted ints), batch_stats {"<n>": {processed, skipped, discarded}}, updated_at}` at
`<run dir>/checkpoint.json`. `load_checkpoint(run_dir, run_name=, dataset_hash=, distribution=,
seed=, stamp=, warn=)` → Checkpoint; raises `DatasetChanged(old, new)` on a hash mismatch.
`open_checkpoint(..., new_dataset=False)`: on mismatch with `new_dataset` the run dir contents
move to `previous-<stamp>/` and a fresh checkpoint starts. `save_checkpoint(run_dir, cp, now)`:
temp file in the same dir, write, flush, fsync, `os.replace`, then fsync the dir (best effort).

Edge cases / failure modes:
- missing file → empty checkpoint; unreadable or wrong shape → empty with a warning, the old file
  renamed to `checkpoint.json.bad-<stamp>` (kept, never deleted);
- another `run_name` in the file → empty (the file is overwritten by the next save);
- a crash between temp write and rename leaves the old checkpoint intact (only `*.tmp` litter,
  removed on the next save);
- distribution or seed differ from the file → kept from the new run with a warning (samples then
  mix two settings; the hash rule is about rows, not sampling);
- only the parent writes the checkpoint.

Files: `src/indus/research/checkpoint.py`, `tests/unit/research/test_checkpoint.py`.

Our design differences / decisions: format number and hash fields are new; `discarded` instead of
the reference's no-reasoning-specific key (more discard reasons may come); `previous-<stamp>/`
archive on `--new-dataset`; the bad copy instead of only a warning.

Tests we will write: changed dataset refused (both hashes in the message); `--new-dataset`
archives (old files under `previous-…`, fresh checkpoint); corrupt checkpoint warns and keeps a
copy; atomic write survives a crash injected between temp write and rename; run name change →
empty; round trip.

## Decisions for the lead
- Archive stamp `YYYYmmddTHHMMSSZ` from the scope clock; a clash adds `-2`, `-3`.

Compare checklist (filled after the rewrite; reopened the checkpoint methods and `run`, and the
reference checkpoint tests):
- [x] changed dataset refused, both hashes named (test: `tests/unit/research/test_checkpoint.py::test_changed_dataset_is_refused_naming_both_hashes`)
- [x] `--new-dataset` archives (test: `test_new_dataset_archives_the_old_run`)
- [x] corrupt checkpoint warns and keeps a copy (test: `test_corrupt_checkpoint_warns_and_keeps_a_copy`)
- [x] atomic write survives a crash before the rename; stale temp files cleared (test:
  `test_atomic_write_survives_a_crash_before_the_rename`)
- [x] gap found: the reference's save creates missing parent folders — fixed by `mkdir` in
  `save_checkpoint`, pinned by `test_save_creates_the_run_dir`
- Deliberate differences: indices are honoured only for the same dataset hash (the reference
  honours them for any dataset under the run name, R10.3, D-16.31); `discarded` instead of a
  no-reasoning-specific stat key; the bad file is kept.
- Not taken: the reference's lock around the write (only the parent writes, one thread).

## Status

Done (2026-10-19).
