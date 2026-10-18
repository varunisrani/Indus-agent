# Task: V4A multi-file patches + lint after edit (P05-20) Phase: 05 Owner: wave-2 builder D

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/patch_parser.py` (whole:
envelope markers, op kinds, validation pass, apply order), `tools/file_operations_lint.py` (whole:
in-process table, shell linter table, unusable-linter detection, before/after delta, LSP hooks).
Closed before writing. The V4A envelope itself is a third-party format (published with OpenAI's
`apply_patch` tool); its grammar below is described from that public description, in our words.
Evidence: none.

What it does (own words): a model that prefers the V4A format sends one text that edits several
files at once. The text sits between `*** Begin Patch` and `*** End Patch` and holds a list of
file sections: `*** Add File: <path>` followed by `+` lines (the new file); `*** Delete File:
<path>`; `*** Update File: <path>`, optionally followed by `*** Move to: <new path>`, then hunks.
A hunk starts at an `@@` line that may carry an anchor text (a line such as `class Foo` that the
change sits below; several `@@` lines narrow step by step), then lines starting with a space
(context), `-` (removed) or `+` (added); `*** End of File` pins the hunk to the end of the file.
We also accept `*** Move File: <a> -> <b>` as a plain rename. The whole patch is applied all or
nothing: every file is read and every new content computed in memory first; guard refusals and
fail-closed parse checks run before the first write; writes and deletes then run in order and, if
one fails, the ones already done are undone from the copies read at the start.

Lint after edit: after a write or patch, the edited file is checked and only problems the edit
introduced are reported. `.py` (compile), `.json`, `.yaml`/`.yml`, `.toml` are checked in process
from the text itself; `.js`/`.mjs`/`.cjs` (`node --check`) and `.ts`/`.mts`/`.cts` (`tsc --noEmit`)
run through the task's environment when the tool exists there. For the parse-only formats (JSON,
YAML, TOML) a write that would turn a valid file (or a new file) into one that does not parse is
refused before anything is written ("fail closed").

Inputs → outputs: `parse(text) -> Patch` (raises `PatchSyntaxError` naming the line);
`apply_patch(patch, files, *, lint_host=None) -> PatchOutcome`; `run_patch_mode(args, files,
*, lint_host=None) -> str` (the `patch` tool's `mode: "patch"` branch, JSON text);
`PATCH_MODE_PROPERTIES` + `with_patch_mode(schema)` (schema fragment for the patch tool);
`wants_v4a(args)`. Lint: `check_text(path, text)`, `gate(path, before, after) -> str | None`,
`lint_edit(path, before, after, *, host=None) -> LintReport`.

Files: `src/indus/tools/builtin/v4a.py`, `src/indus/tools/builtin/lint.py`,
`src/indus/tools/builtin/exec_host.py` (new: the small command-host protocol shared with
search_files), `tests/unit/tools/builtin/test_v4a.py`, `tests/unit/tools/builtin/test_lint.py`;
shared: `src/indus/foundation/yamlio.py` (`check_syntax`: event-level YAML parse so multi-document
files and custom tags are not refused), `docs/internals/acceptance-map.md` (ACC-C12-2, ACC-C14-1).

Edge cases / failure modes:
- Envelope: text before `Begin` / after `End` (code fences, a heredoc wrapper) ignored; missing
  `End Patch` refused (a cut-off patch must never half-apply); CRLF accepted; unknown `***` header,
  a body line that does not start with ` `/`-`/`+` inside an update, an empty path, an update with
  neither hunks nor a move, a delete with a body → `PatchSyntaxError` with the 1-based line.
- Apply: Add on an existing file (use Update), Update/Delete of a missing file, Move onto an
  existing path, two sections on one file (later ones see earlier results), a hunk that is already
  applied (skipped with a note), a hunk not found (error names file, hunk number, first line looked
  for), ambiguous anchor (first one after the cursor wins, as the format intends).
- Matching: exact, then trailing-space-insensitive, then whitespace-trimmed, then punctuation
  normalised (smart quotes, dashes, NBSP); searched from the cursor forward, then the whole file.
- File endings: an existing file keeps its line ending (LF/CRLF) and its final-newline state; an
  added file ends with a newline.
- Rollback failure (the undo itself fails) is reported with the paths left changed.
- Lint: linter missing → skipped; linter that cannot run (timeout) → skipped with a reason;
  only one Python error is reported by compile, so "pre-existing" is decided by message with
  positions removed; shell linters lint the old content in a hidden temp sibling only when the
  new content fails.

Our design differences / decisions:
- The patch tool (P05-19, slice A) owns the tool entry; this module gives the mode branch,
  the schema fragment and the parser/applier. File access goes through a `PatchFiles` protocol
  (resolve, read, write, delete, refusal); the integration adapts P05-15's `FileOps` and P05-18's
  write guards to it.
- Our own line matcher for hunks (no dependency on the `old_string` fuzzy strategies).
- True rollback in the apply phase (the reference only validates first).
- `*** Move to:` (published grammar) and `*** Move File: a -> b` both accepted.
- No LSP tier (P24).

Tests we will write:
- parse: every op kind, anchors, End of File, Move to + Move File, fences ignored, each malformed
  case names its line, CRLF.
- apply: multi-file success with diff; second file failing to match → nothing written; write
  failure mid-way → earlier writes undone; move + update; add on existing refused; delete missing
  refused; already-applied hunk skipped; refusal from guards stops before writing; fail-closed
  JSON refused; CRLF and missing final newline kept; hypothesis: random edits round trip.
- lint: each in-process linter; gate refuses new breakage, allows already-broken; delta with a
  pre-existing Python error not reported, a new one reported; shell linter via a fake host
  (clean, introduced, pre-existing through the temp sibling, missing tool); real `node --check`
  when node exists.

## Decisions for the lead
- `PatchFiles.refusal(path, action, content)` is where write guards plug in; V4A asks it for every
  path before writing anything. Wire it to P05-18's guard chain and baseline update at integration.
- The patch tool's handler should call `v4a.run_patch_mode` when `v4a.wants_v4a(args)`; models of
  families that prefer V4A see `with_patch_mode(schema)` (family list is the patch tool's choice).
- `lint_edit` needs the task's environment as an `ExecHost` for the shell linters; without one only
  in-process linters run.

Compare checklist (filled after the rewrite; reopened `tools/patch_parser.py`,
`tools/file_operations_lint.py`, and `tools/file_operations.py` for the fail-closed caller only):
- [x] Op kinds and markers (test: `test_v4a.py::test_parse_every_section_kind`)
- [x] Everything validated before the first write (test:
  `test_v4a.py::test_second_file_miss_writes_nothing`, `::test_guard_refusal_stops_before_any_write`)
- [x] Add on an existing file refused; Move onto an existing path refused; Delete of a missing file
  refused (tests: `::test_add_on_existing_and_delete_of_missing_are_refused`,
  `::test_move_onto_an_existing_file_is_refused`)
- [x] Overlay across sections: delete then add of one path, move then update of the new name
  (tests: `::test_delete_then_add_of_one_path_in_one_patch`,
  `::test_plain_move_and_a_later_update_of_the_new_name`)
- [x] Context-only patch is "no changes"; already-applied hunks skipped
  (tests: `::test_context_only_patch_changes_nothing`, `::test_already_applied_hunk_is_skipped`)
- [x] Gap found: an addition-only hunk under an anchor that appears several times must not guess —
  fixed by refusing it, pinned by `::test_added_lines_below_a_repeated_anchor_are_refused`
- [x] Gap found: a loose (whitespace/punctuation) match must keep the file's own context lines —
  fixed by `_merge`, pinned by `::test_whitespace_and_punctuation_drift_still_matches_with_a_note`
- [x] Lint table and fail-closed set; before/after delta (tests: `test_lint.py`)
- [x] Gap found: per-file `tsc` inside a tsconfig project reports errors the project does not
  have — fixed by skipping it there, pinned by
  `test_lint.py::test_real_tsc_is_skipped_inside_a_tsconfig_project`
- [x] Gap found: a UTF-8 byte order mark must not fail the JSON/TOML checks — pinned by
  `test_lint.py::test_data_checkers_accept_a_byte_order_mark`
- Deliberate differences: the reference parses from the top when `Begin` is missing and to the end
  when `End` is missing; we refuse both (a cut-off patch never half-applies). The reference treats
  an unprefixed line inside an update as context; we refuse it (published grammar). The reference
  validates first but does not undo a failed apply; we roll back. The reference's fail-closed gate
  refuses any unparsable data file; ours lets an already-broken file be edited (repairs in
  steps). Problems are compared with numbers masked instead of exact lines, so an error that only
  moved is still "pre-existing". Python is checked with `compile` (catches what `ast.parse`
  misses, e.g. `return` outside a function). Added files end with a newline.
- Not taken: LSP diagnostics (P24); `go vet` / `rustfmt` shell linters; the reference's
  managed-Node runner and trusted-workspace rules for `npx` (we only run `node`/`tsc` found on
  the environment's PATH, never `npx`).

## Status

Done (2026-10-18). Parser, all-or-nothing applier, the patch-mode branch and schema fragment, and
lint after edit are built and tested (62 tests in `test_v4a.py` + `test_lint.py`). Not wired yet:
the `patch` tool's handler (P05-19, slice A) must call `v4a.run_patch_mode` when
`v4a.wants_v4a(args)` and adapt its `FileOps` + write guards to `PatchFiles`; `write_file`
(P05-18) should call `lint.gate` before writing and `lint.lint_edit` after. Shared change:
`foundation/yamlio.py` gained `check_syntax`.

## Integration (lead)

Merged in the P05 wave 2 integration (2026-10-18, lead's verify tree).
- Wired into `patch` (mode branch) and `write_file`/`patch` (gate before, `lint_edit` after) — see
  patch-fuzzy and write-file. D's `PatchFiles` protocol was already async; A's adapter was
  changed to it (`read_text → None` when missing, `refusal(path, action, content)`).
- `exec_host` stays as the narrow tools-side protocol (docstring updated for D-04.7). The lint
  host is the task's live environment (`file_ops.environment_of`).
- Route-aware schema (V4A shown per model family) left for P06.
