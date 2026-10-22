# Task: Verify and verify-on-stop (P24-17) Phase: 24 Owner: P24 slice L1 builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/verify/recipes.py` (detection
order and command choices), `agent/verify/runner.py` (phases, timeouts, tail, start + readiness,
compose refusal), `agent/verify/environment.py` and `__init__.py` (skimmed), `agent/verify_hooks.py`
(pre_verify nudge cap), `hermes_cli/verify_cmd.py` (flags, report, exit codes),
`agent/verification_stop.py` (verify-on-stop switch and nudge). Closed before writing. Evidence: none.

What it does (own words): `indus verify` looks at a workspace and works out how to check it: the
first lockfile decides the package manager (pnpm, bun, yarn, npm; uv, poetry, pipenv); a
`package.json` gives a Node recipe (framework from its dependencies, scripts for build/test/start),
else Python (Django, FastAPI, Flask or generic), Go, Rust, Maven/Gradle, a Makefile's targets, a
compose file. It then runs phases `bootstrap`, `build`, `test` in that order, each command with a
600 s timeout through the terminal backend (so the same environment builder and approval rules
apply), stops at the first failure, and keeps the last 2,000 characters of output per command.
Unless skipped, a recipe's start command is booted and its readiness URL on 127.0.0.1 polled for up
to 60 s, then the service is stopped. A compose recipe refuses to build or start when that project
already has running containers. `--phase` picks phases, `--json` prints the result.
Verify-on-stop (off by default, `verify.on_stop`) runs `build` and `test` when the turn edited code
files and, on failure, sends the agent back with the failing command's tail (at most 2 times per
turn). The `pre_verify` plugin hook, after code edits, may also send the agent back (at most 3).

Inputs → outputs: workspace path → `Recipe`; `run_verify(host, root, recipe, phases)` →
`VerifyResult` (phases with exit code, duration, tail, timed_out; readiness). CLI exit 0 ok, 1
failed or no recipe, 2 bad path. Gates → nudge texts for `core.stop_gates`.

Files: `src/indus/tools/verify/{__init__,recipes,runner}.py`, `src/indus/cli/commands/verify.py`,
`tests/unit/tools/verify/test_recipes.py`, `tests/unit/tools/verify/test_runner.py`,
`tests/fixtures/verify/workspaces/`; gates in `src/indus/core/stop_gates.py`; shared: config
`verify` section, `cli/app.py` row.

Edge cases / failure modes:
- no recipe → exit 1 with a hint; a path that is not a directory → exit 2;
- a phase timing out → `timed_out`, no exit code, later phases not run;
- a command the approval rules refuse → that command fails with the refusal text, nothing runs;
- unreadable/invalid `package.json` → not a Node project (falls through to the next detector);
- prose-only edits (Markdown, text, LICENSE…) never trigger verify-on-stop;
- a start command that never answers → readiness false after 60 s, the process group is stopped.

Our design differences / decisions:
- Verify-on-stop runs the checks itself instead of asking the model to prove it ran them (the
  reference keeps a verification-evidence ledger we do not have); its nudge carries the failing
  output. Off by default as in the reference.
- Commands go through an `ExecHost` (the terminal backend) with an optional approval gate; the CLI
  uses the local backend and the approval pipeline without a person to ask (dangerous commands are
  refused, never run unasked).
- No saved recipe manifest in 1.0 (recorded as a follow-up).

Tests we will write: detection table over temp workspaces (pnpm lock, uv lock, Makefile with
`test`, compose, Go, Rust, Django, FastAPI, gradle wrapper, broken package.json); phase order and
stop-on-failure; per-phase timeout with a fake backend; tail 2,000 chars; approval refusal;
readiness timeout on a fake clock; compose refusal; CLI JSON and exit codes; verify-on-stop off by
default, on → nudge once per failure and capped; pre_verify cap.

## Decisions for the lead
- `verify.on_stop` runs `build` + `test` (not bootstrap, not start) at turn end; the gate needs the
  session's terminal environment (`host_for(session)`) from the host wiring.
- Recipes start servers on 127.0.0.1 (the reference binds 0.0.0.0 for Django/FastAPI/Flask).
- `indus verify` runs commands through the approval rules with nobody to ask (a command that
  needs approval is refused, not run).

Compare checklist (filled after the rewrite; reopened the files above):
- [x] lockfile order and Node/Python/Go/Rust/Java/Make/compose order (`test_recipes.py` table)
- [x] 600 s phase timeout, 60 s ready timeout, 2,000-char tail (`test_runner.py`)
- [x] compose live-container refusal (`test_runner.py::test_compose_with_running_containers_is_refused`)
- [x] pre_verify cap 3, verify-on-stop off by default (`tests/unit/core/test_stop_gates_lsp.py` verify cases)
- [x] gap found: a project with only `setup.py` got `pip install -r requirements.txt` — we use
  `pip install -e .` (`test_recipes.py::test_poetry_and_pyproject_install` covers the pyproject
  path); started services bind 127.0.0.1, matching the loopback readiness probe
  (`test_recipes.py::test_recipe_table`).
- [x] start + readiness: SIGTERM to the group, 10 s, SIGKILL
  (`test_runner.py::test_local_starter_and_loopback_probe`,
  `::test_readiness_polled_on_loopback_until_the_deadline`).
- Deliberate differences: on-stop runs the checks; approvals apply; no manifest (`--save`).
- Not taken: project-facts command merge (no such module here), evidence ledger, the
  `coding` flag in the `pre_verify` payload (no coding-context detector here).

## Status

Done (2026-10-21). Shared: config `verify` section (+ test), `cli/app.py` row, the CLI golden
order and non-TTY rows. Follow-up: a saved recipe file (`.indus/verify.json`, `--save`).

## Wiring (wave 42, WB — D-16.799)

Wired: `VerifyOnStopGate` is in every agent's standard gates (`core.stop_gates_lsp.standard_gates`),
running in the session's terminal environment (`EnvironmentRegistry.acquire` for the session) when
`verify.on_stop` is true; `PreVerifyGate` gets the plugin bus's `pre_verify` (`AgentOptions.pre_verify`,
set by `plugins.session.plugin_options`). The turn's edited files come from `LspToolHooks` even with
LSP off (`tests/unit/core/test_stop_gate_wiring.py::test_pre_verify_sees_the_turn_edits_without_a_language_server`).
