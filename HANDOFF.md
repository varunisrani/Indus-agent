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
UI PARITY IN FLIGHT (owner 2026-10-24: "same to same UI as Hermes, every page and component, web + desktop"; brief
`.agents/wip/w51/brief.md`). Wave 51 foundations: w51a web, w51b desktop (scratchpad wt-w51a/b). Wave 52 page builders
start after 51 lands, from the specs in scratchpad/w52-specs/ (13 spec writers running). Agent ids: scratchpad/agents.txt.
Reference + Indus screenshots and side-by-side PDFs: scratchpad/compare/. Owner decision overrides the D-16 palette row
(record a D-id at integration); clean room still applies to code and assets.
ALL 648 PLANNED TASKS BUILT. Main `c1c20da` = wave 50 landed (2026-10-23 ~13:00 UTC; LEAD-NOTES "Wave 50 landing notes
(integrator)"; D-16.1150..1179): attached config.set (D-16.549 closed), daemon shell.exec + quick exec (D-16.1094),
Windows foreground-only plan() + starter races, Termux apt update kind + repo validate, desktop canary identity + Windows
update verification, config-defaults and skills front-matter twins (RM25), room-watch race fix (D-16.1179). Gate: pytest
18777 passed + 1 (the room-watch race, fixed; test_watch 50/50); perf 18/1; vitest 2992; Electron 43/2; web 14.
Before that: wave 49 (`8a9f9f3`, the last 4 planned tasks). Owner items: signing secrets (checklist §0, RM14), first
signed mac/Windows runs, Termux digest/host/fingerprint, MSIX + Windows publisher in identity.json, release-environment
reviewers, D-16.1158 (attached !command on host vs sandbox), D-16.1071, D-16.998/999, certifi by 2026-11-09, pypdf
re-lock on/after 2026-10-27, licences, people reviews. Agent follow-ups: LEAD-NOTES "Wave 50 landing notes" list.
Turn: AGENT_1 ACTIVE (window resets 17:30 UTC). Main `bd1eaf9` = wave 48 landed (LEAD-NOTES "Wave 48 landing notes
(integrator)"): desktop room view on groups.watch + approval card, attached TUI reads/slash/header events, desktop polish,
PTY env, Windows terminal schema + console, katex/sprintf-js cleared, startup ~400 ms faster. Release checklist: 32 done.
Left for agents (small): attached config.set (needed by /yolo /reasoning /fast /verbose /busy), daemon shell.exec for quick
exec, /status queued count; Windows plan() background mapping, starter-test race, real windows-local run; config-defaults
and skills front-matter twins (RM25, judge on a quiet runner); pypdf re-lock on/after 2026-10-27 (RS37); certifi by 2026-11-09
(RM7, owner). Owner: TUI/web room views (D-16.1071) and the list below. /tmp leftovers from gates (upd-*, scoped_dir*,
pytest-of-root): owner said do NOT delete them (2026-10-23); leave them.
Turn: AGENT_1 ACTIVE (window resets 12:00 UTC). Main `83104de` = wave 47 landed on top of waves 45+46 (`44c9a4f`)
(LEAD-NOTES "Wave 47 landing notes (integrator)"). All 648 planned tasks except P23-15/16 (owner signing) and P24-21/22
(post-1.0) are built; release checklist: 27 done, must 21 / should 14 open — almost all owner decisions or owner runs.
Agent-doable follow-ups left: RS3 room views (desktop GroupRoom.tsx, TUI, web) on groups.watch + approval card; daemon
serves complete.path / commands.catalog / config.get; notice when a chat's folder is gone; dashboard PTY child drops
COLORFGBG/TERM_PROGRAM; desktop localEcho admission-id binding; Windows real runs (RL26, install.ps1); JS gate rerun on
re-locked node_modules (needs pnpm registry metadata); oneshot_first_token budget on slow boxes; katex / sprintf-js advisories.
UI screenshots: final set (34 PNGs, main daea8cf) sent to the owner 11:10 UTC; every wave-47 UI fix confirmed working.
Minor UI follow-ups seen in that pass: TUI sessions picker rows show only the id in default mode (--local showed title,
model, source); desktop About › Version log shows a ResizeObserver loop ERROR line and long run paths; the desktop
transcript does not scroll an approval card into view when it opens below an expanded tool row; sidebar/web rail titles
are width-clipped (expected). The Licences page is empty outside a packaged app (expected).
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
- Last landings: wave 50 (`c1c20da`; D-16.1150..1179; LEAD-NOTES "Wave 50 landing notes (integrator)"), wave 49 (`8a9f9f3`; D-16.1110..1147; LEAD-NOTES "Wave 49 landing notes (integrator)"), wave 48 (`bd1eaf9`; D-16.1070..1102; LEAD-NOTES "Wave 48 landing notes (integrator)"), wave 47 (`83104de`; D-16.1030..1059; LEAD-NOTES "Wave 47 landing notes (integrator)"), waves 45+46 (`44c9a4f`; D-16.970..1025; LEAD-NOTES "Waves 45 + 46 landing notes (integrator)"), P25-20..24 release prep wave 44 (`b9a43a1`; D-16.920..964; LEAD-NOTES "P25 wave 44 landing notes (integrator)"), P25-01..04 security wave 43 (`4945b48`; D-16.900..912; LEAD-NOTES "P25 wave 43 landing notes (integrator)"), P24 wiring wave 42 (`226a5ae`; D-16.860..879; LEAD-NOTES "P24 wiring wave 42 landing notes (integrator)"), P25-05..19 (`08bcf51`; D-16.830..849; LEAD-NOTES "P25 wave 41 landing notes (integrator)"), P24 (`5c0cff8`; D-16.770..802; notes in LEAD-NOTES "P24 waves 37–39 landing notes (integrator + lead)"), P23 (`fd44e16`; D-16.740..769; Electron E2E harness fixes), test_tui_publish race fix (`19ced11`),
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
(6) Hindi videos request (2026-10-20): DROPPED by the owner on 2026-10-23 ("no need to do this for now").
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
