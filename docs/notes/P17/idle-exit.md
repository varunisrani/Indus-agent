# Task: Idle exit for auto-started daemons (P17-23) Phase: 17 Owner: wave-28 builder L1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/run_idle_exit.py` (all:
arming, the busy probes, the poll loop, the exit). Research 06 §3.22 (idle exit / scale-to-zero),
research 05 §3.2 (a client's first ensure starts the daemon with idle exit on). Our P17-15
`service/gateway_ensure.py` (`AUTOSTART_ENV`, `planned_stop_reason`, the recover mode) and its
note, P17-22's note (`messaging` viewers), P15-24/25 `gateway/daemon/process.py`, `lifecycle.py`,
`status.py`, P17-08 `service/daemon_host.py` + `viewers.py`, P17-10 `ws_auth.TicketBook`, P17-13
`gateway/ws/server.py`, P15-21 `gateway/cron_host.py`, P14 `cron/store.py` / `cron/ticker.py` /
`cron/due.py`, P10 `core/session_scheduler.py`. Closed before writing.
Evidence: none.

What it does (own words): a daemon that a client's `ensure` started on its own (detached, not by a
service manager, not by `indus gateway run`/`start` by hand) is a convenience nobody else will stop.
Left alone, every throwaway profile (a script, a test, one `indus chat -q`) would keep a daemon
alive forever. So such a daemon watches itself and exits once it has served nobody and had nothing
to do for `gateway.idle_exit_seconds` (600 s by default; 0 turns it off). "Nothing to do" means all
of these, checked every poll:

- no attach client: no viewer of any session other than the messaging intake's own (`messaging`
  viewers are the gateway's own and never count), no live WebSocket connection, no ticket minted
  and not yet used (a client is between `session-ticket` and its dial);
- no turn: no running generation on the host, no session on the chat service that is busy or has
  input waiting, no chat turn holding a claim in the intake;
- no admission queued in the runtime ledger (queued rows stuck behind an unknown turn do not count:
  that queue is paused until a person resolves it, and the next daemon restarts it);
- no cron run in flight and no cron job due within the next tick (60 s);
- no messaging platform enabled (or connected), and no HTTP surface (API server, webhook) enabled:
  a person who configured one of those wants a daemon that stays;
- no loop or heartbeat due on a session the daemon holds within the next tick, no background
  process of the terminal tool still running in one of those sessions;
- nothing changed since the last poll (a new connection, a new session, a new event): a client that
  came and went between two polls still counts as activity.

A probe that cannot answer (unreadable cron store, a store error) counts as busy: the daemon stays.
Idle time is measured from the last poll that saw the daemon busy (or from arming). When it reaches
the window, the loop-only facts are checked once more with nothing awaited in between, and the exit
begins at once: the status file says `draining` with exit reason `idle_exit` (so a racing client
can tell), the lifecycle stops with reason `idle_exit` (exit code 0), and the normal shutdown runs.
The status file ends `stopped` with `exit_reason: idle_exit`. That is not a planned stop
(`planned_stop:<reason>`), so the next client's `ensure` — `start` or `recover` — starts a fresh
daemon without saying anything about it. A client that races the exit (its `identify` is answered
by the leaving daemon) waits for that process to go and then starts a new one.

Arming: only when the daemon's environment carries `INDUS_GATEWAY_AUTOSTART=1` (set by `ensure`'s
detached start, never by a service unit or `gateway start`) and the daemon is not supervised (a
service manager would just restart it). The marker is removed from the daemon's own environment
once read, so processes the agent starts do not inherit it.

Inputs → outputs: env `INDUS_GATEWAY_AUTOSTART`, config `gateway.idle_exit_seconds` (float ≥ 0,
default 600, re-read every poll so setting 0 disarms a running daemon), the daemon's live state →
a stop with reason `idle_exit`, status `stopped`/`idle_exit`, exit 0; `ensure` then starts again.

Files: `src/indus/gateway/daemon/idle_exit.py`, `tests/unit/gateway/daemon/test_idle_exit.py`.
Shared: `src/indus/gateway/daemon/process.py` (arm at `running`, `DaemonOptions.idle_exit_seconds`,
the terminal process table kept for the probe), `src/indus/service/gateway_ensure.py` (wait out a
daemon that is idle-exiting), `src/indus/foundation/config/schema.py` +
`tests/unit/foundation/config/test_schema.py` (the `gateway.idle_exit_seconds` row),
`docs/internals/acceptance-map.md` (ACC-K47-1).

