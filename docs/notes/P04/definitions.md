# Task: Definitions + schema hygiene (P04-10) Phase: 04 Owner: wave-5 builder D

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `model_tools.py`
(`get_tool_definitions`, `_tool_defs_cache_key`, `_compute_tool_definitions`, the dynamic schema
rewriters and `_apply_dynamic_schemas`), `tools/registry.py` (`get_definitions`, per-pass probe
memo), `tools/schema_sanitizer.py` (whole). Test names of `tests/tools/test_schema_sanitizer.py`,
`tests/tools/test_schema_boolean_required.py`. Research `02-tools.md` §3.1, §3.1a.
Closed before writing.
Evidence: none.

What it does (own words): turns a selection of tool names into the tool array a provider call
carries. Unknown names and tools whose probe says no are dropped; each surviving tool becomes
`{type: "function", function: {name, description, parameters}}` sorted by name, patched by its
`schema_overrides`; sentences that name other tools are added only when those tools survived
(the model otherwise calls tools that do not exist, R02.21); then a sanitiser fixes, on a deep
copy, only the schema shapes the target route rejects. The result is memoised, but every caller
gets its own list and its own entry dicts, so a caller appending a tool never poisons the next
call (R01.13).

Inputs → outputs: `definitions(registry, selection, route=None, *, scope=None) -> list[dict]`;
`sanitize(tools, quirks) -> list[dict]`; `quirks_for(route) -> frozenset[Quirk]`;
`property_key_renames(props) -> dict[str, str]`; `resolve_ref(node, root) -> node`.

Files: `src/indus/tools/definitions.py`, `src/indus/tools/schema_sanitize.py`,
`tests/unit/tools/test_definitions.py`.

Edge cases / failure modes:
- probes run once per distinct probe per pass.
- `schema_overrides` raising or returning a non-mapping → static schema, warning.
- a `rewrite` hook may return None to drop a tool for this selection (e.g. a host-exec tool when
  the session has no terminal).
- sanitiser quirks: object without `properties`; `type` arrays and bare-string schemas;
  top-level nullable `anyOf`/`oneOf`; `default` beside `$ref`; top-level combinators
  (`allOf/anyOf/oneOf/enum/not`); property keys outside `[A-Za-z0-9_.-]{1,64}` (deterministic
  rename, collisions get `_2`, `_3`…, `required` follows); legacy boolean `required` lifted into
  the parent list; `default`/`const`/`enum`/`examples` values never walked as schemas; large
  `maxLength` removed for grammar-based local servers.
- missing or non-dict `parameters` → empty object schema.

Our design differences / decisions:
- Route-aware: `quirks_for(route)` picks the fixes; no route → every fix (safe default for
  golden fixtures). Always: malformed shapes (bare strings, boolean `required`, missing
  parameters). Local / custom endpoints: object properties, `type` arrays, big `maxLength`.
  `gemini`: `type` arrays. Anthropic wire / bedrock: top-level nullable unions, property keys.
  `responses`: top-level combinators. Hosts named like fireworks: `default` beside `$ref`.
- Memo key = (available names, registry generation, profile key, quirks); the probe verdicts are
  part of the key, so a probe flip shows up on the next call (the reference keys on selection and
  serves a stale list until generation changes).
- Cross-references are data on the entry (`cross_refs=(CrossRef(tool, text), …)`) plus an
  optional `rewrite(function_dict, available)` hook; no per-tool if-chain in definitions.
- Two-level copies (entry dict and its `function` dict) on every return.
- Const-union collapse and reactive strips (pattern/format, slash enums) are MCP / recovery
  concerns, left to P13 / P03.

Cross-slice names needed: none (uses `indus.providers.route.RuntimeRoute`, landed).

Tests we will write:
- aliasing: appending to / mutating a returned list or entry never changes the next result.
- sorted, unknown skipped, unavailable skipped, probe once per pass, overrides merged, overrides
  raising ignored.
- cross-ref sentence present only when the named tool survives; rewrite returning None drops.
- sanitiser table, one case per quirk; quirks only applied for the routes that need them;
  original input never mutated; golden: same input → identical JSON across runs.

## Decisions for the lead
- Quirk → route mapping as above; adjust in `quirks_for` when a provider proves otherwise.

Compare checklist (filled after the rewrite; reopened `model_tools.py`, `tools/registry.py`,
`tools/schema_sanitizer.py`):
- [x] sorted, unknown and unavailable skipped, one probe per pass (test: `test_definitions.py::test_sorted_function_entries`, `::test_unknown_names_skipped`, `::test_unavailable_tools_skipped_one_probe_per_pass`)
- [x] overrides shallow-merged; raising overrides ignored (test: `::test_schema_overrides_merged_and_failures_ignored`)
- [x] new list and new entry dicts on every return (R01.13) (test: `::test_appending_to_a_returned_list_never_changes_the_next`)
- [x] memo invalidated by generation; probe flips show next call (test: `::test_memo_hit_and_generation_invalidates`, `::test_probe_flip_shows_on_next_call`)
- [x] cross-refs only for surviving tools; rewrite may drop (R02.21) (test: `::test_cross_reference_only_when_the_tool_survives`, `::test_cross_reference_added_when_present`, `::test_rewrite_can_drop_or_change`)
- [x] each sanitiser quirk (test: `::test_object_without_properties`, `::test_type_arrays`, `::test_bare_string_schemas_always_fixed`, `::test_nullable_union_collapsed_with_meta_carried`, `::test_default_beside_ref_dropped`, `::test_top_level_combinators_removed_nested_kept`, `::test_bad_property_keys_renamed_with_required`, `::test_legacy_boolean_required_lifted`, `::test_literal_defaults_and_consts_never_treated_as_schemas`, `::test_huge_max_length_removed_for_grammar_routes_only`)
- [x] golden: identical output across runs (test: `::test_output_is_byte_stable_across_runs`)
- [x] gap found: schema-valued `dependencies` were copied, not walked — fixed in `_clean`, pinned by `::test_schema_dependencies_walked_name_lists_kept`
- Deliberate differences: fixes chosen per route (the reference applies every fix for every
  provider, including key renames and `required: []`); memo keyed by the surviving names so probe
  flips are seen at once; cross-references are entry data instead of per-tool rewriter functions;
  a raising rewrite drops the tool (fail closed).
- Not taken: tool-search assembly (P13), print mode, kanban selection, const-union collapse and
  reactive strips (MCP / recovery paths).

## Status

Done (2026-10-18). Files: `src/indus/tools/{definitions,schema_sanitize}.py`,
`tests/unit/tools/test_definitions.py`.
