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
