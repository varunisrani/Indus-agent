# P00-guards — clean-room guard suite

Task: P00-guards
Phase: P00 (foundations)
Owner: builder (guards)
Read: `docs/plan/README.md`, `docs/plan/research/*.md` (section 6 of each, for trap id formats),
the clean-room protocol sections 6–8 (skill reference, not the reference repo). The reference
checkout was only listed (file types, sizes, commit), never read for content.

## What

One stdlib-only Python package, `tooling/checks`, that every later commit must pass:

```
python -m tooling.checks [--require-ref] [--ref PATH] [--json] [--closest N]
                         [--strict] [--body-file F] [names...]
```

It runs every named check (default: all), keeps going past failures, prints a summary table and
exits with the worst code: `0` clean, `1` violations, `2` setup problem. Each check also runs
alone as `python -m tooling.checks.<name>` with the same flags.

Reference path: `--ref`, else env `INDUS_REF`, else `/root/reference/hermes-agent` if it exists.
Without a reference, `similarity` and `assets` warn and skip (exit 0); with `--require-ref` a
missing reference is exit 2.

| Check | Input | Fails when |
|---|---|---|
| `similarity` | product files vs the reference | a shared run of 50 code tokens, 6 meaningful lines or 12 prose words |
| `banned` | text files under `src/ apps/ scripts/ tests/ tooling/` | any pattern in `banned_patterns.json` matches (content or path) |
| `assets` | SHA-256 of media files on both sides | any identical file |
| `licenses` | runtime deps (pyproject + transitive Requires-Dist; apps/*/package.json) | licence outside `licenses_allow.json` without an exception |
| `traps` | `docs/internals/traps-ledger.md` vs trap ids in `docs/plan/**` | trap without a row, row without coverage, test ref that does not exist |
| `acceptance` | `docs/internals/acceptance-map.md` vs `docs/plan/03-feature-inventory.md` | P0/P1 feature without an ACC id, missing test (`--strict`: any Review row) |
| `prbody` | PR body (`--body-file`, `PR_BODY`, or `GITHUB_EVENT_PATH`) | missing `Reference:` or `Task note:` line |

## Inputs / outputs

- Data files (all JSON, all owned here): `banned_patterns.json` (`pattern`, `hint`, `flags`),
  `similarity_allow.json` (list of `{path, reason}`), `licenses_allow.json`
  (`allow`, `notes`, `exceptions`).
- Cache: `~/.cache/indus-checks/<ref-commit>-<checker-hash>-<detector>-<size>.pickle`
  (override the directory with `INDUS_CHECKS_CACHE`). The checker hash covers the similarity
  sources and the Python minor version, so any change to tokenizing rebuilds the index.
- `--json` prints one JSON object (`{"code", "checks": [{name, code, violations, warnings, notes,
  data}]}`) instead of the table.

## Similarity design

Units per detector, each unit carrying its source line:

- **tokens** — Python via `tokenize` (drop COMMENT, NL, NEWLINE, INDENT, DEDENT, ENCODING,
  ENDMARKER; nothing else normalised); other code via a regex scanner (string literals whole,
  identifiers, numbers, one token per punctuation char) that strips `//`, `/* */` (C-like) or
  `#` (shell/YAML/TOML-like) comments. Gate window 50.
- **lines** — stripped lines of code/config files, minus blanks, whole-line comments, imports
  (including multi-line Python and JS import blocks) and lines made only of brackets and
  punctuation. Gate window 6.
- **prose** — lower-cased `[a-z0-9_]+` words of Markdown/text files plus code comments and
  Python string statements (docstrings). Gate window 12.

Index: every window is hashed with a polynomial rolling hash (mod 2^61−1) over per-unit 64-bit
values (blake2b, 8 bytes, memoised per distinct unit). The index stores, per detector and window
size, a sorted `array('Q')` of `(hash40 << 24) | file_id`. A lookup is a bisect; every candidate is
verified against the actual reference file units (so hash collisions never reach a report) and
then extended both ways to its longest common run. Gate mode reports runs ≥ the gate size;
`--closest N` probes with windows of 20 tokens / 3 lines / 6 words (separate cached index),
scores run ÷ gate size and prints the top N without gating.

Reference walk skips `.git`, `node_modules`, `dist`, `build`, `vendor`, `.venv`, `__pycache__`,
lockfiles, files with a NUL byte in the first 8 KiB, files over 2 MB and extensions we do not
classify (e.g. the reference's per-address contributor files). Indexing uses a process pool
(`INDUS_CHECKS_JOBS`, default 2 because the machine is shared).

Product side: `src/ apps/ tooling/ scripts/ tests/` (minus `tooling/checks/tests/fixtures/`),
Markdown under `docs/`, and `*.md` at the root.

**Identifier-list heuristic (docs/plan only).** Plan docs list reference names on purpose. When
building prose units for a file under `docs/plan/`, a whole line is skipped if more than 70% of
its whitespace-separated tokens contain `_`, `.`, `/` or a digit (and it has at least 3 tokens).
Ordinary sentences stay in; the 12-word gate is unchanged.

Allow-list: `similarity_allow.json` entries `{ "path": <glob on our repo path>, "reason": <text> }`;
an entry without a reason is a setup problem (exit 2). Matching hits are dropped and counted.

## Edge cases

- Python files that do not tokenize fall back to the regex scanner with `#` comments.
- Several reference files with the same run: the hit is reported once with `(+N more)`.
- Overlapping windows inside one run collapse to one hit (dedupe by extended run start).
- Traps: ids are taken only from definition positions (start of a list item or table row, bold or
  backticked) inside sections whose heading mentions "trap", so cross-references do not create ids.
- Test refs (`path::test`, `path::Class::test`, `[param]` suffix stripped) are checked with `ast`.
- `licenses`: Requires-Dist entries guarded by `extra ==` are skipped; other markers are not
  evaluated (no `packaging` dependency) — a conditional dep that is not installed is a note, an
  unconditional one is a setup problem.
- `prbody` without any body source (local run) is skipped with a note.
- Acceptance inventory: only rows inside area sections (`## <Letter>. …`, letters A–O, Q, S) whose
  id starts with that letter count; tier-count tables and the legend are never read. A mixed tier
  `P0 → P1` is gated by its first tier; `—` is dropped (not gated, no warning); a row with no
  recognisable tier is a warning. `acceptance --write-skeleton` writes one `ACC-<id>-1` row per
  P0/P1 feature with `Review: not yet planned (phase TBD)`.

## Our differences from the protocol example

- Python, stdlib only; index is a sorted packed array plus on-demand verification instead of a
  hash map, to keep ~25M reference windows in a few hundred MB.
- `banned` also checks file paths, not only contents.
- `traps --write-skeleton` / `acceptance --write-skeleton` create (or extend) the ledgers with a
  `Review: not yet planned (P00-guards)` line per missing id; existing rows are never rewritten.

## Decisions for the lead

1. Root `README.md`/`AGENTS.md`/`CLAUDE.md`/`THIRD_PARTY_NOTICES.md` and `docs/` are outside the
   banned scan; a nested `README.md` under a scanned folder is still scanned.
2. `SOUL.md` is not in the banned list (left for the lead).
3. Unknown file extensions are not indexed on either side (listed in `similarity.py`).
4. Allow-list misconfiguration (missing reason) is exit 2, not exit 1.
5. A Review line without a trailing `(<task id>)` is a warning, not a failure.
6. `--closest` turns the similarity check into a report (exit 0) for that run.
7. The default reference path lives in `tooling/checks/reference.json` (data, exempt from `banned`
   like `banned_patterns.json`), so no product module spells the reference's name.
8. `similarity_allow.json` starts with one entry (lead ruling): `THIRD_PARTY_NOTICES.md`, the MIT
   licence text we must reproduce verbatim.
9. `licenses`: an empty `[project].dependencies` (dev groups only) is a pass.
10. Real-reference detection tests carry `@pytest.mark.ref` (marker registered in pyproject).

## Tests planned

Unit tests per check on tiny synthetic fixtures under `tooling/checks/tests/fixtures/` and in
`tmp_path`; detection tests against the real reference (skipped when absent): a 60-token slice of a
reference `.py` file and a 12-word run from a reference `.md`, copied into a temp product tree at
test time, must be caught; a clean synthetic file must pass.

## Compare checklist (after)

- [x] No reference text in any file written here (fixtures synthetic; detection tests copy at runtime).
- [x] Banned names appear only in `banned_patterns.json` and the banned tests' runtime strings.
- [x] Full suite run with `--require-ref`; timing recorded in the report.
