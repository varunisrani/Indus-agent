## w52d (web Channels, Webhooks, Pairing, Keys) — 11 files, base 0a7f30a (main + w51a), clean scan
- Pages rebuilt on the foundation; shared additions in ui/controls.tsx (SectionHeading, EmptyCard, PageSpinner, CodeChip,
  Callout; Badge `secondary`, Button `danger-outline`; CardHeader subtitle; Card id) and ui/CopyButton.tsx (`look`);
  ModelsPage.tsx: `export function DeviceLogin` only (slice b rewrites ModelsPage — keep the export when merging).
- Decisions W52-d-1..6. Integrator: light-theme warning token → reference's pale yellow (shared token, owner wants parity).
- Gate: biome, tsc, web vitest 298, web e2e 14, tooling.checks ok. Compare PDF scratchpad/w52d/compare.pdf.
## w52a (web Sessions + Chat) — base 0a7f30a, clean scan
- Sessions finished (source multi-select menu, History rows with checkbox/icons/rename/bulk delete, search-as-you-type,
  auto dry-run prune, empty state), Chat (terminal card, 240px collapsible side panel with live status badge — fixes the
  "always answering" bug, model picker dialog `chat/ModelPickerDialog.tsx`, header session title).
- Shared: PageHeader `<HeaderTitle>`; ui/Dialog.tsx Modal className props (defaults unchanged); index.css terminal tokens
  and amber `mark`. Decisions W52-sc-1..8. Integrator: ONE shared model picker (this one) — slice b's Models dialog should
  use it. acceptance-map pins "shows the dry-run count before pruning" (kept).
- Gate: biome, tsc, sessions/chat vitest green, web e2e 5/5 (dashboard+chat), tooling.checks ok after test-name fix.
  Full web vitest: 3–6 App/ThemeMenu/Webhooks tests time out under load (same on base 0a7f30a) — integrator must root-cause
  (likely the new fonts/theme provider slowing App renders in jsdom), not just rerun.
## w52b (web Analytics, Models, Logs) — base 0a7f30a, clean scan
- Pages rebuilt; new lib/useTokenAnalyticsFlag.ts (App uses it), ui/DataTable.tsx, ui/ModelPickerDialog.tsx (shared,
  self-contained, onApply({provider, model})); controls.tsx: CardHeader subtitle/below, PeriodButtons, RefreshButton,
  PageSpinner (w52d also adds PageSpinner + CardHeader subtitle — integrator dedupes); foundation note appended.
- TWO model pickers exist: w52a chat/ModelPickerDialog.tsx and this ui/ModelPickerDialog.tsx — integrator keeps ONE
  (ui/ version, shared) and points chat at it, keeping w52a's fuzzy highlight/current tags/notice where better.
- Decisions W52-b-1..7 (Logs auto-refresh off; acceptance title kept). Gate: biome, tsc, vitest 308/308 at low load,
  web e2e 8/8, tooling.checks ok. Backend follow-ups: usage api_calls + by_model, per-provider model list.
## w52c (web Cron, Skills, Plugins, MCP) — base 0a7f30a, clean scan
- Pages rewritten + new pages/cron/{JobModal,Blueprints}.tsx, pages/mcp/AddServerModal.tsx,
  pages/skills/{HubView,Toolsets,SkillDialogs}.tsx; lib/api.ts + restTypes.ts additions (cron profile/blueprints/skills,
  plugin force); testing/mockApi.ts blueprints route.
- Shared: controls.tsx Badge `secondary` (also w52d — dedupe), SectionHeading (also w52d — dedupe), Segmented `fill`;
  Dialog.tsx `narrow` size + Modal description (w52a also adds Modal props — merge).
- Decisions W52-c-1..13. Gate: biome, tsc, page vitest 40/40, tooling.checks ok. Full-suite App/Webhooks timeouts same as
  base. e2e auth.spec.ts:76 (stale token reload) failed in this run — integrator: check on base before blaming.
