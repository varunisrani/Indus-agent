# Task: Edit diagnostics and the LSP stop gate (P24-16) Phase: 24 Owner: P24 slice L1 builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/lsp/edit_check.py` (write
budget, serialized checks, baseline reuse, takeover), `agent/lsp/deferred.py` (owners, caps,
pending tracking, turn-end settle), `agent/lsp/range_shift.py` (line map over a diff),
`agent/turn_stop_gates.py` (the LSP gate and the gate order). Closed before writing. Evidence: none.
Protocol pin: LSP 3.17 `Diagnostic` (range, severity 1–4, code, source, message).

What it does (own words): after `write_file`/`patch` the edit is checked by the language server
without holding the write. The check syncs the bytes the edit wrote, waits for the server's verdict
and, only when that verdict has errors, compares it with the verdict on the bytes from before the
edit (cached per file when an earlier check saw exactly those bytes, ≤256 files): the old errors
are moved to their new lines (a line map built from a diff of before and after; errors on deleted
lines drop out) and removed, so only errors the edit introduced are reported. The edit tool waits
at most 0.08 s inline; a slower verdict is queued for the editing session and rides that session's
next tool result (≤64 sessions, ≤32 files each). When the model answers without another tool call,
the LSP stop gate waits up to 5 s for that session's checks still running; if new errors arrived it
hands the answer back as an interim row, adds a message listing the errors and the turn goes on
(A28). One check is bounded at 60 s including a cold spawn. A newer edit of the same file by the
same session takes the older check over: it diffs against the older baseline, so an error the first
edit made is still reported unless the second fixed it. Checks of one file run one at a time.

Inputs → outputs: `lint.lint_edit(path, before, after)` with an active `EditDiagnostics` and a
bound owner → the lint report gains the new LSP errors (status `introduced`) when the verdict came
inline; `LspToolHooks` (a `ToolHooks` seam) binds the owner for a call and appends queued notes to
the next tool result; `core.stop_gates_lsp.LspStopGate` → a nudge for `core.stop_gates`.

Files: `src/indus/tools/lsp/diagnostics.py`, `src/indus/core/stop_gates.py` (registry, shared with
P24-17), `src/indus/core/stop_gates_lsp.py`, tests `tests/unit/tools/lsp/test_diagnostics.py`,
`tests/unit/core/test_stop_gates_lsp.py`; updated `src/indus/tools/builtin/lint.py` (P05-20).

Edge cases / failure modes:
- server gives no verdict in time → nothing reported (never stale lists: "no data" ≠ "clean");
- error present before the edit and only moved down by inserted lines → not reported;
- the same message on a second line → reported (range is part of the identity);
- a new file → every error is new;
- dead server mid-check → the pair is marked broken; the edit result is unchanged;
- a session that never takes its notes → oldest owners dropped past 64; 32 files per owner;
- stop gate fires at most 3 times per turn, and only when there is something new; once delivered
  a note is gone, so the loop re-enters exactly once for one new error;
- warnings/hints are not "errors": only severity 1 (or none given) is reported.

Our design differences / decisions:
- Async on the caller's loop (no loop thread); the 0.08 s inline wait is measured on the clock.
- Notes ride the next tool result through the `ToolHooks.transform` seam (no change in the
  dispatcher); the owner is the session id, else the task id.
- The gate's nudge is a durable user row after the interim assistant row (never scaffold), so
  replaying the transcript later shows the same bytes.
- The per-turn list of edited files is kept here (`turn_edits`) and also feeds verify-on-stop.

Tests we will write: range shift table; delta with moved pre-existing error; new error reported
once; inline result inside 0.08 s vs deferred delivery on the next tool result; per-owner caps;
takeover keeps the first edit's error; stop gate re-enters once; settle bounded at 5 s; lint
report merge; 60 s ceiling.

## Decisions for the lead
- Only error-severity diagnostics are reported (the row says "errors"); warnings stay quiet.
- Wiring: wrap the dispatcher's hook seam with `LspToolHooks(inner)` and register
  `LspStopGate` in the loop's `StopGates`; install one `EditDiagnostics` per process.

