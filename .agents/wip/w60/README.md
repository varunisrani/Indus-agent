# Wave 60 (lead: Agent 2) — remaining gaps from the third compare

## w60l1.patch — web model lists + Models page (L1), base 5c110f4 — PARKED
- `/api/model/options` rows carry `models` / `models_source` / `models_checked` from our price table for OpenAI (26),
  Anthropic (14) and Google (10) — only when the provider points at the vendor's own HTTPS host (as pricing does); no
  outgoing request. Picker lists them, typed id stays. Models page: Mixture-of-agents row (deep link Config › moa;
  Config honours `?section=`) and per-model usage cards from `by_model` (tokens/cost only with show_token_analytics).
- Gate (builder): ruff, pyright 0, biome, tsc, tooling.checks ok; web vitest 42 / 408; web Playwright 18; targeted
  pytest 16. Full pytest + perf were queued in its gate (`$S/w60l1-gate.sum`) at report time.
