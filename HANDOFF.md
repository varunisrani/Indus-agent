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
- By: **Agent 2** | At: 2026-10-19 10:15 UTC | Branch: `main` (pushed to origin)
- Why now: Agent 2 at 78% of its window (resets 11:00 UTC); P13 wave 1 is landed and nothing is in
  flight, so the next wave goes to Agent 1 (fresh since its 07:00 reset).
- Last verified code commit: `78fdc96` (P13 wave 1: `pytest -m "not perf"` 9436 passed / 17 skipped;
  `-m perf` alone 11 passed; pyright 0; lint-imports kept; tooling.checks --require-ref ok; pnpm green
  at `b5aa7d7` with 854 JS tests and the TUI e2e lane 12 passed).
- Agent 2's commits this turn: `6df7114` (CI gate without extras), `94d604e` (P10 done, P12-01..05,
  28..31), `fa4fe5e` (P12-06..26), `c66c233` (P12-27, 32), `b5aa7d7` (P12-33, **P12 done**),
  `78fdc96` (P13-01..04, 13..17, 20, 24).

## Usage at last stop
📊 Agent 1 usage: fresh after its 07:00 UTC reset — READY
📊 Agent 2 usage: 78% used, 22% left (reading 09:56 UTC) — resets 11:00 UTC — READY (handed back)

## Handoff history
| # | From → To | Time (UTC) | Steps done | Reason |
|---|-----------|------------|------------|--------|
| 1 | Agent 1 → Agent 2 | 2026-10-18 14:23 | P00, P01, P03, P02-01..07 | Agent 1 at ~75% usage; wave 4 is a clean start |
| 2 | Agent 2 → Agent 1 | 2026-10-18 17:45 | P02-08..23 (P02 done), P04 (done, D-04.1: asyncio kept), P05-01..07, 22..24 | Agent 2 at 71%; next wave would end near 90%; Agent 1 fresh |
| 3 | Agent 2 → Agent 1 | 2026-10-19 10:15 | P10-19/20 (P10 done), P12 all (Demo 2), P13-01..04, 13..17, 20, 24; CI gate without extras | Agent 2 at 78%; wave boundary; Agent 1 fresh |

## Progress
- [x] Stage 0 — intake decisions (`docs/plan/README.md`) — by Agent 1
- [x] Stage 1 — research: 10 area specs in `docs/plan/research/` — by Agent 1
- [x] Stage 2 — guards (`tooling/checks/`) and scaffold landed in `c790e93` (P00-01..07) — by Agent 1
- [x] Stage 3 — plan: inventory (495 features, 251 P0/P1), what-we-change (D-16.1..92),
  naming, architecture, roadmap, build plan, layout file, 26 phase files with 648 tasks
  (`docs/plan/phases/README.md` has the index and totals) — by Agent 1
- [~] Stage 4 — build: P00–P12 done; P13 wave 1 done (`78fdc96`); next P13 wave 2, then P14

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
- Last fully verified commit: `78fdc96` (P13 wave 1; full gate green).
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
Turn: AGENT_1 (took it at 10:19 UTC 2026-10-19). In flight: P13 wave 2 in four slices from `2f62d28` —
N1 = P13-05..08, N2 = P13-09..12, N3 = P13-18, 19, 21..23, N4 = P13-25..28. If paused, patches are in
`.agents/wip/p13w2/`.
Builder rules: `docs/plan/phases/builder-rules.md`; read LEAD-NOTES landing
notes for waves 7–10 first (every seam and follow-up is there). Nothing in flight, `.agents/wip/` empty.
Gate (CLAUDE.md): note CI installs **no optional extras** — keep optional imports pyright-ignored and
pin probes in tests that need an extra; perf tests run alone (`-m perf`), and `tests/conftest.py`
freezes collected objects out of the GC for perf runs. TUI e2e: `pnpm --filter @indus/tui run e2e`
(needs `node-gyp` for the optional `node-pty`).
1. **P13 wave 2** (spec `docs/plan/phases/P13-mcp-plugins-hooks-hub.md`): P13-05..12 (MCP discovery
   + registration, calls, trust/breaker, reload/list_changed, OAuth, sampling/elicitation, OSV
   preflight, `indus mcp` CLI) on M1's connection API; P13-18 prompt sections, P13-19 shell hooks +
   `indus hooks`; P13-21..23 plugin install/consent/CLI (use `content_scan`); P13-25..28 skills hub +
   project-skill trust. Wire the deferred seams: `pre_llm_call` in `core/prologue.py`, plugin
   `approve` with P13-07, memory provider manager into the session host, plugin skills/commands.
2. Then P14 (cron).
3. Open: D-04.2 (f) streaming fps re-bench on an unloaded Node 24 machine (26–28 fps on Node 22 here);
   bridge follow-ups (LEAD-NOTES waves 8/9: launch options in `session.create`, `session.delete`,
   model list, `config.get` fields, segment boundary on redirect); packaging force-include of
   `apps/tui/dist`; D-ids listed in wave 7/10 notes; owner items: Pillow (MIT-CMU), voice `av`
   (LGPL) and numpy licences, Piper/eSpeak NG ruling (D-16.71), real-key P09 demo run.

## Watch out for
- Clean room: never copy reference code, comments or docs; task note before code; reopen the
  reference only for the Compare step. Evidence and captures stay in `~/rea-lab/`.
- `THIRD_PARTY_NOTICES.md` and `LICENSE` are allow-listed in `tooling/checks/similarity_allow.json`
  (licence texts). Never add an allow-list entry without a reason.

## Resume notes
<!-- Newest first. One line per resume. -->
- 2026-10-19 06:30 UTC — Agent 2 resumed from Agent 1's handoff `8d23a10`; gate was red without optional extras (fixed in `6df7114`).
- 2026-10-19 06:11 UTC — Agent 1 handed the turn to Agent 2 (owner's request); main `f303495` green, nothing in flight.
- 2026-10-18 14:26 UTC — Agent 2 resumed from Agent 1's handoff 1; gate green at `e62b76c` (1905 passed).
