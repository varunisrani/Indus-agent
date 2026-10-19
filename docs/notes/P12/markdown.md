# Task: Markdown and streaming markdown (P12-12) Phase: 12 Owner: wave-8 builder R2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `ui-tui/src/components/markdown.tsx`
(block grammar, inline token order, table fit tiers, render cache), `ui-tui/src/components/streamingMarkdown.tsx`
(stable-boundary scanner), `ui-tui/src/components/streamingAssistant.tsx` (how the live area stacks
segments, tools and the streaming row), `ui-tui/src/lib/text.ts` (only the live-tail bound helper),
`ui-tui/src/config/limits.ts`. Research `05-tui.md` §3.13, §3.30. Evidence: none.

What it does (own words): assistant text arrives as markdown. The renderer turns a subset of it into
terminal lines: headings (ATX `#` and setext underlines), bullet / numbered / task lists, block
quotes, pipe tables fitted to the width, fenced code with token colours, `diff` fences with added /
removed colouring, inline code, bold / italic / strike, links and bare URLs, footnote lines,
horizontal rules and `MEDIA:` lines (a labelled path row). Text that already carries ANSI escapes
is printed as it is. While a reply streams, only the part up to the last safe block boundary is
rendered as markdown; the unfinished tail is plain text, so an open code fence or half a table
never flickers between layouts. A long live reply is cut to its last 16,000 chars / 240 lines with a
note of what was left out. Parsed output is cached (512 entries) keyed by text, width and theme.

Inputs → outputs: `(text, width, theme, { hyperlinks })` → `StyledLine[]` (each line a list of
styled spans, already wrapped to `width`); `<Markdown>` / `<StreamingMarkdown>` print those lines
through Ink `<Text>` with no further wrapping, so every block's height is known exactly (the
transcript virtualizes on it, P12-13).

Files: `apps/tui/src/render/markdown.tsx`, `apps/tui/src/render/streamingMarkdown.tsx`,
`apps/tui/src/render/styled.tsx` (spans, width, wrapping, ANSI-aware hard wrap, `<Lines>`),
`apps/tui/src/render/highlight.ts` (small keyword/string/comment/number colouring),
`apps/tui/src/render/tones.ts` (theme colour roles with fallbacks),
`apps/tui/src/render/markdown.test.tsx`, `apps/tui/src/render/streamingMarkdown.test.tsx`.

Edge cases / failure modes:
- `__init__`, `__dunder__` and `snake_case_name` stay literal; `_x_` alone is italic; `**` before `*`.
- Inline code is verbatim (no emphasis inside); bare URLs drop trailing `.,;:!?)` out of the link.
- Fence closes only with the same character and at least the opening length; an unclosed fence runs
  to the end of the text (final render) and keeps the streaming tail plain.
- Tables: ragged rows padded / cut to the header's column count; ideal widths if they fit; else
  shrink the overflow proportionally down to each column's longest word; else scale and hard-break
  words; columns never below 3; 2-space gap; very tall rows or overflow fall back to a
  "header: value" list per record.
- Wide (CJK, emoji) characters count as 2 cells; combining marks as 0.
- ANSI payload: lines hard-wrapped with the open SGR state carried to the next line.
- Streaming text that no longer extends what was scanned (front trim, reuse) resets the scanner.
- Empty text renders nothing.

Our design differences / decisions:
- Our own span model and line wrapper instead of letting Ink wrap: exact heights for virtual rows
  (D-04.2: only visible rows go to Ink).
- Blank source lines decide the gaps between blocks (runs of blanks collapse to one) so a block
  looks the same while streaming and after completion.
- OSC 8 is written only when the caller says the terminal supports it; otherwise a link shows its
  label underlined followed by the URL dimmed in brackets when the label differs.
- Left out (05 §5): LaTeX-to-Unicode, definition lists, HTML `<details>` handling, audio directives,
  highlight / sub / superscript marks. Our own glyphs for bullets, task boxes, quote bar, rule.
