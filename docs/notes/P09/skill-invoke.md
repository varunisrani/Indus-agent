# Task: Skill invocation as slash commands (P09-09) Phase: 09 Owner: P09 slice B builder

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/skill_commands.py` (stacking
cap and stop rule, how a loaded skill becomes one user message, config injection, auto-load),
`agent/skill_preprocessing.py` (template tokens, inline shell, the community-skill exclusion).
Closed before writing.
Evidence: none.

What it does (own words): every visible skill can be run as `/skill-name [instruction]`. Up to five
leading `/skill` words may be stacked (`/a /b fix the login page`); parsing stops at the first word
that is not an installed skill, so `/usr/bin/env`-style paths and later slash words stay part of
the instruction. The expansion is one new user message: a short frame naming what the user ran,
each skill's body (with its folder and support files named), any config values the skill declares
(a "skill config" block), then the user's instruction. The system prompt is never touched. Before
a body is sent, `${INDUS_SKILL_DIR}` and `${INDUS_SESSION_ID}` are replaced (on by default,
`skills.template_vars`), and — only when `skills.inline_shell` is on, and never for community hub
skills — each `` !`command` `` snippet is run (10 s limit each, environment from the P05 builder,
working directory = the skill folder) and replaced by its output. `skills.auto_load` names skills
loaded in full into every new session when its prompt is first built (missing names warn once and
are skipped; `INDUS_IGNORE_RULES` turns it off); `indus chat -s/--skill` preloads more the same
way. Each load bumps `use_count`.

Inputs → outputs: typed slash text, catalog, scope, session id → `Expansion(text, loaded,
missing)` or None (not a skill command); auto-load names → a prompt block.

Files: `src/indus/learning/skills/invoke.py`, `tests/unit/learning/skills/test_invoke.py`.

Edge cases / failure modes:
- the same skill typed twice → the second stops the stack (it becomes text);
- a stacked skill that vanished between listing and loading → named in `missing`, others load;
- ambiguous names are not slash commands (their bare name cannot load);
- inline shell: non-zero exit, no output, timeout, bash missing → a bracketed marker in place of the
  snippet, never an exception;
- config values: `skills.config.<key>` from the profile, else the declared default; none → no block;
- empty instruction → the frame asks the agent to apply the skill to the conversation so far.

Our design differences / decisions:
- the frame and markers are our own wording; skills are wrapped in `<skill name=… path=…>` blocks;
- the expansion is plain text; surfaces wire it in their own way (the classic CLI returns it as
  `SlashResult.submit`); `CommandTable` merges bundles over skills (bundle wins on the same slug);
- community detection reads the hub lock's `trust` field (P13 owns the hub; until then nothing is
  community);
- auto-load and preload produce one block that `SkillsIndexBuilder` appends after the index, so it
  is frozen with the prompt and rebuilt with it.

Tests we will write:
- stack of 5 expands, the 6th slash word stays text; `/notaskill/path` is not expanded; a repeated
  name stops the stack; unknown first word → None;
- inline shell off by default; on → the runner is called once with a 10 s limit; a timeout gives
  the marker; never for a community skill;
- template tokens substituted; skill config block; system prompt bytes unchanged after an
  invocation; auto-load missing name warns once; `INDUS_IGNORE_RULES` suppresses; `use_count`.

## Decisions for the lead
- The CLI `-s/--skill` flag and filling `SlashContext.extensions.skill_commands` are thin wiring in
  `cli/app.py` / `cli/launch.py` (shared files); see the report for what was done there.

Compare checklist (filled after the rewrite; reopened both files):
- [x] stacking cap 5 and stop rule (test: `tests/unit/learning/skills/test_invoke.py::test_stack_of_five`)
- [x] inline shell off by default, 10 s, never community (test: `::test_inline_shell_*`)
- [x] template tokens (test: `::test_template_tokens`)
- Deliberate differences: the reference keeps several markers to parse invocations back out of
  stored messages (for titles and history views); we do not parse our own frames back.
- Not taken: plugin skill commands for messaging surfaces (P13), the setup note for skipped
  secrets in slash expansions (the view tool covers it).

## Status

Done (2026-10-19). Shared: see report.

## Integration (lead, P09 wave 1 landing, 2026-10-19)

Integration: `cli/slash/skill_cmds.py` fills `Extensions.skill_commands` (skills and bundles) at REPL
start and the banner counts the skills; the existing global `-s/--skills` flag feeds
`auto_load_block(extra=...)` through `cli/runtime.agent_factory(preload=...)`. D-16.145.
