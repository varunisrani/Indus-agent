# Task: Preview pane (P20-28) Phase: 20 Owner: wave-35 builder D2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/electron/preview-guest-preload.ts`
(all: the click hand-off rules), `apps/desktop/src/app/chat/right-rail/preview-pane.tsx` (security
bits: webview creation, partition, webpreferences, the guest hand-off listener, history sync, load
failures), `apps/desktop/src/app/chat/right-rail/preview-file.tsx` (live reload and the 512 KiB text
cap), and for the main-process side the parts of `apps/desktop/electron/main.ts` those files call
(preview file watch ~5,905–6,075, the guest escape hatch ~12,440–12,500, capture ~16,847),
`apps/desktop/electron/preview-file-watch.ts`, `apps/desktop/electron/preview-guest-escape.ts`,
`apps/desktop/electron/preview-capture.ts` (first 60 lines), `apps/desktop/electron/watch-storm-breaker.ts`
(first 60 lines), `apps/desktop/src/lib/preview-external.ts`. Closed before writing.
Evidence: none.

What it does (own words): the preview pane shows a web page (usually a local dev server the agent
started) or a local file. A page lives in a `<webview>` on the persistent partition
`persist:indus-preview` — a plain name with no character Electron would escape on Windows (R08.9).
The guest never gets `allowpopups`; main's attach guard (P20-02) forces our guest preload, Node off,
isolation and sandbox on. The guest preload exposes nothing to the page. It watches clicks before
the page does; when the person really pressed the left mouse button on a link that wants a new
window, it reports that link's resolved address to our page (`sendToHost`). A click made by page
script never counts as real, so it is ignored. The host pane then asks main to open it through `external.open({url, guestContentsId})`:
main checks that the guest belongs to the asking window and that the guest itself saw a real input
within 2 s, and opens only http/https (D-16.20, R08.8). `window.open` in the guest is denied by the
deny-all handler with no side effect.

Toolbar: back / forward / reload, an address field, capture to image (main's `capturePage` of that
guest → PNG data URL; refused for a guest of another window or an empty image), open in browser
(our own button: a real click in the app window), pop-out (shown when the pane host provides it).
File previews: text up to 512 KiB through `fs.readText` (a longer file shows a truncation notice),
images through `fs.readDataUrl` (preview cap), HTML files in the webview. Main watches the file's
folder (so editors that save by rename still count), filters by name and reports a change once per
120 ms burst; the pane re-reads (or reloads the guest). A watch dies with the window that asked.

Escape hatch: main sees every key in a guest before the page does (`before-input-event`). Escape
while the host window is fullscreen leaves fullscreen; Ctrl/Cmd+Shift+W asks the host to close the
preview (`indus:preview:close-requested`). The `indus://close-preview` link is P20-34's.

Inputs → outputs: target `{kind: "url", url}` or `{kind: "file", path}` → webview / text / image;
guest trusted click → `external.open`; capture → data URL; file change → re-read after 120 ms.

Files: `apps/desktop/electron/preview/guestPreload.ts` (+ test), `electron/preview/guestPreloadEntry.ts`
(the bundled entry), `apps/desktop/electron/preview/fileWatch.ts`
(+ test), `apps/desktop/electron/preview/guest.ts` (+ `guest.test.ts`: escape hatch, guest ownership,
capture, install), `apps/desktop/src/panes/preview/PreviewPane.tsx`, `apps/desktop/src/panes/preview/target.ts`,
`apps/desktop/src/panes/preview/PreviewPane.test.tsx`. Shared: `electron/main.ts` (install line,
guest preload path, `isGuestOf`), `scripts/build-electron.mjs` (third bundle `preview-guest.cjs`),
`electron/ipc/{channels,schemas}.ts` + regenerated `src/global.d.ts`.

