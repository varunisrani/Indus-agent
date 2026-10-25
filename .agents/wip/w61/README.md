# Wave 61 (lead: Agent 2) — OpenAI ChatGPT sign-in (owner decision D-16.1282)

## w61m1.patch — backend + CLI + dashboard REST (M1), base b26c004 — PARKED (lands with M2)
- `openai-codex` (alias `chatgpt`) provider on the ChatGPT Codex backend (Responses wire, streamed); browser PKCE on
  127.0.0.1:1455 (CLI only) + OpenAI's device code; tokens only in the secret store, renewed ahead of expiry under a
  per-credential + cross-process lock; `indus auth add openai-codex [--device]`, `auth status`, `auth logout`; dashboard
  REST `/api/providers/login/{start,<id>}` + `/accounts/{provider}` (status, sign-out) — no token, device id or
  verifier in any answer. Not built (P2 stands): ~/.codex/auth.json, tool renames, any Anthropic route.
- Gate (builder, run 2): pytest 18867 passed / 0 failed; perf 18 / 0 failed (run 1's first-token regression fixed by
  keeping httpx off the start-up path); ruff, pyright 0, tooling.checks ok.
- Lead review: loopback bind 127.0.0.1, state check, no token in responses/logs, no credential literals. On main + M1:
  one import union in test_profiles_models_routes.py; ruff clean; sign-in + docs tests 113 passed; route list fresh.
- Owner item: 4 early builder commands ran without the ENV RULE — uv may have written cache under the real ~/.cache.

## w61m2.patch — "Sign in with ChatGPT" UI (M2) — PUSHED with M1 on the owner's request
- Builder: web vitest 43 / 413; desktop vitest 189 / 1900; Electron 44 / 2 skipped; tsc, check-ipc, tooling.checks ok;
  its lint error fixed in-tree; web Playwright run was still in progress.
- Lead on main + M1 + M2: one import union in apps/web/src/pages/ModelsPage.tsx (L1's Layers + M2's icons); biome 0
  errors, tsc web + desktop, check-ipc, bridge types, ruff, route list, ModelsPage + EnvPage tests 25 passed,
  tooling.checks ok. NOT run on the combined tree (owner said "just push, I will do the rest"): full pytest, perf,
  full vitest, Electron lane, web Playwright.
