# Task: IPC registry with sender checks (P20-03) Phase: 20 Owner: wave-33 builder D1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/electron/preload.ts`
(channel list = API surface, skimmed for groups), `apps/desktop/electron/hardening.ts` (the few
existing sender checks). Research 08 §3.8 (sender checks: only a few handlers check), §2 (channel
groups); D-16.18. Closed before writing.
Evidence: none.

What it does (own words): every message the renderer can send to main arrives through one
registry. A channel is declared once in a table (`electron/ipc/channels.ts`: name, kind, bridge
group and method) with its payload and answer schemas (`electron/ipc/schemas.ts`, zod). A handler
is registered with `registerIpc(channel, schema, handler)` (or the typed `handle(name, handler)`
that takes the schema from the table). Before the handler runs, the sender is checked: the frame
that sent the message must be its web contents' main frame, its URL must be on the app origin (the
dev server origin, or the packaged `file://` index path exactly), and the web contents must belong
to a window the window manager created — so an iframe, a webview guest, a devtools page or a window
nobody registered is refused. Then the payload is parsed by the channel's schema; a malformed
payload is refused. A refusal answers a fixed error (`{ ok: false, code: "sender-rejected" }` or
`"bad-request"`) and logs once per channel and reason. Answers travel as an envelope
`{ ok: true, value } | { ok: false, code, message }`, which the preload unwraps, so expected
failures never become Electron "error in handler" stack logs.

A static scan (`scripts/check-ipc.mjs`) fails CI when any file under `electron/` (tests excluded)
calls `ipcMain.handle/on/once/handleOnce/addListener/…` or `webContents.ipc.*` outside
`ipc/registry.ts`, or registers a channel name not starting with `indus:`.

Inputs → outputs:
- `createIpcRegistry({ ipc, isKnownSender, appOrigin, log })` → `{ registerIpc, handle, on,
  sync, seal, registered }`.
- `checkSender(event, { appOrigin, isKnownWebContents })` → `null` (ok) or a reason
  (`no-frame`, `not-main-frame`, `foreign-origin`, `unknown-window`, `guest`).
- channel kinds: `invoke` (request/answer), `send` (fire and forget, renderer → main), `sync`
  (`sendSync`, launch facts only), `event` (main → renderer pushes; `broadcast`/`sendTo` helpers
  validate the payload against its schema before sending).
- `seal()`: every declared `invoke`/`sync` channel still without a handler gets one that answers
  `{ ok: false, code: "not-available" }`, so a renderer slice can run before its main slice lands.

Files: `apps/desktop/electron/ipc/{channels,schemas,registry,senderCheck,envelope}.ts`,
`apps/desktop/electron/ipc/registry.test.ts`, `apps/desktop/scripts/check-ipc.mjs`,
`apps/desktop/scripts/check-ipc.test.ts`.

Edge cases / failure modes:
- `senderFrame` null (frame navigated away or destroyed before the message is processed) → refused;
- a subframe on the app origin (iframe of our own page) → refused: only main frames talk to main;
- a webview guest (`getType() === "webview"`) even on an allowed URL → refused;
- `file://` sender with another path (a downloaded HTML file opened in the window) → refused;
- dev origin with another port or scheme → refused;
- duplicate registration for one channel → throws at startup (a programming error);
- channel not in the table or not `indus:` → throws at registration;
- handler throws `IpcError(code, message)` → that code; anything else → `internal` (logged with the
  channel name only, never the payload);
- `sync` sender refused → `event.returnValue = { ok: false, code: "sender-rejected" }`, never
  undefined (an unset returnValue blocks the renderer).

Our design differences / decisions:
- Every handler is sender-checked (D-16.18) — the reference checks only a few.
- Envelope answers for every channel (the reference special-cases one expected 404).
- `seal()`'s "not-available" answer for declared channels without a handler yet.

Tests we will write:
- foreign senders (iframe frame, webview guest, unknown window, null frame, other file path, other
  dev port) rejected for a sample of 10 channels; the handler never runs; logged once.
- malformed payloads rejected; well-formed pass with the parsed (defaulted) value.
- envelope for success, `IpcError`, unexpected throw; `send` and `sync` kinds; `seal()`.
- non-`indus:` and duplicate registrations throw.
- static scan: clean tree passes; a seeded raw `ipcMain.handle` (also `ipcMain.on`, `.ipc.handle`)
  fails with the file and line.

## Decisions for the lead
- One table for names (`channels.ts`, plain data, imported by the preload) and one for schemas
  (`schemas.ts`, zod, main and the type generator only), so the sandboxed preload bundle carries no
  zod. A compile-time check ties the two tables together.

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] Foreign senders (same-origin iframe, foreign iframe, webview guest — also on an app URL — unknown window, other file, gone frame, destroyed contents) rejected on 12 sampled channels, handler never runs, logged once (test: `electron/ipc/registry.test.ts::sender checks`); real window: a window the manager did not create gets `sender-rejected` on every call (test: `realWindow…::a window the manager did not create…`).
- [x] Malformed payloads (wrong type, extra key, full URL path, bad enum, missing, empty profile, wrong arity) → `bad-request` (test: `payload validation`).
- [x] Static scan fails on seeded raw `ipcMain.handle/on/once`, destructuring, index access, `webContents.ipc.handle`, aliasing; comments/tests/registry exempt (test: `scripts/check-ipc.test.ts`); `node scripts/check-ipc.mjs` clean on the tree.
- [x] Table/schema parity (compile-time `satisfies` + test `every channel … has schemas of its kind`).
- [x] Compared with the reference's few checks (main frame + app origin on three handlers): ours adds the known-window and guest checks and covers every handler.
- Deliberate differences: envelope answers everywhere; `seal()` "not-available" for declared channels without a handler; payload schemas strict.

## Status

Done (2026-10-21).
