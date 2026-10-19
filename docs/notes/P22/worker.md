# Task: Per-prompt worker (P22-07) Phase: 22 Owner: wave-19 builder R1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `batch_runner.py` (container image
preparation, single prompt processing, the batch worker). Research 10 §3.1 step 8. Closed before
writing.

What it does (own words): inside a pool process, runs the prompts of one batch one after another.
For each prompt not yet done: get its container image ready (Docker backend only) before any model
call, sample its toolsets, build a fresh in-process agent with everything that would pollute a
training sample switched off, run one conversation, count stats from the raw messages, convert,
always close the agent, and write the row (or a tombstone) durably. Failures write nothing and are
retried by the next run.

Inputs → outputs: `BatchJob{batch, items: [(index, DatasetRow)], skip: frozenset[int]}` and
`WorkerSettings` (picklable: home, profile id, env, run dir, model / provider / base URL,
distribution, seed, max iterations, reasoning effort or None, prefill messages, ephemeral system
prompt, routing, registry tool names, verbose). `run_batch(scope, settings, job, *, runner=,
probe=, register_image=, sampler=)` → `BatchOutcome{batch, processed, skipped, discarded,
failed, completed, tools, reasoning, bad_arguments}`; `process_batch(job)` is the pool entry (scope
rebuilt in the process from the settings set by the pool initializer).

Agent build (`SessionAgentRunner`): a `SessionService` over a `MemoryStore` (no state DB rows), its
own lock dir, owner `batch`; `AgentOptions(platform="batch", source="batch", registry=built-ins
only (no learning/memory tools), builder=the P04 builder with context files off and no persona
from context files, system_message=ephemeral prompt, loop max_iterations / reasoning effort,
session_fields {model, hidden})`; the agent's run context `unattended` (approvals deny by
default) and the process terminal registry; the model caller wrapped so prefill messages go after
the system message and routing goes into `extra_body.provider` **at request time only**. One
session per prompt (`create` → `submit` → `wait`), then `close` + `aclose` in `finally`, plus the
session's terminal environment and kernels released (as the cron host does).

Container images: a row with an image under `terminal.backend: docker` → `docker image inspect`
(10 s) and, when absent, `docker pull` (600 s) before any model call; a failed pull → failure with
the first 500 chars of stderr. Then the per-task override (task id `task_<index>`) goes through the
`register_image` hook. P21-14 is not built: the default hook refuses (failure result "per-task
container images need P21-14"), so image rows fail (zero model calls) until it lands.

Outcome rules: exit `completed` → row with `completed: true`; `max_iterations`,
`budget_exhausted`, `invalid_tool_strikes`, `guardrail_halt` → row with `completed: false,
partial: true`; any other exit (provider failure, interrupted, …) or an exception → failure (no
row). No reasoning on any assistant turn → tombstone `no_reasoning` (counts as complete).

Files: `src/indus/research/worker.py`, `tests/unit/research/test_worker.py`,
`tests/fixtures/model_scripts/research_*.yaml`.

Our design differences / decisions: the agent comes through `SessionService` + `MemoryStore`
(nothing lands in the profile's session store); prefill/routing through a caller wrapper; the worker
stops between prompts when its parent died (orphaned after a kill), so it cannot race a new run.

Tests we will write: fake model + fake probe: pull failure costs zero model calls; agent closed on
success and on exception; no-reasoning run → tombstone; prefill reaches the request but not the
trajectory; ephemeral prompt reaches the system message but not the trajectory; context files never
read (spy on the loader); tool rows counted; row durable before the completion callback.

## Decisions for the lead
- Image rows fail until P21-14 provides per-task overrides (`register_image` hook).
- Partial = the run stopped early but has a usable trajectory (list above).

Compare checklist (filled after the rewrite; reopened image preparation, single prompt, batch worker):
- [x] pull failure before any model call, 500-char preview (test: `tests/unit/research/test_worker.py::test_failed_pull_costs_zero_model_calls`)
- [x] agent always closed (tests: `test_tool_loop_writes_a_durable_row_and_closes_the_agent`,
  `test_agent_is_closed_when_the_run_raises`)
- [x] no reasoning → tombstone, counted complete (test: `test_no_reasoning_run_leaves_a_tombstone`)
- [x] stats before conversion; hallucinated tool keeps its own row (test: `test_hallucinated_tool_is_counted_under_its_own_name`)
- [x] context files never read; prefill and ephemeral prompt reach requests only (test:
  `test_tool_loop_writes_a_durable_row_and_closes_the_agent`)
- [x] failures write nothing (tests: `test_provider_failure_writes_nothing`, `test_agent_is_closed_when_the_run_raises`)
- Deliberate differences: a missing `docker` CLI under the Docker backend is a failure (the
  reference ignores it); image rows fail until P21-14 (refusing hook); a turn that ended with a
  provider failure or an interruption is a failure, not a sample; toolsets come from the prompt's
  own RNG; the worker stops between prompts when its parent is gone.
- Not taken: the log prefix and log preview length (no such options here).

## Status

Done (2026-10-19). For the lead: the `register_image` hook is the P21-14 seam (task id
`task_<index>`, the row with `image`/`cwd`).

Integrated (lead, 2026-10-19): `ImagePrep.register` stays the P21-14 seam (default `refuse_images`);
P21-14's per-task override registry plugs in there (task id `task_<index>`). Until then a row naming
an image fails with no model call; under the Docker backend the image is still probed and pulled
before the refusal.
