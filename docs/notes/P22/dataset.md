# Task: Dataset loader and dataset hash (P22-01) Phase: 22 Owner: wave-19 builder R1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `batch_runner.py` (the dataset
loading method of the runner class and the chunking helper). Research 10 §3.1. Closed before
writing.

What it does (own words): a batch run starts from a JSONL file of prompts. Each line that is a JSON
object with a non-empty text `prompt` becomes one row; everything else is skipped with a warning
naming its line, so a few bad lines never stop a long run but are never silent either. The rows that
survive are fingerprinted (the dataset hash) so a checkpoint can tell whether it belongs to this
dataset (P22-05).

Inputs → outputs: `load_dataset(path, max_samples=None)` → `Dataset{path, rows, max_samples, hash,
skipped}`; `DatasetRow{index, prompt, image, cwd, data}`. `warning_lines(skipped)` gives the first
20 warnings and then one "… and N more" line. `dataset_hash(rows, max_samples)`. `chunk(pairs,
size)` splits `(index, row)` pairs into consecutive batches. `DatasetError` for a missing/unreadable
file or an empty result (the CLI exits 2).

Files: `src/indus/research/dataset.py`, `tests/unit/research/test_dataset.py`,
`tests/fixtures/research/datasets/*.jsonl`, `src/indus/research/__init__.py` (marker only).

Edge cases / failure modes:
- blank and whitespace-only lines are skipped quietly; a BOM on the first line is tolerated;
- invalid JSON, a non-object, a missing/empty/non-text `prompt` → warning with the line number;
- `image` or `container_image` (first non-empty text wins) and `cwd` are optional; other keys are
  kept in `data` (and so in the hash) but ignored by the runner;
- `max_samples` keeps the first N kept rows (indices unchanged); `max_samples < 1` is a usage
  error raised by the CLI, not here;
- undecodable bytes → `DatasetError` naming the file.

Our design differences / decisions:
- the hash is SHA-256 over the canonical JSON (sorted keys, no spaces, UTF-8, non-ASCII kept) of
  each kept row in order, one per line, then a final `max_samples=<n|none>` line; reformatting the
  file never changes it, editing or moving a row does, and so does `max_samples` (D-16.31);
- rows keep their index among the kept rows (the index the checkpoint records);
- the reference's `docker_image` key is spelled `container_image` here (phase file).

Tests we will write: warnings with line numbers and the 20 + count cap; image/cwd fields; hash
stable across whitespace and key order, changed by one prompt and by a moved row; `max_samples`
truncation keeps indices and changes the hash; empty and missing files raise `DatasetError`.

## Decisions for the lead
- Index = position among kept rows (not the file line). Change in `load_dataset` if line numbers
  are wanted.

Compare checklist (filled after the rewrite; reopened `batch_runner.py`):
- [x] blank lines skipped, invalid JSON / missing prompt warned and skipped (test:
  `tests/unit/research/test_dataset.py::test_bad_lines_are_warned_with_their_line_numbers`)
- [x] empty result and missing file fatal (test: `test_empty_and_missing_files_are_dataset_errors`)
- [x] `max_samples` keeps the first N (test: `test_max_samples_keeps_the_first_rows_and_changes_the_hash`)
- Deliberate differences: the reference keeps any object with a `prompt` key (even empty or not
  text); we require non-empty text. It prints each warning; we cap at 20 plus a count. It has no
  dataset hash; ours binds the checkpoint (D-16.31).
- Not taken: nothing.

## Status

Done (2026-10-19).
