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

## w28-e15.patch: P16-25 Photon adapter and sidecar — done, ships DISABLED (base c84f1db)
- Builder checks: photon unit 65 passed, contract 11 checks; brief set 546 passed / 48 skipped; gateway/wizards/config/
  integration gateway 2474 passed; pnpm 957 passed (photon-sidecar 22); pnpm -r typecheck, biome, ruff, pyright,
  lint-imports, tooling.checks ok. No packages (Python or Node); both locks unchanged — integrator: confirm
  `pnpm install --frozen-lockfile --offline` accepts the new workspace package without a lock change.
- Licence: spectrum-ts not available offline; reference lock shows MIT for spectrum-ts but one transitive BlueOak-1.0.0
  (not allow-listed) and a postinstall patch (refused). Sidecar built on our own provider interface (SpectrumLink), like the
  WhatsApp bridge; operator-supplied provider. OWNER DECISION alongside Baileys.
- New: apps/photon-sidecar (package.json @indus/photon-sidecar, src main/http/link/relay, fakeLink, tests),
  gateway/adapters/photon (__init__, adapter, sidecar — imports PidRecord/Child/spawn_process/port_free from
  whatsapp/sidecar.py), tests/fakes/platform/photon_wire.py, contract photon_case.py, unit tests, docs/notes/P16/photon.md.
- Shared: contract conftest import; schema row platforms.photon + test; traps R02.22 refs; ACC-K12-2.
- Wiring (core, proposed): BUILTIN_NAMES += photon after bluebubbles; PlatformEntry("photon", "iMessage (Photon)", lazy
  indus.gateway.adapters.photon:create_adapter, check_fn=modules_installed("httpx"), required_env=("PHOTON_PROJECT_ID",
  "PHOTON_PROJECT_SECRET"), INDUS_PHOTON_* policy env, max_len=8000, dm_is_private=True, platform_hint=photon.PLATFORM_HINT,
  lock_env=("PHOTON_PROJECT_ID",), install_hint=<in report/note>); NO BUILTIN_CREDENTIALS row while disabled (a row would
  enable from env alone); target rule (+<6–15 digits>, any;-;+<digits>, any;+;<id>, email handles); setup step (two
  credentials to SecretStore + provider, enabled: true, connect as check); indus send case with FakeLauncher; doctor port
  8789; remove photon from E17's matrix pending allow-list.
- Decisions (D-ids): Spectrum not shipped / adapter disabled with operator provider; tapbacks only with
  platforms.photon.reactions; no own typing cooldown; never reap a port held by another process (8789 unless
  sidecar_port); lazy media; 502 on link send = maybe-delivered, no text resend; npm ci --ignore-scripts with no install
  fallback; reply-quote memory in-memory only.
- Slip: builder's pnpm fetched 85 packages from the network into its scratchpad store before being pinned offline (nothing
  under /root, no lock change). /root/.npm/_logs/2026-10-20T14_05_* came from another worktree (L3) — owner item.
- Follow-ups: owner decision on a Spectrum provider + live lane; processing tapbacks once the runner calls them; polls/
  effects, setup device login, E.164 redaction in session descriptions (core); sidecar packaging (P23/P25).

## w28-l3.patch: P17-26 dual-host matrix + P17-27 shared-backend demo E2E — done (base 3d74df4)
- Builder checks: integration service + e2e service + touched suites 163 passed; broad run 5000 passed / 2 load timeouts
  (gateway start_status_stop, feishu media) that pass alone 2/2 — integrator: watch them in the full gate; e2e 3/3 (~30 s);
  matrix ~3.5 min, marked slow; pnpm 908 passed; typecheck, biome, ruff, pyright, lint-imports, tooling.checks ok. No
  stand-ins, no packages, no protocol/config changes.
- New: tests/integration/service/conftest_hosts.py (session_host inprocess/attached, HostRig, allow-list loader),
  test_dual_host_matrix.py (49 cases), tests/fakes/daemon.py, tests/fixtures/service/dual_host_allowlist.yaml,
  apps/tui/src/test/hosts.ts + hosts.test.tsx, tests/e2e/service/test_shared_backend.py, apps/tui/src/test/
  sharedBackendDriver.tsx (vite build --ssr into ignored node_modules/.shared-backend), notes dual-host-matrix.md,
  shared-backend-demo.md.
- Real P17 fixes (each with a failing-first test): (1) daemon emits message.complete + session.usage after turn.done (live
  and replay); (2) SessionService.create(source=) stores the creating surface so -c/--resume latest works after attached
  runs; (3) RemoteSessionService turns request.settled{answered} into server_request.cancelled(answered_elsewhere);
  (4) TUI steerParams sends the running generation; (5) lost turns reported after a daemon restart (CLI lists /discard
  lines after a replay gap; TUI reconnect + outcome-unknown line).
- Shared: service/daemon_host.py, session_service.py, remote/host.py (L2 also edits), cli/attach.py, attach_remote.py,
  unit tests; apps/tui appRuntime/recovery/testRuntime/entry/turnStore/submission/localCommands; traps R05.1, R09.11;
  ACC-K51-1/2.
- Allow-list decisions: cli.z.approvals (D-16.490), cli.q.denies (D-16.487), bridge.lost_mid_turn (D-16.10), proposed new:
  daemon sends no session.info/session.title; daemon serves no model.options / config.get/set.
- Follow-ups: unattended on prompt.submit + run-scoped auto-approve on the daemon; prompt.submit generation 0 when idle;
  session.resume returns no stored messages; prompts typed on another surface not shown; admission.state for unknown
  admissions in the TUI; usage_numbers duplicated (daemon_host vs tui_bridge/events); new session has no row until first
  turn (D-16.426); Demo 3 SIGKILL-mid-send not under both hosts.
