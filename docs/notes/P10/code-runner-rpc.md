# Task: `execute_code` runner (script mode) and RPC (P10-08) Phase: 10 Owner: P10 wave-1 builder H

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/code_execution_tool.py`
(run flow, limits, truncation and spill, result shape, handler), `tools/code_execution_rpc.py`
(whole), `tools/code_execution_env.py` (env scrub, interpreter and cwd choice),
`tools/approval.py` (`check_execute_code_guard` only). Spec: research `02-tools.md` §3.19, §3.13,
§3.14; traps R02.22 (secrets in child processes). Closed before writing.
Evidence: none.

What it does (own words): the model sends Python source; we run it in a child Python process and
hand back what it printed. The script can call a handful of our own tools through a module
(`indus_tools`, P10-09) that talks to this process over a private socket. Every such call goes
through the normal tool dispatcher, so approvals, hooks and the tools' own checks still apply.

- Staging: a fresh private directory (0700, files 0600) holds `indus_tools.py` and `script.py`;
  it and the socket are removed on every exit path.
- Interpreter and cwd: mode `project` (default) runs in the session's working directory with the
  active virtualenv/conda interpreter when it exists and answers as Python 3.8 or newer, else our
  own interpreter; `strict` runs in the staging directory with our interpreter.
- Child environment: `safety.child_env(variant="code")` (safe-prefix names only, any credential
  word dropped, passthrough names, pins), then our RPC variables, `TZ` from the profile's zone,
  forced UTF-8, no bytecode files, `PYTHONPATH` = staging dir first, Indus's own entries removed.
- RPC: Unix socket in the staging dir (loopback TCP on a random port on Windows); a 32-byte
  random token per run; per request: token check (constant time) → allow-list → budget (50
  dispatched calls per run, refusals are free) → terminal foreground check → dispatch → log.
- Limits: `code_execution.timeout` 300 s, then SIGTERM, then SIGKILL 5 s later (the local
  backend's ordered tree kill). Interrupt (the turn's stop signal) kills the child the same way.
- Output: stdout ≤ 50,000 bytes inline (first 40 %, last 60 %, a marker naming the cut); the full
  text (≤ 5,000,000 bytes) goes to `<home>/cache/exec/stdout-<digest>.txt` and the result names
  it; stderr ≤ 10,000 bytes, shown after a separator when the exit code is not 0. ANSI escapes
  stripped, secrets masked (`redact_output`).
- Result JSON: `status` (`success|error|timeout|interrupted`), `output`, `tool_calls_made`,
  `duration_seconds`, `exit_code`; `error` when not a success; `tool_errors` (first five failed
  in-script calls); truncation fields; `hint` (P10-09) on a recognised failure.
- Whole-script approval (E9) before anything is written or spawned.
- `command` instead of `code` → error that points at `terminal`; `code` not text or empty → error.

Inputs → outputs: `{code, reset?}` plus context (`scope`, `session_id`, `turn_id`, `cancel`,
`broker`, `environments`, `run_context`, `approval_judge`, `tool_registry`, `tool_hooks`,
`enabled_tools`) → JSON text.

Files: `src/indus/tools/builtin/code/{__init__,runner,rpc,gate,output,settings,launch}.py`,
`tests/unit/tools/builtin/code/{test_runner,test_rpc}.py` (+ shared `_rig.py`, `conftest.py`).
Shared: `src/indus/tools/builtin/__init__.py` (BUILTIN_MODULES row), `src/indus/tools/toolsets.py`
(`code_execution` set, core list), `src/indus/core/tool_round.py` (`handler_extras` offers
`tool_registry`, `tool_hooks`, `enabled_tools`), `src/indus/foundation/config/schema.py`
(`code_execution` section) and its test, goldens under `tests/golden/`.

Edge cases / failure modes:
- the socket path is too long for AF_UNIX (deep temp dirs): the staging dir moves to a short
  private dir under the system temp dir.
- request line not JSON, not an object, unknown tool, wrong token, args not an object → refused
  without touching the budget; the stub raises so a runaway loop stops.
- the script spawns grandchildren that keep the pipes open: the tree kill takes the group.
- an in-script `terminal` call that is dangerous still meets the terminal approval pipeline and
  its hardline floor (the tool runs as usual).
- the script prints megabytes: the capture keeps the first 5 MB plus a rolling tail; inline view
  and spill come from it.
- no `scope` → refused before anything happens (R02.8).

Our design differences / decisions:
- Module `indus_tools`, variables `INDUS_RPC_*` (naming §a8).
- Pipeline refusals (token, allow-list, budget, foreground rule) come back as a wire-level
  `refused` answer and the stub raises `ToolCallRefused`; tool failures come back as the tool's
  own `{"error": ...}` dict and are listed in `tool_errors`. The reference returns both as dicts.
- Forbidden `terminal` arguments are refused, not silently dropped.
- The RPC server is a small thread server that hands each request to the calling turn's event
  loop, so a kernel (P10-10) keeps working when later calls come from another loop.
- Spill file is owner-only (0600) in a private cache dir.

Tests we will write:
- script calling `read_file` through RPC; 51st call refused in script; wrong token refused;
  env lacks `*_API_KEY` set in the parent scope; 60 kB stdout → head/tail + spill file; timeout
  on a fake clock → SIGTERM then SIGKILL; manual approval denied → nothing spawned; `command`
  argument → pointer error; interrupt kills the child; stderr shown on non-zero exit; strict vs
  project cwd; interpreter choice.

## Decisions for the lead
- Whole-script approval policy (E9): isolated backend → allowed; auto-approve or
  `approvals.mode: off` → allowed; nobody can answer (no broker, cron, one-shot, unattended) →
  the existing `approvals.*_mode` policy (default deny, so `indus -z` refuses execute_code unless
  `approvals.single_query_mode: approve`); interactive → asked once per session (choices once,
  session, deny; never "always", a script is not an allowlist entry), the smart judge first in
  `smart` mode. The reference never asks in an interactive CLI; we ask once per session.
- Handler extras needed from the host: `tool_registry`, `tool_hooks`, `enabled_tools` (added to
  `core/tool_round.handler_extras`). Without `tool_registry` the tool loads the built-ins into a
  private registry.
- `code_execution.kernel: auto` = session kernel on the local backend and a remote kernel where
  one starts (P10-11), script otherwise; `off` = script everywhere.

Compare checklist (filled after the rewrite; reopened `tools/code_execution_tool.py`,
`tools/code_execution_rpc.py`, `tools/code_execution_env.py`):
- [x] limits 300 s / 50 calls / 50 kB 40-60 / 10 kB / 5 MB spill (`tests/unit/tools/builtin/code/test_runner.py::test_long_output_is_cut_and_spilled`,
  `::test_fifty_first_call_is_refused_inside_the_script`, `::test_timeout_sends_sigterm_then_sigkill`)
- [x] env scrub list and safe prefixes via `child_env(variant="code")` (`::test_child_env_has_no_credentials`)
- [x] token check, allow-list, budget, foreground rule, log (`tests/unit/tools/builtin/code/test_rpc.py`)
- [x] whole-script approval before anything is written (`::test_manual_approval_denied_spawns_nothing`,
  `::test_session_approval_is_asked_once`, `::test_nobody_to_ask_blocks_by_default`,
  `::test_isolated_backend_needs_no_approval`)
- [x] misrouted `command`, non-text or empty `code` (`::test_misrouted_command_points_to_terminal`, `::test_bad_code_argument`)
- [x] interrupt kills the child (`::test_interrupt_kills_the_child`); project/strict cwd and interpreter
  (`::test_project_mode_runs_in_the_session_directory`, `::test_strict_mode_runs_in_its_private_directory`,
  `::test_interpreter_choice`)
- Deliberate differences: output is masked before it is cut and spilled (the spill file never holds a
  secret the inline text hides); the spill is 0600 in a private dir; refusals raise in the script; the
  RPC server is thread-based and loop-independent; forbidden terminal arguments are refused, not dropped;
  interactive sessions are asked once per session (see Decisions).
- Not taken: the gateway self-restart guard (no gateway process yet, P15); Windows essential env names
  (Windows is WSL2, D-16.55); the reference's per-host observability counter.

## Status

Done (2026-10-19). Files: `src/indus/tools/builtin/code/{__init__,settings,output,launch,rpc,gate,runner}.py`,
tests `tests/unit/tools/builtin/code/{_rig,conftest,test_rpc,test_runner}.py`. Shared: BUILTIN_MODULES row,
`tools/toolsets.py` (`code_execution` set, `execute_code` in the core list) and its test,
`core/tool_round.handler_extras` (+ `tool_registry`, `tool_hooks`, `enabled_tools`), config section
`code_execution` and `test_schema`, `tests/unit/cli/setup/test_wizard.py` (the core bundle answer now
includes `code_execution`), tool-array goldens regenerated once for the new toolset (`execute_code`
joins the cli, cron, api_server and acp arrays; prompt text unchanged).

## Wave 46 (builder g): tests that survive a long base temp (release checklist RS11)

Read: none from the reference (test hygiene only).

- `tests/short_paths.py`: `short_dir(prefix)` makes a fresh owner-only folder directly under `/tmp`
  (system temp folder where there is no `/tmp`) and removes it afterwards; `tests/conftest.py`
  exposes it as the `short_tmp` fixture. Socket tests use it instead of `tmp_path`:
  `test_rpc.py::test_long_socket_path_moves_to_a_short_private_dir` (the product's short dir now
  lands in `short_tmp`, which stays short under any `--basetemp`),
  `test_watchdogs.py::test_sd_notify_datagrams`, and the two `short_home` fixtures in
  `test_control_socket.py` / `test_attach_verbs.py` (same behaviour, one helper).
- `test_runner.py::test_manual_approval_denied_spawns_nothing` asserted the full marker path in the
  approval text; the approval display wraps and shortens a long line, so a long base temp failed
  it. The script now names the path on a line of its own and the test checks the short line that
  writes the file.
- Checked: those five files under `--basetemp` of 140 bytes: 122 passed, 1 skipped (both named
  tests failed before). A full-suite sweep for other socket tests under a long base temp was not
  run (time box); `tests/contract/mcp_serve` and `tests/integration/gateway` sockets live in the
  profile home, whose long-path pointer file the product already handles (ran green above for
  `test_daemon_start.py` and `test_gateway_e2e.py`).
