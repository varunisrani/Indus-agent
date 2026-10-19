# Task: remote kernels and script shipping (P10-11) Phase: 10 Owner: P10 wave-1 builder H

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/code_kernel_remote.py`
(whole), `tools/code_execution_tool.py` (remote per-call path, file shipping, env file launch),
`tools/code_execution_rpc.py` (poll loop). Spec: research `02-tools.md` §3.19; trap R02.2
(bounded vs full capture). Closed before writing.
Evidence: none.

What it does (own words): when the terminal backend is not this machine (Docker, SSH, later P21
sandboxes), all we have is `env.execute`. So:

- Check `python3` exists remotely; without it the call fails with a clear error.
- Kernel path (`code_execution.kernel: auto`): make a private kernel dir (`umask 077`, mode 700,
  `cells/` and `rpc/` inside) in the backend's temp dir; ship the runner, the `indus_tools` module
  (file transport) and an env file (RPC dir, token, TZ) through stdin, never argv; start the
  runner detached in its own session, PID recorded. Each call writes `cells/req_<n>.json`
  (temp + rename) and polls every 0.5 s (on the scope's clock) for `cells/res_<n>.json`; the same
  round trip drains tool requests (`rpc/req_<n>.json`), which are answered through the normal
  dispatcher and written back as `rpc/res_<n>.json` (temp + rename, 0600).
- If the kernel cannot start, fall back to shipping one script per call: private dir, module,
  script, env file, `python3 script.py` under the call's timeout, the same request-file RPC
  polled beside it, the dir removed after.
- Every internal read passes `bounded=False` (R02.2): a 2 MB tool result reaches the script whole.
- Kernel lost (transport drop, container restart, idle self-exit): the next call starts fresh and
  says state was lost. Timeout kills the runner (TERM, then KILL after 5 s) and removes the dir.
- Same pool rules as local kernels (cap 4 unpinned, idle 1,800 s, child owners pinned).

Inputs → outputs: as P10-08; `kernel: {mode: "remote", reused, ...}` on the kernel path.

Files: `src/indus/tools/builtin/code/remote.py`, `src/indus/tools/builtin/code/remote_runner.py`
(runner source), `tests/unit/tools/builtin/code/test_remote.py`.

Edge cases / failure modes:
- a half-written request file: only renamed (complete) files match the glob.
- a malformed or unauthorised request file: removed, no answer, nothing counted.
- the cell result never comes: bounded by the cell timeout, then the kernel is killed.
- the kernel dir vanishes: the runner exits by itself.
- the backend's temp dir: `env.temp_dir` when the backend has one, else the directory of its
  snapshot file, else `${TMPDIR:-/tmp}` asked from the remote.

Our design differences / decisions:
- One round trip per poll tick serves both the cell result and pending tool requests.
- Kernel runner calls `os.setsid()` and is killed as a group.

Tests we will write:
- over the fake docker client (`tests/fakes/backend/docker_cli.py`) and a bash-backed remote
  shell: kernel round-trips a cell and keeps state; missing `python3` → error, kernel start
  failure → script fallback; a request file answered with a `read_file` result; 2 MB result
  intact and every internal read unbounded; timeout kills the remote runner; no files left.

## Decisions for the lead
- Give `Environment` a public `temp_dir` (P11 backends know it); we read it when present.

Compare checklist (filled after the rewrite; reopened `tools/code_kernel_remote.py`):
- [x] remote poll 0.5 s (`tests/unit/tools/builtin/code/test_remote.py::test_default_poll_interval_is_half_a_second`); private 700 dirs
  and the token never on a command line (`::test_kernel_round_trips_cells_and_answers_request_files`)
- [x] request file answered with a `read_file` result (same test); over the fake docker client
  (`::test_docker_kernel_round_trip`)
- [x] unbounded internal capture, 2 MB result whole (R02.2: `::test_two_megabyte_tool_result_arrives_whole`)
- [x] kernel start failure → shipped script (`::test_kernel_start_failure_falls_back_to_a_shipped_script`);
  no `python3` but a `python` 3.8+ → shipped script (`::test_missing_python3_ships_a_script_with_python`);
  no Python at all → clear error (`::test_no_python_at_all_is_a_clear_error`)
- [x] timeouts stop the runner and remove its dir; next call reports lost state
  (`::test_remote_kernel_timeout_stops_the_runner`, `::test_shipped_script_errors_and_timeouts`)
- Deliberate differences: one poll round trip serves the cell result, tool requests and a liveness check
  (in-script tool calls may wait up to 0.5 s; the reference polls requests every 0.1 s); the remote cell
  captures fd 1/2 into private temp files, so output of programs a cell starts comes back too; a request
  with a wrong token gets a refusal answer instead of silence; scripts may run under `python` when
  `python3` is missing (the plan's "missing python3 → script fallback").
- Not taken: modal/daytona/vercel specifics (P21 backends inherit the generic path).

## Status

Done (2026-10-19). Files: `src/indus/tools/builtin/code/{remote,remote_runner}.py`,
`tests/unit/tools/builtin/code/test_remote.py`.
