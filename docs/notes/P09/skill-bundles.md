# Task: Skill bundles (P09-10) Phase: 09 Owner: P09 slice B builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/skill_bundles.py` (file
shape, slug rule, how a bundle loads), `hermes_cli/bundles.py` (verbs and their flags). Closed
before writing.
Evidence: none.

What it does (own words): a bundle is a small YAML file `<home>/skill-bundles/<slug>.yaml` naming
several skills that are usually wanted together: `{name?, description?, skills: [...],
instruction?}`. Its slug is the slugified `name`, else the file stem. `/<slug> [text]` loads every
listed skill into one user message, the bundle's instruction first, then the user's text; a listed
skill that is missing is skipped and named. When a bundle and a skill share a slug the bundle wins.
`indus bundles` manages the files: `list`, `show <name>`, `create <name>` (interactive on a
terminal, or with `--skill/-s`, `--description`, `--instruction`; `--force` to overwrite),
`delete <name>` (asks; without a terminal `-y` is needed, exit 1 otherwise, per the P07 confirm rule) and `reload` (re-reads the folder and reports the
count).

Inputs → outputs: YAML files → `Bundle` objects; `/slug text` → `Expansion`; CLI verbs → files
created/removed and printed tables.

Files: `src/indus/learning/skills/bundles.py`, `src/indus/cli/commands/bundles.py`,
`tests/unit/learning/skills/test_bundles.py`, `tests/unit/cli/commands/test_bundles.py`. Shared:
`src/indus/cli/app.py` (one `bundles` registry row).

Edge cases / failure modes:
- `skills` missing, empty, or not a list of names → the file is rejected (logged once and skipped
  on scan; `create` refuses);
- YAML that is not a mapping, or unreadable → skipped with a warning;
- two files with the same slug → the first by file name wins, the other is logged;
- a slug that is not a valid command word after slugifying (empty) → rejected;
- `create` on an existing slug without `--force` → refused, exit 1; non-terminal `create` without
  `--skill` → exit 2 with a hint; non-terminal `delete` without `-y` → exit 2;
- every listed skill missing → the bundle expands to nothing and the surface says so.

Our design differences / decisions:
- the slug rule: lowercase, runs of characters outside `[a-z0-9._-]` become `-`, edges trimmed;
- the bundle frame and instruction wording are ours; the instruction comes first by design;
- scan results are cached by the folder's file list and their `(mtime_ns, size)`; `reload` clears.

Tests we will write:
- one missing skill: the rest load and the gap is named; bundle beats a same-slug skill; empty
  `skills` rejected; slug from name vs file stem; invalid files skipped;
- CLI (CliRunner, non-TTY): create via flags, create without `--force` refuses overwrite, list,
  show, delete with `-y`, delete without `-y` exits 1, reload count.

## Decisions for the lead
- None beyond the shared registry row.

Compare checklist (filled after the rewrite; reopened both files):
- [x] slug default = file stem; bundle beats same-slug skill (test: `tests/unit/learning/skills/test_bundles.py`)
- [x] `create --force` and non-TTY rules (test: `tests/unit/cli/commands/test_bundles.py`)
- [x] (lead, at wave 2 landing) both `.yaml` and `.yml`; a byte-order mark is read; a duplicate slug
  keeps the first file alphabetically; an empty or missing `skills` list skips the file — all match.
  Differences kept: no default description text when the file has none (ours shows nothing);
  `create` without `--skill` needs a terminal instead of reading lines from stdin.
- Deliberate differences: none of substance; texts are ours.
- Not taken: bundle completion metadata for TUI (P12 reads `list_bundles()`).

## Status

Partial (2026-10-19). `bundles.py` and the CLI group are written and lint/type clean, but the two test files were not written before wrap-up, so the compare checklist above is unverified. Shared: `src/indus/cli/app.py`.

## Integration (lead, P09 wave 1 landing, 2026-10-19)

Integration: tests written by the lead — `tests/unit/learning/skills/test_bundles.py` (slug default,
invalid files, save/delete, missing skill named, bundle beats same-slug skill) and
`tests/unit/cli/commands/test_bundles.py` (non-TTY create via flags, `--force`, delete needs `-y`,
list/show/reload); contract rows added for every `bundles` verb. The reference compare for this
task is still open (wave 2).
