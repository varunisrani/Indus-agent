# 18 · Phase-wise build plan

How Indus Agent gets built, week by week and phase by phase. The phase order, sizes and
dependencies come from `15-roadmap.md` (D-15.1); every task is specified in its phase file under
`phases/` (`phases/Pnn-<slug>.md`), and every path follows `phases/00-layout-and-conventions.md`.
This page is the map; the phase files are the territory. Feature ids follow
`03-feature-inventory.md` (install/distribution/CI is area **S**), decisions `16-what-we-change.md`.

## 1. The loop every task follows

Each task is one module plus its tests, about one builder-day. A builder receives the phase file
and the task's row, and nothing else, and runs these five steps in order:

1. **Read.** Open the reference files the task's "Read in reference" cell names, at the sizes and
   sections given there (docs first, then code, then the reference's tests for edge-case lists
   only), plus the area spec in `research/` and any REA Evidence id the task cites. Big files are
   read by the named functions, never end to end.
2. **Task note.** Write `docs/notes/Pnn/<slug>.md` from `docs/notes/_template.md` **before any
   code**: what the module must do, in our words; the numbers and failure modes; the traps it pins;
   the design we chose; the `Read:` line listing every reference file read. The note is the
   evidence that the rewrite was not a transcription.
3. **Close the reference and rewrite.** No reference file or Evidence on screen. Build from the
   note, the phase file and our own names (`17-naming-and-glossary.md`), with the tests the Verify
   cell lists. Fakes and injected clocks only: no network, no real sleeps.
4. **Reopen and compare.** Walk the reference branch by branch against what we wrote; tick the
   phase's compare checklist in the note, each tick naming its test. A gap is closed in our own
   way, never by pasting. A difference we keep gets a D-id in `16-what-we-change.md` §2.7.
5. **Check.** Run the phase's "Run until green" loop (lint, format, types, tests, then
   `uv run python -m tooling.checks --require-ref`). Any failure sends the builder back to step 3.
   The lead then re-runs the full loop on the merged result and lands one commit per slice, swaps
   the slice's trap rows and acceptance ids from `Review:` to test references, and writes any
   deviation back into the phase file so later phases stay consistent.

