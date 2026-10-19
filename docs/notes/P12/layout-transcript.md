# Task: Layout, transcript and message rows (P12-13) Phase: 12 Owner: wave-8 builder R2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `ui-tui/src/components/appLayout.tsx`
(shell, transcript / prompt / composer panes, status rule placement), `ui-tui/src/components/messageLine.tsx`
(row kinds, gutters, collapse rules, timestamps), `ui-tui/src/components/streamingAssistant.tsx`
(live area order). Research `05-tui.md` §3.7, §3.25, §3.30. Evidence: none.

What it does (own words): the full-screen shell. Top to bottom: the transcript pane takes the free
height; under it the prompt zone shows the one blocking card; under that the composer pane shows
the queued-message preview, the status rule (top or bottom of the composer, or off) and the input.
The transcript is a virtual list: every item (banner + session panel first, then user, assistant,
system, tool, diff rows, then the live streaming items) is laid out into styled lines at the
current width, so each item's height is known; only the lines inside the viewport are handed to
Ink. The view follows new output while it sits at the bottom; once the user scrolls up, new
output does not move it. After a width change the row that was at the top stays at the top.
Long system lines (over 400 chars) collapse to their first line and a char count; a user message
backed by a paste token and over 300 chars collapses the pasted part. Optional `[HH:MM]` stamps on
user and assistant rows. Scrolling: Shift+Up/Down one row, PgUp/PgDn half the viewport (at least
4), the wheel accelerates on sustained scrolling in one direction and ignores a single reversed
tick right after a fast run; with a modifier held it moves one row per event.
The floating completion list window shows at most 16 rows centred on the selection.

Inputs → outputs: transcript items (props) + live turn state (turnStore) + ui settings (uiStore) +
viewport size → Ink tree with only visible lines; scroll actions → new offset.

Files: `apps/tui/src/components/AppLayout.tsx`, `Transcript.tsx`, `MessageRow.tsx`, `virtualRows.ts`,
`Transcript.test.tsx`, `virtualRows.test.ts`; stand-ins (see Decisions).

Edge cases / failure modes:
- Empty transcript → banner only; zero-height viewport → nothing rendered.
- Width change: heights re-laid out; anchor (item key + line within it) restored.
- Content shrink (e.g. `/clear`) below the offset clamps to the bottom.
- 5,000 items: offsets via prefix sums, window found by binary search; only visible lines rendered.
- Wheel direction flip right after a fast run is treated as bounce and dropped once.
- Completion list shorter than 16 → all rows; selection near the ends pins the window to the edge.

Our design differences / decisions:
- Our layout produces lines itself (no Ink measuring pass); "measured heights" are the exact line
  counts of our own layout, cached per item key + width + theme + version.
- Rows are lines, not whole messages: a long message never goes to Ink in full (D-04.2 finding).
- Banner text and glyphs are our own (D-16.23): `indus agent` wordmark, our prompt glyphs.
- Left out: ambient rails, pets, agents monitor, journey, widget slot, FPS overlay (05 §5).

Tests we will write:
- 5,000-row transcript renders only visible rows (frame line count = viewport, first/last ids).
- Width change keeps the anchor row; scroll up stops follow; new output does not jump.
- 401-char system line collapsed; 300-char paste-backed user message collapsed; timestamps.
- PgUp half viewport min 4; wheel acceleration, flip suppression, precision mode; completion window.

## Decisions for the lead
- Transcript history (the list of finished messages) is not in the wave-8 contract; `Transcript`
  takes `items: TranscriptItem[]` as a prop (type in `MessageRow.tsx`). P12-27 owns where the list
  lives (suggest a `messages` field on turnStore or its own store).
- Scroll state lives in `virtualRows.ts` as a small vanilla zustand store (`scrollStore`) with pure
  reducers; key routing (R3, P12-20) calls `scrollBy` / `scrollPage` / `wheel`.
- Prompt zone and input are slots (`prompt`, `input` props) so R3/R4 components plug in at P12-27.
- Paste-token shape assumed `[paste #N …]` or `[[paste:N …]]` (`isPasteBacked`); R3's P12-17 token
  must match one of them, or the regex in `MessageRow.tsx` changes with it.
- Inline-diff segments are recognised by a segment id starting `diff` or by diff-looking text
  (`isDiffText`); R1's turn controller should give diff segments such an id.

Compare checklist (filled after the rewrite; reopened `appLayout.tsx`, `messageLine.tsx`,
`streamingAssistant.tsx`):
- [x] pane order transcript → prompt zone → composer (queued preview, status rule top, input,
  status rule bottom); status off hides both (tests: `Transcript.test.tsx::layout > …`)
- [x] row kinds and gutters; system collapse over 400 chars; paste-backed user collapse over 300;
  `[HH:MM]` stamps on user/assistant only (tests: `message rows > …`)
- [x] live area order: sealed segments (tools attached), reasoning, tool area, streaming row, plan
  (test: `virtual transcript > shows the live turn …`)
- [x] turn rule above later user rows (test: `message rows > draws a turn rule …`)
- [x] only visible lines reach Ink; anchor survives width change; scroll-up stops follow; appended
  output does not move the view (tests: `virtual transcript > …`, `virtualRows.test.ts`)
- [x] gap found: finished tools shown both in a sealed segment and in the trail → the live tool
  area drops trail rows already attached to a segment (pinned by the live-turn test)
- Deliberate differences: rows are lines from our own layout (no Ink scroll box / measuring
  pass); the live plan panel sits at the end of the live area instead of under the latest user
  row; no response separator line between a tool trail and the reply (a blank line instead);
  the status rule has no margin row above it.
- Not taken: ambient rails, pet pane, agents monitor and journey views, widget slot, FPS pane,
  sticky-prompt tracker, scrollbar column, goal / loop bars, background-task line (status rule
  carries the count), click-to-expand system rows (keys/mouse land with P12-20 / P12-27).

## Status

Done (2026-10-19). `components/{AppLayout,Transcript,MessageRow}.tsx`, `components/virtualRows.ts`;
30 tests (`Transcript.test.tsx` 15, `virtualRows.test.ts` 15). Hiding the input while a card owns
input, and wiring scroll keys / wheel to `scrollBy` / `scrollPage` / `scrollWheel`, are P12-20 /
P12-27 composition work.
