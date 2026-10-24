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
