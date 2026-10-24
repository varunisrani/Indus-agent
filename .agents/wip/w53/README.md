# Wave 53 (lead: Agent 2) — UI parity integration + w52 batch 2

## w53-desk.patch — desktop integration (INT-D), base e402468 = main 8233c54-content + w51b (local), 89 files
- = w51b (desktop foundation, NOT included — apply `.agents/wip/w51/w51b.patch` first) + w52g + w52h + w52m merged.
  So land: w51b.patch, then w53-desk.patch (its base is main + w51b).
- Conflicts resolved: Shell.tsx imports, Composer.tsx lucide imports (h's attach menu replaces g's Plus), ui.css /
  ui/components.tsx (h's menu/HUD/tooltip/SmallDialog + m's PanelLabel). Review pane moved onto notify() toasts and
  SmallDialog (role prop, alertdialog). E2E-only fixes: chat.electron onboarding selector `data-ui="onboarding-endpoint-url"`;
  surfaces HUD pop-out via row context menu "Open in window".
- Gate (INT-D): biome 915 files 0 errors (5 CSS-order warnings); format clean; tsc desktop clean; check-ipc + bridge types ok;
  desktop vitest 174 files / 1730 passed; Electron E2E 43 passed / 2 skipped (packaged-build only); tooling.checks ok.
- D-16.1210 – 1229 (§2.7). Landing notes: landing-w53-desk.md. Follow-ups listed there (g's leftovers, HUD approval,
  quick-entry target, BootOverlay toast, CSS order, re-capture g/h/m).

## w53j.patch — builder j, desktop settings B (spec desktop-settings-b.md), base e402468 (main + w51b), 33 files
- 29 of 33 reference slugs: Memory, Voice (own view), Advanced (7 pages), Notifications, Providers (accounts, keys,
  endpoints, local models), Gateways, Keyboard, Tools & Keys › Keys, Sessions, Plugins, About (version, uninstall,
  licences). Not built: Billing (lead decision), Screen capture + Tools & Keys › Settings (spec). New
  settings/pagesB/*, fieldCopy.ts, stringsB.en.ts; shared edits in own blocks (pages.ts incl. `PageDef.after`, navMeta,
  SettingsOverlay, context `go`, schemaPage field copy, search, keybindings, strings, i18n spread).
- Field moves out of settings-a's pages (terminal.backend/timeout, tool_output.max_chars, chat/voice) — slice i makes
  the same removals. Integrator: ONE label/order mechanism (j's fieldCopy.ts + curated order vs i's), customPages.tsx
  conflicts (both slices cut pages out of it).
- Gate (builder): biome 0 errors (1 existing warning); tsc clean; desktop vitest 172 files / 1712 passed; Electron E2E
  chat 9 + surfaces 6 passed; tooling.checks ok. Decisions W52-j-1..9 in docs/notes/ui-parity/desktop-settings-b.md.
- Still differs: Plugins keeps shared contrib markup; no endpoint delete (PUT /api/config is additive); no "Current"
  pill; no gradient edge on provider cards. Compared against spec numbers (no reference screenshots in this container).

## w53i.patch — builder i, desktop settings A (spec desktop-settings-a.md), base e402468 (main + w51b), 22 files
- All 21 slugs: Model (main, fallbacks, auxiliary, MoA), Chat, Appearance (general, theme, typography, window,
  chat display, pet), Workspace (projects, shell, files; `page=desktop` alias → projects), Safety, Browser, Passwords.
  New settings/{modelPages,appearancePages,pagesA,controlsA}.tsx, fieldCopyA.ts, stringsA.en.ts, settingsA.css + test.
  Shared: pages.ts (`PageDef.order`, views browser/passwords, manifest moves), strings, i18n spread, navMeta
  `pageIcon()`, SettingsOverlay (child icons, rail footer, search pill no autofocus), context (schema, focusField,
  saveField, savePatch), schemaPage (FIELD_COPY_A, comma lists, order), search, customPages (PAGES_A spread),
  ipc schemas chatTextScale max 175.
- Rows without a feature behind them render disabled with "Not available in this build yet." (lead decision).
- Gate (builder): biome 0 errors; tsc clean; settings tests 11 files / 100; full desktop vitest 173 / 1711 (before a
  final label-only edit); Electron E2E chat + surfaces 15 passed; tooling.checks ok. Decisions W52-dsa-1..16, W52-i-1..3.
- Merge with j: one field-copy map (fieldCopyA.ts + j's fieldCopy.ts), customPages.tsx, context.tsx (j adds `go`),
  overlay child icons; `default-directory` stays on Workspace › Projects (e2e + lead decision), j's Sessions page edits
  the same value under `sessions-directory` — both kept.
