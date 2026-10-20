# Wave 28 parked patches (P17-23..27)

## w28-l1.patch: P17-23 idle exit for auto-started daemons — done (base 3d74df4)
- Builder checks: 22 new tests; daemon/config/integration service + ensure/daemon/attach suites 379 passed / 2 skipped;
  ruff, pyright, lint-imports, tooling.checks ok. No stand-ins, no packages, no TS.
- New: gateway/daemon/idle_exit.py, tests/unit/gateway/daemon/test_idle_exit.py, docs/notes/P17/idle-exit.md.
- Shared: gateway/daemon/process.py (INDUS_GATEWAY_AUTOSTART -> daemon.autostarted, removed from the daemon's scope env
  only; DaemonOptions.idle_exit_seconds; idle_watch started at running; terminal_processes set), service/gateway_ensure.py
  (IDLE_EXIT_REASON; waits out a draining/stopping idle-exit daemon then starts a new one; discover answers
  absent/idle_exit), config row gateway.idle_exit_seconds (float >= 0, default 600) + test, ACC-K47-1.
- Behaviour: armed only when autostarted and not supervised; poll min(30, max(0.1, window/4)); busy on any of: enabled/
  connected platform, HTTP part, unused ticket, WS connection, non-messaging viewer, running turn, queued input (not
  behind an unknown turn), cron running or due within 60 s (unless paused), loop/heartbeat due within 60 s, terminal
  background process, any change since last poll; check errors count as busy. Exit: draining/idle_exit, exit 0, status
  stopped/idle_exit (not a planned stop), next ensure restarts silently.
- Decisions for the lead (D-ids at integration): cron rule follows the row (due within the next tick), not the
  reference's "any runnable job"; extra busy checks; unknown turns / loops / heartbeats rules; P17-15 file touched;
  marker scope. Proposed §2.7 row in the note.
- Follow-ups: kanban / hosted-room / handoff checks once those features exist; describe capabilities not emptied while
  leaving. P17-27 e2e can shorten the window via gateway.idle_exit_seconds.
