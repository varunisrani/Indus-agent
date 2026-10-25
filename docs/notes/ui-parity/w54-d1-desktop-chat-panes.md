# Task: Desktop chat + panes parity fixes (W54-D1) Phase: ui-parity Owner: builder w54d1

Status: Done for wave 54 (2026-10-25), except the items under "Not taken" and "Follow-ups".

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): no reference source opened by this
builder; the wave-53 side-by-side gap list (per-slug lines) and the reference screenshots were the
input.
Evidence: reference screenshots of the wave-53 compare set (never in the repo), desktop slugs
`chat-conversation`, `chat-streaming`, `tool-row`, `tool-row-expanded`, `approval-card`,
`session-sidebar`, `context-menu`, `right-sidebar`, `files-pane`, `review-pane`, `hud`, each
`--light` and `--dark`; Indus-only `quick-entry`.

What it does (own words): closes the visible gaps of the chat column, the session list and the
tool-pane side group.
- The prompt box reads at body size, left aligned: a pending-count badge class of the Messaging page
  (`.msg-bubble`) leaked into the transcript (same class name, global CSS) and shrank every prompt
  to a tiny centred banner. The badge class is now `.msg-pending`.
- A terminal call opens into a `$ command` block with its exit badge and its own copy button
  (command and output), and the output reads like a screen (no "Output" label, no inset box). Other
  tools keep the labelled argument/output sections.
- The chat tab carries a dot (grey quiet, blue while a turn runs, orange while an approval or
  question waits) and, for a chat the list does not hold yet, its first prompt as the title
  instead of "Chat". A fresh chat is listed in the sidebar as soon as its turn starts (the list
  refreshes on `turn.started` for an unknown session).
- Sidebar rows: an orange dot for a chat waiting for the person, blue pulse while working, teal
  for new activity, a teal ring for a chat that is still live (`is_active`), grey otherwise; a ⋮
  button on hover (in the place of the time) opens the same menu as a right-click; the menu gains
  Branch (`session.branch`, then the new chat opens).
- While a reply runs the composer shows one Stop; Steer and Queue appear only once something is
  typed (they were disabled-but-visible before).
- Side group: the Review column is wider (about 380 px at the usual width; Files and Preview
  stay about 236 px), the chat keeps at least about 40 % of the span; Ctrl+J toggles the right
  sidebar (Files over Terminal) like Ctrl+Alt+B, except inside a terminal where Ctrl+J is the
  shell's line feed. The terminal's canvas got a real font family (a raw `var(--…)` made xterm
  fall back to a proportional font with wide cells).
- HUD: the sheet shows the latest exchange from its question down, no scrollbar.
- Quick entry: the card fills its window (640×112), so a desktop without a compositor no longer
  paints a black margin around it.

Inputs → outputs: transcript items, open server requests, `turn.started` / `turn.done` events,
`GET /api/sessions` rows, `session.branch` → DOM of the chat column, the tab strip, the sidebar and
the side panes. No protocol or API change.

Files: `apps/desktop/src/chat/{ToolRow,transcriptReact}.tsx`, `chat/{transcript.css,
transcriptStrings.en.ts}`, `chat/composer/Composer.tsx`, `app/Shell.tsx`,
`app/messaging/{MessagingPage.tsx,messaging.css}`, `sidebar/{Sidebar,SessionRowMenu}.tsx`,
`sidebar/{sessionList.ts,sidebar.css,strings.en.ts}`, `panes/{PaneShell.tsx,panes.css,presets.ts,
shortcuts.ts,strings.en.ts}`, `panes/terminal/xtermView.ts`, `ambient/ambient.css`,
`electron/ambient/quickEntry.ts`, tests next to them.

Edge cases / failure modes:
- `turn.started` for a session already listed does not refresh (no extra request per turn).
- Branch of a busy session: the backend answers `SESSION_BUSY`; a toast says the branch failed.
- Branch is offered only when the sidebar host has a connection (no dead menu item).
- Ctrl+J with focus inside a terminal pane goes to the shell.
- The tab dot uses live turn events only; `is_active` (recently active) is not "working".

Our design differences / decisions:
- W54-D1-1: terminal block copy copies `$ command` plus the output; other tools keep the head copy.
- W54-D1-2: dot ranking attention > running > unread > live > idle; live is a ring, not a fill.
- W54-D1-3: side columns sized per pane (Review 380, the others 236 at a 1180 span), the chat
  keeps ≥ 480 of it; Ctrl+J is an alias of the right-sidebar toggle. A wider Preview column (420)
  was tried and dropped: with it `e2e/core/security.electron.test.ts` (hostile guest
  window-open.html) no longer found the preview guest; cause not found in the time box.
- W54-D1-4: xterm gets the resolved mono family (`terminalFont()`), never a CSS variable.
- W54-D1-5: quick-entry window 640×112 with the card filling it (was 640×168 with a margin).
- W54-D1-6: the HUD sheet shows the latest turn only (earlier turns stay in the main window).
- W54-D1-7: one Stop while a reply runs; Steer/Queue only with text typed.
- W54-D1-8: row menu adds Branch only; Open in terminal, Appearance, Export and Move to project stay
  out (no desktop file-writer bridge for an export file, no per-chat appearance or project move in
  our backend, no terminal-resume flow in the desktop).
- The "Chat:" title prefix in the captures comes from the scripted fake model's title answer
  (`tests/fakes/model/http_serve.py`), not from the product; nothing to drop in product code.

Tests we will write:
- ToolRow: terminal block copy button, no "Output" label, `terminalCopy` text.
- sessionList: `turn.started` for an unknown session asks for a refresh.
- Sidebar: ⋮ opens the row menu; Branch only with a host handler and called with the newest id;
  dot ranking.
- shortcuts: Ctrl+J / ⌘J → toggle-right.
- quick entry bounds at the new height.

Compare checklist (re-shot with the w54d1 kit copy, both themes, next to the reference PNGs):
- [x] chat-conversation: prompt box body size, left aligned (test: none, CSS class rename)
- [x] tool-row / tool-row-expanded: terminal block + copy (test: `transcriptLook.test.tsx`)
- [x] chat-streaming: chat listed at once, tab titled by its prompt, one Stop
  (test: `sessionList.test.ts::running dots follow turn events…`)
- [x] approval-card: orange dot on tab and row (kit shot)
- [x] session-sidebar: ⋮ on hover, unread/live dots (test: `Sidebar.test.tsx::W54-D1-2…`)
- [x] context-menu: Branch added (test: `Sidebar.test.tsx::W54-D1-8…`)
- [x] right-sidebar / files-pane / review-pane: wider review column, monospace terminal, Ctrl+J
  (test: `shortcuts.test.ts`)
- [x] hud: starts at the question (kit shot)
- [x] quick-entry: no black margin (test: `quickEntry.test.ts`)
- Deliberate differences: our terminal pane keeps its instance rail; the side group remains our
  pane tree (a Ctrl+J toggle over it, not a separate sidebar component).
- Not taken: per-message timestamp/actions row under replies (no reply time in our history rows),
  the context-menu items listed in W54-D1-8.

## Decisions for the lead
- W54-D1-5 changes the quick-entry window height (main process constant); revert the constant and
  the CSS together if a margin is wanted back.
- Follow-up: the session switcher (Shell) still treats `is_active` as running; align it with the
  sidebar's live/running split if wanted.
