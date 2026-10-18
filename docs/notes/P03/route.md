# Task: Route resolution and key lookup (P03-05) Phase: 03 Owner: P03-A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/runtime_provider.py`
(resolution ladder order, requested-provider precedence, stale base-URL drop, host-gated key
candidates), `website/docs/developer-guide/provider-runtime.md` (precedence section, key scoping
section). Closed before writing. Research `01-agent-core.md` §3.8, `04-cli-config.md` R04.9.
Evidence: none.

What it does (own words): `resolve(scope, *, model, provider, base_url, api_mode)` turns what the
user asked for into one `RuntimeRoute{provider, model, api_mode, base_url, api_key, source,
headers}`. Precedence for provider, model, base URL and mode: explicit arguments, then the saved
config (`model.provider/default/base_url/api_mode`), then env (`INDUS_INFERENCE_PROVIDER`,
`INDUS_INFERENCE_MODEL`, and a profile's own base-URL env such as `OPENAI_BASE_URL` for `custom`),
then the profile defaults. Saved config beats stale shell exports. The key is read per the
profile's env names in priority order, store first then the scope's env view; placeholder-shaped
values count as missing. A key is attached only when the route's host is exactly the profile's
host (and a profile on https is never downgraded to plain http off-loopback). `OPENAI_API_KEY` is
the generic key of `custom` and named custom endpoints and is never sent to another bundled
vendor's host. A config `base_url` that sits on another bundled vendor's host while
`model.provider` names this one is dropped with a warning (stale route, R04.9); a config base URL
is ignored when the explicit provider differs from the saved one. `auto` picks a custom endpoint
when a base URL is saved, else the first auto-detectable bundled profile with a usable key.

Inputs → outputs: scope + optional overrides → `RuntimeRoute`; errors `NoUsableProvider`
(`provider.no_usable`, lists the env names to set), `KeyHostMismatch`
(`provider.key_host_mismatch`), `InvalidBaseURL`.

Files: `src/indus/providers/route.py`, `tests/unit/providers/test_route.py`.

Edge cases / failure modes:
- Unknown provider name → `NoUsableProvider` naming it.
- `custom` without any base URL → `NoUsableProvider` saying where to set one.
- `api_key` profile with no usable key → `NoUsableProvider` with its env names.
- `responses` mode (official OpenAI host detected, or asked for) → chat completions with a logged
  note until P08; other future modes → `ProviderError`.
- `repr(route)` and `str(route.api_key)` never show the key.

Our design differences / decisions:
- A short precedence function instead of a ladder of provider-specific rungs; pools, OAuth,
  Bedrock/Vertex/Azure and fallback chains are P07/P08.
- A vendor profile pointed at a foreign host raises `KeyHostMismatch` instead of silently
  sending or silently dropping the key (the user picks `custom` for a proxy).
- No host-derived `<VENDOR>_API_KEY` guessing for custom endpoints.

Tests we will write: precedence table (explicit > config > env > default); config anthropic +
shell `OPENAI_BASE_URL` → anthropic URL; `OPENROUTER_API_KEY` never on `https://my.host/v1`;
`OPENAI_API_KEY=sk-` missing; store beats `.env`/env; stale vendor base URL dropped with warning;
`auto` with only `DEEPSEEK_API_KEY` → deepseek; `repr` masks; placeholder table; generic key
refused on a vendor host; https→http downgrade refused; responses hint falls back; named custom
with `key_env`.

## Decisions for the lead
- `RuntimeRoute` carries two extra optional fields: `profile` (the resolved profile, not part of
  equality or repr) and `warnings` (notes such as the dropped stale URL). P03-B2's
  `fake_route(...)` can leave both at their defaults.
- `Secret` (masked wrapper) lives in `route.py`; `fingerprint()` gives the client-cache key.
  Transports read `route.profile` and `route.api_key.reveal()` (lead ruling for P03-B1).

Compare checklist (filled after the rewrite; reopened the ladder, `resolve_requested_provider`,
`_config_base_url_for_provider`, `_host_gated_env_key_candidates`, the precedence doc):
- [x] explicit → config → env → defaults (test: `tests/unit/providers/test_route.py::test_precedence_table`)
- [x] config wins over `INDUS_INFERENCE_PROVIDER` and stale `OPENAI_BASE_URL` (tests:
  `::test_config_provider_beats_env`, `::test_config_anthropic_ignores_shell_openai_base_url`)
- [x] base URL only used for the provider it was saved with (test: `::test_explicit_provider_ignores_other_providers_base_url`)
- [x] aggregator key only to its own host (test: `::test_openrouter_key_never_sent_to_custom_host`)
- [x] local custom endpoint with provider auto does not pick up a cloud key (test: `::test_auto_with_saved_base_url_is_custom`)
- Deliberate differences: mismatched vendor host raises instead of silently picking another rung;
  no host-derived key guessing; no credential pools, OAuth, portal or external processes.
- Not taken: `providers.<name>.enabled` guard (P07), MoA / Azure / Vertex / Bedrock shortcuts
  (P08), `model.openai_runtime` overlay (not planned), credentialless-custom special errors
  beyond `NoUsableProvider`; per-vendor key-prefix validation (a wrong vendor's key in the
  right variable) — P07 setup can add it.
- Placeholder rule: minimum length 8 (task row) instead of the reference's 4; `xxx`, angle
  brackets, unexpanded `${...}`, single repeated characters and fill-me-in words also count as
  missing (test: `::test_placeholder_table`).

## Status

Done (2026-10-18).
