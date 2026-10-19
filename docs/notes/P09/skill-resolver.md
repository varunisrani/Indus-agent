# Task: Skill discovery and resolution (P09-06) Phase: 09 Owner: P09 slice B builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/skill_utils.py` (search
roots and their order, the same-skill test, candidate ranking, catalog statuses, disabled matching,
external/create dirs). Closed before writing.
Evidence: none.

What it does (own words): every consumer that needs "the skill called X" — the prompt index,
`skills_list`, `skill_view`, slash commands, preload/auto-load and (later) cron — asks one
`SkillCatalog`. The catalog scans the roots in tier order: project (reserved for P13, always empty
here), the profile's `<home>/skills/`, the optional `skills.create_dir`, then each
`skills.external_dirs` entry (`~` and `${VAR}` expanded, missing folders skipped quietly). A skill
in a higher tier hides every same-named skill below it, for every consumer, and the hidden copy is
logged once. Two different skills with one name inside the winning tier are both listed under their
relative paths; loading the bare name fails with "ambiguous skill name, use one of …". Copies that
are provably the same skill (same real file through a symlink, or byte-identical) are not a
conflict: the shallowest path wins. `skills.disabled` (plus `skills.platform_disabled.<surface>`)
hides a skill and makes a view refuse it. Plugin skills are named `plugin:skill` and sit outside the
tiers so they never collide. Parsing is cached per `SKILL.md` by `(path, mtime_ns, size)`.

Inputs → outputs: scope (home, env, config) + surface → `SkillCatalog` with `records` (every scanned
skill with status unique / ambiguous / shadowed / broken), `visible(ctx)`, `resolve(identifier)` →
`SkillRecord` or `SkillNotFound` / `AmbiguousSkill` / `SkillDisabled`.

Files: `src/indus/learning/skills/resolver.py`, `tests/unit/learning/skills/test_resolver.py`,
`tests/fixtures/skills/tiers/`. Shared: `src/indus/foundation/config/schema.py` (`skills` rows).

Edge cases / failure modes:
- a root that is missing, a file, or unreadable → skipped (debug log);
- `SKILL.md` with broken frontmatter → record with status `broken` and the reason; never listed in
  the index, a view reports the reason;
- folders `.git`, `.archive`, `.locks`, `.hub`, `.curator_backups`, `node_modules`, caches are not
  scanned; a folder holding `SKILL.md` is not descended into (support folders are not skills);
- the same external dir listed twice, or equal to the local root → scanned once;
- name declared in frontmatter differs from the folder name → the declared name wins, the folder
  name and relative path also resolve;
- a symlinked skill folder pointing into another root → one skill (realpath test).

Our design differences / decisions:
- one immutable catalog object per scan instead of module globals; consumers hold it for a call;
- broken skills are kept as records (status `broken`) so `indus skills check` can show them;
- external dirs are writable in place; they are a convenience, not a protection boundary (noted for
  P09-11 and the curator, which treat them as not curator-managed);
- the project tier is an empty, reserved parameter until P13 adds trust and quarantine.

Tests we will write:
- fixture trees: cross-tier shadowing gives the same path from index rows, list, view and slash
  lookups; same-tier duplicate raises ambiguity naming both paths; identical copies resolve to the
  shallowest; symlink view is one skill; disabled hidden and view refused (global and per surface);
  missing external dir ignored; `${VAR}` and `~` expansion; scan cache reused until a file changes;
  plugin names never collide.

## Decisions for the lead
- Per-surface disabling is a separate key `skills.platform_disabled: {<surface>: [names]}` beside
  the flat `skills.disabled` list (two simple shapes instead of one union type).
- A broken `SKILL.md` keeps its slot in name resolution only when nothing else owns the name.

Compare checklist (filled after the rewrite; reopened `agent/skill_utils.py`):
- [x] tier order project > local > create_dir > external (test: `tests/unit/learning/skills/test_resolver.py::test_cross_tier_shadowing_is_the_same_for_every_consumer`)
- [x] same-tier ambiguity lists both paths (test: `::test_same_tier_duplicate_is_ambiguous`)
- [x] identical copies / symlink view resolve to one (test: `::test_identical_copies_resolve_to_shallowest`, `::test_symlink_view_is_one_skill`)
- [x] disabled hides and refuses view (test: `::test_disabled_hidden_and_refused`)
- Deliberate differences: the reference also accepts a legacy flat `<name>.md` file; we accept only
  `SKILL.md` folders (no legacy installs to carry).
- Not taken: project-skill trust and quarantine (P13), hub ownership checks (P13).

## Status

Done (2026-10-19). Shared: `src/indus/foundation/config/schema.py` (`SkillsSection`).
