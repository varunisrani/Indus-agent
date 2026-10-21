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
