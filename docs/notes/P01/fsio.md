# Task: file I/O helpers (P01-07) Phase: 01 Owner: S-config builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `utils.py` (the atomic replace helper,
the shared temp-file writer and the JSON writer), `hermes_cli/auth.py` (the cross-process file lock
helper only, for the re-entrancy and degrade rules). Closed before writing.
Evidence: none.

What it does (own words): every write to a user-owned file goes through one helper that never
leaves a half-written file behind. Data goes into a temp file beside the target, is flushed and
synced, and then renamed over the target; the directory is synced on POSIX so the rename survives a
crash. New files are owner-only; an existing file keeps its mode (Docker volumes often carry 0640
and must stay readable to the group). Symlinks are refused by default so a planted link cannot
redirect a write; when the caller opts in, a live link is written through to its target and a
dangling link is replaced by a regular file (we never create a file at a path the link chose). A
cross-process lock (`FileLock`) serialises read-modify-write cycles on shared files (`.env`,
secrets, config).

Inputs → outputs:
- `atomic_write_bytes(path, data, *, mode=None, preserve_mode=True, follow_symlink=False) -> Path`,
  `atomic_write_text(path, text, *, encoding="utf-8", ...)`, `atomic_write_json(path, obj, *,
  sort_keys=False, indent=2, ...)` (always a trailing newline). Return the path actually written.
- `FileLock(path, timeout=15.0, poll=0.05, clock=None, wait=None)`: context manager and
  `acquire()`/`release()`; raises `LockTimeout` (a `TimeoutError`) naming the lock path and the
  holder pid read from the lock file.

Files: `src/indus/foundation/fsio.py`, `tests/unit/foundation/test_fsio.py`.

Edge cases / failure modes:
- any exception (also `KeyboardInterrupt`) after the temp file exists removes the temp file and
  leaves the target untouched.
- missing parent dir → `FileNotFoundError` (callers own directory creation, so a write cannot
  resurrect a deleted profile dir).
- target is a symlink and `follow_symlink` is false → `SymlinkRefused` (an `OSError`).
- dangling symlink with `follow_symlink` → the link itself is replaced, the dangling target is not
  created.
- lock on a filesystem without `flock` (ENOLCK / EOPNOTSUPP / EINVAL) → one warning per process,
  then depth-only locking (never a crash).
- same thread re-enters the same lock path → depth counter, no kernel call; a different thread in
  the same process blocks like another process would (flock is per open file description).

Our design differences / decisions:
- temp name `.<name>.tmp-<pid>-<random hex>`, created `O_EXCL` at 0600 and chmod-ed to the final
  mode before the rename, so the content is never readable at the process umask.
- the reference falls back to copy+unlink on cross-device renames; we do not (the temp file is in
  the same directory, so a rename cannot cross devices) — left out until a real case shows up.
- re-entrancy is tracked per resolved lock path in a module registry, not per caller-supplied
  holder object, so two `FileLock` objects for the same path in one thread do not deadlock.
- waiting: the deadline is read from the injected clock's `monotonic()` (default `time.monotonic`)
  and the pause between polls is an injectable `wait(seconds)` (default: a `threading.Event` wait,
  which is not a banned sleep). Tests pass `FakeClock.monotonic` and `FakeClock.advance`.

Tests we will write:
- crash simulation (exception from inside the write) keeps the original and leaves no temp file.
- new file 0600; existing 0640 kept; explicit `mode` wins; `preserve_mode=False` → 0600.
- symlink refused; live symlink followed keeps the link; dangling link replaced, target not made.
- JSON writer: sorted keys option, trailing newline.
- lock: same-thread re-entry; timeout with fake clock against a raw flock holder, message names
  the pid; second process (multiprocessing, `slow`) times out with a short real timeout; degrade
  path with a patched `flock` raising ENOLCK warns once.

## Decisions for the lead
- `FileLock` takes `wait=` besides `clock=` because the P01-19 `Clock` protocol only has an async
  `sleep`; a lock is synchronous. Change: drop `wait` if `Clock` grows a sync sleep.

Compare checklist (filled after the rewrite; reopened `utils.py`, `hermes_cli/auth.py`):
- [x] temp + fsync + replace + temp removed on any exception (test: `tests/unit/foundation/test_fsio.py::test_crash_mid_write_keeps_original_and_no_temp`)
- [x] new file mode never umask-readable; existing mode kept (tests: `::test_new_file_is_owner_only`, `::test_existing_mode_is_preserved`)
- [x] lock re-entrant per thread, degrade to depth-only with a warning, holder pid in the timeout message (tests: `::test_lock_reentrant_same_thread`, `::test_lock_degrades_without_flock`, `::test_lock_timeout_names_holder_pid`)
- Deliberate differences: the reference writes through a symlink to keep it; we refuse unless asked
  and never create a dangling link's target. No cross-device copy fallback (see above). The
  reference's minimum 1 s lock deadline is not kept: the caller's timeout is used as given.
- Not taken: owner (uid/gid) preservation — P01-03 owns ownership policy (`INDUS_UID`/`INDUS_GID`
  are not in our plan yet).

## Status

Done (2026-10-18). Nothing outside the task's files.
