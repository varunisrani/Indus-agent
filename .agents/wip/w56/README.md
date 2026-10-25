# Wave 56 (lead: Agent 2) — more UI parity follow-ups

## w56g2.patch — web themes, profiles skill_count, cron blueprint fields (G2), base 383e3f5 — LANDED with D-16.1276
- Lands AFTER w55f2 (uses `skill_count` and blueprint `fields`). 8 presets (4 light, 4 dark, our own palettes), each
  WCAG-AA tested against the index.css mixes; Clear Blue is a named exception (muted 3.7, soft 2.7 — owner call since it
  follows the reference). Profile cards read `skill_count`. Job form turns text/choice blueprint slots into inputs.
- Gate (builder): web vitest 41 / 383; web Playwright 15; tsc + biome clean; tooling.checks ok.
- Follow-ups: route could send prompt/schedule templates; desktop job form could reuse the slot rule.

## w56g1.patch — desktop window prefs + session Export (G1), base 383e3f5 — LANDED with D-16.1277
- App actions Left/Right (band cluster side), File browser (open Files once per new workspace folder), Floating composer
  (grip, drag/keys, docks when off) as real desktop prefs; all 8 Window & layout rows live.
- Row menu Export: IPC `indus:sessions:export` (strict `{sessionId}` only; main fetches `/api/sessions/{id}/export`
  through the REST proxy, save dialog over the asking window, JSON Lines like `indus sessions export`, mode 0600).
  Lead review: no renderer-supplied path, id URL-encoded, registry sender check. Save dialog not driven by E2E.
- Gate (builder): desktop vitest 184 / 1853; Electron E2E 43 / 2 skipped; tsc 0; check-ipc + bridge ok; tooling.checks ok.

## Lead gate on F2 + G2 + G1 combined (land3)
tsc web + desktop 0; biome 0 errors; check-ipc + bridge ok; tooling.checks ok; vitest 299 files / 3270 passed; Electron E2E 43 / 2 skipped; web Playwright 15/15. (First run had no per-app node_modules — a lead setup slip, rerun clean.)
