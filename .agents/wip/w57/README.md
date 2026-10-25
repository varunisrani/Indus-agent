# Wave 57 (lead: Agent 2) — bugs from the second compare

## w57h1.patch — H1, base 383e3f5 — PARKED (lead gate with G1+G2 running)
- Web Docs panel followed fixed light colours → theme tokens. Messaging labels all 153 gateway catalogue keys (no raw env
  names). Error card: Retry whenever the host can resend (was only for "temporary" failures), Open logs on every layer.
  Web model picker lists models for custom/local endpoints via the existing test-endpoint route (typed id fallback).
- "Files column ~110 px" was a misreading of the half-scale sheet: measured 228 px in the app (236/1180 share); a test
  now pins the shares 236 / 380 / 420. Prompt wrap = 110-char working path.
- Gate (builder): desktop vitest 184 / 1839; Electron 43 / 2 skipped (security 12/12); web vitest 40 / 369; web
  Playwright 15; tsc + biome clean; tooling.checks ok.
