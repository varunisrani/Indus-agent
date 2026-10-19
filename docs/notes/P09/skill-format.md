# Task: Skill format, validation and linter (P09-05) Phase: 09 Owner: P09 slice B builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/skill_utils.py` (platform,
environment and app conditions, metadata condition keys, config variable declarations),
`tools/skill_linter.py` (rule ids and thresholds only), `tools/skill_manager_tool.py` (the limit
constants). Closed before writing.
Evidence: none.

What it does (own words): a skill is a folder holding `SKILL.md` — YAML frontmatter between `---`
fences, then a Markdown body. The frontmatter is checked by one Pydantic model (`SkillFrontmatter`)
whose field names follow the agentskills.io layout; product-specific keys live under
`metadata.indus`. Agent writes are held to hard limits (name shape and length, description length,
`SKILL.md` size, support file size, support files only under four folders). Visibility decides
whether a skill is offered in the current session: host OS (`platforms`), toolset/tool presence
(`requires_*` hide when absent, `fallback_for_*` hide when present), runtime environment tags
(`environments`, unknown tag = shown), apps that must be offerable (`requires_apps`, unknown app =
hidden) and the session surface (`session_platforms`). The linter gives advice only — it never
blocks a write — and returns findings with stable rule ids.

Inputs → outputs: `SKILL.md` text, support file path and bytes, a `VisibilityContext` → a parsed
skill or `SkillFormatError` with every problem listed; `visible()` / `hidden_reason()`;
`lint_skill(text, skill_dir)` → `[LintFinding(rule, message)]`.

Files: `src/indus/learning/skills/__init__.py`, `src/indus/learning/skills/format.py`,
`src/indus/learning/skills/lint.py`, `tests/unit/learning/skills/test_format.py`,
`tests/unit/learning/skills/test_lint.py`, `tests/fixtures/skills/invalid/`.

Edge cases / failure modes:
- no frontmatter fence, an unclosed fence, YAML that is not a mapping, a BOM before the fence;
- name 64 vs 65 chars, upper case, leading dot/dash, spaces; description 1,024 vs 1,025;
- `SKILL.md` exactly 100,000 chars passes, one more fails; support file 1 MiB passes, +1 byte fails;
- support path absolute, with `..`, in a folder other than the four, or `SKILL.md` itself;
- condition lists given as a single string instead of a list (accepted as a one-item list);
- an unknown environment tag shows the skill; an app name nobody declared hides it.

Our design differences / decisions:
- our namespace is `metadata.indus`; field names otherwise follow agentskills.io;
- `examples/` is part of the layout (readable) but agent writes go only to references, templates,
  scripts and assets;
- environment and app detection are injected callables (`VisibilityContext`), so the format module
  never probes the host; the defaults detect `docker` and `container` only;
- linter has three rules (incident log, sprawl, oversized); the reference's other style rules are
  not taken;
- incident-log threshold: at least 5 `#NNN`/`PR NNN`/`issue NNN` references **and** at least 5
  per 1,000 characters of prose (fenced code ignored), per the phase table.

Tests we will write:
- parametrised name/description/content/support size edges and support path refusals;
- frontmatter parsing (BOM, missing fence, non-mapping YAML) and nested `metadata.indus` fields;
- visibility truth table for every condition, including the unknown-tag and unknown-app cases;
- each lint rule fires on a synthetic body; a clean skill gives no finding.

## Decisions for the lead
- Incident-log rule = count ≥ 5 and density ≥ 5 per 1,000 prose chars (the phase text gives one
  number; the absolute floor stops a 150-char body with one PR number from tripping it).
- `references/` sprawl counts every file under `references/` recursively (not only `.md`).

Compare checklist (filled after the rewrite; reopened `agent/skill_utils.py`, `tools/skill_linter.py`):
- [x] limits 64 / 1,024 / 100,000 / 1 MiB and the name pattern (test: `tests/unit/learning/skills/test_format.py::test_name_rules`, `::test_content_limit`, `::test_support_file_size`)
- [x] support writes only under references/templates/scripts/assets (test: `::test_support_path_rules`)
- [x] unknown environment tag fails open, unknown app fails closed (test: `::test_visibility_table`)
- [x] linter thresholds 60 reference files, 24,000-char body (test: `tests/unit/learning/skills/test_lint.py`)
- Deliberate differences: the reference parses broken YAML by splitting lines on `:`; we refuse
  broken frontmatter on writes and record a load error on discovery (the skill is listed as broken,
  not guessed at). The reference's incident rule uses 4 refs / 0.5 per 1k; the phase table says 5
  per 1k and we follow it.
- Not taken: style lints (marketing words, missing sections, shell utility names, forbidden files,
  dangling links, POSIX gating) — not in the phase scope.

## Status

Done (2026-10-19). Shared: none.

## Integration (lead, P09 wave 1 landing, 2026-10-19)

Integration: YAML 1.1 guard added — `check_name` refuses names YAML 1.1 reads as non-text
(`on/off/yes/no/y/n/true/false/null`, numbers) and a frontmatter `name` that loaded as a boolean,
null or number is a clear error (`test_format.py::test_yaml_boolean_names_refused`). D-16.142.
