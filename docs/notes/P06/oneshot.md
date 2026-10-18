# Task: One-shot `-z` and finite `chat -q` (P06-12) Phase: 06 Owner: P06 slice A builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/oneshot.py` (flow,
exit-code mapping, usage file keys, toolset validation), `hermes_cli/cli_single_query.py` (exit
code decision for `-q`/`-Q`), `hermes_cli/main.py` (`_run_and_exit_oneshot`,
`_exit_after_oneshot`, cleanup list). Research `04` §3.4.8, §3.5, trap R04.20. Closed before
writing.
Evidence: none.

What it does (own words): `indus -z PROMPT` runs one turn and prints only the final reply on
stdout; stderr stays silent except for usage errors and a one-line failure reason. Approvals are
auto-approved for the run (`INDUS_YOLO_MODE=1` in the run's scope) but the floors still refuse a
hardline command; the run context is `single_query`, and the host answers no server request kind
(clarify/sudo cannot block). Toolsets: `-t` (`all`/`*` allowed) else the `indus-cli` platform
bundle. `--provider` without `--model` (or `INDUS_INFERENCE_MODEL`) is a usage error. Exit codes:
0 completed with text; 1 completed but no text (also: could not start); 2 failed, partial, budget
or iteration cap, and usage errors; 130 interrupted. `chat -q` finite runs (non-TTY, `--oneshot`,
`-Q`) print the reply and a `session_id:` line on stderr; codes 0 completed, 1 anything not
completed or never ran, 130 interrupted. `--usage-file PATH` gets a JSON report even on failure
(cost fields null with `cost_status: unknown` until P08; token counters null until per-turn usage
reaches `TurnResult`); a failed write never changes the code. Then cleanups run (terminal
runtime: background jobs and environments; host; store), streams flush, logging shuts down and
the process leaves through `os._exit(rc)` (only when running as the real process).

Inputs → outputs: prompt, model/provider/toolsets/reasoning/resume → stdout final text, exit code,
optional usage JSON.

Files: `src/indus/cli/oneshot.py`, `tests/unit/cli/test_oneshot.py`,
`tests/integration/cli/test_oneshot_exit_codes.py` (+ `tests/integration/cli/fake_runner.py`).

Edge cases / failure modes:
- Ctrl-C during the turn → the turn is interrupted through the service, exit 130, usage file
  still written.
- Text printed but turn not completed (provider failed after a partial reply) → 2 for `-z`.
- Lone surrogates in model text are replaced before writing.
- `-z` with `-c`/`-r` continues that session.

Our design differences / decisions:
- "Could not start" (no provider, bad route) exits 1 for `-z` like the reference's agent-failed
  path; the plan's table lists only turn outcomes.
- No auxiliary usage block yet (P08).

Tests we will write:
- unit: exit-code tables for both modes, usage report shape, provider-without-model usage error.
- integration (subprocess, fake model selected by env): each `-z` code row, stdout purity, usage
  file on failure, hardline command refused under `-z`.

## Decisions for the lead
- Token counters in the usage file are `null` with `tokens_status: "unknown"`; wiring needs a
  usage accumulator on `TurnResult` (P08 follow-up).

Compare checklist (filled after the rewrite; reopened `oneshot.py`, `cli_single_query.py`):
- [x] both exit tables (tests: `test_oneshot.py::test_oneshot_code_table`,
  `test_finite_code_table`)
- [x] stdout holds only the final text (test: `test_oneshot_exit_codes.py::test_completed_prints_only_text`)
- [x] usage file on failure, write failure ignored (tests: `test_usage_file_*`)
- [x] hard exit after cleanup (test: `test_hard_exit_runs_after_cleanups`)
- Deliberate differences: see above.
- Not taken: kanban exit 75, gateway lost-owner exit 3 (arrive with P24/P17).

## Status

Done (2026-10-18).
