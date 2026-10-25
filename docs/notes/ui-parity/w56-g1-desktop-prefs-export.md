# Task: Desktop window prefs + session Export (W56-G1) Phase: ui-parity Owner: builder w56g1

Status: Done for wave 56 (2026-10-25); see the end.

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`):
`apps/desktop/src/app/settings/settings-manifest.ts` (the appActions / fileBrowser / composerPopout
entries: page and search words only), `apps/desktop/src/i18n/en.ts` (the three rows' one-line
descriptions, to learn what each row means), a name search for "floating composer" / "file browser"
/ "app actions" over `apps/desktop/src` (file names and comments only, no component code opened).
Evidence: the wave-53 desktop compare set, slugs `settings-appearance-window-and-layout--{light,dark}`
(reference and ours), `context-menu--{light,dark}`, and `$S/compare/deliver/gaps.md` (lines for
those slugs). Nothing copied; the rows and the IPC below are written from this note.

What it does (own words):
- Settings › Appearance › Window & layout gets the last three rows that were disabled since W55-F3,
  each a desktop pref kept by main (one file per device), defaults keeping today's look:
  - **App actions** (Right | Left): where the window band's own tool cluster sits — settings,
    layout preset, layout editor, Files / Review toggles, Simple / Advanced. Right (default) is
    today: at the window controls. Left puts the cluster at the left end of the pane area; the
    top-left zone's tabs then start after it and the top-right zone keeps clear of the window
    controls only.
  - **File browser** (off by default): when on, each time the window gets a workspace folder (at
    start, or on opening a chat in another folder) the Files pane is shown beside the chat;
    turning it on with a workspace already set shows it at once. Off leaves the layout alone.
  - **Floating composer** (off by default): when on, the composer carries a grip; dragging the
    grip lifts the composer off the bottom into a card over the conversation, kept inside the
    chat column; the grip as a button floats / docks it (keyboard: Enter or Space; the arrow keys
    move a floating one). Off docks it again and hides the grip. The HUD keeps its own composer.
- The session row menu gains **Export**: the renderer sends only the session id; main reads the
  conversation from the server (`GET /api/sessions/{id}/export` over the REST proxy, the current
  connection and its profile), asks where to save with the system save dialog (parented to the
  asking window, a default name in Downloads), and writes the file there. The window never names
  a path and never sees the file's contents.

Inputs → outputs: Settings rows → `indus:prefs:set {appActionsSide | fileBrowser |
floatingComposer}` → `indus:prefs:changed` → the display store → `.pane-shell[data-app-actions]`,
a `store.open("files")`, the composer dock's grip. Row menu Export → `indus:sessions:export
{sessionId}` → main: REST GET → save dialog → file (JSON Lines, the same objects `indus sessions
export` writes, so `indus sessions import` reads it back) → `{saved: false}` or `{saved: true,
fileName}` → a toast.

Files: apps/desktop/electron/{prefs.ts,prefs.test.ts,ipc/schemas.ts,ipc/channels.ts,main.ts},
apps/desktop/electron/sessions/{export.ts,export.test.ts} (new), apps/desktop/src/global.d.ts
(generated), apps/desktop/src/settings/{displayPrefs.ts,appearancePages.tsx,stringsA.en.ts,…},
apps/desktop/src/panes/{PaneShell.tsx,panes.css}, apps/desktop/src/chat/{ChatView.tsx,
ComposerDock.tsx (new)}, apps/desktop/src/app/{App.tsx,Shell.tsx}, apps/desktop/src/sidebar/
{SessionRowMenu.tsx,Sidebar.tsx,strings.en.ts}, tests beside them.

Edge cases / failure modes:
- A prefs file from an older build has none of the three fields: the defaults apply (readPrefs).
- File browser with no Files pane registered or in Simple mode (Files hidden): nothing opens.
- The same workspace again (re-render, same chat): no reopen, so closing Files sticks until the
  folder changes.
- Floating composer: a window resize can leave the card outside the column; it is clamped back on
  the next move, and a column narrower than the card docks nothing (the card shrinks to the
  column). Turning the pref off while floating docks it.
- Export: no connection → "not connected" error toast; 404 → "no such session"; 413 → the server's
  "too large here" message; cancel → nothing written, no toast; a write failure → error toast. The
  dialog is parented to the sender's window only when main knows that web contents.

Our design differences / decisions:
- W56-G1-1 App actions moves our band cluster (the reference's moves its own titlebar actions).
  The W55-F1-3 "shift to a wider zone" placement applies on the right only; on the left the
  cluster always sits at the pane area's start.
- W56-G1-2 File browser: the band's Files toggle does not write the setting (the reference's
  titlebar toggle does). Our layout already persists per mode, so the toggle's state survives a
  restart by itself; the setting only adds "show Files when a workspace arrives".
- W56-G1-3 Floating composer position lives in the window (not saved): a restart docks it.
- W56-G1-4 Export writes JSON Lines named `indus-session-<id>.jsonl`, the CLI's format, rather
  than the web page's one-object JSON, so the file round-trips through `indus sessions import`;
  written with mode 0600 (a transcript can hold secrets).

Tests we will write:
- prefs: defaults for the three fields, an old file without them, a bad value ignored, patch.
- displayPrefs/appearance: the three rows enabled, each writes its field.
- PaneShell: `data-app-actions="left"` and the top-left zone marked; File browser opens Files when
  a workspace arrives, not again for the same one, never when off.
- ComposerDock: no grip when off; grip floats/docks; drag moves; pref off docks.
- Export: proxy path and profile, JSONL written to the chosen file, cancel writes nothing, 404 /
  413 / not-connected errors, the renderer's payload carries no path (schema strict).
- Row menu: Export item present with an exporter, absent without.

## Decisions for the lead
- W56-G1-1..4 above.

Compare checklist (filled after the rewrite; reopened the manifest entries and the en.ts lines):
- [x] App actions Left/Right live (test: `src/settings/windowPrefs.test.tsx::App actions`)
- [x] File browser opens Files with a workspace, once per folder, never in Simple mode (test:
  `windowPrefs.test.tsx::File browser`)
- [x] Floating composer: grip float/dock, drag out of the dock, clamped, arrow keys, setting off
  docks, main window only (test: `windowPrefs.test.tsx::Floating composer`)
- [x] Export item in the row menu (test: `windowPrefs.test.tsx::session row menu Export`); main
  side: proxy route, dialog in the sender's window, JSON Lines 0600, cancel, 404/413/offline,
  strict payload (test: `electron/sessions/export.test.ts`)
- [x] The three Settings rows write their prefs (test: `displayPrefs.test.tsx::General and Window
  & layout`, which pinned them disabled before and now pins them live); prefs defaults and old
  files (test: `electron/prefs.test.ts::window choices`)
- Deliberate differences: see W56-G1-1..4.
- Not taken: Open in terminal, Appearance ›, Move to project › in the row menu (out of this slice).

## Status

Done (2026-10-25). The three Window & layout rows are live desktop prefs with today's look as the
default; Export runs through main (`indus:sessions:export`). The Export path is covered by unit
tests (main and renderer); the save dialog itself was not driven in the Electron lane. The re-shot
captures (window-and-layout row page, the row menu with Export, all three prefs on over a chat)
stay in the scratchpad.
