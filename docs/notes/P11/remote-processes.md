# Task: Background processes on non-local backends (P11-12) Phase: 11 Owner: P11 builder B

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/process_registry.py`
(`spawn_via_env`, `_env_poller_loop`, the sandbox branch of `_signal_kill`),
`tools/process_registry_env_log.py` (log delta command, skimmed). Spec: research `02-tools.md`
§3.12, §6 R02.2, R02.12, R02.17, R02.19; phase file P11-12. Closed before writing.
Evidence: none.

What it does (own words): `terminal(background=true)` on Docker, SSH or any backend that is not
the host gets a job that lives inside that backend. The spawn is one short `env.execute` (10 s):
make the temp dir, start `( nohup bash -lc <cmd> > log 2>&1; echo $? > exit )` in the background
in its own process group (`set -m`), with its own stdio detached so the call returns at once,
and print its PID from the pid file. Files: `indus_bg_<proc>.{log,pid,exit}` in the backend temp
dir. No PID → `failed_start`. While any remote job of a backend runs, one poll loop per
environment wakes every 2 s and, for each job, runs one command that prints the log size, the
new log bytes (base64, so a multibyte character cut at the seam survives), whether the process
group is alive, and the exit code when the exit file exists. Every read uses unbounded capture
(R02.2). New output goes into the P05 registry (`ingest`), so watch patterns, heartbeats and the
2,000-char completion tail work unchanged; the exit goes through `notice_exit`, so notify-on-exit
fires once and only where it can be delivered (R02.17). Kill: TERM to the remote process group,
the P05 grace (`terminal.daemon_term_grace_s`), then KILL (R02.12). A backend that is gone (the
environment was evicted or cleaned up, a connection error, or `environment_recreated`) → the job
is `lost` (R02.19). No PTY: a `pty=true` call runs without one and the result says so.

Inputs → outputs: spawn request with the environment → `ProcessRecord`; polls → registry output,
completion events.

Files: `src/indus/backends/remote_processes.py`, `tests/unit/backends/test_remote_processes.py`.
Shared: `src/indus/backends/processes.py` (route non-local spawns), `src/indus/tools/builtin/terminal.py`
(`_background` passes the environment instead of refusing non-host backends).

Edge cases / failure modes:
- the wrapper prints junk before the pid → the last all-digit line wins; none → `failed_start`
  with the output.
- log shrank (rotated/truncated) → read from 0 again.
- a poll fails once (pool busy, timeout) → retry next round; 5 failures in a row → `lost`.
- kill of a job whose environment is gone → `lost`, no remote call.
- process exit while a poll is in flight → the final poll after the exit file appears reads the
  rest of the log before the completion.
- 300 kB of output in one poll → all of it (base64 decoding; no cap).

Our design differences / decisions:
- one poll command per job per round (the reference runs up to three: log delta, `kill -0`,
  exit file) and base64 for the delta (the reference trims to a UTF-8 boundary in shell).
- one poll task per environment, not one thread per job.
- jobs run in their own process group so the kill reaches the whole tree.

Tests we will write (scripted fake backend): spawn returns the pid and the command text; poll
reads the log tail; exit file → completion event once; 300 kB log read intact and `bounded=False`
on every call; missing pid → `failed_start`; connection error → `lost`; `environment_recreated` →
`lost`; kill sends TERM to the group then KILL after the grace; `pty=true` → note; notify only
where deliverable.

Compare checklist (filled after the rewrite; reopened `tools/process_registry.py`):
- [x] wrapper shape, 10 s, unbounded reads, pid file (test: `tests/unit/backends/test_remote_processes.py::test_spawn_reports_the_pid_and_runs_the_wrapper`)
- [x] no pid → failed_start with the output (test: `::test_missing_pid_is_a_failed_start`)
- [x] log delta per poll, completion once, unbounded capture (tests: `::test_poll_feeds_output_and_completion_fires_once`, `::test_large_log_read_intact_across_a_split_character`)
- [x] backend gone / recreated / closed → lost (test: `::test_backend_gone_marks_the_job_lost`); repeated read failures → lost (`::test_repeated_read_failures_end_as_lost`)
- [x] kill: TERM to the group, grace, KILL; tail read before the exit (test: `::test_kill_terms_the_group_then_kills_after_the_grace`)
- [x] no PTY, no stdin, note in the result (test: `::test_pty_is_not_available_remotely`); notify only where deliverable (`::test_no_completion_where_nothing_can_receive_it`)
- [x] end to end over the fake ssh CLI (test: `::test_ssh_background_job_end_to_end`)
- Deliberate differences: the reference sends one `kill <pid>` (no group, no SIGKILL); we follow the plan's ordered group kill. The reference treats any poll exception as lost at once; we retry transient failures (busy pool) up to 5 times. One poll command per job (the reference: up to three). The job's group is its own (`set -m`).
- Not taken: re-attaching remote jobs after a restart (their sandbox handle is gone; P11-14 marks them lost, like the reference skips them).

## Status

Done (2026-10-18). `src/indus/backends/remote_processes.py`, `tests/unit/backends/test_remote_processes.py`,
`tests/unit/backends/_aio.py` (async test helper). Shared: `src/indus/backends/processes.py`
(`SpawnRequest.environment`, `_spawn_remote`, pollers per environment, `notice_exit(reason=)`),
`src/indus/tools/builtin/terminal.py` (`_background` passes non-local environments through instead of
refusing), `tests/unit/tools/builtin/test_terminal_plan.py` (the old refusal test now checks the remote
spawn). Open for the lead: a poll may be the call that sees a backend's one-shot
`environment_recreated` flag, so the next foreground command would no longer carry the warning; Docker
recovery (P11-06) should also mark that environment's jobs lost directly.

## Integration (lead)

Merged in the P11 integration (2026-10-18, lead's verify tree on main with P08 wave 1; slices applied A, B, C).
- The poll only peeks at `environment_recreated`: when a poll result carries it, the poller marks the job lost and calls `mark_recreated` again, so the next foreground command still warns. Jobs also record the environment's `recreations` at start (`RemoteJob.generation`); a changed count means lost, and `stop` / `force_stop` never signal a job of a replaced sandbox. Tests: `test_poll_peeks_at_the_recreated_flag`, `test_replaced_sandbox_loses_the_job_without_signalling`.
- Docker recovery ends the environment's jobs directly through the lost handler the host registers (`TerminalRuntime`).
- The SSH end-to-end test keeps its job files in the test directory (it used to leave `indus_bg_*` files in `/tmp`).
- D-16.130.
