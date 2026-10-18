# Task: `@` context references (P08-19) Phase: 08 Owner: P08 slice B builder

Read: none from the reference (the task row names only research). Research `01` §3.5 (last
bullet) and `05` §3.9 (token forms used by completion).
Evidence: none.

What it does (own words): before a user message is stored, reference tokens in it are expanded
into context blocks the model sees: `@file:PATH` (optionally `:a-b` for a line range, 100 KiB per
file with a truncation marker), `@folder:PATH` (a listing two levels deep, at most 200 entries),
`@diff` (the working-tree diff of the git root, capped), `@git:REF` (one commit's patch, capped),
`@url:URL` (through `web_extract`, so the URL policy applies) and any prefix a plugin registers.
Paths resolve against the session's working directory and pass the file-tool path guards;
credential files (env files, Indus secret and auth stores, SSH and cloud keys, key stores) are
refused with a visible note instead of being expanded. The expanded blocks are budgeted against
the context window; an item that does not fit becomes a one-line pointer. The store keeps the
typed text as the row content and the expanded text as its wire text, and every surface gets this
through `SessionService.submit`.

Inputs → outputs: message text, scope, cwd, budget → `Expansion{text, blocks, refusals}`.

Files: `src/indus/core/references.py`, `tests/unit/core/test_references.py`. Shared:
`src/indus/service/session_service.py` (expansion in `submit`).

Edge cases / failure modes:
- no token in the text → returned unchanged without any I/O;
- `@file:` on a directory, a missing file, a binary file, a device → refusal note;
- line range beyond the end → the lines that exist; `b < a` → refusal;
- trailing sentence punctuation after a token is not part of the path; quoted paths with spaces;
- not a git repository / git missing → refusal note for `@diff` / `@git:`; refs starting with `-`
  or with odd characters refused (no option injection);
- `@url:` blocked by policy or failing extract → refusal note;
- the same token twice → expanded once.

Our design differences / decisions:
- block wording and markers are ours; refusals ride in the expanded text as one line each and are
  also returned so a surface can show them;
- budget: the caller passes `budget_chars`; the service derives it from the context window
  (25 % of the window at 4 chars/token) until P08-02's trigger arithmetic is joined at integration;
- folder listing skips VCS and dependency/cache folders.

Tests we will write:
- `@file:a.py:2-4` inserts exactly three lines; whole file; truncation over 100 KiB;
- `@file:.env`, an SSH key, the Indus auth store → refused with a note;
- `@folder:` depth and count caps; `@diff` / `@git:HEAD` in a temp repo; bad ref refused;
- `@url:` to a private address refused by the URL policy; extract stub used for a public one;
- over-budget item → pointer; plugin prefix registered through the seam expands;
- service: typed text stored as content, expansion as wire text.

## Decisions for the lead
- Budget default (25 % of the window) is interim; replace with the P08-02 trigger minus the
  current prompt estimate when slice A lands.

Compare checklist (filled after the rewrite; no reference file read):
- [x] Forms and caps (test: `tests/unit/core/test_references.py::test_file_line_range_inserts_exactly_those_lines`)
- [x] Credential paths refused (test: `tests/unit/core/test_references.py::test_credential_files_refused_with_a_note`)
- Not taken: bare `@word` fuzzy mentions (that is completion, P12) and image references.

## Status

Done (2026-10-18). Wired into `SessionService.submit` (shared file).

## Integration (lead)

Merged in the P08 wave 1 integration (2026-10-18). The budget now follows P08-02: the room under
the session's compression trigger after the last reported prompt (`references.budget_for_trigger`,
4 chars per token; no room → every item is a pointer); 25 % of the window remains only for a
session with compression off (`session_service._reference_budget`). D-16.106.
