# 15 · Roadmap

Milestones, phases, demos, acceptance criteria and risks for Indus Agent 1.0 (full parity). The
phase order is the lead's decision; this doc sizes it, groups it into milestones and marks what can
run side by side. Phase files (`phases/Pnn-*.md`) and `18-phase-wise-build-plan.md` expand each
phase into tasks.

## 1. How to read the sizes

- **Task** = one module and its tests, one task note, about one builder-day.
- **Weeks** = calendar weeks for the phase with 3–4 builder sub-agents working parallel slices and
  the lead landing one commit per slice. A phase is 1–3.5 weeks.
- **Track**: `B` backend (Python), `F` frontend (TypeScript), `B+F` both. Frontend work builds on
  mock data and the generated protocol types until its transport lands.
- Sizes come from the research specs (behaviour count, trap count, reference size per area) and
  will be corrected after P06 using measured task throughput (D-15.2).

## 2. Phases

| Phase | Name | Track | Tasks | Weeks | Depends on | Goal / done when |
|---|---|---|---|---|---|---|
| P00 | Foundations | B+F | 14 | 1 | — | Repo, uv + pnpm workspaces, CI (lint, types, tests, aggregate gate), clean-room guards (banned identifiers, n-gram check, licence allow-list, pin policy), import-linter layers, fakes skeleton, brand tokens, **D-04.1 async trial** started |
| P01 | Platform basics | B | 21 | 1.5 | P00 | Home + profiles (pre-import resolution, ids, tombstones), `Scope`, config load/merge/save (round trip, last-known-good, fail-closed), `.env` io, `SecretStore` (keychain + 0600), logging with redaction, clock, platform facts |
| P02 | State store | B | 23 | 1.5 | P01 | SQLite writer/reader discipline, schema 1 with declarative reconcile, sessions, messages (codec, surrogate scrub, order by id), FTS5 + LIKE fallback, titles, usage, compression records, export/import JSONL, prune, integrity check, **D-04.6 stress test** |
| P03 | Providers + fake model | B | 19 | 1.5 | P01 | Provider profile registry, Chat Completions + Anthropic transports, SSE parser, streaming with stale detection, error classifier, retry with backoff, key-to-host scoping, context-length resolution, **scriptable fake model** (stream, tool calls, errors, delays), replayed live captures |
| P04 | Turn loop + tool contract | B | 23 | 2 | P02, P03 | Agent and turn loop with single exit funnel, prompt builder (tiers, persona, context files, caching invariant), persist order, alternation repair, tool registry/dispatch contract, toolsets + resolver, result budget + spill, interrupt/steer/redirect, `SessionService` in-process host, protocol package + TS codegen (D-04.3), **D-04.1 decided** |
| P05 | Core tools | B | 24 | 2 | P04 | File tools (read guards, stale-write refusal, fuzzy patch), local backend + execution contract (snapshot, cwd marker, timeout backstop), `terminal` foreground/background + `process_manage`, approvals (floors, deny globs, patterns, manual prompt, allowlists, yolo), env builder + redaction, web search/extract with SSRF policy, todo, clarify |
| P06 | Classic CLI chat → **Demo 1** | B | 15 | 1 | P05 | click app, `-p` pre-parse, bare `indus` = chat, prompt_toolkit REPL with streaming render, approval card, core slash commands, bang shell through guards, one-shot `-z` / `-q` with exit codes, resume; **walking skeleton** |
| P07 | CLI commands | B | 26 | 2 | P06 | `setup` wizard, `model` picker, `auth` (keys, device code, PKCE, credential pool), `config` subcommands, `sessions` (list/search/export/prune/repair/recover), `profile`, `tools`, `doctor`, `logs`, `backup`/`import`, `checkpoints`, completion, first-run guard, non-TTY behaviour |
| P08 | Context and cost | B | 19 | 1.5 | P06 | Compression (threshold arithmetic, pruning, boundary alignment, summary call, deterministic fallback, cooldowns, overflow recovery), prompt cache markers, usage/cost with pricing tables, Responses + Bedrock + Gemini transports, credential pools, fallback chain with turn-scoped restore, smart approvals, fast mode, liveness watchdog |
| P09 | Learning loop | B | 25 | 2 | P08 | Memory stores + tool (frozen snapshot, batch budget, staged deletes), skills (format, index, view, slash invocation, manage tool, linter, usage), background review fork, `/learn` and `/refine`, curator (stale/archive, pins, cron protection), bundled starter skills, learning journey commands |
| P10 | Delegation and rich tools | B | 26 | 2 | P09 | `delegate_task` (sync batch, then background), `execute_code` (script then kernel), browser tools (Playwright), vision, image/video generation, TTS/STT and voice mode, computer use, mixture-of-agents, side questions, goals/loops/heartbeat |
| P11 | Docker + SSH backends | B | 21 | 1.5 | P05 | Docker (hardened flags, persistent dirs, label reuse, recovery, env by name only), SSH (ControlMaster, SendEnv, sync), remote file sync, background process notifications and watch patterns, checkpoints and rollback, worktrees |
| P12 | TUI → **Demo 2** | F (+B bridge) | 33 | 3 | P06 (+ F-track prework) | `tui_bridge` stdio server, `@indus/client` channel, Ink app: transcript, streaming row, tool trail, status rule, composer (multiline, history, completion, paste collapse, editor), cards (approval, clarify, sudo, secret, confirm), model picker, session switcher, themes, interrupt/queue/steer, terminal-mode hygiene, crash recovery |
| P13 | MCP, plugins, hooks, hub | B | 28 | 2 | P09 | MCP client (stdio + HTTP, naming clamp, trust gate, breaker, reload, OAuth), plugin manifest/discovery/allow-list/`register(ctx)`, hooks bus with timeouts, plugin install from git with SHA pin + scan, external memory provider API, skills hub (sources, search, install, update, scanner) |
| P14 | Cron | B | 19 | 1.5 | P09 | Grammar (bare duration repeats, `in` once, cron expr, ISO, D-16.54), job store with lock, ticker (`indus cron tick` / `cron daemon`), agent and script jobs, preflight, attempt ledger with unknown state, recursion guard, silence/failure tokens, local delivery, cron tool |
| P15 | Gateway core + Telegram/Discord/Slack → **Demo 3** | B | 36 | 2.5 | P14 (P12 not needed) | Daemon (one profile, P8), service unit, control socket, adapter contract + registry, authz/pairing/allowlists, session keys, busy guards, slash commands, chunker + formatting, streaming edits, delivery ledger + boot recovery, approvals over chat, voice/image intake, cron ticker moved in, API server + webhook, three adapters |
| P16 | More gateway platforms | B | 36 | 3 | P15 | WhatsApp (bridge + Cloud), Signal, Matrix, Mattermost, Email, SMS, Teams, Google Chat, Feishu, DingTalk, WeCom (+callback), Weixin, QQ, LINE, BlueBubbles, ntfy, IRC, SimpleX, other catalogue adapters; per-adapter fake-platform contract tests |
| P17 | Daemon as shared backend | B+F | 27 | 2.5 | P15, P12 | Daemon hosts `SessionService` for all surfaces; WS endpoint with single-use tickets; turn lease (TTL, wait, namespace-aware liveness); generation-bound controls; event replay since cursor; server-request re-delivery; CLI and TUI attach by default with `--local` fallback (parity path, D-16.10) |
| P18 | Web dashboard | B+F | 30 | 2.5 | P17 (+ F-track prework) | FastAPI server, auth gates (loopback token, gated mode, Host guard, WS helper), REST for sessions/config/keys/logs/analytics/cron/skills/plugins/MCP/channels/pairing/profiles/models/files, SPA pages, chat tab (PTY + xterm, keep-alive registry, resize escape, replay sanitising) |
| P19 | ACP + MCP server mode | B | 18 | 1.5 | P17 | ACP adapter (attach + `--local`, history replay ordering, permission bridge, idempotent retries, unknown-outcome refusal), MCP serve (conversation/message tools, long-poll events, approval tools wired to the daemon) |
| P20 | Desktop app → **Demo 4** | F (+B) | 40 | 3.5 | P17, P18 (+ F-track prework) | Electron shell (single instance, sandbox, bridge, sender checks, CSP, fuses), backend resolver + installer runner, boot overlay with latching, transport via `@indus/client`, chat, sidebar, panes (preview, files, git, terminal), settings, onboarding, deep links, HUD, quick entry, updates, unsigned builds on 3 OSes |
| P21 | Remaining backends | B | 16 | 1.5 | P11 | Singularity, Modal (direct), Daytona, Vercel Sandbox over the shared contract; sync-back rules (R02.31); SDK timeout wrappers (R02.30) |
| P22 | Research tooling | B | 15 | 1 | P10 | Batch runner (process pool, fsync order, tombstones, dataset-hash checkpoint), toolset distributions, one trajectory converter, compressor (remote code off, rejects file), eval harness folder |
| P23 | Packaging and release | B+F | 23 | 2 | P20 | `install.sh` + `install.ps1` (stage ladder, pinned uv with sha256, staging-then-rename, receipts), Docker image + Compose, Nix flake, desktop signing paths, `update`/`uninstall` finished, release pipeline (tested bytes only, receipts, channels) |
| P24 | Multiplex and extras | B+F | 47 | 3.5 | P17, P16 | Multiplexed profiles in one gateway (scope binding, fail-closed reads), hosted rooms, relay, A2A, kanban board and workers, projects, LSP, verify, import-agent, desktop pet and wake word, `indus pm`, desktop management pages, connections, bots, plugin SDK, themes, browser backends and vault, egress proxy, plugin packs, hub extras, suggestions, browser-control broker (K39), Termux/MSIX if capacity (D-16.15) |
| P25 | Hardening, parity, release → **v1.0** | B+F | 24 | 2 | all | Security review, REA parity pass and ledger, acceptance map `--strict`, traps ledger complete, performance budgets, docs site, `THIRD_PARTY_NOTICES.md`, release checklist |
| **Total** | | | **648** | **51.5 serial** | | |

