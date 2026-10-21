# Wave 39 parked patches (P24 UI wave; lead: Agent 2) — base f16f343

Brief: `brief.md` (slices U1 = P24-19/20/25/26/33, U2 = P24-27..31, U3 = P24-32/34..37). Integrate together with
`.agents/wip/w37` and `.agents/wip/w38` (the P24 backend slices) — see HANDOFF NEXT STEP.

## w39-u2.patch: P24-27 management pages 1, 28 management pages 2, 29 connections registry + remote auth, 30 SSH remotes, 31 bots in the desktop app — done (28 without the learning-graph read; 29 without the serve's RFC 8252 broker; 30 main-side modules, no real sshd; 31 against stand-ins)
- D-16.691 (remote WebSocket dial) FIXED here: POST /api/auth/ws-ticket mints a ticket for a token-mode client (was 409
  not_gated); authenticate_dashboard_upgrade accepts a single-use purpose- and profile-bound ticket in token mode (the token
  stays accepted for the web SPA); every remote connection.ticket mints a server ticket with main's credential
  (mintRemoteTicket); main reserves the dial with DialReservations.reserveTicketDial (strips Origin only, matched by window +
  URL); the window offers indus-dashboard-v1 + indus-dashboard-ticket.<t>; only that single-use ticket crosses the bridge;
  the old swap (reserveRemote) is unused, kept and marked superseded. Pinned by install.test.ts (registry remote dials with
  a server-minted ticket per dial, never the token), test_ws_auth.py::test_a_token_mode_ticket_opens_one_socket_through_the_app,
  test_ws_ticket_route.py::test_a_token_mode_client_gets_a_ticket. NOT run inside a real Electron against a real serve —
  the integrator must (Electron E2E). Flip D-16.691 / D-16.685 at integration.
- Builder checks: pnpm lint (781) / fmt:check / -r typecheck clean; check-ipc, gen-bridge-types --check ok; desktop vitest
  1293 passed / 12 skipped (137 files; late-edited files rerun green); web vitest 251 passed; Python ruff / pyright 0 /
  lint-imports kept; unit service/bots_rpc, cli/importers, web, ws_auth, foundation + session-import / export-keys /
  route-list contracts 1086 passed / 2 skipped (one test_scope failure fixed, module 20 passed); contract+integration web 288
  passed / 1 failed = tests/contract/web/test_tui_publish.py::test_a_refused_credential_stops_the_feed_at_once — fails 3/3
  alone EVEN WITH U2's serve files reverted to HEAD (pre-existing in its env; check on main, root-cause); tooling.checks 11/11.
- New: apps/desktop/electron/connections/{registry, nativeOauth, proxyHeaders, remoteDial, install, ssh, sshUpdate}.ts
  (+tests), electron/artifacts/download.ts (+test), testing/fakeServe.ts, src/app/{manage, messaging, webhooks, cron,
  profiles, agents, command-center, learning-map, artifacts}/*, src/bots/*, src/settings/ConnectionsPage.tsx;
  src/indus/service/{session_import, bots_rpc}.py, src/indus/cli/importers/{__init__ (byte-identical to w37 X1's), sessions}.py,
  src/indus/web/routes/session_import.py; tests unit/cli/importers/*, unit/service/test_bots_rpc.py, contract/web/
  test_{session_import_routes, profile_export_keys}.py; tests/fakes/ssh_bin/{ssh (755), README.md}; notes docs/notes/P24/
  {management-pages-1, management-pages-2, connections, ssh-remotes, desktop-bots}.md.
- Shared: electron/backend/{install, remote, tickets}.ts, electron/boot/orchestrator.ts (rest.headers), electron/api/proxy.ts
  (ProxyTarget.headers), electron/secrets.ts (per-connection names), electron/ipc/{channels, schemas}.ts (+9 invoke channels,
  111 → 120: indus:connections:{list, test, save, remove, set-primary, set-launch-mode, sign-in, sign-out}, indus:artifacts:
  download), electron/main.ts, src/global.d.ts, src/app/{routes.ts, pages.tsx, App.tsx} (pages slot; reserved pages webhooks,
  command-center, learning-map, bots), src/main.tsx, src/i18n/en.ts, src/settings/{pages.ts, customPages.tsx};
  apps/web/src/lib/api.ts (/api/session-import scoped), apps/web/src/test/routes.generated.ts; src/indus/{service/ws_auth.py,
  web/routes/ws_ticket.py, web/routes/__init__.py}, foundation/errors.py (3 bots.* codes); ledgers ACC-N16-1/2, N17-1/2,
  N22-1, R08.4/5/11/23.
- STAND-INS: tests/unit/service/_stubs_u2.py for w37 R1's state/bot_chat.py (find(profile) → tip|None, ensure(profile) →
  (tip, created), title-only, adopt before mint, over the real store); apps/desktop/src/test/_stubs_u2.ts for U3's
  contribution registry (register({id, routes, nav}) → unregister()). bots.* / groups.* are not in the generated protocol —
  the renderer reaches them via rpcOf(connection) (a widening cast): declare them in protocol + mount on the daemon.
- Decisions (number at integration): session-import mapping in indus/service/session_import.py (cli facade); import route reads
  HOME/USERPROFILE from scope.env; registry file connections.json (0600), default launch mode primary; SSH recovery record
  <userData>/ssh-update/<id>.json, a host without Indus needs an install command passed in (no invented URL, D-16.690);
  bot metadata <profile home>/bot.json, canonical title "Bot chat" (R1).
- Follow-ups: RFC 8252 broker on the serve; GET /api/learning/graph + memory delete; a subagent.* daemon event; per-run cron
  history over REST; protocol + daemon mounting for bots.* / groups.*; profile_rows into profiles.list; R1 adapters; register
  the bots contribution with U3's registry; preview pane uses SshBackends.previewPort; remote / SSH Electron E2E spec; QR
  image for Telegram setup; preload-bridge.md channel count → 120. No packages, no lock change.
