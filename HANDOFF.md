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
- By: **Agent 1** | At: 2026-10-22 01:09 UTC | Branch: `main` (pushed to origin)
- Why now: Agent 1 at 90% of its window (resets 02:00 UTC), the usage guard. Nothing is running; no worktrees in flight.
- Last fully verified code commit: `226a5ae` (P24 wiring wave 42: pytest "not perf" 18055 passed / 0 failed; perf 18/1;
  vitest 2789; Electron E2E 36/1; web Playwright 9; pyright 0; lint-imports kept; tooling.checks ok).
- Agent 1's landings this turn (since 21:05): P24 (`5c0cff8`), P25-05..19 (`08bcf51`), P24 wiring (`226a5ae`); built and
  parked: w43 SEC1 (P25-01/02) and SEC2 (P25-03/04) in `.agents/wip/w43/`.

## Usage at last stop
📊 Agent 1 usage: 90% used, 10% left (reading 01:02 UTC) — resets 02:00 UTC — PAUSED
📊 Agent 2 usage: last recorded 2026-10-21 20:54 (3%, reset 01:50) — expected fresh — READY

## Handoff history
| # | From → To | Time (UTC) | Steps done | Reason |
|---|-----------|------------|------------|--------|
| 1 | Agent 1 → Agent 2 | 2026-10-18 14:23 | P00, P01, P03, P02-01..07 | Agent 1 at ~75% usage; wave 4 is a clean start |
| 2 | Agent 2 → Agent 1 | 2026-10-18 17:45 | P02-08..23 (P02 done), P04 (done, D-04.1: asyncio kept), P05-01..07, 22..24 | Agent 2 at 71%; next wave would end near 90%; Agent 1 fresh |
| 3 | Agent 2 → Agent 1 | 2026-10-19 10:15 | P10-19/20 (P10 done), P12 all (Demo 2), P13-01..04, 13..17, 20, 24; CI gate without extras | Agent 2 at 78%; wave boundary; Agent 1 fresh |
| 4 | Agent 1 → Agent 2 | 2026-10-20 16:25 | P13 wave 2, P14, P15 (Demo 3), P16 31/36, P17 (M5), P21, P22; parked P16-25/33..36, P18-01..10/17..22, P19-01..07 | Agent 1 at 86%; owner asked to hand over; nothing in flight |
| 5 | Agent 2 → Agent 1 | 2026-10-21 03:45 | P16 (36/36), P18 (30/30, M6 web demo), P19 (18/18, attached ACP on a real daemon), P20-01..07; load-failure root causes; P20-08..24 integrated but unverified (wip patch) | Owner asked to hand over; Agent 2 at 83% |
| 6 | Agent 1 → Agent 2 | 2026-10-21 10:20 | P20-08..40 landed (P20 complete); P23 integrated (green, needs rebase onto P20); P24-01..18, 23, 24, 38..44 built + parked; 45 partial | Agent 1 at 91%; nothing in flight |
| 7 | Agent 2 → Agent 1 | 2026-10-21 17:25 | P23 landed (21/23); P24 all 47 built except 21/22 (post-1.0) and integrated into one patch (w40, Python full gate unfinished) | Agent 2 at 90% (guard) |
| 8 | Agent 1 → Agent 2 | 2026-10-22 01:09 | P24 landed (`5c0cff8`) and wired (wave 42 `226a5ae`, full gate green); P25-05..19 landed; P25-01..04 built and parked (w43) | Agent 1 at 90% (guard); nothing in flight |

## Progress
- [x] Stage 0 — intake decisions (`docs/plan/README.md`) — by Agent 1
- [x] Stage 1 — research: 10 area specs in `docs/plan/research/` — by Agent 1
- [x] Stage 2 — guards (`tooling/checks/`) and scaffold landed in `c790e93` (P00-01..07) — by Agent 1
- [x] Stage 3 — plan: inventory (495 features, 251 P0/P1), what-we-change (D-16.1..92),
  naming, architecture, roadmap, build plan, layout file, 26 phase files with 648 tasks
  (`docs/plan/phases/README.md` has the index and totals) — by Agent 1
- [~] Stage 4 — build: P00–P19, P21, P22 done (521/648); P20 7/40 landed + 17 integrated in .agents/wip/w34; next P20 rest, P23, P24, P25