## 3. Tracks side by side

The phase order above is never changed, but these pieces run in parallel with it (different files,
no shared ownership). Running a phase early in parallel is not a reorder; it only uses idle
builder slots.

| Calendar window | Backend track (critical path) | Frontend track (mock data until transport lands) | Other parallel backend work |
|---|---|---|---|
| Weeks 1–6 | P00 → P01 → P02 → P03 | P00 shared: pnpm workspace, `@indus/ui` tokens, Biome, vitest | — |
| Weeks 7–11 | P04 → P05 → P06 (**Demo 1**) | F1: `@indus/client` (channel, buffer, heartbeat, reconnect) against a fake server; **D-04.2 Ink spike**; TUI shell on recorded event streams | — |
| Weeks 12–17 | P07 → P08 → P09 | F2: TUI composer, cards, pickers on mocks | P11 (Docker/SSH) alongside P08–P09 (depends only on P05) |
| Weeks 18–21 | P10 → P12 lands (**Demo 2**) | P12 joins F2 work to the real stdio bridge | P22 can start any time after P10 |
| Weeks 22–27 | P13 → P14 → P15 (**Demo 3**) | F3: web SPA shell, pages on mock REST, xterm host; F4: desktop main process, bridge, security, boot overlay on a fake backend | — |
| Weeks 28–30 | P16 and P17 | F3/F4 continue | P16 adapters run beside P17 (adapter files only) |
| Weeks 31–37 | P18 → P19 → P20 (**Demo 4**) | F3 joins P18, F4 joins P20 | P21 alongside P19–P20 (depends only on P11) |
| Weeks 38–45 | P23 → P24 (3.5 weeks) → P25 (**v1.0**) | desktop packaging in P23 | — |

