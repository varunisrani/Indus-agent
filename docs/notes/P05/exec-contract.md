# Task: Execution contract + command wrapper (P05-01) Phase: 05 Owner: wave-1 builder D

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/environments/base.py` (whole:
snapshot bootstrap and its fallbacks, the command wrapper, heredoc stdin, marker parsing, the
unified execute flow), `tools/environments/base_session_env.py` (whole: session-var exclusion,
bootstrap and wrapper script builders, marker split), `website/docs/developer-guide/terminal-environment-plugin.md`
(whole: the object contract a backend must meet), `tools/terminal_tool_sudo.py` (only the
compound-background rewrite and the quote-aware scanner it uses). Test names only of
`tests/tools/test_terminal_compound_background.py`, `test_snapshot_multiline_session_env_injection.py`,
`test_terminal_cwd_echo.py`, `test_base_environment.py`. Spec: research `02-tools.md` traps R02.1,
R02.10, R02.23. Closed before writing.
Evidence: none.

What it does (own words): every backend runs a command the same way, so the terminal tool, the
file tools and later backends (P11, P21) see one contract.

- `Environment` (protocol): `await execute(command, *, timeout, cwd, stdin, bounded, on_activity,
  cancel) -> ExecResult`, `await cleanup()`, `describe()`, plus the flags `isolated` (commands do
  not touch the host: deny globs apply, R02.6), `container_paths` (cwd and paths are the
  sandbox's), `stdin_mode` (`pipe` = process stdin, `payload` = the backend ships the bytes itself,
  `heredoc` = stdin is embedded in the script).
- `ExecResult`: `output`, `returncode` (the field is `returncode`, as the reference code has it;
  its docs say `exit_code`, R02.23), `cwd_observed`, `timed_out`, `interrupted`,
  `full_output_path`, `output_total_chars`, `stdin_error`, `environment_recreated`, `pid`.
- A spawn-per-command shell model: at start a login bash writes a **snapshot** of its exported
  variables, functions (not `_private` helpers) and aliases to `<tmp>/indus-snap-<id>.sh` (built in
  a `mktemp` file, then moved over, umask 077, 30 s limit). Per-session variables
  (`INDUS_SESSION_*`, `INDUS_HOME`, `AI_AGENT`, `INDUS_AGENT`, …) are **unset in a subshell before
  `export -p` runs**: filtering `declare -x` lines would let a value with an embedded newline smuggle
  a command into the next `source` (R02.1). If the bootstrap fails, every command runs under
  `bash -l`; if a ≤15 s probe shows plain `bash -c true` works while login bash is broken, commands
  run under plain `bash -c` instead.
- Each command runs as one script: source the snapshot quietly → `cd` to the cwd (`~`, `~/x` keep
  their expansion through `$HOME`; everything else single-quoted; failure exits 126) → the command
  through `eval` → keep its status → (persistent shell on) re-dump the environment the same safe
  way → print `\n__INDUS_CWD_<id>__<pwd -P>__INDUS_CWD_<id>__\n` → exit with the status. The marker
  line is stripped from the output; `cwd_observed` is set only when this command printed it (a
  killed or exited command did not, so the old cwd stays, R02.10).
- `A && B &` / `A || B &` become `A && { B & }`: otherwise bash backgrounds a subshell that waits
  for B and its open stdout keeps the call from returning. Quotes, escapes, comments, redirects
  (`2>&1`, `&>`) and groups never count; a following command on the same line gets `;`.
- Heredoc stdin (`stdin_mode="heredoc"`): delimiter `INDUS_STDIN_<random hex>`, bytes delivered
  exactly (no extra trailing newline).

Inputs → outputs: command text, cwd, timeout, stdin text → wrapped bash script → `ExecResult`.

Files: `src/indus/backends/contract.py`, `src/indus/backends/wrapper.py`,
`src/indus/backends/spawned.py` (new: the shared spawn-per-command flow every shell backend
inherits), `tests/unit/backends/test_wrapper.py`, `tests/unit/backends/test_spawned.py`.
Shared: `src/indus/foundation/errors.py` (rows `backend.error`, `backend.connection`,
`backend.unavailable`).

Edge cases / failure modes:
- output without a trailing newline before the marker; binary noise (U+FFFD) around it; marker
  absent (killed command) → no cwd change; a second marker pair later wins; marker split across
  the 4 KiB look-back is ignored.
- session variable with a newline and `$(…)` exported by a command: never in the snapshot, never
  executed by a later `source`.
- a cwd starting with `-` (`cd --`), with spaces, `~`, `~/`, `~/a b`.
- compound rewrite: idempotent; `|` inside the chain kept; `;`, newline end a chain; `&` after
  `>`/`<` is a redirect; trailing command on the same line gets a separator.
- heredoc payload containing the delimiter-like text, quotes, `$`, backslashes, no trailing
  newline, or several trailing newlines.

Our design differences / decisions:
- `execute` and `cleanup` are coroutines (asyncio core, D-04.1); the reference's are blocking.
- Cancellation is a small `CancelSignal` protocol (`is_set()`) in `backends.contract`: backends sit
  below `core` and may not import `core.interrupts`; `TurnControl.stop` (an `asyncio.Event`) fits it.
  Cancelling the awaiting task also kills the command.
- Per-command `eval` wrapper and marker for every backend, local included (the reference's local
  backend reads a cwd file instead).
- Heredoc delivery is our own: the body goes into a variable with a sentinel character that is
  removed again, and a process substitution feeds the group; no reliance on `read -d ''`.
- Profile-scoped passthrough save/restore around `source` is not needed: an environment never
  serves two profiles (the registry keys it by profile home, R02.8).
- `backends/__init__.py` stays a one-line docstring (the layers test requires it); public names
  live in `indus.backends.contract`.

Tests we will write: golden wrapper and bootstrap scripts; marker split table (no newline, binary
noise, absent, two pairs, look-back limit); tilde-preserving quoting; compound rewrite table +
`bash -n` parse + a trailing statement that really runs; heredoc round trip through real bash;
snapshot exclusion with a hostile multi-line session value through real bash; bootstrap fallback
to `bash -l` and to plain `bash -c`; `ExecResult`/flags defaults.

## Decisions for the lead
- Added `src/indus/backends/spawned.py` (shared execute flow) — the row's two files could not hold
  it without mixing the pure script builders with process handling.
- `backends/__init__.py` exports nothing (layers test); import from `indus.backends.contract`.
- The command clock is the real `SystemClock` by default, not `scope.clock`: a real child runs on
  real time; tests inject a `FakeClock` with fake processes.

Compare checklist (filled after the rewrite; reopened `tools/environments/base.py`,
`tools/environments/base_session_env.py`, the plugin guide, the compound-background rewrite):
- [x] wrapper steps in order: quiet source, agent markers, pagers, `cd --` or 126, eval, status,
  umask, guarded re-dump, marker, exit (test: `tests/unit/backends/test_wrapper.py::test_golden_command_wrapper`,
  `::test_wrapper_without_snapshot_or_persistence`)
- [x] bootstrap: mktemp + mv, export dump with session vars unset first, functions by name
  (no `_private`), aliases, shell options, cd back to the configured cwd, marker (test:
  `::test_golden_bootstrap`, `tests/unit/backends/test_spawned.py::test_snapshot_ready_reads_the_starting_cwd`)
- [x] R02.1 session vars never in the snapshot, multi-line value never executed (test:
  `::test_session_var_with_newline_never_reaches_a_later_source`,
  `tests/unit/backends/test_local.py::test_session_id_reaches_commands_but_not_the_snapshot`)
- [x] fallbacks: login per command; plain `bash -c` when the ≤15 s probe passes (tests:
  `test_spawned.py::test_broken_login_shell_falls_back_to_plain_bash`,
  `::test_both_broken_runs_every_command_under_a_login_shell`,
  `test_local.py::test_snapshot_failure_falls_back_to_plain_bash`)
- [x] marker split: last pair, 4 KiB look-back, strip from the injected newline; no marker → no
  cwd change (tests: `test_wrapper.py::test_marker_split`, `::test_marker_absent`,
  `tests/contract/backends/test_execution_contract.py::test_exit_without_marker_keeps_the_cwd`)
- [x] reported cwd must exist locally, else not adopted (test: `test_local.py::test_cd_persists_and_cwd_is_reported`)
- [x] tilde-preserving `cd` quoting (test: `test_wrapper.py::test_cd_target_quoting_keeps_tilde`)
- [x] compound `&` rewrite incl. redirects, trailing separator, case arms, idempotence (tests:
  `::test_compound_background_rewritten`, `::test_compound_background_left_alone`,
  `::test_rewrite_parses`, `::test_rewritten_trailing_statement_runs_and_does_not_wait`)
- [x] heredoc stdin byte-exact, fresh delimiter (tests: `::test_heredoc_stdin_is_byte_exact`,
  `::test_heredoc_delimiter_is_fresh_each_time`, `test_spawned.py::test_heredoc_backends_get_stdin_inside_the_script`)
- [x] `returncode` field (R02.23), recreated flag folded once (tests: `::test_contract_defaults`,
  `test_spawned.py::test_recreated_sandbox_is_reported_once`)
- [x] gap found: a per-call workdir must not become the session cwd (R02.10) — `_take_cwd(adopt=)`,
  pinned by `test_spawned.py::test_per_call_workdir_is_reported_but_never_adopted`
- Deliberate differences: async `execute`/`cleanup`; a `CancelSignal` protocol instead of
  thread interrupt bits; marker for the local backend too (no cwd file); our heredoc delivery
  (sentinel character) instead of `read -d ''`; `compgen -A function` to list functions.
- Not taken: profile-scoped passthrough save/restore around `source` (an environment never serves
  two profiles); login-shell init file lists (`shell_init_files`, auto `~/.bashrc`); host file
  fetch helpers (`fetch_file`, `fetch_realpath`: P05-15/P11 can add them on the contract);
  macOS `open` rewrite; the remote file-sync `_before_execute` hook (P11 adds it next to
  `_refresh_cwd`); sudo rewrite (P05-10 hooks into `_prepare`).

## Status

Done (2026-10-18). Files: `src/indus/backends/{contract,wrapper,spawned}.py`,
`tests/unit/backends/{test_wrapper,test_spawned}.py`. Shared: `src/indus/foundation/errors.py`
(three `backend.*` rows). `backends/__init__.py` unchanged (layers test).
