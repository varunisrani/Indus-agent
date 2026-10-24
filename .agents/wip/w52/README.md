## w52d (web Channels, Webhooks, Pairing, Keys) — 11 files, base 0a7f30a (main + w51a), clean scan
- Pages rebuilt on the foundation; shared additions in ui/controls.tsx (SectionHeading, EmptyCard, PageSpinner, CodeChip,
  Callout; Badge `secondary`, Button `danger-outline`; CardHeader subtitle; Card id) and ui/CopyButton.tsx (`look`);
  ModelsPage.tsx: `export function DeviceLogin` only (slice b rewrites ModelsPage — keep the export when merging).
- Decisions W52-d-1..6. Integrator: light-theme warning token → reference's pale yellow (shared token, owner wants parity).
- Gate: biome, tsc, web vitest 298, web e2e 14, tooling.checks ok. Compare PDF scratchpad/w52d/compare.pdf.
