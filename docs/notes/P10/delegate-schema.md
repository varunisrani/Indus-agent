# Task: delegate_task schema and input normalisation (P10-01) Phase: 10 Owner: P10 slice G builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/delegate_tool.py` (schema,
limits, input folding), `tools/delegate_tool_config.py` (defaults, stripped fields),
`tools/delegate_tool_tasks.py` (task list rules). Closed before writing.
Evidence: none.

What it does (own words): the model-facing `delegate_task` tool. One call either spawns subagents
(`tasks: [{goal, context?, output_schema?, images?, group?}]`) or controls running background
ones (`action: list | steer | stop`, with `subagent_id` and `message`). Whatever the model sends is
normalised into one request object before anything runs: an old single `goal`/`context` becomes a
one-entry task list, a per-task `role` and a top-level `background` are accepted and ignored,
fields that only trusted config may set (toolsets, model pin, worktree, provider routing,
iteration cap) are dropped, and broken input becomes a short model-facing error. The schema's
description is rebuilt per session so it names the live concurrency cap. The tool is inline: the
registry entry only carries the schema; a direct registry dispatch gets the "run by the agent
loop" error. Defaults (D-16.24): 10 children at once, depth 1, 250 iterations per child, 3 async
children, summary cap 24,000 chars (floor 2,000), one-shot runs at most 2 children in total.

Inputs → outputs: tool arguments → `DelegateRequest` (action, tasks, subagent_id, message,
ignored-field notes) or `DelegateInputError`; `delegation.*` config → `DelegationLimits`.

Files: `src/indus/tools/builtin/delegate.py`, `tests/unit/tools/builtin/test_delegate.py`;
shared: `src/indus/tools/builtin/__init__.py` (module list), `src/indus/tools/toolsets.py`
(`delegation` set, core membership), `src/indus/foundation/config/schema.py` (+ test),
`src/indus/core/turn_state.py` (`LoopOptions.max_child_calls` from the cap).

Edge cases / failure modes:
- `tasks` sent as a JSON string → parsed; unparsable → error. `tasks: []` with a `goal` → legacy.
- A task that is not an object, or has an empty goal → error naming its index.
- More tasks than the cap → error naming the cap and the config key.
- `images`: a bare string becomes a list; more than 8 → error; empty entries → error.
- `output_schema` as a JSON string → parsed; not an object → error (meta-check in P10-04).
- Unknown action → error listing the four; steer without message / stop without id → error.

Our design differences / decisions:
- Batch quality gates of the reference (placeholder/template goals) not taken: a short goal is a
  model choice, not an input error.
- `group` is accepted and kept on the task; results are delivered per call (no per-group split).

Tests we will write:
- schema snapshot with the live cap; legacy single goal folded; `role` ignored; trusted fields
  stripped; 9 images rejected; direct registry dispatch → inline-only error; action errors.

## Decisions for the lead
- `delegate_task` joins the shared core list (every platform bundle offers it); the `delegation`
  toolset holds it alone. Goldens regenerate.
- Config section `delegation` (keys: max_concurrent_children, max_depth, max_iterations,
  max_async_children, summary_max_chars, oneshot_max_children, child_toolsets, model,
  inherit_mcp_toolsets, worktree).
- Prompt-surface goldens regenerated (`tests/golden/prompt_surface/*/tools.json`): `delegate_task`
  joins every bundle through the core list, and `process_manage` gains its `purpose` property.
- `LoopOptions.from_scope` now reads `delegation.max_concurrent_children` into `max_child_calls`
  (P04-13's per-batch cap; it was a fixed 3).
- Intended differences: the model-facing argument names are ours; no placeholder/template goal
  gate; `group` kept but results are delivered per call.

Compare checklist (filled after the rewrite; reopened `tools/delegate_tool.py`, `tools/delegate_tool_tasks.py`, `tools/delegate_tool_config.py`):
- [x] defaults 10 / depth 1 / 250 / async 3 / 24,000 / one-shot 2 (test: `tests/unit/tools/builtin/test_delegate.py::test_defaults_follow_d_16_24`)
- [x] live cap in the schema text (test: `tests/unit/tools/builtin/test_delegate.py::test_schema_snapshot_shows_the_live_cap`)
- [x] legacy goal folded, role/background ignored, trusted fields stripped (tests: `::test_legacy_single_goal_is_folded_into_one_task`, `::test_role_and_background_are_accepted_and_ignored`, `::test_trusted_fields_are_stripped_from_the_normalised_task`)
- [x] 8-image cap (test: `::test_image_limits`); inline-only dispatch (test: `::test_direct_registry_dispatch_returns_the_inline_only_error`)
- Deliberate differences: no batch goal-quality gate; the description is our own text.
- Not taken: `independent_completions` / per-group delivery; spawn pause switch (TUI `/agents`, later).

## Status

Done (2026-10-19). Shared: `tools/builtin/__init__.py`, `tools/toolsets.py` (+ `tests/unit/tools/test_toolsets.py`),
`foundation/config/schema.py` (+ test), `core/turn_state.py`, goldens, `tests/unit/cli/setup/test_wizard.py`
(the bundle checklist now has a `delegation` row).
