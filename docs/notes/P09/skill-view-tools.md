# Task: `skills_list` and `skill_view` tools (P09-08) Phase: 09 Owner: P09 slice B builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/skills_tool.py` (the list
cache, the three disclosure levels, linked-file groups, required-variable handling and where the
secret prompt is allowed). Closed before writing.
Evidence: none.

What it does (own words): progressive disclosure. `skills_list` (level 0) gives one row per
visible skill — name, description, category — and is cached per profile for 30 seconds (cleared
at once by `skill_manage`). `skill_view(name)` (level 1) returns the whole `SKILL.md`, its parsed
frontmatter and the support files the skill ships, grouped by folder. `skill_view(name, file_path)`
(level 2) returns one support file; the path must still be inside the skill folder after symlinks
are resolved. A skill can declare environment variables it needs: when one is missing and the
session can ask a person for a secret (the local CLI answers `secret` server requests), the person
is asked and the value is stored in the profile's secret store (they may skip). Surfaces that
cannot answer (messaging) or must not wait (cron, one-shot, unattended) never ask; the result
carries a setup hint pointing at the local CLI. Declared variables join the process passthrough set
so sandboxed terminal and code runs receive them. Every view bumps `view_count`/`last_viewed_at`
(P09-12) and records read evidence per (session, skill, file) with the content hash, which the
review fork's write guard (P09-11) checks. Both tools are read-only (parallel-admissible).

Inputs → outputs: `skills_list {category?}` → JSON `{skills: [...], count}`;
`skill_view {name, file_path?}` → JSON `{name, path, content, metadata, linked_files, setup?}` or `{name, file_path, content}`; errors as `{"error": ...}`.

Files: `src/indus/learning/skills/view_tools.py`, `tests/unit/learning/skills/test_view_tools.py`.

Edge cases / failure modes:
- unknown, ambiguous, disabled or broken skill → error naming the reason (ambiguous lists paths);
- `file_path` with `..`, absolute, or a symlink leading out of the skill → refused;
- support file missing → error listing the files that exist; binary file → size note, no content;
- file larger than 1 MiB (external skills) → first 1 MiB with a cut note;
- secret request unsupported / withdrawn / skipped → setup note, the view still succeeds;
- telemetry failure never fails the view.

Our design differences / decisions:
- the list cannot see the session's tool array (handlers are not given it), so `skills_list`
  applies OS, environment, app and surface rules but not the tool/toolset conditions; the prompt
  index (which knows the tools) applies all of them;
- read evidence stores the sha256 of what was shown, so a file changed after the view is not "read";
- the secret prompt goes through the P04 server-request broker (`secret` kind), never a direct
  terminal prompt.

Tests we will write:
- list cache honoured within 30 s on an injected clock and refreshed after; category filter;
- view of `SKILL.md`, a nested support file, `../` escape and symlink escape refused;
- missing env var: fake broker answering `secret` stores the value; a broker raising
  `RequestUnsupported` (gateway) gives the setup hint; cron context never asks;
- view bumps `view_count`; disabled skill refused; read evidence recorded with the hash.

## Decisions for the lead
- `skills_list` ignores `requires_tools`/`fallback_for_tools` style conditions (see above); a later
  seam could offer the session's tool names as a handler extra.

Compare checklist (filled after the rewrite; reopened `tools/skills_tool.py`):
- [x] 30 s list cache (test: `tests/unit/learning/skills/test_view_tools.py::test_list_cache_ttl`)
- [x] linked files grouped by folder; level-2 path containment (test: `::test_view_nested_support_file`, `::test_escape_refused`)
- [x] secrets asked only where a person can answer (test: `::test_missing_env_prompts_on_cli`, `::test_missing_env_hint_on_gateway`)
- Deliberate differences: the reference resolves plugin skills through a separate path; ours go
  through the same catalog. The reference also checks credential files; P09 has none.
- Not taken: security warnings for project skills (P13).

## Status

Done (2026-10-19). Shared: none.
