# Wave 60 (lead: Agent 2) — remaining gaps from the third compare

## w60l1.patch — web model lists + Models page (L1), base 5c110f4 — PARKED
- `/api/model/options` rows carry `models` / `models_source` / `models_checked` from our price table for OpenAI (26),
  Anthropic (14) and Google (10) — only when the provider points at the vendor's own HTTPS host (as pricing does); no
  outgoing request. Picker lists them, typed id stays. Models page: Mixture-of-agents row (deep link Config › moa;
  Config honours `?section=`) and per-model usage cards from `by_model` (tokens/cost only with show_token_analytics).
- Gate (builder): ruff, pyright 0, biome, tsc, tooling.checks ok; web vitest 42 / 408; web Playwright 18; targeted
  pytest 16. Full pytest + perf were queued in its gate (`$S/w60l1-gate.sum`) at report time.

## w60l2.patch — desktop tool keys + tool access (L2), base main — LANDED with D-16.1284
- Builder gate: biome, ruff, pyright 0, tsc, check-ipc, tooling.checks ok; desktop vitest 189 / 1887; Electron 43 / 2
  skipped; full pytest 18800 passed / 0 failed. (Builder report not received; tree unchanged after its gate.)

## Landing L1 + L2 (lead, fast path on the owner's request: no new agents)
- Combined tree main + L1 + L2: ruff + format, pyright 0, lint-imports kept, biome 0 errors, tsc desktop + web,
  check-ipc, tooling.checks ok; targeted pytest (models, cron/skills, config/env routes, key_needs) 64 passed.
  L1 landed with D-16.1283 on its own web lanes (vitest 408, Playwright 18) + the above; the full pytest on the combined
  tree runs after landing as confirmation.

- Confirmation after landing: full pytest on main + L1 + L2: 18801 passed / 0 failed / 134 skipped.
