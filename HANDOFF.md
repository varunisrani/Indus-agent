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
- By: **Agent 1** | At: 2026-10-20 16:25 UTC | Branch: `main` (pushed to origin)
- Why now: Agent 1 at 86% of its window (resets 18:00 UTC); owner asked to hand over when usage is full. Nothing is
  running (no builders, no integrators, no gate); every finished slice is parked as a patch in `.agents/wip/`.
- Last verified code commit: `d1b05df` (P17-23..27, P17 COMPLETE: `pytest -m "not perf"` 14591 passed / 79 skipped;
  `-m perf` alone 11 passed; pyright 0; lint-imports kept; codegen --check clean; tooling.checks --require-ref ok;
  pnpm 944 passed; pnpm -r typecheck clean).
- Agent 1's code landings this turn (since 2026-10-19 10:19): P13 wave 2, P14, P15, P16-01..24/26..32, P17 (all),
  P21, P22 — see "Last landings" below.

## Usage at last stop
📊 Agent 1 usage: 86% used, 14% left (reading 16:14 UTC) — resets 18:00 UTC — PAUSED (handed off)
📊 Agent 2 usage: last recorded 2026-10-19 (78%, long since reset) — expected fresh — READY

## Handoff history
| # | From → To | Time (UTC) | Steps done | Reason |
|---|-----------|------------|------------|--------|
| 1 | Agent 1 → Agent 2 | 2026-10-18 14:23 | P00, P01, P03, P02-01..07 | Agent 1 at ~75% usage; wave 4 is a clean start |
| 2 | Agent 2 → Agent 1 | 2026-10-18 17:45 | P02-08..23 (P02 done), P04 (done, D-04.1: asyncio kept), P05-01..07, 22..24 | Agent 2 at 71%; next wave would end near 90%; Agent 1 fresh |
| 3 | Agent 2 → Agent 1 | 2026-10-19 10:15 | P10-19/20 (P10 done), P12 all (Demo 2), P13-01..04, 13..17, 20, 24; CI gate without extras | Agent 2 at 78%; wave boundary; Agent 1 fresh |
| 4 | Agent 1 → Agent 2 | 2026-10-20 16:25 | P13 wave 2, P14, P15 (Demo 3), P16 31/36, P17 (M5), P21, P22; parked P16-25/33..36, P18-01..10/17..22, P19-01..07 | Agent 1 at 86%; owner asked to hand over; nothing in flight |

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
Turn: AGENT_2 (handed over by Agent 1 at 16:25 UTC 2026-10-20). Main `d1b05df`+status commits, green.
Done: P00–P15, P17, P21, P22 complete; P16 31/36. 476/648 tasks landed; 28 more built and parked.
Nothing in flight. Three integrations are ready to start, independent of each other (run them in parallel, each in
its own scratchpad worktree from main, full gate, then land one at a time, rebasing the next onto main):
1. **P16 integrator** — `.agents/wip/w28/`: w28-e15.patch (P16-25 Photon, ships DISABLED), w28-e16.patch (P16-33 native
   streams adapter side + P16-34 Telegram topics/stickers) + w28-e16-core-proposal.patch (the StreamConsumer native
   branch — apply it, check script w28-e16-prop_check.py.txt), w28-e17.patch (P16-35 Discord voice + P16-36 adapter
   matrix + doctor gateway checks). D-16.560..579. Online `uv lock` for the new `discord-voice` extra (davey 0.1.6,
   pynacl 1.5.0; fill the 00-layout pin rows). Photon wiring per README (no BUILTIN_CREDENTIALS row while disabled);
   remove photon from the matrix test's PENDING; E17 made contract cases auto-discovered, so E15's conftest import line is
   redundant. Lands P16 COMPLETE (36/36).
2. **P18 integrator** — `.agents/wip/w29/`: w29-w1.patch (P18-01..06 app factory + auth), w29-w2.patch (P18-07..10 PTY,
   chat WS, profile scope; stand-in for W1's gate), w29-f1.patch (P18-17..22 frontend prework; includes pnpm-lock.yaml
   with 13 new npm packages — verify with `pnpm install --frozen-lockfile`, licence + age-check them). Join W2's
   chat_gate onto W1's `guarded_ws`; decide WS credentials in query (row) vs subprotocol (our "no tokens in URLs" rule —
   prefer subprotocol, make F1's wsUrl.ts match); add a `dashboard` extra (ptyprocess, pywinpty on Windows). D-16.580..599.
3. **P19 integrator** — `.agents/wip/w29/w29-a1.patch` (P19-01..07 ACP). Online `uv lock` for agent-client-protocol
   0.12.1 (acp extra + dev group). Merge `tests/fakes/daemon.py` with the one P17 landed (L3's harness). Daemon methods the
   attached host assumes (session.mutate, admission.cancel, config.set show_reasoning, acp.editor_policy) are owed —
   record or add small. D-16.600..609.
Then new builders: P18-11..16 (REST), 23..30 (remaining pages, chat host, login, build/serve, e2e M6); P19-08..18; then
P20, P23, P24, P25. Briefs to copy from: `.agents/wip/w28/brief-p16.md`, `.agents/wip/w29/brief.md`.
Lead loop: builders in scratchpad worktrees never commit; extract with the clean-room-rebuild skill's extract-patch.sh
(--exclude '**/__pycache__/**' --exclude uv.lock --exclude '**/node_modules/**'), scan (credential regexes, /root or ~
writes, STAND-IN first lines, conflict markers), park in .agents/wip/<wave>/ with a README section, then one integrator
per group runs the full gate; the lead commits ("[Agent 2] …"), rebases, fast-forwards, pushes, updates the phase table
in docs/plan/phases/README.md and this file.
- Last landings: P17-23..27 idle exit, profile binding, protocol additions, dual-host matrix, shared-backend demo
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
- Clean room: never copy reference code, comments or docs; task note before code; reopen the
  reference only for the Compare step. Evidence and captures stay in `~/rea-lab/`.
- `THIRD_PARTY_NOTICES.md` and `LICENSE` are allow-listed in `tooling/checks/similarity_allow.json`
  (licence texts). Never add an allow-list entry without a reason.

## Resume notes
<!-- Newest first. One line per resume. -->
- 2026-10-19 06:30 UTC — Agent 2 resumed from Agent 1's handoff `8d23a10`; gate was red without optional extras (fixed in `6df7114`).
- 2026-10-19 06:11 UTC — Agent 1 handed the turn to Agent 2 (owner's request); main `f303495` green, nothing in flight.
- 2026-10-18 14:26 UTC — Agent 2 resumed from Agent 1's handoff 1; gate green at `e62b76c` (1905 passed).
