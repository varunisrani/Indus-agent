# Task: `indus approvals test` (P07-18) Phase: 07 Owner: P07 slice C builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/subcommands/approvals.py`
(whole), `hermes_cli/approvals_test.py` (whole). Research `04-cli-config.md` §3.21. Closed
before writing.
Evidence: none.

What it does (own words): `indus approvals test [--backend local] [--json] -- CMD…` answers "what
would happen if the agent ran this?" without running it, asking anyone or saving anything. It
calls the real P05 pipeline (`safety.approvals.decide`) for the current profile, with a fresh
approval memory (so no session approval counts and none is recorded) and a stand-in for the
person: when the pipeline gets as far as asking, the stand-in notes it and declines to answer, and
the command reports "would ask". The backend's flags come from the terminal backend registry, so
an isolated backend gets the pipeline's isolated path (only the hardline floor and deny globs).
Output: verdict, the step and rule that decided, a plain sentence, and the normalised trace from
detection. Exit codes: 0 allow, 2 would ask, 3 deny; no command → usage line, exit 1; unknown
backend → exit 1.

Inputs → outputs: words after `--` joined by spaces → text report or `--json` object
`{command, backend, verdict, exit_code, step, outcome, rule, description, trace}`.

Files: `src/indus/cli/commands/approvals.py` (the `approvals` group with `test`),
`tests/unit/cli/commands/test_approvals.py`.

Edge cases / failure modes:
- `--yolo` / `INDUS_YOLO_MODE` or `approvals.mode: off` → allow (step auto), floors still deny.
- `command_allowlist` entries → allow (allowlisted).
- An isolated backend: dangerous but not hardline → allow; deny glob → deny.
- Nothing is written: config file bytes and the approval memory are unchanged.

Our design differences / decisions:
- `--backend` instead of `--env-type` (our backend vocabulary); exit 1 for a missing command.
- We call the pipeline itself rather than re-composing its steps, so the answer cannot drift from
  what the terminal tool does; the reference composes evaluators by hand.

Tests we will write:
- exit-code table over corpus samples (hardline, dangerous, safe, deny glob, allowlist, mode off,
  yolo, isolated backend); `--json` shape; nothing written (config bytes, state); no command → 1;
  unknown backend → 1.

Compare checklist (filled after the rewrite; reopened both files):
- [x] isolated backends skip guards except deny rules (test:
  `test_approvals.py::test_isolated_backend_skips_all_but_deny`)
- [x] hardline and deny before yolo/off (test: `::test_verdict_table`)
- [x] allowlist and detection (test: `::test_verdict_table`)
- [x] exit codes 0/2/3, usage (tests: `::test_verdict_table`, `::test_no_command_is_usage`)
- Deliberate differences: verdict words `allow`/`ask`/`deny` plus the pipeline's step and outcome;
  the runtime-delete and `sudo -S` floors are reported by the same pipeline.
- Not taken: `approvals suggest` (P2, lands with P09).

## Status

Done (2026-10-18).
