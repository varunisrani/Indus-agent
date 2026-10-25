# Wave 56 (lead: Agent 2) — more UI parity follow-ups

## w56g2.patch — web themes, profiles skill_count, cron blueprint fields (G2), base 383e3f5 (main be4bf77 + w55f2) — PARKED
- Lands AFTER w55f2 (uses `skill_count` and blueprint `fields`). 8 presets (4 light, 4 dark, our own palettes), each
  WCAG-AA tested against the index.css mixes; Clear Blue is a named exception (muted 3.7, soft 2.7 — owner call since it
  follows the reference). Profile cards read `skill_count`. Job form turns text/choice blueprint slots into inputs.
- Gate (builder): web vitest 41 / 383; web Playwright 15; tsc + biome clean; tooling.checks ok.
- Follow-ups: route could send prompt/schedule templates; desktop job form could reuse the slot rule.
