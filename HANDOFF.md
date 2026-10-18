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
- By: **Agent 1** | At: 2026-10-18 12:30 UTC | Branch: `main` (pushed to origin)
- Why now: no handoff yet; Agent 1 holds the turn. Plan complete; next is the first build wave.
- Last verified code commit: `ef25266` (P00 complete; 266 pytest, 50 vitest, all 9 checks ok).

## Usage at last stop
📊 Agent 1 usage: 8% used (reading 11:07 UTC), resets 16:00 UTC — ACTIVE (plan done)
📊 Agent 2 usage: unknown — READY

## Handoff history
| # | From → To | Time (UTC) | Steps done | Reason |
|---|-----------|------------|------------|--------|
| — | none yet | | | |

## Progress
- [x] Stage 0 — intake decisions (`docs/plan/README.md`) — by Agent 1
- [x] Stage 1 — research: 10 area specs in `docs/plan/research/` — by Agent 1
- [x] Stage 2 — guards (`tooling/checks/`) and scaffold landed in `c790e93` (P00-01..07) — by Agent 1
- [x] Stage 3 — plan: inventory (495 features, 251 P0/P1), what-we-change (D-16.1..92),
  naming, architecture, roadmap, build plan, layout file, 26 phase files with 648 tasks
  (`docs/plan/phases/README.md` has the index and totals) — by Agent 1
- [~] Stage 4 — build: P00 done (`c790e93`, `ef25266`); P01 done (`cee5f44`); wave 3 (P02-01..07, P03) built, integrating

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
- Last fully verified commit: `c790e93` (code); plan commits after it are docs only, gate green.
- Repo: https://github.com/varunisrani/indus-agent (`main`). REA approved and registered (rea-agents 6.7.0).
- Owner-open: trademark search (D-16.40), app id domain / GHCR owner /
  docs domain (P25-20).

## Decisions (and why)
- All intake and product decisions (names, pin, scope, stack, P1–P8, lead defaults) are in
  `docs/plan/README.md`; the clean-room variant (Read → Rewrite → Compare, lead, 2026-10-18) is in
  `docs/plan/01-clean-room-protocol.md`.
- Branch: `main`; both agents push to it, commits prefixed `[Agent 1]` / `[Agent 2]`, never force-push.

## Env vars needed (names only)
- `INDUS_REF` — path to the pinned reference checkout (default `/root/reference/hermes-agent`).

## NEXT STEP (start here)
1. Wave 3 (P02-01..07 store infra; P03 providers in 3 slices) is built; the four slice patches are in
   `.agents/wip/wave3/` (each with its `.base`). Agent 1 is integrating them in its verify worktree.
   If that did not land, re-integrate: apply w3-p02a, w3-p03a, w3-p03b1, w3-p03b2 in that order with
   `git apply --3way`, resolving shared files (pyproject, uv.lock → re-lock, foundation/errors.py
   union with one copy of ProviderHTTPError/ProviderStreamError, ledgers union), delete the test-only
   `_stubs_p03*.py` files, run the full loop, commit, delete `.agents/wip/wave3/`.
2. Then wave 4: P02-08..23 (sessions, listing, messages, rewind, compression records, titles, FTS,
   search, usage, export/import, delete, prune, browse, stress test, M0 doctor, session metadata).
3. Open lead decision: host SQLite 3.45.1 is in the WAL-reset bug range, so stores open in DELETE mode
   here; consider a uv-managed CPython with newer SQLite and/or an explicit override.

## Watch out for
- Clean room: never copy reference code, comments or docs; task note before code; reopen the
  reference only for the Compare step. Evidence and captures stay in `~/rea-lab/`.
- `THIRD_PARTY_NOTICES.md` and `LICENSE` are allow-listed in `tooling/checks/similarity_allow.json`
  (licence texts). Never add an allow-list entry without a reason.

## Resume notes
<!-- Newest first. One line per resume. -->
- none yet
