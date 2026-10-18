# Task: `terminal` tool — foreground run and result (P05-12) Phase: 05 Owner: wave-2 builder C

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/terminal_tool_result.py`
(whole: finalisation order, exit-code tables, spill handling, key order), `tools/terminal_hints.py`
(whole: masked-success shapes, failure hint idea), `tools/terminal_tool.py` (`_run_foreground`
retry loop, `_error_json`, `_fatal_error_json`, `_degraded_result`). Spec: research `02-tools.md`
§3.7, §3.7a, traps R02.9, R02.10. Closed before writing.
Evidence: none.

What it does (own words): runs an approved foreground command and turns what came back into the
one JSON result the model reads.

- Run: `EnvironmentRegistry.run` with bounded capture. A backend exception (anything but the
  degraded/disabled cases the registry already answers) is retried up to 3 times after 2, 4 and
  8 s on the injected clock; an exception that talks about a timeout answers rc 124 at once; the
  turn's cancel signal is checked during each backoff, so an interrupt is never swallowed by a
  retry. A stale interrupt from the approval wait is cleared once before this, never inside it (R02.9).
- Finalise, in this order: record the observed cwd as the session cwd only when this command
  printed the marker and no per-call `workdir` was used (R02.10); the sudo flags the P05-10
  layer sets on the result; head/tail cut to `tool_output.max_chars` (50,000); strip ANSI escapes;
  command-aware redaction (P05-06); exit-code meaning; a failure hint, or on rc 0 a masked-failure
  warning; the overflow log is rewritten redacted (force mode) or dropped when that fails.
- Exit-code meaning: negative → "terminated by signal N"; > 128 for 137/139/141/143/152/153 →
  "usually signal N"; informational codes by the last pipeline segment's base command: grep/rg 1 =
  no matches, diff 1 = files differ, test/`[` 1 = false, find 1 = partial, curl 6/7/22/28, git 1.
- Key order is fixed: `output, exit_code, error`, then `cwd, environment_recreated,
  full_output_path, output_total_chars, approval, exit_code_meaning, hint, sudo_auth_failed,
  sudo_cache_cleared`; an absent optional field is omitted, never `null`.
- One builder per outcome shape: finished, timed out (exception path), background started,
  background failed to start, rejected, blocked, pending approval, degraded, disabled.

Inputs → outputs: `ExecResult` (or `BackendStatus`, or an exception) + command, workdir, approval
note → JSON text.

Files: `src/indus/tools/builtin/terminal_result.py`, `tests/unit/tools/builtin/test_terminal_result.py`.

Edge cases / failure modes:
- rc 130 is an interrupt only with the wait loop's `[Command interrupted]` marker; an approved
  run then says "approved, then interrupted".
- rc 124 from our deadline vs a command's own `exit 124` (ExecResult.timed_out tells).
- empty output → `""` (never `null`); output of only escape codes → `""`.
- `cmd | tail -5` with a Python traceback in the output and rc 0 → masked-failure hint.
- `grep -q x f || echo none` with rc 0 and no failure text → no hint.
- overflow log unreadable → path dropped, the file removed, the result still returned.
- a backend exception text carrying a secret → redacted before it reaches the model.

Our design differences / decisions:
- Hints are our own short list (command not found, Python import error, permission denied, 126,
  137, 124); the reference's tool-specific ones (gh JSON fields, merge conflicts, rate limits)
  are left out.
- `cwd` is echoed only when the command ended somewhere other than where it started.
- No verification-evidence field and no output-transform plugin hook yet (P13 adds hooks).

Tests we will write:
- golden JSON per outcome shape (byte-exact strings, key order).
- exit-code meaning table; masked-failure hint on/off cases.
- cwd recorded only without workdir and only when the marker printed.
- retries then success on a fake backend (fake clock 2/4/8 s); timeout-text exception → rc 124;
  cancel during backoff → interrupted; exhausted retries → error with redacted text.
- ANSI stripped, secrets redacted, overflow log redacted on disk.

## Decisions for the lead
- R02.9: the terminal tool calls the approved decision's `clear_stale_interrupt(cancel)` once
  before `run_foreground` (never inside the retry loop); a cancel during a backoff still ends the
  call as interrupted.
- Sudo (lead's note): every attempt runs inside `indus.backends.sudo.use_sudo_caller(broker=…,
  turn_id=…)` when that module exists (a null context otherwise), and `finish` copies
  `sudo_auth_failed` / `sudo_cache_cleared` from the result (read with `getattr`, since this
  tree's `ExecResult` has no such fields yet). Background jobs do not get the sudo rewrite yet.

Compare checklist (filled after the rewrite; reopened `tools/terminal_tool_result.py`,
`tools/terminal_hints.py`, `tools/terminal_tool.py`):
- [x] cwd recorded only from this command's marker and never from a workdir
  (test: `test_terminal_result.py::test_cwd_not_recorded_with_workdir`,
  `::test_cwd_recorded_from_marker`)
- [x] order: truncate → strip ANSI → redact → meaning → hint (`::test_ansi_stripped_and_redacted`)
- [x] signal notes negative/≥128, no note for 130 (`::test_exit_code_meaning_table`)
- [x] masked success needs both a masking shape and a failure shape; read-only heads exempt
  (`::test_masked_failure_hint`)
- [x] approved + interrupted wording (`::test_approved_then_interrupted`)
- [x] spill file redacted, handle dropped on failure (`::test_overflow_log_is_redacted`)
- [x] key order and omitted optionals (`::test_golden_*`, one per outcome shape)
- [x] retry 2/4/8 then error; timeout text → 124 (`::test_retries_then_success`,
  `::test_timeout_exception_is_124`)
- Deliberate differences: the reference's `truncation_note` field is folded into the cut marker the
  capture already writes (the result names `full_output_path`); `exit_code` for a guard block is
  -1 everywhere.
- Not taken: verification evidence, output-transform hook, metrics, tool-specific hints.

## Status

Done (2026-10-18).

## Integration (lead)

Merged in the P05 wave 2 integration (2026-10-18, lead's verify tree).
- Sudo: direct `use_sudo_caller(broker=…, turn_id=…, interactive=…)`; the sudo flags are read as
  `ExecResult` fields. `run_foreground` gained `interactive` (false for non-interactive run
  contexts → headless sudo).
- Stale interrupt: unchanged call site (once, before the retries), now on B's real decision.
