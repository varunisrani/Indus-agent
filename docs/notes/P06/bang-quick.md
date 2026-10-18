# Task: Bang shell and quick commands (P06-11) Phase: 06 Owner: P06 slice C builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/bang_shell.py`
(whole), `cli.py` (quick-command runner). Research `04-cli-config.md` §3.7.2 (quick commands),
§3.8; traps R04.27, R04.28. Closed before writing.
Evidence: none.

What it does (own words): `!cmd` at the prompt runs a shell command on the machine the CLI runs on
without spending a model turn. It is not a way around the safety rules: the command goes through
the same approval pipeline the agent's terminal tool uses, as a local, non-isolated command, so the
floors (hardline, deny globs) block and a dangerous command asks the person. If the approval module
cannot be loaded, the command is refused (fail closed — intended difference D-16.77 from the
reference, which allows). The child environment comes only from `safety.child_env` (provider keys
removed); if that fails nothing starts. Output (stdout and stderr merged) streams line by line.
Quick commands are user-defined `/name` entries in `quick_commands`: `exec` runs a shell snippet with
the same sanitised environment, a 30 s limit and redacted output; `alias` re-dispatches
`/<target> <args>`.

Inputs → outputs:
- `is_bang(text)`: only a leading `!` after trimming counts (`fix it!` is a prompt).
  `parse_bang(text)`: `!  ls` → `ls`; `!!` → `!`; bare `!` → "" (usage line printed).
- `run_bang(text, ctx) -> int`: refused (non-interactive or gateway/cron marker) → 126 with a
  line; guard unavailable → 126; blocked by the guard → 126 with the reason; env failure → 127;
  launch failure → 127; timeout (120 s, clock-driven) → kill tree, 124; Ctrl+C or cancel → kill,
  130 (the session survives); otherwise the exit code (non-zero shown after the output). Runs on
  the host in the session cwd, else `terminal.cwd` from config, else the process cwd. Nothing is
  written to the transcript.
- `run_quick(name, spec, args, ctx) -> SlashResult`: exec → redacted output or "no output",
  "timed out after 30s", error line; alias → `dispatch("/target args", redispatch)`; bad entry →
  one line.

Files: `src/indus/cli/bang.py`, `src/indus/cli/quick_commands.py`, `tests/unit/cli/test_bang.py`,
`tests/unit/cli/test_quick_commands.py`.

Edge cases / failure modes:
- `!rm -rf /` → blocked by the hardline floor before anything starts.
- `!env` → no provider key in the child (child_env blocks managed names).
- A process that ignores its output and sleeps → the clock deadline kills the whole process group.
- A missing or non-directory cwd → falls back to the next candidate.
- The approval ask (a dangerous but not hardline command) goes through `ctx.ask_approval`; no
  callback → treated as not answered (blocked, not a refusal).
- Quick `exec` with an empty command, unknown type, alias without a target → one line each.
- Quick alias loops (`a` → `b` → `a`) stop after a small depth with a message.

Our design differences / decisions:
- Fail closed when the guard cannot load (D-16.77, R04.27); refusal codes 126 (not run) are ours.
- Timeout runs on the injected clock (poll loop), so tests use a fake clock with a real process.
- Quick commands also run on the clock-driven runner (30 s), output captured then redacted with
  `redact_output(force=True)`.

Tests we will write:
- `!rm -rf /` blocked by the floor; `fix it!` is a prompt; `!!`; bare `!`; `!env` has no provider
  key; timeout 124 with a fake clock and a real `sleep`; Ctrl+C 130; launch failure 127; guard
  missing refuses; dangerous command asks and deny blocks; nothing reaches the port; quick exec
  output redacted; quick alias re-dispatch; quick timeout; env has no provider key.

## Decisions for the lead
- Exit code 126 for "not run" (refused, blocked, guard missing) — the reference returns nothing
  there; 126 is the shell's "cannot execute" code. Easy to change.
- Gateway/cron markers read from the scope env: `INDUS_GATEWAY_SESSION`, `INDUS_CRON_SESSION`,
  `INDUS_SESSION_PLATFORM` (names to confirm when P14/P15 land).

Compare checklist (filled after the rewrite; reopened `hermes_cli/bang_shell.py`, `cli.py`):
- [x] leading `!` only, `!!`, bare `!` usage (test: `test_bang.py::test_parse_rules`)
- [x] host only, session cwd then config cwd then process cwd (test: `::test_cwd_order`)
- [x] guard missing fails closed (test: `::test_guard_missing_refuses`) — intended difference D-16.77
- [x] sanitised env, launch fails if the sanitiser fails (test: `::test_env_failure_refuses`)
- [x] 124 / 130 / 127 (tests: `::test_timeout_kills_with_124`, `::test_ctrl_c_kills_with_130`,
  `::test_launch_failure_127`)
- [x] quick exec redacted and scrubbed (tests: `test_quick_commands.py::test_exec_output_redacted`,
  `::test_exec_env_has_no_provider_key`)
- Deliberate differences: fail closed; clock-driven timeout; 126 for "not run"; quick `exec` shows
  stdout and stderr merged (the reference shows stdout, else stderr); quick aliases stop after a few
  hops; the whole process group is killed on timeout or Ctrl+C.
- Not taken: Windows hidden-console flags (the runner uses `start_new_session` on POSIX only).

## Status

Done (2026-10-18).

## Integration (lead)

- The interrupted line now names the code: `!: interrupted (exit 130)` (the REPL shows no other
  trace of the code); `test_bang.py` updated.
- In the REPL a bang line runs in `slash_bridge.terminal_section` (prompt away, real stdout,
  plain `KeyboardInterrupt` on Ctrl+C), so output streams and Ctrl+C reaches `run_shell`.
  `ask_approval` is the approval card read on the terminal (digits as on the REPL card).
  Covered end to end by `tests/e2e/test_demo1.py::test_ctrl_c_during_a_bang_command_gives_130_and_the_session_goes_on`.
