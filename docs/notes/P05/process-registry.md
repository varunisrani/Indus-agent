# Task: Process registry — background jobs (P05-13) Phase: 05 Owner: wave-2 builder C

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/process_registry.py`
(`ProcessSession`, `spawn_local`, `_spawn_local_pty`, `_track_started`, `_reader_loop`,
`_move_to_finished`, `_publish_finished`, heartbeat loop, `_check_watch_patterns`,
`_global_watch_admit`, `get`/`_resolve_prefix`, `_prune_if_needed`), `tools/process_registry_notifications.py`
(event rendering, heartbeat tail), `tools/process_registry_termination.py` (kill order and survivor
check), `tools/process_registry_wait_policy.py` (read for P05-14). Spec: research `02-tools.md`
§3.12, §3.22, traps R02.12, R02.17, R02.18. Closed before writing.
Evidence: none.

What it does (own words): keeps every background job the agent starts — its output, its state and
who should hear about it — and turns exits, watched lines and heartbeats into messages that start a
new turn where a surface can receive them.

- Spawn (local backend only for now): `bash` in its own session/process group, stdout+stderr into
  one pipe, or a pseudo-terminal (`ptyprocess`, extra `pty`) when asked; environment from the
  local environment's builder (child env + PATH fixes), cwd from the call. The record exists, with
  its owner (profile home, session id, env key), before any output is read. No pid → the record is
  `exited` with reason `failed_start`, never reported as running.
- Reading needs no thread per job: the pipe or PTY fd is non-blocking and the event loop reads it
  when readable; the exit is noticed through a Linux pidfd (a 0.2 s clock poll elsewhere). On exit
  the pipe is drained without waiting (a grandchild that keeps the pipe open cannot hold the
  completion back). Output goes into a rolling 200,000-character buffer.
- Ids `proc_` + 12 hex; any unique prefix of at least 4 hex digits is accepted.
- Limits: at most 64 tracked; finished jobs kept 30 minutes; over the cap the least recently used
  finished job is dropped first; 64 running jobs refuse a new one.
- Kill: the local backend's ordered kill (P05-03: snapshot descendants, SIGTERM the leader, grace
  `terminal.daemon_term_grace_s`, SIGTERM survivors, SIGKILL the rest, recycled-PID guard).
- Notifications (`process_notify.py`): a `Deliveries` table says where each (profile, session) can
  receive a completion — the surface registers a sink; nothing registered means not deliverable.
  - notify-on-exit queues one completion carrying a redacted 2,000-character tail; a receipt hook
    (P11) runs before the event is handed over.
  - where completions cannot be delivered (one-shot, cron, delegated children: nothing registered)
    notify flags are cleared and the result tells the model to poll (R02.17).
  - watch patterns: ≤ 1 notice per 15 s per process; a window with dropped matches is a strike;
    3 consecutive strike windows or 8 delivered notices disable watching (notify-on-exit instead,
    with one notice saying so); a global breaker allows 15 notices per 10 s across all processes,
    then 30 s of silence with one "tripped" and one "resumed" notice (R02.18).
  - heartbeat every N ≥ 60 s with the output since the last tick (≤ 2,000 characters), quiet ticks
    skipped; one ticker task every 5 s serves every process.
  - notify + watch → watch dropped with a note.
- `persist_on_release`: `release(session)` (session end, `/new`) kills the session's jobs except
  those; process exit (`shutdown`) kills everything.

Inputs → outputs: spawn request → `ProcessRecord`; process output/exit → buffer, state,
`ProcessEvent`s to the registered sink.

Files: `src/indus/backends/processes.py`, `src/indus/backends/process_notify.py`,
`tests/unit/backends/test_processes.py`, `tests/unit/backends/test_process_notify.py`.
Shared: `pyproject.toml` (extra `pty = ["ptyprocess==0.7.0"]`), `uv.lock` (relocked here to test;
lead re-locks), `docs/plan/phases/00-layout-and-conventions.md` §4 (ptyprocess row).

Edge cases / failure modes:
- `bash` missing, bad cwd, Popen raising → `failed_start` record with the error as its output.
- process exits before the reader is armed → the drain at exit still collects the output.
- a grandchild keeps the pipe open after the leader exits → completion fires at the leader's exit.
- UTF-8 split across reads → incremental decoder.
- PTY extra not installed → pipe mode with a note.
- kill of an exited job → `already_exited` with the tail.
- watch match after exit → ignored.
- the delivery sink raises → logged; the registry keeps going.

Our design differences / decisions:
- asyncio-native reading (fd readers + pidfd) instead of one reader thread per job.
- No systemd scope / memory cap, no crash-recovery checkpoint file, no retained receipts (P11), no
  sandbox spawn path (P11), no handoff (P10).
- Lookups are limited to the caller's profile home (R02.8): another profile's id is "not found".
- Delivery is a registered sink per (profile, session) rather than a global queue drained by every
  surface; the service (P06) registers one per attached interactive session.

Tests we will write:
- real short-lived processes: spawn → exit code + output; failed start; prefix lookup; PTY spawn
  (skipped without the extra); kill uses the ordered kill (SIGTERM-ignoring child SIGKILLed);
  grandchild holding the pipe does not delay completion; release spares persist_on_release.
- fake clock: watch spacing and strikes, lifetime cap, global breaker (16 matches in 10 s →
  tripped), heartbeat ticks (quiet skipped, ≤ 2,000 chars), LRU at 65, finished TTL.
- delivery: completion only to a registered sink; notify cleared for an unregistered session;
  completion text redacted.

## Decisions for the lead
- `ptyprocess==0.7.0` (ISC, 2020-12-28) added as extra `pty`; row added to 00-layout §4.
  ptyprocess ships no type information; it is used through a small typed shim. It forks a
  multi-threaded process (Python warns about `forkpty()`); its child only sets up the terminal and
  execs, so that one warning is silenced at the call.
- Running cap: a 65th *running* job is refused (the reference lets the table grow); finished ones
  are dropped LRU first.
- Completion sink wiring into `SessionService` (start a turn, or queue while one runs) is P06's;
  until then nothing is deliverable and every notify is turned into "poll instead".

Compare checklist (filled after the rewrite; reopened the four reference files above):
- [x] owner recorded before the reader starts; failed start never running
  (test: `test_processes.py::test_failed_start_is_not_running`)
- [x] orphaned pipe does not block completion (`::test_grandchild_holding_pipe_does_not_delay_exit`)
- [x] kill order via P05-03 (`::test_kill_sigkills_a_sigterm_ignoring_child`)
- [x] watch spacing/strikes/lifetime/global breaker (`test_process_notify.py::test_watch_*`,
  `::test_global_breaker_trips_at_sixteen`)
- [x] heartbeat skips quiet ticks and caps output (`::test_heartbeat_*`)
- [x] completion only where deliverable (`test_processes.py::test_notify_needs_a_registered_session`,
  `test_terminal_plan.py::test_notify_kept_only_where_deliverable`)
- [x] LRU at 65, TTL 30 min (`::test_lru_drops_the_least_recently_used_finished_job`,
  `::test_finished_jobs_are_kept_thirty_minutes`)
- Deliberate differences: kill result `exit_code` is the real one the process ended with (or -15 when
  unknown) rather than always -15; the strike rule is the same, but strikes are counted per
  15 s window on the injected clock.
- Not taken: systemd scopes, checkpoint/recovery, retained receipts (hook only), sandbox spawns,
  ownership handoff, desktop live-output sinks, Windows.

## Status

Done (2026-10-18).

## Integration (lead)

Merged in the P05 wave 2 integration (2026-10-18, lead's verify tree).
- No code change at the merge. P06 host wiring left open: register a completion sink per session
  in `Deliveries` (start a turn or queue one, checking `ProcessRegistry.consumed(process_id)`
  first), and `await terminal.runtime().shutdown()` at exit. Until then nothing is deliverable and
  notify falls back to "poll instead".
