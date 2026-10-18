# Task: `read_file` with guards and loop breaker (P05-16) Phase: 05 Owner: wave-2 builder A

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/file_tools.py` (read
handler guard order, char budget, device blocklist and symlink hops, special-file kinds, dedup
stub/block, record-successful-read, schemas), `tools/file_tools_read_tracking.py` (whole: tracker
containers and caps, not-found cache, consecutive counter, compaction reset, baselines, page
coverage), `tools/binary_extensions.py` (whole), `agent/file_safety.py` (read denylist and NT
namespace prefixes only). Closed before writing.
Evidence: none.

What it does (own words): reads a text file page by page with `LINE|CONTENT` numbering and
refuses anything that would hang, leak or loop. Guards run in a fixed order: Windows NT/device
prefixes on the raw string → device blocklist (`/dev/zero`, random, stdin/tty/console, stdio and
`/dev/fd/0-2`, `/proc/<pid>/{environ,cmdline,maps,smaps,…}`) checked on the literal path and on
every symlink hop → FIFO/socket/device refused on the host → internal denylist (Indus secret
stores, auth/config control files, `.env*` (not `.env.example`-style), `.envrc`) → document
extraction (P05-17) → binary refusal pointing to vision → not-found remembered for 60 s with
similar-name suggestions → dedup → real read. A page is cut to the char budget
(`file_read_max_chars`, 100,000) on a line boundary with `next_offset`; long lines are clamped;
UTF-16 is transcoded with a note; files over 10 MB are refused; big files read wide get a "read a
section" hint; merge-conflict blocks are counted. Output is redacted (secret files get the
`NAME=value` pass).

Loop breaker (R02.15): the same (path, offset, limit) of an unchanged file returns a small
"unchanged" stub; the second stub in a row becomes a refusal; identical reads in a row (file
changing or not) warn at 3 and are refused at 4; the first read after a compaction boundary
returns full content again. Refusals and stubs are not errors: they carry `harness_refusal: true`
so the failure classifiers (P04-18 guardrails count `{"error": …}` results) do not escalate
them. A task that has seen every line of the current file unredacted (one page or contiguous
pages) gets a write baseline (P05-18).

Inputs → outputs: `{path, offset?, limit?}` → JSON `{content, total_lines, file_size, truncated,
next_offset?, truncated_by?, hint?, encoding?, conflict_blocks?, warning?}`; stub
`{status: "unchanged", …}`; refusal `{status: "blocked", harness_refusal: true, message, path}`;
errors `{"error": …}` (not found adds `similar_files`). Config `file_read_max_chars`.

Files: `src/indus/tools/builtin/read_file.py`, `src/indus/tools/builtin/read_tracking.py`,
`tests/unit/tools/builtin/test_read_file.py`; shared: `src/indus/tools/builtin/__init__.py`
(module list line).

Edge cases / failure modes:
- offset/limit as strings, zero, negative, over 2,000 → coerced and clamped.
- symlink chain into `/proc/self/environ` refused; `/dev/stdin` refused before resolution.
- a missing file then created by another tool: the not-found cache is checked against the disk
  before it answers.
- empty file → `content: ""`, `total_lines: 0`.
- redacted page → never a baseline.
- tracker caps: 500 history, 1,000 dedup/baselines, 256 coverage ranges per file.

Our design differences / decisions:
- One tracker per (profile home, task) (R02.8), held in-process; versions are sha256 of the bytes
  on both the host and shell paths (we read whole files ≤10 MB anyway).
- Refusal marker key `harness_refusal` (our name).
- `note_other_tool(scope, task_id)` resets the consecutive counter, stub hits and not-found
  cache; `compaction_boundary(scope, task_id)` is P08's hook.

Tests we will write:
- each guard in order (device, symlink hop, FIFO, denylist, binary, size, not-found + similar),
  paging and `next_offset`, char budget cut, UTF-16, conflict count, redaction, stub → refusal,
  warn at 3 / block at 4, compaction resets, baseline only on full unredacted reads (and by
  contiguous pages), register_tools schema.

## Decisions for the lead
- `file_read_max_chars` is read from config with a 100,000 fallback; the key belongs in the
  config schema (P05-04/08 own `schema.py`).
- The dispatcher should call `read_tracking.note_other_tool(scope, task_id)` after every tool
  other than `read_file`/`search_files` (e.g. as a `ToolHooks.after`); P08 calls
  `compaction_boundary` after compression.
- `config.yaml` of the profile is read-denied (spec: "auth/config control files"); easy to drop.

Compare checklist (filled after the rewrite; reopened `tools/file_tools.py`,
`tools/file_tools_read_tracking.py`):
- [x] Guard order and each guard (tests: `tests/unit/tools/builtin/test_read_file.py::test_device_files_and_symlinks_to_them_are_refused`,
  `::test_nt_namespace_refused`, `::test_fifo_is_refused_without_opening`,
  `::test_secret_files_are_refused`, `::test_binary_files_are_refused`,
  `::test_files_over_ten_megabytes_are_refused`, `::test_not_found_suggests_and_is_remembered_until_created`)
- [x] Paging, char budget on a line boundary, long-line cut, UTF-16, conflicts, redaction
  (`::test_numbered_lines_and_paging`, `::test_char_budget_cuts_on_a_line_boundary`,
  `::test_long_lines_are_cut`, `::test_utf16_is_transcoded_with_a_note`,
  `::test_merge_conflict_blocks_are_counted`, `::test_secrets_in_content_are_masked_and_give_no_baseline`)
- [x] Stub → refusal, warn 3 / block 4, other tool resets, compaction serves once, refusals are not
  errors (`::test_repeat_read_gets_a_stub_then_a_refusal`, `::test_identical_reads_warn_at_three_and_block_at_four`,
  `::test_another_tool_resets_the_consecutive_count`, `::test_compaction_boundary_serves_full_content_once`)
- [x] Baselines: full unredacted read or contiguous pages of one version only
  (`::test_full_read_records_a_baseline_and_partial_does_not`, `::test_contiguous_pages_make_a_baseline`,
  `::test_pages_of_different_versions_do_not_combine`)
- Deliberate differences: versions are sha256 of the bytes on every backend (the reference stats
  host files and has no dedup on remote backends); the dedup check happens after the bytes are
  read. Missing-path answers are re-checked against the disk before being served (same as the
  reference). Extracted documents give a baseline only for notebooks (other formats are refused
  by the write guard anyway).
- Not taken: "file was modified since you last read it" warnings on later edits (the stale-write
  rule covers `write_file`); hosted OCR wording.

## Status

Done (2026-10-18). Shared: `src/indus/tools/builtin/__init__.py` (three module lines).

## Integration (lead)

Merged in the P05 wave 2 integration (2026-10-18, lead's verify tree).
- `file_read_max_chars` (100000) added to the config schema (top level).
- Dispatcher follow-ups recorded, not wired: call `read_tracking.note_other_tool(scope, task_id)`
  after every tool other than `read_file`/`search_files` (a `ToolHooks.after` in the host, P06);
  P08 calls `read_tracking.compaction_boundary` after compression.
