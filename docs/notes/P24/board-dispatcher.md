# Task: Board dispatcher and worker lanes (P24-12) Phase: 24 Owner: wave-37 builder B1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `hermes_cli/kanban_db_dispatch.py`
(constants at the top, `DispatchResult`, `_classify_worker_exit` / `_exit_code_kind`,
`_classify_dead_worker_exit`, `_PROTOCOL_VIOLATION_FAILURE_LIMIT`, the memory-derived concurrency
helpers, `_default_spawn`), `gateway/kanban_watchers.py` (notifier and dispatcher loops),
`gateway/kanban_watchers_dispatcher.py` (settings), `gateway/kanban_watchers_notifier.py` (skim:
event kinds, cursor, failure count). Research 02 R02.17, R02.22. Closed before writing.

What it does (own words): a periodic tick turns `ready` board tasks into one-shot worker
processes. Each worker is `indus chat -q <task prompt>` for the task's assignee profile, run in the
task's workspace with the worker toolset, and it must end its run through a board tool (complete,
block, request review). The tick also notices how workers ended and books it: a clean exit that
left the task running is a protocol violation (three in a row block the task), exit 75 (provider
rate limit or overload) releases the task and cools that profile's lane for 300 s without counting
a failure, exit 78 (the provider refused the configuration) blocks the task as `capability` at
once, any other death counts a failure and the second failure blocks the task. A cap on running
workers comes from memory: one worker per 512 MB, never fewer than 2 or more than 8. Workers get
their environment from the one child-env builder (R02.22) and run one-shot, so nothing can deliver
a background completion to them (R02.17). Logs go to one file per task, rotated at 2 MiB with one
backup.

Watchers (daemon side): a notifier posts finished/blocked/review events to chats subscribed to a
task (cursor per subscription, rewound on a failed send, dropped when the task is archived), and
an hourly collector removes old finished-worker records (log files of runs ended long ago, pids of
ended runs) and subscriptions of archived tasks.

Inputs → outputs: `Dispatcher(store, spawner, settings).tick()` → `TickReport` (spawned,
rate_limited, crashed, violations, blocked, reclaimed, promoted, skipped_capacity, cooled lanes);
`indus kanban dispatch` runs one tick headless. `BoardWatchers.notify_once()` /
`gc_once()` / `run()`.

Files: `src/indus/board/dispatcher.py`, `src/indus/board/watchers.py`, `src/indus/board/__init__.py`,
`tests/unit/board/test_dispatcher.py`, `tests/integration/board/test_worker_lane.py`.

Edge cases / failure modes: worker not found on PATH or spawn error → failure counted; unassigned
ready task skipped; a lane cooled by exit 75 skips only that assignee's tasks; a worker still alive
past `max_runtime + 30 s` is terminated and booked as a timeout failure; a worker that outlives its
closed run by 120 s is terminated (reap); memory unknown → the floor (2).

Our design differences / decisions:
- one `Spawner` protocol (`spawn`, `poll`, `alive`, `terminate`); `ProcessSpawner` is the real one;
  tests use a fake spawner or real `python -c` children;
- the worker gets `INDUS_BOARD_TASK`, `INDUS_BOARD_RUN`, `INDUS_BOARD_CLAIM`, `INDUS_BOARD_SLUG` and
  `INDUS_BOARD_ROOT` through the builder's force entries (every `INDUS_` name is otherwise
  blocked); `--source kanban` keeps the worker's session out of lists;
- memory unknown → floor 2 instead of no cap (a cap that always exists is easier to reason about);
- wiring the tick and watchers into the gateway daemon's loop is the daemon owner's step (see
  decisions).

Tests we will write: fake worker exiting 75 cools the lane 300 s and leaves failures at 0; 78 blocks
as capability; clean exit while running counts a violation, the third blocks; second crash blocks;
concurrency clamp table; capacity respected; unassigned skipped; log rotation at 2 MiB; spawned env
has no provider key and carries the board pins; integration: real child processes exiting 0/75/78.

## Decisions for the lead
- Gateway daemon wiring (`src/indus/gateway/daemon/process.py`): start `BoardWatchers.run()` and a
  `Dispatcher` tick every `kanban.dispatch_interval_seconds` when `kanban.dispatch_in_gateway` is
  true; delivery callback = the delivery router's send for a `(platform, chat, thread)` target.
- Memory unknown → 2 workers (reference: no cap).

Compare checklist (reopened the files above):
- [x] exit 0 while running = violation, three → blocked (`tests/unit/board/test_dispatcher.py::test_silent_end_counts_a_violation_and_three_block`)
- [x] exit 75 cools 300 s, no failure (`::test_exit_75_cools_the_lane_without_a_failure`)
- [x] exit 78 blocks as capability (`::test_exit_78_blocks_as_capability`)
- [x] crash failure limit 2 (`::test_two_crashes_block_the_task`)
- [x] concurrency clamp 512 MB, 2..8 (`::test_concurrency_clamp_table`)
- [x] runtime cap + 30 s grace; reap grace 120 s (`::test_max_runtime_terminates_after_the_grace`, `::test_worker_outliving_its_run_is_reaped`)
- [x] log rotation 2 MiB × 1 (`::test_worker_log_rotates_at_2_mib`)
- [x] child env from the builder, no provider key (`::test_worker_env_has_pins_and_no_provider_key`)
- [x] notifier cursor, kept on failure, dropped after 12 failures or on archive; hourly GC (`::test_notifier_posts_outcomes_once`, `::test_notifier_keeps_the_cursor_on_failure_and_drops_after_the_limit`, `::test_notifier_unsubscribes_archived_tasks`, `::test_gc_forgets_old_workers_and_files`)
- [x] real child processes: completion with a handoff, exits 0/75/78 (`tests/integration/board/test_worker_lane.py::test_a_worker_completes_its_task_with_a_handoff`, `::test_exit_codes_of_real_workers`)
- Deliberate differences: no respawn guard (recent success / PR URL heuristics), no auto-decompose,
  no per-profile in-progress cap, no singleton dispatcher lock file (one daemon per root serves the
  board), memory pressure levels left out.
- Not taken: Windows handle registry for exit codes (we keep the `Popen` objects we started); the
  exit-code trailer in the worker log: a headless `indus kanban dispatch` cannot read the exit code
  of a worker an earlier pass started, so such a worker that dies with the task still running is
  booked as a crash (follow-up: have the worker write its exit code next to its log).

## Integration (P24 integrator, 2026-10-21)

Wired: `gateway/board_part.py` (`BoardPart`, BACKGROUND) runs one `BoardWatchers` loop per existing
board when `kanban.dispatch_in_gateway` is on, at `kanban.dispatch_interval_seconds`, with notices
through the daemon's delivery router (D-16.789). `kanban_create`'s `delivery_origin` is still not
handed in by the loop (the cron tool has the same gap).

## Wiring check (wave 42, builder WA, 2026-10-21)

Read: no reference file. The integrator's `BoardPart` is in `PART_FACTORIES` and now in
`ROOT_PART_FACTORIES` (a multiplexing host runs it once, never in each served profile). End to end
through a real daemon: `tests/integration/gateway/test_board_in_daemon.py::test_a_task_outcome_reaches_its_subscribed_chat_through_the_daemon`.

## Status

Done (2026-10-21), wired.