## Files changed (uncommitted or parked)
`.agents/wip/w34/`: w34-int-wip.patch (the integrated P20-08..24 tree, base 3ba165d — land this, not the three slice patches),
landing-p20a.md (integrator's notes), licences-w34.txt (158 new npm packages, all allowed), and the three slice patches +
README for reference. Nothing uncommitted on main.

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
Turn: AGENT_1 ACTIVE (window resets 07:00 UTC). Main `4945b48` = P25-01..04 security landed (full gate green: pytest
18198/0, perf 18, vitest 2883, Electron E2E 41, web Playwright 14; D-16.900..912; LEAD-NOTES "P25 wave 43 landing notes
(integrator)"). In flight: wave-44 integrator in scratchpad verify24 (brief `.agents/wip/w44/integrator-brief.md`; R1 P25-20/21,
R2 P25-22/23, R3 P25-24 patches + README in `.agents/wip/w44/`; fixes F-1 installer scripts not attached to releases).
Also in flight: wave-45 builders S45 (SEC-14/15/18, wt-w45s, D-16.970..979) and P45 (P24 partial items: message_agent in
bot-chat, suggestion offers on connect, LspUpkeep.release + `indus lsp restart`, wt-w45p, D-16.980..989); brief
`.agents/wip/w45/brief.md`.
Also in flight (03:30 UTC, owner asked for 12+ parallel agents): wave-46 builders a–h, k in scratchpad wt-w46{a..h,k}
(brief `.agents/wip/w46/brief.md`: SEC-4 taint fence, re-lock aiohttp/pynacl/vite, SEC-6/8, desktop data-ui hooks +
licences screen, desktop contribution areas, TUI picker / lost turn / approvals, CI + test hygiene, user guide for P24,
bundled dashboard dist + version on source installs). After all land: UI screenshots for the owner.
Done: P00–P22 complete; P23 21/23 (15/16 signing open); P24 45/47 built and wired (21/22 post-1.0); P25 19/24 landed (01–19).
1. **Land wave 44** when its integrator is green (P25-20..24). If it stopped mid-way, its staged diff is the work: save it.
2. **Open security findings** (docs/internals/threat-model.md): SEC-4 needs the owner's design call (fencing what
   execute_code / delegate_task pass back); SEC-14 per-profile LSP pool; SEC-15 frame room transcripts as data; SEC-18 small
   desktop/web items; SEC-17 re-lock (certifi, aiohttp, vite, pynacl) before 2026-11-09, when deps_audit starts failing.
3. Follow-ups from wave 42: bot-chat turns offering message_agent; suggestion offers on integration connect; remaining desktop
   contribution areas; RFC 8252 broker; approvals in room turns + room event push; LspUpkeep.release and `indus lsp restart`;
   `oneshot_first_token` perf budget headroom (~30 ms). Older: perf baseline on CI, 9 desktop data-ui hooks, TUI model picker bug.
