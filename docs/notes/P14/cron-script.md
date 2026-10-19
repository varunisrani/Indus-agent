# Task: script runner and wake gate (P14-09) Phase: 14 Owner: wave builder C2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `cron/scheduler_script.py`
(path confinement, interpreter choice, timeout resolution, process-tree stop, output redaction),
`cron/scheduler_prompt.py` (`_parse_wake_gate` only), `cron/scheduler.py` (`_run_no_agent_job`),
`website/docs/guides/cron-script-only.md` (skimmed). Closed before writing. Evidence: none.

What it does (own words): a job may name a script. Scripts live in `<home>/scripts/` only: the
value is resolved against that folder (absolute and `~` paths too) and the real path, symlinks
followed, must stay inside it. `.sh`/`.bash` run under bash; anything else under the job's
`interpreter` (an absolute path to a Python executable) or our own Python. The child environment
comes from `safety.child_env` (provider keys and every Indus-managed credential removed; the
profile's passthrough names resolved through its secret store), the working folder is the job's
workdir or the scripts folder, the child runs in its own session, and the run has a time limit:
`INDUS_CRON_SCRIPT_TIMEOUT` → `cron.script_timeout_seconds` → 3,600 s. On timeout (or when the run
is cancelled) the whole process tree is stopped (TERM, short grace, KILL), never just the leader.
stdout and stderr are redacted before they leave the runner.

Wake gate: if the last non-empty stdout line is a JSON object with a boolean `wake`:
`{"wake": false}` → this tick is skipped silently (no model call, nothing delivered, saved as a
skip); `{"wake": true, "context": {...}}` → the context goes into the prompt (P14-10). The gate line
itself is removed from the script output.

Script-only jobs (`no_agent`, H5): no model, no tokens. Trimmed stdout is the message; empty stdout
(or `wake: false`) → silent success; non-zero exit or timeout → an error alert that is delivered.
The failure token has no meaning in script output; silence tokens do (a script may print
`[SILENT]`).

At create (called by C3's create/update path): a `script` value that looks like a command line
(spaces, shell operators, quotes, a leading `-`) instead of a file name is refused with a hint.

Inputs → outputs: scope, `JobSpec`, clock → `ScriptResult {ok, exit_code, stdout, stderr,
timed_out, wake, context, error}`.

Files: `src/indus/cron/script.py`, `tests/unit/cron/test_script.py`,
`tests/fixtures/cron/scripts/` (wake-gate, slow, failing scripts; the symlink escape is built at
test time). Shared: `foundation/config/schema.py` (`cron.script_timeout_seconds`).

Edge cases / failure modes:
- NUL in the value, `..` traversal, absolute path elsewhere, symlink pointing out → refused.
- missing script / a directory → error naming the profile's scripts folder.
- interpreter relative, missing, not executable or not a Python → error.
- huge output → each stream capped at 1 MiB kept (the rest drained and dropped).
- a grandchild keeps the pipe open after the leader died → readers are abandoned after 5 s.
- non-UTF-8 bytes → decoded with replacement, never an exception.

Our design differences / decisions:
- Gate key `wake` (ours) with an optional `context` object.
- A pre-run script that fails (non-zero, timeout) fails the run with an alert and no model call
  (the reference hands the error text to the agent). Smaller blast radius, no tokens.
- No shebang support and no managed-venv bootstrapping: our Python or a pinned interpreter.

Tests we will write:
- outside dir, traversal and symlink escape refused; command-line value refused at create.
- env lacks provider keys and has a passthrough name from the secret store.
- wake false → skip; wake true with context → context parsed, gate line removed.
- timeout kills leader and grandchild (fake clock driven; children block on a pipe, no sleeps).
- script-only: stdout is the message, empty → silent, non-zero → error alert.

## Decisions for the lead
- A pre-run script with empty output (and no explicit gate) skips the run silently, as the
  reference does; `wake: true` with no output still runs.
- Interpreter names accepted: `python`, `python3`, `python3.N`, `pypy3` (basename, after symlink).

Compare checklist (filled after the rewrite; reopened `cron/scheduler_script.py`,
`_parse_wake_gate`, `_run_no_agent_job`):
- [x] confinement incl. absolute, `~`, traversal, symlink, NUL
  (`tests/unit/cron/test_script.py::test_script_outside_the_scripts_folder_is_refused`,
  `::test_symlink_escape_is_refused`, `::test_inside_paths_resolve`)
- [x] bash for .sh/.bash, pinned interpreter checks (absolute, executable, a Python, no pythonw)
  (`::test_interpreter_must_be_an_absolute_python`)
- [x] env from the one builder, no provider keys, passthrough from the store
  (`::test_env_lacks_provider_keys_and_keeps_passthrough`)
- [x] timeout env → config → 3,600; tree kill on timeout and on cancel; bounded pipe drain
  (`::test_timeout_kills_the_whole_tree`, `::test_cancel_kills_the_tree`)
- [x] output redacted, lossy decode; non-zero exit reports code and stderr
  (`::test_failing_script_reports_code_and_stderr`)
- [x] wake gate on the last non-empty line (`::test_wake_false_skips`,
  `::test_wake_true_passes_the_context`, `::test_parse_wake_gate`)
- [x] gap found: the working folder without a workdir is the script's own folder (ours used the
  scripts root) — fixed, pinned by `::test_script_runs_in_its_own_folder_without_a_workdir`
- [x] gap found: a pre-run script that succeeds with no output means "nothing to report" (no model
  call) — fixed in the runner, pinned by
  `tests/unit/cron/test_runner.py::test_pre_run_script_without_output_skips_the_model`
- Deliberate differences: gate key `wake` + `context`; a failing pre-run script fails the run
  without a model call (the reference hands the error to the agent); a script-only job without a
  script is blocked by preflight (alert once) instead of being auto-paused.
- Not taken: managed-store venv bootstrapping and Windows launch details (our Python or a pinned
  interpreter; Windows is out of scope, D-16.55).

## Status

Done (2026-10-19). `src/indus/cron/script.py`, fixtures in `tests/fixtures/cron/scripts/`.
`check_script_value` is the create-time refusal C3's create/update path calls.
