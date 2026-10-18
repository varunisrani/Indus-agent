# Task: Compression engine + gates + cooldowns (P08-05) Phase: 08 Owner: wave-10 builder A

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/compression_facade.py`
(module docstring, the timeout wrapper's shape), `agent/conversation_compression.py` (commit
pipeline docstrings, aux-ceiling helper), `agent/turn_context_compaction.py`
(`_preflight_compression`), `agent/turn_preflight.py` (`run_preflight_compression`),
`agent/context_compressor.py` (failure ladder constants ~740–830, `_on_summary_failure`,
`should_compress_info`, `_tripped`, the anti-thrash recovery window, `_apply_real_prompt_verdict`,
`_assemble_head/_assemble_tail/_summary_placement`, `compress`). Spec: research `01` §3.19 (gates,
attempt cap, cooldowns, assemble), `03-state.md` §3.10 (compression records, counters), trap
R01.9. Closed before writing.
Evidence: none.

What it does (own words): one `ContextEngine` per session decides when to compress and does it.
Interface: `should_compress`, `compress`, `on_session_start/end`, `update_from_response`; the
built-in engine is chosen by `context.engine` (`builtin`; any other name logs and falls back —
plugins never activate by themselves, P13 adds the plugin path).

Gates: turn-start preflight, pre-request pressure (and post-tool, which is the pre-request check
right after a tool round), provider-proven overflow (P08-06), manual `/compress [focus] [here N]
[--preview]`, idle (opt-in, off by default). Every automatic gate asks the meter (P08-01):
`ok` → nothing; `wait` → nothing (one request to get real usage); `compress` → try.

Limits: at most `compression.max_attempts` (3, ≤ 10) ineffective attempts per turn; the budget is
re-armed only when a provider response reports prompt tokens below the trigger after a completed
compaction. A compaction is "ineffective" when the first real reading after it is still over the
trigger; two such readings trip a breaker (automatic compression stops; after 300 s one probe is
allowed). Failure cooldowns are stored on the session row (P02 counters): stalls/timeouts
60 → 300 → 900 s (never below `compression.timeout_s`, 120 s); summaries cut by the output cap
climb the same ladder on their own counter; decode / closed stream / empty → flat 30 s; manual
compress clears the cooldown; a stall that repeats (second consecutive) commits the deterministic
summary; three consecutive overload aborts commit it too; `abort_on_summary_failure: true` always
aborts (transcript unchanged).

Assemble: head + summary row (role chosen so roles alternate) + tail; a call whose results were
cut away gets stub results, a result whose call was cut away is dropped; then
`PromptLifecycle.invalidate("compaction")` — the one sanctioned rebuild — and the anchor is
cleared. Commit: in place (default; pre-compaction rows soft-archived via a P02 write with the
concurrent-tail rule) or rotating (child session via `publish_continuation`). The todo list
(P05) is re-injected after the summary when one is open. Events: `Status(COMPRESSING)` while it
runs and a `Notice` keyed `session.compressed` carrying the mode. A preflight that times out
while the request is still over the window ends the turn `compression_exhausted` with the
"nothing was dropped, start a new session" message.

Inputs → outputs: rows + meter + settings + summariser → `Outcome{status: compressed | skipped |
blocked | failed | aborted, messages, summary, mode, fallback_used, reason}`; durable counters via
`CooldownBook`.

Files: `src/indus/core/compression/engine.py`, `src/indus/core/compression/cooldown.py`,
`tests/unit/core/compression/test_engine.py`, `tests/unit/core/compression/test_cooldown.py`;
shared updates: `src/indus/core/loop.py`, `src/indus/core/prologue.py`,
`src/indus/core/turn_state.py` (one `context` field), `src/indus/core/agent.py` (engine wiring),
`src/indus/state/compression.py` (in-place compaction write), `src/indus/foundation/config/schema.py`.

Edge cases / failure modes:
- a summary that takes longer than `timeout_s` → `stall` (ladder), the turn goes on uncompressed;
- the store refuses (`CompressionBusy`, `TranscriptMoved`) → nothing committed, attempt refunded;
- interrupt during the summary call → cancelled, counters restored (P02 snapshot/restore);
- manual compress with nothing to compress → "nothing to compress yet", no strike;
- `here N`: only the newest N rows are kept verbatim; `--preview` reports what would happen.

Our design differences / decisions:
- Gates live in the loop as two calls (`preflight` in the prologue, `before_request` in the
  iteration); the engine owns all policy.
- Ladder rung counters (stall, truncation) are stored next to the anchor in `model_config`
  (`compression_ladder`) because P02 has no columns for them; the deadline itself uses the P02
  cooldown column.
- The `session.compressed` signal is a `Notice` with that key (no new protocol event type —
  avoids a protocol/golden change in this slice).
- `/compress` slash wiring through the service port is not in this slice (see lead decisions);
  `Agent.compress(focus, keep_last=, preview=)` is the entry point it should call.

Tests we will write:
- fake clock: cooldown ladder per failure class (stall, truncated, decode, empty, overload);
- attempt cap and re-arm; breaker after two ineffective readings; probe after 300 s;
- manual compress clears the cooldown; stall ×2 → fallback committed; overload ×3 → fallback;
- `abort_on_summary_failure` aborts; alternation valid after every assemble (seeded property);
- prompt rebuilt exactly once per compaction; anchor cleared;
- loop integration: preflight compresses a long session through `TurnKit` and the turn completes.

## Decisions for the lead
- `/compress` CLI/service wiring (control_cmds + `session.compress` request) left for the P07
  integration: the engine exposes `compress(..., manual=True, focus=, keep_last=, preview=)` and
  `Agent.compress()`; `cmd_compress` still says it is not wired.
- `session.compressed{mode}` carried as `Notice(key="session.compressed")`; a typed event can be
  added to `protocol.events` later with a golden regeneration.
- In-place commit write added to `state/compression.py` (`compact_in_place_in`) — P02 file, shared.

Compare checklist (filled after the rewrite; reopened the compressor's failure
code, `agent/compression_facade.py`, `agent/turn_context_compaction.py`, `agent/turn_preflight.py`):
- [x] Ladder 60/300/900 per class, floor `timeout_s`; flat 30 s for decode/empty (test:
  `tests/unit/core/compression/test_cooldown.py::test_ladder_per_failure_class`, `test_cooldown.py::test_flat_and_other_classes`)
- [x] Truncation on its own counter (test: `test_cooldown.py::test_truncation_climbs_its_own_rung`)
- [x] Counters durable across a restart (test: `test_cooldown.py::test_counters_survive_a_restart`)
- [x] Attempt cap and re-arm on a real reading under the trigger (test: `test_engine.py::test_attempt_cap_and_rearm`)
- [x] Breaker after two ineffective readings, one probe after 300 s (test: `test_engine.py::test_breaker_after_two_ineffective`)
- [x] Manual compress clears the cooldown (test: `test_engine.py::test_manual_compress_clears_the_cooldown`)
- [x] Stall ×2 → deterministic summary (test: `test_engine.py::test_repeated_stall_commits_the_fallback`)
- [x] Overload ×3 → deterministic summary (test: `test_engine.py::test_three_overloads_commit_the_fallback`)
- [x] `abort_on_summary_failure` (test: `test_engine.py::test_abort_on_summary_failure_keeps_the_transcript`)
- [x] Alternation after every assemble (test: `test_engine.py::test_property_alternation_after_assemble`, seeded)
- [x] Prompt rebuilt exactly once per compaction; notice and status (test:
  `test_engine.py::test_loop_preflight_compresses_and_rebuilds_once`)
- [x] In place (compacted rows archived, late rows carried) and rotating commits (tests:
  `test_engine.py::test_loop_in_place_archives_compacted_rows`,
  `test_engine.py::test_in_place_write_carries_rows_that_arrived_meanwhile`,
  `test_engine.py::test_loop_rotating_mode_continues_in_a_child`)
- [x] Gap found: a model/provider switch resets the old route's cooldown and strikes — added to
  `set_route` (test: `test_engine.py::test_route_change_clears_the_old_routes_cooldown`)
- Deliberate differences: a JSON-decode failure aborts like a closed stream (the reference
  commits the fallback for JSON decode only); no aux→main retry inside one attempt; no
  structural no-op backoff timer (a no-op is simply not counted); the summary deadline is a
  clock race, not a progress-watching thread; the compression note is not appended to the
  system prompt (our prompt is rebuilt instead).
- Not taken: idle gate (setting exists, off; the host timer belongs to P15/P17), memory-provider
  context, the fallback-streak "bench the summary model" rule.

## Status

Done for the engine, cooldowns, loop gates, `Agent.compress` and Agent wiring (2026-10-18).
Handed to the lead: `/compress` slash command → `Agent.compress` through the session port;
register the engine on slice B's `UsageLedger.add_listener` if the loop's direct
`observe_usage` call is replaced; after a rotating compaction the host must adopt the child id
(the Agent's sink still names the parent).

## Integration (lead)

Merged in the P08 wave 1 integration (2026-10-18).
- `/compress [focus] [here N] [--preview]` is wired: `cmd_compress` → `SessionPort.compress`
  (`HostPort`) → `SessionService.compress` → `Agent.compress`. The engine's notice reports the
  result; the command prints only previews, "nothing to compress"/blocked reasons and a move to a
  continuation. Busy turns are held by the dispatcher.
- Usage reaches the engine only through the ledger (`expect_usage` + `on_usage`, see usage-cost).
- Rotating compaction is adopted end to end: `_rotate` moves the engine (`adopt_session`: anchor
  and counters on the child row) and calls `SessionRuntime.on_continued`; the Agent moves its sink,
  ledger and cleanup ids and tells `on_session_moved` listeners; `SessionService` re-keys the
  attached session, keeps the old id as an alias for every call (`current_id`) and moves the lock
  when it can; the REPL follows the first event carrying the new id and the slash context follows
  the REPL.
- `ContextEngine` protocol gained `expect_usage`, `on_usage`, `adopt_session`.
