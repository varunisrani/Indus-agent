# Task: Interactive trajectory saver (P22-10) Phase: 22 Owner: wave-21 builder R2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/trajectory.py` (all).
Research 10 §3.3 (interactive saver). Closed before writing.

What it does (own words): with `indus chat --save-trajectories` (or config
`research.save_trajectories: true`) every finished turn of a CLI session appends the whole
conversation so far, as one training sample, to a JSON Lines file in the working directory:
`trajectory_samples.jsonl` when the turn completed, `failed_trajectories.jsonl` when it did not.
The sample comes from the one shared converter (P22-03), so it has the same shape as a batch row's
`conversations`. Several writers may append to the same file at once (a gateway, batch workers,
two chats), so each append holds an exclusive lock on the whole file around write + flush.

Inputs → outputs: `sample_record(turns, model, completed, timestamp)` →
`{conversations, timestamp, model, completed}`; `target_file(directory, completed)`;
`append_locked(path, record)` (serialise first, open in append mode, lock, write, flush, unlock);
`redact_turns(turns, scope)` (every value through the P05 output redactor in force mode, so no
secret reaches the disk even with `security.redact_secrets` off); `TrajectorySaver(scope,
directory)` with `attach(service)` → undo, an `on_turn_end` listener that loads the session's
stored messages, converts, redacts and appends (synchronously, in the service's end-of-turn
bookkeeping, so a one-shot run cannot close the service before the sample is written).

Lock: POSIX `fcntl.flock(LOCK_EX)`; Windows `msvcrt.locking` on one byte at offset 0 (seek to 0,
lock, seek to end; unlock the same byte). The line is built before the lock is taken.

Edge cases: a session with no user message yet → nothing saved; a write error is logged as a
warning and never breaks the turn; scaffold rows (internal notes) are not part of the sample; the
prompt is the first user message; a rotating compaction's new session id is followed
(`current_id`).

Files: `src/indus/research/saver.py`, `tests/unit/research/test_saver.py`; shared:
`src/indus/cli/chat.py` (flag), `src/indus/cli/args.py` (`ChatArgs.save_trajectories`),
`src/indus/cli/runtime.py` (`ServiceSpec.save_trajectories`, attach in `build_service`),
`src/indus/foundation/config/schema.py` + its test (`research.save_trajectories`).

Our design differences / decisions: the sample is redacted before it is written (the reference
writes raw text); the timestamp is UTC from the scope clock; the files go to the session's working
directory (the `--in`/worktree folder when one is entered); the listener runs when the turn has
ended (a few milliseconds of reading and one append); a failed write is a warning in the log, not
on the screen.

Tests we will write: two processes × 200 appends → 400 valid lines; completed vs failed routing;
the record is the shared converter's output for the golden fixtures; a secret in a tool result is
masked on disk; a `chat -q --save-trajectories` run against the fake model writes the file; the
config switch alone turns it on.

## Decisions for the lead
- Wired for the CLI (`chat`, `chat -q`, and `-z` through the config switch) via `build_service`;
  the TUI bridge and the gateway build their own services and are not wired (gateway is outside
  this slice).

Compare checklist (filled after the rewrite; reopened `agent/trajectory.py`):
- [x] completed → `trajectory_samples.jsonl`, else `failed_trajectories.jsonl`; record `{conversations,
  timestamp, model, completed}` (test: `tests/unit/research/test_saver.py::test_completed_and_failed_go_to_their_own_files`)
- [x] exclusive whole-file lock around write + flush, line serialised first; flock / 1-byte msvcrt
  range at offset 0 (test: `test_two_processes_append_400_valid_lines`)
- [x] a failed save is a log warning, never an error for the turn (code: `TrajectorySaver.save_turn`)
- [x] shared converter (test: `test_saved_sample_is_the_shared_converters_output`; R10.7)
- Deliberate differences: values pass the P05 redactor in force mode before writing (test:
  `test_secrets_never_reach_the_disk`); UTC timestamps from the scope clock; files land in the
  session's working directory; the listener is synchronous inside the end-of-turn bookkeeping so
  a one-shot `chat -q` cannot lose the sample to the service closing; the reference's scratchpad
  tag helpers are not taken (not our tag).
- Not taken: a per-call `filename` override.

## Status

Done (2026-10-19). Shared: `src/indus/cli/chat.py` (flag), `src/indus/cli/args.py`
(`ChatArgs.save_trajectories`), `src/indus/cli/runtime.py` (`ServiceSpec.save_trajectories`,
`_trajectory_saver` in `build_service`), `src/indus/foundation/config/schema.py` +
`tests/unit/foundation/config/test_schema.py` (`research.save_trajectories`),
`docs/user/reference/trajectory-format.md` (a section on saved chat turns).
