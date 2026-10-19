# Task: Starter skill set (P09-22) Phase: 09 Owner: P09 slice C builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none. The phase row allows the
skills catalogue page for names only; our fifteen names are already fixed by the phase file
(D-16.33, D-16.43), so no reference skill file, catalogue page or skill text was opened. Research
`07` §3.12 (inventory of names and the "MIT in frontmatter" rule) only.
Evidence: none.

What it does (own words): fifteen small, practical skills ship in the repo's `skills/` tree and are
seeded into every profile (P09-21). Each one teaches the agent a repeatable way to do a common job
with Indus Agent's own tools (`terminal`, `read_file`, `write_file`, `patch`, `search_files`,
`web_search`, `web_extract`, `execute_code`, `todo_list`, `clarify`, `memory`, `skill_view`,
`skill_manage`, `cronjob_manage`, `delegate_task`). The index in the prompt shows the name and the
short description; the agent opens the body with `skill_view` when the job matches.

Inputs → outputs: none at runtime; files `skills/<category>/<name>/SKILL.md` (+ `references/` where
it earns its place) → validated by the P09-05 rules (stand-in until slice B lands).

Files: `skills/software-development/{git-workflow,code-review,systematic-debugging,test-first,
github-cli,shell-scripting,project-onboarding}/SKILL.md`, `skills/writing/writing-clearly/SKILL.md`,
`skills/research/research-with-sources/SKILL.md`, `skills/data/{data-wrangling,pdf-and-documents}/
SKILL.md`, `skills/productivity/{scheduling-jobs,meeting-notes-to-actions}/SKILL.md`,
`skills/indus/{skill-authoring,indus-guide}/SKILL.md`, three `references/` files,
`tests/unit/learning/skills/test_starter_skills.py`, `tests/unit/learning/skills/_stubs_c.py`.

Structure every skill follows (ours): frontmatter (`name`, `description` ≤ 60 chars, `version`,
`author`, `license: MIT`, `metadata.indus.{category, tags}`), then a one-paragraph purpose, then
`## When to use`, `## Procedure` (numbered steps naming our tools), `## Pitfalls`,
`## Verification`, and an optional `## Quick reference` last.

Edge cases / failure modes:
- A name or wording that matches a banned identifier fails the guard: the product name in prose is
  "Indus Agent", the CLI `indus`.
- Shared 12-word runs with the reference skill trees fail the similarity guard; the texts are
  written from scratch and the guard runs over `skills/` (scan root added).
- Visibility conditions would hide a skill on some surfaces; only `requires_toolsets: [terminal]`
  is used, on the skills that cannot work without a shell.
- Descriptions are clipped at 60 chars in the index (P09-07), so each fits whole.

Our design differences / decisions:
- Our own set of fifteen (not the reference's 58), our categories, our section order.
- `pdf-and-documents` lives under `data`; `meeting-notes-to-actions` under `productivity`.
- `references/` only for `skill-authoring` (frontmatter fields), `indus-guide` (command and file
  map) and `git-workflow` (conflict and recovery recipes).

Tests we will write:
- exactly the fifteen expected names, one per directory, dir name == frontmatter name, category
  path == `metadata.indus.category`.
- every frontmatter validates (stand-in `SkillFrontmatter` with the P09-05 fields and limits),
  `license == "MIT"`, description ≤ 60 chars, body has the four sections in order.
- the stand-in linter reports zero findings; support files only under allowed dirs.
- no banned identifier in any skill file (patterns from `tooling/checks/banned_patterns.json`).
- seeding into a temp home installs exactly these fifteen (P09-21 `sync_bundled`).

## Decisions for the lead
- Validation uses `tests/unit/learning/skills/_stubs_c.py` (stand-in `SkillFrontmatter`,
  `parse_skill_md`, `lint_body`); at integration switch the test's import to slice B's
  `indus.learning.skills.format` / `lint` and delete the stub.

Compare checklist (filled after the rewrite):
- [x] 15 skills validate with zero lint findings (test: `test_starter_skills.py::test_frontmatter_valid`, `::test_lint_clean`)
- [x] n-gram guard over `skills/` clean (`uv run python -m tooling.checks --require-ref`)
- [x] seeding installs exactly these 15 (`::test_seeding_installs_exactly_the_starter_set`)
- Deliberate differences: our own names, categories, structure and texts (D-16.33).
- Not taken: the reference's optional catalogue (hub, P13).

## Status

Done (2026-10-19). Shared: `tooling/checks/similarity.py`, `tooling/checks/banned.py` (scan root
`skills`).

## Integration (lead, P09 wave 1 landing, 2026-10-19)

Integration: `_stubs_c.py` deleted; `test_starter_skills.py` validates with
`format.check_skill_text` and `lint.lint_skill`; a new test resolves the seeded set through the
resolver and pins `core/prompt/texts.HELP_SKILL_NAME == "indus-guide"` (was `indus-agent`).