Owner items: see the list below and the release checklist's must rows once wave 44 lands (release base D-16.690, signing
secrets, licence rulings incl. D1's dated exceptions and the Git for Windows source offer, D-16.800–802, similarity verdicts
(20 regions), ACC Review rows, parity reference captures, stray paths at / and /root (also /root/.indus written during this
session by an unattributed run), /tmp/pytest-of-root).
Lead loop: builders in scratchpad worktrees never commit; extract with the clean-room-rebuild skill's extract-patch.sh
(--exclude '**/__pycache__/**' --exclude uv.lock --exclude '**/node_modules/**' — never exclude git-ignored paths), CHECK the
patch is non-empty, scan, park in .agents/wip/<wave>/ with a README section, push, THEN remove the worktree AND its $S dir; one
integrator per group runs the full gate (pytest --basetemp short); the lead commits, rebases, fast-forwards, pushes, updates the
phase table and this file.
- Last landings: P25-01..04 security wave 43 (`4945b48`; D-16.900..912; LEAD-NOTES "P25 wave 43 landing notes (integrator)"), P24 wiring wave 42 (`226a5ae`; D-16.860..879; LEAD-NOTES "P24 wiring wave 42 landing notes (integrator)"), P25-05..19 (`08bcf51`; D-16.830..849; LEAD-NOTES "P25 wave 41 landing notes (integrator)"), P24 (`5c0cff8`; D-16.770..802; notes in LEAD-NOTES "P24 waves 37–39 landing notes (integrator + lead)"), P23 (`fd44e16`; D-16.740..769; Electron E2E harness fixes), test_tui_publish race fix (`19ced11`),
  P20 (Agent 1: `2a1991b`, `52deb8b`), P18-23..30 (`71389d4`; P18 COMPLETE; Playwright web-e2e lane not yet run in CI; D-16.650..658),
  P18/P19 follow-ups (prompt-size breakdown — P18-16 complete; ACP no_mcp + model row + reattach e2e; TUI →
  dashboard events; mcp-serve doc; backup download ticket; P04 persona-flag prompt fix), P20-01..07 desktop foundation (`86f44f7`; IPC/preload contract in docs/notes/P20/preload-bridge.md),
  load-failure fixes + clock quiesce helpers (full gate 15652 passed / 0 failed), P19-08..18 + D-16.602 daemon side (`eab425c`; P19 COMPLETE; attached `indus acp` works against a real
  daemon; `indus mcp serve`; D-16.626..642), P18-11..16 dashboard REST (`9786b32`; download tickets, no tokens in URLs, daemon `stop` verb,
  generated route list; D-16.596..598, 610..622), drive-harness fix (`155ef32`), learning/prefetch test fixes (`0e9a538`,
  `ca2860b`), P16-25, 33–36 Photon (disabled), native draft streams, Telegram topics/stickers, Discord voice, adapter matrix
  + doctor gateway checks (`aa696ad`; P16 COMPLETE; D-16.560..570; notes in LEAD-NOTES "P16-25, 33..36 landing notes"), P19-01..07 `indus acp` (`3ade87c`; D-16.600..609; attached session/new waits on D-16.602 session.mutate +
  acp.editor_policy on the daemon; notes in LEAD-NOTES "P19-01..07 landing notes"), P18-01..10, 17..22 web app factory + gates, gated login, OIDC, proxies, WS auth (subprotocol only), PTY
  bridge, chat WS routes, profile scope, SPA shell and pages (`077ef96`; D-16.580..595; notes in LEAD-NOTES "P18-01..10, 17..22
  landing notes (integrator)"), P17-23..27 idle exit, profile binding, protocol additions, dual-host matrix, shared-backend demo
  (`d1b05df`; P17 COMPLETE; D-16.540..557; notes in LEAD-NOTES "P17-23..27 landing notes (integrator)"), P16-02/03/07/19/21/22/30 WhatsApp bridge (no Baileys mapping), Matrix E2EE, Weixin, Yuanbao + tools,
  Raft (`cfa6def`; D-16.524..539; notes in LEAD-NOTES "P16 wave 27 landing notes (integrator)"), P17-15..22 ensure/attach channel/remote session host, CLI + TUI attach by default, @indus/client WS
  transport, store ownership across processes, messaging on the shared host (`090479b`; ssh home-retry test fix at landing), P16-31/32 gateway setup wizard + per-platform target parsers (`8708c50`), P17-13/14 WebSocket attach endpoint + subscriptions (`8e91087`; wsproto into base deps), P16-14/15/24/28/29 Feishu + doc tools, BlueBubbles, SimpleX, Buzz (`19e9a77`), P16-11/12/13/16/17/18/20 Teams, Graph, Google Chat, DingTalk, WeCom ×2, QQ Bot (`6cc32b0`), P17-08..12 session host, creation policy, tickets, attach verbs, RPC dispatcher (`ec2b0eb`; also a root
  test fixture that resets the process-exit fence), P16-04/10/23/26/27 WhatsApp Cloud/SMS/LINE/ntfy/IRC + per-platform lock keys (`4aa2c78`), P16-05/06/08/09 Signal/Matrix/Mattermost/Email (`b702a98`, with a P02-19 VACUUM lock-patience
  fix), P15-33..36 Telegram/Discord/Slack + Demo 3 (`5378127`; P15 COMPLETE; notes in LEAD-NOTES
  "P15 wave 22 + Demo 3 landing notes"), P16-01 kit (`e3b3aa1`) with two root-cause fixes — P08-13 expired provider reset no longer arms the
  cooldown ladder, P05-03 local stop_tree waits for the whole tree after SIGKILL; P17-01..07 (`ff7bbe3`; notes in LEAD-NOTES "P17 wave 1 landing notes"), P21-12 rest, 13, 14, 16 + P22-10..15 (`6f04798`; notes in LEAD-NOTES "P21 B3 + P22 R2
  landing notes"), P15-19..32 (`d8eb3c0`: ledger, `indus send`, cron in daemon, approvals over chat, media
  intake, daemon, control socket, `indus gateway`, API server, webhooks; notes in LEAD-NOTES "P15 waves 2–3
  landing notes"), P15-01..18 (`c0e0201`), P21-01..11, 15 and P22-01..09 (`42a852e`).
  Done: P00–P15, P21, P22 complete; P16 31/36; P17 complete.
Owner follow-ups from P21/P22: live vendor smoke tests, ACC-D5-2 on a real Apptainer; `research` extra
licences to confirm: `regex` (CNRI-Python) and pyarrow's bundled zlib (Zlib) / Boost (BSL-1.0).
Owner decisions pending (Agent 1, 2026-10-20): (1) WhatsApp bridge: Baileys pulls GPL-3.0 libsignal + LGPL sharp —
allow an operator-installed copyleft provider, drop bindBaileys, or build on whatsapp-web.js (Apache-2.0, Chromium)?
(Landed in `cfa6def` without the Baileys mapping; Photon (P16-25) likewise ships disabled: Spectrum has a BlueOak-1.0.0
transitive + a postinstall patch — same decision.) (2) webhook `auth: auto` replay; (3) licences:
regex (CNRI-Python), pyarrow bundled zlib/Boost, coincurve (MIT OR Apache-2.0); (4) Matrix E2EE uses libolm (deprecated upstream, timing
side-channel advisories) behind our Engine interface in an optional Linux-only extra — ship it until vodozemac? (5) stray
/root/indus-backup-2026-01-01-090000.zip, log lines a builder appended under /root/.indus, one pnpm cache line in
/root/.cache/pnpm/lockfile-verified.jsonl, npm debug logs /root/.npm/_logs/2026-10-20T14_05_* (deletion needs the owner's OK).
(6) The owner also asked (2026-10-20) to update two Hindi videos (remove captions; on-screen English → Devanagari Hindi,
same font/colour/position) — waiting for the files.
Owner items: licences (Pillow MIT-CMU; voice extra av/numpy; Piper/eSpeak NG; new P21 vendor-extra
transitives), real-key P09 demo, manual run of a paused cron job (runs without resuming — confirm); webhook `auth: auto` accepts an
untimestamped generic signature (replay after the 1 h delivery-id window) — decide whether to require the
timestamped form.

## Watch out for
- Disk: the session's writable allowance (~40 GB) filled up on 2026-10-21 21:20 (ENOSPC stopped the P24 lander and G1). Lead
  freed ~22 GB by deleting finished builders' scratch dirs. Every brief must say: pytest with `--basetemp=$S/pt` (deleted after
  each run), remove build output once a step passes, and the lead deletes a builder's $S dir when it parks the slice. Owner
  item: /tmp/pytest-of-root (~2 GB of stale basetemps) and /root (~6 GB) — agents are blocked from cleaning them.
- Parking a builder: run extract-patch, CHECK the patch file exists and is non-empty, and only then remove the worktree
  (never chain the removal with `;`). Do not pass --exclude for paths git already ignores (e.g. `.venv/**`): `git add`
  fails and the script exits without a patch. (2026-10-21: the I2 worktree was lost this way and rebuilt by its builder.)
- Clean room: never copy reference code, comments or docs; task note before code; reopen the
  reference only for the Compare step. Evidence and captures stay in `~/rea-lab/`.
- `THIRD_PARTY_NOTICES.md` and `LICENSE` are allow-listed in `tooling/checks/similarity_allow.json`
  (licence texts). Never add an allow-list entry without a reason.

## Resume notes
<!-- Newest first. One line per resume. -->
- 2026-10-19 06:30 UTC — Agent 2 resumed from Agent 1's handoff `8d23a10`; gate was red without optional extras (fixed in `6df7114`).
- 2026-10-19 06:11 UTC — Agent 1 handed the turn to Agent 2 (owner's request); main `f303495` green, nothing in flight.
- 2026-10-18 14:26 UTC — Agent 2 resumed from Agent 1's handoff 1; gate green at `e62b76c` (1905 passed).
