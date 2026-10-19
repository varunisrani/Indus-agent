# Task: Single-tool SWE runner (P22-14) Phase: 22 Owner: wave-21 builder R2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `mini_swe_runner.py` (all).
Research 10 §3.4 and trap R10.7. Closed before writing.

What it does (own words): a minimal coding loop for data generation: the model gets one tool,
`terminal`, and a task; it runs commands in a fresh environment (local, Docker or Modal) until it
answers without a tool call, prints our completion sentinel, or runs out of iterations. Each task
becomes one line of the same trajectory format the batch runner writes (the shared converter).

Flow per task: environment through the backend registry with per-task overrides (task id
`swe_<n>`, `{backend, image, cwd}`; local cwd = a fresh temp dir removed afterwards, container cwd
`/tmp`); up to `--max-iterations` (15) model calls, each one Chat Completions request through the
provider layer with a 300 s deadline, our system prompt and only the `terminal` tool; no tool
calls → completed; each call's `command` runs with its own `timeout` or `--command-timeout` (60 s);
a call without a command gets an error result and runs nothing; output containing
`INDUS_SWE_TASK_COMPLETE` stops the loop as completed (after the batch's other calls); a model
error ends the loop not completed; the environment is released in `finally`.

Output line: `{conversations, completed, api_calls, metadata {model, env, timestamp}}`; a task
that raised → `{conversations: [], completed: false, api_calls: 0, error, metadata}`. Lines are
appended (never truncated) with the saver's locked append, after the P05 redactor.

Per-task images: P21-14 (slice B3, in parallel) owns the per-task override registry; the runner
takes an `OverrideRegistrar` seam (`register(scope, task_id, overrides) -> overrides`, `clear`),
whose default passes the overrides straight to the registry (Modal and local read `image`/`cwd`
from them already). Tests use a stand-in (`tests/unit/research/_stubs_r2.py`).

Inputs → outputs: `load_prompts(path)` (JSONL `{prompt}` / `{task}` or raw text lines);
`SweSettings`; `SweRunner(scope, settings, *, chat, environments)`; `run_task(task) -> dict`;
`run_all(tasks, output) -> list[dict]`.

Files: `src/indus/research/swe_runner.py`, `tests/unit/research/test_swe_runner.py`; shared:
`src/indus/cli/commands/research.py` (the `swe` verb).

Our design differences / decisions: the shared converter (gpt turns always start with a think
block, same as batch: R10.7); append mode for every mode (R10.7); our sentinel and prompts; no
API key flag (keys come from the profile); the provider layer instead of a raw client; secrets
masked before writing; a missing command runs nothing.

Tests we will write: fake model + fake environment: sentinel stops the loop; missing `command`
handled; no tool calls → completed; max iterations → not completed; output equals the converter's
format for the same messages (golden fixture); file appended not truncated; exception → error
line; environment released and temp dir removed; per-task overrides registered and cleared.

## Decisions for the lead
- Sentinel `INDUS_SWE_TASK_COMPLETE`; default output `swe-trajectories.jsonl` in the working
  directory; default image is the profile's `terminal.docker_image` (no hard-coded image).
- When P21-14 lands, plug its registry into `swe_runner.OverrideRegistrar` (as for
  `worker.ImagePrep.register`). Done at the landing: `RegistryEnvironments.open` registers
  `TaskOverride(image, backend, cwd)` in `backends.task_overrides.TASK_OVERRIDES` under the task id,
  commands go to the registry with the task id and no call overrides (the registry plans the image
  per backend), and `close` releases the task's environment, then clears the override and removes
  the temp folder (in `finally`). The `OverrideRegistrar` / `PassThroughOverrides` seam is gone.

Compare checklist (filled after the rewrite; reopened `mini_swe_runner.py`):
- [x] one environment per task, local cwd = temp dir, container cwd `/tmp`, cleaned in `finally`
  (tests: `tests/unit/research/test_swe_runner.py::test_local_run_completes_on_the_sentinel`,
  `test_registry_environments_use_per_task_overrides_and_release`)
- [x] max iterations 15, request deadline 300 s, command timeout 60 s or the call's own
  (tests: `test_sentinel_stops_the_loop`, `test_provider_chat_sends_one_request_with_the_terminal_tool`)
- [x] no tool calls → completed; sentinel → completed; API error → not completed (tests:
  `test_sentinel_stops_the_loop`, `test_max_iterations_and_model_errors_end_not_completed`)
- [x] result shape `{content: {output, exit_code, error}}`; prompts file `{prompt}` / `{task}` /
  raw lines (tests: `test_output_is_the_shared_converters_format`, `test_load_prompts`)
- [x] exception per task → `{conversations: [], completed: false, error}` line
  (test: `test_file_is_appended_and_a_failed_task_leaves_an_error_line`)
- Deliberate differences: the shared converter (every gpt turn starts with a think block; R10.7);
  append mode everywhere (R10.7); a missing command runs nothing (the reference runs an echo) and a
  call to another tool name is refused instead of run as a command; our sentinel, prompts and tool
  description; the provider layer instead of raw clients and no key flag; no hard-coded image;
  secrets masked before writing.
- Not taken: the per-model temperature contracts (the provider layer owns request shaping).

## Status

Done (2026-10-19). The P21-14 stand-in (`tests/unit/research/_stubs_r2.py`) was removed at the
landing; the tests run on the real override registry. Landing fix: the `error` field of a failed
task's line goes through the P05 redactor in force mode like the turns
(`test_a_failed_tasks_error_line_is_masked`).
Shared: `src/indus/cli/commands/research.py` (`swe` verb, seams `swe_chat`, `swe_environments`).
