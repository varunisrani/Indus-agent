# Wave 29 parked patches (P18, P19)

## w29-w2.patch: P18-07 PTY bridge + P18-08 keep-alive registry + P18-09 chat WS routes + P18-10 profile scope — done (base e02d3f5)
- Builder checks: web unit/contract/integration + test_tui_launch 148 passed; ruff, pyright, lint-imports, tooling.checks ok
  (similarity guard flagged 3 short runs in bridge.py/registry.py, rewritten). No packages (ptyprocess 0.7.0 ISC already
  locked as the `pty` extra — P18-01's `web` extra should include it, plus pywinpty on Windows; PTY tests skip without).
- New: src/indus/web/pty/{__init__,bridge,resize,registry,channels}.py, web/scope.py, web/ws/{pty_route,events_fanout,
  rpc_sidecar,chat_gate}.py (chat_gate: assumed W1 gate interface, purposes web-pty/web-events/web-rpc, SidecarLink), tests
  unit/integration/contract web, fakes tests/fakes/pty/*, notes docs/notes/P18/{pty-bridge,pty-registry,chat-ws-routes,
  profile-scope}.md.
- STAND-IN: tests/contract/web/_stubs_w2.py (W1's gate). Interface: `await gate(websocket, purpose)` accepts and returns an
  identity with .credential in {token, ticket, internal} or closes and returns None; internal credential value + loopback
  base URL for SidecarLink. web/ws/__init__.py is W1's (namespace package until then).
- Shared: cli/tui_launch.py (~20 lines: INDUS_TUI_DASHBOARD_SESSION_FILE absolute regular non-link user-owned file → renderer
  writes its session id there, launcher keeps it) + 2 tests; traps R09.6/7/19/20 + R02.22 line; ACC-L4-1; empty test
  __init__.py files (W1 may add the same).
- Wiring for the app factory (W1/integrator): mount PtyRoute.endpoint /api/pty, EventsRoutes.publish_endpoint /api/pub,
  events_endpoint /api/events, RpcSidecar.endpoint /api/ws (purpose via chat_gate.purpose_of); one PtyRegistry(clock=) per
  backend with run_reaper() and close_all() at stop; one ChannelFiles() closed at stop; one EventsFanout;
  install(app, ProfileScopes(base_scope)).
- Decisions (D-ids): POSIX fd-readiness reads (0.2 s) instead of thread polling; no psutil (killpg + /proc); PTY child is
  the real `indus --tui`; /api/pub internal credential only; 1:1 path closes 4410 on child exit (reference 1000); resume key
  as given (no lineage walk); multi-profile read per request; invalid profile id → 404; P18-01 must not publish the
  backend's .env into the process env.
- Follow-ups: TUI publishing its events (read INDUS_TUI_SIDECAR_URL / INDUS_TUI_SIDECAR_CREDENTIAL); forkpty-from-threads
  exec helper; server-side resume-id canonicalisation.
