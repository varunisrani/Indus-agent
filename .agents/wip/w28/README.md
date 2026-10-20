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

## w28-l2.patch: P17-24 profile binding on attach + P17-25 protocol additions and codegen — done (base 3d74df4)
- Builder checks: 28 new tests; unit protocol/service/gateway/cli/tui_bridge + contract + integration service/gateway/cli/
  state 5386 passed / 54 skipped; codegen --check clean; pnpm 899 passed; pnpm -r typecheck clean; ruff, pyright,
  lint-imports, tooling.checks ok. No stand-ins, no packages, no TS source changes (generated only).
- New: service/remote/profile_guard.py (+test), tests/unit/protocol/test_attach_contract.py, _attach_samples.py, notes
  docs/notes/P17/profile-guard.md, protocol-attach.md.
- Shared: protocol/{errors,attach,events,methods,codegen}.py + packages/protocol/src/generated/*; gateway/ws/methods.py
  (runtime.describe, admission.receipt, maintenance.run; submit refusals answer own codes); gateway/ws/server.py (scope
  guard); gateway/daemon/process.py (2 lines: endpoint built inside the mount try — L1 also edits this file);
  control_socket.py (peer_uid via guard); service/gateway_control.py (foreign_listener check); service/remote/channel.py
  (grant_for checks describe vs own home; gateway.ready mismatch closes 4005); service/remote/host.py (keeps journal entry
  on OUTCOME_UNKNOWN; filters presence events); cli/commands/gateway_grant.py; service/ws_auth.py,
  request_redelivery.py (codes from the table); foundation/secrets.py (SecretStore.index_path); protocol tests; traps
  R05.14, R05.15, R03.23.
- Codes: 3005 STALE_GENERATION, 3006 ADMISSION_CONFLICT, 3007 QUEUE_FULL, 3008 OUTCOME_UNKNOWN, 3009 POLICY_CONFLICT,
  3010 LEASE_LOST, 4005 PROFILE_MISMATCH, 4401 TICKET_INVALID, 4404 NOT_SHOWN; extra 2005 ADMISSION_NOT_FOUND;
  OUT_OF_BAND_ERRORS list.
- Behaviour changes: exact retry of a lost input answers OUTCOME_UNKNOWN (was "streaming" forever); submit refusals carry
  own codes; store.turn_lease_lost -> LEASE_LOST; missing profile_id = mismatch (fail closed).
- Wiring owed: admission.state not emitted yet; daemon controls keep D-16.365 (no STALE_GENERATION raise); creation
  policy still not on the wire (D-16.490).
- Follow-ups: emit admission.state; policy on the wire; retire server_request.cancelled; STALE_GENERATION decision.
