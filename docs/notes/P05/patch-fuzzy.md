# Task: `patch` with fuzzy matching (P05-19) Phase: 05 Owner: wave-2 builder A

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/fuzzy_match.py` (whole:
strategy list and order, position mapping, ambiguity listing, already-applied, escape-drift
detection, reindent, did-you-mean), `tools/escape_drift_autocorrect.py` (whole),
`tools/file_operations.py` (`patch_replace`, no-match result, persistence check),
`tools/file_tools.py` (`patch_tool` handler). Closed before writing.
Evidence: none.

What it does (own words): replaces `old_string` with `new_string` in one file. The search tries
nine strategies in order and stops at the first that finds anything: exact, line-trimmed,
whitespace-normalised, indentation-flexible, escape-normalised (literal `\n`/`\t` in the search
text), trimmed-boundary (first/last line trimmed), unicode-normalised (smart quotes, dashes,
ellipsis, NBSP, minus, other Zs spaces), block-anchor (first and last line equal, middle similar),
context-similarity (every line similar). Identical old/new is rejected; several matches without
`replace_all` are rejected with up to five `L<n>: …` locations; similarity strategies never
apply with `replace_all`. An edit that is already in the file (new text present, old text gone)
answers as a no-op success. Escape drift: quotes with a spurious backslash and doubled backslash
runs are corrected only when the corrected search text exists verbatim in the file; otherwise
drift is detected and refused with an explanation. A non-exact match re-indents the replacement
onto the file's indentation. The result carries a unified diff and the strategy name; writes go
through the same guards and baseline rules as `write_file` (a patch made from a full baseline
keeps the baseline; a blind patch does not create one).

Inputs → outputs: `{path, old_string, new_string, replace_all?}` → `{success, diff, strategy,
files_modified, resolved_path, note?}` or `{"error": …}` (no match adds a did-you-mean snippet;
repeated misses add a stronger hint). `fuzzy_match.find_and_replace(content, old, new,
replace_all) -> Replacement`.

Files: `src/indus/tools/builtin/patch.py`, `src/indus/tools/builtin/fuzzy_match.py`,
`tests/unit/tools/builtin/test_fuzzy_match.py`, `tests/unit/tools/builtin/test_patch.py`,
`tests/fixtures/fuzzy/*.txt`; shared: `src/indus/tools/builtin/__init__.py` (module list line).

Edge cases / failure modes:
- empty or whitespace-only `old_string` → error with recovery text.
- CRLF files: matching on LF text, the result written back with CRLF; BOM kept.
- missing file → error naming `write_file` for new files.

Our design differences / decisions:
- `mode` other than `replace` answers an error until P05-20 (slice D) adds V4A; the handler,
  guards and baseline helpers are importable for it (`patch.check_write_targets`,
  `patch.apply_text_edit`).
- unicode-normalised matches splice `new_string` as given (we do not re-apply the file's original
  typography inside the replaced span).
- The round-trip property test uses a seeded random generator (hypothesis is not in the dev group
  yet).

Tests we will write:
- one fixture per strategy where only that strategy (and later ones) match; ambiguity listing;
  replace_all; similarity + replace_all refused; already-applied; escape-drift autocorrect only when
  verbatim text exists; drift refusals; reindent; did-you-mean; property: exact patch then its
  inverse restores the file; tool-level: diff + strategy, guards, CRLF, baseline carry.

## Decisions for the lead
- V4A (P05-20) reaches files through `patch.PatchFiles` (resolve, read_text, write_text, delete, refusal); its async methods must match slice D's `PatchFiles` protocol at merge.

Compare checklist (filled after the rewrite; reopened `tools/fuzzy_match.py`,
`tools/escape_drift_autocorrect.py`):
- [x] Strategy order with one fixture per strategy where earlier ones miss
  (`tests/unit/tools/builtin/test_fuzzy_match.py::test_each_strategy_is_the_first_that_matches_its_fixture`); the
  indentation-flexible and trimmed-boundary strategies can never be first (line-trimmed accepts
  everything they accept, in the reference too), so they are tested alone
  (`::test_strategies_that_an_earlier_one_always_covers_still_match_alone`)
- [x] Ambiguity listing (5 + "more"), replace_all, similarity + replace_all refused, empty /
  whitespace / identical, already applied (`::test_several_matches_list_locations`,
  `::test_replace_all_refuses_approximate_matches`, `::test_already_applied_is_detected`)
- [x] Escape drift: corrected only with verbatim evidence, ambiguous refusals, newline-literal
  refusal, literal tab (`::test_quote_drift_is_corrected_when_the_fixed_text_exists`,
  `::test_quote_drift_is_not_corrected_without_verbatim_evidence`,
  `::test_doubled_backslashes_are_halved_when_the_halved_text_exists`, `::test_newline_literal_drift_is_refused`)
- [x] Re-indent; property round trip (`::test_non_exact_match_is_reindented_onto_the_file`,
  `::test_exact_patch_then_inverse_restores_the_file`)
- [x] Tool: diff + strategy, CRLF/BOM, guards, misses, shell backend
  (`tests/unit/tools/builtin/test_patch.py`)
- Gap found and fixed: a similarity match whose replacement leaves the file unchanged was a
  "successful" no-op write; it now answers as already applied (`::test_already_applied_is_detected`).
- Deliberate differences: typography inside a unicode-normalised span is not re-applied;
  `PatchFiles` (for P05-20) has async `read_text`/`write_text`/`delete`/`refusal` because the
  shell backends are async; `refusal(path, content=False)` skips the binary-document guard for
  deletes/moves.
- Not taken: shared edit-match metrics.

## Status

Done (2026-10-18). Shared: `src/indus/tools/builtin/__init__.py`.

## Integration (lead)

Merged in the P05 wave 2 integration (2026-10-18, lead's verify tree).
- V4A wired: `patch` sends a call that `v4a.wants_v4a` to `v4a.run_patch_mode` with
  `PatchFiles` and `lint_host=environment_of(ops)`. `PatchFiles` now matches `v4a.PatchFiles`:
  `read_text` returns None for a missing file, `refusal(path, action, content)` (binary-document
  check only for writes). `write_text` no longer runs the lint gate itself: `apply_patch` gates
  every file before the first write, and a rollback must always be able to put the old text back.
- The schema keeps the replace shape; the handler accepts V4A from any model. Showing `mode:
  patch` to the model families that prefer it needs a route-aware schema (the definitions
  builder passes only the scope to `schema_overrides`) — P06 seam.
- Replace edits lint through the same functions as `write_file`. Tests:
  `test_patch.py::test_patch_mode_*`, `::test_lint_gate_refuses_breaking_a_data_file`,
  `::test_lint_after_reports_only_errors_the_edit_introduced`.
