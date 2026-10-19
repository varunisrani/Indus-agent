# Task: Clarify, sudo and secret cards (P12-22) Phase: 12 Owner: wave-8 builder R4

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `ui-tui/src/components/prompts.tsx`
(clarify part), `ui-tui/src/components/maskedPrompt.tsx` (whole), `ui-tui/src/app/useMainApp.ts`
(the `clarify.lock` answer path only), `ui-tui/src/app/createGatewayEventHandler.ts` (abandoned
clarify flush, `request.cancel` only). Research `05-tui.md` §3.17. Ours:
`docs/notes/P12/bridge-server-requests.md` (lock semantics, decline, merge of locked answers).
Evidence: none.

What it does (own words): a clarify request carries one or more questions. The card lists them all;
one is active at a time (Tab / Shift+Tab move between them, wrapping). A question with choices shows
them as numbered rows plus a last "Other" row that switches to free text; a question without choices
is free text at once. Enter locks the active answer, a digit picks a choice directly, Space toggles a
choice in a multi-select question. Every lock is sent at once with `clarify.lock` so a later timeout
cannot lose it; the card then jumps to the next open question. Locked answers stay editable (Tab
back, lock again) until the last lock, which answers the whole request on the server. Esc while
typing leaves the text field; Esc otherwise (or Ctrl+C) cancels the rest: the request is answered
with what is locked and the other questions skipped. If the server withdraws the request, the card
closes and its questions are written to the transcript with a "(timed out)" marker so the agent's
next words still make sense.

Sudo and secret cards are one masked line: dots instead of characters, Enter submits, Esc or
Ctrl+C sends nothing. The typed value lives only in the card's ref and goes straight into the
answer; it never enters a store, a log line or the history, and is never rendered.

Inputs → outputs:
- clarify `{questions[{id, text, choices?, multi_select?}]}` → per lock `clarify.lock {session_id,
  request_id, question_id, answer[]}` → `{remaining}`; `remaining: []` resolves the card with the
  full answer map. Free text skipped (empty) → `answer: []`.
- sudo `{reason}` → `{kind: sudo, password}` (null when cancelled or empty).
- secret `{name, prompt}` → `{kind: secret, value}` (null when cancelled or empty).

Files: `apps/tui/src/cards/ClarifyCard.tsx`, `apps/tui/src/cards/clarifyState.ts`,
`apps/tui/src/cards/MaskedCard.tsx`, tests `ClarifyCard.test.tsx`, `MaskedCard.test.tsx`.

Edge cases / failure modes:
- questions with blank text or no id are dropped; no usable question → answered at once (all skipped).
- multi-select Enter with nothing toggled locks the row under the cursor.
- "Other" text in multi-select is added to the toggled choices.
- a lock RPC that fails keeps the card open and shows the error line; the lock can be retried.
- a lock reply after the card closed (withdrawn) is ignored.
- masked input: pasted chunks append whole; Backspace removes one character; Ctrl+U clears.

Our design differences / decisions:
- the question state machine is a pure module (`clarifyState.ts`) so every key is testable
  without a terminal.
- answers are string lists on the wire (our protocol), not JSON-encoded strings.
- cancel / empty sudo or secret answers `null` (our protocol's "cancelled" value) rather than "".
- after the last lock the channel still sends its own `server_request.answer`; the bridge refuses it
  as not found (already answered) and the channel only logs a diagnostic line.

Tests we will write:
- two-question fixture: lock order (two `clarify.lock` calls in order), multi-select answer array,
  Other → free text, empty free text skips, Tab wraps and restores, timeout keeps locked answers and
  writes the "(timed out)" line.
- masked: typed value never appears in any rendered frame; Enter submits it; Esc sends null.

## Decisions for the lead
- `RpcChannel` has no way to mark a request settled without answering; a `settle(id)` would remove
  the redundant answer after the last clarify lock.

Compare checklist (filled after the rewrite; reopened `prompts.tsx` clarify part, `maskedPrompt.tsx`):
- [x] lock order, one `clarify.lock` per lock, last lock resolves (test:
  `apps/tui/src/cards/ClarifyCard.test.tsx::locks in order: a digit for the first, toggles then Enter for the multi-select`,
  `apps/tui/src/cards/serverRequests.test.ts::locks each answer through clarify.lock and the last one resolves`)
- [x] multi-select answer array, digits toggle, Enter with nothing toggled takes the cursor row
  (test: `ClarifyCard.test.tsx::multi-select Enter with nothing toggled takes the row under the cursor`)
- [x] Other → free text; empty text skips (test: `ClarifyCard.test.tsx::Other switches to free text`,
  `::empty free text skips the question`)
- [x] Tab / Shift+Tab wrap; a revisited answer is restored and can be re-locked (test:
  `ClarifyCard.test.tsx::returning to a locked question shows its answer, and it can be locked again`)
- [x] timeout keeps locked answers and writes the "(timed out)" record (test:
  `serverRequests.test.ts::a withdrawn clarify writes its questions with the locked answers as timed out`,
  `::flushOpenClarify records the open card when the clarify tool completed`)
- [x] masked value never in a frame; Enter submits; Esc / Ctrl+C send nothing (test:
  `apps/tui/src/cards/MaskedCard.test.tsx`)
- [x] gap found: a finished or cancelled clarify left no trace in the transcript — the card now
  writes its questions and answers marked "answered" / "cancelled" (pinned by
  `serverRequests.test.ts::locks each answer through clarify.lock and the last one resolves`,
  `::Esc on clarify keeps locked answers and skips the rest`)
- Deliberate differences: the reference records the finished set as a user message plus a trail
  row; ours writes one system line (the transcript row shapes belong to R2). Tab also works while
  typing free text.
- Not taken: shared-control clarify answers (one card per question over a generation-bound RPC).

## Status

Done (2026-10-19). `clarifyState.ts` added beside the row's files.