A phase is split into slices with no shared files (the phase file's "Files to create" table names
each file's owning task); three or four builders work slices in parallel. A demo phase is a
milestone gate: the owner sees the demo before the next milestone's non-parallel work starts
(D-15.4, proposed).

## 2. Week by week

About 45 calendar weeks at plan pace with two tracks (51.5 serial phase-weeks, compressed by running
P11, P16, P21, P22 and the frontend prework beside the critical path). P24 grew to 47 tasks and
3.5 weeks at reconciliation (weeks 40–43). Sizes are re-estimated
after Demo 1 and Demo 2 from measured throughput (D-15.2).

| Week | Backend | Frontend | Demo |
|---|---|---|---|
| 1 | P00 Foundations: uv + CI, guards, layer contract, fakes skeleton, D-04.1 trial starts | P00 shared: pnpm workspace, `@indus/ui` tokens, Biome, vitest | — |
| 2 | P01 Platform basics: home, profiles, scope, config load/save | tokens and primitives | — |
| 3 | P01 → P02: secrets, logging, clock; store connection and writer | tokens and primitives | — |
| 4 | P02 State store: schema 1, sessions, messages, FTS | — | — |
| 5 | P02 → P03: export/import, prune, D-04.6 stress test; provider registry | — | **M0**: `indus doctor --minimal` |
| 6 | P03 Providers + fake model: transports, SSE, retry, captures | — | — |
| 7 | P04 Turn loop + tool contract: loop, prompt builder, registry | F1: `@indus/client` against a fake server | — |
| 8 | P04: service host, protocol + TS codegen, **D-04.1 decided** | F1 + **D-04.2 Ink spike** | — |
| 9 | P05 Core tools: file tools, local backend, terminal | F1: TUI shell on recorded event streams | — |
| 10 | P05: approvals, env builder, web tools, todo, clarify | F1 continues | — |
| 11 | P06 Classic CLI: REPL, approval card, one-shot, resume | F1 continues | **Demo 1 / M1** walking skeleton |
| 12 | P07 CLI commands: setup, model, auth, config | F2: TUI composer and cards on mocks | — |
| 13 | P07: sessions, profile, doctor, logs, backup | F2 continues | — |
| 14 | P08 Context and cost: compression, caching, usage · beside it P11 Docker | F2: pickers on mocks | — |
| 15 | P08: more transports, pools, fallback · P11 SSH, sync, processes | F2 continues | — |
| 16 | P09 Learning loop: memory, skills, review fork | F2 continues | — |
| 17 | P09: curator, starter skills, journey | F2 continues | **M2** daily driver |
| 18 | P10 Delegation and rich tools: delegate, execute_code, browser | F2 → P12 bridge prep | — |
| 19 | P10: vision, media, voice, computer use, MoA, goals | P12 joins F2 work to the stdio bridge | — |
| 20 | P12 `tui_bridge` · beside it P22 Research tooling | P12 TUI on the real bridge | — |
| 21 | P12 bridge hardening · P22 compressor, evals | P12 recovery, terminal-mode hygiene | **Demo 2 / M3** TUI |
| 22 | P13 MCP, plugins, hooks, hub: MCP client, manifests | F3: web SPA shell, pages on mock REST | — |
| 23 | P13: hooks bus, plugin install, hub | F3 + F4: desktop main process, bridge, security | — |
| 24 | P14 Cron: grammar, store, ticker | F3/F4 continue | — |
| 25 | P14 → P15: runner, delivery, cron tool; gateway daemon | F3/F4 continue | **M4** extensible |
| 26 | P15 Gateway core: authz, session keys, busy guards, chunker | F3/F4 continue | — |
| 27 | P15: delivery ledger, API server, Telegram/Discord/Slack | F3/F4 continue | **Demo 3** messaging |
| 28 | P16 More platforms · P17 Daemon as shared backend | F3/F4 continue | — |
| 29 | P16 · P17: WS tickets, turn lease, replay | F3/F4 continue | — |
| 30 | P16 · P17: CLI/TUI attach by default | F4 boot overlay on a fake backend | **M5** messaging complete |
| 31 | P18 Web dashboard: FastAPI, auth gates, REST | F3 joins P18 | — |
| 32 | P18: chat tab PTY, remaining routes | P18 SPA pages | — |
| 33 | P18 → P19 ACP + MCP serve · beside it P21 Remaining backends | P18 finished | **M6** shared backend + web |
| 34 | P19 · P21 Modal, Daytona, Vercel | F4 joins P20 Desktop | — |
| 35 | P20 backend parts · P21 sync-back, contract suite | P20: chat, sidebar, panes | — |
| 36 | P20 | P20: settings, onboarding, updates | — |
| 37 | P20 | P20: HUD, quick entry, unsigned builds on 3 OSes | **Demo 4 / M7** editors + desktop |
| 38 | P23 Packaging: installers, Docker, Nix | P23 desktop signing paths | — |
| 39 | P23: update/uninstall, release pipeline | P23 desktop artefacts | — |
| 40 | P24 Multiplex and extras: multiplexed gateway, rooms, relay | P24 desktop pet, wake word | — |
| 41 | P24: A2A, kanban, projects, LSP, verify, import-agent | P24 kanban board UI | — |
| 42 | P24: `indus pm`, browser backends and vault, egress proxy, memory caps | P24 desktop management pages, connections, bots | — |
| 43 | P24: plugin packs, hub extras, suggestions, browser-control broker | P24 plugin SDK, themes, voice; dashboard console and plugins | **M8** breadth + ship path |
| 44 | P25 Hardening: security review, parity pass, ledgers | P25 web/desktop security, perf | — |
| 45 | P25: notices, docs site, gates from a fresh clone | P25 docs site | **M9 / v1.0** candidate |

## 3. Phases

Each section: the goal, the main reference reading (paths with sizes in lines; the phase file
names the exact functions), the headline of what gets written, and the done-when test. Reference
paths are for the Read step only.

### P00 · Foundations — [phases/P00-foundations.md](phases/P00-foundations.md)
- **Goal:** a repo where every later task lands safely: uv + pnpm workspaces, CI with one aggregate
  gate, clean-room guards, layer contract, fakes skeleton, brand tokens; D-04.1 trial started.
- **Read in reference:** `.github/workflows/ci.yaml` [585], `scripts/ci/classify_changes.py`
  [757], `scripts/ci/required_results.py` [123], `scripts/run_tests_parallel.py` [1664: options,
  retry, slicing], `tests/conftest.py` [1533: fixture list], `pyproject.toml` [787], `ruff.toml` [473].
- **Write:** CI workflow and gate; pin, licence, banned-name, similarity, asset, ledger checks;
  import-linter; pyright; test fixtures and fakes skeleton; pnpm workspace; `@indus/ui` tokens;
  English locale + parity check; async trial harness.
- **Done when:** CI green with all guards; a seeded violation of each guard fails CI (M0 criteria).

### P01 · Platform basics — [phases/P01-platform-basics.md](phases/P01-platform-basics.md)
- **Goal:** home, profiles and `Scope` resolved before any import; config that round-trips with
  comments and fails closed; secrets in the keychain (P4); logging with redaction.
- **Read in reference:** `hermes_constants.py` [1467: home/profile ~450 lines], `hermes_cli/config.py`
  [4055: load, save, `.env` writers, set/get], `hermes_cli/config_defaults.py` [3156: data only],
  `hermes_cli/env_loader.py` [833], `hermes_logging.py` [1005], `hermes_time.py` [149],
  `hermes_yaml.py` [98], platform host and resolver modules [~1,100 total].
- **Write:** `platform/` facts and lookup; `foundation/` home, profiles, preparse, scope, yamlio,
  config schema/loader/writer/backups, envfile, secrets, redact, log, clock, ids, errors.
- **Done when:** config round-trip keeps comments; broken YAML serves last-known-good; keychain
  probe never prompts; deleted profile never resurrected.

### P02 · State store — [phases/P02-state-store.md](phases/P02-state-store.md)
- **Goal:** one SQLite store per profile with writer/reader discipline, schema 1 with declarative
  reconcile, FTS5 with LIKE fallback, export/import, prune, integrity; D-04.6 stress test.
- **Read in reference:** `hermes_state.py` [1730], `hermes_state_messages.py` [1972: append,
  compaction, resume], `hermes_state_sessions.py` [1980], `hermes_state_schema.py` [1379],
  `hermes_state_search.py` [1368], `hermes_state_fts.py` [453], `hermes_state_wal.py` [669],
  `hermes_state_readpool.py` [259], `website/docs/developer-guide/session-storage.md` [604].
- **Write:** `state/` connection, journal, writer, readpool, registry, schema, sessions, listing,
  messages, rewind, compression records, titles, fts, search, usage, portability, deletion,
  maintenance, browse.
- **Done when:** 4 processes × 8 threads for 60 s with a VACUUM lose no rows and surface no lock
  error; R03 traps for single-process use are tests. **M0 demo:** `indus doctor --minimal`.

### P03 · Providers + fake model — [phases/P03-providers-fake-model.md](phases/P03-providers-fake-model.md)
- **Goal:** talk to real model APIs over our own raw transports, with stale-stream detection,
  error classification and retry; a scriptable fake model every later test uses.
- **Read in reference:** `providers/base.py` [435], `agent/transports/*.py` [53–971 each],
  `agent/chat_completion_helpers.py` [4017: stale timeouts 427–700, `_StreamingCall`],
  `agent/error_classifier.py` [1504], `agent/retry_utils.py` [195], `agent/model_metadata.py`
  [2636: `get_model_context_length`], `developer-guide/provider-runtime.md` [229].
- **Write:** provider profiles and registry, hosts and routes, SSE parser, Chat Completions and
  Anthropic transports, streaming driver, classifier, retry, context length, fake model, captures.
- **Done when:** two live captures per transport replay green; the fake model scripts streams,
  tool calls, errors and delays.

### P04 · Turn loop + tool contract — [phases/P04-turn-loop-tool-contract.md](phases/P04-turn-loop-tool-contract.md)
- **Goal:** the agent loop with one exit funnel, persist-before-effect, a byte-stable prompt, the
  tool registry/dispatch contract, `SessionService` in-process, and the protocol package with TS
  codegen; D-04.1 decided.
- **Read in reference:** `agent/conversation_loop.py` [1867], `agent/turn_context.py` [1356],
  `agent/system_prompt.py` [881], `agent/prompt_builder.py` [1779: discovery, gates], 
  `agent/tool_executor.py` [1957], `tools/registry.py` [1086], `toolsets.py` [495],
  `model_tools.py` [993], `tools/tool_result_storage.py` [495], `agent/agent_runtime_helpers.py`
  [3807: repair passes 550–790].
- **Write:** `core/` agent, loop, prompt builder, context files, repair; `tools/` registry,
  toolsets, dispatch, budget + spill; `service/` in-process host; `protocol/` + codegen.
- **Done when:** golden prompt + tool array byte-stable across resume; alternation valid after an
  interrupt at every point; D-04.1 written up with all five criteria.

### P05 · Core tools — [phases/P05-core-tools.md](phases/P05-core-tools.md)
- **Goal:** the tools a coding agent needs on the local machine, behind the shared execution
  contract and the approval floors.
- **Read in reference:** `tools/environments/base.py` [804], `base_session_env.py` [190],
  `environments/local.py` [1178], `tools/terminal_tool.py` [1653: plan, acquire, foreground],
  `tools/process_registry.py` [2766: spawn, wait, kill], `tools/approval.py` [1290],
  `tools/approval_detection.py` [1572: labels only], `tools/file_tools.py` [1430],
  `tools/file_operations.py` [1717], `tools/web_tools.py` [573], `tools/url_safety.py` [582].
- **Write:** `backends/` contract, wrapper, wait, capture, local, registry, processes; `safety/`
  approvals, child env, redaction, URL policy, path guards; file, terminal, process, web, todo,
  clarify tools.
- **Done when:** contract suite green on local; yolo cannot bypass a floor; timeout returns rc 124
  at timeout + 2 s.

### P06 · Classic CLI chat → Demo 1 — [phases/P06-classic-cli-demo1.md](phases/P06-classic-cli-demo1.md)
- **Goal:** the walking skeleton: `indus` chats, streams, asks approval, runs, saves, resumes.
- **Read in reference:** `cli.py` [1793], `hermes_cli/main.py` [3602: profile override, continue
  arg, `cmd_chat`, parser], `hermes_cli/commands.py` [568], `hermes_cli/oneshot.py` [665],
  `hermes_cli/bang_shell.py` [141], `hermes_cli/cli_commands_mixin.py` [2416: skim].
- **Write:** click app with lazy groups, `-p` pre-parse, REPL with streaming render, approval
  card, core slash commands, bang shell, one-shot `-z`/`-q`, resume, version.
- **Done when:** **Demo 1** with a real key and with the fake model in CI; kill tests at each
  persist point; `-z` exit codes match the table; `--version` < 150 ms.

### P07 · CLI commands — [phases/P07-cli-commands.md](phases/P07-cli-commands.md)
- **Goal:** the management surface: setup, model, auth, config, sessions, profile, tools, doctor,
  logs, backup/import, checkpoints, completion; every command has non-TTY behaviour.
- **Read in reference:** `hermes_cli/setup.py` [765] (+ setup siblings ~1,900), `hermes_cli/auth.py`
  [2490: registry, store, resolve], `auth_device_flow.py` [452], `hermes_cli/profiles.py` [2460],
  `hermes_cli/backup.py` [2181: exclusions, run], `hermes_cli/doctor*.py` [~3,300], `logs.py` [350].
- **Write:** `cli/commands/*`, wizards, pickers, doctor registry, first-run guard; acceptance map
  and traps ledger pages get their reserved ids (D-15.3).
- **Done when:** non-TTY tests for every command; `setup` from a clean home to first answer < 3 min.

### P08 · Context and cost — [phases/P08-context-and-cost.md](phases/P08-context-and-cost.md)
- **Goal:** long sessions stay cheap and on task: compression, cache markers, usage and cost,
  three more transports, credential pools, fallback chain, smart approvals, fast mode, watchdog.
- **Read in reference:** `agent/context_compressor.py` [5823: constants, threshold, `compress`],
  `agent/conversation_compression.py` [4500: contract docstring], `agent/prompt_caching.py` [377],
  `agent/usage_pricing.py` [785], `agent/credential_pool.py` [3097: selection, cooldowns],
  `agent/codex_responses_adapter.py` [1312], `agent/bedrock_adapter.py` [1536: skim],
  `agent/gemini_native_adapter.py` [1012: skim], `tools/approval_smart.py` [170].
- **Write:** `core/` compression and usage; `providers/` responses, bedrock, gemini transports,
  pools, fallback; `safety/` smart judge.
- **Done when:** compression threshold table (R01.7) green; a 2-hour fake session compresses
  without losing the task.

### P09 · Learning loop — [phases/P09-learning-loop.md](phases/P09-learning-loop.md)
- **Goal:** the agent remembers and writes its own skills; ~15 starter skills written fresh
  (D-16.43).
- **Read in reference:** `tools/memory_tool.py` [485], `memory_tool_store.py` [700],
  `agent/background_review.py` [1349], `agent/curator.py` [1212], `tools/skill_manager_tool.py`
  [928], `tools/skills_tool.py` [765], `agent/skill_commands.py` [1001], `tools/skill_usage.py` [748].
- **Write:** `learning/` memory stores and tool, skills format/index/loader/manage tool, review
  fork, curator, journey; `skills/` starter set.
- **Done when:** memory writes never change the current prompt; the curator never touches a
  hand-written skill; a review-fork skill appears next session (M2).

### P10 · Delegation and rich tools — [phases/P10-delegation-rich-tools.md](phases/P10-delegation-rich-tools.md)
- **Goal:** subagents, code execution, browser, vision, media, voice, computer use, MoA, side
  questions, goals/loops/heartbeat.
- **Read in reference:** `tools/delegate_tool.py` [813] (+ siblings ~3,600), `tools/code_execution_tool.py`
  [969], `tools/code_kernel.py` [911], `tools/browser_tool.py` [1376] (+ ~2,600), `tools/vision_tools.py`
  [1192], `tools/tts_tool.py` [682], `tools/transcription_tools.py` [602], `tools/voice_mode.py` [1555].
- **Write:** `tools/builtin/` delegate, code, browser, media, computer_use; `core/` subagents, MoA.
- **Done when:** a subagent batch of 10 runs and returns; kernel cap never evicts a running
  child's kernel (R02.32).

### P11 · Docker + SSH backends — [phases/P11-docker-ssh-backends.md](phases/P11-docker-ssh-backends.md)
- **Goal:** sandboxed execution in Docker and on SSH hosts, background processes with
  notifications, checkpoints and rollback, worktrees.
- **Read in reference:** `tools/environments/docker.py` [1332: init, args, recreate, cleanup],
  `environments/ssh.py` [302], `environments/file_sync.py` [500: push side], `tools/process_registry.py`
  [2766: notifications, watch patterns], `tools/checkpoint_manager.py` [1673: header],
  `website/docs/user-guide/docker.md` [949].
- **Write:** `backends/` docker, ssh, remote sync, process notifications, checkpoints, worktrees.
- **Done when:** Docker passes env by name only (cmdline check); background notification arrives;
  contract suite green on three backends.

### P12 · TUI → Demo 2 — [phases/P12-tui-demo2.md](phases/P12-tui-demo2.md)
- **Goal:** `indus --tui`: an Ink app over a Python stdio bridge with streaming, tool trail,
  cards, pickers, themes and crash recovery.
- **Read in reference:** `tui_gateway/server.py` [2185: emit, ask, approval], `tui_gateway/entry.py`
  [395], `ui-tui/src/gatewayClient.ts` [974], `apps/shared/src/json-rpc-channel.ts` [613],
  `ui-tui/src/app/createGatewayEventHandler.ts` [1824: the switch], `turnController.ts` [1188],
  `components/textInput.tsx` [1896: keys], `components/appChrome.tsx` [1030].
- **Write:** `tui_bridge/`; `@indus/client`; `apps/tui` transcript, composer, cards, pickers,
  status rule, themes, terminal-mode helpers.
- **Done when:** **Demo 2**: kill the Python child mid-turn and the TUI recovers; terminal modes
  reset on every exit path.

### P13 · MCP, plugins, hooks, hub — [phases/P13-mcp-plugins-hooks-hub.md](phases/P13-mcp-plugins-hooks-hub.md)
- **Goal:** extensions: MCP servers' tools, plugins with hooks, external memory providers, the
  skills hub with scanning.
- **Read in reference:** `tools/mcp_tool.py` [742] (+ `mcp_tool_*` ~6,000), `tools/mcp_oauth.py`
  [1269], `hermes_cli/plugins.py` [2235], `plugins_loader.py` [762], `plugins_manifest.py` [560],
  `agent/shell_hooks.py` [842], `tools/skills_hub*.py` [~3,800], `tools/skills_guard.py` [958].
- **Write:** `mcp/`, `plugins/` (manifest, discovery, allow-list, hooks bus, install/scan),
  memory provider API, hub.
- **Done when:** name clamp + hash suffix test; a hung pre-tool hook blocks its tool; hub refuses
  a `dangerous` scan.

### P14 · Cron — [phases/P14-cron.md](phases/P14-cron.md)
- **Goal:** scheduled agent and script jobs with the reference grammar (P6), at-most-once firing.
- **Read in reference:** `cron/jobs.py` [3484: grammar, store], `cron/scheduler.py` [4425: tick,
  dispatch], `cron/scheduler_delivery.py` [1923], `tools/cronjob_tools.py` [1200],
  `hermes_cli/cron.py` [1005].
- **Write:** `cron/` grammar, store, ticker, runner, delivery; cron tool; `indus cron` commands.
- **Done when:** attempt recorded before dispatch, unknown never re-run; DST fall-back test (M4).

### P15 · Gateway core → Demo 3 — [phases/P15-gateway-core-demo3.md](phases/P15-gateway-core-demo3.md)
- **Goal:** one gateway daemon per profile (P8) on Telegram, Discord and Slack plus API server and
  webhook, with durable delivery.
- **Read in reference:** `gateway/run.py` [5245], `gateway/run_inbound.py` [1987],
  `gateway/run_busy.py` [1528], `gateway/platforms/base.py` [4904], `gateway/session.py` [1358],
  `gateway/delivery_ledger.py` [564], `gateway/stream_consumer*.py` [~2,100],
  `plugins/platforms/telegram/` [8427], `discord/` [9004], `slack/` [7754],
  `gateway/platforms/api_server.py` [4611].
- **Write:** `gateway/` daemon, service unit, control socket, authz/pairing, session keys, busy
  guards, chunker, streaming, ledger, approvals over chat, API server, webhook, three adapters.
- **Done when:** **Demo 3**: kill between "reply produced" and "acknowledged", restart, see a
  labelled recovered reply; default-deny auth tests.

### P16 · More gateway platforms — [phases/P16-more-platforms.md](phases/P16-more-platforms.md)
- **Goal:** every other chat platform behind the same adapter contract.
- **Read in reference:** `plugins/platforms/<name>/` per adapter (whatsapp [1412] + bridge [2149],
  signal [1657], matrix [3533], feishu [5684], wecom [2170], weixin [1465], qqbot [2838],
  google_chat [2585], others 300–2,000 each).
- **Write:** one `gateway/adapters/<platform>/` package per platform, each with the shared
  fake-platform contract suite.
- **Done when:** every adapter passes the contract suite (M5).

### P17 · Daemon as shared backend — [phases/P17-daemon-shared-backend.md](phases/P17-daemon-shared-backend.md)
- **Goal:** the daemon hosts `SessionService` for every surface; CLI and TUI attach by default
  with `--local` fallback (D-16.10).
- **Read in reference:** `gateway/session_authority.py` [1212] (+ `session_*.py`, 30–900 each),
  `gateway/control_socket.py` [542], `gateway/status.py` [2123], `tui_gateway/event_replay.py`
  [178], `hermes_state_runtime.py` [933].
- **Write:** `service/` turn lease, generation fences, event replay, server-request re-delivery;
  daemon WS endpoint with single-use tickets; attach mode for CLI and TUI.
- **Done when:** two processes cannot both write one session; every surface test runs against both
  hosts.

### P18 · Web dashboard — [phases/P18-web-dashboard.md](phases/P18-web-dashboard.md)
- **Goal:** the dashboard: auth gates that fail closed (D-16.25), REST for every admin page, SPA
  pages, and a chat tab embedding the real TUI.
- **Read in reference:** `hermes_cli/web_server.py` [1473], `web_server_dashboard.py` [1191],
  `web_routers/chat_ws.py` [787], `pty_bridge.py` [381], `dashboard_auth/*.py` [3087],
  `web/src/lib/api.ts` [2811], `web/src/pages/ChatPage.tsx` [2073], `SessionsPage.tsx` [2158].
- **Write:** `web/` server, auth, REST routers, WS rpc/pty/events; `apps/web` pages and chat tab.
- **Done when:** non-loopback start refused without a login provider; PTY keep-alive survives tab
  switch and reload (M6).

### P19 · ACP + MCP server mode — [phases/P19-acp-mcp-serve.md](phases/P19-acp-mcp-serve.md)
- **Goal:** editors drive sessions over ACP (attach or `--local`); other agents read and send
  messages through `indus mcp serve`.
- **Read in reference:** `acp_adapter/gateway_server.py` [962], `acp_adapter/server.py` [1099],
  `acp_adapter/session.py` [582], `acp_adapter/tools.py` [853], `permissions.py` [153],
  `mcp_serve.py` [763].
- **Write:** `acp/` both modes, permission bridge, replay ordering; `mcp_serve/` tools and
  long-poll events (D-16.67).
- **Done when:** history replay completes before the load response; approval tools absent until
  wired (D-16.30).

### P20 · Desktop app → Demo 4 — [phases/P20-desktop-demo4.md](phases/P20-desktop-demo4.md)
- **Goal:** the Electron app: secure shell, runtime installer, chat, sidebar, panes, settings,
  onboarding, deep links, HUD, quick entry, updates; unsigned builds on three OSes.
- **Read in reference:** `apps/desktop/electron/main.ts` [18,619: named parts only], `preload.ts`
  [717], `hardening.ts` [657], `bootstrap-runner.ts` [1048], `window-open-policy.ts` [106],
  `src/app/gateway/hooks/use-gateway-boot.ts` [1751], `src/store/gateway.ts` [2484],
  `src/app/settings/` [26,308: per page], `electron-builder.config.cjs` [350].
- **Write:** `apps/desktop` main, preload bridge with sender checks, CSP, fuses, boot overlay,
  renderer surfaces on `@indus/ui` and `@indus/client`.
- **Done when:** **Demo 4**: clean-machine install, onboarding, chat with panes; every IPC handler
  rejects a foreign sender; transcript-once oracle green (M7).

### P21 · Remaining backends — [phases/P21-remaining-backends.md](phases/P21-remaining-backends.md)
- **Goal:** Singularity, Modal (direct only, D-16.16), Daytona and Vercel Sandbox behind the shared
  contract, with sync-back rules (R02.31) and SDK deadlines (R02.30).
- **Read in reference:** `tools/environments/singularity.py` [215], `modal.py` [282],
  `daytona.py` [207], `vercel_sandbox.py` [421], `file_sync.py` [500: sync-back half],
  `remote_common.py` [99], `tools/terminal_tool_backends.py` [373].
- **Write:** sandbox records, SDK call wrapper, stdin staging, four backends, sync-back, vendor
  env hygiene, probes, doctor/setup entries, per-task overrides, fake SDKs, contract suite.
- **Done when:** contract suite green over seven backends; one live run per vendor recorded.

### P22 · Research tooling — [phases/P22-research-tooling.md](phases/P22-research-tooling.md)
- **Goal:** batch trajectory generation that resumes without paying twice, our own documented
  trajectory format (D-16.59), a compressor with remote code off (D-16.60), eval conventions.
- **Read in reference:** `batch_runner.py` [1020], `toolset_distributions.py` [106],
  `agent/trajectory.py` [56], `agent/agent_runtime_helpers.py` [3807: trajectory helpers ~111–230],
  `trajectory_compressor.py` [868], `mini_swe_runner.py` [406], `evals/compaction/runner.py` [473].
- **Write:** `research/` dataset, distributions, converter, stats, checkpoint, batch IO, worker,
  orchestration, saver, compressor, SWE runner, eval kit; `indus research` CLI.
- **Done when:** a 100-prompt batch killed mid-run resumes with no prompt run twice; compression
  under target with a rejects file for timeouts.

### P23 · Packaging and release — [phases/P23-packaging-release.md](phases/P23-packaging-release.md)
- **Goal:** install via `install.sh`, `install.ps1`, Docker (our own slim image, D-16.64) and Nix;
  `update`/`uninstall` finished; a release pipeline on GitHub Releases + GHCR (D-16.58) publishing
  only tested bytes; SemVer + canary (D-16.56); one signing path per desktop OS. No PyPI (P5).
- **Read in reference:** `scripts/install.sh` [1298], `scripts/install.ps1` [1670],
  `Dockerfile` [543], `docker/stage2-hook.sh` [792], `flake.nix` [57] + `nix/` [5646],
  `hermes_cli/update_cmd.py` [1968: phase dispatch], `hermes_cli/uninstall.py` [1211],
  `scripts/release.py` [725], `scripts/releases/` [`versioning.py` 213, `stable.py` 1040],
  `apps/desktop/scripts/` signing hooks [~1,300].
- **Write:** build guard, bootstrap pins, both installers from fragments, product and sandbox
  images, entry scripts, compose, Nix flake and modules, macOS/Windows signing, update channels/
  apply/finish, uninstall, versioning, release CLI, release workflow and publication controller.
- **Done when:** fresh-VM installs on Ubuntu, macOS and Windows; Docker and Nix lanes green; a
  release dry run plans the full release (M8 ship path).

### P24 · Multiplex and extras — [phases/P24-multiplex-extras.md](phases/P24-multiplex-extras.md)
- **Goal:** several profiles in one gateway (scope binding, fail-closed reads), hosted rooms,
  relay, A2A, kanban board and workers, projects, LSP, verify, import-agent, desktop pet and wake
  word; Termux/MSIX only if capacity (D-16.15).
- **Read in reference:** `gateway/hosted_room*.py` [~8,500], `gateway/relay/` [5430],
  `plugins/platforms/a2a/` [2038], kanban modules [~17,700 across tools and CLI],
  `agent/lsp/` [4010], `agent/verify/` [628], `hermes_cli/projects_cmd.py` [241],
  `hermes_cli/agent_import.py` [665], `website/docs/developer-guide/multiplexing-gateway.md`.
- **Write:** gateway scope router, rooms, relay, A2A adapter, kanban store/workers/board, projects,
  LSP client, verify recipes, importer, desktop pet and wake word.
- **Done when:** two profiles served by one gateway with cross-profile reads failing closed (M8).

### P25 · Hardening, parity, release → v1.0 — [phases/P25-hardening-parity-release.md](phases/P25-hardening-parity-release.md)
- **Goal:** a release candidate the owner can tag: security review done, REA parity pass and
  obligation ledger closed, acceptance map `--strict` and the traps ledger complete, performance
  budgets, docs site, generated `THIRD_PARTY_NOTICES.md`, similarity `--closest` report with
  verdicts, release checklist.
- **Read in reference:** `website/docs/user-guide/security.md` [1016: boundary list],
  `website/docusaurus.config.ts` [217] and `website/sidebars.ts` [920: structure only],
  `website/docs/reference/` [titles only], `tools/environments/local.py` [1178: Windows branches].
- **Write:** security review + static guards + security suites, dependency audit, parity runner,
  scripted model server, scenarios, obligation ledger, parity report, strict acceptance and
  complete traps checks, perf budgets, MkDocs site with generated reference pages, notices
  generator, closest-regions report, artefact sweep, release checklist, fresh-clone gates,
  canary rehearsal, smoke and rollback kit, native-Windows Git Bash backend (best effort).
- **Done when:** every gate green from a fresh clone on the candidate commit; parity ledger with no
  open or failed rows; checklist ends "Ready for `v1.0.0` at `<sha>`"; agents stop and the owner
  tags. **M9:** the candidate installed from the public channels on three OSes.

## 4. After v1.0

From `16-what-we-change.md` §2.8 and the "Later" column of §2.4. Each item keeps its D-id; building
one moves its row to §2.7.

- **D-16.41** Reading or importing a reference home (sessions export, memory, skills): an optional
  one-way importer from a field-mapping spec, owner's call.
- **D-16.45** A curated plugin catalog and optional-MCP catalog, once there is a review process and
  hosting.
- **D-16.46** An out-of-process plugin host (plugins run in-process behind allow-list, scan and
  consent in 1.0).
- **D-16.47** A separate native setup installer app (the desktop's in-app installer covers 1.0).
- **D-16.48** A light, remote-only desktop variant.
- **D-16.49** Telemetry and install-receipt upload (1.0 keeps receipts local; opt-in only if the
  owner asks).
- **D-16.50** Import from another agent product's home (OpenClaw migration).
- **D-16.51** Non-English locales and a docs translation, written fresh with human review.
- **D-16.15** Termux APT channel and MSIX/Store packages, if P24 had no capacity for them.
- Subprocess "external agent" provider transport, if the owner agrees (§2.4).
- Dashboard plugins beyond the SDK basics (owner).
- Object storage with channel records for release artefacts beyond GitHub Releases (D-16.58).
- Not planned at all: the vendor portal provider and its commands (**D-16.12**) and impersonating
  subscription OAuth (**D-16.11**); the managed Modal gateway (**D-16.16**).
