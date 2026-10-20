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

## w29-f1.patch: P18-17..22 web frontend prework (SPA shell, REST client, Sessions, Config+Keys, Logs/Analytics/Files, Cron/Skills/Plugins/MCP) — done (base e02d3f5)
- Builder checks: pnpm 1029 passed (94 new in @indus/web, against the typed fake backend src/testing/mockApi.ts);
  pnpm -r typecheck clean; biome apps/web clean (22 pre-existing format problems in tests/fixtures + tests/fakes JSON on
  main); web build ok (largest chunk react 257 kB); tooling.checks ok (similarity rewrites done). No Python.
- PATCH INCLUDES pnpm-lock.yaml (builder ran `pnpm install` ONLINE with the store in its scratchpad; the integrator must
  re-verify with `pnpm install --frozen-lockfile` and licence-check every new package).
- New npm (exact pins): deps react-dom 19.3.0, react-router 8.4.0, @tanstack/react-query 5.104.1, @radix-ui/react-dialog
  1.1.23, @radix-ui/react-switch 1.3.7; dev tailwindcss 4.3.3, @tailwindcss/vite 4.3.3, @testing-library/react 16.3.3,
  @testing-library/dom 10.4.2, @types/react 19.3.0, @types/react-dom 19.3.0, axe-core 4.13.0 (MPL-2.0, allow-listed),
  jsdom 29.1.1 (30 needs Node >= 22.22.2). All MIT otherwise. Check publish ages (>= 14 days).
- New: apps/web (@indus/web: vite/vitest/tsconfig, shell, lib/api.ts + wsUrl + bootstrap + restTypes (hand-written until
  P18-11..16 generate), pages, ui, testing), notes docs/notes/P18/{spa-shell,rest-client,page-sessions,page-config-keys,
  page-logs-files,page-cron-skills}.md.
- STAND-IN: apps/web/src/test/_stubs_f1.ts (generated route list {method, path, scoped}) — replace with P18-11..16 output.
- Shared: biome.json (Tailwind directives for apps/web/src/**/*.css only); traps R09.1/5/6/20/22 lines; ACC-L1-1, L3-1,
  L4-1, L5-1, L6-1, L20-1 lines.
- Backend contract the REST tasks must match (see report in note rest-client.md): sessions mutation-snapshot {exists,
  revision, generation}, DELETE identity in query / PATCH in body; POST /api/sessions/prune {older_than_days, dry_run} ->
  {dry_run, count, skipped_open}; search snippets <mark> rendered as text; GET /api/config/effective {entries:[{key, value,
  source}]}; files upload multipart POST /api/files/upload (path, file), DELETE /api/files?path=; /api/plugins, install
  {url, sha}, enable|disable; cron grammar strings, delivery targets {targets}; POST /api/auth/ws-ticket {purpose}.
- Wiring: shell/routes.ts pending() entries for P18-23..25; <App chatHost={lazy(...)}> for P18-26; sidebarFooter for
  P18-25/28 (auth identity call with allowUnauthorized); P18-29 dev-bootstrap plugin in vite.config.ts.
- Decisions (D-ids): scoped families add /api/sessions and /api/cron; family names /api/daemon, /api/providers/login,
  /api/plugins; whole-segment matching; profile switch remounts pages with own query cache; no font files / i18n / plugin
  slots; SVG charts; Docs link to repo docs until P25-20; logs pause while hidden; prune dry-run first; Config Reset discards
  unsaved edits only.
- Follow-ups: Node 22.22.0 here vs engines >= 24.21; the 22 fixture JSON format problems.

## w29-a1.patch: P19-01..07 ACP entry, initialize/authenticate, backends (local + attached), session/new, load/resume/fork, list — done (base e02d3f5)
- Builder checks: unit/contract acp + schema 115 passed; config, cli/test_app, protocol, state sessions passed; contract cli
  -k "acp or table" 3 passed; ruff, pyright, lint-imports, tooling.checks ok. Full suite and perf not run.
- NEW PACKAGE: agent-client-protocol==0.12.1 (Apache-2.0 from the sdist LICENSE; schema v1.19.0, protocol 1) in the `acp`
  extra AND the dev group; uv.lock restored — integrator re-locks online (ACP tests import the SDK).
- New: src/indus/acp/{agent,auth,backend,local_host,attached_host,store_view,sessions,history,listing,paths,stdout_guard,
  entry,command}.py; tests unit/acp, contract/acp (conftest, harness, tests), tests/fakes/acp_client.py,
  tests/fakes/daemon.py (CONFLICT: L3 (w28-l3) also adds tests/fakes/daemon.py — merge both harnesses), notes
  docs/notes/P19/acp-*.md.
- Shared: pyproject (extra + dev pin), acp/__init__.py (one-line docstring), cli/app.py (lazy acp row), cli/entry.py
  (STDIO_COMMANDS += acp), schema acp.allow_local_fallback (default false) + test, state/sessions.py (set_cwd/set_cwd_in),
  protocol/attach.py (ACP_EDITOR_POLICY), contract cli non_tty_table.yaml, THIRD_PARTY_NOTICES, 00-layout §4,
  16-what-we-change D-16.29/D-16.83 pointers.
- Decisions (D-ids): SDK pin + dev group; schema v1.19.0 has no `models` field -> model picker as configOptions select
  category "model", no session/set_model (affects P19-12); command in src/indus/acp/command.py (layer contract), not
  cli/commands/acp.py; no daemon auto-start on first dial (D-16.83); attached launch options beyond cwd/model/toolsets
  refused (D-16.490); fork returns child id without replay; authenticate failure -32000; session/list reads the store
  without a daemon; local model/rewind/reset answer mutation_unavailable until P19-12.
- Daemon interface assumed (served by tests/fakes/daemon.py; today's daemon answers -32601): describe stands in for
  runtime.describe; session.mutate {session_id, operation (editor_mcp|branch|workspace), payload, request_id}; config.set
  show_reasoning; session.usage; admission.cancel. Until the daemon advertises acp.editor_policy and serves these,
  attached session/new answers acp_policy_unavailable; --local works.
- Follow-ups: trap rows R09.9/13/14/16 and ACC-M1-1/2, M2-1/2/3, M8-1/2, Q7-2; tool kinds in replay (P19-10); approval
  re-offered at load by a second editor test; local frozen launch policy not written to the session row;
  `indus tools install browser` for --setup-browser.
