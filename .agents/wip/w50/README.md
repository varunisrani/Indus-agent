## w50e (desktop identity follow-ups) — 12 files, base 7eee12d, clean scan
- Runtime identity from the build stamp's channel (`electron/identity.ts`): canary = `Indus Canary`, Linux launcher id
  `ai.indus.desktop.canary` (own .desktop + icon; stable untouched); staged `desktopName` per channel.
- Windows updates: `verifyUpdateCodeSignature: true`, `win.signtoolOptions.publisherName` from
  `packaging/msix/identity.json`; `dist-win.mjs` refuses a SIGNED build while the publisher is `OWNER-TO-FILL`.
- Decisions W50-e-1..4 (update owner on Windows stays `manual` until a signed run). Integrator: update the D-16.1122 row
  and W49-b-6 text (Windows verification now on).
- Builder gate: biome, tsc, desktop vitest 1676 passed, tooling.checks ok, pytest contract/packaging/ci/release 2741 passed.
