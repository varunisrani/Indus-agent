# Task: Parallel planner + executor (P04-14) Phase: 04 Owner: wave-6a builder X

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/tool_dispatch_helpers.py`
(the batch planner, admission, path canonicalising and overlap), `agent/tool_executor.py`
(worker caps, deadlines, the start-order gate, the authorization gate's human-wait exclusion, the
concurrent batch wait loop, unfinished-slot results, the sequential runner and its deadline, the
segmented dispatcher, the activity heartbeat). Spec: research `01-agent-core.md` §3.7, trap R01.14.
Closed before writing.
Evidence: none.

What it does (own words): a batch of tool calls is cut into ordered segments. Each call is either
a **barrier** (interactive tools such as `clarify`, arguments that are not a JSON object, tools
whose own deadline is longer than the batch deadline, and anything not known to be safe) or
**parallel-admissible** (read-only tools, and path-scoped file tools). Path-scoped readers may
overlap each other; a writer conflicts with any overlapping path (either direction, prefix of the
canonical path parts), and a conflict closes the current run so the call starts a new one after
it. Runs shorter than two calls become sequential, neighbouring sequential pieces merge, and no
call ever moves across a barrier, so side-effect order equals fully sequential execution.

The executor runs a parallel segment as asyncio tasks, at most 8 at once (4 when the batch has an
image-generation call; config can lower it), under one batch deadline (420 s). Workers begin in
call order: each waits for the previous one to have started, but never longer than the start-order
gate (120 s, or half the batch deadline when that is shorter) so a wedged worker cannot starve the
rest. Time a human spends answering an approval prompt is added back to every deadline (read from
the server-request broker's cumulative wait counter). Sequential calls run one at a time with their
own deadline (420 s by default, configurable). A slot that never finished gets a synthesized
result: `timeout` (deadline), `cancelled` (it was running when the turn was interrupted) or
`skipped` (it never started). Abandoned work is cancelled and never awaited again (a sync handler's
thread is left behind, not joined). While tools run, a heartbeat stamps the turn's activity time
every 30 s.

Inputs → outputs: calls (name + parsed args or a parse failure) and a `run_one(index)` coroutine
factory → one `SlotOutcome` per call (result, status, duration, started flag), in call order.

Files: `src/indus/core/tool_plan.py`, `src/indus/core/tool_exec.py`,
`tests/unit/core/test_tool_plan.py`, `tests/unit/core/test_tool_exec.py`; shared:
`src/indus/tools/registry.py` (a `ToolTraits` value on `ToolEntry` and `register(traits=...)`).

Edge cases / failure modes:
- a single call → always one sequential segment.
- `search_files` with no path reserves the working directory.
- relative paths resolve against the turn's working directory (not the process cwd).
- a tool with no registry entry → barrier (unknown tools get their error from dispatch).
- deadline hit while workers are parked at the gate → they are released and do not start.
- interrupt arrives between two sequential calls → the rest are `skipped`.
- the approval broker's wait counter missing → nothing excluded.

Our design differences / decisions:
- asyncio tasks instead of a thread pool; the start-order gate and deadlines run on the injected
  clock, so tests use a fake clock.
- Safety comes from registry traits (`read_only`, `path_arg`, `writer`, `interactive`, `mutating`)
  with a small built-in table for core tool names until P05 registers real tools.
- Not taken: tool-search bridge peeling, MCP per-server parallel opt-in (P10 adds a trait),
  V4A patch header parsing (P05 `patch` can set its own path list later), terminal approval
  batching, daemon thread pool.

Tests we will write: planner table (overlapping writer/reader, reader/reader overlap, barrier in
the middle, run of 1, unparseable args, extended deadline, unknown tool); executor with fake tools
and a fake clock: 9 parallel calls never exceed 8 concurrent; image cap 4; deadline → synthesized
timeout results; a 300 s fake human wait does not trip the 420 s deadline; start order; interrupt
→ running cancelled, unstarted skipped; sequential deadline; heartbeat stamps.

## Decisions for the lead
- `ToolTraits` added to `tools/registry.py` (shared, additive): P05 sets traits when it registers
  real tools; until then `tool_plan.BUILTIN_TRAITS` covers the core names.
- Limits are `ExecLimits` fields (no config keys yet); config keys can be added with P05.

Compare checklist (filled after the rewrite; reopened `agent/tool_dispatch_helpers.py` (planner,
admission, overlap) and `agent/tool_executor.py` (limits, gates, batch wait loop, unfinished slots,
sequential runner, segmented dispatch)):
- [x] barrier rules and admission (tests: `tests/unit/core/test_tool_plan.py::test_barrier_kinds`,
  `::test_segment_table`)
- [x] overlap rule, writer vs any, reader vs reader allowed (test: `::test_segment_table`,
  `::test_default_path_reserves_the_working_directory`)
- [x] run of 1 demoted; neighbours merged; order kept (tests: `::test_segment_table`,
  `::test_order_is_never_changed`)
- [x] 8 workers / image 4 / 420 s / gate ≤ half the deadline (tests:
  `tests/unit/core/test_tool_exec.py::test_nine_parallel_calls_never_exceed_eight`,
  `::test_image_generation_caps_at_four`, `::test_batch_deadline_synthesizes_timeouts`,
  `::test_start_gate_releases_a_parked_worker_after_its_bound`)
- [x] human-wait exclusion (test: `::test_human_wait_is_not_counted_against_the_deadline`)
- [x] synthesized results for timeout / interrupt / never-started / crashed worker (tests:
  `::test_stop_cancels_running_and_skips_unstarted`, `::test_worker_crash_becomes_an_error_result`)
- [x] sequential deadline; exempt for delegation and interactive tools; a tool's own longer
  deadline wins (test: `::test_sequential_call_deadline`; exemptions in `tool_round._execute`)
- [x] heartbeat while tools run (test: `::test_heartbeat_touches_while_tools_run`)
- Deliberate differences: asyncio tasks on the injected clock instead of a daemon thread pool; a
  slot unfinished at the deadline is `timeout` whether or not it started; traits come from the
  registry (`ToolTraits`) with a small built-in table.
- Not taken: tool-search bridge peeling, MCP per-server opt-in, V4A patch header paths, terminal
  approval batching, the authorization serialization lock (prompts are serialized by the broker,
  P04-21/P05).

## Status

Done (2026-10-18). Files: `src/indus/core/tool_plan.py`, `src/indus/core/tool_exec.py`,
`tests/unit/core/test_tool_plan.py`, `tests/unit/core/test_tool_exec.py`; shared:
`src/indus/tools/registry.py` (`ToolTraits`, `NO_TRAITS`, `ToolEntry.traits`, `register(traits=)`).
