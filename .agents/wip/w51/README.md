## w51a (web UI foundation, parity) — 43 files, base 511b156, clean scan
- Tokens/presets in apps/web (canvas+ink mixed in CSS; presets Deep Teal (default), Deep Teal Large, Clear Blue, Graphite;
  data-theme still indus-light/dark), type-role utilities, open fonts (Archivo, EB Garamond, JetBrains Mono, Courier Prime
  via @fontsource), shell (wordmark, icon nav, collapsed rail — fixes clipped labels, footer, theme menu, EN menu),
  PageHeader with HeaderAfterTitle/HeaderActions, components in src/ui/ (see docs/notes/ui-parity/web-foundation.md),
  Sessions page rebuilt as the worked example.
- Packages: lucide-react@1.52.0 (ISC); @fontsource-variable/{archivo,eb-garamond,jetbrains-mono}@5.3.0,
  @fontsource/courier-prime@5.3.0 (OFL-1.1). OFL-1.1 added to licenses_allow.json + licence text; notices regenerated.
  pnpm-lock.yaml changed (lead-owned: integrator re-verifies `pnpm install --frozen-lockfile --offline`).
- Decisions W51-a-1..6. Gate: biome, tsc, web vitest 295, web e2e 14, tooling.checks ok, notices --check ok.
- Compare PDF: scratchpad/w51a/compare.pdf (15 screens). Close match on sessions/shell in both themes.
## w51b (desktop UI foundation, parity) — 49 files, base 511b156, clean scan
- deskTokens.ts (--desk-* light/dark measured; --indus-color-* mapped; dark primary text #151617), Inter + Abril Fatface
  (@fontsource 5.3.0, OFL-1.1), 34px title band, sidebar (Sessions|Bots tabs, New session, Capabilities, Messaging,
  Artifacts, Scheduled jobs, PINNED/SESSIONS, footer row + More menu), chat-only default layout, new-chat wordmark +
  composer bar with model/effort chips, transcript base, settings shell, app/overlay/OverlayFrame.tsx (overlay card,
  nav+breadcrumb, list-and-detail), settings/primitives.tsx (+ PageDef.curated "More settings" fold).
- Bugs fixed: model chip, light-theme terminal, double clear button. Decisions W51-b-1..11.
- Lockfile + licenses_allow.json + OFL text + notices also changed by w51a: integrator merges package.json changes, then
  regenerates pnpm-lock (pnpm install with the proxy), notices (tool) — never hand-merge generated files.
- Gate: biome (3 CSS order warnings), tsc, desktop vitest 1698, Electron e2e 31/31, tooling.checks ok.
- Compare PDF: scratchpad/w51b/compare.pdf. Capture kit pane steps need aria-label lookup (follow-up).
