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
- By: **Agent 2** (holds the turn, ACTIVE) | At: 2026-10-18 15:50 UTC | Branch: `main` (pushed to origin)
- Agent 2 took the turn at 14:26 UTC (`782a431`).
- Last verified code commit: `87719b2` (wave 5, P04-01..12 + 17: 3292 pytest passed, 3 skipped; pyright 0; pnpm lint/fmt/typecheck/test ok; 10 checks ok). P02 complete at `928ce0b`.

## Usage at last stop
📊 Agent 1 usage: 8% used (reading 11:07 UTC), resets 16:00 UTC — ACTIVE (plan done)
📊 Agent 2 usage: 30% used, 70% left (reading 15:26 UTC) — resets 19:20 UTC — ACTIVE

## Handoff history
| # | From → To | Time (UTC) | Steps done | Reason |
|---|-----------|------------|------------|--------|
| 1 | Agent 1 → Agent 2 | 2026-10-18 14:23 | P00, P01, P03, P02-01..07 | Agent 1 at ~75% usage; wave 4 is a clean start |

## Progress
- [x] Stage 0 — intake decisions (`docs/plan/README.md`) — by Agent 1
- [x] Stage 1 — research: 10 area specs in `docs/plan/research/` — by Agent 1
- [x] Stage 2 — guards (`tooling/checks/`) and scaffold landed in `c790e93` (P00-01..07) — by Agent 1
- [x] Stage 3 — plan: inventory (495 features, 251 P0/P1), what-we-change (D-16.1..92),
  naming, architecture, roadmap, build plan, layout file, 26 phase files with 648 tasks
  (`docs/plan/phases/README.md` has the index and totals) — by Agent 1
- [~] Stage 4 — build: P00 done (`c790e93`, `ef25266`); P01 done (`cee5f44`); P03 + P02-01..07 done (`ee74300`); P02 done (`98fa4e8`, `928ce0b`, Agent 2); P04-01..12 + 17 done (`87719b2`); wave 6a = P04-13..16, 18, 19, 22, 23 (route), 21 (lock + broker) building

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
**Agent 2, in flight (15:50 UTC):** wave 6a builders: X = P04-13/14/15/16/18/19 (core loop),
Y = P04-22 golden + P04-23 RouteBinding, Z = P04-21 session lock + server-request broker. Then wave
6b = P04-20 Agent, rest of P04-21 (SessionService, InProcessHost), P04-23 D-04.1 criteria + decision;
then P05, P06 (Demo 1). Landing notes per wave: `docs/plan/phases/LEAD-NOTES.md`. If you find this
mid-wave with the turn PAUSED, look in `.agents/wip/` for saved slices.

Previous plan (wave 4 is done):
Builder rules for sub-agents: `docs/plan/phases/builder-rules.md` (replace `<scratch>` with your own scratch dir). Lead notes and cross-phase rulings: `docs/plan/phases/LEAD-NOTES.md`.
1. Wave 4: P02-08..23 (sessions, listing, messages, rewind/clear/branch, compression records, titles,
   FTS5 + fail-open, search, usage, export/import, delete/tombstones, prune/VACUUM, read-only browse +
   integrity, D-04.6 stress test, M0 doctor report, per-session metadata API). Build on the landed
   schema (`src/indus/state/schema.py`, documented in `docs/notes/P02/schema.md`), writer and registry.
   Suggested slices: (a) 08-13 + 23, (b) 14-16, (c) 17-20, (d) 21-22.
2. Then P04 (turn loop + tool contract), P05 (core tools), P06 (classic CLI → Demo 1).
3. Open lead decision: host SQLite 3.45.1 is in the WAL-reset bug range, so stores open in DELETE mode
   here; consider a uv-managed CPython with newer SQLite and/or an explicit override.
4. Open follow-ups: record live provider captures (P03-11; `scripts/dev/record_capture.py`, needs API
   keys); add `hypothesis` + `pytest-asyncio` to the dev group (pins within exclude-newer).

## Watch out for
- Clean room: never copy reference code, comments or docs; task note before code; reopen the
  reference only for the Compare step. Evidence and captures stay in `~/rea-lab/`.
- `THIRD_PARTY_NOTICES.md` and `LICENSE` are allow-listed in `tooling/checks/similarity_allow.json`
  (licence texts). Never add an allow-list entry without a reason.

## Resume notes
<!-- Newest first. One line per resume. -->
- none yet
