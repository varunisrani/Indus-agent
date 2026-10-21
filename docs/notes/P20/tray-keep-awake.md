# Task: Tray, keep awake, notifications (P20-36) Phase: 20 Owner: wave-35 builder D4

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/electron/minimize-to-tray.ts`
(all), `apps/desktop/electron/main.ts` (translucency / keep-awake 17,018–17,260), and, for the rules
those lines call into: `apps/desktop/electron/keep-awake-controller.ts`, `power-save.ts`,
`tray-host.ts`, `link-title-window.ts`, `notification-actions.ts`, `notification-linux.ts` (first
200 lines: connection, capabilities, actions). Research 08 §3.21. Closed before writing.
Evidence: none.

What it does (own words):
- **Minimise to tray** (off by default, a per-device choice in `ambient.json`). When on and a tray
  can really be shown, minimising an app window (main, secondary, instance — never the HUD or quick
  window) hides it instead, and closing the main window hides it instead of closing; the tray's
  "Show Indus" (or a click) brings every hidden window back, "Quit Indus" goes through the normal
  quit path, so the quit guard still asks (P20-13). While quitting, nothing is hidden. On Windows a
  hidden window also leaves the taskbar; on macOS the Dock icon hides only when no app window is
  visible. On Linux a tray object is no proof that anybody shows it: the option is available only
  when a StatusNotifier host is registered (asked over D-Bus), and if that host goes away hidden
  windows are restored and the option reads unavailable. A window shown by anything else (a deep
  link, a notification click, a second launch) stops counting as hidden.
- **Keep awake**: never / while working (default) / always. Main owns one power-save blocker
  (`prevent-app-suspension`, the display may still sleep): "always" holds it; "while working" holds
  it exactly while some window reports a running turn (`indus:work:active`, P20-13's report) and
  releases it when the last one ends — one start per working period, no churn on repeated reports.
  If the machine went to sleep after ≥ 55 s without input while a turn ran and keep awake was off,
  the next wake offers "keep awake while working" once per device (the main window shows the
  offer; accepting sets the mode).
- **Notifications** (`indus:notify`): a title, body, a tag (the same tag replaces the previous
  notification) and up to four action buttons. A click raises the window that asked (a user
  action) and tells it which notification and action (`indus:notification:action`). macOS shows
  actions natively; Linux sends them over D-Bus to the desktop's notification service when it
  advertises the `actions` capability and listens for its answer; elsewhere a click only. Off in
  settings → nothing shown. **Completion sounds** are our own: a short two-tone chime synthesised
  by the renderer (Web Audio), played when a notification for a finished turn or a waiting
  approval is shown and sounds are on.
- **Link titles and favicons** (`indus:links:preview {url}`): main fetches http/https pages only
  (no credentials in the URL), first a plain capped GET of the HTML for `<title>` and the icon link,
  then — only when that finds no title — a hidden, hardened window: no preload, sandbox, isolation,
  an in-memory partition, audio muted, every popup/download/permission refused, off-screen and
  unfocusable, destroyed after at most 8 s. Favicons come back as small data URLs (≤ 64 KiB,
  images only); answers are cached.

Inputs → outputs:
- `ambient.get` / `ambient.set({ minimizeToTray?, keepAwake?, notifications?, sounds? })` →
  `{ tray: { enabled, available, reason }, keepAwake, notifications, sounds }`, broadcast as
  `indus:ambient:changed`.
- window `minimize` / main `close` → hidden (or not); tray Show / host lost → restored.
- work reports + power `suspend`/`resume` + system idle seconds → blocker start/stop, one offer
  (`indus:keep-awake:offer`).
- `notifications.show({ title, body?, tag?, actions? })` → `{ shown }`; clicks → raise + event.
- `links.preview({ url })` → `{ title, favicon }` (nulls when unknown).

Files: `apps/desktop/electron/ambient/{tray,keepAwake,notifications,dbus,trayIcon,linkPreview,
settings}.ts`, tests `ambient/{tray,keepAwake,notifications,dbus,linkPreview,settings}.test.ts`,
`apps/desktop/src/ambient/{sound,turnNotices}.ts` (+ test). Shared: `electron/quitGuard.ts`
(a change listener so keep awake follows the report without polling), channel/schema rows.

Edge cases / failure modes:
- Linux: no `gdbus`, no session bus, no watcher, host not registered, the call hangs → tray
  unavailable (reason given), windows never hidden; the host disappears later → hidden windows
  come back and the option reads unavailable;
- tray creation throws (no icon support) → unavailable, nothing hidden;
- turning the option off restores hidden windows; a stale minimise (window already restored by
  the time the deferred hide runs) is not hidden;
- quit started (and not cancelled by the quit guard) → windows close normally;
- keep awake: mode switch while working (always → while-working keeps the blocker, → never
  releases); blocker stopped by the OS behind our back → restarted on the next reconcile; garbage
  in `ambient.json` → defaults;
- offer: only after a timed sleep (idle ≥ 55 s), only while working, only with keep awake off,
  only once per device, only when a main window can show it (else kept for the next wake);
- notifications: more than 4 actions refused by the schema; same tag twice → the first is closed;
  D-Bus delivery failure → falls back to a plain notification; a click on a notification whose
  window has gone → the main window;
- link previews: `file:`, `javascript:`, user-info URLs, > 2 KiB → refused; non-HTML or huge
  bodies → capped read; the hidden window never shows, never opens anything, is always destroyed.

Our design differences / decisions:
- D-Bus through the system's `gdbus` tool (spawned without a shell, bounded by a timeout) instead
  of a D-Bus client library: no new package, and a desktop without it simply has no tray hiding and
  plain notifications.
- The tray glyph is drawn by code (a bitmap built at runtime), so no image file is needed until
  P20-38 ships icons; a packaged icon path, when given, wins.
- Completion sounds are synthesised, not shipped as files (our own by construction).
- Not taken: translucency/glass, Windows toast XML actions, a "curl" binary for titles (we use
  main's own fetch).

Tests we will write:
- tray: no StatusNotifier host → unavailable and minimise does not hide; host present → minimise
  hides (deferred), Show restores all, close of main hides, quitting → no hide, host lost →
  restored; option off → restored; Windows skip-taskbar; tray creation failure.
- keepAwake: blocker started once per working period and released; always/never; mode change;
  offer once after a ≥ 55 s idle sleep mid-turn while off; not offered when on, when idle < 55 s or
  when not working; persisted `offered`.
- notifications: tag replaces; click raises the asking window and emits the action; Linux D-Bus
  path with actions, fallback when gdbus fails; disabled → not shown.
- dbus: GVariant quoting, parsing of `(<true>,)` and `(uint32 7,)`, `ActionInvoked` monitor lines.
- linkPreview: refused URLs, title from HTML, entity decoding, favicon data URL with cap, hidden
  window fallback options (no preload, sandbox) and destruction on timeout (fake window).
- settings: sanitising and persistence.

## Decisions for the lead
- D-Bus via `gdbus` (glib) instead of a library; watcher poll every 10 s while the option is on.
- Settings file `ambient.json` in userData holds minimise-to-tray, keep-awake mode, the one-time
  offer flag, notifications on/off and sounds on/off (defaults: off, while-working, not offered,
  on, on).
- Link previews: first a capped GET (512 KiB, 6 s), then a hidden window (8 s), cache 200 entries
  for 30 minutes, at most 2 hidden windows at once.
- Shared file touched: `electron/quitGuard.ts` (`subscribe` — keep awake follows the work report
  without polling). The renderer now reports running turns (`work.report`) from
  `src/ambient/AmbientHost.tsx`; before this nothing called it.
- Wiring for other slices: Settings (P20-32) reads/writes `indusDesktop.ambient.get/set`
  (`minimizeToTray`, `keepAwake`, `notifications`, `sounds`) and listens to `ambient.onChanged`;
  any surface may ask for a link title/favicon with `indusDesktop.links.preview({ url })`.

Compare checklist (filled after the rewrite; reopened `minimize-to-tray.ts`, `tray-host.ts`,
`keep-awake-controller.ts`, `power-save.ts`, `notification-linux.ts`, `link-title-window.ts` and
`main.ts` 17,018–17,260):
- [x] No StatusNotifier host → option unavailable and windows not hidden (test:
  `electron/ambient/tray.test.ts::Linux without a StatusNotifier host: …`); host lost → restored
  (test: `tray.test.ts::the host going away restores hidden windows …`).
- [x] Hide after the native minimise, never a window restored meanwhile; close of main hides;
  Quit goes through `app.quit` (quit guard); quitting stops hiding (tests: `tray.test.ts`).
- [x] Windows: skip-taskbar + blur on hide, real activation on restore; Linux keeps a parked tray
  (tests: `tray.test.ts::Windows: …`, `::turning it off restores windows …`).
- [x] Gap: a window un-hidden by something else (deep link, notification, second launch) must stop
  counting and, on Windows, be activated after the native restore — added (test:
  `tray.test.ts::a hidden window shown by something else …`).
- [x] Blocker started once per working period and released; offer shown once after a ≥ 55 s idle
  sleep mid-turn with keep awake off (tests: `electron/ambient/keepAwake.test.ts`).
- [x] Notifications: same tag replaces, click raises the asking window, macOS actions, Linux D-Bus
  actions with the `default` click, fallback without the service (tests:
  `electron/ambient/notifications.test.ts`).
- [x] Hidden link window: no preload, muted, popups denied, downloads cancelled (partition hook in
  `main.ts`), off-screen, unfocusable, destroyed on finish or after 8 s (tests:
  `electron/ambient/linkPreview.test.ts`).
- Deliberate differences: D-Bus through `gdbus` instead of a D-Bus library (no new package); the
  tray glyph is drawn in code; completion sounds are synthesised; link previews refuse loopback and
  private-network hosts (the reference does not), so a model-written link cannot make main poke a
  local service; the plain fetch replaces the reference's `curl` tier.
- Not taken: translucency/glass, Windows toast-XML actions, the notification delivery cap and
  retry cool-down of the reference's own D-Bus client (each `gdbus` call is bounded by 2 s instead).

## Status

Done (2026-10-21). Real tray/notification behaviour needs a desktop session: covered by fakes here
(no Electron binary, no session bus); P20-40 should check the tray option on a Linux runner with
and without a StatusNotifier host.

Integration (wave 35, D-16.721): with main's preferences store passed in, tray, keep-awake, notification and sound settings and the global shortcuts are read from and written to `desktop-prefs.json`; `ambient.json` keeps only the offer flag; the renderer gates finished-turn and approval notices per kind.
