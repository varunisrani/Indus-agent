# Task: session listing (P02-09) Phase: 02 Owner: P02 wave-4 builder B

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_state_sessions.py`
(`list_sessions_rich` 1331–1464, `_session_filter_where` 112–155, `_cwd_prefix_clause`,
`_chain_search_where` 1069, `_project_compression_tips` 1103, `list_recent_sessions_bounded`
1147–1324, `INTERNAL_LISTING_SOURCES` 239), `hermes_state_common.py` (preview constants 26–33,
`_shape_preview`, `_sql_preview_raw`, `_sql_freshest_of` and the epoch window, listable/branch/reset
child predicates). Closed before writing.
Evidence: none.

What it does (own words): the session picker's data. `list_sessions` returns one summary per
*conversation* a person would recognise: roots plus user-made branch and reset children; compression
continuations never appear as rows of their own — a root whose chain continued is shown with the
tip's fields (id, title, counters, model, preview, activity), keeping the root's start time so the
order does not jump when a rotation happens. Each row carries a short preview (the first real thing
the user typed) and `last_active`, the freshest trustworthy time we know. Internal machine sessions
(board workers, tool integrations, one-shot runs, sub-agents) stay out of human pickers unless asked
for. `recent` is the bounded variant for the agent's own "recent conversations" browse: it starts
from the activity index, resolves only a small candidate set, and gives up at a time budget instead
of stalling a turn.

Inputs → outputs:
`list_sessions(store, filters=None, limit=20, offset=0) -> list[SessionSummary]`;
`ListFilters(sources, exclude_sources, cwd_prefix, workspace, min_messages, archived, hidden,
pinned, include_subagents, include_internal, query)`;
`recent(store, limit=20, budget=3.0) -> list[SessionSummary]`;
`shape_preview(text) -> str`, `INTERNAL_SOURCES`.
`SessionSummary` fields: id, source, title, preview, started_at, last_active, ended_at, end_reason,
message_count, tool_call_count, model, cwd, git_root, git_branch, profile_id, parent_session_id,
kind, archived, pinned, hidden, unread, root_id (set when projected), lineage_ids.

Files: `src/indus/state/listing.py`, `tests/unit/state/test_listing.py`.

Rules:
- Preview: first user row of the session in **id order** (R03.14) that is live (`active = 1`),
  has content and is not a compression handoff row (see `compression.md`: rows written by
  `publish_continuation` carry `finish_reason = 'indus.handoff'`). Multimodal content contributes
  its text parts joined by a space. Shaping: CR/LF → space, trim, keep the first 63 chars; longer
  than 60 → first 60 + "...". A projected tip without an eligible row falls back to the root's.
- `last_active`: the newest of `last_activity_at`, the newest message timestamp and `started_at`,
  where each value outside [2020-01-01, now + 1 day] is ignored (a 2099 stamp or a garbage double
  cannot pin a row to the top); nothing usable → None.
- Visible rows: `parent_session_id` NULL, or a child whose `_branched_from` / `_reset_from` marker
  names its own parent (R03.13: a copied marker naming another id does not count). With
  `include_subagents` also delegate/unmarked children of a non-compression parent and
  `source = 'subagent'`. A compression continuation is never its own row.
- Internal sources `kanban`, `tool`, `oneshot`, `subagent` are hidden unless `include_internal`,
  named in `sources`, or (`subagent` only) `include_subagents`.
- Tri-states: `archived` False (default) hides archived, True shows only archived, None both; same
  for `hidden` (default False) and `pinned` (default None). Flags are lineage-wide (R03.11), so
  the root's value is the conversation's.
- `cwd_prefix`: cwd equals the prefix or lies under it (`/` or `\` separator; LIKE wildcards in
  the prefix escaped). `workspace`: `git_root` equals it, or no `git_root` and cwd under it.
- `query`: case-insensitive substring over every id of the forward compression chain, the chain's
  titles (also with `-`, `_`, `.` and spaces removed so `an94` finds `AN-94`) and the first eligible
  user message of each chain member. Done in SQL (recursive CTE) before LIMIT/OFFSET.
- Order: root `started_at` DESC, then id DESC. `unread` = `last_read_at` is set and `last_active`
  is later (NULL = read, 0 = unread).
- `recent`: candidates = up to max(128, 8 × limit) non-archived, non-hidden, non-internal,
  non-delegate rows by `COALESCE(last_activity_at, started_at)` DESC (activity index); each is
  mapped to its lineage root (`lineage.root_in`), roots deduplicated and projected to their tip,
  then sorted by `last_active`. Before each candidate step the clock is checked against
  `budget`; past it the walk stops and what was resolved so far is returned.

Edge cases / failure modes:
- tip walk raises `AmbiguousChain` (two live continuations): the row keeps the root's own fields
  (fail closed, logged) instead of guessing a tip.
- empty session: preview "", last_active = started_at (if in window).
- CJK preview: slicing is by code point, so no half characters.
- every `IN (...)` list (page hydration) chunked at 900.

Our design differences / decisions:
- module functions on the handle; dataclasses instead of dicts; `kind` comes from SQL predicates
  equivalent to `sessions.child_kind` (kept in SQL so filtering happens before LIMIT).
- no skill-scaffold excerpting (no skills yet), no session keys or local-reset lineages, no pinned
  back-fill pass (pinned is a plain tri-state filter), no compact-row mode.
- `query` matches the first user message itself rather than only its 60-char shaped preview.
- `recent` returns a partial result at the budget rather than raising.

Tests we will write:
- preview table: multiline, 200 chars, exactly 60/61 chars, CJK, hidden first rows (rewound row,
  handoff row), multimodal parts.
- root + tip chain → one row with the tip's id/title/counters and the root's start; title falls
  back to the root when the tip has none; ambiguous chain keeps the root.
- `subagent` source and delegate child hidden unless asked; internal sources; branch child shown,
  inherited marker naming another id not treated as a branch.
- filters: cwd prefix (sibling dir not matched, `_` escaped), workspace, sources include/exclude,
  min messages, archived/hidden/pinned tri-states, query by id, by title (punctuation-free), by
  first message, across the chain.
- activity: 2099 timestamp ignored; message newer than activity wins; unread flag.
- `recent`: order by activity, continuation resolved to one row, budget honoured with a slow
  fake reader (clock advanced per statement).

## Decisions for the lead
- "Hidden rows" for previews = rows with `active = 0` plus compression handoff rows (schema 1 has
  no per-message display kind).
- Out-of-window activity values are ignored rather than clamped to the bound.
- Ordering is by root `started_at` only; an `order="activity"` mode can be added when a surface
  needs it.

Compare checklist (filled after the rewrite; reopened the files named in `Read:`):
- [x] preview eligibility and shaping: 63-char head, 60 + "...", CR/LF folded, hidden and handoff
  rows skipped (tests: `tests/unit/state/test_listing.py::test_preview_table`,
  `::test_preview_skips_hidden_rows_and_handoffs`, `::test_preview_of_multimodal_and_empty_sessions`)
- [x] root → tip projection keeps the root start, title falls back to the root, ambiguous chain
  keeps the root (`::test_compression_chain_is_one_row_with_tip_fields`,
  `::test_untitled_tip_shows_the_root_title`, `::test_competing_continuations_keep_the_root`)
- [x] internal sources and sub-agent visibility; listable children by marker
  (`::test_internal_and_subagent_sources`, `::test_children_by_kind`)
- [x] cwd prefix: trailing separators trimmed, LIKE wildcards escaped, both separators
  (`::test_cwd_prefix_and_workspace`)
- [x] chain search over ids and titles incl. the punctuation-free title form
  (`::test_query_by_id_title_and_first_ask_across_the_chain`)
- [x] activity window: out-of-window values ignored, freshest of three
  (`::test_far_future_activity_is_ignored`, `::test_freshest_of_activity_messages_and_start`)
- [x] unread semantics NULL = read, 0 = unread (`::test_unread_flag`)
- [x] bounded recent browse: indexed candidates, root → tip, budget
  (`::test_recent_orders_by_activity_and_resolves_chains`, `::test_recent_stops_at_the_budget`)
- [x] gap found: the archived-only view must still show hidden rows (it is the recovery surface)
  — fixed in `_where`, pinned by `::test_counts_and_tristate_flags`
- Deliberate differences: preview picks the first row by id, not by timestamp (R03.14); `recent`
  returns what it resolved instead of raising at the deadline; `query` also matches the first
  user message; no pinned back-fill pass, no compact rows, no skill-scaffold excerpts, no local
  reset lineages or session keys (none exist yet).
- Not taken: ordering by chain activity (`order_by_last_active`) — no surface needs it yet.

## Status

Done (2026-10-18). `listing.py` with 25 unit tests. Uses `lineage.lineage_ids_in` / `root_in`
(slice A) and `messages.decode_content`; handoff recognition from `state/handoff.py`.
