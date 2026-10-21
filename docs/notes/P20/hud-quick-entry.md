# Task: HUD, quick entry and extra windows (P20-35) Phase: 20 Owner: wave-35 builder D4

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/electron/quick-entry.ts`
(all: accelerator grammar, settings sanitising, the one-registration controller, window bounds, the
submit relay), `apps/desktop/electron/main.ts` (HUD 13,151–13,670: state file, snap-to-pointer,
cursor feed, spawn/open/close/respawn; quick entry 13,672–13,870: window options, hide on blur,
show/toggle; and the quick-entry IPC handlers next to keep-awake around 17,160–17,260). Research 08
§3.21 and §3.12 (window kinds). Closed before writing.
Evidence: none.

What it does (own words):
- **HUD.** "Pop out" puts one conversation into a small frameless, transparent, always-on-top window
  that floats over other apps while the reply streams. It is opened for a session (the one the
  person was last looking at when the global toggle is pressed), shows that session's transcript
  tail and a composer, and the main window steps aside while it is up (it comes back when the HUD
  closes, without being focused). Pressing and holding on the HUD's grip moves it (main follows the
  pointer while the renderer holds the press); its edges resize it the same way; "Reset layout"
  puts it back at the default spot on the display under the pointer. Where it was and how big it
  was is saved (`hud-state.json` in userData) and reused only while at least 48 px of it is still
  on a connected display. Two global shortcuts: toggle (default `CommandOrControl+Shift+H`, always
  registered while enabled) and snap-to-pointer (default `CommandOrControl+Shift+G`, registered only
  while a HUD is open, so it does not hold a chord hostage the rest of the time). Opening the HUD
  for another session switches it in place (`indus:hud:changed` tells it); for another profile the
  window is replaced, because a renderer adopts its backend once.
- **Quick entry.** A global shortcut (default `CommandOrControl+Shift+Space`) toggles a small
  transparent always-on-top composer on the display under the pointer. It has no connection of its
  own: the text goes to main (`indus:quick:submit`), main hands it to the app window the person
  used last (`indus:quick:deliver`), that window submits it into its last-active session (a new
  chat when none) through the same send path the composer uses, and answers (`indus:quick:ack`).
  Main answers the quick window with the outcome: delivered (window hides, the receiving window
  is brought forward — a user action), empty, no app window, or "not confirmed yet" after 15 s
  (never a failure, never retried by itself: the prompt may still land; a late answer is logged).
  Losing focus or Esc hides it.
- **Accelerators.** One grammar for every global shortcut: modifiers then exactly one key; at
  least one modifier (a bare key would be stolen from every app), never Esc (it dismisses our own
  windows); spelling is normalised so a saved value round-trips. Conflicts are reported, never
  thrown: two of our actions on one chord (`conflict`, naming the other action), a chord the OS or
  another app holds (`taken`), an invalid string (`invalid`), a registration that throws
  (`failed`). A failed registration never crashes main and leaves the other shortcuts working.
  Settings live in `shortcuts.json` (userData); a bad saved value falls back to the default.
- **Extra windows.** Secondary (one popped-out session, optional spectator `watch`) and instance
  (peer profile) windows already come from the window manager (P20-06); this task makes the HUD and
  quick window go through the same manager (same factory, URL builder, sender checks and crash
  budget) with their own options (always on top, skip taskbar off macOS, not resizable by the OS —
  the renderer drives resizing — not minimisable/maximisable/fullscreenable).
- **Which session is "last active".** App windows report the session their route shows whenever it
  changes and when they gain focus (`indus:session:active`); main keeps the most recent report per
  window and picks the newest live one.

Inputs → outputs:
- `parseAccelerator(text)` → `{ ok, accelerator }` or `{ ok: false, reason: empty | no-modifier |
  no-key | invalid-key | invalid-modifier | reserved }`.
- `ShortcutBoard.apply(settings)` → per action `{ action, accelerator, enabled, registered, error }`.
- `indus:hud:open {sessionId?, profile?}`, `indus:hud:close`, `indus:hud:reset-layout`,
  `indus:hud:drag {phase, mode}` → `indus:hud:changed {open, sessionId, profile}` to every window.
- `indus:quick:submit {text}` → `{ ok: true, sessionId } | { ok: false, code, message }`;
  `indus:quick:dismiss`, `indus:quick:shown` (event), `indus:quick:deliver` (event) →
  `indus:quick:ack`.
- `indus:shortcuts:get` / `indus:shortcuts:set {action, accelerator?, enabled?}` → the board.
- `indus:session:active {sessionId}`.

Files: `apps/desktop/electron/ambient/{shortcuts,hud,quickEntry,activeSession,install}.ts`,
`apps/desktop/electron/ambient/{shortcuts,hud,quickEntry,activeSession}.test.ts`,
`apps/desktop/src/hud/Hud.tsx`, `apps/desktop/src/quick/QuickEntry.tsx`,
`apps/desktop/src/quick/deliver.ts` (+ test), `apps/desktop/src/ambient/AmbientHost.tsx`,
`apps/desktop/src/ambient/strings.en.ts`. Shared: `electron/ipc/{channels,schemas}.ts` (my rows),
`src/global.d.ts` (regenerated), `electron/windows/{manager,factory,windowUrl}.ts` (HUD/quick
options, a HUD URL with its session, `openHud`/`openQuick`, a window-created hook),
`src/app/App.tsx` (HUD and quick roots, AmbientHost), `src/main.tsx` (the HUD starts its
connection), `src/i18n/en.ts` (one spread), `electron/main.ts` (one install line).

Edge cases / failure modes:
- accelerator `G`, `Shift`, `Ctrl+A+B`, `A+Shift`, `Ctrl+Escape`, `   ` → refused with the reason;
  `ctrl+shift+g` → `Control+Shift+G`; duplicate modifiers collapse;
- two actions given the same chord → the second is `conflict` and not registered; the first keeps it;
- `register` returns false or throws → `taken` / `failed`, others still registered; turning an
  action off releases its chord; quitting releases all;
- saved HUD bounds on an unplugged monitor, NaN, below the minimum → default bounds;
- HUD drag: the press is lost (pointer up outside, window closed) → the drag ends after 30 s or
  when the window goes away; a drag never shrinks the HUD below its minimum;
- quick submit while no app window exists → `no-target` (the window keeps the text); a deliver
  that never gets an answer → `unconfirmed` after 15 s; a duplicate or late ack is ignored (logged
  once); an ack from a window that was not asked is ignored;
- quick submit from any sender other than the quick window → refused;
- HUD opened while open for the same session → raised only; HUD closed from its own side (crash,
  Cmd+W) → snap shortcut released, main window back, every window told.

Our design differences / decisions:
- Our names: `indus:hud:*`, `indus:quick:*`, `indus:shortcuts:*`, `hud-state.json`,
  `shortcuts.json`.
- Drag is driven by main polling the pointer between the renderer's "start" and "end" (16 ms),
  instead of the renderer sending positions: no window coordinates cross the bridge, and moving
  between displays works the same everywhere.
- The toggle shortcut is a separate, rebindable action stored with the others (one shortcuts file
  for all global chords) rather than a fixed key.
- Not taken (P24 per D-16 §2.4): window-below context, cursor feed for Linux click-through, game
  overlay cue, Hyprland/compositor adapters, tap-to-summon gestures, pet and wake windows.

Tests we will write:
- shortcuts: grammar table (refusals and normal forms); board: conflict between our actions,
  `taken`, `failed` (throwing register), disabled releases, re-apply releases the old chord,
  dispose; settings file sanitising.
- hud: default bounds, saved bounds kept/clamped/refused (unplugged display), persisted after a
  move (debounced, fake clock), drag move and edge resize with the minimum, drag safety stop, snap
  clamps to the work area, open twice → one window, open for another session → `changed` event,
  other profile → replaced, close restores the main window once and releases snap.
- quickEntry: bounds on the pointer's display; toggle shows/hides; blur hides; submit relay:
  delivered, empty, no target, unconfirmed after 15 s (fake clock), late ack ignored, wrong sender.
- activeSession: newest live window wins; closed window forgotten.
- renderer deliver: text submitted into the reported session through `ChatActions`; no session →
  `session.create` then send; failure answers `ok: false`.

## Decisions for the lead
- HUD defaults: 560×240, minimum 360×160, 72 px above the work area's bottom, centred; drag poll
  16 ms, drag safety stop 30 s; state written 300 ms after the last change.
- Quick entry: 620×150, a quarter down the display; relay timeout 15,000 ms; on delivery the
  receiving app window is raised (a user action).
- The snap shortcut is registered only while a HUD is open; the toggle and quick entry are
  registered at launch (each can be turned off).
- The main window hides while the HUD is up and is shown again (not focused) when it closes.
- Shared files touched for this task: `electron/windows/{factory,manager,windowUrl}.ts` (floating
  options for the HUD/quick kinds, `openHud` / `openQuick` / `onWindowCreated` / `kindOf` /
  `windowOf`, the HUD's session in the URL hash), `src/app/App.tsx` (HUD and quick roots, the
  ambient host in app windows), `src/main.tsx` (the HUD starts its connection), `src/i18n/en.ts`
  (one spread), the channel and schema rows, `electron/main.ts` (install line).
- Wiring for other slices: any surface may open the HUD for a session with
  `indusDesktop.hud.open({ sessionId })` (e.g. a sidebar "Pop out" item, P20-25); Settings (P20-32)
  reads and writes the three global shortcuts with `indusDesktop.shortcuts.get/set` (a refused chord
  comes back as `rejected: { action, reason }`, nothing saved).

Compare checklist (filled after the rewrite; reopened `quick-entry.ts` and the HUD / quick-entry
parts of `main.ts`):
- [x] Accelerator `G` alone refused, Escape reserved, modifier after the key refused, spelling
  normalised (test: `electron/ambient/shortcuts.test.ts::accelerator grammar`).
- [x] Disabled never touches `register`; a chord another app holds is reported `taken`; a throwing
  register is reported, never thrown (test: `shortcuts.test.ts::a chord another app holds is reported taken; a throwing register is reported, never thrown`).
- [x] Conflict between two of our own actions reported with the other action (test:
  `shortcuts.test.ts::two of our actions on one chord: …`) — beyond the reference, which has one chord per feature.
- [x] HUD bounds persisted (debounced) and reused only while on a display; reset layout; snap
  clamps to the work area (tests: `electron/ambient/hud.test.ts`).
- [x] Snap chord held only while a HUD is open; released on close from either side (tests:
  `electron/ambient/install.test.ts::the HUD toggle opens the last-active session; …`,
  `hud.test.ts::a closed HUD (from its own side) still restores main and tells every window`).
- [x] Same profile → switch in place with a broadcast; other profile → respawn whose old close is
  not a teardown (test: `hud.test.ts::another session switches in place; another profile replaces the window`).
- [x] Quick entry submits into the last-active session of the window used last (tests:
  `install.test.ts::quick entry submits into the last-active session of the window used last`,
  `src/quick/deliver.test.ts`); unconfirmed after 15 s never invites a resend, late answer only logged
  (test: `quickEntry.test.ts::no answer in 15 s → unconfirmed …`); only the quick window may submit,
  only the asked window may answer.
- [x] Quick window hides on blur, repositions to the pointer's display, tells the renderer it was
  re-summoned (test: `quickEntry.test.ts::toggles; …`); turning the feature off closes it.
- [x] Gap: the reference keeps the quick window and the HUD above full-screen apps and on every
  workspace (`setAlwaysOnTop` level, `setVisibleOnAllWorkspaces`) — added `floatAbove` (best effort).
- Deliberate differences: drag is main following the pointer between "held" and "let go" (no
  coordinates over the bridge); the main window comes back without focus when the HUD closes (the
  reference focuses it; a HUD that crashed must not steal focus, R08.20); the quick window forwards
  to the window used last instead of always the primary, and has no session picker; the toggle
  chord is rebindable like the others.
- Not taken: the late-result reconcile message to the quick window (logged instead), the cursor
  feed for Linux click-through, game-overlay cue, compositor adapters, frost/vibrancy, tap-to-summon
  gestures (P24).

## Status

Done (2026-10-21). Electron-launching checks were not possible here (no Electron binary): the HUD,
quick window and global shortcuts are covered by unit tests with fakes and the registry wiring
test; P20-40's E2E should open the HUD and fire quick entry in a real window.