Edge cases / failure modes:
- a synthetic click, a middle/right click, a click on a link without `target=_blank` → nothing;
- a `file:`, `javascript:`, `indus:` or credentialed URL from the guest → refused by main (and by
  the pane's own scheme check first);
- `window.open` from the guest → denied, nothing opened;
- capture of a guest that is gone or belongs to another window → `not-found` / refused;
- the watched file deleted → no event (a later re-create reports again); watch of a path outside the
  allowed roots or a sensitive file → refused; the owning window closes → the watch is released;
- a burst of saves → one change after 120 ms of quiet;
- a 600 KiB text file → first 512 KiB and a truncation notice;
- a page that swallows Escape in fullscreen → main's hook still leaves fullscreen.

Our design differences / decisions:
- The pane does not mount a webview for a remote connection's files: file previews read local paths
  only in local mode (remote mode shows the REST read of P20-29's files source).
- No console panel, devtools toggle, annotate mode or agent automation in 1.0 (N12 P3 parts later).
- The storm breaker (a burst of > 1,000 events in 1 s switches to a 5 s stat poll) is armed on every
  platform, not only Windows.

Tests we will write:
- guest preload: trusted primary click on `_blank` (and on a span inside it) forwards the absolute
  URL; synthetic click, other buttons, plain links → nothing; nothing exposed in the main world;
- main: guest input routing (Escape only while fullscreen, Ctrl/Cmd+Shift+W), `isGuestOf` by host
  and partition, capture refusals, external open of a guest's trusted click vs an untrusted one, a
  `file:` link refused;
- file watch: debounce 120 ms (fake clock), other names ignored, owner destroyed → closed, outside
  roots / sensitive refused, storm → poll;
- pane: webview attributes (partition, no allowpopups), guest hand-off → `external.open` with the
  guest id, `file:` hand-off refused, 600 KiB text shows truncation, live reload re-reads.

## Decisions for the lead
- Pop-out is a prop (`onPopOut`): a preview window kind does not exist yet (P20-35 / the pane shell
  decide how a pane moves to its own window); the button shows only when the host passes it.
- Ctrl/Cmd+Shift+W in a guest sends `indus:preview:close-requested` (the P20-34 event) to the host
  window from P20-28's key hook; P20-34 keeps the deep-link path.
- The storm breaker is armed on all platforms (threshold 1,000 events per second).
- The renderer keeps its own copies of `PREVIEW_PARTITION` and the guest's `sendToHost` channel name
  (renderer code never imports `electron/` modules); a test keeps them equal to main's.
- "Add to chat" for a captured image is a prop (`onAttachCapture`); the composer has no image
  hand-off API yet, so `d2Panes.tsx` does not pass it (lead wiring).

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] Trusted primary click on a `_blank` anchor forwards the resolved absolute URL; synthetic click
  dropped (test: `electron/preview/guestPreload.test.ts::forwards a trusted primary click on a _blank link (and on a child of it)`,
  `::drops synthetic, non-primary and plain-link clicks`).
- [x] Host admits http/https only, then opens through the audited opener (tests:
  `src/panes/preview/PreviewPane.test.tsx::a guest hand-off goes to external.open with the guest id`,
  `::a file: hand-off from the guest is refused before main`; main:
  `electron/preview/guest.test.ts::an untrusted guest click cannot open; a trusted one can, http/https only`).
- [x] Partition `persist:indus-preview`, no `allowpopups`, isolation/sandbox webpreferences (test:
  `PreviewPane.test.tsx::renders the webview on the preview partition without popups`).
- [x] Escape exits fullscreen only while fullscreen; Ctrl/Cmd+Shift+W closes (tests:
  `guest.test.ts::Escape leaves fullscreen only while fullscreen; Ctrl/Cmd+Shift+W closes`,
  `::the guest's key hook acts before the page: fullscreen left, close requested`).
- [x] Watch the folder, filter by name, 120 ms debounce, owner teardown, storm → poll (tests:
  `electron/preview/fileWatch.test.ts::watches the file's folder and reports one change per 120 ms burst`,
  `::dies with the window that asked, and only the owner may stop it`, `::an event storm switches the watch to a slow poll`;
  pane: `PreviewPane.test.tsx::live reload re-reads the file on main's (debounced) change event; unwatches on close`).
- [x] 512 KiB text cap with a truncation notice (test: `PreviewPane.test.tsx::a 600 KiB text file shows the truncation notice`).
- [x] Gap found: the reference's capture fails closed on an empty image — ours too
  (`guest.test.ts::capture refuses a guest of another window and an empty image`).
- [x] Gap found: a load failure of a sub-frame must not replace a healthy page — the pane ignores
  non-main-frame and aborted (-3) failures (`PreviewPane.test.tsx::a sub-frame load failure does not show the error; a main-frame one does`).
- Deliberate differences: no console panel, devtools, annotate or automation (N12 P3 later); the
  storm breaker runs on all platforms; the guest's ownership is checked in main for capture and
  external open (the reference trusts the renderer's guest id for capture).
- Not taken: the reference keeps one guest for every later URL target and steers it with
  `loadURL`; ours keeps the guest for address-bar navigation (`::the address bar refuses other schemes
  and navigates the same guest`) but builds a new one when a target arrives from outside the pane.
  Console panel, devtools, annotate and agent automation are later (N12 P3).

## Status

Done (2026-10-21). Shared: `electron/main.ts` (guest preload path, `did-attach-webview` adoption,
`isGuestOf` for the external opener, `installPanes` step), `electron/panesInstall.ts` (new, all four
panes' main side), `scripts/build-electron.mjs` (third bundle), channel schemas + `src/global.d.ts`.
Mounted through the stand-in pane registry (`src/panes/_stubs_d2.ts`, `src/panes/d2Panes.tsx`) until
D1's pane shell lands. Electron E2E of the hostile guest pages is P20-40's (no Electron binary here).
