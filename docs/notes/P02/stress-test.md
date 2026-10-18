# Task: D-04.6 stress test (P02-21) Phase: 02 Owner: P02 wave-4 builder E

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`):
`tests/hermes_state/test_write_lock_patience.py` (all 233 lines, for the contention shapes only:
a sibling connection holding `BEGIN IMMEDIATE` / `BEGIN EXCLUSIVE` for multi-second stretches, a
shortened patience that must surface the "busy, not damaged" error, a DELETE-mode reader whose
cached schema meets a sibling's exclusive lock inside the FTS constructor). Closed before writing.
Our notes: `docs/notes/P02/{writer,readpool,maintenance,search,fts}.md`.
Evidence: none.

What it does (own words): the decision D-04.6 keeps the standard-library `sqlite3` module (with our
own writer / read-pool layer) instead of apsw, on one condition: the store survives real
multi-process contention without losing a row and without a lock error ever reaching a caller.
This test is that condition. Four writer processes, each with eight threads, append to their own
sessions through the public `messages.append` for a fixed wall-clock window, while a fifth
process forces one `maintenance.vacuum` in the middle (VACUUM rewrites the whole file under an
exclusive lock: the longest hold a healthy store sees) and a sixth lists, fetches recent
conversations and searches in a loop through the read pool. Afterwards the parent reopens the
store and checks the transcript against what every caller was told. Without this test a
regression in the writer's patience, the read path or VACUUM's locking would only show up as
lost turns on users' machines.

Inputs → outputs: `INDUS_STRESS_SECONDS` (default 60, registered in `tests/conftest.py` and read
at import, before the per-test env isolation drops `INDUS_*`) → one `slow` test per journal mode
that passes or fails, and a printed metrics line per run (mode, appends, appends/s, max and p99
append latency, vacuum duration and sizes, reader operations, reader max latency). Workers write
a JSON result file each; the parent aggregates.

Files: `tests/integration/state/test_stress.py`, `tests/integration/state/stress_worker.py`,
`tests/conftest.py` (shared: the default), this note.

Shape:
- parent creates the store (so the six processes do not race schema creation; R03.9 has its own
  unit test), records the journal mode, closes it, spawns the six workers with `INDUS_HOME` on the
  test's temp home (the isolation guard would refuse the real root), waits for six `ready` lines,
  then sends one wall-clock start/deadline pair on stdin to all of them.
- writer `w<k>`: opens its own handle, creates sessions `stress-w<k>-t<j>` (j = 0..7), then eight
  threads append `{"role": user|assistant, "content": "stress w<k> t<j> seq <n> …"}` until the
  deadline. Each thread records the returned id in call order, the call latency, and any
  exception (type and text). Never sleeps: the loop condition is the deadline.
- vacuum: opens a handle, waits on an `Event` until the window's midpoint (a deadline, not a
  sleep for timing), runs `vacuum(store, force=True)` once, records start, end and sizes.
- reader: loops `list_sessions`, `recent`, `search` (word, phrase, prefix) until the deadline,
  recording counts, hits, latency and exceptions.

Asserts (after all workers exit 0):
- no worker reported any exception; in particular no `StoreLocked` and no text containing
  "database is locked" (checked separately so the failure message names it).
- every returned id is present exactly once and carries its own message (session + sequence);
  per session the stored ids in id order equal the returned ids in call order, and there are no
  stored rows that no caller was told about; `message_count` matches.
- the VACUUM ran once, inside the window, with appends committed both before it started and
  after it ended (otherwise it did not stress anything).
- the reader made progress and its searches found rows.
- `PRAGMA integrity_check` = `ok`, `fts.integrity_ok` is True (strict check on the writer
  connection) and the index is not marked stale (a fail-open would hide FTS trouble).

Journal modes: the host SQLite here is 3.45.1, inside the WAL-reset bug range, so a new store opens
in DELETE mode and every read runs on the writer connection with the 5 s read busy timeout. The
test runs in the mode the host gives (`host`) and, where that is not WAL, once more with the
linked-version pin the unit tests use (`wal-pinned`: parent and workers patch
`journal.linked_sqlite_version`), which is the only way the read pool's pooled read-only
connections are stressed on this host.

Negative control: the same harness for a short window with every worker's append budget cut to
0.01 s: workers patch `writer.ROUTINE` to 0.01 s and bind the append path's budget
(`messages.TRANSCRIPT`) to it, because `messages.append` uses `TRANSCRIPT`, so patching `ROUTINE`
alone would not touch the code under test. A seventh process holds `BEGIN IMMEDIATE` for 1.5 s in
the middle (the reference's "sibling holds the write lock for seconds" shape, longer than the
writer connection's 1 s busy timeout). The control asserts that `StoreLocked` reached a caller
and that the same checker that passes the real run fails this one.

Edge cases / failure modes:
- a worker crash or a hang: every worker has a hard `wait(timeout=…)` of window + 120 s and its
  stderr is shown in the failure.
- start skew: imports and opens happen before `ready`; the shared deadline comes after all six
  are ready, so every process measures the same window.
- appends that raised may or may not have landed; the "no unknown rows" check only applies when
  no append raised (which the test also requires).
- the store is never pointed at a real home: `INDUS_HOME` is the per-test temp home.

Our design differences / decisions:
- the reference has no multi-process stress test; its patience tests hold the lock from a thread.
  Ours uses real processes and the public API only.
- "max write wait" is measured as the longest `messages.append` call (mutex wait + SQLite busy
  wait + retries + commit), i.e. what a turn would feel.

Tests we will write:
- `test_stress.py::test_store_survives_the_stress[host]`, `[wal-pinned]` (skipped when the host
  already opens WAL), `::test_negative_control_surfaces_store_locked`.

## Decisions for the lead
- Negative control binds the append budget to the patched `ROUTINE` (see above) and adds a 1.5 s
  lock holder so the failure is deterministic rather than load-dependent.
- `wal-pinned` runs WAL on a SQLite build inside the WAL-reset bug range (test-only, temp home).
  The bug's race is narrow and was never hit here; if a `wal-pinned` run ever fails with
  corruption on such a build, check it against that bug before blaming the store. Change: drop
  the parameter (the `host` run alone still decides D-04.6 on this host).
- `INDUS_STRESS_SECONDS` is read once at import into `tests.conftest.STRESS_SECONDS`, since the
  autouse isolation fixture removes every `INDUS_*` variable before a test body runs.

Compare checklist (filled after the rewrite; reopened `test_write_lock_patience.py`):
- [x] a sibling's multi-second write-lock hold is waited out by appends: the stress VACUUM hold
  (both modes) and the unit test `tests/unit/state/test_writer.py::test_lock_held_for_three_seconds_is_waited_out`;
  a forced VACUUM itself waits out a 3 s sibling hold in both modes (probe, not kept as a test:
  the writer's patience covers it).
- [x] a shortened patience surfaces the contention error, never a damage error (test:
  `tests/integration/state/test_stress.py::test_negative_control_surfaces_store_locked`, which
  also asserts the text says "contention").
- [x] DELETE-mode reads meeting a sibling's `BEGIN EXCLUSIVE` inside the FTS constructor: probed
  with a 2.5 s hold on an existing handle (search, list) and on a fresh open + search; all wait
  it out (2.5–3.2 s). Our lock classifier reads the BUSY result code first, so the constructor
  wording does not matter. Not kept as a test (no gap found).
- Deliberate differences: real processes and the public API instead of a lock-holding thread
  against private budget attributes.
- Not taken: the "budgets are at least 10 s / 30 s" constant assertions (`test_writer.py` pins
  the budgets themselves).

## Measurements

Host: 4 cores shared with other builders (load 0.5–1.7 during the runs), Linux, Python 3.13,
SQLite 3.45.1 (WAL-reset bug range → new stores open in DELETE). "Max write wait" is the longest
single `messages.append` call; p99 is the worst per-process p99. Each line is one run of the
whole file (`host`, then `wal-pinned`, then the control).

| run | mode | s | appends | appends/s | max wait | p99 wait | VACUUM | reader ops (max) |
|---|---|---|---|---|---|---|---|---|
| 10 s #1 | delete | 10 | 3,288 | 328.8 | 3.06 s | 1.84 s | 0.08 s, 1.56→1.45 MB | 79 (1.14 s) |
| 10 s #1 | wal | 10 | 7,001 | 700.1 | 1.59 s | 0.79 s | 0.09 s, 2.88→2.85 MB | 5,984 (0.04 s) |
| 10 s #2 | delete | 10 | 3,171 | 317.1 | 4.67 s | 2.60 s | 0.46 s | 82 (1.16 s) |
| 10 s #2 | wal | 10 | 6,908 | 690.8 | 1.68 s | 0.81 s | 0.14 s | 5,936 (0.03 s) |
| 10 s #3 | delete | 10 | 3,398 | 339.8 | 2.72 s | 2.56 s | 0.11 s | 78 (0.72 s) |
| 10 s #3 | wal | 10 | 6,855 | 685.5 | 1.40 s | 0.60 s | 0.06 s | 5,602 (0.08 s) |
| 60 s | delete | 60 | 18,379 | 306.3 | 6.44 s | 1.82 s | 0.94 s, 7.52→7.22 MB | 403 (1.75 s) |
| 60 s | wal | 60 | 37,807 | 630.1 | 2.14 s | 0.96 s | 0.51 s, 16.7→15.1 MB | 10,669 (0.07 s) |

Every run: zero exceptions in any caller, every returned id stored once in call order, no
unreported rows, `message_count` exact, `integrity_check` ok, FTS strict integrity-check ok, no
stale marker. The negative control failed as intended in all four runs (`StoreLocked` from every
writer during the 1.5 s hold; 218–226 appends/s outside it).

Observations (not failures):
- the long tail of write waits is in-process queueing: eight threads share one writer mutex, and
  the thread holding it can spend up to the connection's 1 s busy timeout inside SQLite before the
  writer's own retry loop runs. Worst seen 6.4 s against the 60 s `TRANSCRIPT` budget.
- DELETE mode halves write throughput and starves the reader (403 operations in 60 s, worst 1.75 s,
  every read on the writer connection) against 10,669 operations, worst 73 ms, through the WAL
  read pool. It stays correct; WAL on a fixed SQLite (≥ 3.51.3 or the 3.50.7 / 3.44.6 backports)
  is the shape users should get.

**D-04.6 verdict: stdlib `sqlite3` holds.** No lost or duplicated row, no lock error reached a
caller and both integrity checks passed in DELETE and WAL, at 10 s three times in a row and at
the 60 s default, with a forced VACUUM in the middle. No fallback to apsw is needed.

## Status

Done (2026-10-18). `test_stress.py` (3 `slow` tests: `host`, `wal-pinned`, negative control) and
`stress_worker.py`. Shared: `tests/conftest.py` (`STRESS_SECONDS`, 3 lines). No store bug found;
`docs/internals/traps-ledger.md` has no P02-21 Review row, left unchanged.
