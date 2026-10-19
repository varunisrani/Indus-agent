# Task: Chunker (P15-14) Phase: 15 Owner: wave-1 builder G3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/platforms/base.py`
(`truncate_message`, `utf16_len`, `_custom_unit_to_cp`, `_prefix_within_utf16_limit`),
`gateway/platforms/helpers.py` (`fence_state_after` only, called by `truncate_message`),
`gateway/stream_consumer_fences.py` (all, 38 lines). Research `06-gateway.md` §3.13 and trap R06.11.
Closed before writing.
Evidence: none.

What it does (own words): a reply longer than one platform message is cut into pieces that each
fit. Cuts prefer a line break, then a space; a piece never ends inside a fenced code block without
closing it, and the next piece reopens the fence with the same language so every piece renders on
its own. A cut never leaves one inline code span half open (that breaks Telegram MarkdownV2), never
splits a character (emoji and other astral characters cost two UTF-16 units on Telegram), and never
hangs on a tiny limit. Multi-piece output gets a `(i/n)` marker outside any fence. The result also
says which source offsets each piece carries, so a send that stopped half way can resume with only
the undelivered remainder (P15-04, R06.4).

Inputs → outputs: `split_message(text, limit, *, len_fn=code_points, marker=plain_marker)` →
`Chunked(chunks=(Chunk(text, start, end), ...), source)`; `Chunked.texts`,
`Chunked.remainder(delivered)` (source text from the first undelivered piece). Helpers:
`code_points`, `utf16_units`, `prefix_within(text, budget, len_fn)` (largest code-point offset
that fits), `fence_state(text, open_lang)` (language of the fence still open after `text`, or
None), `close_fences(text)` (append the closers an interim frame needs).

Files: `src/indus/gateway/outbound/chunker.py`, `tests/unit/gateway/outbound/test_chunker.py`.

Edge cases / failure modes:
- fits → one piece, no marker; empty text → one empty piece.
- limit < 1 → `ValueError`; limits below 32 units are "bare" mode: hard cuts by code point, no
  markers and no fence repair (no room for them), still at least one code point per piece; one
  code point wider than the limit (an emoji at UTF-16 limit 1) is sent alone, the only case a
  piece may exceed the limit.
- the marker reserve grows with the count: 1–99 pieces fit the default reserve, more pieces
  re-split with a wider reserve.
- newline before half the budget → cut at the last space; no space → hard cut.
- unpaired inline backtick in the piece (outside fences, escaped ones ignored) → back off to the
  space or newline before it when that point is past a quarter of the budget.
- hard cut never lands between a backslash and the character it escapes (MarkdownV2 text) and
  never inside a fence marker line.
- remainder: leading whitespace stripped outside a fence; inside an open fence only the one
  newline at the cut is dropped, so code indentation survives (the reference strips all).
- a language tag so long that the reopen prefix eats most of the budget is dropped from the
  reopen (plain ``` fence) rather than starving the piece.

Our design differences / decisions:
- Names: `split_message`, `Chunk`, `Chunked`, `code_points`, `utf16_units`.
- Offsets map returned with the pieces (the reference returns strings only).
- Marker after a fence close goes on its own line (`\n(i/n)`) instead of `` ``` (i/n) ``, which is
  not a valid closing fence; the marker text is injected (`marker(i, n)`) so MarkdownV2 can escape
  its parentheses.
- Code indentation is kept across a reopened fence (see edge cases).

Tests we will write:
- seeded-random property tables (hypothesis is not installed): every piece ≤ limit under both
  length functions; source slices + whitespace gaps rebuild the input; piece text minus prefix /
  closer / marker equals its source slice; fences balanced per piece with the language kept; no
  unpaired backtick introduced; limits 1–20 terminate; astral characters never split.
- golden cases: fence carry, marker on its own line after a closer, back-off before an inline
  code span, backslash escape not split, remainder after partial delivery.

## Decisions for the lead
- Bare mode under 32 units (no markers, no fence repair). Change: `BARE_LIMIT` in the module.

Compare checklist (filled after the rewrite; reopened `gateway/platforms/base.py` `truncate_message`
and the UTF-16 helpers, `gateway/platforms/helpers.py` `fence_state_after`,
`gateway/stream_consumer_fences.py`):
- [x] fits → single piece (test: `tests/unit/gateway/outbound/test_chunker.py::test_text_that_fits_is_one_piece`)
- [x] marker reserve, room for reopen prefix and closer (`::test_pieces_fit_the_limit_under_both_length_functions`, `::test_many_pieces_widen_the_marker_reserve`)
- [x] newline unless before half, then space, then hard cut (`::test_cut_prefers_newline_then_space`)
- [x] progress guarantee on tiny limits / wide characters (`::test_tiny_limits_terminate`, `::test_astral_characters_are_never_split`)
- [x] inline-code back-off past a quarter (`::test_cut_backs_off_before_an_open_inline_code_span`)
- [x] fence close + reopen with the language (`::test_fence_is_closed_and_reopened_with_its_language`)
- [x] UTF-16 counting, no surrogate split (`::test_utf16_counts_astral_characters_twice`)
- [x] gap found: the reference counts fence backticks as inline ones; ours tracks inline spans
  outside fences only — pinned by the property test's "no unpaired backtick" assertion.
- [x] gap found: a span whose opening backtick has no break before it past a quarter → cut right in
  front of the backtick (the reference leaves the span split) — same property test.
- Deliberate differences: offsets map and `remainder`; marker on its own line after a closer;
  code indentation kept in a reopened fence; marker text injected (MarkdownV2 escapes it); bare
  mode below 32 units; no cut between a backslash and its character; never inside a fence line.
- Not taken: paragraph-packing splitter variants (not used by P15 senders).

## Status

Done (2026-10-19). Only the task's files plus the shared ledgers (R06.11 rows).
