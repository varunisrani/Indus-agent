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

## w53k.patch — builder k, desktop capabilities (spec desktop-capabilities.md), base f3f7772 (LOCAL = e402468 + w53-desk), 25 files
- Apply AFTER w51b + w53-desk. New shared list-and-detail frame app/masterDetail.tsx (+css, test); capabilities tabs
  (Skills, Tools, Connectors/MCP, Plugins), Messaging rewrite, Bots roster in the sidebar (SESSIONS | BOTS strip, edge
  tab "Scheduled jobs"); Shell.tsx LeftSidebar edited (one "Main" nav kept; /bots route kept for deep links).
- Done: skills, tools, mcp, messaging, bots-tab. Partial: capabilities-plugins (Bots surface not a Desktop row;
  Desktop switch read-only until contrib/host.ts implements `DesktopPluginSource.setEnabled`).
- Gate (builder): biome 0 errors (5 existing warnings); tsc clean; desktop vitest 176 files / 1739 passed; Electron E2E
  boot + surfaces 10 passed; tooling.checks ok. Decisions W52-k-1..7 in docs/notes/ui-parity/desktop-capabilities.md.
- Follow-ups: setEnabled + Bots row in desktopPluginSource; toolset write route; readiness probe; skill catalog /
  SKILL.md routes; plugin uninstall + catalog; bot presence + create-room RPC; messaging docs_url; "1 tools" plural.

## w53l.patch — builder l, desktop command center (spec desktop-command-center.md), base f3f7772 (LOCAL = e402468 + w53-desk), 45 files
- Apply AFTER w51b + w53-desk. Command center (sessions, system, usage, maintenance), session import (new overlay),
  cron + new-job dialog, profiles, agents, artifacts, webhooks, learning map (starmap), sidebar no-match line: done.
  Layout editor partial (no per-zone grip chips; band preset select kept for tests/e2e).
- New app/overlay/kit.tsx (+css) panel controls; routes.ts moves webhooks / command-center / learning-map to overlays
  (paths unchanged); Shell.tsx footer More menu + palette; ManageOverlay removed (unused); foundation additions:
  EmptyState description/look, SmallDialog size wide/closeLabel, OverlayCard titlebarActions/surface/id + focus,
  ListDetail reshaped.
- Integrator: k's app/masterDetail.tsx has TextTabs too — keep ONE TextTabs / search control (dedupe with
  overlay/kit.tsx QuietSearch); both edit Shell.tsx LeftSidebar/footer.
- Gate (builder): desktop vitest 176 files / 1745 passed; Electron E2E surfaces + boot 10/10; tsc clean; biome 0 errors;
  tooling.checks ok. Decisions W52-l-1..11 in docs/notes/ui-parity/desktop-command-center.md.

## w53-web.patch — web integration (INT-W), base 574689d (LOCAL = main + w51a), 112 files
- Land order: w51a.patch, then w53-web.patch. = w52 a–f merged: ONE model picker (ui/ModelPickerDialog.tsx; chat's
  copy deleted), deduped Badge secondary / SectionHeading / PageSpinner / CardHeader subtitle+below / Modal props;
  light warning token #ffbd38; Button uppercase (except ghost; `caps={false}`).
- Web vitest timeouts root-caused (CPU-bound: React dev element creation, jsdom style computation, lazy page modules
  evaluated inside the first test): new src/testing/renderApp.tsx preloads routed pages; route test split per route;
  w52f's 20 s timeout removed. e2e auth.spec race (loading card vs "No sessions yet") fixed in 4 waits.
- Gate (INT-W): tsc web/e2e/ui 0; web vitest 40 files / 360 passed; web Playwright 15/15; pyright 0; pytest -k "login or
  auth" 704 passed / 2 skipped; notices fresh; tooling.checks ok. D-16.1190 (owner parity decision, §3.2) + 1191–1209.
  Landing notes: landing-w53-web.md.

## w53-settings.patch — settings integration (INT-S), base f3f7772 (LOCAL = e402468 + w53-desk), 44 files
- = w53i + w53j merged on the desktop integration: one field-copy map (settings/fieldCopy.ts, i18n keys), customPages
  rebuilt, context/SettingsOverlay/navMeta unions, PageDef.order + .after. All 713 config fields placed: none lost,
  two shown twice on purpose (security.allow_private_urls, checkpoints.max_snapshots).
- Gate (INT-S): tsc clean; check-ipc + bridge ok; desktop vitest 175 files / 1757 passed; Electron E2E 43 passed /
  2 skipped; tooling.checks ok; 51 settings pages captured both themes, none broken. D-16.1230–1249. Landing notes:
  landing-w53-settings.md.

## w53-kl.patch — capabilities + command center integration (INT-KL), base f3f7772 (LOCAL = e402468 + w53-desk), 73 files
- = w53k + w53l merged: one "Main" nav in both sidebar panels; one control kit app/kit.tsx (TextTabs `look="page"`,
  QuietSearch, PanelPill tones, MetaGrid framed, DetailHead, TextAction; EmptyState look="panel" fill/role); plugin
  Desktop switch live (contrib/host.ts `setEnabled`, no IPC); "1 tool" singulars; `plugins.rescanAll` (was shadowed).
- Gate (INT-KL): tsc clean; check-ipc + bridge ok; desktop vitest 178 files / 1756 passed; Electron E2E 43 passed /
  2 skipped; tooling.checks ok; k 7 + l 15 page pairs captured, none broken. D-16.1250–1269. Landing notes: landing-w53-kl.md.

## Landing tree (lead) — main + w51a + w53-web + w51b + w53-desk + w53-settings + w53-kl
- pnpm-lock regenerated (pnpm install), notices regenerated (`notices.py refresh` + write; 439 components).
- Unions: 16-what-we-change.md (D-16.1190–1269 in order), desktop i18n spreads; no duplicate string keys (2099 keys).
- Root-caused on the combined tree: parity scan missed `onboarding-custom-option` (w52m passed the hook through a
  `ui` prop) → ProviderRow takes `data-ui`. Repo lint was red on main (Agent 1's kit scripts under .agents/) → biome
  and ruff exclude `.agents`.
- Full pytest on land-1 (before k+l): 18778 passed / 1 failed (the parity hook above, fixed) / 134 skipped; perf 18/1.
