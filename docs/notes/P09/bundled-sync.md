# Task: Bundled skill seeding and sync (P09-21) Phase: 09 Owner: P09 slice C builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/skills_sync.py` (manifest
rules, new / existing / deleted / dropped classification, replace-with-backup),
`tools/skills_sync_bundled_ops.py` (opt-out marker, remove-pristine, reset with restore),
`tools/skills_sync_optional.py` (only the runtime-cache exclusion helper). Research `07` §3.11.
Closed before writing.
Evidence: none.

What it does (own words): the repo ships a `skills/<category>/<name>/` tree. Seeding copies each
of those packages into a profile's `<home>/skills/<category>/<name>/` and remembers, per skill, the
SHA-256 of the package as it was copied (`<home>/skills/.bundled_manifest`, one `name:hash` line
per skill, sorted). Later syncs use that record to tell three cases apart without asking anyone:
the user's copy still equals what we copied (safe to refresh when the shipped version changed), the
user changed it (never touched again), or the user deleted it (stays deleted: the row is kept and
nothing is copied). A skill that we stop shipping keeps its row while a live or archived copy of it
exists, because that row is the only proof the copy came from us (the curator reads it to keep its
hands off bundled skills, P09-18). Generated caches (`__pycache__`, `.pytest_cache`, `.mypy_cache`,
`.ruff_cache`, `*.pyc`) are neither hashed nor copied, so running a helper script never makes a
skill look edited. A marker file `<home>/.no-bundled-skills` stops every seeding path.

Inputs → outputs: `sync_bundled(home, source=…)` → `SyncReport` (added, updated, unchanged, kept as
user-modified, kept because a same-name local skill exists, left deleted, rows cleaned, failures,
opted out). `set_opt_out(home, on)` → changed?; `remove_pristine(home, …)` → removed / kept with
reasons; `opt_in(home, seed=…)`; `reset(home, name, restore=…)` → `ResetResult`;
`list_modified(home, …)`; `bundled_source(env)` → the shipped tree. `indus profile create
--no-skills` writes the marker; a fresh (non-clone) profile is seeded at creation.

Files: `src/indus/learning/skills/bundled_sync.py`, `tests/unit/learning/skills/test_bundled_sync.py`.
Shared: `src/indus/cli/commands/profile.py` (`--no-skills`, seeding a new profile) and its test
`tests/unit/cli/commands/test_profile.py`; `tooling/checks/{similarity,banned}.py` (scan root
`skills`); package markers `src/indus/learning/skills/__init__.py`, `tests/unit/learning/__init__.py`,
`tests/unit/learning/skills/__init__.py` (slice B creates the same files).

Edge cases / failure modes:
- A same-name skill already in the home that we never recorded: kept as is; recorded only when it is
  byte-identical to what we ship (so a later upstream change can refresh it).
- Refresh is copy-new-then-swap: the new copy is written to a hidden sibling, the old dir is moved to
  a second hidden sibling, the new one renamed in, the old one removed. An interrupted swap leaves
  the old copy in the hidden sibling; the next sync moves it back before classifying, so a crash is
  never read as "user deleted it".
- Copy failure on a new skill: not recorded, retried on the next sync, reported in `failed`.
- Malformed manifest lines (no `:`) are ignored: the skill is then treated as new, and a differing
  local copy is kept, never overwritten.
- Deleting is scoped: anything not strictly under `<home>/skills` is refused.
- Two syncs at once: serialised by a lock file beside the manifest.
- Read-only package sources (system installs): copied files get user write bits so later refreshes
  and resets can remove them.
- Opted out: no copying, no manifest cleaning (the marker means "leave the tree alone").

Our design differences / decisions:
- SHA-256 over (relative posix path, NUL, size, bytes) per file instead of an MD5 stream; no legacy
  v1 manifest migration (we never wrote v1 lines).
- Skill name = the package directory name; the starter-skill test pins dir name == frontmatter
  `name`. No frontmatter parsing here, so this module does not depend on slice B.
- Opt-out stops seeding completely; we have no "essential skills" list that still seeds.
- No external-dirs deferral and no curator-suppressed list yet: `skills.external_dirs` lands with
  the resolver (P09-06) and pruning built-ins is archive-only (P09-18). Hooks noted below.
- No rename/recategorise recovery: our tree is new; noted as a follow-up for when a skill moves.
- Category `DESCRIPTION.md` files are not shipped by the starter set, so none are seeded.
- The API never prompts; confirmations (`opt-out --remove`, `reset --restore` without `--yes`) are
  the CLI's job (P09-24 owns `indus skills sync|opt-out|opt-in|reset|list-modified`).

