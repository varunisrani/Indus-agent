# Task: Liveness watchdog and run budget (P08-16) Phase: 08 Owner: P08 slice B builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/turn_liveness.py` (settings,
sampling, commit under the activity lock), `agent/conversation_loop.py`
(`_maybe_inject_run_budget_wrapup` only). Research `01` §3.9 (run-budget cap row), §3.18, §3.23.
Closed before writing.
Evidence: none.

What it does (own words): a turn that shows no activity for `agent.turn_liveness_s` seconds
(default 600, checked every 15 s, `<= 0` turns it off) is stopped by the system so the session is
released. The decision is taken on an observation — the turn's generation and the time of its last
activity — and committed only if both are unchanged when re-read under the activity lock; a turn
that moved in between, or a newer turn, is never stopped. Tools and streams count as activity
through heartbeats (`TurnState.touch` on the loop, `Watchdog.heartbeat` from worker threads).
A wall-clock run budget (`agent.run_budget_s`, off by default; the CLI flag sets the same value)
adds one wrap-up notice when 80 % has passed, appended to the newest tool result that is not stored
yet (never a new system message); when the budget is spent the turn ends `budget_exhausted`.
Implicit provider timeouts are capped at `max(60 s, 50 % of what is left)`; explicit ones never.

Inputs → outputs: config/flag, activity stamps, fake or system clock → system interrupt with
exit `interrupted_by_system` + failure reason `liveness`, or `budget_exhausted`; one notice event;
the wrap-up text on a tool row; capped timeout numbers.

Files: `src/indus/core/liveness.py`, `tests/unit/core/test_liveness.py`. Shared:
`src/indus/core/prologue.py` (registration, assigned), `src/indus/core/turn_state.py` (one
optional `run_budget` field on `SessionRuntime`), `src/indus/core/tool_round.py` (wrap-up hook
right before tool results are stored), `src/indus/foundation/config/schema.py` (two keys).

Edge cases / failure modes:
- bad config values (text, NaN, inf, bool) → default with one warning; `<= 0` disables;
- activity exactly at 599 s keeps the turn alive; the abort fires on the first poll at/after 600 s;
- the watchdog sees a generation that is not the running one → it stops itself;
- a turn already ended → the watchdog ends; it is also stopped when the turn task finishes;
- wrap-up: no unstored tool row yet → try again later, latched only after a real append;
  list (multimodal) tool content → one more text part;
- budget spent while a tool runs → interrupt (tools get `cancelled`), exit `budget_exhausted`.

Our design differences / decisions:
- asyncio task on the turn's loop instead of a timer thread; commit happens synchronously on the
  loop after re-reading the stamp under the lock, so a loop-side touch cannot interleave;
- no sleep-aware idle meter (host sleep counts as idle) — follow-up if laptops complain;
- notice copy is ours; the timeout cap is a pure function the caller applies (`cap_timeout`).

Tests we will write:
- settings table; idle 600 s aborted, activity at 599 s kept; stale generation never aborted;
  a touch between observation and commit vetoes; threaded heartbeat keeps it alive;
- wrap-up injected once on the unstored tail tool row, not on stored rows, list content;
- budget exhausted ends `budget_exhausted`; timeout cap arithmetic.

## Decisions for the lead
- Poll period is a constant (15 s) with a keyword override for tests; no config key.
- `agent.run_budget_s` 0 = off. Applying `cap_timeout` to P03's implicit stream/request timeouts
  needs a hook in `model_call`/`streaming` (not ours): follow-up.
- The run budget lives on `SessionRuntime.run_budget`; it starts at the first turn that sees it
  (one-shot runs have one turn).

Compare checklist (filled after the rewrite; reopened `agent/turn_liveness.py`, the wrap-up helper):
- [x] Abort bound to (generation, timestamp), revalidated under the lock (test: `tests/unit/core/test_liveness.py::test_touch_between_observe_and_commit_vetoes`)
- [x] `<= 0` disables, invalid falls back (test: `tests/unit/core/test_liveness.py::test_settings_table`)
- [x] Wrap-up only on an unstored tool row, latched after a real append (test: `tests/unit/core/test_liveness.py::test_wrap_up_only_on_unstored_tool_row`)
- [x] Timeout cap `max(60, 50 % of remaining)`, explicit never capped (test: `tests/unit/core/test_liveness.py::test_cap_timeout_table`)
- Deliberate differences: the reference also stops a durable lease and dumps thread stacks; the
  lease is P17's, and our stall log names the last activity note instead of a stack dump.
- Not taken: separate poll config key, sleep-aware idle meter.

## Status

Done (2026-10-18). Registration in the prologue; wrap-up hook in the tool round; timeout cap
wiring handed on (decisions).

## Integration (lead)

Merged in the P08 wave 1 integration (2026-10-18) unchanged. Still open (recorded): `cap_timeout`
is not applied to P03's implicit stream/request timeouts (needs a hook in `model_call` /
`streaming`); the `--run-budget` CLI flag is not wired (config key only).
