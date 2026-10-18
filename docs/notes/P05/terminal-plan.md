# Task: `terminal` tool — planning and guards (P05-11) Phase: 05 Owner: wave-2 builder C

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/terminal_tool.py`
(`_handle_terminal`, `terminal_tool`, `_plan_execution`, `_run_approval_guards`, `_pre_exec_block`,
`_resolve_command_cwd`, `_with_promoted_note`, `_command_requires_pipe_stdin`, the schema),
`tools/terminal_tool_guards.py` (workdir allowlist, foreground background guidance, trailing `&`
conversion; skimmed the gateway lifecycle and Windows self-repo guards), `tools/terminal_tool_background.py`
(result fields of a background start, async-support note). Spec: research `02-tools.md` §3.6,
§3.7a, traps R02.8, R02.11. Closed before writing.
Evidence: none.

What it does (own words): the model-facing `terminal` tool. It checks and normalises the call,
decides how it will run (foreground, background, or a foreground call promoted to a tracked
background job), runs the pre-exec guards and the approval pipeline, then hands over to the
foreground runner (P05-12) or the process registry (P05-13).

- Normalisation, before anything runs: a stray `code` argument (an `execute_code` call sent here)
  is refused with a pointer to `execute_code`. Background-only arguments on a foreground call
  (`notify`, `pty`, `persist_on_release`, legacy `notify_on_complete` / `watch_patterns`) are refused
  with the corrected call spelled out; a foreground `heartbeat` is quietly dropped, because refusing
  it made weak models resend the identical call (R02.11). `notify: true` = tell me on exit,
  `notify: [..]` = watch patterns; a heartbeat implies notify on exit.
- Plan: command must be a string; timeout ≤ 0 refused; cwd = per-call `workdir`, else the
  session's recorded cwd, else the configured `terminal.cwd` (the environment's own start dir).
  A foreground command whose only backgrounding is one trailing ` &` runs as `background=true`
  for the command without it. Other backgrounding shapes (inline `&`, `nohup`/`disown`/`setsid`,
  dev servers, `docker compose up`, `python -m http.server`, …) are refused with guidance unless
  the call only asks for help or a version. A foreground timeout above
  `terminal.max_foreground_timeout` (600) becomes a tracked background job with notify-on-exit
  and a "do not run it again" note (poll wording where completions cannot be delivered).
- Pre-exec guards run on the shared `tools` pool under a deadline `max(timeout, 30 s)`; a guard
  that does not finish refuses the call ("not run, try again") — fail closed. The one guard here:
  the workdir character allowlist (letters/digits plus `/ \ : _ - . ~ space + @ = ,`; control
  characters refused). Guards are a list so later phases (gateway lifecycle in P15) add rows.
- Approval: the call goes through the P05-08/09 pipeline (`indus.safety.approvals.decide` with an
  `ApprovalContext(scope, session_id, turn_id, run_context, broker, isolated, cwd)`, names from
  the lead's note) behind a small gate interface; approved → run (a user approval adds the
  `approval` note); blocked → blocked result; pending → pending-approval result; the `timeout` /
  `not_answered` outcomes are reported as "nothing ran, not a refusal" (R02.5). The default gate
  fails closed when the pipeline cannot be loaded. An approved decision's
  `clear_stale_interrupt(cancel)` is called once before the run and its retries (R02.9).
- PTY: only for background, local backend; a command that needs piped stdin/EOF
  (`gh auth login --with-token`) runs without a PTY and the result says why.

Inputs → outputs: tool args + context (scope, session id, turn id, broker, cancel signal) → one
JSON string from the result shapes in P05-12.

Files: `src/indus/tools/builtin/terminal.py`, `tests/unit/tools/builtin/test_terminal_plan.py`,
`tests/unit/tools/builtin/_stubs_c.py` (stand-in for the P05-08 names, removed at integration),
`tests/unit/tools/builtin/_terminal_rig.py`, `tests/fakes/terminal.py` (test helpers),
`tests/integration/tools/test_terminal_local.py` (real local backend end to end),
`tests/integration/core/test_d04_1_terminal_tool_backstop.py` (D-04.1 criterion (b) against the
real tool). Shared: `src/indus/tools/builtin/__init__.py` (BUILTIN_MODULES line),
`src/indus/foundation/config/schema.py` + `tests/unit/foundation/config/test_schema.py` (two
`terminal.*` keys), `pyproject.toml` (layer order: tools above backends).

Edge cases / failure modes:
- `command` missing, `None`, a number or a list → refused naming the type.
- `timeout` 0, negative, a float string → refused / coerced by dispatch's argument repair.
- `workdir` relative → refused (must be absolute); with `;`, `$`, backticks, newline → blocked.
- trailing `&` with a `$(…)`, a `;`, a newline or a second `&` → refusal, never a conversion.
- `cmd --help &` → help/version calls are never refused or converted.
- unresolved profile scope → refused before any environment is touched (R02.8).
- the pipeline module missing → blocked, nothing runs.
- the environment cannot be created → `disabled`; unreachable → `degraded` (P05-04).

Our design differences / decisions:
- Config is read from the call's `Scope` (D-16.75); no process-environment bridge.
- The guard chain has no gateway-lifecycle or Windows self-repo guard yet (P15, D-16.55).
- No "yield to background" when the user types mid-command: an interrupt stops the command
  (follow-up for P06/P12 surfaces).
- Session cwd: the environment (one per session key) keeps its cwd; the tool also keeps a small
  per-(profile, session) record so a reaped and recreated environment starts where the session was.
- Background processes run on the local backend only until P11 adds the sandbox spawn path.

Tests we will write:
- each normalisation case (code arg, foreground notify/pty/persist, foreground heartbeat dropped,
  notify true/list, heartbeat implies notify, bad heartbeat).
- plan: non-string command, timeout ≤ 0, trailing `&` → background, inline `&`/nohup/`npm run dev`
  refused, `--help` allowed, 900 s foreground → promoted with note, poll-only note when not
  deliverable.
- guards: bad workdir blocked, relative workdir refused, guard deadline overrun → "not run, retry".
- approval gate: blocked/pending/approved-by-user shapes; default gate fails closed without the
  pipeline; the adapter maps the stand-in decision.
- D-04.1 (b): real local backend through the tool: timeout kill with a responsive loop, wedged
  wait killed by the +2 s backstop, SIGTERM-ignoring child SIGKILLed.

## Decisions for the lead
- **Layer order**: `indus.tools` and `indus.backends` were independent siblings in the
  import-linter contract, so no tool could import a backend. The terminal tool (and P05-15 file
  ops) must, so `pyproject.toml` now lists `indus.tools` above `indus.backends` (backends import
  nothing from tools). Easy to revert if the lead prefers another seam.
- New config keys: `terminal.max_foreground_timeout` (600) and `terminal.process_wait_auto_notify`
  (true) in `TerminalSection`.
- Approval names follow the lead's note on slice B's API (`decide`, `ApprovalContext`,
  `clear_stale_interrupt` on the decision). The stand-in sets them on the real package for a test
  only; `pipeline_gate` reads them from `indus.safety.approvals` at call time. `run_context` is
  taken from a handler keyword of the same name (an `extras["run_context"]`, if the host offers
  one); it is None otherwise — the host should say what it carries (cron, one-shot, …).
- Extras (lead ruling): the tool reads `broker`, `cancel` and `environments` (the
  `EnvironmentRegistry`) from `ToolContext.extras`; without `environments` it uses the process
  runtime's registry. `core/tool_round.py` still offers only `broker` — the host wiring is the
  lead's. The host's registry should be made with
  `background_alive=runtime().processes.alive_for_env_key` so the reaper keeps environments
  with running jobs.
- Process-wide defaults: `terminal.runtime()` builds one `EnvironmentRegistry` + `ProcessRegistry`
  + `Deliveries` per process; the host must call `await runtime().shutdown()` at exit (P06).

Compare checklist (filled after the rewrite; reopened `tools/terminal_tool.py`,
`tools/terminal_tool_guards.py`, `tools/terminal_tool_background.py`):
- [x] stray `code`, foreground notify/pty/persist refused with the corrected call, foreground
  heartbeat zeroed (test: `test_terminal_plan.py::test_foreground_background_only_args_refused`,
  `::test_foreground_heartbeat_is_dropped`)
- [x] explicit `notify` wins over legacy flags; heartbeat implies notify
  (`::test_notify_forms`)
- [x] timeout ≤ 0 refused before the default is applied (`::test_timeout_must_be_positive`)
- [x] trailing `&` conversion only for the one unambiguous shape (`::test_trailing_amp_becomes_background`,
  `::test_trailing_amp_with_other_backgrounding_is_not_converted`)
- [x] over-cap foreground promoted, note says do not re-run, poll-only wording when not
  deliverable (`::test_overcap_foreground_is_promoted`, `::test_promoted_note_poll_only_when_not_deliverable`)
- [x] workdir allowlist and guard deadline fail closed (`::test_workdir_with_metacharacters_blocked`,
  `::test_guard_deadline_overrun_refuses`)
- [x] pending / blocked / approved-by-user shapes (`::test_gate_outcomes`)
- [x] gh `--with-token` never under a PTY (`::test_pty_disabled_for_piped_stdin_commands`)
- Deliberate differences: the reference re-checks that stripping a trailing `&` leaves the approval
  identity unchanged (some of its patterns key on `&`); our detector (P05-07) has no `&`-keyed
  categories, so the stripped command is what the pipeline judges — re-check at integration with
  the real pipeline. Guard refusals use `exit_code: -1` with `status: "blocked"` (the reference's
  lifecycle guard used 1); one shape for every block.
- Not taken: gateway lifecycle guard (P15), Windows self-repo guard (D-16.55), yield-to-background,
  per-task image overrides (P11), metrics hooks, the homebrewed-CI-poller hint (reference-specific).

## Status

Done (2026-10-18). Planning, normalisation, guards and the gate seam are in; the P05-08 pipeline
is reached through `pipeline_gate` (stand-in in tests until slice B lands).

## Integration (lead)

Merged in the P05 wave 2 integration (2026-10-18, lead's verify tree).
- Layer decision taken as D-04.7 (`docs/plan/04-architecture.md` §8, 00-layout §1, architecture §2
  import direction, `tests/unit/test_layers.py`): `indus.tools` sits above `indus.backends`.
- Stand-in removed: `pipeline_gate` imports `indus.safety.approvals` and uses B's typed API
  (`ApprovalContext`, `ApprovalDecision`, `decide`); `clear_stale_interrupt` calls the decision's
  method. Tests use a scripted `approvals.decide` returning real `ApprovalDecision` objects, plus one
  test through the real pipeline.
- Trailing `&` re-check done with the real detector: `rm -rf / &`, `curl x | sh &` and
  `git push --force … &` get the same pattern key as without the `&`.
- Tool round: `core/tool_round.handler_extras` now offers `cancel` (the turn's stop event) and
  `environments` (`SessionRuntime.environments`) next to `broker`
  (`tests/unit/core/test_tool_round.py::test_handlers_get_cancel_broker_and_environments`).
- P06 host wiring left open: set `SessionRuntime.environments = terminal.runtime().environments`
  (one registry for terminal, file tools and `search_files`); offer `extras["run_context"]`;
  `await terminal.runtime().shutdown()` at process exit. `background_alive` is already wired in
  `TerminalRuntime.create`.
