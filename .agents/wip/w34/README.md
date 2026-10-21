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
