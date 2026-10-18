# HANDOFF — Indus Agent (clean-room rebuild of hermes-agent)
(Live lock and usage: `.agents/turn.json`, `.agents/agent-1.json`, `.agents/agent-2.json`)

<!-- The turn holder keeps this file current after every step and fills it in completely at a
     handoff, so an agent with only this repository can carry on. Secret names only, never values.
     Account labels only ("Account 1", "Account 2"), never email addresses. Times in UTC. -->

## Goal
Rebuild hermes-agent (`https://github.com/NousResearch/hermes-agent` at
`ab3fe9dffb0a89d534cb795e2a2940e5f6542056`) as **Indus Agent**, at full parity: same features and
architecture, our own code. Clean room: Read → Rewrite → Compare, with guards in CI that forbid
copying (`docs/plan/01-clean-room-protocol.md`). Code lives in `src/indus/` (Python, uv) and later
`apps/`; the plan in `docs/plan/`; the guards in `tooling/checks/`.

## Last stop
- By: **Agent 1** | At: 2026-10-18 10:47 UTC | Branch: `main` | Commit: P00 not yet landed
- Why now: no handoff yet; Agent 1 holds the turn and is working.
- Last verified code commit: none yet (P00 scaffold and guards are being landed).

## Usage at last stop
📊 Agent 1 usage: unknown — ACTIVE (P00 foundations)
📊 Agent 2 usage: unknown — READY

## Handoff history
| # | From → To | Time (UTC) | Steps done | Reason |
|---|-----------|------------|------------|--------|
| — | none yet | | | |

## Progress
- [x] Stage 0 — intake decisions (`docs/plan/README.md`) — by Agent 1
- [x] Stage 1 — research: 10 area specs in `docs/plan/research/` — by Agent 1
- [~] Stage 2 — guards (`tooling/checks/`) and scaffold (P00: package, rules files, CI, mailbox);
  built in parallel worktrees, not yet landed — by Agent 1
- [~] Stage 3 — planning: product decisions P1–P8 recorded; inventory, what-we-change,
  architecture, roadmap and phase files still to write — by Agent 1
- [ ] Stage 4 — build phases

## Files changed (uncommitted or parked)
Nothing parked in `.agents/wip/`.

## Commands
- Install: `uv sync` (uv 0.11, Python 3.13 from `.python-version`)
- Run: `uv run indus --version`
- Test, the full loop: `uv run ruff check . && uv run ruff format --check . && uv run pytest -q`,
  then `uv run python -m tooling.checks --require-ref`
- Reference (outside the repo): `git clone https://github.com/NousResearch/hermes-agent.git
  /root/reference/hermes-agent && git -C /root/reference/hermes-agent checkout
  ab3fe9dffb0a89d534cb795e2a2940e5f6542056`; set `INDUS_REF` if it lives elsewhere.
- Processes stopped: none running.

## Current state
- Last fully verified commit: none yet.
- Known bugs / follow-ups: the product licence is undecided (owner's call; no `LICENSE` file yet).

## Decisions (and why)
- All intake and product decisions (names, pin, scope, stack, P1–P8, lead defaults) are in
  `docs/plan/README.md`; the clean-room variant (Read → Rewrite → Compare, lead, 2026-10-18) is in
  `docs/plan/01-clean-room-protocol.md`.
- Branch: `main`; both agents push to it, commits prefixed `[Agent 1]` / `[Agent 2]`, never force-push.

## Env vars needed (names only)
- `INDUS_REF` — path to the pinned reference checkout (default `/root/reference/hermes-agent`).

## NEXT STEP (start here)
1. Land P00: the scaffold (this file, package, rules, CI, mailbox) and the guards in
   `tooling/checks/`; run the full loop including `--require-ref`; push to `main`; confirm CI green.
2. Finish the plan: feature inventory, what-we-change, architecture, roadmap, then the phase files
   in `docs/plan/phases/`.
- Needs the owner: the product licence; REA registration approval.

## Watch out for
- Clean room: never copy reference code, comments or docs; task note before code; reopen the
  reference only for the Compare step. Evidence and captures stay in `~/rea-lab/`.
- `THIRD_PARTY_NOTICES.md` reproduces the reference's licence verbatim on purpose; the similarity
  guard needs an allow-list entry for it.

## Resume notes
<!-- Newest first. One line per resume. -->
- none yet
