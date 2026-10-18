# Task: `process_manage` tool (P05-14) Phase: 05 Owner: wave-2 builder C

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/process_registry.py` (the
tool schema and handler at the bottom; `poll`, `read_log`, `wait`, `kill_process`, `write_stdin`,
`submit_stdin`, `close_stdin`, `list_sessions`), `tools/process_registry_wait_policy.py` (whole).
Spec: research `02-tools.md` §3.12, trap R02.23 (tool name). Closed before writing.
Evidence: none.

What it does (own words): the model's handle on background jobs started by `terminal`.

- `list`: this conversation's jobs (same profile and session): id, command (cut to 200), cwd, pid,
  status, uptime, a 200-character preview, exit fields.
- `poll`: status, pid, uptime, the last 1,000 characters, exit fields once exited. Reading a poll
  does not use up the completion: the completion message still comes.
- `log`: lines with ANSI removed; no `offset` → the last `limit` (default 200) lines; an explicit
  `offset: 0` is the head; `total_lines`, `showing`. Reading through the end of an exited job's
  log uses up its completion (the model already has it).
- `wait`: until exit, the window, or an interrupt. Default and maximum = `terminal.timeout`
  (180 s); a larger value is cut with a note; ≤ 0 refused. Window over → `status: "timeout"`,
  `process_running: true`, and a note that says plainly this is not an error. Interrupt (the
  turn's cancel signal) → `status: "interrupted"`. Exit → exit fields + the 2,000-character tail,
  completion used up.
- `kill` (ordered kill, output tail), `write` (raw), `submit` (adds a newline), `close` (EOF) —
  the stdin actions return status only.
- `handoff`: reserved for P10 subagents; answers "not available".
- Two `wait`/`poll` calls in a row on a running job with no new output switch on notify-on-exit
  and tell the model to stop polling — only where completions are deliverable, and only when
  `terminal.process_wait_auto_notify` is on.
- Every output-bearing result (and the command echo) is redacted; an unknown id is a not-found
  result.

Inputs → outputs: `{action, session_id?, data?, timeout?, offset?, limit?}` → JSON text.

Files: `src/indus/tools/builtin/process_manage.py`, `tests/unit/tools/builtin/test_process_manage.py`.
Shared: `src/indus/tools/builtin/__init__.py` (BUILTIN_MODULES line).

Edge cases / failure modes:
- `session_id` sent as a number → treated as text.
- missing `session_id` for a per-job action → error naming the action.
- unknown action → error listing the actions.
- `limit` ≤ 0 → error; `offset` past the end → empty selection with `total_lines`.
- stdin actions on an exited job → `already_exited`; on a job without stdin → error.
- prefix that matches two jobs → not found (never a guess).

Our design differences / decisions:
- Tool name `process_manage` (code-observed name; docs said `process`) — R02.23 / D-16.24.
- `list` shows only this conversation's jobs in this profile; no retained receipts until P11.
- Interrupt comes from the turn's cancel signal or task cancellation, not a per-thread bit.

Tests we will write:
- each action against a registry with fake process handles; offset 0 vs omitted; wait timeout
  wording; wait interrupt; auto-notify after two empty polls (and not when undeliverable or
  switched off); prefix lookup; redaction of output and command; handoff not available.

## Decisions for the lead
- none beyond P05-13's.

Compare checklist (filled after the rewrite; reopened the handler and wait policy):
- [x] offset 0 = head, omitted = tail (test: `test_process_manage.py::test_log_offset_zero_is_head`)
- [x] poll never consumes; log through the end / wait consume (`::test_poll_running_then_exited_and_never_consumes`,
  `::test_log_through_end_consumes`)
- [x] wait clamp note, ≤ 0 refused, timeout wording "not an error" (`::test_wait_*`)
- [x] auto-notify after two unchanged checks, only where deliverable (`::test_auto_notify_*`)
- [x] stdin actions unredacted status only (`::test_stdin_actions`)
- Deliberate differences: the "interrupted by a steer" variant collapses into the single
  interrupted status (our steer does not release tool waits).
- Not taken: handoff (P10), retained receipts (P11), desktop terminal-tab close.

## Status

Done (2026-10-18).
