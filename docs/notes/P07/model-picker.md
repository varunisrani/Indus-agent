# Task: `indus model` picker (P07-04) Phase: 07 Owner: P07 slice A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/main.py`
(`select_provider_and_model`, `_resolve_active_provider`, `_pick_provider`),
`hermes_cli/main_provider_setup.py` (picker rows, key prompt keep/replace, custom endpoint,
reasoning after a pick, stale `OPENAI_BASE_URL` clean-up), `hermes_cli/models.py` (provider model
cache: file, TTLs, fallback rows, stale-while-revalidate only), `hermes_cli/providers.py`
(skimmed for named custom provider lookup). Research `04` §3.17, trap R04.9. Closed before
writing.
Evidence: none.

What it does (own words): `indus model [--refresh]` changes the saved chat model. It needs a
terminal (otherwise it prints how to do it with `indus config set` and exits 1). `--refresh`
clears the model-list cache first. It shows the current model and the active provider, resolved
as: a saved custom provider whose URL equals `model.base_url` → the configured provider (config,
then `INDUS_INFERENCE_PROVIDER`) → auto-detection; an unknown name warns and falls back to
auto-detection; nothing at all prints a friendly "no provider is set up yet" line. Then a list:
hosted providers from the P03 registry (D-16.42 set) in catalogue order, local servers as one
group row (LM Studio, Ollama, vLLM, llama.cpp), saved custom providers, "Custom endpoint",
"Reasoning effort only", "Leave unchanged". For the chosen provider: ask for its key when none
is usable (masked; stored through `SecretStore`, so the keychain by default), or offer to keep or
replace an existing one; providers that declare a sign-in flow run it instead (seam, none
declared yet); list models live from `/v1/models` when the profile supports it (cached), else
ask for the id; custom endpoint asks the base URL, an optional key and auto-detects the model
(one model → taken; several → list). Then the reasoning effort is offered. Saving writes
`model.default`, `model.provider`, and `model.base_url` / `model.api_mode` only when the route
needs them; moving to a named provider drops the old `base_url`/`api_mode` and a stale
`OPENAI_BASE_URL` in `.env` (R04.9). Cancel (Ctrl+C or "Leave unchanged") prints "No change."
and writes nothing — keys typed along the way are only stored when the whole pick is saved.

Cache `<home>/cache/provider_models.json`: rows keyed by provider + base URL, with a credential
fingerprint (a new key invalidates the row); live rows fresh for 3,600 s, then served stale while
a background refresh runs (up to 7 days); curated fallback rows live 60 s and are never served
stale; a failed probe is remembered for 60 s (negative TTL) so a dead endpoint is not hit on every
open.

Inputs → outputs: terminal answers, config, secrets, `/models` → `config.yaml` model section,
the key in the secret store, `.env` cleaned of a stale `OPENAI_BASE_URL`, `agent.reasoning_effort`.

Files: `src/indus/cli/model_picker.py`, `src/indus/cli/model_cache.py`,
`tests/unit/cli/test_model_picker.py`; shared: `src/indus/cli/app.py` (registration line).

Edge cases / failure modes:
- `/models` failing, empty or not JSON → type the model id (default: current model);
- a key prompt left blank on a provider that needs one → "No change." and nothing written;
- a local server's URL kept at the profile default → no `model.base_url` written;
- unreadable cache file → ignored (treated as empty), cache write failure → warning only;
- switching custom → named clears `base_url`, `api_mode` and `.env` `OPENAI_BASE_URL`;
- switching named → custom keeps the URL the user typed.

Our design differences / decisions:
- `pick(...)` returns a `Selection` without saving; `apply(...)` saves it. `fallback add` and
  setup reuse the same picker (the reference temporarily saves and restores the primary route).
- No aux-model menu, no portal or impersonating sign-ins (P2, P3); sign-in flows are a registry
  (`SIGN_IN_FLOWS`) that P07-10's OpenRouter PKCE key mint joins at integration.
- Curated model lists: P03 has none, so "curated" means the provider's model aliases when it has
  some, else typing the id.
- The key of a bare custom endpoint is `OPENAI_API_KEY` (what P03's `custom` profile reads).

Tests we will write:
- respx-mocked `/v1/models` → list → choice saved with key in the secret store;
- cache TTL with a fake clock: fresh, stale served + revalidated, fallback rows 60 s, negative
  probe 60 s, `--refresh` clears, credential change invalidates;
- custom → named clears base URL, api_mode and `OPENAI_BASE_URL`;
- cancel at the provider list and at the key prompt writes nothing;
- non-TTY → guidance, exit 1; active-provider resolution table (named custom by URL, configured,
  env, unknown → warn, none → friendly line).

## Decisions for the lead
- Model-list cache keyed by provider + base URL + key fingerprint, in a new file separate from
  P03's `cache/model_lists.json` (P03's TTL cache stays the context-length source).
- Cancel exits 0 ("No change.").

Compare checklist (filled after the rewrite; reopened `hermes_cli/main.py`,
`hermes_cli/main_provider_setup.py`, `hermes_cli/models.py`):
- [x] active provider order and unknown-provider fallback (test:
  `tests/unit/cli/test_model_picker.py::test_active_provider_table`)
- [x] "Leave unchanged" / cancel → "No change." (test:
  `tests/unit/cli/test_model_picker.py::test_cancel_writes_nothing`)
- [x] existing key: keep or replace (test:
  `tests/unit/cli/test_model_picker.py::test_existing_key_kept_or_replaced`)
- [x] stale `OPENAI_BASE_URL` cleared after leaving custom; kept when staying custom (tests:
  `tests/unit/cli/test_model_picker.py::test_custom_to_named_clears_stale_route`,
  `tests/unit/cli/test_model_picker.py::test_custom_endpoint_detects_its_only_model`)
- [x] reasoning effort offered after a pick, skippable (test:
  `tests/unit/cli/test_model_picker.py::test_live_list_pick_saves_everything`)
- [x] cache: 1 h TTL, credential fingerprint, stale-while-revalidate, a failed fetch never
  replaces a live row (test: `tests/unit/cli/test_model_picker.py::test_cache_ttls`)
- [x] curated fallback rows live 60 s and are never stale-served; negative probe 60 s (test:
  `tests/unit/cli/test_model_picker.py::test_fallback_rows_and_negative_probe`)
- [x] only non-empty live results are cached as live rows (test:
  `tests/unit/cli/test_model_picker.py::test_cache_ttls`)
- [x] `/models` failing → type the id (tests:
  `tests/unit/cli/test_model_picker.py::test_listing_failure_falls_back_to_typing`,
  `tests/unit/cli/test_model_picker.py::test_fetch_errors_are_swallowed`)
- Deliberate differences: picker never saves mid-flow (reference saves per flow, then restores
  for fallback); one flat list with a local-server group instead of vendor groups (our catalogue
  is 14 hosted rows).
- Not taken: the "clear the saved key" option of the key prompt (that is `indus auth remove`,
  P07-08); aux model config, remove-custom-provider row (that is `indus config unset
  providers.<name>`, P07-12), Bedrock/Vertex/Azure flows (P08).

## Status

Done (2026-10-18). Shared file: `cli/app.py`.

## Integration (lead)

- `SIGN_IN_FLOWS` stays the override registry; `sign_in_for(provider)` adds the documented
  key-mint flows of `providers.auth.flows` (OpenRouter), loaded only when a key is asked for so
  `indus model --help` stays light. A failed sign-in prints the reason and changes nothing.
