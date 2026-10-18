# Task: Wait loop, backstop, bounded capture (P05-02) Phase: 05 Owner: wave-1 builder D

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/environments/base_wait.py`
(whole: debug trace only), `tools/environments/base_output.py` (whole: head/tail collector, spill
tee, stdin writer, drain loop), `tools/environments/streams.py` (module docstring and the stream
helpers: long-lived stdio, not this task), `tools/environments/base.py` (the wait loop, the
foreground registry and exit fence, the execute backstop), `agent/deadline.py` (`run_bounded_sync`
only). Test names only of `tests/tools/test_terminal_bounded_execute.py`,
`test_local_background_child_hang.py`, `test_terminal_timeout_output.py`,
`test_terminal_exit_semantics.py`,
`test_tool_output_limits.py`. Spec: research `02-tools.md` traps R02.2, R02.3; D-04.1.
Closed before writing.
Evidence: none.

What it does (own words): waits for a spawned command without blocking the event loop and always
comes back.

- Poll loop: starts at 5 ms, grows ×1.5 up to 200 ms, on an injected clock. Each round: the turn's
  cancel signal → kill the tree, return the output so far + `[Command interrupted]`, rc 130,
  `interrupted=True`; the deadline → kill, partial output + `[Command timed out after Ns]`, rc 124,
  `timed_out=True` (a command that itself exits 124 is not flagged); an activity callback at most
  every 10 s so long commands do not look idle. Cancelling the awaiting task kills the tree too.
- Drain: the child's stdout+stderr are read on a worker of the `terminal` blocking pool with a
  0.1 s `select`, decoded incrementally (UTF-8, replacement characters), and stopped ~0.3 s after
  the leader exits even when a background grandchild keeps the pipe open.
- **Backstop** (R02.3): the whole spawn + wait runs as its own task, raced against a wall-clock
  bound of timeout + 2 s; if the inner wait never comes back the tree is killed and the result is
  rc 124 with `timed_out=True` and whatever output was captured.
- **Exit fence**: one process-wide table of running foreground commands. `fence_and_kill()` (hard
  exit) closes the fence — no new foreground spawn starts, it gets rc 130 "host is exiting" — and
  force-kills every live tree within 0.5 s.
- **Bounded capture** (foreground model path only, R02.2): keep the first 40 % and the last 60 %
  of `tool_output.max_chars` (50,000) and, on the first overflow, tee the full stream (from byte
  0) to `<home>/cache/terminal-output/out-<ts>-<pid>-<id>.log` (exclusive create, no symlink
  follow, 0600, ≤5,000,000 chars with a capped note); spill files older than 7 days are pruned when
  a new bounded capture starts. The rendered text always fits the limit, a required status suffix
  included. Internal callers (file ops, log reads) pass `bounded=False`: nothing is cut.
- Stdin is written by its own worker; an encode failure (lone surrogate) is reported as
  `stdin_error` and stdin is still closed so the child sees EOF.

Inputs → outputs: process handle, capture, timeout, clock, cancel signal, activity callback →
`ExecResult`.

Files: `src/indus/backends/wait.py`, `src/indus/backends/capture.py`,
`tests/unit/backends/test_wait.py`, `tests/unit/backends/test_capture.py`,
`tests/integration/core/test_d04_1_terminal_backstop.py` (D-04.1 criterion b, real `sleep`).

Edge cases / failure modes:
- deadline and exit in the same round → the exit wins (no flag).
- a wedged reader / a wait that never returns → still back by timeout + 2 s.
- interrupt before the first poll; interrupt and deadline together → interrupt.
- output with no characters at the deadline → the timeout note alone (no leading blank line).
- spill dir unwritable or a planted symlink → no spill, capture still works.
- multibyte character split across reads; invalid UTF-8 → U+FFFD.
- after the fence: spawns refused; a spawn that raced the fence is force-killed at once.

Our design differences / decisions:
- asyncio: the wait is a coroutine on the injected `Clock`; the backstop races a task against
  `clock.sleep(timeout + 2)` instead of a thread with a blocking wait. Drains and stdin writes go
  through `foundation.blocking.run_blocking` on a dedicated `terminal` pool (32 workers, refuses
  when full), never raw threads.
- The kill on the deadline path is the backend's own ordered kill (P05-03); the backstop and the
  exit fence use the backend's force kill.
- Our truncation note wording; the note's length is fixed before the cut so the result never
  exceeds the limit (no fixed-point loop).
- Not taken: the "yield to background" hand-over (P05-11/13 decide), the opt-in debug trace.

Tests we will write: fake process + fake clock: deadline → 124 + flag, own exit 124 → no flag,
interrupt → 130, cancel of the awaiting task kills, activity callback cadence, wedged wait bounded
by timeout + 2 s, poll back-off sequence, stdin error surfaced; capture: 60,000 chars → 50,000 kept
+ overflow log with the full text, suffix kept inside the limit, 6 MB unbounded intact, spill cap,
spill file mode 0600 and symlink refused, old spills pruned; exit fence; D-04.1 (b) with a real
`sleep` child: killed by timeout + 2 s while a heartbeat task keeps ticking.

## Decisions for the lead
- New `ERROR_CODES` rows live with P05-01 (`backend.*`); this task adds none.
- `tool_output.max_chars` (50,000) added to the config schema with a `ToolOutputSection`.
- The `terminal` blocking pool (32) is local to `backends.wait`, not in `POOL_SIZES`, because a
  foreground command holds its drain worker for its whole life.

Compare checklist (filled after the rewrite; reopened `tools/environments/base_output.py`,
`base_wait.py`, the wait loop / foreground table / execute backstop in `base.py`,
`agent/deadline.py::run_bounded_sync`):
- [x] adaptive poll 5 ms ×1.5 → 200 ms (test: `tests/unit/backends/test_wait.py::test_poll_backs_off_from_5ms_to_200ms`)
- [x] interrupt → kill, rc 130, note, flag; also set before the first poll (tests:
  `::test_interrupt_returns_130_with_output`, `::test_interrupt_set_before_the_first_poll`,
  `tests/contract/backends/test_execution_contract.py::test_interrupt_gives_130`)
- [x] deadline → kill, partial output + note, rc 124 + flag; no blank line when silent; own
  `exit 124` unflagged; exit beats deadline (tests: `::test_deadline_kills_flags_and_keeps_partial_output`,
  `::test_timeout_without_output_has_no_leading_blank`, `::test_command_exiting_124_itself_is_not_flagged`,
  `::test_exit_wins_over_a_deadline_in_the_same_round`)
- [x] R02.3 backstop timeout + 2 s, force kill, rc 124 (tests: `::test_backstop_bounds_a_wait_that_never_returns`,
  `tests/integration/core/test_d04_1_terminal_backstop.py::test_backstop_kills_when_the_wait_itself_never_returns`,
  `::test_timed_out_command_is_killed_while_the_loop_stays_responsive` — D-04.1 criterion b)
- [x] reader stops after exit even when a grandchild holds the pipe; wedged reader bounded
  (tests: `::test_wedged_reader_is_still_bounded`, `test_wrapper.py::test_rewritten_trailing_statement_runs_and_does_not_wait`)
- [x] caller cancellation kills the tree (tests: `::test_cancelling_the_wait_kills_the_tree`,
  `test_local.py::test_cancelled_call_kills_its_command`)
- [x] activity callback every 10 s (test: `::test_activity_callback_every_ten_seconds`)
- [x] stdin on its own worker; encode failure → `stdin_error` + note, child still sees EOF
  (tests: `::test_stdin_error_is_reported`, `test_local.py::test_stdin_pipe_and_encode_failure`)
- [x] exit fence: refuse after, force-kill live, kill a spawn that raced it (tests:
  `::test_fence_force_kills_live_commands_and_refuses_new_ones`, `::test_no_spawn_after_the_fence`,
  `::test_a_spawn_that_raced_the_fence_is_force_killed`)
- [x] R02.2 bounded 40/60 window, spill from byte 0 on first overflow, 0600 exclusive no-follow,
  5,000,000 cap, 7-day prune; unbounded intact (tests: `tests/unit/backends/test_capture.py` all,
  `test_execution_contract.py::test_bounded_capture_cuts_and_keeps_the_full_text`,
  `::test_unbounded_capture_keeps_everything`)
- [x] gap found: an ordered kill with a 2 s grace on our deadline raced the backstop (both 2 s) —
  the deadline kill now gets a 1.5 s budget (`DEADLINE_KILL_BUDGET_S`), pinned by
  `test_wait.py::test_deadline_kills_flags_and_keeps_partial_output` and the D-04.1 test's
  `TIMEOUT <= elapsed < TIMEOUT + 2`
- [x] gap found: zombies left by a non-reaping PID 1 kept a dead group "alive" for the whole
  grace — liveness now ignores zombies, pinned by the D-04.1 timing test above
- Deliberate differences: asyncio wait on an injected clock; backstop = racing task, not a
  thread; drain/stdin workers from a dedicated `terminal` blocking pool; the backstop uses the
  force kill (the reference used the graceful kill plus a tree sweep); our cut-note wording,
  sized up front.
- Not taken: yield-to-background hand-over (P05-11/13), debug wait tracing, the graceful
  (unfenced) "kill every live foreground command" variant — the host only needs the hard one;
  adapters for SDK backends without a real process (P21 adds a handle).

## Status

Done (2026-10-18). Files: `src/indus/backends/{wait,capture}.py`,
`tests/unit/backends/{test_wait,test_capture,conftest}.py`,
`tests/integration/core/test_d04_1_terminal_backstop.py`. Shared: `src/indus/foundation/config/schema.py`
(`tool_output.max_chars`).
