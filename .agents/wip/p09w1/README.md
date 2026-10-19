# P09 wave 1 — slice status at hand-off (Agent 1, 2026-10-19)

Base `29ddd7b`. Each patch applies with `git apply --3way` on that base; `<patch>.base` holds the base id.
Builder worktrees had no optional `docs` extra, so `test_layers` failed there only on pypdf/docx imports
(environmental); integrate in a fully synced venv.

## Slice C — `w13-p09c.patch` — DONE (P09-21, P09-22)
- 15 starter skills under `skills/<category>/<name>/SKILL.md` (+3 references files), MIT, fresh text.
- `src/indus/learning/skills/bundled_sync.py` (manifest `<home>/skills/.bundled_manifest`, sha256,
  user-edited skills never touched, opt-out marker), `profile create --no-skills`.
- `tooling/checks/{similarity,banned}.py` now scan `skills/` (the phase's n-gram check needed it).
- Integration: swap `tests/unit/learning/skills/_stubs_c.py` (STAND-IN) for B's format/lint modules
  and delete it. Wheel needs `[tool.hatch.build.targets.wheel.force-include] "skills" =
  "indus/_bundled_skills"`. Seeding callers still to wire (P09-24 `indus skills sync`, first run, P25
  installer).
- Package markers `src/indus/learning/skills/__init__.py`, `tests/unit/learning{,/skills}/__init__.py`
  are also created by B — expect add/add conflicts; keep B's.

## Slice B — `w13-p09b.patch` — P09-05..09, P09-12 DONE; P09-10 PARTIAL; P09-11, P09-13 NOT STARTED
- Done: format/limits/lint (05), resolver/catalog (06), prompt index + delta (07), skills_list /
  skill_view (08), invocation logic (09, not wired), usage sidecar + WRITE_ORIGIN (12).
- Partial: bundles (10) — code and `indus bundles` CLI written, no tests.
- Not started: `skill_manage` (11), mutation ledger (13).
- Shared: `foundation/config/schema.py` (SkillsSection, no `write_approval` row — A's), `cli/app.py`
  (bundles row).
- Wiring still to do: register `indus.learning.skills.view_tools` in `tools/builtin/__init__.py` and a
  `skills` toolset in `tools/toolsets.py`; session service uses `SkillsIndexBuilder` and
  `stage_index_delta` per turn; CLI fills `Extensions.skill_commands` and adds `indus chat -s/--skill`.
- Name mismatch: `core/prompt/texts.HELP_SKILL_NAME` is `"indus-agent"` but C's skill is `indus-guide`.
- YAML 1.1: a skill named `off`/`on`/`yes`/`no` parses as a boolean — add a guard.
- Memory tool (A) should share `indus.learning.skills.usage.WRITE_ORIGIN` / `is_unattended_review()`.

## Slice A — `w13-p09a.patch` — DONE (P09-01..04, P09-25), host wiring NOT done, Compare step open
- `src/indus/learning/memory/{store,tool,prompt_block}.py`, `learning/pending.py` (P09-03 gate:
  `PendingStore.stage/list/get/discard/count`, `gate_on`, `decide`, `approve`, `reject`,
  `render_approval`, `skill_diff`; write-origin var `bind_origin` / `current_origin` /
  `unattended_review`), `learning/session_search.py`. Interface documented in
  `docs/notes/P09/pending-writes.md`.
- Shared: `foundation/config/schema.py` (`memory` section, `session_search.summarize`),
  `safety/threat_scan.py` (scopes memory, skill), `tools/toolsets.py` (toolsets memory,
  session_search added to CORE_TOOLS), `core/prompt/builder.py` (`BuildContext.memory_guidance`).
- MUST before landing: `memory` / `session_search` are in CORE_TOOLS but `cli/runtime.agent_factory`
  does not register them — wire registry (`load_builtins` + both `register_tools`), `inline=` executors
  (`memory_wiring(scope).execute`, `session_search_wiring(scope, agent.aux).execute`) and
  `options.builder = MemoryPromptBuilder(...)` (exact recipe in A's notes), or drop them from CORE_TOOLS.
- Two write-origin context vars exist (A: `learning/pending.py`; B: `learning/skills/usage.py`
  `WRITE_ORIGIN`) — keep one (A's, in the neutral module) and point B at it.
- Prompt order: P04's volatile tier keeps skills index → memory → user profile (pinned by
  `test_volatile_tier_order`); the phase row wants memory before the skills index (two lines in
  `builder._render` + that test).
- `skills.write_approval` config row: nobody added it (A expected B, B left it to A) — add it.
- Compare step (reopen reference, tick checklists) not done for A's five notes.

## Integration order (suggested)
A → B → C with `git apply --3way` on a fresh worktree off `main`; resolve the package-marker
add/add conflicts; then the wiring and fixes listed above; then wave 2 (P09-11, P09-13, P09-10 tests,
P09-14..20, P09-23, P09-24). D-ids for P09 start at D-16.136.
