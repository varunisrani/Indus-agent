# Rules for every Indus Agent builder

Indus Agent is a clean-room rebuild of the reference agent (hermes-agent, MIT). The repo is a Python
project managed with uv (package `indus` under `src/indus/`; guards under `tooling/checks/`; desktop,
TUI and web are TypeScript under `apps/` later). The plan lives in `docs/plan/` (research specs in
`docs/plan/research/`, phase files in `docs/plan/phases/`).

## Where you work
- Only inside your own worktree (its path is in your task). Never touch /home/user/indus-agent (the
  main checkout), the lead's verify folder or another builder's worktree.
- Do **not** commit, stash, reset, rebase or push. Leave changes uncommitted; the lead takes a diff.
- Add only the dependencies your task names (exact pins, ≥14 days old). uv.lock / pnpm-lock.yaml are lead-owned: you may regenerate them in your worktree to test, the lead re-locks after merging.
- Own only the files your task names, plus new files you create for it. If you must touch another
  file, keep the change minimal and list it in your report as "shared".

## Clean room (CI enforces it)
- Never put a credential-shaped literal (glpat-, ghp_, sk-, xox*, AKIA…) in tests or fixtures: build fake tokens at runtime by concatenation. GitHub push protection rejects them.
- Never copy reference code, comments, docs, strings, tests or assets. The reference checkout is at
  `/root/reference/hermes-agent` (pinned ab3fe9d), outside the repo. Read it in the Read step, close
  it while writing, reopen only for the Compare step. Write everything in your own words and structure.
- Never write `hermes`, `Hermes`, `HERMES_`, `nous`, `Nous`, `NousResearch` in product code or tests
  (`src/`, `apps/`, `tooling/` except the banned-list data and its tests, `scripts/`). Our names:
  product "Indus Agent", CLI `indus`, env prefix `INDUS_`, home `~/.indus`.
- Write the task note first: `docs/notes/<phase>/<slug>.md` (Task / Phase / Owner / Read: paths;
  then what, inputs/outputs, edge cases, our differences, tests planned; compare checklist after).

## Decisions
- Decisions in your task are final. If one you need is missing, take the smallest choice that is easy
  to change, write it under "Decisions for the lead" in your task note, and go on.

## The machine is shared (4 cores, up to 4 agents)
- Run only the tests you touch: `uv run pytest <paths> -q -p no:cacheprovider` (no xdist).
- A test that fails under load is not "a flake" until it also fails, or passes, run alone.
- Before you finish: `uv run ruff check <your paths>`, `uv run ruff format --check <your paths>`,
  your targeted tests, and (once it exists) `uv run python -m tooling.checks --require-ref`.

## Style
- Python 3.13+, type hints, small modules, plain names. Match surrounding code. No drive-by refactors.

## Final report (short)
1. Files changed, "shared" ones marked. 2. Per task id: done / partly / not, one line each.
3. Tests and checks run with exact counts; "not run" and why otherwise. Never claim an unrun pass.
4. For the lead: intended differences, decisions taken, follow-ups found.
