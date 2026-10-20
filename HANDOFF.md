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
Turn: AGENT_1 (took it at 10:19 UTC 2026-10-19). Done: P00–P14 (P13 `d4815be`, P14 `b1f2839`).
P14 = cron (`b1f2839`, M4 demo test tests/integration/cron/test_m4_demo.py).
In flight:
- Wave 28 builders (brief `.agents/wip/w28/brief.md`): L1 P17-23 idle exit (wt-w28l1), L2 P17-24 profile binding +
  P17-25 protocol additions/codegen (wt-w28l2), L3 P17-26 dual-host matrix + P17-27 shared-backend demo (wt-w28l3).
  L1, L2, L3 DONE, parked; P17-23..27 integrator running in scratchpad verify16 (brief
  `.agents/wip/w28/l-integrator-brief.md`, D-16.540..559).
- Wave 28 P16 builders (brief `.agents/wip/w28/brief-p16.md`): E15 P16-25 Photon (wt-w28e15), E16 P16-33 native
  streams + P16-34 Telegram topics/stickers (wt-w28e16), E17 P16-35 Discord voice + P16-36 adapter matrix/doctor (wt-w28e17).
  E15, E17 DONE, parked; E16 running. Next: one P16 integrator for E15+E16+E17 (D-16.560..579; re-lock for discord-voice).
  If paused, patches go in `.agents/wip/w28/`.
- Wave 29 builders (brief `.agents/wip/w29/brief.md`, owner asked for more parallel agents): W1 P18-01..06 web server +
  auth (wt-w29w1), W2 P18-07..10 PTY + chat WS + profile scope (wt-w29w2), F1 P18-17..22 frontend prework (wt-w29f1),
  A1 P19-01..07 ACP (wt-w29a1). If paused, patches go in `.agents/wip/w29/`.
- Later: P18-11..16, 23..30; P19-08..18; P20, P23, P24, P25.
- Last landings: P16-02/03/07/19/21/22/30 WhatsApp bridge (no Baileys mapping), Matrix E2EE, Weixin, Yuanbao + tools,
  Raft (`cfa6def`; D-16.524..539; notes in LEAD-NOTES "P16 wave 27 landing notes (integrator)"), P17-15..22 ensure/attach channel/remote session host, CLI + TUI attach by default, @indus/client WS
  transport, store ownership across processes, messaging on the shared host (`090479b`; ssh home-retry test fix at landing), P16-31/32 gateway setup wizard + per-platform target parsers (`8708c50`), P17-13/14 WebSocket attach endpoint + subscriptions (`8e91087`; wsproto into base deps), P16-14/15/24/28/29 Feishu + doc tools, BlueBubbles, SimpleX, Buzz (`19e9a77`), P16-11/12/13/16/17/18/20 Teams, Graph, Google Chat, DingTalk, WeCom ×2, QQ Bot (`6cc32b0`), P17-08..12 session host, creation policy, tickets, attach verbs, RPC dispatcher (`ec2b0eb`; also a root
  test fixture that resets the process-exit fence), P16-04/10/23/26/27 WhatsApp Cloud/SMS/LINE/ntfy/IRC + per-platform lock keys (`4aa2c78`), P16-05/06/08/09 Signal/Matrix/Mattermost/Email (`b702a98`, with a P02-19 VACUUM lock-patience
  fix), P15-33..36 Telegram/Discord/Slack + Demo 3 (`5378127`; P15 COMPLETE; notes in LEAD-NOTES
  "P15 wave 22 + Demo 3 landing notes"), P16-01 kit (`e3b3aa1`) with two root-cause fixes — P08-13 expired provider reset no longer arms the
  cooldown ladder, P05-03 local stop_tree waits for the whole tree after SIGKILL; P17-01..07 (`ff7bbe3`; notes in LEAD-NOTES "P17 wave 1 landing notes"), P21-12 rest, 13, 14, 16 + P22-10..15 (`6f04798`; notes in LEAD-NOTES "P21 B3 + P22 R2
  landing notes"), P15-19..32 (`d8eb3c0`: ledger, `indus send`, cron in daemon, approvals over chat, media
  intake, daemon, control socket, `indus gateway`, API server, webhooks; notes in LEAD-NOTES "P15 waves 2–3
  landing notes"), P15-01..18 (`c0e0201`), P21-01..11, 15 and P22-01..09 (`42a852e`).
  Done: P00–P15, P21, P22 complete; P16 31/36; P17 22/27.
Next: P15-33..36 — builders T/D/S (Telegram own Bot API client over httpx, no LGPL; Discord; Slack), each
with `create_adapter(AdapterConfig)`, a send-only open for `indus send` and media fetchers; Demo 3 (P15-36)
at integration. Builder brief: `.agents/wip/p15w4/brief.md`. Then P16, P17.
Owner follow-ups from P21/P22: live vendor smoke tests, ACC-D5-2 on a real Apptainer; `research` extra
licences to confirm: `regex` (CNRI-Python) and pyarrow's bundled zlib (Zlib) / Boost (BSL-1.0).
Owner decisions pending (Agent 1, 2026-10-20): (1) WhatsApp bridge: Baileys pulls GPL-3.0 libsignal + LGPL sharp —
allow an operator-installed copyleft provider, drop bindBaileys, or build on whatsapp-web.js (Apache-2.0, Chromium)?
(E13 parked in `.agents/wip/w27/`, ships nothing copyleft either way.) (2) webhook `auth: auto` replay; (3) licences:
regex (CNRI-Python), pyarrow bundled zlib/Boost, coincurve (MIT OR Apache-2.0); (4) Matrix E2EE uses libolm (deprecated upstream, timing
side-channel advisories) behind our Engine interface in an optional Linux-only extra — ship it until vodozemac? (5) stray
/root/indus-backup-2026-01-01-090000.zip and two log lines a builder appended under /root/.indus (deletion blocked).
Owner items: licences (Pillow MIT-CMU; voice extra av/numpy; Piper/eSpeak NG; new P21 vendor-extra
transitives), real-key P09 demo, manual run of a paused cron job (runs without resuming — confirm); webhook `auth: auto` accepts an
untimestamped generic signature (replay after the 1 h delivery-id window) — decide whether to require the
timestamped form.

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
