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
- By: **Agent 2** | At: 2026-10-18 17:45 UTC | Branch: `main` (pushed to origin)
- Why now: Agent 2 at 71% of its window (resets 19:20 UTC); the next P05 wave would end near 90%
  mid-wave, while Agent 1 is fresh (reset 16:00), so a clean hand-back between waves is cheaper.
- Last verified code commit: `c7e5797` (P05-01..07: 4723 pytest passed, 3 skipped; pyright 0;
  lint-imports kept; pnpm lint/fmt/typecheck/test ok at `87719b2`; tooling.checks --require-ref ok, 11 checks).
- Agent 2's commits this turn: P02 `98fa4e8`, `928ce0b`; P04 `87719b2`, `cc17d69`, `4d1588d`,
  `246d691`, `2587135`; P05 `79695d2`, `c7e5797`.

## Usage at last stop
📊 Agent 1 usage: 1% used (reading 16:06 UTC), resets 21:00 UTC — READY
📊 Agent 2 usage: 71% used, 29% left (reading 17:41 UTC) — resets 19:20 UTC — PAUSED (handed back)

## Handoff history
| # | From → To | Time (UTC) | Steps done | Reason |
|---|-----------|------------|------------|--------|
| 1 | Agent 1 → Agent 2 | 2026-10-18 14:23 | P00, P01, P03, P02-01..07 | Agent 1 at ~75% usage; wave 4 is a clean start |
| 2 | Agent 2 → Agent 1 | 2026-10-18 17:45 | P02-08..23 (P02 done), P04 (done, D-04.1: asyncio kept), P05-01..07, 22..24 | Agent 2 at 71%; next wave would end near 90%; Agent 1 fresh |

## Progress
- [x] Stage 0 — intake decisions (`docs/plan/README.md`) — by Agent 1
- [x] Stage 1 — research: 10 area specs in `docs/plan/research/` — by Agent 1
- [x] Stage 2 — guards (`tooling/checks/`) and scaffold landed in `c790e93` (P00-01..07) — by Agent 1
- [x] Stage 3 — plan: inventory (495 features, 251 P0/P1), what-we-change (D-16.1..92),
  naming, architecture, roadmap, build plan, layout file, 26 phase files with 648 tasks
  (`docs/plan/phases/README.md` has the index and totals) — by Agent 1
- [~] Stage 4 — build: P00–P12 done (P12 at `b5aa7d7`); P13 wave 1 building; then P13 rest, P14

## Files changed (uncommitted or parked)
Nothing parked in `.agents/wip/`; nothing uncommitted.

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
- Last fully verified commit: `c7e5797` (P05-01..07; full gate green).
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
**Agent 2 holds the turn (since 2026-10-19 06:30 UTC).** Landed this turn: `6df7114` (gate green
without optional extras, as CI installs), `94d604e` (P10-19/20 → **P10 done**; P12-01 spike →
D-04.2 decided; P12-02..05 client; P12-28..31 bridge), `fa4fe5e` (P12-06..26 renderer), `c66c233`
(P12-27 app, P12-32 launcher), `b5aa7d7` (P12-33 Demo 2 → **P12 done**). Gate at `b5aa7d7`:
`pytest -m "not perf"` 8975 passed / 17 skipped, `-m perf` alone 11 passed, pyright 0, pnpm green
(854 JS tests), `pnpm --filter @indus/tui run e2e` 12 passed. Landing notes: LEAD-NOTES waves 7–9.
In flight: **P13 wave 1** — M1 (P13-01..04 MCP config, naming, transports, lifecycle; adds the `mcp`
SDK), M2 (P13-13..17 plugin manifest, discovery, loader, context, hooks bus), M3 (P13-20 content
scanner, P13-24 memory provider API). Contract: LEAD-NOTES gets it at landing; if the turn is PAUSED
look in `.agents/wip/p13w1/` for the parked slice patches (apply with `git apply --3way`, re-lock,
regenerate goldens if tools changed, run the full gate).
Next after wave 1: P13-05..12, 18, 19, 21..23, 25..28; then P14 (cron).
Open: D-04.2 (f) streaming fps re-bench on an unloaded Node 24 machine (26–28 fps on Node 22 here);
bridge follow-ups (LEAD-NOTES wave 8/9); owner items: Pillow (MIT-CMU), voice `av` (LGPL) and numpy
licences, Piper/eSpeak NG ruling (D-16.71), real-key P09 demo run, D-ids for computer_use differences.

## Watch out for
- Clean room: never copy reference code, comments or docs; task note before code; reopen the
  reference only for the Compare step. Evidence and captures stay in `~/rea-lab/`.
- `THIRD_PARTY_NOTICES.md` and `LICENSE` are allow-listed in `tooling/checks/similarity_allow.json`
  (licence texts). Never add an allow-list entry without a reason.

## Resume notes
<!-- Newest first. One line per resume. -->
- 2026-10-19 06:11 UTC — Agent 1 handed the turn to Agent 2 (owner's request); main `f303495` green, nothing in flight.
- 2026-10-18 14:26 UTC — Agent 2 resumed from Agent 1's handoff 1; gate green at `e62b76c` (1905 passed).
