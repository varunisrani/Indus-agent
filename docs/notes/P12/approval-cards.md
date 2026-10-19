# Task: Approval, confirm and pager cards (P12-21) Phase: 12 Owner: wave-8 builder R4

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `ui-tui/src/components/prompts.tsx`
(approval and confirm parts), `ui-tui/src/app/createServerRequestHandler.ts` (whole),
`ui-tui/src/components/appOverlays.tsx` (pager widget only), `ui-tui/src/app/useInputHandlers.ts`
(pager keys and the Ctrl+C overlay cancel only), `ui-tui/src/app/createGatewayEventHandler.ts`
(`request.cancel` and the abandoned-clarify flush only), test names in
`ui-tui/src/__tests__/approvalAction.test.ts`. Research `05-tui.md` §3.17, R05.11, R05.22. Ours:
`docs/notes/P12/bridge-server-requests.md`, `client-channel.md`, the wave-8 contract (overlay store).
Evidence: none.

What it does (own words): the agent's questions arrive as server requests. `serverRequests.ts`
turns each one into a card in the overlay store and returns a promise the channel answers with when
the person decides. The approval card shows the (already redacted) command wrapped to the card width
so the whole command can be read before it runs; only the first ten wrapped lines are shown, then a
"N more lines" note. Choices come from the request, always in the order once, session, always, deny,
and are picked with the arrows and Enter or with a digit (never letters: R05.11). Esc and Ctrl+C
deny. Opening a card sets the status text and rings the terminal bell unless the request is a replay
or the bell is turned off. A withdrawn request (`server_request.cancelled`) closes its card. The
confirm card is the local yes/no used before destructive commands. The pager shows long slash output
one screen at a time.

Inputs → outputs:
- `server_request` approval `{command, description, choices?}` (+ tolerated `smart_denied`,
  `allow_permanent`) → approval card → `{kind: approval, choice}`.
- id-bearing request frames (desktop-only kinds such as preview or window reads) → no handler → the
  channel answers `-32601` (P12-02).
- withdrawal → card closed, the pending promise settles with the kind's refusal (the channel drops it).
- confirm `{title, detail?, confirmLabel?, cancelLabel?, danger?}` → Enter/y confirm, Esc/n/Ctrl+C
  cancel.
- pager `{title?, lines}`: Up/k, Down/j, PgUp/b, PgDn/Space/Enter (closes on the last page), g/G,
  Esc/q/Ctrl+C close. Page size = rows − 6 (at least 5). Long output = more than 180 characters or
  more than 2 non-empty lines.

Files: `apps/tui/src/cards/ApprovalCard.tsx`, `ConfirmCard.tsx`, `Pager.tsx`, `serverRequests.ts`,
`CardView.tsx` (kind → component, for P12-27), `payloads.ts` (card payload shapes), `openCards.ts`
(`askConfirm`, `openPager`), `keys.ts` (key flags), `text.ts` (width-aware hard wrap), `tones.ts`
(theme colours with fallbacks), `rpcLike.ts` (the RPC shape used here), `inkHarness.ts` (test-only
Ink driver), tests `ApprovalCard.test.tsx`, `Pager.test.tsx`, `serverRequests.test.ts`.

Edge cases / failure modes:
- `choices` present → filter the canonical order by it; `deny` is always kept; absent →
  `smart_denied` gives once + deny, `allow_permanent: false` drops always.
- empty description → a fixed fallback title; an empty command line renders as a blank row.
- wide characters (CJK, emoji) count two columns in the wrap; control characters are stripped.
- digit out of range is ignored; Up at the top / Down at the bottom stay put.
- a second answer after a withdrawal is dropped by the channel; the card must not reopen.
- replayed request (`meta.replayed`) → no bell.

Our design differences / decisions:
- cards are presentational components plus pure key reducers; the answer path is a promise returned
  to `RpcChannel` (no separate request store).
- our own labels, hints, glyphs and colours; double border kept for approval (the row asks for it).
- the answer carries no `all` field: our protocol's `ApprovalAnswer` has none.

Tests we will write:
- 25-line command at 80 cols → 10 lines + "15 more lines"; long single line wraps (no clipping).
- digits pick; Esc and Ctrl+C deny; arrows clamp; smart_denied shows 2 options; allow_permanent.
- replayed request rings no bell; bell off rings none; `request.cancel` closes the card.
- desktop-only kind answered `-32601` through a real `RpcChannel` + fake server.
- pager keys, last-page close, page size floor.

## Decisions for the lead
- The bell and the "bell on prompt" flag are injected (`ringBell`, `bellOnPrompt`); config sync
  keeps `bellOnPrompt` in its extras until `UiState` grows a field.
- Ctrl+C on an open card is handled by the card itself (deny / cancel). R3's key router must leave
  keys to a card whose `ownsInput` is true.

Compare checklist (filled after the rewrite; reopened `prompts.tsx`, `createServerRequestHandler.ts`,
pager parts of `useInputHandlers.ts`):
- [x] choices in canonical order, filtered by `choices`; smart denial → once + deny; no permanent
  allow → always dropped (test: `apps/tui/src/cards/ApprovalCard.test.tsx::approval choices`)
- [x] command wrapped to cols − 8 (min 20), first 10 rows, "N more lines" (test:
  `ApprovalCard.test.tsx::shows 10 lines and '15 more lines' for a 25-line command at 80 columns`)
- [x] digits pick, letters do nothing, Esc / Ctrl+C deny, arrows clamp (test: `ApprovalCard.test.tsx::approval keys`)
- [x] status text + bell, none on replay or when the bell is off (test:
  `apps/tui/src/cards/serverRequests.test.ts::a replayed request rings no bell`, `::rings no bell when bell_on_prompt is off`)
- [x] withdrawal closes the card, no answer sent (test: `serverRequests.test.ts::request.cancel closes the card and nothing is answered`)
- [x] desktop-only kinds refused -32601 (test: `serverRequests.test.ts::answers a desktop-only request kind with -32601`)
- [x] pager keys, page size floor, close after the last page (test: `apps/tui/src/cards/Pager.test.tsx`)
- [x] gap found: a password or secret card withdrawn by the server vanished without a word — now a
  system line says the agent stopped waiting (pinned by
  `serverRequests.test.ts::a withdrawn password card says the agent stopped waiting`)
- Deliberate differences: the reference does not force `deny` into a `choices` list; ours does (our
  protocol says deny is always allowed). Our confirm card has no selected row: Enter confirms, as
  the row asks. Two requests at once are shown one after the other (a FIFO in `serverRequests.ts`).
- Not taken: vault unlock / save-login / one-time-code cards, shared-control (generation-bound)
  answers, connection cards (not in our protocol).

## Status

Done (2026-10-19). Built against R1 stand-ins (`state/uiStore.ts`, `state/overlayStore.ts`,
`state/destination.ts`, `theme/theme.ts`). Shared: `apps/tui/package.json` gains the workspace dep
`@indus/protocol`. `CardView.tsx` is ready for P12-27 to mount.
