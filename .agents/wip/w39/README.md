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

## w39-u1.patch: P24-19 desktop pet + `/pet`, 20 wake word, 25 preview annotate + agent-driven automation, 26 capabilities page, 33 themes + locale plumbing — done (wiring owed below; 26 lacks two REST routes)
- Builder checks: Python ruff / pyright 0 / lint-imports; pytest own + foundation/config + contract/cli 746 passed / 2 skipped;
  neighbours (unit/service, tools/builtin/media, cli/setup, tui_bridge, contract/web) 921 passed; pnpm lint / fmt / -r
  typecheck (8 packages) clean; check-ipc, gen-bridge-types ok; desktop vitest 1296 passed (132 files); TUI 778 passed;
  @indus/ui 80 passed; tooling.checks ok. Not run: apps/web tests (untouched), Electron real-window E2E.
- New package: sherpa-onnx==1.13.8 (Apache-2.0, 2026-09-10, no deps) in a new `wake-sherpa` extra — integrator re-locks
  (uv.lock excluded from the patch).
- New: Python service/{pets, pets_rpc, wake_rpc, capabilities_rpc}.py, cli/commands/pet.py, tools/builtin/media/
  wake_word.py, tools/builtin/preview.py (+ tests; tests/fixtures/desktop/vscode_theme.json ours); electron/windows/
  {petWindow, wakeIndicator}.ts, electron/theme/marketplace.ts (+tests); renderer src/pet/*, src/wake/*,
  src/panes/preview/{annotate, automation}.ts, src/app/capabilities/*, src/theme/{vscodeImport, themeStore,
  AppearancePage}.ts(x), src/i18n/packs.ts (+tests); packages/ui/src/tokens/presets.ts (+test); apps/tui/src/commands/{wake,
  pet}.ts (+tests); notes docs/notes/P24/{pet, wake-word, preview-annotate, capabilities-page, themes}.md.
- Shared: pyproject.toml (wake-sherpa extra), cli/app.py (pet row), foundation/config/schema.py (pet, wake_word sections),
  contract cli non_tty_table + help catalogue; electron windows/{factory, manager, windowUrl}.ts (kinds pet, wake) + factory
  snapshot, ipc/{channels, schemas}.ts (+14: indus:pet:{show, hide, get, say, pointer, state}, indus:wake:{indicator,
  indicator-get, indicator-state}, indus:themes:{search, download}; each also checks the sender's window kind),
  src/global.d.ts, electron/main.ts (3 installs); renderer src/app/{App, bridge, theme}.tsx, i18n/en.ts, panes/{d2Panes,
  preview/PreviewPane}.tsx, profiles/storageFamilies.ts, settings/customPages.tsx; apps/tui/src/slash/localCommands.ts;
  packages/ui/src/index.ts; ledgers ACC-J22-1, J23-1, N12-2, N15-1, N20-2, N28-1, N34-1, R09.22.
- STAND-IN: src/test/_stubs_u1.ts for U3 (P24-32): DesktopPluginSource.list() → {id, name, version?, enabled}[] (app-level).
- WIRING OWED (integrator): protocol rows + TS codegen for pet.*, wake.*, capabilities.summary (declared beside their
  handlers); register on the daemon table, merge PET_DEADLINES / WAKE_DEADLINES; one daemon WakeRpc whose emit sends
  wake.detected to the owning connection + connection_closed on disconnect; preview.* server-request kinds in protocol +
  @indus/client, desktop routes them to windowPreviewAutomation.handle, `preview` port offered to tool handlers,
  preview.register_tools(registry, surface=…) for desktop-created sessions; wake.detected → TUI voice mode and desktop
  WakeController.attach with handOff = U3's dictation (P24-35); U3's voice settings page mounts WakeToggle;
  CapabilitiesRoute gets summary + U3's plugin source, delete the stand-in.
- OWNER DECISIONS: openWakeWord (the D-16.52 default) cannot be locked on Python 3.13 (tflite-runtime has no 3.13 wheels) —
  no `wake` extra; wait, or make sherpa-onnx the default engine; Porcupine left out (vendor key + vendor terms) — §2.8 entry?
- Decisions (number at integration): one picture per mood (no sprite sheet, no image lib), no gallery (D-16.23); pet CLI adds
  use / off / list; consent its own setting wake_word.consent, indicator always shows "listening"; annotation pins batch by
  shared container / containment, annotate_preview = agent-to-person notes; presets paint only once picked; per-profile
  appearance its own storage key.
- Follow-ups: REST DELETE /api/plugins/{name} and per-tool MCP enable; TUI /wake on needs a 180 s per-call deadline; terminal
  pane per-profile font; TUI theme sync colours; pet availability probe; wake busy-mic resume retry; /pet and /wake into the
  Python command registry; full-screen detection for other apps on Linux/Windows.

## w39-u3.patch: P24-32 desktop plugin SDK (PARTIAL), 34 uninstall from Settings, 35 desktop voice (daemon methods stood in), 36 dashboard admin console, 37 dashboard plugins — done otherwise
- Builder checks: pnpm lint / fmt / -r typecheck clean; desktop vitest 1310 passed (131 files; incl. plugin-scheme
  real-window test + realWindow security 8 passed); web vitest 275 passed (33 files); Python ruff / format clean, pyright 0,
  lint-imports kept; contract/cli + contract/web + unit/web + console 1170 passed / 1 failed = the same
  test_tui_publish.py::test_a_refused_credential_stops_the_feed_at_once (fails with all U3 src reverted too); tooling.checks
  ok (two similarity hits restructured). No packages.
- P24-36: engine in src/indus/service/console.py (layer rule; row says cli/console_engine.py, which holds the `indus console`
  REPL + `--run LINE`; no -c, the argv rewrite owns -c/--continue); each command a child process (python -m indus …,
  child_env, new process group); cancel/timeout kill the tree. P24-37: backend routes under /api/ext/<name>/ (D-16.587
  prefix; row says /api/plugins/<name>/); new `web-plugin` WS purpose. P24-34: offered only on confirmed ownership (CLI
  --gui-summary + success install receipt + not under /nix/store); typed phrase "uninstall everything" checked in main.
  P24-35: mic / talk buttons appear only when a VoicePort is passed to the Composer. P24-32 done: registry (transaction
  rollback, 200 contributions / plugin), SDK + context, disk plugins (load-time check, indus-plugin: scheme, hot reload),
  loader, host, status-bar area; NOT done: consumers for the other 13 shell areas, Settings → Plugins inventory page.
- New: apps/desktop/electron/plugins/{diskPlugins, importScan, sdkNames}.ts (+tests, pluginScheme.electron.test.ts),
  electron/uninstall.ts (+test), src/contrib/{registry, sdk, loader, host, react, testHost}.ts(x) (+tests),
  src/chat/voice/{dictation, conversation, VoiceButtons, strings.en} (+tests), src/settings/{UninstallSection.tsx (+test),
  uninstallStrings.en.ts}; apps/web/src/console/ConsoleModal.tsx (+test), apps/web/src/plugins/{registry, sdk, slots,
  usePluginState} (+tests); src/indus/service/console.py, src/indus/cli/console_engine.py, src/indus/web/{console,
  plugins}.py; tests contract/web/test_{console_socket, dashboard_plugins}.py, unit/cli/test_console_engine.py,
  tests/fixtures/web/plugins/ (4 sample plugins, ours); notes docs/notes/P24/{desktop-plugin-sdk, desktop-uninstall,
  desktop-voice, admin-console, dashboard-plugins}.md.
- Shared: electron/{main.ts, ipc/channels.ts, ipc/schemas.ts (+5: indus:plugins:{list, rescan, changed},
  indus:uninstall:{summary, run}), security/csp.ts (+test; script-src adds indus-plugin: only — the scheme must be registered
  corsEnabled), scripts/viteConfig.test.ts; renderer src/{global.d.ts, main.tsx, app/StatusBar.tsx, chat/composer/
  Composer.tsx, i18n/en.ts, profiles/storageFamilies.ts, settings/{context.tsx, customPages.tsx, pages.ts}}; apps/web/src/
  {App.tsx, main.tsx, lib/bootstrap.ts, shell/routes.ts, test/setup.ts, test/routes.generated.ts}; Python cli/app.py (console
  row), service/ws_auth.py (WEB_PLUGIN), web/{app.py, state.py, spa.py, routes/__init__.py}; tests contract/cli (help
  catalogue, non_tty_table), contract/web/test_route_list.py, unit/web/test_spa.py, unit/web/ws/test_ws_auth.py.
- STAND-IN: apps/desktop/src/test/_stubs_u3.ts — daemon voice.transcribe {session_id, audio_b64, mime} → {text},
  voice.speak {text} → {audio_b64, mime} (to be added on the daemon).
- Wiring owed: dashboard CLI flag for AppOptions.console_enabled; chatSlots passes voice={rpcVoicePort(...)} once the daemon
  has the voice methods; desktop plugin consumers (panes → paneRegistry, routes, sidebar, palette, keybinds, themes, composer,
  decorations, settings pages, transcript directives) + inventory page over PluginLoader.inventory(); desktop plugin sockets
  need a ticketed /api/ext/ route in main; dashboard header/footer plugin slots have no render point. U1's _stubs_u1.ts
  (DesktopPluginSource) and U2's _stubs_u2.ts (register({id, routes, nav})) map onto U3's registry/loader — reconcile.
- Decisions (number at integration): disk plugins one file per plugin <home>/desktop-plugins/<id>.js|.mjs; isolation: plugins
  still run in the page (not a sandbox, R07.19) — main checks every file before load (only SDK / React imports; refuses
  indusDesktop, require, process, eval, Function, __proto__), the SDK holds no bridge object, its RPC door is read-only;
  uninstall phrase "uninstall everything", the helper only runs the CLI and never deletes a packaged bundle itself.
- Follow-ups: plugin-route gate refuses a socket to a switched-off plugin before the upgrade (403) — an anonymous caller can
  tell off from on for plugin sockets (R09.4 holds for HTTP only); Windows uninstall may need a system Python.
