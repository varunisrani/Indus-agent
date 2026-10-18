# Task: Result budget + spill (P04-12) Phase: 04 Owner: wave-5 builder D

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/tool_result_storage.py`
(whole), `tools/budget_config.py` (whole), `tools/tool_output_truncate.py` (whole). Test names of
`tests/tools/test_tool_result_storage.py`, `tests/tools/test_budget_config.py` (names, plus its
first 80 lines), `tests/tools/test_tool_output_truncate.py`. Research `02-tools.md` §3.4.
Closed before writing.
Evidence: none.

What it does (own words): keeps one huge tool result, or a turn full of big ones, from filling
the context window. A result over its threshold is saved in full to a private file under the
profile home and replaced by a short block: a tag, the original size, where the file is, a hint
to page it with `read_file` offset/limit (instead of re-running the tool), and a preview. When a
whole turn's results exceed the turn budget, the largest ones are spilled first. Spill files live
as long as the sessions that point at them; a sweep once per home per process deletes old
unreferenced ones. A shared head/tail truncation (40/60 around one marker) serves tools that cut
their own output.

Inputs → outputs:
- `Budget{per_result, per_turn, preview, mcp_per_result, overrides}`; `budget_for_window(tokens)`;
  `Budget.threshold(tool_name, registry_cap=None)` (read_file unlimited, `mcp__` 50,000, registry
  caps clamped to the default).
- `SpillSink` protocol `save(name, text) -> str | None`; `HostSpill(home)` writes
  `<home>/cache/spill/<name>.txt` at 0600 and verifies the byte count.
- `spill_if_needed(text, tool_name, call_id, *, budget, sink, threshold=None) -> str`;
  `enforce_turn_budget(messages, *, budget, sink) -> list[dict]`; `preview(text, limit)`;
  `spill_file_name(call_id)`; `sweep(home, *, retention_days, now, referenced)`;
  `sweep_once(...)`; `store_references(store)`.
- `head_tail(text, limit, head=0.4, label="output") -> str`.

Files: `src/indus/tools/budget.py`, `src/indus/tools/spill.py`, `src/indus/tools/truncate.py`,
`tests/unit/tools/test_budget.py`, `tests/unit/tools/test_spill.py`.

Edge cases / failure modes:
- window unknown / ≤0 → defaults (100,000 / 200,000); window known → clamp(window×4×0.15,
  8,000, 100,000) and clamp(window×4×0.30, 16,000, 200,000).
- call ids with path separators, dots or odd characters → one safe file name (hash suffix when
  changed or too long); a call id can never escape the spill folder.
- preview: text ≤ limit unchanged; else cut at the last newline if it lies past the midpoint,
  else at the limit.
- write fails or size mismatch → inline truncation with a "could not be saved" note.
- turn budget: already-spilled results are not candidates; multimodal lists count text parts
  only and spill only their largest text part; images stay.
- sweep: retention = `sessions.retention_days` in hours, floored at 24 h; unknown reference set
  (`None`) keeps everything; referenced files kept regardless of age; missing folder → 0.

Our design differences / decisions:
- Folder `cache/spill` (row), tag `<spilled-result>`; all wording ours.
- MCP prefix is `mcp__` (our MCP tool naming).
- The sink is an interface: the redactor force mode (P05) and the remote-backend copy (P05/P11)
  plug in as other sinks; `HostSpill` takes an optional `redact` callable, default none.
- `enforce_turn_budget` returns a new list with replaced dicts (inputs not mutated).
- The MCP result-envelope unwrapping (pageable text) belongs with the MCP client (P13); not here.
- References come from the store via `store_references(store)` (reads `content` and
  `api_content` of message rows); callers may pass any callable.

Cross-slice names needed: none.

Tests we will write:
- threshold table at 4 windows (none, 8k, 64k, 200k) plus read_file / mcp / registry cap.
- spill file content == original, mode 0600, block mentions size/path/read_file, preview newline
  rule (both sides of the midpoint), unsafe call id, failed sink → inline note.
- turn budget spills largest first and stops when under; spilled results skipped; multimodal.
- sweep keeps a file referenced by a stored message and deletes an unreferenced 25 h-old one
  (fake clock), floor 24 h, unknown references keep all, once per home.
- head_tail: unchanged under limit; marker names omitted and total; 40/60 split; seeded-random
  table (no hypothesis installed) checking kept length == limit.

## Decisions for the lead
- Default retention when config is unreadable: 90 days (the schema default), floored at 24 h.

Compare checklist (filled after the rewrite; reopened `tools/tool_result_storage.py`,
`tools/budget_config.py`, `tools/tool_output_truncate.py`):
- [x] thresholds: pinned, override, MCP prefix, registered cap clamped, default (test: `test_budget.py::test_read_file_unlimited_even_when_overridden`, `::test_mcp_prefix_tighter_and_capped_by_scaled_default`, `::test_override_wins_over_prefix_and_default`, `::test_registered_cap_clamped_to_per_result`)
- [x] window scaling with floors and caps (test: `::test_threshold_table_by_window`)
- [x] spill file == original, 0600, block contents, preview newline rule (test: `test_spill.py::test_spill_file_content_equals_original`, `::test_block_contents`, `::test_preview_newline_rule`)
- [x] safe file names; no escape from the folder (test: `::test_spill_file_names_are_safe`, `::test_call_id_cannot_escape_the_spill_folder`)
- [x] nothing writable → inline note (test: `::test_nothing_writable_gives_inline_note`, `::test_host_spill_unwritable_folder_returns_none`)
- [x] turn budget largest first, spilled skipped, multimodal largest text part (test: `::test_turn_budget_spills_largest_first`, `::test_turn_budget_keeps_going_until_under`, `::test_already_spilled_results_are_not_candidates`, `::test_multimodal_spills_only_its_largest_text_part`)
- [x] retention floor, referenced kept, unknown keeps all, once per home (R02.16) (test: `::test_retention_hours_floor_and_defaults`, `::test_sweep_keeps_referenced_and_deletes_unreferenced_old`, `::test_sweep_with_unknown_references_keeps_everything`, `::test_sweep_once_per_home`)
- [x] head/tail 40/60 with one marker (test: `test_budget.py::test_marker_names_omitted_and_total`, `::test_seeded_random_kept_length_is_the_limit`)
- [x] gap found: the sweep ran only when a caller remembered to call it — `host_spill_for(scope, referenced)` now sweeps once per home on the first save, pinned by `test_spill.py::test_host_spill_for_sweeps_once_on_first_save`
- Deliberate differences: unreadable or non-finite retention falls back to 90 days (the schema
  default), not 24 h; MCP prefix `mcp__`; the turn budget returns new dicts; folder `cache/spill`
  and all wording ours.
- Not taken: MCP envelope unwrapping (P13), sandbox copy and readability probe (P05/P11 sinks),
  the expired-archive hint for `read_file` (P05 file tools).

## Status

Done (2026-10-18). Files: `src/indus/tools/{budget,spill,truncate}.py`,
`tests/unit/tools/{test_budget,test_spill}.py`. The sweep test uses a real P02 store
(`registry.open_unshared`, `sessions.create`, `messages.append`). Property checks are seeded-random
tables (hypothesis is not installed).
