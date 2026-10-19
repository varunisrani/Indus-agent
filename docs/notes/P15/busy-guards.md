# Task: Busy guards and busy-input modes (P15-12) Phase: 15 Owner: wave-18 builder G4

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/run_busy.py` (command
tables, busy slash routing, queue/steer/interrupt handling, busy acks),
`gateway/platforms/base.py` (`merge_pending_message_event`, inbound text batching,
`handle_message` / the while-active path). Research 06 §3.9, §3.10, R06.1. Evidence: none.

What it does (own words): decides what a message does to a conversation that is already
replying.

- Guard 1 (adapter level, in `AdmissionPipeline.accept`): a key with an active claim or running
  turn does not start a handler task: the event is authorized and charged to the bot-loop guard
  once, then stored or merged into the key's pending queue and the running turn is signalled per
  the busy mode. Control commands skip this and run inline.
- Guard 2 (runner level, inside the handler after authorization, pause gate and prompt replies):
  a recognised command runs its mid-run variant (`stop`, `new`, `queue`, `steer`, `goal`, `loop`),
  else its normal handler when its registry busy policy allows and it is in the any-time table,
  else one fixed "not while I'm replying" answer. Plain messages go to the same busy-mode logic as
  guard 1.
- `/stop`, `/new`, `/queue`, `/status`, `/approve`, `/deny` are awaited inline by `accept`, never
  through a background task (R06.1).
- Busy modes (`display.busy_input_mode`): `interrupt` (default) redirects the running model call
  with the text (`SessionService.redirect`: shown text kept as checkpoint, finished tool results
  kept; a foreground command moves to background inside the agent); a redirect the turn refuses
  queues the text as the next turn (never a forced interrupt: it could cut off subagents); media
  interrupts the turn and runs next. `queue`: every follow-up is its own next
  turn in arrival order; a photo burst merges into the queued photo. `steer`: text goes into the
  running turn after its next tool call (`SessionService.steer`); before the turn started, or for
  media, it falls back to queue.
- Busy text in interrupt and steer modes merges in a quiet window (0.35 s, hard cap 1.0 s from
  the first piece; `display.busy_text_quiet_s` / `busy_text_cap_s`); one short acknowledgement per
  merge (queue mode: per message) unless `display.busy_ack` is off; the first acknowledgement of
  the install carries a one-time tip and sets `onboarding.seen.busy_input`.
- Split-paste batching (`batching.py`): plain text from one key is held for a quiet window
  (0.3 s; 1.0 s when the last chunk is within 100 units of the platform's split threshold, 4,000 by
  default; platform `extra` keys `text_batch_delay_seconds` / `text_batch_split_delay_seconds`
  clamped to 2.0 s / 4.0 s) and merged into one event (texts joined by a newline, media
  concatenated).

Inputs → outputs: events for a busy key → service calls (redirect / steer / interrupt), queued
events, acknowledgements; command events → command replies.

Files: `src/indus/gateway/core/busy.py`, `src/indus/gateway/core/batching.py`,
`tests/unit/gateway/core/test_busy.py`, `tests/unit/gateway/core/test_batching.py`.

## API
`indus.gateway.core.batching`:
- `Batcher[T](clock, *, flush, delay_for, cap=None, merge)`: `add(key, item)`, `flush_now(key)`,
  `pending(key)`, `aclose()`; one timer task per key, restarted by each item, bounded by `cap`.
- `TextBatchPolicy(quiet=0.3, split_quiet=1.0, split_threshold=4000, near=100)`:
  `delay_for(last_len)`, `from_extra(extra, split_threshold=None)` (clamped to the ceilings).
- `merge_events(first, later)` (texts joined with a newline, media concatenated, photo kind wins).

`indus.gateway.core.busy`:
- `CONTROL_COMMANDS`, `MID_RUN_COMMANDS`, `BUSY_MODES`, `is_control(event)`, `busy_mode(raw)`.
- `PendingQueue`: per-key FIFO (`push`, `push_front`, `pop`, `peek_all`, `clear`, `depth`) with
  photo-burst merging and a per-key cap (20).
- `BusyGuard(pipeline)`: `adapter_level(event, key)`, `runner_level(event, key) -> bool`,
  `while_busy(event, key)`, `aclose()`; ack copy in `ack_text(mode)` and `ONE_TIME_TIP`.

Edge cases / failure modes:
- a redirect rejected (the turn is between phases / not started) → queued as the next turn.
- steer before the agent started → queue.
- `/stop` during the claim's setup (no session yet) drops the claim at once.
- the pending queue is capped; the overflow is dropped with a log line and a notice.
- `/approve` while a turn waits for approval works from both guards.

Our design differences / decisions:
- Guard 1 lives in the intake (adapters only emit) instead of each adapter's base class.
- No Telegram follow-up grace window, no drain-time queueing (P15-25 decides drain replies), no
  subagent/compression demotion checks (the agent's redirect refuses unsafe moments itself).
- Busy text in queue mode is never merged (each message is its own turn, as the row says).
- Ack and tip copy are ours.

Tests planned: approval unblocked by `/approve` at both guard levels; each mode with the fake
clock (interrupt redirects, queue runs in order, steer lands in the running turn and falls back to
queue before start); three split pastes merged into one turn; near-threshold chunk extends the
window; one ack per merge; first ack carries the tip once.

## Decisions for the lead
- Pending queue is in memory (per process); P15-26 boot recovery does not restore it.
- `display.busy_*` keys from P15-02 are read on every message (no restart needed).

Compare checklist (filled after the rewrite; reopened `gateway/run_busy.py` busy routing and
tables, `gateway/platforms/base.py` pending merge and text batching):
- [x] `/stop`, `/new`, `/queue`, `/status`, `/approve`, `/deny` bypass both guards, inline
  (`tests/unit/gateway/core/test_busy.py::test_a_turn_waiting_for_approval_is_unblocked_by_approve[adapter|runner]`,
  `::test_control_commands_run_inline_while_busy`,
  `::test_an_idle_queue_command_from_the_adapter_does_not_hold_up_reading`)
- [x] mid-run variants, then busy policy + any-time table, else the fixed reply
  (`::test_commands_while_busy_use_the_busy_policy`,
  `tests/unit/gateway/core/test_commands.py::test_every_command_while_busy_runs_its_variant_or_the_fixed_reply`)
- [x] modes: interrupt redirect with one merged text and one ack, queue in order, steer into the
  turn, steer before start → queue, media interrupts (`::test_interrupt_mode_redirects_with_one_merged_text_and_one_ack`,
  `::test_queue_mode_runs_each_follow_up_as_its_own_turn_in_order`, `::test_steer_mode_lands_in_the_running_turn`,
  `::test_steer_before_the_turn_started_falls_back_to_the_queue`, `::test_media_in_interrupt_mode_stops_the_turn_and_runs_next`)
- [x] acks optional, one-time tip persisted (`::test_acks_can_be_turned_off_and_the_tip_shows_once`)
- [x] photo bursts merge, other media stay separate, queue capped (`::test_pending_queue_merges_photo_bursts_and_is_capped`)
- [x] guard 1 re-authorizes (`::test_unauthorized_messages_to_a_busy_chat_are_dropped`)
- [x] split-paste windows 0.3 s / 1.0 s near the threshold, clamps 2.0 s / 4.0 s, quiet window
  anchored at arrival (`tests/unit/gateway/core/test_batching.py`, 16 tests)
- Deliberate differences: no Telegram post-start grace window, no drain-time queueing (P15-25),
  no subagent / compression demotion checks for media (text never forces an interrupt); queue
  depth cap 20 (ours); a merged photo burst keeps the first event's ids.

## Status

Done (2026-10-19). `src/indus/gateway/core/busy.py`, `src/indus/gateway/core/batching.py`; tests
`tests/unit/gateway/core/test_busy.py` (14), `tests/unit/gateway/core/test_batching.py` (16).
