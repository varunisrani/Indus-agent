Wave 41 (P25 tooling, first slices) builds from main 34a3315. Landed: P00–P22, P23 (21/23; 15/16 signing open). P24 (47 tasks)
is being landed in parallel by the lead from .agents/wip/w40/p24-int-wip.patch — your tooling must work on today's tree AND pick
up P24's code when it lands (discover commands / config rows / tools from the code, never from hard-coded lists). Do not touch
files P24 changes unless your row owns them.
Read in full first: docs/plan/phases/P25-hardening-parity-release.md (your rows, Files, Fixtures, Traps, Acceptance, Run until
green, Done when), AGENTS.md, HANDOFF.md, docs/plan/phases/LEAD-NOTES.md (P23 + P20 sections), docs/plan/01-clean-room-protocol.md,
docs/plan/16-what-we-change.md §2.7 rows for P25, and the code your rows build on (tooling/checks, docs/, scripts/).

Slices (each owns only its tasks' files; stand in other slices' pieces only under tests/.../_stubs_<slice>.py with FIRST LINE
"# STAND-IN: removed at integration", and state the interface you assumed):
- Q1 = P25-05 parity runner (product side), P25-06 scripted model server for both apps, P25-07 product scenarios, P25-08
  obligation ledger, P25-09 compare, classify, report. The reference app is NEVER run or installed here: the runner drives OUR
  product against the scripted model server and records results in the format the row defines; reference-side captures, if
  the row needs them, are recorded as owed (evidence lives in ~/rea-lab on the owner's machine).
- D1 = P25-14 docs site build, P25-15 generated reference pages (CLI, config, tools, protocol — generated from the code),
  P25-16 user guide content (our own words; never copy reference docs), P25-17 THIRD_PARTY_NOTICES generation (from lock files
  + dist metadata; the hand-written file becomes generated + checked fresh).
- G1 = P25-10 acceptance map strict, P25-11 traps ledger complete (tooling + reports: list every unpinned row; do not invent
  tests for features you did not build — record gaps), P25-18 similarity `--closest` report, P25-19 clean-room sweep of built
  artefacts (wheels/sdist are refused by design: sweep docker/desktop/web build outputs produced in scratch dirs), P25-12 /
  P25-13 performance budgets (Python + TypeScript) with budgets from the row.
P25-01..04 (security), 20..24 (release) come after P24 lands.

Rules:
- ENV RULE: `export S=<your scratch dir>` as ONE statement, then `export HOME=$S/home XDG_CACHE_HOME=$S/home/.cache
  UV_CACHE_DIR=$S/home/.cache/uv npm_config_cache=$S/home/.npm` as a SECOND statement, BEFORE your first uv/pnpm command. Leave
  TMPDIR unset. Never write under the real ~, /root or /. Never `git stash`. pnpm store: /tmp/claude-0/-home-user/3c64a974-
  489c-5625-af4d-c59c9cfe7428/scratchpad/pnpm-store-p20 with --frozen-lockfile.
- New libraries only if the row names one (docs site generator etc.): dev-only, exact pins, uploaded >= 14 days before
  exclude-newer 2026-10-04, licence on licenses_allow.json; leave uv.lock / pnpm-lock to the lead and list each package.
- No credential-shaped literals; no real network; never pkill by pattern; stop every process you start.
- D-ids: do not number them; list intended differences under "Decisions for the lead" in each task note.
- Every task: note first in docs/notes/P25/<slug>.md; Read the reference paths in your row, close them, write; Compare after;
  tests per Verify; update only your trap/ACC rows. Clean room: never copy reference code, comments or docs.
- Work only in your worktree; never commit.
Before finishing: uv run ruff check <paths>, uv run ruff format --check <paths>, uv run pyright <paths>, uv run lint-imports,
uv run pytest <your test dirs> -q -p no:cacheprovider -m "not perf", uv run python -m tooling.checks --require-ref; TS checks if
you touch TS.
Usage: the lead may message you to wrap up. Then stop starting new tasks, get finished tasks passing, report done / partial.
Report: files (new / shared), task status, test/check results, wiring for the lead, decisions, packages + licences, follow-ups.