Compare checklist (filled after the rewrite; reopened the four files above):
- [x] write budget 0.08 s, 256 cached verdicts, 64 owners × 32 notes, 5 s turn-end wait, 60 s
  ceiling, 3 gate notes per turn (tests in `test_diagnostics.py`, `test_stop_gates_lsp.py`)
- [x] baseline shifted across the edit before the set difference (`test_diagnostics.py::test_new_error_reported_and_preexisting_one_not`)
- [x] takeover by the same session keeps the older baseline (`::test_second_edit_by_same_session_keeps_first_baseline`)
- [x] checks of one file serialized (`::test_checks_of_one_file_run_one_at_a_time`)
- [x] gap found: a superseded note is restored when the newer check gets no verdict — fixed by
  carrying the older note in the in-flight record, pinned by `::test_no_verdict_restores_the_older_note`.
- Deliberate differences: no seed-push special case (the server's first push is accepted when its
  version matches); no synchronous tsserver request; owner binding through the hook seam.
- Not taken: the event log file; the kanban terminal-tool guard (board slice B1).

## Status

Done (2026-10-21). `lint.py` gains the LSP tier (only when a checker is installed; otherwise
unchanged). The loop and dispatcher wiring is for the lead:
1. dispatcher hook seam: `LspToolHooks(inner=<the plugin seam>)` (binds the owner per call, so
   edits are recorded per turn even with LSP off, and hands queued notes to the next result);
2. `core/loop.py::_handle_answer`, just before a text answer is accepted (after the intent-ack
   check): `decision = await gates.run(StopContext(session_id, turn_id, final,
   tuple(diagnostics.turn_edits(owner, turn_id)), task_id=…))`; when set,
   `state.add(*continuation_rows(decision, final, reasoning=…))`, flush, keep `final` as the
   budget-exhaustion fallback, and return (the loop iterates again);
3. register `LspStopGate()` first, then `VerifyOnStopGate(host_for, enabled=lambda: verify.on_stop)`,
   then `PreVerifyGate(plugin_hooks.pre_verify, limit=verify.max_hook_nudges)`.

## Wiring (wave 42, WB — D-16.799)

Wired: every agent's tool seam is `LspToolHooks(<host seam>)` (`core.agent._edit_seam`, all hosts and
subagents, once — a seam already wrapped is kept), so the turn's edited files are recorded with LSP
off too; `core.loop._handle_answer` asks `SessionRuntime.stop_gates` after the intent-ack check and
before the repetition check (`_sent_back`): a decision adds `continuation_rows`, flushes, keeps the
answer as `TurnState.gated_answer` (the turn's answer if the budget runs out: `_no_grace` and an
empty grace summary use it) and the loop goes on; the gates' counts and the turn's edit list are
dropped at turn end (`_forget_gates`). Gates: `core.stop_gates_lsp.standard_gates` — `LspStopGate`,
`VerifyOnStopGate` (session terminal environment via `runtime.environments`, `verify.on_stop` read at
each answer, `verify.max_stop_attempts`, `verify.phase_timeout`), `PreVerifyGate` when the host gives
`AgentOptions.pre_verify` (plugins: `plugins.session.pre_verify_source`). Hosts may pass
`AgentOptions.stop_gates`. DEMO (Done-when item): `tests/integration/core/test_lsp_stop_gate.py`
— a real `SessionService` with `LspUpkeep`, the real `write_file`, a language server over stdio:
the edit's error arrives after the tool result, the LSP gate sends the agent back with it (durable
row after the interim answer) and the turn ends on the fix. Also `tests/unit/core/test_stop_gate_wiring.py`.
ACC-A28-1 / ACC-A28-2 rows updated.

Decisions for the lead (wiring): `continuation_rows` makes both rows scaffold for a gate that is not
durable (the interim answer was durable, so dropping the scaffold nudge at the end left two
assistant rows in a row in the store); subagents get the standard gates too (no `pre_verify`); the
review fork builds its runtime itself and has none.
