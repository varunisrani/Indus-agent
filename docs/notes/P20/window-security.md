# Task: Window security baseline (P20-02) Phase: 20 Owner: wave-33 builder D1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/electron/session-windows.ts`
(all), `apps/desktop/electron/window-open-policy.ts` (all), `apps/desktop/electron/hardening.ts`
(navigation, opener and file-permission parts), `apps/desktop/ENGINEERING.md` ("Guest content never
opens anything by itself"). Research 08 §3.12, §3.28, traps R08.8, R08.20; D-16.20. Closed before
writing.
Evidence: none.

What it does (own words): every window the app makes comes from one factory, so no window can
drift from the security baseline: context isolation, the renderer sandbox, no Node, web security
on, the `<webview>` tag only where a chat window needs the preview pane, devtools only in dev
builds, autoplay allowed (voice later), and no focus-on-navigation for chat windows (a streaming
route change must not raise a blurred window, R08.20). Each window's contents may only show the app
itself — the dev server origin in dev, the packaged `file://` index when packaged; any other
navigation or redirect is cancelled. `window.open` and `target=_blank` are denied for every frame of
every web contents (app frames, iframes, webview guests) and the handler does nothing else: it
opens no URL and creates no window (R08.8). Opening a link in the OS browser is one audited path:
http/https only, no credentials in the URL, and only right after a real user input on that web
contents, observed by main (a synthetic `click()` from page script produces no input event).
Permission and permission-check requests are denied unless the origin is the app's own and the
permission is on a short list. Background events never focus a window: focus goes only through the
window manager's explicit user-action path.

Inputs → outputs:
- `webPreferencesFor(kind, { preloadPath, dev })` → the exact preference object for a kind
  (`main`, `secondary`, `instance`, `hud`, `quick`; `hidden` for helper windows without preload).
- `appOrigin` (`{ kind: "dev", origin }` or `{ kind: "file", indexPath }`) + URL → allowed or not.
- `hardenWebContents(contents, { appOrigin, log })`: `will-navigate`, `will-redirect`,
  `will-frame-navigate` (main frame) cancelled off-app; `setWindowOpenHandler` → `{ action: "deny" }`
  with only a log line (origin only); `will-attach-webview` strips any preload except the preview
  partition's guest preload, forces `nodeIntegration` off, `contextIsolation` and `sandbox` on, and
  drops `allowpopups`; guest web contents get the same deny-all window-open handler.
- `ExternalOpener.open({ url, webContentsId })` → `{ ok: true }` or `{ ok: false, reason }`
  (`scheme`, `invalid`, `credentials`, `too-long`, `no-user-activation`, `failed`).
- `permissionPolicy({ appOrigin })` → request/check handlers for a session.

Files: `apps/desktop/electron/windows/factory.ts`, `apps/desktop/electron/security/{navigation,
externalOpen,permissions}.ts`, tests `security/navigation.test.ts`, `security/externalOpen.test.ts`,
`security/permissions.test.ts`, `windows/factory.test.ts`; real-window test
`security/realWindow.electron.test.ts` (with `testing/electron/` harness).

Edge cases / failure modes:
- window.open from the top frame, an iframe, a sandboxed iframe without `allow-popups`, a webview
  guest → deny; the opener spy never called; the observer throwing does not change the decision;
- URLs: `javascript:`, `file:`, `indus:`, `data:`, `blob:`, `mailto:`, a URL with user-info,
  >8 KiB, unparseable → refused; `https://x` from a trusted click → opened once;
- activation: one input event allows one open within 2,000 ms; a second open without new input is
  refused; keyboard Enter counts, mouse move does not;
- navigation: `https://x`, `file:///etc/passwd`, another `file://` path, the dev origin with another
  port → cancelled; in-page hash changes are not navigations (no event);
- `devTools` false in packaged builds even if a window asks; DevTools shortcut not installed.

Our design differences / decisions:
- The reference delegates hub-iframe links to the opener from inside the window-open handler; we
  have no hub iframe in 1.0, so the handler has **no** delegation at all.
- "Trusted click relayed by main" is decided by main itself: main records real input events per web
  contents (`input-event`), and the opener consumes that activation. A renderer cannot forge it.
  P20-28's guest preload forwards the click to the host, and the host's request names the guest's
  web contents, whose own input main has seen.
- Permissions: the app origin may use `clipboard-sanitized-write`, `notifications`, `fullscreen`,
  and audio-only `media`; everything else (and every other origin, the preview partition included)
  is denied. P20-28/P24 widen this per D-id.

Tests we will write:
- factory prefs snapshot per kind (dev and packaged); devtools off when packaged.
- navigation: allowed/cancelled table for dev and file origins; redirect and frame navigation.
- window-open from each frame kind → deny, opener not called, observer failure harmless.
- opener: scheme table, credentials, length, activation window and one-shot consumption, failure.
- permissions: app origin short list, audio-only media, other origins denied, check handler.
- Electron real window (skipped when no binary/display): a hostile guest page's window.open is
  denied and nothing is opened.

## Decisions for the lead
- Activation window 2,000 ms, one open per input (`USER_ACTIVATION_WINDOW_MS`).
- Helper/hidden windows (link-title fetcher, P20-36) use kind `hidden`: no preload, no webview,
  no devtools.

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] window.open from top frame, iframe and webview guest → deny, no opener call, observer failure harmless (test: `electron/security/navigation.test.ts::window.open: deny from every frame…`); real window: all hostile guest pages and the sandboxed iframe open nothing, 6 windows created = the test's own (test: `electron/security/realWindow.electron.test.ts`).
- [x] Opener refuses `javascript:`, `file:`, `indus:`, `data:`, `blob:`, `mailto:`, credentials, >8 KiB (test: `externalOpen.test.ts`); real window: refused without input, allowed once after a real `sendInputEvent` click, refused again (test: `realWindow…::external open`).
- [x] Navigation to `https://x`, other files, other dev ports cancelled (test: `navigation.test.ts::navigation confinement`, real window: `the app window cannot navigate away`).
- [x] Factory prefs snapshot per kind, security floor, devtools dev-only (test: `windows/factory.test.ts`).
- [x] Permissions deny by default, audio-only media (test: `permissions.test.ts`).
- [x] Gap check vs `session-windows.ts`: the reference keeps devtools on in packaged builds — ours dev-only (the row); background throttling not pinned (same reasoning); autoplay and focus-on-navigation as the row says.
- [x] Gap check vs `window-open-policy.ts`: the reference delegates hub-origin links from inside the handler; we have no hub frame, so no delegation at all (D-16.20).
- Deliberate differences: activation is observed by main (`input-event`), so a renderer cannot claim a click; `will-attach-webview` refuses every partition but the preview one and every attach until the guest preload exists (P20-28 passes its path).
- Not taken: `mailto:` in the opener (row: http/https only); YouTube loopback host (no embeds in 1.0).

## Status

Done (2026-10-21). The preview guest preload path is `null` until P20-28; `isGuestOf` in `installExternalOpenIpc` answers false until P20-28 wires guest ownership.