**Estimated calendar:** about 45 weeks at plan pace with two tracks (51.5 serial phase-weeks,
compressed by about 7 weeks by overlapping P11, P16, P21, P22 and the frontend prework for P12
and P20). For one person working
alone, about ×2.5. With sub-agent builders on small tasks, earlier rebuilds finished much faster
than plan pace; we re-estimate after Demo 1 (D-15.2).

## 4. Milestones

| Milestone | Phases | Demo | Acceptance criteria |
|---|---|---|---|
| **M0 · Ground** | P00–P02 | `indus doctor --minimal` prints home, profile, config source per key, keychain status, store health | CI green with all guards; a seeded violation of each guard (banned identifier, 9-gram overlap, unbounded pin, layer import) fails CI; config round-trip preserves comments; broken YAML serves last-known-good; store stress test passes (D-04.6); D-04.1 trial written up |
| **M1 · Walking skeleton** | P03–P06 | **Demo 1:** `indus` with a real API key asks to "create hello.py and run it", streams, asks approval for the command, runs it locally, saves; `indus -c` resumes and the history is intact; the same run passes with the fake model in CI | User row persisted before the call and tool-call row before the side effect (test kills the process at each point); interrupt mid-stream leaves valid alternation; golden prompt + tool array byte-stable across resume; approval floor cannot be bypassed by yolo; `-z` exit codes match the table |
| **M2 · Daily driver** | P07–P11 | `indus setup` from a clean home to first answer in < 3 minutes; a 2-hour session compresses without losing the task; a Docker sandbox run; a skill written by the review fork appears in the next session; a subagent batch | All P07 commands have non-TTY behaviour tests; compression threshold table tests (R01.7) pass; memory writes never change the current session's prompt; Docker passes values by name only (cmdline check); background process notification arrives; curator never touches a hand-written skill |
| **M3 · TUI** | P12 | **Demo 2:** `indus --tui` streams, shows tool trail, approval card, model picker, session switcher; kill the Python child mid-turn and the TUI recovers the same session; resize storm without tearing | Terminal modes reset on every exit path (test each); events before mount are replayed in order; unanswerable server request gets an immediate error; Ctrl+C priority order test; no lost drafts on reconnect |
| **M4 · Extensible** | P13–P14 | An MCP server's tools appear prefixed and callable; a plugin adds a tool and a hook; "every 30m check the build" schedules and runs a cron job with local delivery | Name clamp + hash suffix test; pre-tool hook timeout blocks the tool; hub install refuses a `dangerous` scan; cron at-most-once (attempt recorded before dispatch; unknown never re-run); DST fall-back test |
| **M5 · Messaging** | P15–P16 | **Demo 3:** the same agent answers on Telegram, Discord and Slack; approve a command with a button; kill the gateway between "reply produced" and "acknowledged" and see a labelled recovered reply after restart; a cron job delivers to a Telegram chat | Control commands bypass both busy guards; claim-before-await prevents duplicate agents; timeouts are not retried; chunker property tests (fences, UTF-16, tiny limits); default-deny auth; every P16 adapter passes the shared fake-platform contract suite |
| **M6 · Shared backend + web** | P17–P18 | CLI, TUI and dashboard attached to one daemon on the same session, each seeing the others' turns live; the dashboard chat tab survives a tab switch and reload | Turn lease prevents two writers across processes; tickets single-use; non-loopback dashboard refuses to start without login provider; loopback token rejected in gated mode; PTY keep-alive and supersession codes tested |
| **M7 · Editors + desktop** | P19–P20 | **Demo 4:** Zed (or another ACP client) runs a session with approval cards and diffs; `indus mcp serve` lists and sends messages; the desktop app installs the runtime on a clean machine, onboards a provider and chats with panes | ACP history replay completes before the load response; MCP approval tools absent until wired; every desktop IPC handler rejects a foreign sender; CSP and fuses verified in the packaged build; guest content cannot open a URL; transcript-once oracle in Electron E2E |
| **M8 · Breadth + ship path** | P21–P24 | A Modal/Daytona/Vercel/Singularity sandbox run each; a batch of 100 prompts resumes after a kill without re-paying; fresh-VM installs via `install.sh`, `install.ps1`, Docker, Nix; two profiles served by one gateway | Backend contract suite green for all seven backends; batch fsync order and dataset-hash checkpoint tests; installer never publishes a half clone; release publishes only tested bytes; multiplex reads fail closed |
| **M9 · v1.0** | P25 | Release candidate installed from the public channels on three OSes | Acceptance map `check:acceptance --strict` green; traps ledger has a test or review row for every R-id; REA parity ledger closed or every gap has a D-id in §2.8; security review findings fixed; n-gram and banned-identifier checks clean; owner sign-off on open D-16 rows |

