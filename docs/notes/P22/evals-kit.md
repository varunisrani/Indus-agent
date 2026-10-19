# Task: Eval kit and sample-and-compress helper (P22-15) Phase: 22 Owner: wave-21 builder R2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `evals/compaction/runner.py`
(structure only), `evals/toolperf_abeval/ab_eval.py` (arms and resume), `scripts/
sample_and_compress.py` (skim). Research 10 §3.6, §3.7. Closed before writing.

What it does (own words): `indus.research.evals` gives future A/B experiments one shape:
- results store (`results.jsonl`): a cell = arm + task + repetition, id = SHA-256 of the three
  (first 16 hex); completed cells are skipped on a rerun; a startup crash (non-zero exit with
  empty output) is never recorded, so it runs again;
- arms: two arms may differ in exactly one thing — the code tree put on `PYTHONPATH`, or one
  config key; a second difference (or none) is refused;
- oracle scorers: marker string in the final answer, regex over the final answer, file on disk
  (exists, optionally contains a text);
- trace metrics from session store rows: model calls, tool calls, tool errors, retries (a call to
  the tool that just failed), result bytes, wall time;
- a guard that opens a session store only when it is a copy: under the run folder and with an
  inode different from the live store's (read-only);
- figures labelled `observed` or `modelled`; sums never mix the two, and the report prints them in
  separate sections.

`indus research sample`: datasets come from a required list (`--dataset NAME`, repeatable, or
config `research.sample_datasets`); nothing given → exit 2. Rows (`conversations` lists) are
counted with the compressor's tokenizer; rows with ≥ `--min-tokens` (16000) are kept; `--samples`
(2500) are drawn with `--seed` (42) (all when fewer); they are written to a work folder, compressed
with the P22-13 runner (folder mode) and merged into one JSONL `<out>/<name>.jsonl`.

Files: `src/indus/research/evals/__init__.py`, `src/indus/research/evals/kit.py`,
`src/indus/research/sample.py`, `tests/unit/research/test_evals_kit.py`,
`tests/unit/research/test_sample.py`, `docs/user/research/evals.md`; shared:
`src/indus/cli/commands/research.py` (the `sample` verb), `foundation/config/schema.py` + test
(`research.sample_datasets`), `docs/internals/traps-ledger.md` (R10.7–R10.10 rows).

Our design differences / decisions: a library, not ~39 one-off scripts; no default dataset ever
(the reference defaults to its vendor's datasets); the store guard compares inodes, not only
names; observed/modelled is a type, not a convention; dataset loading goes through an injected
loader (Hugging Face `datasets.load_dataset` lazily by default).

Tests we will write: rerun skips done cells and retries a crashed cell; second arm difference
refused (and zero differences); live store path refused (same inode, outside the run dir), copy
accepted; scorers; trace metrics from a hand-built store copy; observed and modelled kept apart
in the report; `sample` without a dataset list exits 2; sample filters by tokens, samples with
the seed and merges the compressed output.

## Decisions for the lead
- Config row `research.sample_datasets: []`.

Compare checklist (filled after the rewrite; reopened `ab_eval.py` run/score/report,
`sample_and_compress.py` sampling, the compaction runner's layout):
- [x] completed cells skipped on rerun; startup crashes not recorded (test:
  `tests/unit/research/test_evals_kit.py::test_rerun_skips_done_cells_and_retries_a_crashed_one`)
- [x] arms differ by one variable (test: `test_arms_vary_exactly_one_thing`)
- [x] oracle scoring (test: `test_oracles`)
- [x] metrics from traces: model calls, tool calls, errors, retries, bytes, wall time (test:
  `test_live_store_is_refused_and_a_copy_is_read`)
- [x] sample: threshold 16000, 2500 samples, seed 42, batch files of 100, compress, merge (tests:
  `tests/unit/research/test_sample.py::test_draw_keeps_long_rows_and_samples_with_the_seed`,
  `test_run_sample_compresses_and_merges`)
- Deliberate differences: cell ids are hashes, not `<task>-r<n>`; a non-zero exit code alone is
  not a tool error (same rule as the batch statistics; the reference's harness counts it); traces
  come from session store rows, not an observability file; the store guard compares inodes;
  observed/modelled is enforced by type; no default dataset (ACC-O7-1; test
  `test_cli_sample_without_a_dataset_list_exits_2`); rows are tagged `sampled_from` /
  `sampled_tokens`; the draw keeps pool order (no final shuffle); token counting runs in-process.
- Not taken: the task battery, ATOF trace parsing, multi-process token counting.

## Status

Done (2026-10-19). Shared: `src/indus/cli/commands/research.py` (`sample` verb, seam
`dataset_loader`), `src/indus/foundation/config/schema.py` + test (`research.sample_datasets`),
`docs/internals/traps-ledger.md` (R10.7–R10.10 rows now point at tests).
