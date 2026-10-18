# Task: Local backend (P05-03) Phase: 05 Owner: wave-1 builder D

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/environments/local.py`
(env assembly, temp dir choice and pruning, cwd recovery, PATH completion, the process-group kill
and the recycled-PID guard, cleanup), `tools/environments/local_pythonpath.py` (first 100 lines:
which PYTHONPATH entries count as app-owned). Test names only of
`tests/tools/test_local_persistent_shell.py`, `test_local_setsid_descendant_sweep.py`,
`test_local_interrupt_cleanup.py`, `test_local_temp_dir.py`. Spec: research `02-tools.md` trap
R02.12, D-16.55 (Windows out of scope). Closed before writing.
Evidence: none.

What it does (own words): runs commands on the host with bash.

- Each command is `bash -c <wrapped script>` (`bash -l` when the snapshot failed), in a new
  session/process group, with stdout and stderr merged, stdin a pipe only when there is stdin.
- Child environment from the one builder (`indus.safety.child_env`, P05-05), then: missing sane
  PATH entries appended (system dirs, `~/.local/bin`), Indus's own bin directory prepended when it
  is missing, and PYTHONPATH entries that point into Indus's own install (the package's source root,
  the running venv's site-packages) removed.
- Temp dir: `terminal.temp_dir` (when it exists) → `TMPDIR`/`TMP`/`TEMP` (absolute) →
  `<home>/cache/terminal` (entries idle for 24 h pruned once per process) → the system temp dir.
- `terminal.persistent_shell` (default on): off means every command starts from the login-time
  snapshot (exports do not carry over), only the cwd persists.
- A cwd that vanished or became unreadable falls back to the nearest usable parent, so one
  `rm -rf .` does not wedge every later call.
- Kill (R02.12): snapshot the descendants first (they re-parent when the leader dies), SIGTERM the
  leader, wait up to `terminal.daemon_term_grace_s` (2.0) for it, SIGTERM the survivors (its
  group and snapshotted escapees), then SIGKILL whatever is left by the end of the grace. Before
  any group signal the leader's start time is compared with the one recorded at spawn: a recycled
  PID/PGID is never signalled; descendants are identity-checked the same way.
- Force kill (exit fence, backstop): SIGKILL the group (same guard) and the leader, no wait.

Inputs → outputs: scope config (`terminal.cwd`, `timeout`, `persistent_shell`, `temp_dir`,
`daemon_term_grace_s`, `tool_output.max_chars`) → `LocalEnvironment` meeting the contract.

Files: `src/indus/backends/local.py`, `tests/unit/backends/test_local.py`; shared:
`src/indus/foundation/config/schema.py` (terminal keys, see env-registry note).
Stand-in: `src/indus/safety/child_env.py` (slice A owns it).

Edge cases / failure modes:
- grandchild that ignores SIGTERM → SIGKILLed; a `setsid` escapee is reached through the snapshot.
- leader already reaped and its PID recycled (start time differs) → no signal at all.
- the child shares our own process group (should not happen with a new session) → never killpg.
- no `/proc` (macOS): no snapshot, no start time → group kill as best effort.
- `bash` not on PATH → `/bin/bash`; none at all → `BackendUnavailable`.

Our design differences / decisions:
- Process tree and start times read from `/proc` (no `psutil` dependency); elsewhere best effort.
- Kill is a coroutine on the injected clock (the grace wait never blocks the loop).
- Not taken: Windows/MSYS paths and Git Bash (D-16.55), shell init file lists and auto-sourcing
  `~/.bashrc` before the snapshot (login bash reads the profile anyway), desktop display variables,
  session client toolchain env, sudo NOPASSWD probe (P05-10).

Tests we will write (real subprocesses, no sleeps beyond process exit): `cd` persists across calls;
an exported variable persists with the persistent shell on and not off; cwd recovery after the
directory is removed; temp dir precedence; PATH completion and PYTHONPATH strip; grandchild that
ignores SIGTERM is SIGKILLed; `setsid` escapee killed; recycled-PID guard (fake start time mismatch
→ no signal); bootstrap fallback when the login profile exits.

## Decisions for the lead
- Child env stand-in signature assumed: `build_child_env(scope, *, variant="terminal",
  session_id=None) -> dict[str, str]` in `indus.safety.child_env`.
- Indus's bin dir is prepended only when missing from PATH (an existing entry keeps its place), so
  a venv bin dir never jumps ahead of the user's own `python`.

Compare checklist (filled after the rewrite; reopened `tools/environments/local.py` and
`local_pythonpath.py`):
- [x] `bash -c` / `bash -l -c`, new session, merged output, stdin pipe only with stdin (tests:
  `tests/unit/backends/test_local.py::test_cd_persists_and_cwd_is_reported`, `::test_stdin_pipe_and_encode_failure`)
- [x] temp dir precedence and once-per-process idle prune (tests: `::test_temp_dir_precedence`,
  `::test_terminal_cache_pruned_once_per_process`, `::test_prune_keeps_a_dir_with_a_recent_write`)
- [x] PATH completion, own bin dir, PYTHONPATH/venv strip by provenance (tests:
  `::test_complete_path_appends_sane_dirs_and_prepends_own_bin`, `::test_app_owned_pythonpath_entries_are_stripped`,
  `::test_run_env_drops_our_own_pythonpath`)
- [x] persistent shell switch (test: `::test_exports_follow_the_persistent_shell_switch`)
- [x] vanished cwd recovery (tests: `::test_removed_cwd_falls_back_to_the_parent`, `::test_nearest_usable_dir`)
- [x] R02.12 kill order, setsid escapees, TERM-ignoring grandchild (tests:
  `::test_grandchild_ignoring_sigterm_is_sigkilled`, `::test_setsid_escapee_is_reached_through_the_snapshot`,
  `::test_matching_start_time_is_signalled`)
- [x] recycled PID/PGID guard, never our own group (tests: `::test_recycled_pgid_is_never_signalled`,
  `::test_never_signals_our_own_group`)
- [x] gap found: when the child shares our group, its descendants were never TERMed (only the
  final SIGKILL reached them) — now signalled one by one, pinned by
  `::test_own_group_children_get_sigterm_one_by_one`
- [x] config read from the scope (test: `::test_open_local_reads_the_scope_config`)
- Deliberate differences: kill order per R02.12 (leader first, grace, survivors, then SIGKILL)
  instead of group TERM → 1 s → group KILL → sweep; `/proc` instead of `psutil`; exact start-time
  comparison (Linux ticks; no drift tolerance needed); Indus bin dir only prepended when missing.
- Not taken: Windows/MSYS (D-16.55); relative `terminal.cwd` matched against the launch dir's
  tail; managed runtime dirs (`<home>/bin`, package-manager node) on PATH — no managed runtimes
  yet; desktop display variables; client toolchain env; first-party passthrough names in the
  snapshot exclusion; background-artifact triplet grouping in the temp prune (P05-13 keeps its
  files out of the terminal temp dir or groups them).

## Status

Done (2026-10-18). Files: `src/indus/backends/local.py`, `tests/unit/backends/test_local.py`.
Stand-in: `src/indus/safety/child_env.py` (slice A replaces it; assumed
`build_child_env(scope, *, variant="terminal", session_id=None) -> dict[str, str]`).
