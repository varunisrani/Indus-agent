# Task: Approval, clarify, sudo and secret UI (P20-22) Phase: 20 Owner: wave-34 builder C

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `apps/desktop/src/app/session/hooks/use-message-stream/gateway-event/server-requests.ts`
(the blocking-input family comment and `handleServerRequest` dispatch tail) and `input-requests.ts`
(function list: settled events take the card down). The row's `server-requests` path is this file
in the pinned tree. Research 08 §3.9 (server→client requests), §3.13 (approvals & clarify). Closed
before writing.
Evidence: none.

What it does (own words): when the agent needs the person — approve a command, answer clarifying
questions, type a sudo password, give a secret — the daemon sends a server request. One card-stack
host per transcript owns all of them for its session. It is mounted once at transcript level and
keyed by request id, so a request that arrives before its tool row, and the tool row that appears
later, never remount the card (its input state and focus survive).
- **Approval**: shows the redacted command (full, selectable, monospace) and the reason; buttons
  are exactly the backend's `choices` (once / session / always / deny, in its order; deny always
  present). A button answers only on an explicit activation: a click whose pointer went down on that
  same button, or the keyboard (Enter/Space on the focused button, or the card's shortcut keys
  while the card has focus). Dragging onto a button, or a press that started elsewhere, does nothing.
  Deny takes an optional reason, passed to the model.
- **Clarify**: one card for the whole batch; each question is answered and locked on its own with
  `clarify.lock` (choices, multi-select, or free text; Skip sends an empty answer); a locked
  question is read-only; when the daemon answers `remaining: []` the request is complete and
  settles from the daemon's side.
- **Sudo**: a dialog with an unblurred backdrop that shows what the password is for, in full and
  selectable; the password goes only into the answer.
- **Secret**: a dialog that names the variable and the prompt; the value goes only into the
  `server_request.answer` (the backend stores it in the profile's `SecretStore`); nothing is kept in
  the renderer or any storage; Skip sends an empty answer.
- A request withdrawn or answered elsewhere (`server_request.cancelled`, `request.settled`) closes
  its card at once. Kinds the 1.0 app does not implement get no handler, so the client channel
  answers `-32601` at once (the daemon offers them elsewhere).

Inputs → outputs: open server requests for a session (`useOpenServerRequests`) → cards; activations →
`answer(...)` (first answer wins; the client sends `server_request.answer` with the generation) and
`clarify.lock`.

Files: `apps/desktop/src/chat/cards/{CardStack,ApprovalCard,ClarifyCard,SecretDialog}.tsx`,
`CardStack.test.tsx` (the sudo dialog lives in `SecretDialog.tsx`, both are masked-input dialogs).

Names needed from slice R (stand-in `src/gateway/serverRequests.ts`): `OpenServerRequest { id,
sessionId, kind, request, replayed, generation, answer(answer) → boolean }`, `ServerRequestStore {
handlers(), list(sessionId), subscribe(listener) }`, `ServerRequestStoreProvider`,
`useOpenServerRequests(sessionId)`; plus `useGatewayConnection().request` for `clarify.lock`.

Edge cases / failure modes:
- replayed card (after reconnect) answered: the store keeps the first answer; a second click is a no-op;
- clarify lock fails (request gone): the card shows the error and closes on the settle event;
- a deadline: shown as a countdown hint only (the daemon withdraws).

Our design differences / decisions:
- the pointer guard is "down and click on the same button", plus a 250 ms arming delay after a
  card appears (a click already in flight when a card pops under the cursor is ignored).

Tests we will write:
- approval before the tool row, then the tool row → same card DOM node (no remount);
- drag onto Allow (pointer down elsewhere) → no answer; click → answer with the choice; keyboard → answer;
- only backend choices shown; unknown kind → `-32601` frame from the channel;
- settled from another surface → card gone; clarify per-question locks; sudo/secret answers.

## Decisions for the lead
- Arming delay 250 ms (ours).
- A request without `choices` offers once / session / deny (never "always" unless listed).

Compare checklist (filled after the rewrite; reopened the files in `Read:`):
- [x] No remount when the tool row appears (test: `CardStack.test.tsx::keeps the same card instance
  when the tool row appears after the approval` — input state survives too).
- [x] Gestures never grant (tests: `::a drag over Allow does not answer`, `::ignores clicks before
  the card is armed`, `::a real click or the keyboard answers, once`).
- [x] Only the backend's choices (test: `::shows exactly the backend's choices, deny always among them`);
  gap found on compare: the reference offers session too when no list is given — aligned.
- [x] Unknown kind answered -32601 at once (test: `::an unknown kind is refused with -32601 at once`).
- [x] Settled elsewhere closes the card (test: `::a request settled from another surface closes its card`).
- [x] Clarify batch in one card with per-question locks (test: `::clarify: one card, each question
  locked on its own`); sudo and secret dialogs (two tests).
- Deliberate differences: the reference parks requests per session in several stores; ours keeps one
  store fed by the client channel's handler table. The reference asks a second confirmation before
  "always"; ours answers on the explicit activation (the row does not ask for one).
- Not taken: vault / MCP setup / preview / theme / tour desktop-only kinds (answered -32601).

## Status

Done (2026-10-21).

Integration (wave-34 merge, D-16.679): the cards run on slice R's `ServerRequestStore`;
`OpenServerRequest.answer` is bound to `store.answer(id, …)` (first answer wins in the store).

## Wave 48 (W48-c): a new card is scrolled into view

Status: done (2026-10-22, wave-48 builder c).

Read: none (a UI follow-up seen in the wave-47 screenshot pass; no reference file or Evidence).

Bug: the card stack sits after the transcript's rows, inside the scroll area. The transcript
follows the stream only when it re-renders itself; a card that opens re-renders only the stack,
and a tool row expanded above it had already pushed the end out of sight. The card could open
below the visible area with nothing pointing at it.

Fix: `useRevealOnOpen` (`src/chat/cards/ApprovalCard.tsx`) scrolls the card's own section into
view once, when it mounts (`scrollIntoView({ block: "nearest" })`: only as far as needed, nothing
when it is already visible). The approval card and the clarify card use it; the sudo and secret
dialogs are modal and need nothing. Cards are keyed by request id, so a re-render around a card
(a tool row appearing later) never scrolls again.

Tests: `src/chat/cards/CardStack.test.tsx` "scrolls a newly opened card into view once (W48-c)"
(failed on base `1b6e711`: no scroll; passes with the fix: once for r1, not on re-render, once for
a second card r2).

### Decisions for the lead
- **W48-c-2 (D-16.1077):** a card that opens is brought into view even when the reader had scrolled up: an
  open approval or question blocks the turn, so it wins over the reading position (minimal
  scroll, once per card).
