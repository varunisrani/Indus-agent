# Task: Bundled catalogue (P03-03) Phase: 03 Owner: P03-A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/auth.py` registry rows
145–250 (which vendors, which env names — list only), `plugins/model-providers/openrouter/__init__.py`
(which hooks an aggregator needs), `plugins/model-providers/custom/__init__.py` (aliases, no URL).
Closed before writing. Research `04-cli-config.md` §3.16.1.
Evidence: none.

What it does (own words): the provider profiles that ship with the product, in catalogue order —
the order `auto` uses to pick the first provider with a usable key. Key-based vendors only
(D-16.42): `openrouter`, `openai`, `anthropic`, `deepseek`, `xai`, `mistral`, `huggingface`,
`nvidia`, `zai`, `kimi-coding`, `minimax` (Anthropic wire), `alibaba`, `ai-gateway`,
`ollama-cloud`, then the local `lmstudio` (`api_key_optional`, loopback, not auto-detected) and the
bare `custom` (no URL, generic `OPENAI_API_KEY`, not auto-detected). Every base URL and env name is
a third-party fact checked against the vendor's docs on 2026-10-18 (table in the module). Also
`register_managed_env_names()` so the `.env` loader clears provider keys removed from `.env`.

Inputs → outputs: none → `PROFILES` (tuple), `by_name()`, `env_var_names()`.

Files: `src/indus/providers/bundled.py`, `tests/unit/providers/test_bundled.py`.

Edge cases / failure modes:
- Names and aliases unique across the table (and across the registry's local-server profiles).
- Every URL https except loopback local servers; env names `^[A-Z][A-Z0-9_]*$`.
- No portal provider (D-16.12), no impersonating OAuth routes or tokens (D-16.11, R01.12): the
  Anthropic profile reads `ANTHROPIC_API_KEY` only.

Our design differences / decisions:
- Data module of frozen profiles, no plugin directories.
- `openai` uses `max_completion_tokens` (the vendor's current field); chat completions until P08.
- `OPENAI_BASE_URL` belongs to `custom` only (`base_url_env_var`), never to `openai`: the key is
  host-scoped and a proxy is a custom endpoint.
- Anthropic and MiniMax model listing off for now (their `/models` needs vendor headers; P07).

Vendor facts (checked 2026-10-18; the URL is the page that states them):

| name | base URL | key env | source |
|---|---|---|---|
| openrouter | https://openrouter.ai/api/v1 | OPENROUTER_API_KEY | https://openrouter.ai/docs/quickstart |
| openai | https://api.openai.com/v1 | OPENAI_API_KEY | https://github.com/openai/openai-python (README, `_client.py`) |
| anthropic | https://api.anthropic.com | ANTHROPIC_API_KEY | https://docs.anthropic.com/en/api/overview; SDK README |
| deepseek | https://api.deepseek.com/v1 | DEEPSEEK_API_KEY | https://api-docs.deepseek.com/ |
| xai | https://api.x.ai/v1 | XAI_API_KEY | https://docs.x.ai/docs/api-reference |
| mistral | https://api.mistral.ai/v1 | MISTRAL_API_KEY | https://docs.mistral.ai/api/ |
| huggingface | https://router.huggingface.co/v1 | HF_TOKEN | https://huggingface.co/docs/inference-providers/index |
| nvidia | https://integrate.api.nvidia.com/v1 | NVIDIA_API_KEY | https://docs.api.nvidia.com/nim/reference/llm-apis (host); env from NVIDIA's LangChain integration README |
| zai | https://api.z.ai/api/paas/v4 | ZAI_API_KEY | https://docs.z.ai/guides/develop/openai/python |
| kimi-coding | https://api.moonshot.ai/v1 | MOONSHOT_API_KEY, KIMI_API_KEY | https://platform.moonshot.ai/docs/guide/start-using-kimi-api |
| minimax | https://api.minimax.io/anthropic | MINIMAX_API_KEY | https://platform.minimax.io/docs/api-reference/text-anthropic-api |
| alibaba | https://dashscope-intl.aliyuncs.com/compatible-mode/v1 | DASHSCOPE_API_KEY | https://www.alibabacloud.com/help/en/model-studio/compatibility-of-openai-with-dashscope |
| ai-gateway | https://ai-gateway.vercel.sh/v1 | AI_GATEWAY_API_KEY | https://vercel.com/docs/ai-gateway/openai-compat |
| ollama-cloud | https://ollama.com/v1 | OLLAMA_API_KEY | https://docs.ollama.com/cloud |
| lmstudio | http://127.0.0.1:1234/v1 | LM_API_KEY (optional) | https://lmstudio.ai/docs/app/api/endpoints/openai |

Tests we will write: every profile validates and round-trips; names/aliases unique; https except
loopback; env-name regex; banned names absent from every string field; `lmstudio`/`custom` not
auto-detected; catalogue order starts with openrouter; `register_managed_env_names` registers every
key and base-URL env name (so a stale `OPENAI_BASE_URL` dropped from `.env` is cleared, R04.9).

## Decisions for the lead
- `kimi-coding` points at the Moonshot OpenAI-compatible endpoint (the documented key-based API);
  the separate Kimi coding-plan endpoint can be a second profile when someone needs it.
- `LM_API_KEY` / `LM_BASE_URL` are names LM Studio itself does not define; kept for users coming
  from other tools. Easy to rename.
- Gemini, Bedrock, Vertex, Azure, Fireworks, DeepInfra from D-16.42 arrive with their transports
  or auth in P08; nothing here blocks adding them as rows.

Compare checklist (filled after the rewrite; reopened the auth registry rows and the two plugins):
- [x] key-based vendors from the reference list covered where D-16.42 keeps them (test:
  `tests/unit/providers/test_bundled.py::test_catalogue_names_and_order`)
- [x] `lmstudio` excluded from env auto-detect (test: `::test_local_and_custom_not_auto_detected`)
- [x] local aliases `ollama`, `vllm`, `llamacpp` handled in the registry (P03-02)
- Deliberate differences: no portal, no OAuth-only rows (codex, xai-oauth, qwen-oauth,
  minimax-oauth, copilot), no `CLAUDE_CODE_OAUTH_TOKEN`/`ANTHROPIC_TOKEN` (impersonation path);
  `OPENAI_BASE_URL` only for `custom`.
- Not taken: China-region duplicates (kimi-coding-cn, minimax-cn), stepfun, arcee, gmi, actual,
  opencode, kilocode, xiaomi, tencent, alibaba-coding-plan — add as rows when wanted.

## Status

Done (2026-10-18).
