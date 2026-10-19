# Task: Submission, busy modes and queue (+ interpolation) (P12-19) Phase: 12 Owner: wave-8 builder R3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `ui-tui/src/app/useSubmission.ts`
(busy modes, shell escape, interpolation, double Enter, edited queue items), `ui-tui/src/hooks/useQueue.ts`
(item shape, take/prepend/dequeue; canonical journal skipped), the drain effect in
`ui-tui/src/app/useMainApp.ts` (only the effect that drains on settle),
`ui-tui/src/protocol/interpolation.ts`. Research `05-tui.md` §3.12, §3.12.1, §3.16, R05.4, R05.18.
Ours: `docs/notes/P12/bridge-session-prompt.md` (`prompt.submit` `when_busy`, `session.steer`),
`docs/notes/P12/bridge-methods.md` (`shell.exec`). Evidence: none.

What it does (own words): Enter hands the composer text to a pipeline. An open completion is
applied first when that changes the command. An empty Enter does two jobs: twice within 450 ms
while a turn runs it interrupts (the turn stays "busy" if prompts are queued so they drain on the
real settle); while idle with a queue it sends the next queued prompt. A line ending in `\` moves
to the continuation lines. Otherwise continuation + line are joined with newlines and dispatched:
`/…` goes to the slash dispatcher (never queued), `!cmd` runs on the host via `shell.exec` and
prints the output as a system line plus `exit <code>` when the code is non-zero or nothing printed,
`{!cmd}` pieces are replaced by that command's output just before the text leaves (a queued draft
keeps the raw text until then), collapsed pastes expand to their full text, and the result goes
out with `prompt.submit`.

While a turn runs, plain text follows the busy mode: `queue` keeps it in our FIFO and sends one
item each time the session settles (unless an item is open for editing); `steer` asks
`session.steer` and queues the text if the bridge says nothing is running or refuses; `interrupt`
submits at once with `when_busy: "redirect"` and the bridge decides. A prompt carrying an image
token always queues. Up on the first line walks queued items before history: the item's text is
loaded for editing (Esc cancels, Ctrl+X deletes it); submitting it replaces the item and moves it
to the front (or sends it at once when idle). Ctrl+K sends the next queued item now. Nothing is
resubmitted automatically after a reconnect: an item leaves the queue when it is handed to the
bridge.

Inputs → outputs: Enter / Ctrl+K / settle edges, ui state (sid, busy, busy mode), composer
(text, continuation, tokens, queue edit) → RPCs `prompt.submit`, `session.steer`, `shell.exec`,
calls into the slash dispatcher and the turn controller's interrupt, transcript lines, history.

Files: `apps/tui/src/submit/submission.ts`, `apps/tui/src/submit/queue.ts`,
`apps/tui/src/submit/interpolate.ts`, tests `submission.test.ts`, `queue.test.ts`.

Edge cases / failure modes:
- no session yet: text goes to the queue and drains once a session is bound.
- `prompt.submit` refused with `SESSION_BUSY` (race with the settle): the text is queued at the
  front instead of being lost.
- interpolation failure (command refused, timeout): the whole submit is refused with a system
  line; the text is not sent with a placeholder.
- a reply arriving after the user switched session: only the transcript notice is skipped; the
  queue item has already left.
- empty Enter timing: `now - last < 450` interrupts; exactly 450 ms or more does not.
- queue edit index past the end (item drained meanwhile): edit cancelled.

Our design differences / decisions:
- An empty Enter while idle sends the next queued item on the first press (the row's wording); the
  reference waits for a second press. Recorded for the lead.
- Interrupt mode maps to `when_busy: "redirect"`; Ctrl+K while busy also uses `redirect`.
- The renderer shows the user line when it sends (`userLine`), sets `busy` at once so a fast second
  Enter queues; the turn controller (P12-11) owns everything after that.
- Interrupt and slash dispatch are injected (`interrupt`, `runSlash`), owned by P12-11 / P12-24.
- No pending-input journal (offline input) and no server-side queue: the bridge's own `queue`
  mode is not used, so the renderer FIFO stays editable.

Tests we will write:
- double empty Enter at 449 ms interrupts, at 451 ms does not; keepBusy when queued.
- steer: `queued:false` → item queued; RPC error → queued.
- queue mode: three prompts queue; settle edges drain them in order; no drain while editing.
- edited queued item replaces and moves to the front; Ctrl+X deletes it.
- `{!echo hi}`: queued raw, `shell.exec` called only when sent, sent text has `hi`.
- image token in steer mode queues; `!cmd` output and `exit 3`; continuation join.
- reconnect (busy flip, new session id) does not resend a sent item.

## Decisions for the lead
- Single empty Enter while idle drains the next item (row wording) instead of the reference's
  double press; change in `Submitter.submit`.
- `interrupt` busy mode and Ctrl+K-while-busy use `when_busy: "redirect"`.

Compare checklist (filled after the rewrite; reopened `useSubmission.ts`, `useQueue.ts`, the drain effect):
- [x] completion applies before submit (test: `apps/tui/src/submit/submission.test.ts::pipeline` "takes a highlighted completion first")
- [x] double Enter keepBusy with a queue (test: `submission.test.ts::double enter`)
- [x] shell output join and `exit N` rule (test: `submission.test.ts::pipeline` "runs !cmd on the host")
- [x] interpolation refuses the submit on failure (test: `submission.test.ts::interpolation` "refuses the whole send")
- [x] edited item keeps its tokens and goes to the front (test: `apps/tui/src/submit/queue.test.ts`,
      `submission.test.ts::queue editing`)
- [x] no drain while editing, drain one per settle (test: `queue.test.ts`, `submission.test.ts::busy modes`)
- [x] gap found: slash text with a collapsed paste must expand the paste before dispatch while the
      transcript keeps the label (pinned by `submission.test.ts::pipeline` "runs slash commands at once")
- Deliberate differences: first empty Enter drains when idle; redirect mapping; no journal.
- Not taken: canonical admission/receipts, `prompt.cancel`, `/queue` interception (P12-24 handles
  `/queue` as a slash command), streaming boost while typing (P12-11).

## Status

Done (2026-10-19).
