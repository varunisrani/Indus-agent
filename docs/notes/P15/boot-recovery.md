# Task: Boot recovery (P15-26) Phase: 15 Owner: wave-20 builder W3

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `gateway/run_startup.py` (restore
queue, auto-resume and notice parts: `_queue_startup_restore_event`, `_drain_startup_restore_queue`,
`_finish_startup_restore`, `_resume_pending_candidates`, `_resume_owner_authorized`,
`_schedule_resume_pending_sessions`, `_recover_unclean_sessions`, `_ledger_crash_left_replies`,
`_crash_left_reply`), `gateway/run_turn_persistence.py` (marker hand-off: function list only).
Research 06 §3.1 step 7, §3.22 "Interrupted turns"; R06.19, R03.21.
Evidence: none.

What it does (own words): before any chat message runs after a start, the daemon settles what
the previous process left behind, in this order, inside the startup-restore window (messages
that arrive meanwhile are held by the intake and replayed afterwards, in order):
1. W1's ledger sweep (a RECOVERY part) redelivers owed finals.
2. Crash-left turns: a route still carrying the durable active-turn marker means the previous
   process died mid-turn (a graceful stop always clears it).
   - If the session's transcript already holds a final assistant reply written after the turn
     started, that reply is owed, not a new answer: the marker is cleared first, then the reply
     is delivered once with a "recovered reply" label (through `recovery.crash_reply_sink`, which
     W1 may point at the ledger). Never regenerated.
   - Otherwise the turn's side effects are unknown: the route is paused (`resume_pending`, reason
     `unknown_turn`, the marker token kept as its generation). Nothing is replayed (R06.19, R03.21).
     While paused, the first message in that chat gets one notice (what happened, `/reset` starts
     fresh) and is not run; later messages are dropped silently; commands still work. `/reset`
     (`/new`) rotates the route, which clears the pause. An operator can `resolve_unknown(store,
     key, token)`: it clears the pause only when the stored token still matches (generation check).
3. Restart-interrupted routes (flagged by a graceful stop, reason `restart_interrupted`): each is
   resumed at most once — the flag is cleared before the resume starts, so a crash during the
   resume shows up next boot as an unknown turn, never a second resume — when the flag is younger
   than 3,600 s, its owner is still authorized, its platform is connected and the restart-loop
   guard did not trip (tripped or stale: the flag stays; the person's next message simply runs).
   The chat gets a heads-up line, then an internal turn tells the agent it was cut off by a
   restart and should check before repeating anything.
4. Restart notices: when this boot follows an earlier run, each connected platform with a home
   channel and `restart_notice` on gets one "back online" line.
5. The held messages are replayed.

Inputs → outputs: route rows (marker, resume flag), transcripts, the previous status file,
restart-loop guard → paused routes, one resume per interrupted route, labelled recovered replies,
notices, replayed messages.

Files: `src/indus/gateway/daemon/recovery.py`, `tests/integration/gateway/test_boot_recovery.py`.

Edge cases / failure modes:
- a marker older than twice the turn timeout (≥ 1 h) with no final → still unknown (never replayed).
- an unknown route whose origin cannot be parsed → paused, no notice possible until they write.
- the resumed turn itself fails → normal failed-turn notice; the flag is already gone.
- a message held during restore for a route being resumed → replayed after; it meets the busy
  guard of the running resume (queued behind it), never a duplicate agent.
- three boots in a row after one interrupted turn → exactly one resume.

Our design differences / decisions:
- No durable admission FIFO exists before P17; the durable active-turn marker on the route is our
  "started admission" record, and the pause lives on the route row (`resume_pending` +
  `resume_reason`), so no schema change is needed.
- A graceful stop flags interrupted routes `restart_interrupted` (resumable); only a crash leaves
  an unknown turn. The reference resumes crash-left turns that have no reply; we pause them, as
  R06.19 asks.
- The default crash-reply path sends directly (at most once: marker cleared before the send);
  with W1's ledger it becomes at-least-once with the ledger's labels.

Tests we will write:
- SIGTERM mid-turn, boot → one heads-up + one resume turn; boots 2 and 3 → none.
- crash (marker left, no reply) → unknown: the next message gets one notice and no model call; a
  second message no notice; `/reset` unblocks and the next message runs.
- crash with the final in the transcript → delivered once with the label, no model call; next boot
  nothing.
- restart-loop guard tripped → no resume, the flag stays.
- `resolve_unknown` with a wrong token refuses, right token clears.
- restart notice once per platform per boot; opt-out respected.

## Decisions for the lead
- Pause notice / heads-up / recovered-reply label / back-online copy are ours (module constants).
- `resolve_unknown` is a gateway function for now; P15-31's `/resolve-unknown` route and any CLI
  verb should call it (or it moves to `state/` when the CLI needs it).

- A paused conversation still runs commands; a command that turns into a turn (a `/skill`
  rewrite) runs too (an explicit new request from the person).

Compare checklist (filled after the rewrite; reopened `gateway/run_startup.py`
`_schedule_resume_pending_sessions`, `_resume_pending_candidates`, `_resume_owner_authorized`,
`_recover_unclean_sessions`, `_ledger_crash_left_replies`, `_crash_left_reply`, `_finish_startup_restore`):
- [x] held inbound replayed after recovery, in order (P15-11's restore queue; the daemon calls
  `begin_restore` before connecting and `finish_restore` after recovery)
- [x] resume claims the key synchronously before any await (`run_admitted(background=True)`), so a
  replayed message queues behind it, never a duplicate agent
- [x] resume only fresh (≤ 3,600 s), authorized, platform connected, guard not tripped
  (`tests/integration/gateway/test_boot_recovery.py::test_a_stale_interrupted_flag_is_not_resumed`,
  `::test_a_tripped_restart_loop_guard_skips_the_resume_and_keeps_the_flag`)
- [x] at most once across three boots (`::test_a_turn_cut_by_a_stop_resumes_once_across_three_boots`,
  `tests/integration/gateway/test_signals.py::test_sigterm_mid_turn_interrupts_notifies_and_the_next_boot_resumes_once`)
- [x] a stored, unsent final is delivered (labelled), never regenerated
  (`::test_a_reply_written_before_a_crash_is_delivered_once_with_a_label`)
- [x] unknown turns pause, one notice, `/reset` unblocks, generation-checked resolve
  (`::test_a_crash_left_turn_pauses_the_conversation_until_reset`, `::test_resolve_unknown_checks_the_generation`)
- [x] restart notices once per boot per platform, opt-out respected
  (`::test_restart_notices_reach_home_channels_once_per_boot`)
- Deliberate differences: a crash-left turn without a stored reply is paused (R06.19) where the
  reference resumes it once; the default recovered-reply path is at-most-once (marker cleared
  first) until W1's ledger takes `crash_reply_sink`; silence tokens in a recovered reply are handled
  by the outbound sender like any final (no separate machinery-turn rules).
- Not taken: the bounded restore-gate timeout (the resume runs as a claimed background task, so
  the gate opens at once), startup warm-up, multiplexed-profile restores.

## Status

Done (2026-10-19). W1 may set `daemon.recovery.crash_reply_sink` in its part's `prepare` to route
recovered replies through the ledger.
