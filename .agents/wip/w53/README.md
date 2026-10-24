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
