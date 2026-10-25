# Wave 59 (lead: Agent 2) — remaining gaps closed for real

## w59k1.patch — desktop alerts, key actions, rows, reply times (K1), base 65120c2 — PARKED
- Alerts 3 → 6 (approval, input needed, response ready, turn failed, plugin notifications; old prefs migrated). Key
  bindings: stored overrides were never applied — new dispatcher (keyActions.ts) runs New chat, Settings, focus composer
  + 8 new actions; 32 rows in 5 groups. Output limits +file read limit; Voice rows up front + STT device. Reply time +
  Copy from the REST history timestamp.
- Push-to-talk key showed masked: `is_secret_setting` let through `record_key` / `hot_key` / `secret_store`. Lead
  narrowed K1's rule (any `*_store` tail and `push_key` were let through) to those three exact names; test pins that
  look-alikes (`push_key`, `token_store`) stay masked.
- Gate (builder): desktop vitest 188 / 1877; Electron 43 / 2 skipped; full pytest 18784 / 0 failed; tsc, biome, ruff,
  pyright, check-ipc, tooling.checks ok. Lead after the narrowing: ruff, pyright 0, config_env contract tests 15 passed.