## w52e (web Profiles, Profile builder, Config + YAML) — base 0a7f30a, clean scan, all 5 slugs done
- New pages/profiles/*, pages/config/*, lib/yaml.ts (+test), ui/Menu.tsx (foundation dropdown); api.ts statusOf,
  config.defaults, hubInstall profile; mockApi config defaults; routes.ts builder title; dashboard e2e Config clicks Agent.
- Shared: Badge `secondary` (also w52c/d — dedupe). Decisions W52-pc-1..10, W52-e-1..6 (similarity guard reshapes).
- Note for integrator/foundation: reference buttons are UPPERCASE; foundation Button is mixed case — switch the
  foundation's button text style to uppercase (parity) and check tests that match by name (case-insensitive queries).
- Gate: tsc, biome, tooling.checks ok, e2e dashboard 3/3, web vitest 306/306 at maxWorkers=2.
## w52g (desktop transcript) — base 993f0e9 (main + w51b), clean scan; 9/10 slugs done (usage-stats N/A)
- Transcript, streaming, tool rows (incl. rejected; status "blocked" counts as failed), expanded rows, approval card
  (Reject · Always allow ▾ · Run ↵; arming + data-choice kept), error card (raw failure in Details), top-centre toasts via
  notify() (app/Toasts.tsx), no-provider "Backend needs setup" in the status bar, NoticeStrip.
- Shared: i18n/en.ts spread, styles.css toast/tool rules moved to chat/transcript.css, deskTokens `attention`,
  ui/components MenuItem tone/data, Shell mounts ToastRegion, Composer restored-draft uses NoticeStrip (may conflict with
  w52h), e2e helpers click "Run". Remaining: running/approval dots on tab + sidebar row, single Stop button (composer),
  clamp-expand, swipe-to-reject. Electron e2e NOT run — integrator must run it. Decisions W52-g-1..9.
- Gate: desktop vitest 688 (full), tsc, biome, tooling.checks ok. Compare PDF scratchpad/w52g/compare.pdf.
## w52f (web System, Console, Files, Docs, Login, error state) — base 0a7f30a, clean scan, all slugs done
- New pages/system/frame.tsx, console/consoleSession.ts + consoleTerminal.ts (xterm console, fixes `help` wrap),
  pages/DocsPage.tsx (in-app /docs page, fixes direct-load collision), login_page.py restyle (CSP unchanged).
- Shared: routes.ts /docs row, Sidebar docs icon (external link removed), plugins/slots docs, controls.tsx QueryState
  `what` + ErrorState restyle, ActionLog restyle, format.ts formatSize/formatDateTime.
- Decisions W52-docs-1, lang-1, sys-1/2, files-1, login-1, f-1/2. Gate: tsc, biome, vitest 300/301 (App route-table
  timeout also on base), e2e 13/13, auth pytest 194, ruff, tooling.checks ok. Backend follow-ups F1 (host facts), F3 (docs).
## w52h (desktop composer menus, palette, switcher, sidebar rows) — base 993f0e9, clean scan, 10/10 slugs
- New chat/composer/{CompletionDrawer,ComposerHelp,AttachMenu}.tsx (+css/strings), app/SessionSwitcher.tsx (+test),
  sidebar/SessionRowMenu.tsx. Bug fixes: `@` mention (client stripped the @ before complete.path; cwd now sent),
  attach control wired (none before). Ctrl+K palette, Ctrl+Tab switcher, pane tabs Ctrl+PageUp/Down.
- Foundation additions in ui/components.tsx + ui.css (MenuPanel, ContextMenuAt, MenuLabel, MenuSeparator, MenuRow,
  FloatingHud, keyCaps, useHoverTip, SmallDialog; .desk-menu tightened). w52g also edits ui/components (MenuItem tone)
  and Composer.tsx (NoticeStrip) and styles.css — merge both. Shared: i18n/en.ts, settings/keybindings.ts, strings.
- Decisions W52-h-1..8. Electron e2e NOT run; late fixes not re-captured. Desktop vitest all groups pass.
## w52m (desktop panes, HUD, quick entry, onboarding) — base 993f0e9, clean scan, all slugs built
- Right-side group (presets.ts openInSideGroup/sideTree), Files/Review without tab strips (#pane-tab-* ids kept),
  Review header/rows/diff strip/ship bar, Terminal chrome, HUD 620x320, quick entry 640x168, onboarding 720px card with
  "Other providers" and key tile grid. Shared: ui/components PanelLabel; Shell Alt+B; strings blocks.
- Decisions W52-m-1..10. Gate: tsc, biome, desktop vitest 1706/1706, tooling.checks ok, e2e surfaces 6/6, boot 4/4.
  UNCONFIRMED: chat.electron.test.ts (custom endpoint behind "Other providers"; e2e updated, not re-run) — integrator runs it.
- Kits: never delete /tmp/indus-e2e-* globally (it broke parallel builders); use a private TMPDIR under $S.
