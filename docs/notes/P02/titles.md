# Task: titles (P02-13) Phase: 02 Owner: wave-4 builder A

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_state_titles.py` (all),
`hermes_state.py` (title length and rank constants only),
`tests/hermes_state/test_canonical_title_guard.py`. Closed before writing.
Evidence: none.

What it does (own words): a session title is what pickers and `--resume <name>` show, so it must be
printable, single-line and unique among titled sessions (unique index on non-NULL `title`, P02-06).
`sanitize` removes ASCII/C1 control characters, zero-width and bidi-override characters (a
right-to-left override can make a title read differently from what it is) and lone surrogates,
folds every whitespace run to one space, trims, caps at 100 characters, and turns an empty result
into NULL (which clears the title). Titles carry provenance: `user` (typed by a person) outranks
`llm` (generated) which outranks `derived` (from the first message); a NULL provenance on a titled
row counts as `user`. A user write always lands; an automatic one lands only on an untitled row or
over a strictly lower rank, so a generated title never replaces a name the user chose. Write and
check run in one transaction.

When the wanted title is already held by another row: if that row is a compression ancestor of
the session asking (its own earlier segment), the ancestor gives the title up in the same
transaction (the tip is what pickers show); otherwise `TitleTaken`. `next_lineage_title(base)`
drops a trailing ` #N`, finds the highest N among `base` and `base #k`, and returns `base #N+1`
(`name` counts as #1). `resolve_title(text)` prefers the newest `text #N` row over the exact one.
Copying a title across a rotation keeps its provenance.

Inputs → outputs: `sanitize(text) -> str | None`; `set_title(store, sid, title, source="user") ->
bool` (True = written) / `set_title_in(conn, …, source=)`; `copy_title_in(conn, from, to) -> bool`;
`get_title(store, sid)`; `next_lineage_title(store, base)` / `next_lineage_title_in(conn, base)`;
`resolve_title(store, text) -> sid | None`; `TitleTaken(holder=)`.

Files: `src/indus/state/titles.py`, `tests/unit/state/test_titles.py`.

Edge cases / failure modes:
- RLO, ZWSP, BEL, a 300-character title, an all-space title, mixed newlines/tabs, a lone surrogate.
- `%`/`_` inside a base or lookup text must not act as LIKE wildcards.
- automatic write of an empty title is a no-op (only a person clears a title).
- automatic write of the same rank over itself is a no-op; `derived` → `llm` upgrade lands once.
- missing session → False; unknown source → `ValueError`.
- conflict with a branch parent (not a compression ancestor) → `TitleTaken`.

Our design differences / decisions:
- over-long titles are truncated to 100 characters (phase: "caps"), not rejected.
- no canonical bot-chat guard and no ghost-session yield (no such feature in our product yet).
- ancestor check uses the lineage walker (`lineage.ancestors_in`).

Tests we will write (`test_titles.py`): sanitizer table; provenance table (llm vs user, llm vs
derived, derived vs llm, NULL provenance); compression-root title moves to the tip; unrelated
holder → `TitleTaken`; `#N` numbering with gaps (`x`, `x #3` → `x #4`) and wildcard bases;
resolve prefers newest `#N`; copy keeps provenance; generated-input table (seeded random, since
`hypothesis` is not installed): `sanitize` is idempotent and its output is ≤ 100 chars with no
control/zero-width/bidi characters.

## Decisions for the lead
- `next_lineage_title` of a base nobody holds returns the base unchanged.

Compare checklist (filled after the rewrite; reopened `hermes_state_titles.py`):
- [x] character classes stripped, whitespace fold, empty → NULL (test: `test_titles.py::test_sanitize_table`)
- [x] gap found: the interlinear-annotation (U+FFF9–FFFB) and object-replacement (U+FFFC)
  placeholders were not stripped — added to the set, pinned by a `test_sanitize_table` row
- [x] provenance ranks and NULL-as-user (`::test_llm_title_does_not_replace_a_user_title`,
  `::test_llm_title_replaces_a_derived_one_once`, `::test_null_provenance_ranks_as_user`)
- [x] ancestor yields inside the transaction; otherwise conflict
  (`::test_title_held_by_own_compression_root_moves_to_the_tip`,
  `::test_title_held_by_an_unrelated_session_is_taken`, `::test_branch_parent_does_not_yield`)
- [x] `#N` numbering incl. wildcard bases (`::test_next_lineage_title`)
- [x] resolve prefers the newest numbered continuation (`::test_resolve_prefers_the_newest_numbered_continuation`)
- [x] copied title keeps provenance (`::test_copy_title_keeps_provenance`)
- Deliberate differences: over-long titles are cut at 100 (the reference refuses them); C1
  controls are stripped too; line/paragraph separators fold to a space instead of vanishing;
  numbering only counts `base #N` whose base is exactly the stem (`t #2 #5` belongs to `t #2`).
- Not taken: canonical bot-chat rename guard and archived bot-chat / empty-ghost title release
  (no bot mode yet).
- `hypothesis` is not installed: idempotence is checked over a seeded table of 60 × 20 generated
  strings (`::test_sanitize_is_idempotent_on_generated_text`).

## Status

Done (2026-10-18). `titles.py`, 100 unit tests (incl. parametrised rows). Shared: one code in
`foundation/errors.py`.
