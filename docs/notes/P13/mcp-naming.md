# Task: Tool naming, filters and description hygiene (P13-02) Phase: 13 Owner: wave-10 builder M1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/mcp_tool_schema.py` (name
component rule, prefix, 64-char clamp with hash suffix, utility-tool table, filter helpers),
`tools/mcp_tool_registration.py` (the include/exclude predicate only), `tools/mcp_tool_discovery.py`
(filter use only), `tools/ansi_strip.py` (Unicode TAG stripping). Research `07` §3.16.3, R07.21,
R07.25, R07.30; D-16.24. Ours: `src/indus/tools/schema_sanitize.py`, `src/indus/tools/definitions.py`.
Evidence: none.

What it does (own words): turns a server's tool into the name the model sees and the registry
keys: `mcp__<server>__<tool>`, each component with every character outside `[A-Za-z0-9_]` turned
into `_` (so `my-server` → `my_server`). A name longer than 64 characters keeps its first
55 characters and gains `_` plus the first 8 hex digits of the SHA-256 of the full unclamped
name; one warning per full name. The handler that P13-05 registers keeps the server's original
tool name, so the clamp never changes what is called (R07.21). Filters decide which server tools
are registered: when `include` is set (even an empty list) only matching tools pass and `exclude`
is ignored; otherwise everything except `exclude` matches. An entry with `*`, `?` or `[` is a
case-sensitive glob (`fnmatchcase`), any other entry must match exactly. Descriptions lose Unicode
TAG characters (U+E0000–U+E007F) except inside a valid flag sequence (black flag U+1F3F4, tag
letters, cancel tag U+E007F), so 🏴 subdivision flags survive; a missing or blank description gets
our own generic text. Parameter schemas pass through P04's `sanitize_parameters` with no route
quirks (always-on repairs only); route quirks are applied later by `definitions`.

Inputs → outputs: (server name, tool name) → registry name; (include, exclude) → predicate;
description → clean text; server `Tool` → `{name, description, parameters}` schema dict.

Files: `src/indus/mcp/naming.py`, `tests/unit/mcp/test_naming.py`.

Edge cases / failure modes:
- empty components (a server named `""`) stay empty: the name is still `^[A-Za-z0-9_]{1,64}$`.
- non-ASCII letters (`é`) are replaced too (the provider rule is ASCII).
- two long names that differ only after character 55 get different suffixes (hash of full name).
- a name of exactly 64 characters is left as is.
- TAG characters outside a flag sequence, a lone cancel tag, a flag without its cancel tag:
  stripped.
- a non-object `inputSchema` → empty object schema.

Our design differences / decisions:
- Our generic description text ("Tool <tool> from the MCP server <server>.").
- The reference also scans descriptions for injection phrases (warning only); that belongs to the
  shared content scanner (P13-20), not here.
- Utility tools (`list_resources`, …) are P13-05; `naming.tool_name` already names them.

Tests we will write:
- Hypothesis: any server/tool text → name matches `^[A-Za-z0-9_]{1,64}$`, deterministic.
- long names differing only at the end → different suffixes; 64 exact kept; one warning.
- include/exclude precedence table incl. empty include, globs, case sensitivity.
- TAG stripping keeps 🏴󠁧󠁢󠁳󠁣󠁴󠁿 and removes smuggled tag text; missing description text.

## Decisions for the lead
- none

Compare checklist (filled after the rewrite; reopened `tools/mcp_tool_schema.py`,
`tools/ansi_strip.py`, the filter predicate):
- [x] name rule and `mcp__` prefix, every produced name `^[A-Za-z0-9_]{1,64}$`, deterministic (seeded property test `tests/unit/mcp/test_naming.py::test_every_name_is_provider_safe_and_deterministic`)
- [x] clamp: first 55 chars + `_` + sha256(full)[:8], warn once; 64 exact kept; end-only differences stay distinct (`::test_long_names_get_a_hash_suffix_of_the_full_name`, `::test_a_64_character_name_is_kept_as_is`, `::test_names_that_differ_only_at_the_end_stay_distinct`)
- [x] include over exclude, empty include = nothing, globs and exact entries case-sensitive (`::test_include_and_exclude_precedence`)
- [x] TAG characters stripped, flag sequences kept (`::test_tag_characters_are_stripped_but_flags_survive`, `::test_stripping_leaves_no_stray_tag_characters`)
- [x] generic text for a missing description (`::test_missing_description_gets_generic_text`)
- Deliberate differences: the reference rewrites schemas at registration (definitions → $defs, nullable unions, const unions, object repair); we apply P04's always-on repairs here and leave route quirks to `tools.definitions` at request time. The reference's description injection scan belongs to P13-20.
- Not taken: utility tool schemas (P13-05 names them with `tool_name`).

## Status

Done (2026-10-19). `src/indus/mcp/naming.py`, `tests/unit/mcp/test_naming.py`. Hypothesis is not installed; the property tests run seeded generators (40 seeds × 25 cases each).
