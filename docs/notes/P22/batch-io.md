# Task: Durable batch files and resume scan (P22-06) Phase: 22 Owner: wave-19 builder R1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `batch_runner.py` (the JSONL
append helper, the content scan, the dataset filter, the resume step),
`tests/test_batch_runner_durability.py`, `tests/test_batch_runner_discard_resume.py`. Research 10
§3.1, traps R10.1, R10.2. Closed before writing.

What it does (own words): every finished prompt is one JSON line in `batch_<N>.jsonl`, and the line
is on disk (fsync) before anyone counts the prompt as done (R10.1: row → fsync → mark →
checkpoint). A prompt dropped on purpose leaves a tombstone line so a resumed run does not pay for
it again (R10.2). Resume reads every batch file and skips dataset rows whose prompt text is
already there.

Inputs → outputs:
- `BatchRow{prompt_index, conversations, metadata{batch, timestamp, model, distribution,
  toolsets}, completed, partial, api_calls, tool_stats, tool_error_counts, reasoning}` and
  `Tombstone{prompt_index, discarded: DiscardReason, prompt}`; both `.record()` → dict;
- `append_durable(path, record, fsync=os.fsync)`: append mode, one line, flush, fsync, close; only
  a normal return means durable; if the file does not end with a newline (a torn earlier write) a
  newline goes first so the new line never fuses with the torn one;
- `read_batch_file(path, warn)` yields dict lines; a bad line is skipped with a warning (the last
  line without its newline is reported as a torn write);
- `scan_done_prompts(run_dir, warn)` → trimmed prompt texts of every row and tombstone (flat
  `prompt`, else the first human/user turn of `conversations`/`messages`, `value` or `content`);
- `remaining(pairs, done)` → (left, skipped indices).

Files: `src/indus/research/batch_io.py`, `tests/unit/research/test_batch_io.py`.

Edge cases: unreadable batch file → warning, scan goes on; non-object lines ignored; a line marked
`failed` (not written by us, tolerated) ignored; `DiscardReason` is a `StrEnum` (`no_reasoning`
today) so every new reason gets a tombstone.

Our design differences / decisions: the newline repair before appending; a torn line is warned, not
silently dropped; metadata adds distribution and toolsets (the reference keeps toolsets at the top
level as `toolsets_used`); `reasoning` counts per row.

Tests we will write: fsync before the completion callback (spy order); a crash injected after write
but before fsync leaves the index not completed; tombstoned prompt not re-run on resume; torn last
line skipped with a warning and the next append still lands on its own line; scan reads
conversations and messages shapes.

## Decisions for the lead
- none.

Compare checklist (filled after the rewrite; reopened the append helper, content scan, filter,
resume step and the two durability test files):
- [x] fsync before the completion callback (test: `tests/unit/research/test_batch_io.py::test_fsync_happens_before_the_completion_callback`)
- [x] crash before fsync leaves the index not completed (test: `test_a_crash_before_the_fsync_leaves_the_prompt_not_done`)
- [x] tombstone counts as done for resume (test: `test_tombstoned_prompt_is_not_run_again`)
- [x] torn last line skipped with a warning (test: `test_torn_last_line_is_skipped_with_a_warning_and_next_append_is_clean`)
- [x] rows marked `failed` and non-objects ignored by the scan (test: `test_scan_reads_every_batch_file_and_both_shapes`)
- Deliberate differences: newline repair before an append after a torn write; warnings for bad
  lines; toolsets live in `metadata` (the reference: top-level `toolsets_used`); a `reasoning` field
  per row; batch files are read in numeric order.
- Not taken: nothing.

## Status

Done (2026-10-19).
