# Task: Context length and model listing (P03-17) Phase: 03 Owner: P03-A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/model_metadata.py`
(`get_model_context_length` resolution order ~2215–2400, the cache save/get/validate functions,
the fallback warning, the context-key list, overflow parsers 1221–1400 for which phrasings exist),
`providers/base.py` `fetch_models` (models URL choice), `tests/agent/test_model_metadata.py` and
`tests/providers/test_fetch_models_base_url.py` (test names only). Closed before writing.
Evidence: none.

What it does (own words): `resolve(route, *, scope, list_models)` decides how many tokens the model
behind a route can hold, in this order: a config override (`model.context_length` for the saved
default model, or `providers.<name>.models.<id>.context_length`); the profile's
`context_length(model)` hook or its hint; the persistent cache `<home>/cache/context_lengths.json`
(only values a provider confirmed are stored, keyed by base URL + model, checked for sanity on
read); the live `/models` list for custom and local endpoints (`context_length`, `max_model_len`,
`context_window`), stored in the cache when found; our own family table (longest key contained in
the model id; values from vendor docs, cited in the module); else 256,000 with one warning per
(model, base URL). Anything below 64,000 raises `ContextTooSmall` (A35). `remember(...)` stores a
provider-confirmed value (used by P08 after an overflow error). `parse_overflow_limit(message)`
pulls the stated window out of overflow errors and ignores output-cap messages.

`list_models(route, *, client, scope, cache_ttl, refresh)` does `GET {models_url or base_url}/models`
(10 s timeout; models URL only when the route uses the profile's default base URL), caches per base
URL in `<home>/cache/model_lists.json` for `model_catalog.ttl_minutes` (20) of clock time, and turns
any failure into an empty list with a logged reason. Anthropic-wire routes use `/v1/models` with
the vendor's header shape.

Inputs → outputs: route + scope (+ lister) → `ContextWindow{tokens, source}`; message → int|None;
route + client → `[ModelInfo{id, context_length?, owned_by?}]`.

Files: `src/indus/providers/context_length.py`, `src/indus/providers/model_list.py`,
`tests/unit/providers/test_context_length.py`, `tests/unit/providers/test_model_list.py`; shared:
`src/indus/foundation/config/schema.py` (`model_catalog.ttl_minutes`).

Edge cases / failure modes:
- Corrupt cache files are ignored (logged), never fatal; out-of-range values dropped on read.
- Blank model id → fallback without caching; the family table is never consulted for it.
- `model.context_length` is ignored when the saved default model differs from the route's model
  (a stale override for another model).
- Model list: non-JSON body, non-2xx, transport errors, `supports_model_listing: false` → `[]`.

Our design differences / decisions:
- One short ordered function; no aggregator catalogue, models.dev, Bedrock probes, Ollama
  `/api/show`, probe tiers or endpoint metadata layers (P07/P08 add what they need).
- Cache entries carry their source and time; only `endpoint` / `provider-error` sources are written.

Tests we will write: order table (config beats all; hook; cache only for confirmed values;
`/models` with `max_model_len: 32768` → `ContextTooSmall`; family table; unknown → 256,000 with one
warning across two calls); corrupt cache ignored; overflow parser ≥ 10 phrasings plus output-cap
negatives; model list cached for 20 min of fake time then refetched; 500 → `[]`; models URL choice;
Bearer only when a key exists.

## Decisions for the lead
- `resolve` is async because the `/models` step does I/O; P04/P07 call it once per model switch.
- Config schema gains `model_catalog.ttl_minutes: 20` (shared file, one new section).

Compare checklist (filled after the rewrite; reopened the resolution order, cache and parsers):
- [x] config override first (test: `tests/unit/providers/test_context_length.py::test_config_beats_everything`)
- [x] only provider-confirmed values persisted (test: `::test_family_and_fallback_are_not_cached`)
- [x] live `/models` for custom endpoints (test: `::test_local_models_small_window_refused`)
- [x] longest-key family match, 256K fallback warned once (tests: `::test_family_table_longest_key`,
  `::test_unknown_model_fallback_warns_once`)
- [x] 64K minimum (test: `::test_local_models_small_window_refused`)
- [x] output-cap messages not read as context windows (test: `::test_overflow_parser_ignores_output_caps`)
- [x] custom base URL beats the profile's models URL (test:
  `tests/unit/providers/test_model_list.py::test_models_url_only_for_default_base`)
- Deliberate differences: async; cache keyed `base_url|model` with source; no probe tiers.
- Not taken: aggregator/models.dev metadata, Anthropic `/v1/models` context lookup, Bedrock,
  Codex tables, local server probes beyond `/models`, 32K under-report heuristics.

## Status

Done (2026-10-18). Shared change: `model_catalog` section in `foundation/config/schema.py`.
