P14 (cron) is built in three parallel slices from main adaceac (P00–P12 done; P13 wave 1 landed, P13
wave 2 being integrated in parallel — do not touch src/indus/mcp/, src/indus/plugins/ or
learning/skills/hub). Read first: docs/plan/phases/P14-cron.md in full (tasks, files, traps,
acceptance, decisions P6 / D-16.36 / D-16.54 — bare durations repeat), docs/plan/phases/LEAD-NOTES.md
(landing notes: session service, inline tools, aux client, delivery seams, P09 learning host,
P10 delegation/automation, P12 TUI bridge, gate change) and HANDOFF.md.
Slices (each owns only its tasks' files):
- C1: P14-01 schedule grammar, P14-02 next-run computation, P14-03 job model and store, P14-04
  execution ledger, P14-05 store health, P14-06 due detection / lateness / catch-up, P14-17 per-job
  notepad (lives in executions.db). C1 defines the shared models: keep them small and documented in
  the task notes early, because C2 and C3 build on them in parallel.
- C2: P14-07 ticker (`indus cron tick` / `indus cron daemon`), P14-08 preflight, P14-09 script runner
  and wake gate, P14-10 prompt assembly, P14-11 agent job runner, P14-12 result classification and
  output document.
- C3: P14-13 delivery targets and local delivery, P14-14 bookkeeping / failure streak / quota hold,
  P14-15 incidents, P14-16 monitor mode, P14-18 cronjob_manage tool, P14-19 `indus cron` CLI,
  `/cron`, status and doctor.
Cross-slice: test-only stand-ins under tests/.../_stubs_<slice>.py with FIRST LINE "# STAND-IN:
removed at integration" (never in src/) for anything another slice owns (C2/C3 stand in C1's models
and store until integration).
P09 seams: cron runs must never trigger the review loop or curator writes as if foreground (pass
no_review), and project-skill trust binds the job workdir via
learning/skills/project_trust.bind_project_cwd once P13 lands — leave a one-line hook and document it.
Config rows: only your tasks' rows in foundation/config/schema.py and its test (list them as shared).
D-ids: do not number them; list intended differences under "Decisions for the lead" in your notes.
pyproject.toml: only deps your tasks name (exact pins, before the exclude-newer date); CI installs no
optional extras. Leave uv.lock to the lead.
Every task: note first in docs/notes/P14/<slug>.md; Read the reference paths in the row, close them,
write; Compare after; tests per the Verify column (fake model server, fake clock and injected time
zones, tmp homes only — never write under the real ~ or /root, no external network); update only your
trap/ACC rows. No credential-shaped literals.
Before finishing: uv run ruff check <paths>, uv run ruff format --check <paths>, uv run pyright <paths>,
uv run lint-imports, uv run pytest <your test dirs> -q -p no:cacheprovider -m "not perf",
uv run python -m tooling.checks --require-ref.
Usage: the lead may message you to wrap up. On that message, stop starting new tasks, get what you
have to a passing state for the finished tasks, and report which tasks are done / partial.
