# Task: Drain, restart, shutdown, watchdogs (P15-25) Phase: 15 Owner: wave-20 builder W3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/run_shutdown.py`
(constants and state transitions only: interrupting running agents, marking running sessions
resume-pending before the drain wait, shutdown notices), `gateway/drain_control.py` (all),
`gateway/shutdown_watchdog.py` (constants), `gateway/restart_loop_guard.py` (all),
`gateway/systemd_notify.py` (all), `gateway/restart.py` (SIGUSR1 path, exit 75, supervisor markers).
Research 06 §3.22; R06.17, R06.20.
Evidence: none.

What it does (own words): the daemon's signals and its ways of ending.
- SIGINT / SIGTERM → stop: the status goes `draining`, new turns get a short "restarting /
  maintenance" notice, chat turns get `gateway.restart_drain_timeout` (default 0 s) and are then
  interrupted; their routes are flagged restart-interrupted **before** the wait (so a kill during
  the drain still leaves the flag) and their chats are told the work continues when they next
  write (boot recovery resumes them once, P15-26). Then the parts and adapters stop (5 s each) and
  the process exits 0. A planned stop recorded by the CLI/unit (`record_planned_stop`) is read so
  the exit is labelled `planned_stop` instead of `signal`.
- SIGUSR1 → graceful restart: refuse new turns, wait for the running ones up to
  `gateway.restart_after_turn_timeout` (1800 s), then stop the same way and exit 75 so the
  supervisor starts a fresh process.
- SIGUSR2 → dump every thread's stack and every asyncio task's stack to
  `logs/gateway_faulthandler.log`; the daemon keeps running.
- Timeouts: signal interrupt grace 1 s, post-interrupt grace 5 s, adapter disconnect 5 s each,
  executor quiesce 2 s.
- External drain marker `<home>/gateway_drain.json` (written by dashboards later): while active the
  status is `draining` and new turns get a "maintenance" reply; removing it resumes. A malformed
  file counts as active; a marker is stale (ignored) when its `epoch` differs from this machine's
  boot epoch or when it is older than 3,600 s (R06.20). Checked by a 1 s watcher.
- Watchdogs: (1) loop watchdog — a thread asks the event loop to run a callback every 30 s and
  waits 10 s; 3 misses in a row → stacks dumped, status `degraded` (`exit_reason:
  loop_watchdog`), hard exit 75; (2) startup watchdog — not `running` within 300 s → stacks,
  `startup_failed`, hard exit 75; (3) shutdown watchdog — once a stop begins, a thread hard-exits
  after the drain budget + 60 s grace.
- systemd: with `NOTIFY_SOCKET` set, `READY=1` when running and `STOPPING=1` at stop; with
  `gateway.systemd_watchdog_seconds` > 0, `WATCHDOG=1` every half interval while the loop is on
  time.
- Restart-loop guard: boots that find restart-interrupted sessions are recorded in
  `<home>/gateway_boots.json` (≤ 50 kept); boots chain while each gap is ≤ 300 s (never less than
  the 60 s window); the third chained boot trips the guard and that boot skips auto-resume (the
  flags stay for the next person's message). Any I/O failure fails open (not tripped).
- The daemon exports `_INDUS_GATEWAY=1` into its environment so `indus gateway stop/restart` run by
  the agent's own terminal tool can refuse (W4 checks it).

Inputs → outputs: signals, the drain marker, config timeouts → status transitions, notices,
route flags, exit code 0 / 75, stack dumps.

Files: `src/indus/gateway/daemon/lifecycle.py`, `src/indus/gateway/daemon/watchdogs.py`,
`src/indus/gateway/daemon/drain.py`, `tests/unit/gateway/daemon/test_drain.py`,
`tests/unit/gateway/daemon/test_watchdogs.py`, `tests/integration/gateway/test_signals.py`.

Edge cases / failure modes:
- a second SIGTERM during the drain → the drain shortens (interrupt now), never a second stop.
- SIGUSR1 then SIGTERM → the stop wins: the after-turn wait ends, chat turns are interrupted and
  the exit code is 0 (a person asked for a stop, not a restart).
- marker with a future `requested_at` (clock skew) → honoured; no epoch on either side → only
  the age check applies.
- the loop watchdog's own probe must not run on the loop (it is a thread) and never write files
  on the loop.
- `NOTIFY_SOCKET` set but unreachable → notify returns False, never raises.

Our design differences / decisions:
- Marker file name `gateway_drain.json`; epoch = Linux boot id plus PID 1's start time (empty
  elsewhere, which disables the epoch check).
- One `Lifecycle` object owns the stop/restart request, the drain flags and the exit code; signal
  handlers only call into it.