Edge cases / failure modes:
- a client attaches at second 599 → the poll sees the viewer (or the connection, or the outstanding
  ticket) and idle time starts again from that poll;
- a short client between two polls → the activity fingerprint changed → counts as busy;
- a viewer that is the messaging intake's → ignored (an enabled platform already blocks the exit);
- `gateway.idle_exit_seconds: 0` or negative/garbage → 0 disables, garbage keeps the default;
- a supervised daemon, or one started without the marker → never armed;
- an exit racing a new client: the client's dial is refused (daemon stopping) or its `ensure` sees
  `draining`/`idle_exit` and waits for the old pid, then starts a new daemon;
- a probe raising → busy ("probe failed"), logged at debug level, the daemon stays;
- the daemon already stopping for another reason → the watcher ends without touching anything.

Our design differences / decisions:
- Setting name `gateway.idle_exit_seconds` (the phase row), not the reference's longer name.
- Stop reason is exactly `idle_exit` (the reference writes a sentence whose prefix clients match).
- Cron: only a job due within the next tick (or running) keeps the daemon (the phase row); the
  reference keeps it up while any runnable job exists. See "Decisions for the lead".
- No kanban, hosted-room, handoff or multi-profile probes (no such features in our daemon yet).
- The poll interval is `min(30, max(0.1, window / 4))` s; one idle reading at the end of the window
  plus the synchronous re-check replaces the reference's "two consecutive idle readings".

Tests we will write (`tests/unit/gateway/daemon/test_idle_exit.py`):
- fake clock, scripted probe: exits at exactly 600 s; busy at 590 → no exit until 1190; a
  fingerprint change resets; a raising probe keeps it up; window 0 disarms; re-check at the end
  vetoes the exit;
- the daemon probe over a stand-in daemon: attached viewer (not `messaging`), connection, ticket,
  running turn, queued admission (and one behind an unknown row), enabled platform, HTTP part,
  due/running cron, global cron pause, due loop → each named as the busy reason;
- arming: marker + unsupervised only; the marker is not inherited;
- a real in-process daemon (fake model, no platform) armed with a short window stops by itself
  with `stopped`/`idle_exit`, exit 0; a supervised one is not armed; a due cron job keeps it;
- `ensure`: `planned_stop_reason` is None after an idle exit; an idle-exiting daemon is waited out.

## Decisions for the lead
- Cron rule: the row says "no cron job due within the next tick"; the reference keeps the daemon
  up while any runnable job exists. With the row's rule a recurring job that comes due after the
  exit runs once as a catch-up when a client next starts the daemon, but a one-shot job past its
  grace (120 s) is closed as missed. Taken: the row. To switch, change `CRON_HORIZON_S` handling in
  `idle_exit.cron_busy` to "any runnable job" (one line).
- Extra busy conditions beyond the row (all fail closed): outstanding ticket, live WebSocket
  connection, activity fingerprint between polls, HTTP surface enabled, loops/heartbeats due on held
  sessions, running terminal background processes in held sessions, intake chat claims.
- Queued input stuck behind an unknown turn does not keep the daemon (that FIFO is paused until a
  person discards; the next daemon restarts it); the reference counts every queued row. Loops and
  heartbeats keep it only when due within the next tick on a session the daemon holds (the
  reference: any active one), same reasoning as cron.
- The marker `INDUS_GATEWAY_AUTOSTART` is removed from the daemon's scope env once read (processes
  it starts through the scope env do not inherit it); `os.environ` is left alone (raw-environ rule).
- `ensure` (P17-15 file) now waits for a daemon whose status says `draining`/`stopping` with exit
  reason `idle_exit` to exit and then starts a new one (`discover` answers `absent`/`idle_exit`).
- Proposed D-id row (§2.7): "Idle exit (P17-23) | Exit after 600 s with any runnable cron job
  blocking; a long reason label | `gateway.idle_exit_seconds`; only cron due within the next tick
  blocks; reason `idle_exit`; extra probes listed in the note".

