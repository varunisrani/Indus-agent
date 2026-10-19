P10 wave 1 (delegation and rich tools) is built in two parallel slices from main 85cd8a9 (P00–P08 and P11
landed). P09 (learning loop: memory, skills, curator) is being integrated IN PARALLEL in other
worktrees — do not touch src/indus/learning/, cli/runtime.py's prompt-builder wiring, or P09's config
rows. Read first: docs/plan/phases/P10-delegation-rich-tools.md (your task rows, files, traps,
acceptance) and docs/plan/phases/LEAD-NOTES.md (landing notes P04–P08, P11: seams for the turn loop,
inline tools, tool registry/toolsets, process registry, backends, aux client, sessions store).
Slices (each owns only its tasks' files):
- G: P10-01 delegate_task schema, P10-02 child agent builder, P10-03 synchronous batch runner,
  P10-04 output schema validation, P10-05 background delegation, P10-06 child teardown and hand-off,
  P10-07 optional git worktree per subagent.
- H: P10-08 execute_code runner and RPC, P10-09 in-script module and schema, P10-10 persistent
  session kernel (local), P10-11 remote kernels and script shipping (Docker/SSH over P11's backends
  and its fake docker/ssh clients in tests/fakes/backend/).
Later waves (not yours): P10-12..26.
Cross-slice: test-only stand-ins under tests/.../_stubs_<slice>.py with FIRST LINE "# STAND-IN:
removed at integration" (never in src/) for anything another slice owns.
Config rows: add only your tasks' rows to foundation/config/schema.py and its test (list them as shared).
D-ids: do not number them; list intended differences under "Decisions for the lead" in your notes.
pyproject.toml: only deps your tasks name (exact pins, uploaded before the exclude-newer date).
Every task: note first in docs/notes/P10/<slug>.md; Read the reference paths in the row, close them,
write; Compare after; tests per the Verify column (fake model server, fake clock, tmp homes only —
never write under the real ~ or /root or /tmp outside the test's own tmp dir); update only your
trap/ACC rows. No credential-shaped literals.
Before finishing: uv run ruff check <paths>, uv run ruff format --check <paths>, uv run pyright <paths>,
uv run lint-imports, uv run pytest <your test dirs> -q -p no:cacheprovider,
uv run python -m tooling.checks --require-ref.
Usage: the lead may message you to wrap up. On that message, stop starting new tasks, get what you
have to a passing state for the finished tasks, and report which tasks are done / partial.
