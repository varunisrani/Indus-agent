# Wave 58 (lead: Agent 2) — gaps our existing backend can close

## w58j1.patch — desktop (J1), base d8cc714 — PARKED (lead gate with H1 running)
- `/api/analytics/usage` adds `api_calls` (per day + totals) and `by_model` from the existing store; Command center ›
  Usage shows them. Tools tab opens on Terminal so the existing Execution backend picker shows. MoA "no presets" was the
  capture's config (no `moa` section), no code change. Row menu "Open in terminal" (folder rows, local, Advanced mode).
- Left out (no backing): Disable F12 DevTools, Always open links externally, Move to project.
- Gate (builder): desktop vitest 184 / 1857; Electron 43 / 2 skipped; full pytest 18784 passed / 0 failed; pyright 0;
  ruff clean; tooling.checks ok.
