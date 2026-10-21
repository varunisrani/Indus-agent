# Wave 34 parked patches (P20-08..24; lead: Agent 2) — base 9a1917d

Brief: the lead's wave-34 brief (slices M = P20-08..14, R = P20-15..18, C = P20-19..24). Contract: docs/notes/P20/
preload-bridge.md ("Contract for P20-08+"). Integrate R and C together (C stands in R's store), then M.

## w34-c.patch: P20-19 composer, 20 submission journals, 21 busy actions + queue, 22 approval / clarify / sudo / secret cards, 23 model picker + status bar, 24 slash commands — done
- Builder checks: pnpm lint / fmt:check / -r typecheck clean; check-ipc, gen-bridge-types --check, dump-slash-catalog
  --check ok; apps/desktop vitest 350 passed / 10 skipped (31 files; 96 new); tooling.checks ok. No Python.
- STAND-INS (first line `// WAVE34 STAND-IN (owner: slice R) — the lead drops this file at merge`; drop at merge, C then
  runs against R's real modules — reconcile names):
  - src/gateway/connectionStore.ts: REQUEST_TIMEOUTS {default 120_000, startup 60_000, promptSubmit 1_800_000,
    approvalRespond 330_000}; RequestOptions {timeoutMs?, signal?}; SessionEvent = EventTypes[keyof EventTypes];
    ConnectionMode "local"|"remote"; interface GatewayConnection { mode; codeVersion; request<M>(method, params, options?)
    → Promise<result>; onSessionEvent(sessionId, listener) → unsubscribe; reconnect() }; GatewayConnectionProvider;
    useGatewayConnection(): GatewayConnection | null.
  - src/gateway/serverRequests.ts: OpenServerRequest { id, sessionId, kind, request, replayed, generation?, answer(a) →
    boolean (first wins) }; class ServerRequestStore { handlers() (approval/clarify/sudo/secret), list(sessionId),
    subscribe(l) }; ServerRequestStoreProvider; useOpenServerRequests(sessionId|null).
  - src/i18n/index.ts: MessageKey = keyof typeof en; t(key, vars?) with {name}; useT().
  - src/i18n/en.ts: `export const en = {...CHAT_STRINGS}` — R's REAL en.ts MUST spread CHAT_STRINGS from
    src/chat/strings.en.ts or C's strings disappear.
  Mounts: <CardStack sessionId> once after the transcript rows; <Composer sessionId …> under the transcript;
  <StatusBar sessionId> in the shell.
- New: electron/journal/preparedSubmissions.ts (+test), src/chat/composer/{Composer.tsx, completion.ts, attachments.ts,
  goneSession.ts} (+tests), src/chat/{busyActions.ts, QueuePanel.tsx, inflightJournal.ts, slash.ts, ModelPicker.tsx} (+tests),
  src/chat/slashCatalog.json (generated), src/chat/cards/{CardStack, ApprovalCard, ClarifyCard, SecretDialog}.tsx
  (+CardStack test), src/app/StatusBar.tsx (+test), scripts/dump-slash-catalog.mjs, src/chat/{strings.en.ts, rpcErrors.ts,
  testFakes.ts}, notes docs/notes/P20/{composer, journals, busy-actions, approval-cards, model-status, slash}.md.
- Shared: apps/desktop/package.json (react / react-dom 19.3.0; dev @testing-library/react 16.3.3, @testing-library/dom
  10.4.2, @types/react(-dom) 19.3.0, jsdom 29.1.1 — same pins as apps/web, already in the lock; dump:slash script),
  pnpm-lock.yaml (+21 lines, re-lock at merge), tsconfig.json (jsx react-jsx, resolveJsonModule), vitest.config.ts (src
  tests, jsdom per-file docblock) — R likely edits the same three; electron/main.ts (+1 install line, P20-20),
  electron/ipc/schemas.ts (indus:journal:* shapes: prepare {requestId, sessionId, payload}; retire {requestId, receipt
  {outcome accepted|rejected|unknown, admissionId?}}; list entries {requestId, sessionId, createdAt, updatedAt, state
  prepared|uncertain, admissionId?, payload}), src/global.d.ts regenerated. channels.ts unchanged.
- Decisions (number at integration): queue pause held in the window (daemon has no pause — Stop cancels this window's own
  queued admissions and holds them until Resume; `admission.hold` on the daemon is a P17 follow-up); upload body for slice M
  (P20-14): POST /api/files/upload {multipart:{path, overwrite:false, file:{name,type,base64}}}, main encodes multipart;
  uploads land in <cwd>/.indus/attachments/<id>-<name>; local path guard = a one-byte fs.readText (P20-29 could add a
  check channel); journal caps 256 KiB payload / 200 live entries; profile default via config.get model.default /
  config.set model; sticky model keyed by profile only (P20-26 follow-up); arming delay 250 ms; "always" not
  double-confirmed.
- Env note: fake-backend tests fail with a RELATIVE TMPDIR; use an absolute short TMPDIR (e.g. $S/tc).

## w34-m.patch: P20-08 backend resolution ladder, 09 in-app installer runner, 10 local daemon attach / serve ensure / tickets, 11 remote connection, 12 boot orchestrator, 13 supervision / orphans / quit / power, 14 REST proxy in main — done
- Builder checks: pnpm lint / fmt:check / -r typecheck clean; check-ipc, gen-bridge-types --check ok; apps/desktop vitest
  355 passed / 10 skipped (35 files); realBackend.test.ts runs against a REAL daemon + `indus serve` in a temp home;
  tooling.checks ok (one similarity hit restructured). No Python.
- New: electron/backend/{resolver, process, control, localAttach, tickets, remote, remoteLiveness, supervisor, orphans,
  install}.ts (+tests, install.test via the real registry, realBackend.test), electron/installer/{runner, stageProtocol}.ts
  (+test), electron/boot/{orchestrator, progress}.ts (+test), electron/api/proxy.ts (+test), electron/quitGuard.ts (+test),
  electron/security/sensitiveFiles.ts (shared with P20-29), testing/fakeInstaller.mjs, testing/fakeIndus.ts, notes
  docs/notes/P20/{backend-resolver, installer-runner, local-attach, remote-connection, boot-orchestrator, supervision,
  rest-proxy}.md.
- Shared: electron/main.ts (installBackend before registry.seal(); dialog/powerMonitor imports; before-quit returns early
  when defaultPrevented), electron/ipc/channels.ts + schemas.ts (new event indus:backend:notice {kind restarted|crashing,
  …}; indus:connection:ticket also answers `protocols: string[]` — local ["indus-gateway-v1",
  "indus-gateway-ticket.<t>"], remote ["indus-dashboard-v1", "indus-dashboard-ticket.<reservation id>"]; indus:remote:
  test/save url is a plain string normalised in main), src/global.d.ts, testing/fakeBackend/{server.mjs (mint failures per
  home), ensure.mjs (+gateway restart)}.
- Contract for slice R: connection.get starts/joins the boot and resolves with the credential-free descriptor, failures as
  codes boot-local / boot-installer / boot-remote / boot-reauth / boot-reset; connection.ticket and api.request answer
  not-connected until ready; api.request also bad-request / forbidden / too-large / timeout / unreachable; BootProgress
  running=false only on error or while the first-run choice waits; GUI sessions send surface: "gui".
- Bugs the real-runtime test caught (fixed): `indus --version` prints "Indus Agent 0.0.1"; a running serve from an earlier
  launch answering 401 to this launch's token is stopped once and re-ensured.
- Decisions (number at integration): remote WS token mode only in 1.0 — main swaps the reservation id for the real token in
  Sec-WebSocket-Protocol on that one upgrade (token never crosses the bridge; works in unit tests and against the real serve
  from Node, NOT yet verified inside Electron — P20-40 must cover it); stale daemon restarted (gateway restart + ensure) only
  on stale_socket or version skew, others only re-ensure; Retry clears only the start latch; every stage exit without a
  result frame is retried (3×, 500 ms); managed install <home>/indus-agent/.venv, rung 5 POSIX only; orphan ledger records
  installer stage hosts only; proxy refuses /api/files/download-ticket and /api/auth/ws-ticket;
  INDUS_DESKTOP_SKIP_QUIT_PROMPT=1 for P20-40.
- OWNER DECISION: installer script default source guessed as
  https://github.com/indusagi/indus-agent/releases/download/v<ver>/install.sh (override INDUS_DESKTOP_RELEASE_BASE; dev
  override INDUS_DESKTOP_INSTALL_SCRIPT) — confirm the release org/repo (repo today: varunisrani/Indus-agent).
- Follow-ups: update gate stub until P20-37; control-socket client cannot check the listener uid (no SO_PEERCRED in Node);
  Windows PowerShell installer discovery waits for P23.

## w34-r.patch: P20-15 renderer entry / routes / shell, 16 boot overlay + first-run chooser + failure overlay, 17 gateway connection store, 18 transcript — done
- Builder checks: pnpm lint / fmt:check / -r typecheck clean; check-ipc, gen-bridge-types --check ok; apps/desktop vitest
  375 passed (33 files; real-window Electron tests ran with the cached zip unpacked into node_modules); packages/client 93
  passed; apps/tui 757 passed (uses the changed WS transport); tooling.checks ok; manual Electron 44.5.1 smoke from file://:
  shell renders, eval blocked, Shiki/KaTeX/Mermaid render under the strict CSP.
- New: apps/desktop/{index.html, vite.config.ts (calls injectCspMeta in dev and build — D1 follow-up fixed; no
  React-refresh plugin), src/main.tsx, src/styles.css, src/test/setup.ts}, src/app/{routes, Shell, App, pages, theme,
  Loader, bridge, ErrorBoundary}, src/i18n/{index.tsx, en.ts}, src/boot/{BootOverlay, FirstRunChooser, bootRetry,
  bootState}, src/gateway/{connectionStore, replay, serverRequests, timing, react, testSupport}, src/chat/{Transcript,
  ToolRow, Timeline, Markdown, RichBlocks, ErrorCard, ChatView, messageStream, transcriptStore, failure, toolSummary,
  markdown, transcriptReact}, tests, testing/fixtures/streams/ (7), testing/{oracle (shared with P20-40), seeded}.ts, notes
  docs/notes/P20/{renderer-shell, boot-overlay, connection-store, transcript}.md ("Contract for slice C" sections).
- Shared: apps/desktop/{package.json, tsconfig.json, vitest.config.ts} (C edits the same three), root biome.json (Tailwind
  directives for apps/desktop/src/**/*.css), pnpm-lock.yaml (re-lock), packages/client (DESKTOP_BACKOFF; answerTimeoutMs
  option — answers wait 330 s; ws-transport accepts the indus-dashboard-v1 / indus-dashboard-ticket. family and requires the
  server to echo the first offered protocol; exports; tests).
- New packages: shiki 4.5.0, katex 0.19.0, mermaid 11.17.2, lucide-react 1.52.0 (+ already-locked react/react-dom 19.3.0,
  react-router 8.4.0, @radix-ui/react-dialog 1.1.23, tailwindcss + @tailwindcss/vite 4.3.3, testing-library, @types/react,
  axe-core 4.13.0, jsdom 29.1.1, @indus/ui) — integrator verifies licences (mermaid's transitive tree!) and publish ages.
- MERGE with w34-c: drop C's four STAND-IN files (gateway/connectionStore.ts, gateway/serverRequests.ts, i18n/index.ts,
  i18n/en.ts) and use R's; R added aliases for C's names (GatewayConnection.mode/.codeVersion/.reconnect(),
  useGatewayConnection = usePrimaryConnection, useOpenServerRequests(sessionId?), `t` beside useT/translate);
  ServerRequestStore methods are list(sessionId?), get, subscribe, answer(id, answer) → boolean, decline(id) → boolean,
  isAnswered — C called `handlers()` and `OpenServerRequest.answer(a)`: reconcile; R's en.ts is one flat record — add
  `...CHAT_STRINGS` from src/chat/strings.en.ts. Slots: ChatView {composer, cards}, Transcript {footer, onFailureAction}
  (Retry is C's), Shell/AppRoutes {statusBar}; transcript store API addLocalUser / bindLocalUser / dropLocalUser;
  SessionTranscript {running, turnId, generation}.
- MERGE with w34-m: connection.ticket read as {wsUrl, protocols, expiresInMs} (store casts the bridge until M's table lands
  — drop the cast); boot-* rejection → store `failed`, redial when the overlay reports ready; the overlay looks for
  indusDesktop.backend.onNotice or indusDesktop.boot.onBackendNotice — point it at M's `indus:backend:notice` row
  (boot.onNotice); surface: "gui" goes on whoever creates sessions (C's composer session.create).
- Decisions (number at integration): file:// CSP meta connect-src adds loopback ws:/http: any port + wss:/https: (main's
  header, where it applies, narrows to exact origins); replay via session.subscribe from the cursor, watchdog via
  events.since, "cap 256" = at most 256 replay cursors; answer timeout 330 s (reference 300 s), liveness probe 5 s;
  reasoning hidden by default until P20-32 reads display.show_reasoning; no OFL font vendored yet; packages/ui has no React
  primitives.
- Env notes: electron's install.js fails behind the proxy (undici assertion) — unzip $ELECTRON_CACHE's
  electron-v44.5.1-linux-x64.zip into node_modules/electron/dist and write path.txt; relative TMPDIR breaks fakeBackend
  tests (use absolute, e.g. $S/rt). Dev: `pnpm --filter @indus/desktop dev` on 127.0.0.1:5174 +
  INDUS_DESKTOP_DEV_SERVER=http://127.0.0.1:5174.
