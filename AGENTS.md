# Working in the Indus Agent repository

These rules bind every agent and every person who changes this repository. Indus Agent is a
clean-room rebuild: it must behave like its reference while every line in it is our own. The full
protocol is in [`docs/plan/01-clean-room-protocol.md`](docs/plan/01-clean-room-protocol.md); this
file is the short version you must follow.

## Layout

| Path | What lives there |
|---|---|
| `src/indus/` | the Python package (CLI `indus`) |
| `tests/` | product tests |
| `tooling/checks/` | the clean-room guards and their tests |
| `docs/plan/` | the plan: decisions, research specs, phase files |
| `docs/notes/<phase>/<slug>.md` | one task note per task, written before code |
| `docs/internals/` | ledgers and reports (traps, acceptance map, similarity report) |
| `.agents/`, `HANDOFF.md` | the two-agent mailbox and handoff state |
| `apps/` | TypeScript surfaces (TUI, desktop, web), later |

## The reference

- The reference is hermes-agent, pinned at `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`.
- Locally it lives at `/root/reference/hermes-agent`, **outside this repo, read-only**. Point the
  guards at it with `INDUS_REF=/root/reference/hermes-agent` (that is also the default).
- In CI it is fetched at the pinned commit into `$RUNNER_TEMP/ref`, never into the workspace.
- Never copy, vendor, submodule or symlink any part of it into this repository.
- REA Evidence (bundle analysis, decompiled code, captures, screenshots, DB snapshots) and captures
  of the running reference stay in `~/rea-lab/`. Never commit them; cite only an `evidence_id`.

## The loop for every task: Read → Note → Rewrite → Compare → Check

1. **Read** the reference files your task lists (docs first, then code, then its tests), the area
   spec in `docs/plan/research/`, and any Evidence for the task.
2. **Note.** Write `docs/notes/<phase>/<slug>.md` from [`docs/notes/_template.md`](docs/notes/_template.md)
   in your own words, before any code. Its `Read:` line is the audit trail.
3. **Rewrite** with the reference closed: no reference file or Evidence on screen. Work from the note
   and the plan, with our names and our structure.
4. **Compare.** Reopen the reference, walk it branch by branch, and close each gap in your own way.
   Tick the compare checklist in the note. Never paste to fill a gap.
5. **Check.** Tests, lint, and the guards (below). A failure sends you back to step 3.

## Names

- Product **Indus Agent**, CLI and package `indus`, env prefix `INDUS_`, home directory `~/.indus`.
- The reference's names are banned identifiers in product code (`src/`, `apps/`, `scripts/`,
  `tooling/` except the banned-list data and its tests). The banned-identifier check fails the
  build on any of them and prints the name to use instead.
- Reference names may appear only in `README.md`, `AGENTS.md`, `CLAUDE.md`,
  `THIRD_PARTY_NOTICES.md` and `docs/`.
- Vendor-standard names stay as the vendor spells them (for example `TELEGRAM_BOT_TOKEN`).

## Commands

```bash
uv sync                                         # install the dev environment
uv run ruff check .                             # lint
uv run ruff format --check .                    # formatting (drop --check to fix)
uv run pytest -q                                # tests
uv run python -m tooling.checks --require-ref   # all clean-room guards, reference required
```

Without `--require-ref`, a missing reference makes the guards warn and skip. Every gate that counts
(CI, release, "done") uses `--require-ref`.

## Commits and pull requests

- Prefix every commit with `[Agent N]` (your agent number) and set `git config user.name "Agent N"`.
- Never force-push and never rewrite pushed history.
- In `.agents/`, edit only your own files (`agent-N.json`, `usage-log-agent-N.md`), plus
  `turn.json` and `HANDOFF.md` while you hold or hand over the turn.
- Every PR body carries a `Reference:` line (the reference files and Evidence ids read, or `none`
  with the reason) and a `Task note:` line (the note's path). CI checks both.
- Account labels in any file are `Account 1` / `Account 2`; never write an email address.

## Definition of done

A task is done when all five hold:

1. Its task note exists, and its `Read:` line lists every reference file and Evidence id read.
2. The code was written from scratch with our names, and the note's compare checklist is complete.
3. Tests pass, including the edge cases drawn from the reference's tests and the spec's traps.
4. `uv run python -m tooling.checks --require-ref` is green.
5. For UI tasks, the parity capture has been reviewed and every divergence classified.
