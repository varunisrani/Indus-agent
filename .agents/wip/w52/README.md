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
