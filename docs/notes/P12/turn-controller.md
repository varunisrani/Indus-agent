# Task: turn controller (P12-11) Phase: 12 Owner: wave-8 builder R1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `ui-tui/src/app/turnController.ts`
(interrupt, delta, completion, segments, tool rows, notices at turn end). Research `05-tui.md`
§3.13, §3.16, §3.28, trap R05.4. Evidence: none.

What it does (own words): the state machine of one turn on screen.
`idle → busy (turn.started) → [interrupted-sealed (local stop)] → idle (message.complete)`.
While busy, raw reply text collects in a buffer. When a tool starts, the text so far is sealed into
a segment; tools attach to the segment that introduced them (tools that ran before any text wait
and attach to the next segment or the final message). At completion the final text is compared
with the sealed segments: only the part not already shown becomes a new message, and a diff-only
segment is dropped when the final text repeats it. A failed turn that streamed nothing and whose
final slot is empty or a bare "Error: …" line gets a structured failure card instead
(`{layer, code, retryable}` from the turn's failure). Stop: the controller seals at once (later
deltas are ignored), sends `session.interrupt {session_id, generation}`, appends the sealed
segments and the partial text with an italic "interrupted" marker (or a system note when nothing
streamed), shows "interrupted" for 1,500 ms then "ready" — or stays busy with "interrupting…"
when queued input must wait for the real end. When the completion of that turn carries a stored
partial longer than what was sealed, the sealed bubble takes the stored text (R05.4). Events of a
finished turn (late tool or delta events) never bring entries back.

Inputs → outputs: controller methods called by the event handler (`start`, `delta`, `reasoning`,
`interim`, `toolStart`, `toolProgress`, `toolComplete`, `todos`, `done`, `complete`, `reset`) and
by input handling (`interrupt({keepBusy})`) → `turnStore`, `uiStore`, `transcriptStore`,
`overlayStore.closeFlowCards()`, `Rpc.call("session.interrupt")`.

Files: `apps/tui/src/turn/controller.ts`, `apps/tui/src/turn/controller.test.ts`.

Edge cases / failure modes:
- interrupt RPC answers `interrupted: false` (the turn already ended): nothing more happens; the
  completion that won the race reconciles the sealed bubble.
- interrupt RPC fails: a system line only when the same turn is still the current one.
- interrupt while queued input waits: busy stays true until `message.complete`, and the completion
  text is not appended a second time.
- completion without `turn.started` (resume mid-turn) still settles; a `turn.started` while an old
  archive is pending first settles the old one.
- `rendered`-like fields are never read; `delta` only appends.
- tool completion without a start (hidden start) still yields a trail row.

Our design differences / decisions:
- our wire has `turn.done` (outcome) followed by `message.complete` (final text): the outcome is
  recorded on the first, the transcript is settled on the second.
- tools attach to the preceding segment (the commentary that announced them), not the next one.
- failure layer comes from the exit reason (`provider_failed` → provider, `persistence_failed` →
  storage, budget/iteration/guardrail reasons → agent, interrupts → user/system).
- inline diffs: our protocol has no `inline_diff`; a tool result that is a unified diff (with
  `display.inline_diffs` on) becomes its own diff segment.

Tests we will write (fixture-driven where the fixtures cover it): commentary + 3 tools → 2 segments
with tools attached; final text de-duplicated; diff segment dropped when narrated; interrupt seals,
ignores later deltas, reconciles stored partial; interrupt with queue stays busy and does not
duplicate; RPC failure / no-op; error card from the failure; late events of a finished turn dropped.

## Decisions for the lead
- `interrupt()` seals locally before the RPC answers (responsiveness); the reference's shared-daemon
  path waits for the answer. A `false` answer leaves the sealed bubble to be reconciled by the
  completion.
- The italic marker text is `_interrupted_`, the system note "Stopped before any reply arrived."

Compare checklist (filled after the rewrite; reopened `ui-tui/src/app/turnController.ts`):
- [x] commentary + 3 tools → 2 text segments with tools attached (test: `controller.test.ts::seals
  commentary at tool boundaries…`)
- [x] final text de-duplicated; diff segment dropped when narrated (tests: `…::shows only the unseen tail…`,
  `…::drops a diff segment the final text repeats`)
- [x] interrupt seals, later deltas ignored, longer stored partial replaces the bubble (test: `…::seals
  at once, ignores later deltas, and takes the longer stored partial`)
- [x] interrupt with queue stays busy until the real end, nothing written twice (test: `…::with queued
  input stays busy…`)
- [x] completion wins the race / failed interrupt call reported only for the current turn (tests:
  `…::shows the full reply when the completion won the race`, `…::reports a failed interrupt call…`)
- [x] failure card from the turn failure; bare "Error:" slot replaced; partial kept (tests in `describe("failures")`)
- [x] late events of a finished turn dropped (test: `…::never bring back entries of a finished turn`)
- [x] idle clears flow cards of the finished turn, keeps the model picker (test: `…::idles and clears the approval…`)
- [x] notices flushed at the real turn end only (event-handler notice tests)
- Deliberate differences: local seal before the RPC answer (decision above); tools attach to the
  preceding segment; todos are kept across turns (session task list) instead of archived into the
  transcript at turn end; no reasoning-tag splitting inside the reply text (our protocol sends
  reasoning separately).
- Not taken: subagent upserts and spawn history (no subagent events yet), MoA reference blocks,
  tool-token estimates, "analyzing tool output" transient trail line.

## Status

Done (2026-10-19). `apps/tui/src/turn/{controller,toolVerbs}.ts`, `controller.test.ts` (24). Shared: none.