Tests we will write:
- seed copies every package with its category path and writes sorted `name:hash` rows.
- edit one skill, change both upstream → only the untouched one refreshes; edited one reported.
- delete a seeded skill → stays deleted after a sync with an upstream change.
- upstream drop: row kept while a live or archived copy exists, cleaned once neither exists.
- `__pycache__` / `*.pyc` written by a helper script do not mark a skill modified and are not copied.
- pre-existing same-name local skill kept; identical one adopted into the manifest.
- interrupted swap recovered; copy failure not recorded.
- opt-out marker stops sync; `remove_pristine` deletes only byte-identical copies; opt-in with seed.
- reset clears the row (next sync re-baselines); reset `--restore` re-copies over an edited copy.
- `profile create --no-skills` writes the marker; plain create seeds.

## Decisions for the lead
- Wiring of the "install" and "update" seeding paths: there is no installer or `indus update` yet;
  `sync_bundled` is the one entry point. Fresh named profiles are seeded by `profile create`; the
  default profile is seeded only when something calls `sync_bundled` (suggest: P09-24's
  `indus skills sync`, the P25 installer and a first-run call in `foundation.bootstrap` caller
  once the manifest is missing). Easy to move.
- Wheel packaging: the repo-root `skills/` tree is outside `src/indus`; `bundled_source()` looks for
  `INDUS_BUNDLED_SKILLS` (from the scope env), then `indus/_bundled_skills` inside the package, then
  the repo checkout. Packaging needs `[tool.hatch.build.targets.wheel.force-include]
  "skills" = "indus/_bundled_skills"` in `pyproject.toml` (not added: pyproject is lead-owned).
- `tooling/checks/{similarity,banned}.py` now scan `skills/` (the phase's "n-gram check of
  `skills/` against the reference skill trees" had no scan root before).

Compare checklist (filled after the rewrite; reopened `tools/skills_sync.py`,
`tools/skills_sync_bundled_ops.py`):
- [x] new → copy + record; existing → refresh only when upstream changed AND copy equals origin
  (test: `test_bundled_sync.py::test_edited_skill_kept_and_pristine_refreshed`)
- [x] user-deleted stays deleted (`::test_deleted_skill_stays_deleted`)
- [x] upstream-dropped row kept while live or archived copy exists (`::test_dropped_upstream_row_lifecycle`)
- [x] cache dirs and `.pyc` excluded from hash and copy (`::test_runtime_caches_do_not_mark_modified`)
- [x] same-name local skill never overwritten; identical one baselined (`::test_existing_local_skill_kept`, `::test_existing_identical_skill_adopted`)
- [x] interrupted replace recovered before classification (`::test_interrupted_swap_recovered`)
- [x] opt-out stops seeding; remove deletes only pristine copies and drops their rows
  (`::test_opt_out_*`, `::test_remove_pristine_keeps_edited`)
- [x] reset clears row; restore deletes copy first, then clears row, then re-syncs
  (`::test_reset_*`)
- [x] rmtree scope guard (`::test_remove_tree_refuses_outside_skills`)
- [x] read-only sources: the reference chmods on rmtree failure; we give copied files owner write
  bits at copy time instead (`::test_read_only_source_copy_is_removable`)
- [x] missing shipped tree leaves the manifest alone (`::test_missing_source_touches_nothing`)
- [x] `--no-skills` writes the marker; plain create seeds (`test_profile.py::test_create_seeds_bundled_skills`,
  `::test_create_no_skills_opts_out`)
- Deliberate differences: SHA-256 per-file framing instead of an MD5 stream; opt-out seeds nothing
  (no essential set); malformed rows ignored rather than migrated; `remove_pristine` judges a copy by
  its recorded origin only (the reference also skips names no longer shipped); reset without
  `--restore` does not run a sync itself (the next sync re-tracks or leaves the copy as the user's);
  restore copies the one skill directly, so it works on an opted-out profile too.
- Not taken: rename/recategorise relocation, external-dirs deferral, curator-suppressed names,
  official optional-skill provenance backfill (hub, P13), diff (P09-24).

## Status

Done (2026-10-19). Shared: `src/indus/cli/commands/profile.py` (`--no-skills`, seed on create),
`tooling/checks/similarity.py` and `tooling/checks/banned.py` (`skills` scan root).

## Integration (lead, P09 wave 1 landing, 2026-10-19)

Integration: `pyproject.toml` force-includes `skills` as `indus/_bundled_skills` (wheel built and
checked: 15 `SKILL.md` + 3 references); `test_bundled_sync.py::test_wheel_ships_the_skills_where_bundled_source_looks`.
Seeding of the default profile (first run, `indus skills sync`, installer) is still open (P09-24, P25).
