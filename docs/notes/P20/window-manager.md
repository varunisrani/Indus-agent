# Task: Window manager (P20-06) Phase: 20 Owner: wave-33 builder D1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/electron/main.ts`
(`createWindow` 13,873–14,344; secondary/instance windows 12,545–12,900, skimmed; zoom 6,485–6,970),
`apps/desktop/electron/window-state.ts` (first 80 lines), `apps/desktop/electron/zoom.ts` (all),
`apps/desktop/electron/session-windows.ts` (URL builders, registry). Research 08 §3.12, trap R08.15.
Closed before writing.
Evidence: none.

What it does (own words): one manager owns every app window. The main window is frameless with a
34 px title-bar overlay (macOS traffic lights centred in the band; Windows/Linux paint native
controls through the overlay), starts hidden and is shown when the renderer reports its first
themed paint (`indus:window:painted`; a 4 s fallback shows it anyway so a broken renderer is never
invisible). Its bounds and maximized flag are saved in `<userData>/window-state.json` (debounced,
flushed on close) and restored with a minimum of 400×620 and at least 48 px of the window over some
display's work area, else centred on the primary display. Other kinds — secondary (one popped-out
session, `watch=1` for a spectator), instance (a peer window for another profile), HUD, quick — use
the same factory and URL builder: the kind and its options sit in the query before the hash
(`?win=secondary&watch=1&profile=work#/s/<id>`). Transparent kinds (HUD, quick) get a transparent
background at creation. The manager knows every web contents it created; the IPC sender check asks
it. Zoom is one whole-window scale per app (level 0 default, half-level steps, clamped to −4…9),
saved in `<userData>/zoom-state.json` and re-applied after show, restore, resize, move (display
change), every full load and every in-page navigation, with a 100 ms debounce and three 300 ms
settle checks (R08.15); self-sizing transparent windows are excluded. A renderer crash reloads the
window at most 3 times per 60,000 ms; the fourth stays on the crash (logged) — no reload loop.
Nothing in the manager focuses a window on its own: `raise(win, reason)` exists only for explicit
user actions (dock click, second launch, notification click, deep link).

Inputs → outputs:
- `sanitizeWindowState(raw)` / `placeWindow(state, displays, primary)` → bounds + maximized.
- `zoom`: `clampZoom`, `stepZoom(level, dir)`, `zoomPercent(level)`, `ZoomKeeper` (timers through
  an injected clock).
- `CrashReloadBudget(max 3, window 60,000 ms, clock)` → `allow()`.
- `WindowManager.openMain()`, `.openSecondary({ sessionId, watch, profile })` (one per session:
  a second open focuses it — a user action), `.openInstance({ profile })`, `.isKnownWebContents(id)`,
  `.raise(win, reason)`, `.windowUrl(kind, options)`.

Files: `apps/desktop/electron/windows/{manager,windowState,zoom,crashReload,windowUrl}.ts`, tests
`windows/windowState.test.ts`, `windows/zoom.test.ts`, `windows/crashReload.test.ts`,
`windows/windowUrl.test.ts`, `windows/manager.test.ts`.

Edge cases / failure modes:
- saved bounds off every display (monitor unplugged), NaN, strings, negative sizes → clamped or
  defaults; smaller than the minimum → raised to it;
- window-state file corrupt → defaults, overwritten on the next save;
- painted signal never arrives → shown after 4 s; arrives twice → shown once;
- zoom changed in the renderer by Chromium (per-origin memory after navigation) → re-asserted only
  when it drifts (no churn);
- crash burst: 3 reloads in 60 s, the 4th not reloaded; a crash after the window slides out of 60 s
  reloads again;
- window closed while a settle timer is pending → timer cancelled, nothing touches a destroyed
  window.

Our design differences / decisions:
- Reveal on an explicit renderer paint signal plus a fallback, not only `ready-to-show`.
- Zoom range −4…9 (the row gives only the max and the default); step 0.5.
- `raise()` takes a `UserActionReason`, so background code cannot call it by type.

Tests we will write:
- windowState: off-screen saved bounds clamped; tiny windows raised; maximized kept; garbage →
  defaults; round trip through the file (atomic write).
- zoom: clamp/steps/percent; re-applied after a simulated hash navigation (fake clock); settle
  checks; no re-apply when not drifted; excluded windows ignored.
- crashReload: 4th crash in 60 s does not reload (fake clock); window slides.
- windowUrl: query before the hash for each kind; profile and session encoded; dev and file bases.
- manager (mocked BrowserWindow): reveal once on paint or fallback; known web contents; state saved
  on close; secondary de-duplicated; no `focus()` without a user-action reason.

## Decisions for the lead
- Paint-signal fallback 4,000 ms (`REVEAL_FALLBACK_MS`).
- Zoom range −4…9, step 0.5 (`ZOOM_MIN`, `ZOOM_MAX`, `ZOOM_STEP`).

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] Off-screen saved bounds clamped/centred, sliver < 48 px not trusted, second display kept, minimum size, garbage → defaults, corrupt file (test: `windows/windowState.test.ts`).
- [x] 4th renderer crash in 60 s does not reload (fake clock), window slides, clean exit ignored (test: `windows/crashReload.test.ts`, `windows/manager.test.ts::renderer crashes…`).
- [x] Zoom re-applied after a simulated hash navigation (fake clock), settle checks, debounce, no churn, self-sizing excluded (test: `windows/zoom.test.ts`, `manager.test.ts::zoom is re-asserted…`).
- [x] Reveal once on paint or after the 4 s fallback, never focused by the manager; secondary de-duplicated with a user-action raise; geometry saved debounced and at close; maximized restored (test: `manager.test.ts`). Real smoke: the main window is shown on the renderer's paint signal and `window-state.json` is written at quit.
- [x] Gap: the reference re-asserts zoom on `focus` too (DPI changes on alt-tab) — added (drift-guarded, so free when nothing changed).
- [x] Gap check vs the reference's stale fullscreen-bounds repair: we save `getNormalBounds()`, which never reports fullscreen bounds as normal ones.
- Deliberate differences: zoom range −4…9 (reference −9…9) with 0.5 steps; settle checks on every platform (drift-guarded); reveal on an explicit paint signal.
- Not taken: connection-scoped window URLs (`connectionId`, N17), browser pop-out windows, HUD/quick behaviour (P20-35 uses the same factory and URL builder).

## Status

Done (2026-10-21).
