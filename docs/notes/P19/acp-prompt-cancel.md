# Task: `session/prompt` and `session/cancel` (P19-09) Phase: 19 Owner: wave-30 builder A2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `acp_adapter/gateway_server.py`
(`prompt`, `_prepare_submit`, `_settled_receipt`, `_unknown_recovery`, `_has_unknown`,
`_blocked_admission`, `cancel`, `_cancel_admission`, `_slash_command`), `acp_adapter/gateway_reply.py`
(how the reply settles: final remainder, open tools flushed). Our `acp/backend.py`,
`acp/attached_host.py` (`submit` → journal + `retry`, `cancel`, `receipt`), `acp/local_host.py`,
`service/remote/host.py` (`wait`, admission → turn mapping, `OutcomeUnknown`),
`service/session_service.py` (`wait`, `on_turn_end`, `notify`), `acp/history.lost_text`. Closed
before writing.
Evidence: none.

What it does (own words): one editor prompt becomes one admitted turn, and the answer to
`session/prompt` comes only after that turn's updates reached the editor.

1. Unknown session id → `not_found` (`-32002`).
2. A lost turn in the session (state `unknown`, from the backend's ledger read) → refused with the
   `unknown_execution` text: nothing is sent; the user resolves it in a terminal (`indus -r <id>`
   then `/discard <admission>`), never by a resend (R09.11, R06.19, R03.21).
3. The blocks are converted (P19-08). A registered slash command (or alias) with no attachments
   goes to P19-12 and its reply ends the prompt (`end_turn`); `/etc/hosts …` and unknown `/words`
   are prompt text.
4. Retry identity: the fingerprint is taken before staging. If this session's previous submit was
   never acknowledged (transport loss, timeout, the editor's cancel) and the fingerprint matches,
   its `input_id` and staged text are reused, so the daemon's ledger answers with the same
   admission (one user row). Any other prompt — slash commands included — retires the retained
   identity; a later repeat is new work.
5. A `session/cancel` that arrives while the prompt is being staged or submitted is held; staging
   cancelled → `stopReason: cancelled` without sending; submit answered → the admission is cancelled
   at once.
6. Submit with `input_id`. Receipt `terminal` (a retry of a turn that already settled) → its final
   text is shown once and the outcome reported; `unknown` → refused as in 2.
7. Wait for the admission's end (`backend.wait`) racing: a lost turn announced for the session
   (blocked behind it → `unknown_execution`), a replay gap (transport failure), and transport
   replacement (`outcome_unknown`: "do not resend"). Then the projection is flushed up to that turn's
   `message.complete`, so every update precedes the response.
8. Outcome `failed` → JSON-RPC error with `data.reason = "admitted_turn_failed"`; `cancelled` →
   `stopReason: "cancelled"`; otherwise `end_turn`, with `usage` from the session's last usage event
   (`Usage{inputTokens, outputTokens, totalTokens, cachedReadTokens, …}`).

Cancel (`session/cancel`, a notification): unknown session → ignored (a notification has no
answer). A prompt still staging/submitting → remembered and applied once the admission id is known.
Otherwise the editor's **own** current admission is cancelled through `backend.cancel` (queued →
dropped; started → that admission's generation interrupted; anything else → nothing). A successor
turn from another surface is never interrupted (the backend reads the ledger row of this exact
admission before any interrupt).

Inputs → outputs: `session/prompt {sessionId, prompt[]}` → `{stopReason, usage?}` or an error;
`session/cancel {sessionId}` → nothing.

Files: `src/indus/acp/prompts.py`, `tests/contract/acp/test_prompt.py`; shared:
`src/indus/acp/backend.py` (`wait`, `lost` on the interface; `Settled`), `local_host.py`,
`attached_host.py` (their implementations), `agent.py` (handler module registration).

Vendor names used (schema v1.19.0): `session/prompt` (`PromptRequest{sessionId, prompt}`,
`PromptResponse{stopReason: end_turn|cancelled|…, usage}`), `session/cancel`
(`CancelNotification{sessionId}`), `Usage{totalTokens, inputTokens, outputTokens, thoughtTokens,
cachedReadTokens, cachedWriteTokens}`.

Edge cases / failure modes:
- two prompts at once in one session: the second waits for the first's admission to settle in
  the daemon (the daemon queues; the local host refuses busy with `session_busy`);
- the retained identity is per session and kept for the latest 64 sessions;
- a cancel with no current admission and nothing staging is a no-op;
- the waiter never hangs: transport loss, gap and lost-turn signals all wake it.

Our design differences / decisions:
- The reply is "settled" on the projection's `message.complete` for the turn (the daemon sends it
  after `turn.done`; the local host emits the same pair after each turn through
  `SessionService.notify`, so both hosts settle the same way).
- `failed` is a JSON-RPC error whose message is the turn's failure and `data.reason` is
  `admitted_turn_failed` (the error code number itself is `-32603`).

Tests we will write (both hosts where the behaviour exists):
- prompt → streamed chunks precede the response; `end_turn` with usage;
- lost ACK + same prompt → same admission and one user row (attached, reply cut);
  a different prompt retires the identity (two user rows);
- lost turn in the ledger → refusal text, no `prompt.submit` frame sent;
- cancel during a running turn → `cancelled`; cancel after a successor turn from another surface
  started does not interrupt it;
- failed turn → error with `admitted_turn_failed`; unknown session → `-32002`.

## Decisions for the lead
- `AcpBackend` gains `wait(session_id, admission_id) -> Settled(turn_id, outcome, final_text)`
  and `lost(session_id) -> [LostTurn]` (both hosts implement them). The local host emits
  `message.complete` and `session.usage` after every turn (as the daemon host does), so the
  projection settles replies the same way in both modes.

Compare checklist (filled after the rewrite; reopened `gateway_server.py` prompt/cancel, `gateway_reply.py`):
- [x] unknown session, lost turn refusal before any submit (test: `tests/contract/acp/test_prompt.py::test_an_unknown_session_is_not_found`, `::test_a_lost_turn_refuses_the_prompt_without_sending_it`)
- [x] retained identity reused only for the very next prompt with the same fingerprint (test: `test_prompt.py::test_a_lost_ack_and_the_same_prompt_reuse_one_admission`, `::test_a_different_prompt_retires_the_retained_identity`)
- [x] cancel held during staging/submit, applied when the admission is known (`prompts.cancel` / `_run`)
- [x] a settled admission's reply is shown once — from the stream when this editor saw it, else from the receipt (test: `test_prompt.py::test_a_lost_ack_and_the_same_prompt_reuse_one_admission`, `tests/unit/acp/test_projection.py::test_a_retry_receipt_shows_the_reply_once`)
- [x] failed / cancelled / end_turn mapping, usage from the last usage event (test: `test_prompt.py::test_a_failed_turn_is_an_admitted_turn_failed_error`, `::test_cancel_stops_this_editors_running_turn`, `::test_a_prompt_streams_its_reply_before_the_answer`)
- [x] cancel never interrupts a successor turn (test: `test_prompt.py::test_cancel_never_interrupts_a_successor_turn_from_another_surface`)
- Deliberate differences: `session/cancel` for an unknown session is ignored (it is a notification;
  the reference raised); prompts of one session wait for each other (a `/queue` while a reply runs
  waits in this adapter, in both modes); the receipt check waits up to 2 s for the turn's own
  `message.complete` (keyed by generation) before showing the receipt text.
- Not taken: the reference's prepared-control retirement (`_mutations.retire`), which belongs to
  its mutation journal; ours has no such journal here.

## Status

Done (2026-10-20), wave-30 builder A2. Shared: `acp/backend.py` (`Settled`, `wait`, `lost`),
`local_host.py` / `attached_host.py` (implementations; the local host now emits `message.complete`
+ `session.usage` after each turn), `agent.py` (handler registration, prompt book).
