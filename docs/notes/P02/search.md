# Task: search (P02-15) Phase: 02 Owner: P02 wave-4 builder C

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_state_search.py`
(module constants and helpers 1–200, the query sanitiser, the token / OR-relaxation / CJK
helpers, the LIKE boolean compiler, the context batch, `search_messages`,
`_search_messages_impl`, `_search_cjk`), `tests/hermes_state/test_search_or_relaxed_fallback.py`.
Closed before writing.
Evidence: none.

What it does (own words): `search(store, query, …)` finds messages for a person's query and
returns ranked hits with a highlighted snippet and the session's metadata. Raw user text is
never handed to FTS5 as-is: FTS5's query grammar raises on stray specials, so the text is
sanitised first (balanced quoted phrases kept, unmatched specials removed, `*` kept only as a
prefix marker at a term end, hyphenated / dotted terms quoted as phrases, dangling or doubled
`AND/OR/NOT` dropped). The route depends on the query and the store: an explicit tool-role search
scans canonical rows with LIKE (tool rows are only partly indexed); a stale index (fail-open,
P02-14) or a store without FTS uses LIKE with a compiled boolean subset; a Latin query uses the
word index, then on zero hits the trigram index (when every term has ≥ 3 characters, for
substrings such as `ttpcl` in `httpclient`), then an OR-relaxed retry for implicit-AND
multi-term queries; a CJK query uses the trigram index when every term has ≥ 3 characters, else
LIKE with one OR'd clause per term. Live and compaction-archived rows are searchable; rewound
rows only with `include_inactive`. Slow searches log their route.

Inputs → outputs: `search(store, query, *, sources=None, exclude_sources=None, role=None,
started_after=None, started_before=None, include_inactive=False, sort="rank", limit=20,
offset=0, context=False) -> list[Hit]`; `Hit(message_id, session_id, role, snippet, timestamp,
tool_name, source, model, session_started, context)`, `context` = `[ContextLine(role, text)]`
(one neighbour before and after, text cut at 200 chars) or None; `sanitize(query) -> str`;
`or_relaxed(query) -> str | None`; `route_for(...)` names the route for logs.
`sort`: `rank` (bm25), `newest`, `oldest`; LIKE routes order by time (rank means newest).

Files: `src/indus/state/search.py`, `tests/unit/state/test_search.py`; config key
`sessions.search_slow_ms` (default 1000, 0 logs every search) in
`src/indus/foundation/config/schema.py` (shared).

Edge cases / failure modes:
- input capped at 2,048 characters before any processing; empty / all-special → no hits.
- only upper-case `AND/OR/NOT` are FTS5 operators; lower-case words stay terms.
- a leading `NOT`, trailing operator or two operators in a row are syntax errors → dropped.
- `"a""b"` and `""` (empty phrase) handled; an empty phrase is dropped.
- multimodal rows: LIKE and snippets read the same text projection as the index, so JSON keys
  of a parts payload never match.
- MATCH raising an FTS-scoped corruption → `fts.fail_open` and the LIKE route; any other
  operational error on MATCH (should not happen after sanitising) → that route yields nothing.
- trigram cannot see tool rows or `cron` / `subagent` sessions → a query filtered to those
  sources skips the trigram route.
- LIKE terms escape `%`, `_`, `\`; `IN (…)` filters chunked at 900 values.
- context neighbours come from the same session by id order (R03.14), visible rows only,
  fetched in one batch per 900 hits.

Our design differences / decisions:
- module function taking the handle (contract); `Hit` dataclass; snippet markers `[[` / `]]`
  on every route (LIKE snippets are cut and marked in Python around the first term).
- no hidden-row display kind (our schema has none), no unindexed-gap supplement (no chunked
  backfill), no CJK-bigram index (P2).
- CJK and Latin trigram routes require every term (not only CJK terms) to have ≥ 3 characters,
  since one short term empties an implicit-AND trigram match.

Tests we will write:
- sanitiser table (unbalanced quote, `foo-bar`, `v1.2`, trailing `OR`, `*`, leading `NOT`,
  `NOT` doubled, column syntax `a:b`, `^x`, cap); seeded-random property table (hypothesis is not
  installed): no sanitised query raises on MATCH.
- phrase found; `ttpcl` via trigram; zero-hit two-word query relaxed to OR, explicit `NOT` not
  relaxed; CJK 2-char token → LIKE, 3-char → trigram; tool-role match beyond 8,192 chars;
  rewound row hidden unless `include_inactive`, archived (compacted) row found; filters
  (sources, exclude, role, started window); sort orders; context neighbours truncated;
  stale index → LIKE; no-FTS store → LIKE; slow-search log with threshold 0.

## Decisions for the lead
- `role` accepts one role string (the phase row) — a sequence is also accepted for later callers.
- `sort="rank"` on a LIKE route means newest first.

Compare checklist (filled after the rewrite; reopened `hermes_state_search.py` 1–200, 780–1300,
the OR-relaxed test):
- [x] sanitiser: cap, phrases, specials, `*`, dangling operators, dotted/hyphenated quoting (tests:
  `tests/unit/state/test_search.py::test_sanitiser_table`, `::test_sanitiser_caps_the_input`,
  `::test_no_sanitised_query_upsets_fts5` — 8 seeds × 60 random queries; a 3,000-seed run was
  clean before it was cut to this size)
- [x] gap found by the property table: an operator word glued to specials (`AND*`, `x^OR`)
  became a bare operator — fixed by quoting operator words the user did not write alone.
- [x] routing order: tool → LIKE, stale → LIKE, Latin → base → trigram → OR-relaxed,
  CJK → trigram / LIKE (tests: `::test_exact_phrase_is_found`,
  `::test_substring_is_found_through_the_trigram_index`,
  `::test_zero_hit_multi_word_query_is_relaxed_to_any_word`,
  `::test_cjk_short_terms_use_like_and_long_terms_use_trigram`,
  `::test_tool_role_search_reaches_past_the_indexed_prefix`,
  `::test_stale_index_uses_like_with_a_boolean_subset`)
- [x] OR relaxation only for ≥ 2 units and no explicit OR/NOT; phrases stay one unit (test:
  `::test_or_relaxation_table`)
- [x] visibility: live + compacted rows; rewound only with include_inactive (test:
  `::test_rewound_rows_hidden_unless_asked_and_archived_rows_found`)
- [x] filters on every route; context batch; slow log with route (tests:
  `::test_filters_apply_on_every_route`, `::test_context_neighbours_are_batched_and_truncated`,
  `::test_slow_searches_log_their_route`, `::test_slow_threshold_from_config`)
- [x] gap found at compare: a failing context fetch is swallowed in the reference — ours now logs
  and returns the hits without context too.
- Deliberate differences: a store without FTS5 searches with LIKE (the reference returns
  nothing; the phase row asks for LIKE); context neighbours by id order, not timestamp (R03.14);
  an index probe that hits FTS-scoped corruption also fails open before any MATCH; `%` is
  stripped from queries (LIKE escapes its own wildcards).
- Not taken: `fields` projection, unindexed-gap supplement, CJK bigram route, hidden display rows.

## Status

Done (2026-10-18). `search.py` with 67 unit tests (`test_search.py`). Shared:
`src/indus/foundation/config/schema.py` (`sessions.search_slow_ms`) and its defaults test
`tests/unit/foundation/config/test_schema.py` (one added key).
