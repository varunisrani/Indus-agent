# Task: Registry (P03-02) Phase: 03 Owner: P03-A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `providers/__init__.py`
(`register_provider` 122, `get_provider_profile` 192–233, `list_providers` 260),
`tests/providers/test_entry_point_discovery.py` (test names only). Closed before writing.
Evidence: none.

What it does (own words): `Registry(scope)` answers "which profile is called X?" for one profile
home. Layers in order: the bundled catalogue, three local-server profiles (`ollama`, `vllm`,
`llamacpp` on their documented loopback ports; `lmstudio` is bundled), then named custom providers
from config `providers:` (an entry with a `base_url` becomes a `custom`-family profile named by its
key; `api_mode`, `key_env`, `models`, `context_length`, `aliases`, `display_name`, `override`), then
plugin profiles added with `register(profile, source)` (P13). Lookup is by name or alias,
case-insensitive, `None` when unknown. A config entry may not shadow a built-in vendor unless it
says `override: true` (then it wins with a warning); local-server profiles may be redefined
freely. `list_providers(include_hidden=False)` is stable: catalogue order, local servers, custom by
name, plugins in registration order. The config layer is rebuilt when the `providers:` section's
signature changes. Building a registry also registers provider key names with the `.env` loader.

Inputs → outputs: scope (config view) → `lookup(name)`, `list_providers()`, `register(...)`,
`source_of(name)`.

Files: `src/indus/providers/registry.py`, `tests/unit/providers/test_registry.py`.

Edge cases / failure modes:
- `providers.<name>` entries without `base_url` are settings for an existing provider
  (timeouts, per-model context lengths), not new profiles; unknown names without a URL warn.
- Bad entries (non-mapping, bad URL, bad name, bad `key_env`, bad api_mode) are skipped with a
  warning naming the key; the rest of the registry still loads.
- `api_mode` missing → detected from the URL; a `responses` hint falls back to chat completions.
- An inline `api_key` in config is ignored with a warning (secrets live in the store or `.env`).
- Plugin `register` of a name already taken by a built-in or custom profile without
  `override=True` raises `ProviderConflict`.

Our design differences / decisions:
- An object per scope instead of process-wide dicts and per-home layers; no plugin directory or
  entry-point discovery (P13 calls `register`).
- Local servers are distinct profiles with default URLs, not aliases of `custom`.

Tests we will write: alias and case lookup; unknown → None; config `myllm` appears as a chat
profile with derived host; key_env and per-model context reach the profile; shadowing refused
without `override`, accepted with it (warning); local server redefined freely; config change →
refreshed; stable order; hidden filtering; plugin register conflict; managed key names registered.

## Decisions for the lead
- Config shape accepted for `providers.<name>`: `base_url`, `api_mode`, `key_env`, `models`
  (mapping id → settings, or a list of ids), `context_length`, `aliases`, `display_name`,
  `override`, plus the timeout keys P03-08 reads. The schema keeps `providers` free-form
  (`schema_for("providers.x.y")` is `None` by test); validation is done here with warnings.

Compare checklist (filled after the rewrite; reopened the registry functions):
- [x] lookup by name or alias (test: `tests/unit/providers/test_registry.py::test_lookup_by_alias_and_case`)
- [x] named custom providers from config fall back to the custom wire policy (test:
  `::test_config_custom_provider_appears`)
- [x] later registrations replace earlier ones only when allowed (tests: `::test_shadowing_needs_override`,
  `::test_plugin_register_conflict`)
- Deliberate differences: shadowing a built-in needs `override: true` (the reference lets any later
  registration win silently); registry per scope; config-signature rebuild instead of re-stat'ing
  plugin directories.
- Not taken: entry-point discovery, `custom:<route>` names, the legacy `custom_providers` list,
  `enabled: false` blocks (P07), vision tool-message routing for aggregators (P08).

## Status

Done (2026-10-18).
