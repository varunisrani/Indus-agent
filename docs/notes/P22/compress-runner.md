# Task: Compression runner (P22-13) Phase: 22 Owner: wave-21 builder R2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `trajectory_compressor.py`
(`AggregateMetrics`, `TrajectoryMetrics`, the directory / file processing and `main`). Research 10
§3.5 and trap R10.8. Closed before writing.

What it does (own words): `indus research compress INPUT` runs the compressor over a JSONL file
or a folder of them, many entries at once under a concurrency cap and a per-entry deadline, keeps
input order, writes aggregate metrics, and puts every timed-out entry in a rejects file instead of
silently dropping it.

Modes:
- file: `x.jsonl` → `x_compressed.jsonl` (or `--output`), metrics `x_compressed_metrics.json`
  (`<output stem>_metrics.json`), rejects `x_compressed.rejects.jsonl`;
- folder: every `*.jsonl` of `DIR` → `DIR_compressed/` (or `--output`) with the same names,
  metrics `DIR_compressed/<metrics.file>`, rejects `DIR_compressed.rejects.jsonl` beside the
  folder (so a later run over the output folder never reads it as data).
`--sample-percent P` (0 < P ≤ 100, else exit 2) keeps `max(1, floor(n·P/100))` entries per file,
drawn with `--seed` (42). `--dry-run` reads and samples, prints what it would do, writes nothing
and loads no tokenizer.

Per entry: under a semaphore of `max_concurrent_requests`, inside `asyncio.wait_for(…,
per_trajectory_timeout)`; an entry without a `conversations` list passes through unchanged;
timeout → rejects line `{source_file, line, reason: "timeout", entry}` and counted
(`trajectories_timed_out` and failed), not written to the output; any other exception keeps the
original entry and counts it failed. Compressed entries get `compression_metrics` when
`metrics.per_trajectory` is on. The rejects file is always written (empty when nothing timed
out). The summariser is created inside the running event loop.

Metrics JSON: `summary {total, compressed, skipped_under_target, still_over_limit, failed,
timed_out, compression_rate}`, `tokens {before, after, saved, ratio}`, `turns {before, after,
removed}`, `averages {compression_ratio, tokens_saved, turns_removed}` (compressed only),
`summarisation {calls, errors, success_rate}`, `tokenizer {fallbacks}`, `timings {started,
finished, seconds}` (UTC ISO).

Exit codes: 0 done; 1 runtime failure (unwritable output); 2 usage (missing input, bad percent,
bad config, tokenizer cannot load).

Files: `src/indus/research/compress_runner.py`, `tests/integration/research/test_compress_run.py`;
shared: `src/indus/cli/commands/research.py` (the `compress` verb replaces its stub).

Our design differences / decisions: rejects file (R10.8, D-16.31); folder sampling without a temp
copy; output written directly (no temp dir); our metrics field names; a short plain report
instead of the boxed one.

Tests we will write: fake summariser + fake tokenizer: a hanging entry lands in rejects and the
rest finish; order preserved; sampling count formula (table); dry run writes no file; metrics
fields present and consistent (before − after = saved); folder mode names; bad percent exits 2;
CLI end to end (M8 demo: over-limit sample under target, metrics file, empty rejects file).

## Decisions for the lead
- Rejects path: `<output without .jsonl>.rejects.jsonl` (file mode), `<output dir>.rejects.jsonl`
  (folder mode).

Compare checklist (filled after the rewrite; reopened the metrics classes, directory/file
processing and `main`):
- [x] semaphore of `max_concurrent_requests`, per-entry deadline, other exceptions keep the
  original and count failed, output order per file (test:
  `tests/integration/research/test_compress_run.py::test_file_mode_hanging_entry_goes_to_rejects_and_order_is_kept`)
- [x] `max(1, floor(n · p / 100))`, seed 42, 0 < p ≤ 100 (tests: `test_sampling_count_formula`,
  `test_bad_percent_exits_2`)
- [x] file mode `<stem>_compressed.jsonl` + `_metrics.json`; folder mode `<dir>_compressed/`
  (tests: `test_over_limit_sample_lands_under_target_with_an_empty_rejects_file`, `test_folder_mode_names`)
- [x] dry run writes nothing (test: `test_dry_run_writes_nothing_and_loads_no_tokenizer`)
- [x] metrics fields present and consistent (summary/tokens/turns/averages/summarisation/timings)
- Deliberate differences: timed-out entries go to the rejects file (R10.8, D-16.31); sampled
  entries keep input order (the reference writes them in sampled order); no temp-dir round trip;
  `skip_under_target: false` leaves under-target entries out; exit 2 for usage errors (the
  reference prints and returns 0); a short plain report instead of the boxed one; metrics also
  count timeouts, passed-through entries and tokenizer fallbacks.
- Not taken: the progress bar.

## Status

Done (2026-10-19). Shared: `src/indus/cli/commands/research.py` (`compress` verb, seams
`tokenizer_loader`, `summariser_for`), `tests/contract/cli/non_tty_table.yaml` (`research
compress|swe|sample: 2`), `tests/unit/cli/commands/test_research.py` (the "not yet" test replaced).
