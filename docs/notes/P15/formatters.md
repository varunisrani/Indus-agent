# Task: Formatters (P15-15) Phase: 15 Owner: wave-1 builder G3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `plugins/platforms/telegram/adapter.py`
(the MarkdownV2 escape set, `_strip_mdv2`, `format_message` and its bracket safety net, the chunk
send with plain fallback), `gateway/platforms/helpers.py` (`convert_table_to_bullets` and its
table-row helpers), `plugins/platforms/slack/block_kit.py` (limits block, table block builder,
monospace table fallback), `plugins/platforms/slack/adapter.py` (`format_message`, skimmed for
which constructs map to mrkdwn). Research `06-gateway.md` §3.13, trap R06.11. Closed before
writing.
Evidence: none.

What it does (own words): the agent writes ordinary Markdown; each platform wants its own markup.
Telegram's MarkdownV2 rejects a whole message when any of its special characters appears
unescaped outside code, so the converter escapes everything that is not markup it emitted itself,
keeps code verbatim (only `\` and `` ` `` escaped inside code, as the format demands), and turns
tables (which MarkdownV2 lacks) into a bold heading plus bullets per row. If Telegram still refuses
a piece, the plain variant of that piece (escapes undone, markers removed) is sent once. Slack gets
mrkdwn (`*bold*`, `_italic_`, `~strike~`, `<url|text>`, `&<>` escaped, broadcast mentions
defused) and tables as Block Kit `table` blocks when they fit (≤100 rows, ≤20 columns, ≤20,000
characters of cell text), else a code block. Discord takes Markdown natively; tables become code
blocks and embed titles are clamped to 256. A plain-text strip serves SMS and every fallback.

Inputs → outputs: `to_markdownv2(md)`, `markdownv2_to_plain(mdv2)`, `escape_markdownv2(text)`,
`markdownv2_marker(i, n)`; `to_mrkdwn(md)`, `slack_blocks(md) -> list[dict] | None`,
`slack_stream_text(md)` (standard Markdown for native streams, identity); `to_discord(md)`,
`clamp_embed_title(text)`; `strip_markdown(md)`; `tables_to_bullets(md)`; `Markup` names
(`markdownv2`, `mrkdwn`, `markdown`, `plain`) and `formatter_for(markup)` returning the
format / plain / marker triple the sender uses.

Files: `src/indus/gateway/outbound/formatting.py`, `tests/unit/gateway/outbound/test_formatting.py`,
`tests/fixtures/gateway/markdownv2_cases.json`.

Edge cases / failure modes:
- unclosed emphasis (`**a`) or fence: markers become escaped literals; an unclosed fence is closed.
- links with parentheses in the URL (`(https://x/a_(b))`): balanced parens kept in the URL, `)`
  and `\` escaped inside it.
- code containing backslashes and backticks; inline code never touched by emphasis passes.
- nested emphasis (`**bold _it_**`) → nested entities; the same entity is never nested in itself.
- escaping grows the text: the sender chunks after formatting (R06.11).
- tables inside fences are left alone; a separator row is required to call something a table.
- Slack: `<!channel>`/`<!here>`/`<!everyone>` defused; raw `<`/`>`/`&` escaped; 50-block cap, a
  section text over 3,000 split across sections; over the cap → `None` (caller sends mrkdwn).

Our design differences / decisions:
- A small tokenizer of our own (inline scanner with an active-style set) instead of the reference's
  placeholder-regex passes; it emits valid MarkdownV2 by construction, which the tests check with
  a validator written for them.
- Table rows to bullets: heading = first cell (or "Row N"), one bullet per remaining column
  `header: value`.
- Slack table limit 20,000 characters per the phase row (the reference uses 10,000).
- Slack table cells use the `raw_text` cell type; P15-35 must check the cell shape against Slack's
  Block Kit reference at the pinned SDK version.
- Discord tables → aligned code block (Discord has no table markup).

Tests we will write:
- golden JSON cases (nested emphasis, links with parentheses, code with backslashes, tables,
  headers, quotes, lists, unclosed markers).
- seeded-random property: `to_markdownv2` output always passes the test's MarkdownV2 validator;
  each chunk of it (after `split_message` with the MarkdownV2 marker) too for inputs without
  multi-line emphasis.
- parse-error fallback strips markers and escapes.
- Slack: mrkdwn conversion table, table block limits (rows, columns, characters), block cap.
- Discord: embed title clamp, table to code block; plain strip table.

## Decisions for the lead
- Slack table cell budget 20,000 (phase) vs reference 10,000: kept the phase number.

Compare checklist (filled after the rewrite; reopened the Telegram `format_message`, `_strip_mdv2`,
the table helpers and Slack `block_kit.py` table builder):
- [x] escape set matches the MarkdownV2 rules (`tests/unit/gateway/outbound/test_formatting.py::test_every_special_character_is_escaped`)
- [x] code verbatim with `\` and backticks escaped; lead-in fences (`Here is code: ```py`) (`::test_golden_markdownv2_cases`)
- [x] links: only `)` and `\` escaped in the URL, balanced parentheses kept (`::test_golden_markdownv2_cases`)
- [x] tables to bullets outside fences only (`::test_tables_become_bullets_outside_fences_only`)
- [x] parse error → plain once with markers stripped (`::test_plain_fallback_strips_markers_and_escapes`; resend in `test_sender.py::test_bad_format_piece_is_resent_plain_once`)
- [x] output always valid (`::test_markdownv2_output_always_parses`, `::test_chunks_of_markdownv2_output_parse`)
- [x] Slack mrkdwn mapping incl. bold-after-punctuation quirk and broadcast defusing (`::test_mrkdwn_conversion`)
- [x] Slack table limits and block cap (`::test_slack_table_limits_fall_back_to_code`, `::test_slack_block_cap_returns_none`)
- [x] gap found: adjacent italics (`_a__b_`) read as underline → a zero-width space keeps them
  apart (golden case "adjacent italics").
- [x] gap found: bold inside a header was escaped literally → the same style nested in itself is
  flattened (golden case "header and list").
- Deliberate differences: tokenizer instead of placeholder regex passes; bullets rendered as `•`;
  table rows keyed by their first cell; Slack keeps `<@U…>`/`<#C…>`/`<url>` entities and escapes
  every other `<`; one native table block per message (others become code blocks); table text cap
  20,000 per the phase row.
- Not taken: Telegram rich-message line-break normalisation, expandable quotes, Slack rich_text
  blocks for lists/quotes (mrkdwn sections cover P15).

## Status

Done (2026-10-19). `tests/fixtures/gateway/markdownv2_cases.json` added; R06.11 / ACC-K25-1 rows.
