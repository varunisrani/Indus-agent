# Wave 58 (lead: Agent 2) — gaps our existing backend can close

## w58j1.patch — desktop (J1), base d8cc714 — LANDED with D-16.1279
- `/api/analytics/usage` adds `api_calls` (per day + totals) and `by_model` from the existing store; Command center ›
  Usage shows them. Tools tab opens on Terminal so the existing Execution backend picker shows. MoA "no presets" was the
  capture's config (no `moa` section), no code change. Row menu "Open in terminal" (folder rows, local, Advanced mode).
- Left out (no backing): Disable F12 DevTools, Always open links externally, Move to project.
- Gate (builder): desktop vitest 184 / 1857; Electron 43 / 2 skipped; full pytest 18784 passed / 0 failed; pyright 0;
  ruff clean; tooling.checks ok.

## w58j2.patch — web (J2), base d8cc714 — LANDED with D-16.1280
- In-app /docs renders docs/user guides (bundled, safe markdown, mkdocs nav; Docker/Nix copy docs/user + mkdocs.yml);
  `GET/PUT /api/config/raw` (masked read; validated replace through config.save, 422 lists problems, nothing partial;
  write_scope + managed 403 + session gate); `POST /api/skills/create` through the skill_manage handler (write approval
  honoured); `/api/model/options` rows carry `sign_in` (OpenRouter browser PKCE listed with its CLI command).
- Gate (builder): web vitest 42 / 398; web Playwright 17/17; full pytest 18794 passed / 0 failed; ruff, pyright 0,
  biome, tsc, tooling.checks ok; perf not run (lock queue).
- Merge with H1 (DocsPage.tsx): J2's page kept (theme tokens throughout); H1's no-fixed-colour test adapted to it.

## Lead gate on main + J1 + J2 (land4)
ruff, pyright 0, lint-imports, codegen, tooling.checks, notices, biome, typecheck, ipc ok; vitest 302 / 3300; Electron 43 / 2 skipped; web Playwright 17/17; pytest 18795 passed / 0 failed. Perf (confirmed after landing): 18 passed, 1 skipped, 18934 deselected in 49.12s 