- Scale-to-zero, idle exit and the hosted relay are not in P15.

Tests we will write:
- drain marker table: absent, active, malformed (active), other epoch (stale), old (stale), future.
- restart-loop guard table: 2 fast boots no, 3rd trips; slow chain with 200 s gaps trips; a 400 s
  gap resets; I/O error fails open; ≤ 50 kept.
- watchdogs with a fake probe: 3 misses trip, a hit resets the count; shutdown watchdog fires
  after the budget.
- systemd notify over a real Unix datagram socket in tmp.
- real signals to a subprocess: SIGTERM mid-turn interrupts, notifies and exits 0; SIGUSR1 waits
  for the running turn then exits 75; SIGUSR2 writes the stack log and keeps running.

## Decisions for the lead
- Planned-stop record format lives in `state.gateway_runtime` (W4's `stop` writes it).
- `_INDUS_GATEWAY` is set in the daemon process; W4's commands must refuse under it.
- Stack dumps go to `logs/gateway_faulthandler.log` (append), each with a header line.

- The daemon never writes the process env; `_INDUS_GATEWAY` rides the scope env view and
  child_env's force entry (see the daemon-process note).
- systemd watchdog interval comes from `gateway.systemd_watchdog_seconds` (the unit's
  `WatchdogSec` is generated from the same row, P15-28), not from `WATCHDOG_USEC`.
- Every daemon wait uses the injected clock (`Clock.sleep` / `monotonic`); only the watchdog
  threads use wall time.

Compare checklist (filled after the rewrite; reopened `gateway/run_shutdown.py` stop phases
(`_stop_begin_teardown`, `_stop_drain_active_work`, `_stop_interrupt_remaining_work`,
`_stop_finalize_agents_and_adapters`), `gateway/drain_control.py`, `gateway/shutdown_watchdog.py`
constants, `gateway/restart_loop_guard.py`, `gateway/systemd_notify.py`, `gateway/restart.py`):
- [x] routes flagged restart-interrupted before the drain wait; finished turns unflagged
  (`tests/integration/gateway/test_signals.py::test_sigterm_mid_turn_interrupts_notifies_and_the_next_boot_resumes_once`)
- [x] SIGTERM: drain 0 s, interrupt, notify, exit 0 (same test)
- [x] SIGUSR1: wait for the turn, refuse new turns, exit 75
  (`::test_sigusr1_lets_the_turn_finish_refuses_new_ones_and_exits_75`)
- [x] SIGUSR2: thread + task stacks, keeps running (`::test_sigusr2_dumps_stacks_and_keeps_running`)
- [x] drain marker: malformed active, other epoch / older than 3,600 s stale, future honoured
  (`tests/unit/gateway/daemon/test_drain.py::test_marker_verdicts`,
  `tests/integration/gateway/test_signals.py::test_an_external_drain_marker_refuses_new_turns_until_it_is_removed`)
- [x] loop watchdog 30 s / 10 s / 3 strikes → stacks, `degraded`, 75
  (`tests/unit/gateway/daemon/test_watchdogs.py::test_three_unanswered_probes_in_a_row_trip_the_watchdog`,
  `tests/integration/gateway/test_signals.py::test_a_wedged_loop_trips_the_watchdog_writes_degraded_and_exits_75`)
- [x] restart-loop guard: chained gaps up to 300 s, 3 trips, 50 kept, fails open; only boots with
  interrupted sessions count (`test_watchdogs.py::test_restart_loop_guard_chains_boots`,
  `::test_restart_loop_guard_keeps_fifty_and_fails_open`)
- [x] sd_notify: READY with STATUS, WATCHDOG at half interval while on time, STOPPING once
  (`test_watchdogs.py::test_sd_notify_datagrams`)
- [x] planned stop labelled (`test_signals.py::test_sigterm_mid_turn_interrupts_notifies_and_the_next_boot_resumes_once`)
- Deliberate differences: only chats whose turn is cut get the stop notice (the reference also
  notifies every active chat and the home channels before draining); a single interrupt (the
  reference re-signals work still live after the grace); tool subprocesses end with the terminal
  runtime's shutdown at service close rather than an explicit off-loop kill sweep; no floor timer,
  no heartbeat file (the status file's heartbeat serves), no launchd exit-timeout capping (P15-28).
- Not taken: scale-to-zero, idle exit, hosted relay dormancy, detached restart commands, API-run
  and cron drains (the cron part's own `stop`, W2; API runs P15-31).

## Status

Done (2026-10-19). Files as listed above; the executor quiesce (2 s) is not a separate step: the
blocking pools are process-wide and outlive one daemon.
