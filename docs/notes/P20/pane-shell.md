# Task: Pane shell and interface modes (P20-27) Phase: 20 Owner: wave-35 builder D1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/src/components/pane-shell/tree/store.ts`
(node and zone shapes, the exported operations' names only) and `tree/model.ts` (the split/group node
types and the normal form the store relies on). Research 08 §3.12 (pane shell, interface mode, tab
shortcuts). Closed before writing.
Evidence: none.

What it does (own words): the area right of the sidebar is a tree. A split lays its children out in a
row or a column with relative sizes; a zone is a stack of panes shown as tabs, one active. The chat
(route content) is a pane like the others but cannot be closed. Tool panes (preview, files, review,
terminal — P20-28..31) register themselves in a pane registry and are placed by the tree.
Tabs can be dragged onto another zone's centre (join its tabs) or edge (split it), reordered within a
zone, closed (remembered on a closed stack, "reopen last closed" puts it back where it was) and
activated. A zone without stateful panes can be minimised: it leaves the layout space and appears as a
button on a restore rail. A stateful pane (preview, terminal) offers **Hide** instead: its body stays
mounted (DOM node, process, scroll, webview kept) but is `hidden` and `inert`; Close releases it.
Presets rebuild the tree: Basic, Focus, Default, Terminal deck, Quad; panes a preset leaves out go on
the closed stack, so a saved layout remembers closed panes.

Interface mode: Advanced (default) or Simple (chat first). Simple hides the status bar, the profile
rail when there is only one profile, the terminal, files and review panes, the technical tool view
(tool output details) and inline diffs, and keeps thinking collapsed. Each mode keeps its own
arrangement: switching to Simple and back restores the Advanced tree exactly. Keyboard shortcuts work
in both modes.

Shortcuts (platform modifier = ⌘ on macOS, Ctrl elsewhere): new chat (Mod+T), cycle tabs in the
focused zone (Ctrl+Tab / Ctrl+Shift+Tab), jump to tab 1–9 (Mod+1..9; 9 = last), close the active tab
(Mod+W; never when the terminal has focus — the shell owns that key), reopen last closed (Mod+Shift+T),
new window (Mod+Shift+N), toggle the left sidebar (Mod+B) and the tool zones (Mod+Alt+B). Holding the
modifier alone for 400 ms shows slot numbers on the tabs. Unfocused zones are desaturated 20 %.

Inputs → outputs: registry of panes; layout operations (pure functions over an immutable tree) →
`LayoutStore` (per mode, persisted in local storage `indus.desktop.panes.v1`) → `<PaneShell>` DOM.

Files: `apps/desktop/src/panes/paneTree.ts`, `src/panes/presets.ts`, `src/panes/layoutStore.ts`,
`src/panes/PaneShell.tsx`, `src/panes/shortcuts.ts`, `src/panes/strings.en.ts`, `src/panes/panes.css`,
`src/panes/paneTree.test.ts`, `src/panes/layoutStore.test.ts`, `src/panes/PaneShell.test.tsx`,
`src/panes/shortcuts.test.ts`. Shared: `src/app/Shell.tsx` (hosts it), `src/i18n/en.ts`.

Edge cases / failure modes:
- dragging the last tab out of a zone removes the zone; single-child splits unwrap; same-direction
  nested splits flatten (sizes scaled); a pane is never in two places, never lost (property test over
  random operations);
- a drop on the pane's own zone centre reorders; a drop on its own lone zone's edge is a no-op;
- a persisted layout naming an unknown pane, a duplicate pane or a malformed node → repaired on load
  (unknown panes kept on the closed stack only if registered later; duplicates dropped);
- closing the chat pane, minimising the chat zone or a zone with a stateful pane → refused;
- Simple mode: opening a hidden-surface pane is refused (the command is not offered);
- the closed stack is bounded (20); reopening a pane whose zone is gone goes next to its old
  neighbour, else into the default tool zone.