## 5. Risks

| # | Risk | Likelihood / impact | Mitigation | Owner |
|---|---|---|---|---|
| 1 | **asyncio core does not hold up** (blocking libraries, cancellation leaks, plugin hooks) | Medium / high | D-04.1 trial with five pass criteria before P04 ends; fallback to a threaded core behind the same interfaces costs ~1.5 weeks if taken by P04. **Decided 2026-10-18: asyncio core kept** (`docs/notes/P04/d-04-1-decision.md`) | Lead |
| 2 | Upstream Ink lacks capabilities the fork added (sync output, selection, kitty keys) | Medium / medium | D-04.2 spike on the frontend track during P04–P06; fallback writer wrapper | Frontend lead |
| 3 | Scope size: full parity of a ~2.2M-line reference | High / high | Tiers in `03-feature-inventory.md` stay binding; P24 is the pressure valve; re-estimate after Demo 1 and Demo 2; anything that slips goes to §2.8 with a D-id instead of silently missing | Lead + owner |
| 4 | Retrofitting the daemon (P17) onto in-process surfaces breaks behaviour | Medium / high | `SessionService` interface from P04; every surface test runs against both in-process and attached hosts from P17; turn lease and generation fences designed in P04 notes even though built in P17 | Lead |
| 5 | Prompt-cache instability multiplies cost silently | Medium / high | Golden prompt + tool array tests per surface from P04; cache-hit metric in usage; review in P08 | Core builder |
| 6 | SQLite multi-process corruption once daemon + dashboard + CLI share a store | Medium / high | Trap list R03.1–R03.28 turned into tests in P02 (single process) and P17 (multi-process); never live-downgrade WAL; quarantine on corruption | State builder |
| 7 | Messaging platform rate limits and ToS (Telegram floods, Discord sync pacing) | Medium / medium | Fake-platform contract tests; shared per-chat send slot; flood waits handed to the ledger; live bot tests behind a `live` marker | Gateway builder |
| 8 | Clean-room contamination (copied text, names, prompt wording, skills) | Low / high | Guards in P00 CI; fresh copy rule; task notes written before code; n-gram check on docs and code; owner-facing similarity report in P25 | Lead |
| 9 | Desktop signing, notarisation and update channels | Medium / medium | Unsigned builds first; one signing path per OS in P23; follow the documented signing order (R08.22) | Desktop builder |
| 10 | Open owner decisions block phases (providers, skills, approval default, Windows scope, hosting) | Medium / medium | Build proceeds on the recorded recommendation; each decision isolated behind config or a registry so a change is cheap; owner review at each demo | Owner |
| 11 | REA not connected, so "needs REA" questions stay open | Medium / medium | Owner approves registration (D-16.61); until then follow code-observed behaviour and mark tests `ref-unverified` for the parity pass | Owner |
| 12 | Live provider/API drift during a ~1-year build | High / low | Recorded captures per transport refreshed each milestone; a weekly `live` CI lane | Providers builder |

## 6. Roadmap decisions

| ID | Decision | Status |
|---|---|---|
| D-15.1 | Phase order P00–P25 as set by the lead; parallel tracks per §3 do not change the order | ✅ Lead, 2026-10-18 |
| D-15.2 | Re-estimate sizes after Demo 1 and after Demo 2 using measured throughput; update this doc and the phases index | ✅ Lead, 2026-10-18 |
| D-15.3 | Acceptance map and traps ledger pages with their checks are created right after Demo 1 (P06/P07), with ids reserved as review rows, not in P25 | ✅ Lead, 2026-10-18 |
| D-15.4 | A demo is a milestone gate: the owner sees it before the next milestone's phases start their non-parallel work | proposed — lead |