- Syntax colours: a small table of keywords per language family, strings, comments, numbers, from
  theme tokens; unknown languages print in the code colour.

Tests we will write:
- Table at 40 and 80 cols (exact frames, gap, minimum width, fallback list), header-only table.
- Open fence keeps the streaming tail plain; closed fence turns into a code block.
- `__init__` / `__name__` not italic, `_word_` italic, inline code verbatim.
- ANSI payload printed as-is; heading, list, task, quote, rule, footnote, MEDIA row, diff fence.
- Cache hit on re-render (same array identity, counter), cache bounded at 512.
- 20,000-char stream renders only the tail cap with the omitted note; 300 lines → 240.
- Stable boundary: inside fence → none; `\n\n` outside → after it; reset on non-extension.

## Decisions for the lead
- Hyperlink support is a prop (`hyperlinks`), fed by the capability module (R1 P12-08) at
  composition time (P12-27); default off.
- Theme colour keys read with fallbacks (`accent`, `fg-muted`, `danger`, `warning`, `success`,
  `border`, `fg`, `code`) so the renderer works with any palette R1 lands; named ANSI fallbacks.
- The unsettled streaming tail is laid out as markdown when it is plain prose (no open fence, no
  table rows), plain text otherwise; flip `layoutTail` to always-plain if the lead wants the
  stricter reading of the row.
- Follow-up: when the live cap cuts the front of a reply inside a code fence, the kept tail loses
  the opener and reads its closer as a new opener until the reply completes (the final render is
  right). Carrying fence state across the cut needs a scan of the dropped part; left for P12-33
  measurements.

Compare checklist (filled after the rewrite; reopened `markdown.tsx`, `streamingMarkdown.tsx`,
`streamingAssistant.tsx`, `lib/text.ts` live-tail part):
- [x] block kinds: fence, heading (ATX, setext), rule, footnote, bullet / task / numbered, quote,
  table, media row, paragraph (test: `markdown.test.tsx::blocks > parses headings, lists, …`)
- [x] inline precedence: image, link, autolink, code, footnote ref, strike, bold, italic, bare URL;
  code verbatim; `__dunder__` literal (tests: `inline > …`)
- [x] table fit: ideal → proportional shrink to longest word → scaled; min 3, gap 2; tall rows or
  overflow → record view (tests: `tables > …`)
- [x] stable boundary: whole lines only, fence state carried, reset on non-extension
  (tests: `streamingMarkdown.test.tsx::stable boundary > …`)
- [x] live tail 16,000 / 240 with an omitted note (tests: `live layout > renders a 20,000-char …`,
  `keeps at most 240 lines …`)
- [x] cache 512 entries keyed by text + width + theme (+ hyperlink flag) (tests: `render cache > …`)
- [x] gap found: email autolinks `<a@b.org>` → fixed (mailto target), pinned by `inline > renders
  footnote refs, images and autolinks`
- [x] gap found: pipe-led rows without a delimiter row → fixed (table, delimiter-like rows dropped),
  pinned by `tables > reads pipe rows without a delimiter row as a table`
- Deliberate differences: the reference inserts a gap whenever the block kind changes; we follow
  the source's blank lines so a block looks the same streaming and settled. Table cells keep their
  inline styles (the reference strips them to plain text). The reference re-parses the unsettled
  tail as markdown every delta; we do that only for prose, and keep the tail plain while a fence
  is open or table rows are present (the row's "unstable tail as plain text"). Closing fences
  must match the opener's character and length while streaming too (the reference toggles on any
  fence line). Diff lines are coloured by foreground only (no background bands).
- Not taken: math blocks and inline math (LaTeX out of scope), definition lists, `<details>` /
  `<summary>` / bare HTML lines, highlight / superscript / subscript marks, audio directive lines.

## Status

Done (2026-10-19). `render/markdown.tsx`, `render/streamingMarkdown.tsx` plus helpers
`render/styled.tsx`, `render/highlight.ts`, `render/tones.ts`; 42 tests in the two test files (28 + 14).
Hyperlink capability is wired at composition (P12-27).
