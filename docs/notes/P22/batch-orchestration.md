# Task: Batch orchestration (P22-08) Phase: 22 Owner: wave-19 builder R1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `batch_runner.py` (runner init,
worker config, the pool loop, combining batch files, the summary, `run`),
`tests/test_batch_runner_checkpoint.py`. Research 10 §3.1 steps 1–11, traps R10.1, R10.4, R10.5.
Closed before writing.

What it does (own words): the parent of a batch run. It loads the dataset, binds the checkpoint,
works out what is left, prints the effective config (every value, defaults resolved from one
table, the model actually used), runs the batches on a spawn process pool, records each finished
batch in the checkpoint, and at the end merges every batch file into `trajectories.jsonl` and
writes `statistics.json`.

Inputs → outputs: `BatchSettings` (dataset, run name, batch size, distribution, model, base URL,
max turns, workers, resume, new dataset, seed, reasoning effort, prefill, ephemeral prompt,
routing, max samples, verbose, runs root) → exit code (0 done, 1 runtime failure / worker crash,
2 usage / dataset / checkpoint refusal, 130 interrupted). Files under `<runs root>/<run name>/`:
`checkpoint.json`, `batch_<N>.jsonl`, `trajectories.jsonl`, `statistics.json`.

Flow: defaults (`DEFAULTS` table) → dataset (`DatasetError` → 2) → distribution (unknown → 2) →
route resolved once here for the effective config (failure → 1) → run dir → checkpoint
(`DatasetChanged` → 2 unless `--new-dataset`) → remaining = not in checkpoint, minus the content
scan when `--resume` → chunks of `batch_size` → pool of `workers` (spawn; workers ignore SIGINT;
initializer rebuilds the scope) → per finished batch: merge indices, `batch_stats`, atomic
checkpoint (write errors warn) → after the pool: final checkpoint, merge, statistics, summary
table (tools by call count, then reasoning coverage and discards). Ctrl-C → terminate + join the
pool, final checkpoint, 130; a worker crash or a worker exception → same, 1.

Merge: every `batch_*.jsonl` (sorted by N) → `trajectories.jsonl`, dropping tombstones, bad lines,
non-objects and rows whose `tool_stats` names a tool not in the registry (corrupt sample, R10.5).
Statistics: per-tool count/success/failure with `success_rate` / `failure_rate` in percent to 2 dp
(0.0 without calls), reasoning totals, discarded count, kept/dropped counts, run settings.

Files: `src/indus/research/batch.py`, `tests/integration/research/test_batch_run.py`,
`tests/fakes/research/routed_model.py` (loopback fake model answering per conversation), config row
`research.runs_root` (shared: `foundation/config/schema.py` + its test).

Our design differences / decisions: runs root `./research-runs` (config `research.runs_root`)
instead of `data/`; `concurrent.futures.ProcessPoolExecutor` with the spawn context (a dead worker
surfaces as a broken pool instead of a hang); no progress bar, one line per finished batch;
credentials never cross the process boundary as callables — workers resolve the route from the
scope; the effective config prints "provider default" when no reasoning flag was given (R10.4).

Tests we will write: fake model over loopback, 100 prompts, 4 workers: kill the parent's process
group after exactly 37 durable rows (the fake stalls new conversations after 37), rerun with
`--resume` → exactly 63 more conversations, none repeated; merged file = 100 − tombstones − the
hallucinated-tool row; effective config names the model used; changed dataset refused (exit 2);
unit-level orchestration with an in-process executor (checkpoint after each batch, merge rules,
statistics rounding, interrupt → 130 with the pool terminated).

## Decisions for the lead
- Batch numbers restart at 0 on each run (appends to the same files, like the reference).
- With no `--resume`, only checkpoint indices are skipped (hash-bound); `--resume` adds the
  content scan. Mid-batch rows of a killed run are only seen with `--resume`.
- Workers get the parent's env view as a plain dict through the pool's pipe (never argv, never
  disk) and rebuild the scope with `bootstrap.attach_profile_state`, so credentials come from the
  same profile; the route is resolved again in each worker with the model the parent printed.
- Prompts found by the `--resume` content scan are added to the checkpoint's completed indices.

Compare checklist (filled after the rewrite; reopened init, worker config, pool loop, combine,
summary, `run`, and the checkpoint tests):
- [x] row → fsync → mark → checkpoint; the checkpoint written only by the parent (tests:
  `tests/unit/research/test_batch.py::test_a_run_writes_rows_checkpoint_merge_and_statistics`, the integration test)
- [x] every discard leaves a tombstone, the merge drops it (test: `test_merge_drops_tombstones_bad_lines_and_corrupt_rows`)
- [x] corrupt rows (tool not in the registry) dropped at merge (same test; integration test)
- [x] Ctrl-C / worker failure terminate the pool (tests: `test_interrupt_terminates_the_pool_and_saves_the_checkpoint`,
  `test_a_failing_worker_ends_the_run_with_1`)
- [x] kill and resume pays once (test: `tests/integration/research/test_batch_run.py::test_killed_run_resumes_without_paying_twice`:
  37 durable rows at SIGKILL of the process group, exactly 63 conversations on `--resume`, 95
  merged, 4 tombstones, 1 corrupt; then an edited dataset exits 2)
- [x] effective config names the model used and "provider default" for reasoning (same tests)
- Deliberate differences: index skip is bound to the dataset hash (D-16.31; the reference skips
  indices of any dataset under the run name, R10.3); prompts found by the content scan are also
  recorded in the checkpoint; the merge and statistics are written even when nothing was left to
  run; runs root `./research-runs` (config `research.runs_root`); one line per batch instead of a
  progress bar; statistics count this run's prompts like the reference and add the merge counts.
- Not taken: lowering the root log level during the run (no progress bar to protect).

## Status

Done (2026-10-19). Shared: `src/indus/foundation/config/schema.py` (`research.runs_root`) and
its test.
