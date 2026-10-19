# Task: synchronous batch runner (P10-03) Phase: 10 Owner: P10 slice G builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/delegate_tool_dispatch.py`
(skim), `tools/delegate_tool_results.py` (result shape, truncation), `tools/delegate_tool.py`
(heartbeat constants). Closed before writing.
Evidence: none.

What it does (own words): runs a batch of children as asyncio tasks, at most the concurrency cap
at once (the rest wait), each in a copy of the parent's context with the parent's `Scope`
(R01.16). It returns one tool result `{results: [...]}` in task order; each entry has
`subagent_id, goal, status (completed|failed|interrupted|stale), summary` and, when they apply,
`schema_valid`, `errors`, `orphaned_processes`, `worktree`. A summary over the cap keeps its head
and tail with a marker in between. Liveness: every 30 s the runner looks at each child's progress
(model calls, rows, tool phase); 15 quiet cycles (450 s) between tools, or 40 cycles (1,200 s)
stuck inside one tool, interrupts the child and reports it `stale`. No wall-clock timeout. Each
heartbeat also touches the parent turn so its own liveness does not fire. A parent interrupt
reaches every running child (each ends through its own exit funnel, so its transcript stays
well-formed); queued ones never start and report `interrupted`. A child that raises is `failed`;
the others go on. Depth > 0 and one-shot/unattended runs always use this path.

Inputs → outputs: child runs + cap + clock + stop signal → ordered result entries.

Files: `src/indus/core/subagents/batch.py`, `tests/unit/core/subagents/test_batch.py`,
`tests/integration/core/test_delegate_batch.py`.

Edge cases / failure modes:
- Cap 10, 12 tasks → never more than 10 running.
- Interrupt while some are queued → queued ones are reported, never started.
- Summary empty (child produced no text) → status failed with a reason.

Our design differences / decisions:
- No spill file for an over-cap summary; head (70%) and tail (30%) with a marker naming the cut.
- Results are JSON text (one tool result), entries in task order.

Tests we will write:
- 12 tasks cap 10 (probe counter), order kept; one raising → failed; parent interrupt → all
  interrupted with valid alternation; no progress for 450 s → stale; summary truncation marker;
  integration: three file-reading children, three summaries.

Compare checklist (reopened `tools/delegate_tool_results.py`, `tools/delegate_tool.py` constants):
- [x] heartbeat 30 s, stale 15 idle / 40 in-tool cycles (tests: `tests/unit/core/subagents/test_batch.py::test_quiet_child_goes_stale_after_450_s`, `::test_a_child_inside_one_tool_gets_longer`, `::test_progress_resets_the_stale_count`)
- [x] cap and task order (test: `::test_cap_is_respected_and_results_keep_task_order`); failure isolation (`::test_one_child_raising_fails_alone`)
- [x] parent interrupt fan-out, valid alternation (tests: `::test_parent_stop_interrupts_running_and_skips_queued`, `::test_parent_interrupt_reaches_every_child_and_transcripts_stay_valid`)
- [x] summary cap with marker (test: `::test_summary_over_the_cap_keeps_head_and_tail_with_a_marker`)
- [x] usage roll-up `subagent` (test: `::test_child_usage_rolls_up_to_the_parent`); one-shot budget (`::test_one_shot_runs_wait_and_spend_a_total_budget`); depth (`::test_no_deeper_than_max_depth`)
- [x] M2 demo: `tests/integration/core/test_delegate_batch.py::test_three_subagents_read_three_files`
- Deliberate differences: head 70% / tail 30% and no spill file (reference: 75/25 plus a spill
  under `cache/delegation`); no headroom-share budget across the batch.
- Not taken: the stale-result grace window, timeout diagnostics dumps.

## Status

Done (2026-10-19).
