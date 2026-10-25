# Wave 54 (lead: Agent 2) — UI parity fixes from the side-by-side compare

## w54w1.patch — web (W1), base 7b59d33 (= main bc44020 code) — LANDED with D-16.1270
- Analytics chart bug root-caused (one flex column per listed day → one solid block); sessions land on History; real
  Import sessions dialog (P24-28 routes); D-16.1208 uppercase buttons reversed from the screenshots; focus outline in
  `@layer base`; measured dialog scrims; cron error banner; Clear pending live; console light strip.
- Gate (builder): biome + tsc clean; web vitest 40 files / 364 passed; web Playwright 15/15 (before a unit-test-only
  fix); tooling.checks ok (builder and lead on main).
- Left: chat-exchange tool row (drawn by apps/tui in xterm) → follow-up; theme presets 4 → 8.

## w54d2.patch — desktop settings + page states (D2), base 7b59d33 — LANDED with D-16.1271
- Root-caused bugs: password managers "On" from the vault_sources wish list (now PATH detection in main, IPC
  `indus:vault:detect`); keychain switch showed the saved choice (now `effective`); red voice error from starting the
  wake word before the connection (now waits). Uninstall names the missing proof; dev-run licences; persona editor;
  Profiles opens selected; active endpoint card; composer fixed key chips.
- Gate (builder): tsc 0; check-ipc ok; desktop vitest 181 files / 1809 passed; Electron E2E 43 passed / 2 skipped;
  tooling.checks ok. Left: cron blueprint catalogue (backend: serve the 4 built-ins from /api/cron/blueprints), 5 extra
  desktop alerts (no backing), Profiles skills count (no API field).