Our design differences / decisions:
- One chat pane per window (the route's conversation); multi-session tiles are not built in 1.0.
- "New tab" opens a new chat (the chat pane is the only tab kind that can be new).
- Tool panes register through `paneRegistry.register({ id, title, stateful, render })` (P20-28..31).

Tests we will write:
- tree: property test (seeded, 500 random moves/closes/reopens/minimises) → no pane lost or doubled,
  tree canonical; drops at each edge; normalise; parse/repair;
- presets: each preset's shape, closed panes remembered;
- store: mode switch hides surfaces and restores Advanced exactly; persisted across reload;
- shell: Hide keeps the same DOM node (inert, hidden); minimise → restore rail; close/reopen; drag a
  tab to another zone; shortcuts (jump, close not over terminal, hold 400 ms shows slots).

## Decisions for the lead
- Pane registry API for D2 (P20-28..31): `import { paneRegistry } from "src/panes/PaneShell.tsx"`;
  `paneRegistry.register({ id: "preview" | "files" | "review" | "terminal", stateful: boolean,
  title?: string, render(ctx: { sessionId, profile, active, hidden }) })` → unregister. Preview and
  terminal register `stateful: true` (Hide keeps them mounted). Known ids have built-in titles.
- Layout persisted in `indus.desktop.panes.v1` by the main window only (secondary / instance windows
  keep theirs in memory so they never overwrite it).
- One chat pane per window; "new tab" opens a new chat; Simple mode refuses to open the panes it
  hides (switch to Advanced to show them) instead of a per-session reveal.
- The interface mode is set from the pane toolbar (radio Advanced / Simple) and kept with the layout.

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] Node/zone shapes and normal form: no empty zones, single children unwrapped, same-direction
  splits flattened with scaled weights (`paneTree.test.ts::drops empty zones, unwraps lone children and flattens same-direction splits`, `::scales flattened sizes into the parent slot`).
- [x] No lost or doubled pane over random operations (`paneTree.test.ts::never lose or double a pane and always stay canonical`).
- [x] Hide vs minimise vs close (`PaneShell.test.tsx::Hide keeps the stateful pane's DOM node (inert, hidden); showing it keeps its state`, `::minimise moves a zone to the restore rail and back; stateful zones offer Hide only`, `::close and reopen from the toolbar; the chat cannot be closed`).
- [x] Drag between zones (`PaneShell.test.tsx::a tab dragged onto another zone joins its tabs`; drop geometry `::the middle is the centre, else the nearest edge`).
- [x] Presets remember closed panes (`paneTree.test.ts::a preset remembers the panes it leaves out as closed`); each mode keeps its arrangement (`layoutStore.test.ts::Simple mode hides terminal, files and review and restores Advanced on the way back`; shell `interfaceMode.test.tsx`).
- [x] Shortcuts incl. close not over a terminal and the 400 ms slot hint (`shortcuts.test.ts`, `PaneShell.test.tsx::holding the modifier 400 ms shows slot numbers; another key hides them`).
- [x] Gap found: the reference keeps per-mode layout out of secondary/popout windows — main-window-only persistence added.
- Deliberate differences: the reference's per-zone tab-strip modes (always/never/auto), pane share
  memory (a reopened pane gets its old size), floating panes, zone editor and edit mode are not built;
  the mode is a stored choice, not a resolver over per-surface preferences.
- Not taken: close others / to the right, pane reload, swap sides, plugin panes (P24).

## Status

Done (2026-10-21). Shared: `src/app/Shell.tsx` (hosts the pane shell, shortcuts, mode surfaces),
`src/styles.css` (shell grid), `src/i18n/en.ts`, `src/main.tsx`.

Integration (wave 35, D-16.717): the render context gained `workspace`, `mode`, `connection`, `openPreview` and `addToChat` for the working panes (registered at startup by `registerWorkingPanes()`); the shell declares the window's folders to main with `fs.setRoots`.
