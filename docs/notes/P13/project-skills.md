# Task: Project skills — trust and quarantine (P13-28, F12) Phase: 13 Owner: wave-2 builder N4

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `agent/skill_utils.py` (project
root walk-up, trusted dirs, untrusted count, scan quarantine). Closed before writing.
Research: `docs/plan/research/07-learning-skills-plugins-cron.md` §3.6, trap R07.26.

What it does (own words): `indus.learning.skills.project_trust` fills the project tier P09-06
reserved. The project root is the nearest ancestor of the surface's working directory holding
`.git` (a dir, or a file for worktrees/submodules), walking at most 64 levels; the user's home
itself never counts. Skills live under `<root>/.indus/skills/` and `<root>/.agents/skills/` (the
profile's own skills dir is never treated as a project dir). They load only when the root is in
`skills.trusted_project_dirs`; `indus skills trust-project [path]` adds it, and interactive
surfaces may ask once (`offer_trust`). `skills.project_discovery: false` turns the tier off.
Every project skill is scanned with `content_scan` before indexing; reports are cached by content
hash under `<home>/cache/project_skill_scans/` (never in the repo) and rerun on any change.
`dangerous` → quarantined: hidden from the index, list and slash commands; loading it by name
raises `SkillQuarantined` with the reason. A scan failure quarantines (fail closed). Untrusted
roots: skills are counted (banner line) but never parsed into the catalog. Non-interactive
surfaces (cron, API, ACP, gateways) never prompt and inherit the trust list; the root follows the
surface's working directory (`bind_project_cwd(path)`, e.g. cron `workdir`), else
`terminal.cwd`, else the process cwd. Project skills show `[project]` in the prompt index; the
curator never touches them (they are not in the local root) and agent-created skills never land
there (`write_root` is never a project dir).

Edge cases: 65-level-deep cwd stops walking (no root); symlinked project dir resolved once; a
project dir that equals `<home>/skills` skipped; same-named project skill shadows the profile copy
(tier 0) — but a quarantined one does not shadow anything.

Our differences: `.indus/skills/` instead of the reference's product dir; scan cache keyed by
(content hash, scanner id) and rerun on change within one process too; quarantined skills
explain themselves on load.

Tests planned (`tests/unit/learning/skills/test_project_trust.py`, fixture repo
`tests/fixtures/repos/project_skills/`): untrusted → counted, not indexed; trust → indexed with
`[project]`; editing a trusted skill re-scans; dangerous quarantined and load refused; cron in an
untrusted repo never prompts and loads nothing; 65-level-deep cwd stops walking.

## Decisions for the lead
- Declined one-time prompts are remembered per root in `<home>/cache/project_skill_prompts.json`.
- Interactive surfaces: `cli`, `tui`, `desktop`; every other platform name is non-interactive.
- The fixture repo commits its dangerous skill (`.indus/skills/cleanup`, one `rm -rf /` line, the
  same text the scanner corpus already holds); tests copy the repo and add `.git` at run time.
- The banner row and the one-time question live in `cli/launch.py` (`project_skills_check`); a
  "no" is remembered per root; Ctrl+C means "ask again next time".
- `SkillsSettings` gains `quarantined`, `project_root`, `project_pending`; resolver status
  `quarantined` and error `SkillQuarantined`; `skills list --source project`.

Compare checklist (after writing; reopened the file above):
- [x] Walk-up bound 64, `.git` file or dir, home excluded.
- [x] Profile skills dir excluded from project dirs.
- [x] Scan cache outside the repo; fail closed.
- [x] Unattended surfaces inherit trust, never prompt (R07.26).
