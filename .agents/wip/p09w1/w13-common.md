P09 wave 1 (learning loop) is built in three parallel slices from base 29ddd7b (P00–P08 and P11
landed). Read docs/plan/phases/P09-learning-loop.md in full (tasks, files, fixtures, traps,
acceptance) and docs/plan/phases/LEAD-NOTES.md (landing notes for P04–P08 and P11) first: the seams
for the prompt builder, inline agent-level tools, slash registry, aux client, profiles and config schema
are listed there. Do not touch src/indus/backends/ or src/indus/providers/.
Slices (each owns only its tasks' files from the phase file's "Files to create/update"):
- A: P09-01 memory store, P09-02 `memory` tool, P09-03 pending writes + write-approval gate,
  P09-04 frozen memory block in the prompt, P09-25 `session_search` tool.
- B: P09-05 skill format/validation/linter, P09-06 discovery and resolution, P09-07 skills index +
  mid-session delta, P09-08 skills_list/skill_view, P09-09 skills as slash commands, P09-10 bundles,
  P09-11 skill_manage, P09-12 usage telemetry + provenance, P09-13 mutation ledger. Skill writes go
  through A's pending-write gate (P09-03): use a stand-in for it.
- C: P09-22 the fifteen starter skills (authored fresh, our own words, MIT frontmatter; never open the
  reference's skill texts beyond what the phase file allows for the Read step, and never reuse their
  wording) and P09-21 bundled skill seeding and sync. Use B's SkillFrontmatter fields exactly as the
  phase file defines them; validate your skills with a stand-in validator if B's is not there.
Wave 2 (later, not yours): P09-14..20, P09-23, P09-24.
Cross-slice: use test-only stand-ins under tests/.../_stubs_<slice>.py with FIRST LINE "# STAND-IN:
removed at integration" (never in src/) for anything another slice owns.
Config rows: add only your tasks' rows to foundation/config/schema.py and its test (list them as shared).
pyproject.toml: only deps your tasks name (exact pins, uploaded before the exclude-newer date).
Every task: note first in docs/notes/P09/<slug>.md; Read the reference paths in the row, close them,
write; Compare after; tests per the Verify column (fake model/aux server, fake clock, tmp homes only —
never write under the real ~ or /root); update only your trap/ACC rows. No credential-shaped literals.
Before finishing: uv run ruff check <paths>, uv run ruff format --check <paths>, uv run pyright <paths>,
uv run lint-imports, uv run pytest <your test dirs> -q -p no:cacheprovider,
uv run python -m tooling.checks --require-ref.
Usage: the lead may message you to wrap up. On that message, stop starting new tasks, get what you
have to a passing state for the finished tasks, and report which tasks are done / partial.
