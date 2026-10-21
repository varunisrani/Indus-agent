# Task: Diagnostics and small surfaces (P20-39) Phase: 20 Owner: wave-35 builder D5

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/electron/main.ts`
(6,485–6,970: application menu, devtools shortcut, preview/zoom shortcuts, context-menu bridge,
download handling, media permissions; plus a name-only look at the find-in-page wiring
12,360–12,395 and 17,300–17,360 to see where the Linux shortcut is claimed). Research 08 §3.8
(find-in-page, context menu & spellcheck, clipboard rows). Closed before writing.
Evidence: none.

What it does (own words):
- **Find in page.** Ctrl/Cmd+F opens a small find bar over the chat. It searches the text the
  transcript has rendered (not the composer, not hidden screen-reader labels), highlights every match
  with the CSS Custom Highlight API (no DOM mutation, so streaming React rows are untouched), marks the
  current one, scrolls it into view and shows "3 of 12" / "No results". Enter / Shift+Enter step,
  Esc closes and clears. New rows streaming in re-run the search (debounced). On Linux some desktops
  swallow Ctrl+F before the page sees it, so main claims it in `before-input-event` and tells that
  window to open the bar (`indus:find:open`); macOS/Windows keep the renderer binding.
- **Context menu.** Right-click in any web contents builds a native menu from what Chromium reports:
  spelling suggestions (up to five) + "Add to Dictionary" for a misspelled word in an editable
  field; Cut / Copy / Paste / Select All per the edit flags; "Copy Link" (http, https, mailto only —
  copied as text, never opened) and "Copy Image"; "Inspect Element" in dev builds only.
- **Clipboard bridge.** `clipboard.readText()` / `clipboard.writeText(text)` through main (the
  renderer's own clipboard permission is denied by policy). Text only, capped at 1 MiB.
- **Logs.** `logs.recent({limit})` returns the redacted lines main kept in memory; `logs.reveal()`
  shows `desktop.log` in the file manager (main picks the path, the renderer passes none);
  `logs.rendererError({message, stack?, source?})` writes renderer errors to `desktop.log` at ERROR
  (stack cut to 20 lines, rate-limited per window so a render loop cannot flood the file). The
  renderer forwards window `error` and `unhandledrejection` events and the root error boundary.
- **Copy diagnostics.** A "Copy Diagnostics" entry (Help menu, and `diagnostics.copy()` for the
  renderer's command surfaces) assembles a plain-text bundle — app and runtime versions, platform,
  build stamp, paths with the home folder shown as `~`, which `INDUS_*` variables are set (names
  only), window count, recent renderer errors and the recent log — passes the whole text through the
  secret redactor (patterns plus the literal secret values main holds) and puts it on the clipboard.
  The bundle never crosses the bridge; the renderer gets only `{copied, bytes}`.

Inputs → outputs: keys, right-clicks, bridge calls → highlights + count, native menu actions,
clipboard text, `desktop.log` lines, a redacted diagnostics text on the clipboard.

Files: `apps/desktop/electron/diagnostics.ts`, `apps/desktop/electron/diagnostics.test.ts`,
`apps/desktop/electron/contextMenu.ts`, `apps/desktop/electron/contextMenu.test.ts`,
`apps/desktop/src/app/FindBar.tsx`, `apps/desktop/src/app/findInTranscript.ts`,
`apps/desktop/src/app/FindBar.test.tsx`, `apps/desktop/src/app/errorForwarding.ts` + test,
`apps/desktop/src/app/LogsViewer.tsx` + test, `apps/desktop/src/app/clipboard.ts`.
Shared: `electron/ipc/channels.ts` + `schemas.ts` (clipboard, diagnostics, find rows),
`src/global.d.ts` (regenerated), `electron/appMenu.ts` (optional Help items), `src/i18n/en.ts`
(find/logs keys), `src/styles.css` (highlight + bar styles), `src/main.tsx` (two mount lines).

Edge cases / failure modes:
- empty query → no highlights, no count; query found 0 times → "No results";
- matches inside hidden labels (`.sr-only`) or `[data-find-skip]` (the find bar itself) are ignored;
- a match across two text nodes (bold inside a word) is not found (one node at a time) — accepted;
- no `CSS.highlights` (jsdom, an old engine) → count and scroll still work, no paint;
- stack overflow loops → at most 20 renderer errors per window per minute, then one "suppressed"
  line;
- a renderer error carrying a token → redacted by the log like every line;
- clipboard text over 1 MiB → refused by the schema;
- link of `javascript:`, `file:` or `indus:` → no Copy Link item;
- diagnostics with a token in the log, an env value, a URL with user-info, an Authorization header,
  a WS ticket subprotocol or a value main minted → all masked (test).

Our design differences / decisions:
- Find runs in the renderer over the transcript DOM with the Highlight API instead of Chromium's
  `findInPage` (which also matches the find input's own text and moves focus). No IPC per keystroke.
- The context menu is native (main builds it from Chromium's params) — no renderer menu, no extra
  channel; there is no "Open Link" item anywhere (D-16.20).
- No downloads handler: the 1.0 renderer starts no downloads (later with artifacts, N16).
- The diagnostics bundle stays in main; the renderer only learns the byte count.

Tests we will write:
- diagnostics bundle contains no token pattern (vendor keys, bearer, JWT, ticket, URL user-info,
  minted literal, env values), home shown as `~`, sections present, size cap; copy writes the
  clipboard and returns bytes only; renderer error → ERROR line in the log with source, rate limit
  and suppression line; logs.recent/reveal handlers.
- context menu template: suggestions + add-to-dictionary, edit flags, link item only for safe
  schemes, inspect only in dev; Linux find shortcut claimed and emitted, other platforms untouched.
- find: matches over a fixture transcript (count, skips hidden/own UI), FindBar shows count, steps,
  closes and clears; highlight registry called with the ranges.
- error forwarding: error/unhandledrejection forwarded, duplicates dropped, cap.

## Decisions for the lead
- New channels: `indus:clipboard:read-text|write-text`, `indus:diagnostics:copy` (invoke) and
  `indus:find:open` (event), owner P20-39.
- "Command-menu entry": the renderer has no command menu yet; the entry is in the app menu (Help →
  Copy Diagnostics) and the bridge method is ready for the command menu when it lands. The logs
  viewer is a component (`LogsViewer`) for Settings → About/Advanced (P20-32 mounts it).
- Install lines for `main.ts` are in the report (the lead applies them).

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] Ctrl/Cmd+F claimed in main on Linux only, left to the renderer elsewhere (test: `electron/contextMenu.test.ts::Linux find shortcut…`).
- [x] Find runs against the requesting window only (each window has its own bar and DOM; nothing global) (`src/app/FindBar.test.tsx`).
- [x] Spelling suggestions come from Chromium's context-menu params and replace the word (`contextMenu.test.ts::offers spelling suggestions…`).
- [x] Edit menu roles incl. paste-and-match-style (existing `appMenu.ts`; Help items added, optional).
- [x] Diagnostics bundle contains no token pattern (`electron/diagnostics.test.ts::contains no token pattern…`, `::copy diagnostics puts the redacted bundle…`); renderer error appears in the log (`::a renderer error appears in desktop.log`); find bar highlights and count (`FindBar.test.tsx::highlights the count…`).
- Deliberate differences: the reference's context menu is drawn by the renderer with main forwarding spelling facts; ours is native, built in main (no channel, no renderer menu, never an "open link" item). Find uses the Highlight API over the transcript instead of `webContents.findInPage` (no self-match, no focus theft, no per-keystroke IPC). DevTools only in dev builds (the reference ships them in packaged builds) — "Inspect Element" follows that.
- Not taken: the downloads handler (no renderer-started downloads in 1.0), the F12 forwarding and preview reload/close shortcuts (P20-28/P20-35 surfaces), macOS Substitutions submenu, "Check for Updates" in Help (P20-37 adds it to `help`).

## Integration (for the lead)

- `main.ts` (diff in `$S/main-ts-wiring.patch`, d5 scratch): after `installBackend(…)` and before
  `registry.seal()` add `const diagnostics = installDiagnostics(context, { clipboard, revealPath: (file) => shell.showItemInFolder(file), facts: { arch: process.arch, versions: process.versions, locale: app.getLocale() }, homeDir: os.homedir(), env: process.env, resourcesPath: app.isPackaged ? process.resourcesPath : null }); // P20-39`
  and `installContextMenus({ dev, platform: process.platform, onWebContentsCreated: …, popup: …, writeText: (text) => clipboard.writeText(text), isAppWindow: (id) => windows.isKnownWebContents(id), emitFindOpen: (contents) => registry.emit(contents, "indus:find:open", {}) }); // P20-39`;
  pass `help: diagnostics.menuItems` to `appMenuTemplate`. Optional: `knownSecrets` (the serve token
  main minted) and `sections` (boot phase, connection kind) from `installBackend`'s state.
- Renderer already wired in `src/main.tsx` (error forwarding + find bar mount, two lines).
- P20-32 mounts `<LogsViewer />` in Settings → About/Advanced; a future command menu calls
  `indusDesktop.diagnostics.copy()`.
- New channels (table + schemas + regenerated `src/global.d.ts`): `indus:diagnostics:copy`,
  `indus:clipboard:read-text`, `indus:clipboard:write-text` (invoke), `indus:find:open` (event).

## Status

Done (2026-10-21). Shared files changed: `electron/ipc/channels.ts`, `electron/ipc/schemas.ts`,
`src/global.d.ts`, `electron/appMenu.ts`, `src/i18n/en.ts`, `src/styles.css`, `src/main.tsx`.
