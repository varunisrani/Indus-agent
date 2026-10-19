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
- [~] Stage 4 — build: P00 done (`c790e93`, `ef25266`); P01 done (`cee5f44`); P03 + P02-01..07 done (`ee74300`); **P02 done** (`98fa4e8`, `928ce0b`); **P04 done** (`87719b2`…`2587135`, D-04.1 decided: asyncio core kept); P05-01..07, 22..24 done (`79695d2`, `c7e5797`); next P05-08..21, then P06 (Demo 1)

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
Builder rules: `docs/plan/phases/builder-rules.md`; landing notes: `docs/plan/phases/LEAD-NOTES.md`.
Done: P00–P09, P11, and P10 except P10-19/P10-20 (254 of 648 tasks). `main` = `f303495`, full gate green
(`pytest -m "not perf"` 8606 passed / 9 skipped; `pytest -m perf` alone 10 passed; ruff, pyright,
lint-imports, tooling.checks --require-ref, pnpm test). Landing notes for every slice are in LEAD-NOTES.
Turn: AGENT_2 (handed over by Agent 1 at 06:11 UTC on 2026-10-19 at the owner's request). Nothing in
flight, no wip patches, no open worktrees besides the lead's.
Agent 2, start here:
1. Pull main, run the full gate once (commands in CLAUDE.md; perf tests run alone after the rest).
2. Build P10-19 CLI voice mode and P10-20 computer_use (docs/plan/phases/P10-delegation-rich-tools.md),
   one builder each; land behind the gate. Then mark P10 done in docs/plan/phases/README.md.
3. P12 TUI (Demo 2): read docs/plan/phases/P12-tui-demo2.md, slice it into waves, build and land.
   Then P13 (MCP/plugins/hooks), P14 (cron).
How we work: docs/plan/phases/builder-rules.md (builders), LEAD-NOTES.md (seams, gate change),
`.agents/wip/<phase>/` for parked slice patches, `[Agent 2] …` commit prefix, usage guard bands
(75% no new builders, 85% wrap up, 90% hand back: set turn.json to AGENT_1 and push).
Owner items open (LEAD-NOTES "P10 slices I+J+K landing notes"): Pillow licence (MIT-CMU); voice extra's
`av` (LGPL FFmpeg) and numpy (Zlib); Piper/eSpeak NG ruling (D-16.71); real-key P09 demo run.
Open security seam: during a browser navigation Chromium follows redirect hops itself, so one GET can
still reach a refused address if a server answers our check differently; a filtering local proxy
would close it.

## Watch out for
- Clean room: never copy reference code, comments or docs; task note before code; reopen the
  reference only for the Compare step. Evidence and captures stay in `~/rea-lab/`.
- `THIRD_PARTY_NOTICES.md` and `LICENSE` are allow-listed in `tooling/checks/similarity_allow.json`
  (licence texts). Never add an allow-list entry without a reason.

## Resume notes
<!-- Newest first. One line per resume. -->
- 2026-10-19 06:11 UTC — Agent 1 handed the turn to Agent 2 (owner's request); main `f303495` green, nothing in flight.
- 2026-10-18 14:26 UTC — Agent 2 resumed from Agent 1's handoff 1; gate green at `e62b76c` (1905 passed).
