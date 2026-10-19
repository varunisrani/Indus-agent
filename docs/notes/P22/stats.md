# Task: Trajectory stats (P22-04) Phase: 22 Owner: wave-19 builder R1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `batch_runner.py` (the tool-call
success judgement, tool stats, reasoning detection, reasoning stats, the normalisers). Research 10
§3.1. Closed before writing.

What it does (own words): from the raw messages of one run (before conversion) count, per tool,
how often it was called and how often its result looked like a success or a failure, and how many
assistant turns carried reasoning. Rows of a dataset must all have the same stat columns (Arrow /
Parquet export), so the per-row maps list every tool of the run's registry with zeros (R10.5).

Inputs → outputs: `tool_succeeded(content) -> bool`; `tool_tallies(messages) -> dict[name,
Tally{count, success, failure}]`; `normalise(tallies, registered) -> (tool_stats,
tool_error_counts)`; `turn_has_reasoning(message)`; `reasoning_counts(messages) ->
Reasoning{total, with_reasoning, without_reasoning}`; `merge_*` helpers for the totals.

Success judgement: failure only when the content parses as a JSON object with a non-null `error`,
or with a nested object `content` whose `error` is non-null, or with `success: false`. Text that is
not JSON fails only when it is empty or starts with `error:` in any letter case. Exit codes never
count. Non-object JSON (`[]`, numbers) is a success. Parts lists are judged on their text summary.
A result fenced as outside data (P05 untrusted fence) is judged on the fenced body.

Files: `src/indus/research/stats.py`, `tests/unit/research/test_stats.py`.

Edge cases: calls without ids are matched to results by position within the run (same helper as
the converter); results with no matching call are not counted; a tool named only by the model
(not registered) keeps its own row, which the merge uses to drop the sample (R10.5).

Our design differences / decisions: names matched by id first (shared with the converter); the
fence unwrapping; reasoning = non-blank native `reasoning` or a `<think>` tag in the content (the
reference's scratchpad tag is not ours).

Tests we will write: 12-case table of the judgement (incl. `exit_code: 1` success, `"Error: x"`
failure, `[]` success); tallies; normalisation adds zero rows and keeps unexpected names; two rows
with disjoint tools get identical columns; reasoning counts.

## Decisions for the lead
- none.

Compare checklist (filled after the rewrite; reopened the judgement, tallies, reasoning, normalisers):
- [x] judgement table, 12 cases (test: `tests/unit/research/test_stats.py::test_success_judgement`)
- [x] zero rows for every registered tool plus unexpected names (test:
  `test_normalise_adds_zero_rows_and_keeps_unexpected_names`, `test_rows_with_disjoint_tools_get_identical_columns`)
- [x] reasoning counts (test: `test_reasoning_counts`)
- Deliberate differences: results are matched to calls like the converter (by id, then by
  position when ids are missing; the reference counts only id matches); fenced outside data is
  judged on its body; parts lists on their text; reasoning by `<think>` instead of the reference's
  scratchpad tag.
- Not taken: nothing.

## Status

Done (2026-10-19).
