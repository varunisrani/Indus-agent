# Task: Desktop composer menus, palette, switcher, sidebar rows (W52-h) Phase: ui-parity Owner: builder w52h

Status: Done for all ten slugs (2026-10-24, wrapped up early on the lead's usage call). All ten
captured once in both themes (compare PDF `$SP/w52h/compare.pdf`); the last fixes (alias rows
dropped from the slash drawer, `@` sent with its trigger so a bare `@` lists the reference forms,
help card height, switcher slot hint) landed after that capture and were not re-captured.
Remaining / follow-ups: row tooltips only on truncated titles (no cwd line); row dots use the
existing "active" flag, so open-but-idle chats show the working dot where the reference is grey;
"Chat: " title prefix (foundation follow-up); Open in terminal, Appearance, Branch, Export, Move
to project, `@staged`, `@profile`, queue-send key and paste-image omitted (W52-h-4/7).

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none directly by this builder; the
wave-52 spec `desktop-composer-menus.md` (lead's Read + Note of the reference desktop app: composer
completion drawer, trigger popover, help hint, attach menu, model and reasoning pills and menus,
floating HUD constants, command palette, session switcher and its store, shared menu tokens,
sidebar session-actions and filter menus — behaviour only) is this note's starting point.
Evidence: reference screenshots (compare set, never in the repo), desktop slugs `slash-menu`,
`mention-menu`, `model-picker`, `effort-picker`, `composer-help`, `composer-attach-menu`,
`command-palette`, `session-switcher`, `session-sidebar`, `context-menu`, each `--light` / `--dark`.

What it does (own words): the composer's floating surfaces get the reference's quiet look. Typing
`/` opens a narrow drawer above the composer with commands grouped by category, each with a small
terminal glyph; `@` opens the same drawer with reference kinds (diff, file, folder, git, url) and
paths; a lone `?` shows a help card with common commands and the keys we really have. The `+`
button opens an attach menu (files, folder, images, URL, prompt snippets). The model chip opens a
menu with a quiet search, models grouped by provider with a star, and refresh / custom / edit rows;
the effort chip opens a menu with a Thinking switch and the effort list (Max and Ultra included).
Ctrl+K opens a command palette as a floating card at the top of the window with grouped, iconed
rows and key caps. Ctrl+Tab steps through recent chats with a small switcher card (shown when held).
Sidebar rows show a dot, the title and a short time; Shift-click pins; right-click (or Shift+F10)
opens an in-window menu with the real row actions, including rename and delete.

Inputs → outputs: composer text and caret → `complete.slash` / `complete.path` (with the draft's
`cwd`) → drawer rows; picks → existing `config.set` calls and per-model presets; palette entries
from pages, overlays, projects, sessions and plugin commands; row menu → REST PATCH (title,
pinned, archived), DELETE, unread tracker, clipboard, pop-out.

Files: `apps/desktop/src/ui/components.tsx` + `ui/ui.css` (foundation: menu parts, popover, HUD,
tooltip — added in the foundation's own files), `chat/composer/{Composer.tsx,completion.ts,
ComposerHelp.tsx,AttachMenu.tsx,attachments.ts}`, `chat/{ModelPicker.tsx,EffortChip.tsx,slash.ts,
strings.en.ts}`, `contrib/{areas.tsx,strings.en.ts}`, `app/{Shell.tsx,SessionSwitcher.tsx,
chatSlots.tsx}`, `panes/shortcuts.ts`, `settings/{keybindings.ts,strings.en.ts}`,
`sidebar/{Sidebar.tsx,SessionRowMenu.tsx,sessionList.ts,sidebar.css,strings.en.ts}`, `styles.css`
(palette rules), tests beside them.

Edge cases / failure modes:
- `@` in a fresh draft must search the draft's folder (it searched the daemon's folder and showed
  nothing); an empty answer shows "No matches", not a hidden drawer.
- A slash command not in the catalogue (plugin / skill) goes in a last "Other" group.
- Rename with an empty title is ignored; a refused PATCH / DELETE rolls the row back and shows the
  sidebar's error line. Deleting the open chat navigates to the fresh draft.
- Ctrl+Tab with fewer than two chats does nothing; a quick tap switches without showing the card.
- Prevented `contextmenu` keeps Electron's native menu from opening on top of ours.

Our design differences / decisions:
- W52-h-1: no new Radix packages; the foundation's `Menu` gains the menu-surface parts (section
  label, separator, check, switch row, destructive row, submenu panel, positioned-at-pointer
  variant) so every menu in the app shares one family.
- W52-h-2: Ctrl+Tab is "next chat" through the switcher (lead decision); pane-tab cycling moves to
  Ctrl+PageDown / Ctrl+PageUp. The `tab.next` keybinding label becomes "Next chat".
- W52-h-3: the switcher shows no `⌃N` slot labels (our Ctrl+digit keeps jumping pane slots).
- W52-h-4: row menu omits Open in terminal, Appearance, Move to project, Branch and Export (no
  backend / IPC for them; export would need a save-file write IPC) — listed as gaps.
- W52-h-8: `complete.path` gets the whole `@word` (the daemon decides reference vs path by the
  `@`; the client stripped it, which is why `@` in a fresh draft found nothing).
- W52-h-5: hover action buttons (pop-out / pin / archive) leave the row; the same actions live in
  the row menu (the reference shows none on hover).
- W52-h-6: per-model reasoning capability is unknown to us, so the effort chip always shows.
- W52-h-7: `@staged`, `@profile` mentions, paste-image from the menu and a queue-send key do not
  exist in Indus; the menu shows Paste image disabled, the others are omitted.

Tests we will write:
- completion: `cwd` sent for a fresh draft; empty answer keeps the drawer with "No matches";
  slash rows grouped by catalogue category.
- composer: `?` opens help, deleting it closes; `+` menu lists the attach items, URL inserts
  `@url:`; effort menu lists Max/Ultra and the Thinking switch.
- palette: Ctrl+K opens (Ctrl+Shift+P still does), groups and key caps render.
- shortcuts: Ctrl+Tab → switcher step, Ctrl+PageDown → pane cycle.
- sidebar: filter menu switches to Archived; Shift-click pins; row menu rename (PATCH title) and
  delete (DELETE) with a fake API.

Compare checklist (tick in the side-by-side PDF, light AND dark):
- [x] slash-menu: drawer 320 px wide, anchored 3 px inside composer left, ends 5 px above composer;
      group header "SESSION" etc.; terminal glyph in accent; sans `/name` + grey truncated
      description; 28 px rows; highlight tint; tooltip on hover.
- [x] mention-menu: opens on bare `@` in a fresh draft; kind icons coloured; file/folder scope
      header; empty state.
- [x] model-picker: ghost pill with chevron (no border, no "Choose a model" after a chat); 288 px
      menu sitting on the composer; borderless search; provider label; star + name + chevron rows;
      refresh/add/edit footer rows.
- [x] effort-picker: separate "Med"-style pill; 208 px menu; OPTIONS/Thinking switch; EFFORT list
      with check; Max/Ultra present.
- [x] composer-help: `?` opens it; mono commands; key caps; deleting `?` closes.
- [x] composer-attach-menu: `+` at composer left; 240 px menu; ATTACH label; 5 items + snippets +
      tip; Paste image disabled; URL and snippets dialogs work.
- [x] command-palette: Ctrl+K; 544 px HUD at top 12 px, no dim; search row with icon + divider;
      accent uppercase group headings; icons; key caps; selection tint.
- [x] session-switcher: hold Ctrl+Tab shows 304 px HUD with dots/titles; release opens; quick tap
      switches.
- [x] session-sidebar: quiet search with one clear button; PINNED/SESSIONS accent headers; filter
      menu replaces the segmented control; 27 px rows with dot/title/time; selected tint; tooltip
      for long titles.
- [x] context-menu: in-renderer menu on right-click/Shift+F10; grouped items with separators;
      Delete in red; rename inline works; native menu does not double-open.
- [x] No reference product text; no copied long sentences; tokens from the foundation, no stray
      hex in components.
- [x] biome, tsc, vitest (desktop, all 172 files; Electron e2e not run — lead stop), (desktop), Electron e2e touched specs, `python -m tooling.checks
      --require-ref` green (ok, exit 0).
- Intended differences: "Auto" stays first in the effort list (no stored preset); switcher has no
  `⌃N` labels (W52-h-3); slash rows also carry our catalogue text; palette lists our pages/overlays.

## Integration (wave 53, desktop)
Merged with w52g (composer notice strip, `MenuItem` tone, `ui.css` danger rows) and w52m
(`PanelLabel` next to the menu family in `ui/components.tsx` / `ui.css`). Decision ids: W52-h-1 →
D-16.1222 (with W52-m-5); W52-h-2, W52-h-3 → D-16.1223; W52-h-4, W52-h-5 → D-16.1224; W52-h-8 →
D-16.1225; W52-h-6 → D-16.1214; W52-h-7 → D-16.1226. The Electron e2e lane ran at integration:
`surfaces.electron.test.ts` now opens the HUD pop-out from the row menu's "Open in window" (the row's
hover button is gone, W52-h-5; D-16.1229). Biome: the drawer / HUD `> svg` rules add two
`noDescendingSpecificity` warnings in `ui.css` (5 in total with the foundation's 3).