Compare checklist (filled after the rewrite; reopened `gateway/run_idle_exit.py`):
- [x] armed only for an auto-started, unsupervised daemon (test:
  `test_idle_exit.py::test_a_supervised_or_hand_started_daemon_is_not_armed`,
  `::test_a_supervised_daemon_never_idles_out`, `::test_only_the_ensure_marker_arms`)
- [x] exits after the window with nothing to do, recorded as `idle_exit`, not a planned stop (test:
  `::test_exits_at_600_s_of_idle`, `::test_an_auto_started_daemon_stops_itself_as_idle_exit`)
- [x] clients: attached viewers, live connections, outstanding tickets, activity since the last
  look keep it; the intake's `messaging` viewers do not (test: `::test_every_quick_reason`,
  `::test_the_messaging_intake_is_not_a_client`, `::test_activity_between_two_polls_counts`,
  `::test_the_fingerprint_moves_with_connections_sessions_and_events`)
- [x] work: turns, queued admissions, cron, messaging platforms (also reconnecting ones: they are
  enabled), loops/heartbeats, background processes (test: `::test_every_quick_reason`,
  `::test_queued_input_keeps_the_daemon_unless_it_waits_behind_an_unknown_turn`,
  `::test_cron_due_within_the_next_tick_or_running_keeps_the_daemon`,
  `::test_cron_from_the_store_and_due_loops`, `::test_a_due_cron_job_keeps_the_daemon`,
  `::test_a_background_process_in_a_held_session_keeps_the_daemon`)
- [x] every probe fails closed (test: `::test_a_failing_probe_keeps_the_daemon_up`)
- [x] disk reads off the loop, loop-only facts re-checked with nothing awaited before the stop
  (test: `::test_the_last_look_vetoes_the_exit`)
- [x] the exit is visible (status `draining` with `idle_exit`) before the shutdown; a client that
  races it waits for the process to go, then starts a new one (test:
  `::test_ensure_waits_out_an_idle_exiting_daemon`)
- [x] window re-read every poll, 0 disables, a value that is not a number keeps the default (test:
  `::test_the_window_setting_and_the_poll_interval`, `::test_window_zero_disarms_and_a_new_window_counts_from_then`)
- [x] gap found: the reference stamps client activity at arming, so the window counts from the
  start — ours does the same (`IdleWatch.run` sets `idle_since` when it starts).
- Deliberate differences: a window of 0 at arming keeps polling, so turning the setting back on
  later arms the running daemon (the reference never arms it then); idle time counts from the last
  busy poll and one final synchronous re-check replaces "two consecutive idle readings"; the poll
  floor is 0.1 s (0.5 s there) so tests can use short windows; no warning is logged for a
  non-numeric setting (config validation reports it); queued input behind an unknown turn and
  cron jobs not due within the next tick do not keep the daemon (see Decisions); the descriptor's
  capabilities are not emptied while leaving (clients read the status file's `idle_exit` instead).
- Not taken: kanban boards, hosted rooms, pending handoffs, managed workers and the hosted
  scale-to-zero (no such features in our daemon; K47's hosted half is P3); multiplexed profiles
  (P24).

## Status

Done (2026-10-20). Shared files changed: `src/indus/gateway/daemon/process.py` (marker read and
removed from the daemon's env, `DaemonOptions.idle_exit_seconds`, `idle_watch` armed at `running`,
`terminal_processes` kept by the chat service build), `src/indus/service/gateway_ensure.py`
(`IDLE_EXIT_REASON`, an idle-exiting daemon is waited out / `absent` for `discover`),
`src/indus/foundation/config/schema.py` + `tests/unit/foundation/config/test_schema.py`
(`gateway.idle_exit_seconds`), `docs/internals/acceptance-map.md` (ACC-K47-1). No trap rows.

Integration (2026-10-20): D-16.540 (behaviour) and D-16.541 (busy probes, the racing client).
Test daemon profiles (`tests/fakes/daemon.ProfileHome`, the vitest hosts) set
`gateway.idle_exit_seconds: 0` unless a test asks for a window; the e2e
`tests/e2e/service/test_shared_backend.py::test_an_auto_started_daemon_idles_out_and_the_next_chat_restarts_it_silently`
runs the whole loop through `indus chat -q` (D-16.557). The daemon host now appends
`admission.state` for every admission, so a new input moves the activity fingerprint too.
