P09 wave 2 (learning loop) is built in three parallel slices from a local base a9c60dd = main fb1204d
plus the three P09 wave 1 slice patches applied raw (memory/pending gate/session_search = A; skills
core = B; starter skills + bundled sync = C). The lead's integrator is fixing wave 1 IN PARALLEL in
another worktree (wiring memory/session_search/skills into cli/runtime.agent_factory, one write-origin
var kept in learning/pending.py, `skills.write_approval` config row, removing C's stand-in, help-skill
name, YAML boolean-name guard, bundle tests, D-16.136+). Do not do any of that; do not edit
cli/runtime.py. Read first: docs/plan/phases/P09-learning-loop.md (your task rows, files, traps,
acceptance), .agents/wip/p09w1/README.md (wave 1 status) and the wave 1 task notes in docs/notes/P09/.
Slices (each owns only its tasks' files):
- D: P09-11 `skill_manage` tool (writes go through learning/pending.py's gate: gate_on/decide/stage;
  origin from learning/pending.py's current_origin), P09-13 skill mutation ledger, P09-24 learning CLI
  and slash surface (`indus skills list/view/check/list-modified/...`, using C's bundled_sync API).
- E: P09-14 review counters and trigger, P09-15 review fork, P09-16 review prompts and outcome summary
  (our own words), P09-17 `/refine` and `/learn`.
- F: P09-18 curator phase 1, P09-19 curator phase 2 + backups, P09-20 `indus curator` and `/curator`,
  P09-23 learning timeline and map data.
Cross-slice: test-only stand-ins under tests/.../_stubs_<slice>.py with FIRST LINE "# STAND-IN:
removed at integration" (never in src/) for anything another slice owns. Hosting hooks (where the
session service or CLI runtime would call you): expose a small function and document the call site in
your task note under "Wiring for the lead" instead of editing cli/runtime.py.
Config rows: add only your tasks' rows to foundation/config/schema.py and its test (list them as shared).
D-ids: do not number them; list intended differences under "Decisions for the lead" in your notes.
pyproject.toml: only deps your tasks name (exact pins, uploaded before the exclude-newer date).
Every task: note first in docs/notes/P09/<slug>.md; Read the reference paths in the row, close them,
write; Compare after; tests per the Verify column (fake model/aux server, fake clock, tmp homes only —
never write under the real ~ or /root); update only your trap/ACC rows. No credential-shaped literals.
Before finishing: uv run ruff check <paths>, uv run ruff format --check <paths>, uv run pyright <paths>,
uv run lint-imports, uv run pytest <your test dirs> -q -p no:cacheprovider,
uv run python -m tooling.checks --require-ref.
Usage: the lead may message you to wrap up. On that message, stop starting new tasks, get what you
have to a passing state for the finished tasks, and report which tasks are done / partial.
