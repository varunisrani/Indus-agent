# Task: persistent session kernel, local backend (P10-10) Phase: 10 Owner: P10 wave-1 builder H

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/code_kernel.py` (lifecycle,
keys, LRU, child owners, capture, cell result). Spec: research `02-tools.md` §3.19; trap R02.32
(kernel cap starvation). Closed before writing.
Evidence: none.

What it does (own words): instead of a fresh process per call, one Python child per (owner,
mode, interpreter, cwd, tool set) keeps running, so variables and imports survive between
`execute_code` calls.

- Owner: the session id (stable across turns); a delegated child's kernels use
  `<parent>::child::<child>` (P10-06 passes it as the `code_kernel_owner` extra and disposes them
  with `dispose_child`). Child kernels are pinned: never evicted and not counted in the cap.
- Cap: at most `code_execution.max_session_kernels` (4) unpinned kernels; the least recently used
  idle one is closed when a fifth starts; a kernel running a cell is never evicted.
- Idle: `code_execution.kernel_idle_timeout` (1,800 s) on the scope's clock; swept on each
  acquire and by `reap_idle()` (the host may run `run_reaper()`); the runner itself also exits
  after the same idle time of real time, and at once when its request pipe closes (host gone).
- `reset: true` closes the kernel first; the result says `state_reset`.
- Timeout or interrupt kills the kernel (ordered tree kill), the result says state was lost and
  the next call reports `state_lost: true` with a fresh kernel. A cell that calls `sys.exit`
  ends the kernel.
- Wire: requests on a pipe (fd passed to the child), replies on a second pipe; stdout/stderr of
  the child are raw output of the running cell. Per cell the runner captures ≤ 1,000,000 bytes of
  each stream; the host keeps ≤ 1,000,000 bytes of raw output per cell. Then the P10-08 output
  rules (50 kB inline, spill) apply.
- RPC: one socket server per kernel; each cell installs a fresh pipeline (its own budget of 50,
  its own dispatcher bound to this call's context); between cells requests are refused.

Inputs → outputs: as P10-08; the result gains `kernel: {mode: "session", reused, execution_count,
state_reset?, state_lost?, ended?}`.

Files: `src/indus/tools/builtin/code/kernel.py`, `src/indus/tools/builtin/code/kernel_runner.py`
(runner source), `tests/unit/tools/builtin/code/test_kernel.py`.

Edge cases / failure modes:
- two cells for one key at once: a per-kernel lock serialises them.
- kernel dies between cells (killed outside): next call sees it dead, reports state lost.
- host exits without shutdown: `atexit` force-kills every live kernel tree; the runner's pipe
  watchdog ends it even after a SIGKILL of the host.

Our design differences / decisions:
- Requests and replies travel on dedicated pipes, not framed inside stdout.
- `code_execution.kernel: off` disables kernels (script mode, P10-08).

Tests we will write:
- variable from call 1 visible in call 2; `reset` clears; 5th kernel evicts the LRU one; a pinned
  child kernel survives eviction and is outside the count; idle 1,800 s on a fake clock →
  disposed; timeout kills and the next call reports fresh state; RPC inside a cell; dispose by
  owner; no stray processes after shutdown.

## Decisions for the lead
- Host wiring: `await kernel.POOL.shutdown()` and `await remote.POOL.shutdown()` at exit (an `atexit`
  force kill of local kernels is the backstop); for long-lived hosts run
  `kernel.run_reaper(clock, lambda: <idle seconds>)` as a background task; on `/new` or session close
  `await kernel.dispose_session(session_id)`.
- P10-06: pass `code_kernel_owner=kernel.child_owner(parent, child)` in the child's handler extras
  and `await kernel.dispose_child(child_session_id)` at child teardown (closes local and remote ones).

Compare checklist (filled after the rewrite; reopened `tools/code_kernel.py`):
- [x] state kept, reset, per-call budget (`tests/unit/tools/builtin/code/test_kernel.py::test_state_survives_between_calls_and_loops`,
  `::test_reset_discards_state`, `::test_budget_is_per_call`)
- [x] LRU 4 (`::test_fifth_kernel_evicts_the_least_recently_used`), idle 1,800 s on a fake clock
  (`::test_idle_kernel_is_disposed_after_1800_seconds`), per-cell 1 MB (`::test_per_cell_capture_is_bounded`)
- [x] child kernels pinned and outside the cap, running kernels never evicted (R02.32:
  `::test_child_kernels_are_pinned_and_outside_the_cap`, `::test_pool_never_evicts_a_busy_or_pinned_kernel`)
- [x] timeout/interrupt kill and the next call says state was lost (`::test_timeout_kills_the_kernel_and_the_next_call_starts_fresh`,
  `::test_interrupt_kills_the_kernel`, `::test_kernel_killed_between_calls_reports_lost_state`)
- [x] host-death watchdog (`::test_runner_exits_when_indus_closes_its_pipe`); `sys.exit` ends it (`::test_sys_exit_ends_the_kernel`)
- Deliberate differences: pinned (child) kernels are also exempt from the idle sweep (the reference reaps
  them when idle); the next call after a timeout reports `state_lost`; spill of a long cell is done by
  the host, not the runner; replies travel on their own pipe.
- Not taken: the weekly sweep of staging dirs left by a host that died (follow-up: a cheap sweep of
  `indus-kernel-*` / `indus-code-*` older than 7 days under the staging root); Windows parent handle.

## Integration (lead, P10 slices G+H)
- Host wiring lives in `service/code_host.py`: `SessionService.close` disposes the closed session's
  kernels (current id plus the ids it continued from), so `/new`, `/clear` and switching end them;
  `KernelUpkeep` (passed by `cli/runtime.build_service`) starts `run_reaper` with the first session
  of an interactive run and shuts both pools down at `aclose` (before the terminal runtime).
- P10-06: `build_child` sets `SessionRuntime.code_kernel_owner = kernel.child_owner(...)`; the
  `code-kernels` releaser in `core.subagents.teardown` calls `dispose_child`.
- `test_kernel_killed_between_calls_reports_lost_state` waited for the zombie leader in `/proc`,
  which a multi-threaded runner shows before `waitpid` can reap it (4/10 alone failed); it now
  waits for `child.alive()` to turn false (15/15).

## Status

Done (2026-10-19). Files: `src/indus/tools/builtin/code/{kernel,kernel_runner}.py`,
`tests/unit/tools/builtin/code/test_kernel.py`.
