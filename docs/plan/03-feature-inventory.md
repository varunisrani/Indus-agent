# 03 · Feature inventory (what "same features" means for Indus Agent)

Every user-visible feature of the reference agent that the research found, restated in our own
words and given a tier. The owner chose **full parity**: every row is in scope eventually. Tiers
only order the work. Rows changed or removed by the product decisions in `README.md` (P1–P8, plus
the lead defaults) say so in the Feature cell; rows that Indus adds and the reference lacks say
"Added by Indus".

How to read this file:

- **Ids** are an area letter plus a number (`C12`). They never change and are never reused. A
  feature found later takes the next free number in its area.
- **Tier** is binding for order. `P0 → P1` means the plain part is P0 and the rich part is P1; only
  the P0/P1 part is gated by acceptance tests. `—` means dropped by a decision.
- **Surfaces**: `core` (the agent engine, every surface inherits it), `CLI` (classic command line
  and REPL), `TUI` (terminal UI), `D` (desktop app), `W` (web dashboard), `GW` (messaging gateway),
  `API` (OpenAI-compatible HTTP server and webhook receiver), `ACP` (editor adapter), `MCP` (MCP
  server mode), `ops` (installers, packaging, CI).
- **Spec** cites `research/<nn>-*.md` by number and section: `06 §3.4` = gateway spec, section 3.4.
  "docs" means the reference's user docs were the only source (the feature still needs a spec pass
  in the Read step).

| Tier | Meaning | Target |
|---|---|---|
| **P0** | Walking skeleton: without it nothing works end to end. | M1–M2 |
| **P1** | Core parity: what a daily user of the reference would miss within a day. | v1.0 |
| **P2** | Power features heavy users rely on. | v1.x |
| **P3** | Extras, niche platforms, vendor-specific or ambient features. | later, still in scope |

Area letters (the template's letters were replaced to fit an agent product):
A agent core and model providers · B conversation and sessions · C tools · D terminal backends and
sandboxing · E approvals and safety · F learning loop · G extensibility (plugins, hooks, MCP
client) · H automation (cron, task board) · I CLI, configuration and auth · J terminal UI ·
K messaging gateway and platforms · L web dashboard · M editor adapter (ACP) and MCP server ·
N desktop app · O research tooling · P install, distribution, update and CI · Q platform/OS
support and localisation.

Decision tags used below: **P1**–**P8** are the product decisions in `README.md`; "lead default"
refers to the defaults listed under them.

---

## A. Agent core and model providers

| # | Feature | Tier | Surfaces | Spec |
|---|---|---|---|---|
| A1 | One agent object turns a user message into a final answer by calling the model, running requested tools and looping until done; one extra tool-free "summarise what you did" call when the iteration budget runs out | P0 | core | 01 §3.1, §3.2, §3.18 |
| A2 | Durable ordering: the user's message is saved before the first model call and each tool-call message is saved before any tool runs, so a crash never repeats a side effect | P0 | core | 01 §3.3, §3.15 |
| A3 | Strict role alternation is kept on every path (interrupt, timeout, bad tool name, crash); every tool call gets exactly one result; a sanitiser cleans each outgoing copy | P0 | core | 01 §3.6, §3.7 |
| A4 | System prompt built in stable / context / volatile layers once per conversation, stored with the session and reused byte for byte on resume; the only rebuild point is context compaction | P0 | core | 01 §3.4 |
| A5 | Persona: a persona file in the profile home (our name `PERSONA.md`, proposal in `17-naming-and-glossary.md`) seeded on first run and never overwritten; empty or unreadable falls back to a built-in identity written fresh for Indus | P0 | core | 01 §3.5, 04 §3.9 |
| A6 | Project context discovery: native `.indus.md`/`INDUS.md` nearest to cwd, else an `AGENTS.md` chain from repo root to cwd, else `CLAUDE.md`, else cursor rules; each file injection-scanned and head/tail truncated by window size | P1 | core | 01 §3.5 |
| A7 | Subdirectory hints: when a tool touches a new directory inside the workspace, that directory's agent instructions file is appended to the tool result | P2 | core | 01 §3.5 |
| A8 | `@` references (`@file:`, `@folder:`, `@diff`, `@git:`, `@url:`, plugin prefixes) expand into the user message; credential paths refused | P1 | CLI TUI D GW | 01 §3.5, 05 §3.9 |
| A9 | Per-surface platform hint in the prompt (CLI, TUI, desktop, cron, API, each messaging platform), user-overridable with append/replace in config | P1 | core | 01 §3.4 |
| A10 | Model-family guidance blocks (act-don't-describe, execution discipline, developer-role models) switched on by model-name substrings; all text written fresh (lead default) | P1 | core | 01 §3.4 |
| A11 | Date-only timestamp line with zone, plus a runtime block naming OS, home and working directory (or a probe of the remote terminal backend) | P1 | core | 01 §3.4 |
| A12 | Surface switch and skills-index changes delivered as one-shot notes on the next user message instead of rebuilding the prompt | P2 | core | 01 §3.4 |
| A13 | Interrupt from any thread: the in-flight model call is abandoned without leaking partial text, unstarted tools are marked skipped, running ones cancelled | P0 | core | 01 §3.17 |
| A14 | Steer: extra guidance typed during a turn is delivered as its own user message after the current tool batch; never silently lost once accepted | P1 | core | 01 §3.17 |
| A15 | Redirect: a correction during a model call cancels only that call, keeps finished work and retries with the correction | P1 | core | 01 §3.17 |
| A16 | Streaming by default with read/connect timeouts and stale-stream detection; a session flips to non-streaming when a provider refuses streams → stale timeouts scaled by context size, reasoning floors and a 5-strike breaker | P0 → P1 | core | 01 §3.9 |
| A17 | Partial-stream continuation: text already shown is kept and continued after a mid-stream failure; half-streamed tool calls never run | P1 | core | 01 §3.9 |
| A18 | Think-tag scrubber across deltas and a repetition guard against runaway loops | P1 | core | 01 §3.9 |
| A19 | Error classifier with a closed set of reasons (auth, billing, rate limit, overloaded, context overflow, content policy, …) and per-status rules | P0 → P1 | core | 01 §3.10 |
| A20 | Retry with `Retry-After` honoured and jittered exponential backoff, interruptible; retry chatter shown only if all retries fail | P0 | core | 01 §3.11 |
| A21 | Auto-recovery: a turn that failed transiently before any output is parked with a visible countdown and retried a few times | P2 | core | 01 §3.11 |
| A22 | Plain-language terminal failure message naming what happened, what to do and when a limit resets; every failed result carries a reason and a retryable flag | P1 | core | 01 §3.2, §3.11 |
| A23 | Credential pools: several keys or OAuth grants per provider with fill-first / round-robin / least-used / random strategies and cooldowns, tried before fallback | P2 | core CLI | 01 §3.12, 04 §3.16.3 |
| A24 | Fallback chain of provider+model pairs used after retries fail; the primary is restored at the start of the next turn | P1 | core | 01 §3.13 |
| A25 | Output-length recovery: continuation nudges for cut-off text, larger output caps for cut-off tool calls, never running incomplete arguments | P1 | core | 01 §3.14 |
| A26 | Empty or thinking-only reply recovery ladder ending in a clear explanation instead of a blank message | P1 | core | 01 §3.14 |
| A27 | Stall nudges: a short reply that only announces its next step is asked to continue | P2 | core | 01 §3.14 |
| A28 | Stop gates: before finishing, late language-server diagnostics for edited files, verify-on-stop and a plugin verify hook may send the agent back to work | P2 | core | 01 §3.14 |
| A29 | Tool round: unknown tool names auto-repaired or answered with valid names, duplicate calls dropped, malformed JSON re-requested, results returned in call order | P0 | core | 01 §3.15 |
| A30 | Safe parallel tool execution: read-only tools and non-overlapping file paths run concurrently, interactive tools act as barriers | P1 | core | 01 §3.15 |
| A31 | Untrusted-content fencing for web, browser and MCP results so the model treats them as data | P1 | core | 01 §3.15 |
| A32 | Tool-loop guardrails: repeated identical failures warn then block, per-turn caps on web searches and subagent spawns, hard stop only on unattended surfaces | P1 | core | 01 §3.15 |
| A33 | Toolsets: named bundles with includes, a shared core list, per-platform bundles and posture sets (coding, safe, debugging), selected per run with `-t` | P0 → P1 | core | 01 §3.16, 02 §3.2 |
| A34 | Iteration cap (`max_turns`) and optional wall-clock run budget with a single wrap-up notice at 80 % | P0 → P1 | core CLI | 01 §3.18, 04 §3.4.1 |
| A35 | Context-window resolution: config override, endpoint metadata, `/models`, local probes, family table, 256K default; models below 64K refused | P1 | core | 01 §3.8 |
| A36 | Automatic context compression: provider-measured threshold, old tool output pruned, one summary call by the auxiliary model, deterministic fallback summary, escalating cooldowns and an attempt cap | P1 | core | 01 §3.19 |
| A37 | Manual `/compress` with focus topic, `here [N]` and preview | P1 | CLI TUI D GW ACP | 01 §3.19, 04 §3.7.3 |
| A38 | Overflow recovery for payload-too-large, output-cap-too-large and prompt-too-long errors | P1 | core | 01 §3.19 |
| A39 | Provider-native server-side compaction on supported routes; micro-compaction between turns (off by default) | P3 | core | 01 §3.19 |
| A40 | Prompt-cache markers for providers that use them, with 5-minute / 1-hour / auto TTL policy | P1 | core | 01 §3.19 |
| A41 | Usage and cost accounting: canonical token buckets, cost status (actual, estimated, included, unknown), rate-limit headers kept for the usage view | P1 | core | 01 §3.20 |
| A42 | Auxiliary model client for side tasks (summaries, titles, vision, search, memory, curator) with per-task provider/model pins and its own fallback | P1 | core | 01 §3.21 |
| A43 | Provider registry of declarative profiles found from bundled, user and entry-point plugins; lookup by name or alias | P0 | core | 01 §3.8 |
| A44 | OpenAI-compatible Chat Completions transport (aggregators, custom endpoints, local servers, most vendors) | P0 | core | 01 §3.8 |
| A45 | Anthropic Messages transport with budgeted and adaptive thinking | P0 | core | 01 §3.8 |
| A46 | OpenAI Responses transport with reasoning items and incomplete-status handling | P1 | core | 01 §3.8 |
| A47 | AWS Bedrock Converse transport | P2 | core | 01 §3.8 |
| A48 | Native Gemini `generateContent` path | P2 | core | 01 §3.8 |
| A49 | External-agent runtime that hands whole turns to another vendor's agent process (the reference's Codex app-server route), opt-in only | P3 | core CLI | 01 §3.8, docs |
| A50 | Each provider key is sent only to its own host, matched by exact hostname; saved config beats stale shell exports | P0 | core | 01 §3.8 |
| A51 | API-mode auto-detection from the base URL (Responses for official OpenAI hosts, Messages for `/anthropic` paths) | P1 | core | 01 §3.8 |
| A52 | In-place model switch mid-session that rolls back atomically on failure | P1 | core | 01 §3.8 |
| A53 | Reasoning effort ladder `none … ultra` with per-model overrides, clamped to the strongest supported level not above the request | P1 | core CLI | 01 §3.8, 04 §3.17.3 |
| A54 | Aggregator routing preferences (sort by price/throughput/latency, allow/ignore/order lists) | P2 | core | 01 §3.8 |
| A55 | Bundled provider catalogue of about forty vendors and gateways (key-based ones first) | P0 → P2 | core | 01 §3.8, 04 §3.16.1 |
| A56 | Named custom OpenAI-compatible providers, with `ollama`/`vllm`/`llamacpp` as aliases for local servers | P1 | core CLI | 04 §3.16.1 |
| A57 | First-party hosted "portal" provider with its own device login and rate guard. Dropped (P3 decision) | — | core | 01 §3.8, 04 §3.16 |
| A58 | Subscription OAuth routes that pose as another vendor's client, and borrowing Claude Code / Codex CLI logins. Dropped (P2 decision) | — | core CLI | 01 §6 R01.12, 04 §3.16.7 |
| A59 | Vendor OAuth and device-code sign-in (xAI, Qwen, MiniMax, OpenRouter key mint) — only where the vendor documents the flow (P2 decision) | P3 | CLI D | 04 §3.16.4–3.16.5 |
| A60 | Mixture-of-agents turns with named presets of reference models plus an aggregator | P3 | core CLI | 01 §3.24, 04 §3.17.5 |
| A61 | Side question (`/btw`): a detached copy answers about the conversation without changing it | P2 | CLI TUI GW | 01 §3.24 |
| A62 | Fast mode: a provider priority tier applied for a bounded window | P3 | core | 01 §3.24 |
| A63 | Cross-process turn lease per session and a liveness watchdog that ends idle turns | P1 | core | 01 §3.23 |
| A64 | Closed set of turn exit reasons logged for every turn | P1 | core | 01 §3.2 |
| A65 | Persistent goals (`/goal`, `/subgoal`): a judge model checks each answer and keeps the session working until the goal is met | P2 | CLI TUI GW | 04 §3.7.3, docs |
| A66 | In-session recurring loops (`/loop`) and idle heartbeats (`/heartbeat`) that fire as ordinary user turns | P2 | CLI TUI GW | 04 §3.7.3, docs |
| A67 | Use the agent as a Python library: construct it in code and run turns | P2 | core | 01 §3.1, docs |
| A68 | Context-engine interface so a plugin can replace the built-in compressor | P2 | core | 01 §3.19 |

## B. Conversation and sessions

| # | Feature | Tier | Surfaces | Spec |
|---|---|---|---|---|
| B1 | One SQLite store per profile: single writer with time-budgeted retries, WAL that is never silently downgraded, read-only opens for listings | P0 | core | 03 §3.2–3.5 |
| B2 | Sessions persist across restarts and resume by id, title or `latest` (`-r`, `-c`, `/resume`) | P0 | CLI TUI D GW | 03 §3.8, 04 §3.4.3 |
| B3 | Per-terminal continue breadcrumb so parallel terminals resume their own conversations; otherwise most recent in this workspace | P1 | CLI | 04 §3.4.3 |
| B4 | Session ids by surface and source tags; internal sources (subagent, tool, board) hidden from default lists | P0 | core | 03 §3.8 |
| B5 | Titles: unique, sanitised, user > model > derived precedence, lineage numbering `name #2`; auto-title after the first exchange | P1 | core | 03 §3.12 |
| B6 | Rich session listing with preview, last activity and filters (source, workspace, size) | P1 | CLI W D | 03 §3.8, 04 §3.20 |
| B7 | Full-text search over all past messages with snippets → trigram and CJK bigram indexes, LIKE fallback, fail-open when the index is damaged | P1 → P2 | CLI W D | 03 §3.13 |
| B8 | New session (`/new`, `/clear`), retry last turn (`/retry`), undo the last N turns (`/undo N`) | P0 → P1 | CLI TUI D GW ACP | 03 §3.11, 04 §3.7.3 |
| B9 | Branch a session into an independent copy (`/branch`, `--here`) | P1 | CLI TUI D GW ACP | 03 §3.11 |
| B10 | Compression lineage: summary continuations or in-place compaction, with display history that still shows pre-compaction turns | P1 | core | 03 §3.10, 01 §3.19 |
| B11 | Archive, pin, hide and unread state applied to a whole lineage | P2 | D W | 03 §3.8 |
| B12 | Export: JSONL, Markdown, HTML, trace, prompts-only, with optional redaction; `/save json|md|html` in session | P1 | CLI W | 03 §3.24, 04 §3.20 |
| B13 | Import JSONL exports with strict validation limits | P2 | CLI | 03 §3.24 |
| B14 | Import sessions from other coding agents (Claude Code, Codex), including a `--resume @claude` picker | P3 | CLI D | 04 §3.4.3, §3.20 |
| B15 | Delete sessions (cascading to subagent children) with tombstones so a deleted id can never come back | P1 | CLI W D | 03 §3.17 |
| B16 | Manual prune by age/source/title with dry run → opt-in automatic prune and a one-time low-disk prompt | P1 → P2 | CLI W | 03 §3.22 |
| B17 | Automatic archive of idle lineages, orphan sweeps and ghost-row pruning | P3 | core | 03 §3.22 |
| B18 | Insights: token and cost summaries over N days and by source | P2 | CLI W | 04 §3.3 |
| B19 | Resume size guard (20,000 messages) with stable refusal codes | P1 | core | 03 §3.9 |
| B20 | Store safety: replaced-file detection, corruption quarantine, plain-language error buckets with a "run doctor" action | P1 | core | 03 §3.23, §3.25 |
| B21 | Repair and recovery commands: salvage to a new file, schema repair, journal-mode switch, optimise, storage-layout upgrade | P2 | CLI W | 03 §3.23, 04 §3.20 |
| B22 | Multi-process store protections: lost-WAL capture, OFD lock guard, holder scans before maintenance | P2 | core | 03 §3.23 |
| B23 | Resume recap ("previous conversation") printed by the classic CLI | P1 | CLI | 04 §3.6 |
| B24 | Per-session and per-model usage including auxiliary calls, summed across a lineage | P1 | core | 03 §3.14 |
| B25 | Durable input ledger: exact retries return the original result, started-but-unfinished inputs become "unknown" and are never replayed | P2 | core GW | 03 §3.18 |
| B26 | Queue a prompt to run after the current turn (`/queue`, list/edit/move/clear) | P1 | CLI TUI D GW | 04 §3.7.3 |
| B27 | Background session (`/bg`) that runs a prompt separately and posts the result back | P2 | CLI TUI GW | 04 §3.7.3, 06 §3.11 |
| B28 | Hand a CLI session over to a messaging platform (`/handoff`) and pick a platform conversation up again in the CLI | P2 | CLI GW | 06 §3.8 |
| B29 | Isolated git worktree per session (`-w`, `/worktree new|list|prune`) | P2 | CLI TUI | 04 §3.2.1, §3.7.3 |
| B30 | Workspace metadata (cwd, git root, branch) recorded per session; cwd restored on resume unless `--no-restore-cwd`; `--in DIR` scoping | P1 | CLI | 03 §3.8, 04 §3.4.3 |
| B31 | Quick state snapshots in session (`/snapshot create|restore|prune`) | P2 | CLI | 04 §3.23 |
| B32 | Diff of workspace changes from the session (`/diff staged|all|session`) | P2 | CLI TUI GW | 04 §3.7.3 |
| B33 | `/plan` writes a markdown plan into the workspace plans folder | P2 | CLI TUI GW | 07 §3.6 |
| B34 | Configured time zone for every clock the agent shows or schedules with | P1 | core | 03 §3.28 |
| B35 | Message reactions recorded and surfaced once on the author's next turn | P3 | GW D | 03 §3.9 |

## C. Tools

| # | Feature | Tier | Surfaces | Spec |
|---|---|---|---|---|
| C1 | Self-registering tool registry with cached availability probes; every handler returns a string and errors become bounded `{"error": …}` results | P0 | core | 02 §3.1 |
| C2 | Argument coercion and per-provider schema sanitising so one odd schema cannot break a request | P1 | core | 02 §3.1a |
| C3 | Result size budget: per-result and per-turn caps scaled to the window, oversized output spilled to a file with a preview and paging advice | P0 → P1 | core | 02 §3.4 |
| C4 | Tool search bridge (`tool_search`, `tool_describe`, `tool_call`) that hides deferrable tools when they would crowd the window | P2 | core | 02 §3.3 |
| C5 | `terminal` foreground: timeout, exit-code meanings, failure hints, redaction, stable result shape | P0 | core | 02 §3.6–3.7a |
| C6 | `terminal` background processes with notify-on-exit, output watch patterns, heartbeats and auto-backgrounding of long or `&` commands | P1 | core | 02 §3.6, §3.12 |
| C7 | `process_manage`: list, poll, log, wait, kill, write, submit, close, hand off to parent | P1 | core | 02 §3.12 |
| C8 | Yield a running local command to the background when the user starts typing | P2 | CLI TUI | 02 §3.7, §3.24 |
| C9 | Retained, redacted receipts of finished background processes readable by the owning conversation | P2 | core | 02 §3.12 |
| C10 | `read_file`: line-numbered paging, device and secret-file guards, repeat-read loop breaker → document extraction for PDF, Office, notebooks, EPUB, RTF, SQLite | P0 → P1 | core | 02 §3.15 |
| C11 | `write_file`: refuses to overwrite a file the agent has not fully read, protected paths, hash verification | P0 | core | 02 §3.15 |
| C12 | `patch`: exact replace → nine fuzzy matching strategies and the multi-file V4A patch format | P0 → P1 | core | 02 §3.15 |
| C13 | `search_files`: ripgrep content and filename search with output modes | P0 | core | 02 §3.15 |
| C14 | Lint after edit reporting only newly introduced errors → language-server diagnostics | P1 → P2 | core | 02 §3.15 |
| C15 | Language-server manager (`indus lsp`) running servers in the background for diagnostics | P2 | CLI | docs, 04 §3.3 |
| C16 | Automatic filesystem checkpoints before the first change in a turn; `/rollback [N]`, rollback diff, per-file restore | P1 | CLI TUI D GW | 02 §3.26, 04 §3.7.3 |
| C17 | `web_search` through pluggable backends (Exa, Tavily, Brave, SearXNG, DuckDuckGo, …) with a keyless rescue | P1 | core | 02 §3.16 |
| C18 | `web_extract` with URL normalisation, SSRF checks, per-page truncation and stored full text | P1 | core | 02 §3.16 |
| C19 | SSRF policy (private and metadata ranges blocked after DNS) and a website blocklist | P1 | core | 02 §3.16 |
| C20 | `x_search` (public X search through xAI) | P3 | core | 02 §3.5, §3.28 |
| C21 | Browser tools on local headless Chromium: open a page, take an accessibility snapshot, click, type, scroll, press keys, go back, list images, ask about a screenshot, read the console | P1 | core | 02 §3.17 |
| C22 | Other browser backends: cloud providers, user CDP endpoint, alternative engines, real-profile mode, scripted `browser_exec` | P3 | core | 02 §3.17, §3.25 |
| C23 | `browser_cdp` raw DevTools calls and `browser_dialog` for native dialogs | P2 | core | 02 §3.25 |
| C24 | `browser_vault` model-blind autofill from 1Password, Bitwarden or a local encrypted vault | P3 | core CLI | 02 §3.25, 04 §3.14.5 |
| C25 | Private URLs routed to a local browser when a cloud browser is configured | P3 | core | 02 §3.25 |
| C26 | `/browser connect|disconnect|status|use` | P2 | CLI TUI | 04 §3.7.3 |
| C27 | `vision_analyze`: native image input on vision models, auxiliary vision model otherwise, region crops | P1 | core | 02 §3.18 |
| C28 | `video_analyze` (opt-in toolset) | P3 | core | 02 §3.5 |
| C29 | `image_generate` through provider plugins | P2 | core | 02 §3.18 |
| C30 | `video_generate` plus vendor video edit/extend tools (renamed vendor-neutral, see naming doc) | P3 | core | 02 §3.5 |
| C31 | `text_to_speech` with many providers, chunking and voice-bubble output | P2 | core GW | 02 §3.18 |
| C32 | Speech-to-text providers (local and hosted) used by voice notes and voice mode | P2 | GW CLI D | 02 §3.18 |
| C33 | `todo_list` with statuses, merge/replace, a live panel, and re-injection after compression | P1 | core TUI D | 02 §3.21 |
| C34 | `clarify`: 1–5 questions with choices and multi-select, answered on every surface | P1 | core | 02 §3.5, §3.21 |
| C35 | `execute_code`: Python that calls allow-listed tools over RPC, per-call → persistent session kernels and remote kernels | P1 → P2 | core | 02 §3.19 |
| C36 | `delegate_task` subagents: synchronous batch → background children, list/steer/stop, output schema validation | P1 → P2 | core | 02 §3.20 |
| C37 | Optional git worktree per subagent | P3 | core | 02 §3.20 |
| C38 | `computer_use`: background desktop control that never steals cursor or focus | P3 | core D | 02 §3.28 |
| C39 | `manage_connections` and `manage_catalog` for connector and catalog installs from chat | P3 | core D | 02 §3.28 |
| C40 | Platform-specific tools (Discord, Discord admin, Feishu documents, Yuanbao) inside that platform's bundle | P3 | GW | 02 §3.5, §3.28 |
| C41 | Child-process environment scrubbing so provider keys never reach model-written commands, plus output redaction | P0 | core | 02 §3.14 |
| C42 | Sudo support: cached password, masked prompt on interactive surfaces, clear advice on headless ones | P1 | CLI TUI D | 02 §3.23 |

## D. Terminal backends and sandboxing

| # | Feature | Tier | Surfaces | Spec |
|---|---|---|---|---|
| D1 | Shared execution contract for every backend: fresh `bash -c` per call, shell snapshot, cwd marker, bounded capture with overflow log, hard timeout backstop | P0 | core | 02 §3.9 |
| D2 | Local backend: process groups, scratch temp dir, PATH repair, persistent-shell switch | P0 | core | 02 §3.10 |
| D3 | Docker backend: dropped capabilities, resource limits, persistent per-task dirs, reuse by label, recovery when the container vanished, orphan reaper (P7: first tools phase) | P1 | core | 02 §3.10 |
| D4 | SSH backend: multiplexed connection, remote home detection, file sync (P7: first tools phase) | P1 | core | 02 §3.10, §3.11 |
| D5 | Singularity/Apptainer backend (P7: later phase) | P2 | core | 02 §3.10 |
| D6 | Modal backend with your own credentials and filesystem snapshots (P7: later phase) | P2 | core | 02 §3.10 |
| D7 | Vendor-hosted managed Modal gateway. Dropped (P7 decision) | — | core | 02 §3.10 |
| D8 | Daytona backend (P7: later phase) | P2 | core | 02 §3.10 |
| D9 | Vercel Sandbox backend (P7: later phase) | P2 | core | 02 §3.10 |
| D10 | Plugin-provided terminal backends behind the same contract | P2 | core | 02 §3.10b |
| D11 | Environment cache per key with creation lock, idle reaper, per-profile isolation, clean teardown at exit | P0 → P1 | core | 02 §3.8 |
| D12 | Remote file sync: credential files and skills pushed before commands, changed files synced back on cleanup | P1 | core | 02 §3.11 |
| D13 | Degraded-backend result with a retry hint when SSH or Docker is unreachable; the environment is rebuilt on the next call | P1 | core | 02 §3.6 |
| D14 | Terminal settings: default and maximum timeouts, lifetime, env passthrough, credential files, container resources | P1 | core CLI | 02 §3.10a |
| D15 | Egress credential-injection proxy for Docker sandboxes (`indus egress`) so sandboxes hold only proxy tokens | P3 | core CLI | docs, 02 §3.10 |
| D16 | Memory caps for background processes through transient systemd scopes on Linux | P3 | core | 02 §3.12 |
| D17 | Guard against the agent rewriting the app's own git checkout (Windows) | P3 | core | 02 §3.6 |

## E. Approvals and safety

| # | Feature | Tier | Surfaces | Spec |
|---|---|---|---|---|
| E1 | Dangerous-command detection with normalisation (wrappers, interpreter `-c` payloads, quoting tricks, encoded pipes) | P0 → P1 | core | 02 §3.13 |
| E2 | Hardline floor (disk wipes, fork bombs, deleting system roots) and user deny globs that nothing can bypass | P0 | core | 02 §3.13 |
| E3 | Approval modes manual / smart / off, changeable live with `/approvals` | P0 → P1 | CLI TUI D GW | 04 §3.21 |
| E4 | Human choices once / session / always / deny; "always" writes a per-profile allowlist | P0 → P1 | CLI TUI D GW ACP | 02 §3.13 |
| E5 | Smart pre-judge by the auxiliary model with a denial breaker | P2 | core | 02 §3.13 |
| E6 | Auto-approve switch (`--yolo`, `/yolo`, Shift+Tab, env) remembered per session; container backends without host mounts skip prompts | P0 | CLI TUI D GW | 02 §3.13, 04 §3.21 |
| E7 | Unattended policies for cron, one-shot and API/webhook runs, deny by default | P1 | core | 02 §3.13 |
| E8 | Approvals over chat: buttons where supported, `/approve`, `/approve all`, `/deny reason`, timeout notice; a bare "yes" never approves | P1 | GW | 06 §3.12 |
| E9 | Whole-script approval for `execute_code` | P1 | core | 02 §3.13 |
| E10 | `approvals suggest` (propose allowlist entries from history) and `approvals test` (dry verdict with trace) | P2 | CLI | 04 §3.21 |
| E11 | Protected instruction files always ask before a write; credential and system paths refused | P1 | core | 02 §3.15 |
| E12 | Secret redaction in logs, tool output, exports and deliveries (on by default) | P0 → P1 | core | 02 §3.14, 03 §3.27 |
| E13 | Prompt-injection and exfiltration scanning of context files, memory writes, cron prompts and skills | P1 | core | 01 §3.5, 07 §3.1 |
| E14 | Warning before an explicitly requested expensive model or data-training tier; non-interactive runs refuse | P2 | CLI | 04 §3.4.4 |
| E15 | `indus security audit` of installed packages, plugins and pinned MCP servers against OSV | P2 | CLI W | 04 §3.24.8 |
| E16 | Emergency pause (`indus pause` / `resume`) stopping new turns and scheduled runs | P2 | CLI GW | 04 §3.28 |
| E17 | Approval request/response hooks and a pluggable approval transport | P2 | core | 02 §3.29, 07 §3.13.2 |
| E18 | Isolation launches: `--safe-mode`, `--ignore-rules`, `--ignore-user-config` | P1 | CLI TUI ACP | 04 §3.2.1 |
| E19 | Optional write-safe root restricting file writes to listed prefixes | P3 | core | 02 §3.15 |

## F. Learning loop (memory, skills, curator)

| # | Feature | Tier | Surfaces | Spec |
|---|---|---|---|---|
| F1 | Two bounded memory stores (agent notes, user profile) shown in the prompt as a frozen snapshot each session | P1 | core | 07 §3.1 |
| F2 | `memory` tool: add / patch / remove / batch with over-limit guidance and a per-turn retry cap; no silent truncation | P1 | core | 07 §3.1 |
| F3 | Memory write approval gate with pending list, approve and reject | P2 | CLI TUI GW | 07 §3.1 |
| F4 | One external memory provider plugin running alongside the built-in stores (prefetch, sync, own tools) | P2 | core | 07 §3.2 |
| F5 | Background review every N turns or tool iterations that saves memory and improves skills, under a tool whitelist and an input budget | P1 | core | 07 §3.3 |
| F6 | `/refine [focus]` runs that review on demand | P1 | CLI TUI GW | 07 §3.3 |
| F7 | `/learn` turns a folder, URL, conversation or notes into a skill | P1 | CLI TUI D W GW | 07 §3.4 |
| F8 | Short "memory updated / skill patched" notices in chat (off / on / verbose) | P2 | GW | 07 §3.3 |
| F9 | Review runs deferred while a local model server is busy | P3 | core | 07 §3.3 |
| F10 | Skill format: folder with `SKILL.md` frontmatter and support folders; visibility by OS, toolset, environment and app | P1 | core | 07 §3.5 |
| F11 | Skill discovery tiers (project, home, create dir, external dirs), duplicate rules and a disabled list | P1 | core | 07 §3.6 |
| F12 | Project skills loaded only after the user trusts the repo, scanned and quarantined if dangerous | P2 | core | 07 §3.6 |
| F13 | Compact skills index in the prompt; added or removed skills announced once mid-session | P1 | core | 07 §3.6 |
| F14 | `skills_list` / `skill_view` progressive disclosure | P1 | core | 07 §3.6 |
| F15 | Every skill is a slash command (`/skill-name`, up to five stacked); `-s` preloads; auto-load list | P1 | CLI TUI D GW ACP | 07 §3.6 |
| F16 | `skill_manage` tool (create, patch, delete, write/remove support files, batch) with limits, locks and an advisory linter | P1 | core | 07 §3.7 |
| F17 | Skill write approval gate with diff review | P2 | CLI GW | 07 §3.7 |
| F18 | Skill template tokens and opt-in inline shell snippets | P2 | core | 07 §3.5 |
| F19 | Skill-declared secrets prompted securely; skill config values injected when loaded | P2 | CLI TUI D | 07 §3.5 |
| F20 | Skill bundles: one command loads several skills with an instruction | P2 | CLI TUI GW | 07 §3.6 |
| F21 | Curator phase 1: idle-triggered aging of agent-made skills to stale then archive, pin, restore, cron-reference protection | P1 | core CLI | 07 §3.8 |
| F22 | Curator phase 2: optional model-driven consolidation with backups and whole-tree rollback | P2 | CLI | 07 §3.8 |
| F23 | Skill mutation ledger with per-entry rollback | P2 | CLI | 07 §3.8 |
| F24 | Per-skill usage counters (used, viewed, patched) | P1 | core | 07 §3.8 |
| F25 | Learning timeline: list, delete or edit what was learned (CLI, TUI, desktop map) | P3 | CLI TUI D | 07 §3.9 |
| F26 | Skills hub: search, install, update, audit and uninstall from the official set, skills.sh, well-known endpoints, URLs and GitHub taps, with scanner and trust policy | P2 | CLI W D | 07 §3.10 |
| F27 | Additional hub sources (ClawHub, LobeHub, browse.sh) and skill publish/snapshot | P3 | CLI | 07 §3.10 |
| F28 | Bundled skills seeded into the home with an origin-hash manifest; user edits never overwritten; opt-out and reset | P1 | core CLI | 07 §3.11 |
| F29 | Starter skill set authored by Indus from scratch; no reference skill text reused (lead default) | P1 | core | 07 §3.12 |
| F30 | Optional "official" skill catalogue installable from the hub | P3 | CLI W D | 07 §3.12 |
| F31 | `session_search` tool for recalling past conversations | P1 | core | 02 §3.5, 03 §3.13 |
| F32 | Personality overlays (`/personality`) | P2 | CLI TUI GW | 04 §3.7.3 |
| F33 | `/init` drafts a project context file for the current repo | P2 | CLI TUI GW | 04 §3.7.3 |

## G. Extensibility (plugins, hooks, MCP client)

| # | Feature | Tier | Surfaces | Spec |
|---|---|---|---|---|
| G1 | Plugin manifest and discovery from bundled, user, project and entry-point sources; general plugins load only when enabled | P1 | core CLI | 07 §3.13.1 |
| G2 | Plugin context API: tools, hooks, slash and CLI commands, skills → providers, platforms, context engines, terminal backends, approval transports | P1 → P2 | core | 07 §3.13.2 |
| G3 | Hook catalogue: pre/post tool (pre blocks fail closed), pre/post LLM, session start/end → the full set of transform and observer hooks with timeouts | P1 → P2 | core | 07 §3.13.3 |
| G4 | Plugin system-prompt sections, cache-safe and size-bounded | P2 | core | 07 §3.13.3 |
| G5 | `indus plugins` install / update / remove / enable / disable from git with commit pins, static scan and dependency consent | P2 | CLI W D | 07 §3.13.5 |
| G6 | Plugin capability consent stored per plugin | P3 | CLI | 07 §3.13.4 |
| G7 | Plugin packs (several pinned plugins plus config seeds) | P3 | CLI | 07 §3.13.5 |
| G8 | Out-of-process plugin host | P3 | core | 07 §3.13.6 |
| G9 | Curated plugin catalogue with a kill list and live refresh | P3 | CLI W D | 07 §3.14 |
| G10 | Plugin event bus | P3 | core | 07 §3.13.3 |
| G11 | Plugins calling configured MCP servers through an allow-list | P3 | core | 07 §3.16.9 |
| G12 | Gateway hook folders (manifest plus handler) for startup, session, agent and command events | P2 | GW | 06 §3.18 |
| G13 | Shell hooks declared in config with first-use consent; `indus hooks list|test|revoke|doctor`; `--accept-hooks` | P2 | CLI | 04 §3.28 |
| G14 | MCP client: stdio, Streamable HTTP and SSE servers; filtered environment; prefixed tool names; include/exclude; bounded parallel discovery; reconnect with backoff; circuit breaker; `/reload-mcp` and config watching | P1 | core | 07 §3.16 |
| G15 | MCP OAuth (PKCE, client registration, device login), mTLS and identity headers | P2 | core CLI | 07 §3.16.2, §3.16.5 |
| G16 | MCP sampling and form elicitation | P2 | core | 07 §3.16.6 |
| G17 | MCP untrusted tier: non-read-only tools need approval | P2 | core | 07 §3.16.4 |
| G18 | Lazy MCP servers, idle recycling and schema cache | P2 | core | 07 §3.16.3–3.16.4 |
| G19 | `indus mcp` add / list / test / remove → picker, catalogue install, configure checklist, login, enable/disable | P1 → P2 | CLI | 07 §3.16.7 |
| G20 | Curated optional MCP catalogue with install flow and suggestion pills | P3 | CLI W D | 07 §3.16.8 |
| G21 | Managed connector accounts. Partly dropped (P3 decision: hosted connector accounts were a vendor service); local MCP management stays under G14–G20 | — | CLI D | 02 §3.28 |

## H. Automation (cron, task board)

| # | Feature | Tier | Surfaces | Spec |
|---|---|---|---|---|
| H1 | Cron jobs stored per profile with locking; each run writes an output document | P1 | core | 07 §3.15.1 |
| H2 | Schedule grammar: natural day/time phrases, `every <dur>`, cron expressions, ISO times, `in <dur>` once, a bare duration repeats. Kept as reference (P6 decision) | P1 | core | 07 §3.15.2 |
| H3 | Gateway ticker every 60 s with on-time / late / catch-up rules and fire claims | P1 | GW | 07 §3.15.3 |
| H4 | Agent jobs run in a fresh session with a scheduling hint, attached skills, output chaining (`context_from`), workdir, toolsets and an optional model pin | P1 | core | 07 §3.15.4 |
| H5 | Script-only jobs whose stdout is the message (no model) | P1 | core | 07 §3.15.5 |
| H6 | Pre-run script with a wake gate | P2 | core | 07 §3.15.4 |
| H7 | Monitor mode: run only when a script's or URL's output changed | P3 | core | 07 §3.15.4 |
| H8 | Preflight checks; a misconfigured job alerts once and costs no tokens | P1 | core | 07 §3.15.4 |
| H9 | Silence token (nothing new → no message) and failure token | P1 | core GW | 07 §3.15.4, 06 §3.13 |
| H10 | Delivery to origin, local files, a platform's home channel, `platform:chat[:thread]`, all, or lists | P1 | GW | 06 §3.17, 07 §3.15.6 |
| H11 | Delivery into a profile's own main chat as an incoming message | P3 | GW | 07 §3.15.6 |
| H12 | Failure streak nudges, retry after transient network failures, quota holds, incidents with acknowledgement | P2 | core GW | 07 §3.15.7 |
| H13 | Execution ledger: an abandoned run becomes "unknown" and is never re-run | P1 | core | 07 §3.15.7 |
| H14 | Runs in restart-safe worker processes under a service manager | P3 | GW | 07 §3.15.7 |
| H15 | `cronjob_manage` model tool (create, list, update, pause, resume, remove, async run); cron runs cannot create jobs unless allowed | P1 | core | 07 §3.15.8 |
| H16 | `/cron` and `indus cron` create/edit/list/pause/resume/run/remove/status/tick → runs history, incidents, doctor | P1 → P2 | CLI TUI GW | 07 §3.15.9 |
| H17 | Per-job notepad (durable key/value cursors) | P3 | core CLI | 07 §3.15.10 |
| H18 | Suggested jobs and blueprints (starter catalogue, skill blueprints, review suggestions); nothing auto-schedules | P3 | CLI W D | 07 §3.15.11 |
| H19 | Continuable deliveries: a thread or labelled DM turn per delivery so the user can reply | P3 | GW | 07 §3.15.6 |
| H20 | Hosted scheduler provider that fires jobs from a vendor service. Dropped (P3 decision); the provider seam stays for third parties | — | GW | 07 §3.15.3 |
| H21 | Task board: durable board shared by profiles, dispatcher, worker lanes, board tools (show, complete, block, review, comment, attach, link, schedule), review/changes flow and subscriptions | P3 | core CLI W D | 02 §3.28, docs |

## I. CLI, configuration and auth

| # | Feature | Tier | Surfaces | Spec |
|---|---|---|---|---|
| I1 | `indus` command; bare `indus` opens a chat; about seventy subcommands in a stable help order | P0 | CLI | 04 §3.2–3.3 |
| I2 | Unknown command answered in three lines with "did you mean" or an install hint | P1 | CLI | 04 §3.2.2 |
| I3 | Global flags: `-p`, `-m`, `--provider`, `-t`, `-r`, `-c`, `--yolo`, `-z`, `-V` → `--reasoning`, `--in`, `-s`, `-w`, `--pass-session-id`, `--checkpoints`, `--tui`/`--cli` | P0 → P1 | CLI | 04 §3.2.1 |
| I4 | Classic interactive REPL: banner, status bar with context meter and cost, multiline keys, external editor, draft stash, busy-input modes | P0 → P1 | CLI | 04 §3.6 |
| I5 | One-shot `-z`: only the final text on stdout, defined exit codes, `--usage-file` JSON | P0 | CLI | 04 §3.5 |
| I6 | `chat -q` seeds an interactive session on a TTY or answers and exits; `--query-file`; `--format stream-json` event stream | P1 → P2 | CLI | 04 §3.4.5–3.4.7 |
| I7 | Documented exit codes for finite runs (0, 1, 2, 3, 75, 130) | P1 | CLI | 04 §3.4.8 |
| I8 | First-run guard that offers setup when no provider is usable | P1 | CLI | 04 §3.4.4 |
| I9 | One slash-command registry for every surface; dispatch order built-in → quick command → plugin → bundle → skill → unique prefix | P0 | CLI TUI D GW ACP | 04 §3.7 |
| I10 | Quick commands in config (`exec` with a scrubbed env and timeout, or alias) | P2 | CLI TUI GW | 04 §3.7.2 |
| I11 | Bang shell `!cmd` on the local host through the same safety guards; nothing enters history | P1 | CLI TUI | 04 §3.8, 05 §3.12.1 |
| I12 | Home resolution (`INDUS_HOME`, `~/.indus`, `%LOCALAPPDATA%\indus`, suffix for side-by-side channels), skeleton dirs, owner-only permissions | P0 | core | 04 §3.9, 03 §3.1 |
| I13 | Profiles: create (optional clone without messaging credentials), list, use (sticky), show, delete with tombstone, rename, describe, wrapper alias commands; `-p` anywhere on the line | P1 | CLI | 04 §3.10 |
| I14 | Profile export and import archives (credentials excluded) | P2 | CLI W D | 04 §3.10.3 |
| I15 | Profile distributions: install and update a whole agent from a git repo with a manifest | P3 | CLI | 04 §3.10.3 |
| I16 | Config file: built-in defaults deep-merged, `${VAR}` expansion, comment-preserving atomic save that never writes defaults, last-known-good fallback, version stamps and migrations, rolling backups | P0 → P1 | core CLI | 04 §3.11 |
| I17 | `indus config show|get|set|unset|path|env-path|edit|check|migrate`; secret-shaped and UPPER_SNAKE keys routed to `.env`; values coerced by the default's type | P1 | CLI | 04 §3.13 |
| I18 | `.env` secrets: home file beats shell exports; locked atomic writer with a denylist of dangerous names | P0 → P1 | core CLI | 04 §3.14.1–3.14.3 |
| I19 | External secret sources (Bitwarden Secrets Manager, 1Password, command helper) pulled at start-up | P3 | CLI | 04 §3.14.4, docs |
| I20 | Administrator managed scope (`/etc/indus`) whose values users cannot override | P3 | core CLI | 04 §3.14.6 |
| I21 | Package-manager-owned installs refuse config mutation and point updates at the package manager | P2 | CLI | 04 §3.14.6 |
| I22 | Auth store (`auth.json`) with cross-process lock and safe handling of unreadable or corrupt files | P1 | core | 04 §3.16.2 |
| I23 | `indus auth add|list|remove` → `reset|priority|refresh|status|logout` and an interactive menu | P1 → P2 | CLI | 04 §3.16.3 |
| I24 | Generic device-code and loopback PKCE sign-in helpers with remote-session detection and an SSH port-forward hint | P2 | CLI D | 04 §3.16.4–3.16.6 |
| I25 | `indus model` picker: provider → key → live or curated model list → reasoning effort; custom endpoints and local servers | P1 | CLI | 04 §3.17.1 |
| I26 | `/model` switches among configured providers for the session or globally | P1 | CLI TUI D GW ACP | 04 §3.17.2 |
| I27 | `indus fallback list|add|remove|clear` | P1 | CLI | 04 §3.17.4 |
| I28 | `indus moa` presets | P3 | CLI | 04 §3.17.5 |
| I29 | Setup wizard: sections (model, terminal, gateway, tools, agent), non-interactive guidance, full and blank-slate modes, back navigation. The portal "quick" mode is dropped (P3 decision) | P1 | CLI | 04 §3.18 |
| I30 | `indus tools list|enable|disable` per platform, interactive configuration and dependency installers | P1 | CLI | 04 §3.19 |
| I31 | `indus sessions` list / browse / rename / delete / export / prune / archive / pin / stats with shared filters | P1 | CLI | 04 §3.20 |
| I32 | `indus checkpoints status|list|prune|clear` | P1 | CLI | 04 §3.22 |
| I33 | `indus backup` (whole home as a zip, databases copied safely while running) and `indus import` with CRC pre-check → quick snapshots | P1 → P2 | CLI W | 04 §3.23 |
| I34 | `indus doctor` with `--fix`, `--live` probes and advisory acknowledgements | P1 | CLI W | 04 §3.24.1 |
| I35 | `indus dump` support summary with keys shown only as set/unset | P2 | CLI W | 04 §3.24.2 |
| I36 | `indus debug share` redacted report upload, or local print | P3 | CLI | 04 §3.24.3 |
| I37 | `indus status` (one screen) with `--full` and `--deep` | P1 | CLI | 04 §3.24.4 |
| I38 | `indus logs` viewer with file, level, session, time and component filters and follow mode | P1 | CLI | 04 §3.24.5 |
| I39 | `indus usage` account limit windows for providers that publish them | P3 | CLI | 04 §3.24.6 |
| I40 | `indus prompt-size` byte breakdown of a fresh prompt | P2 | CLI W | 04 §3.24.7 |
| I41 | `indus update`: git channel (stable tags / main), syntax check and rollback, dependency sync, config migration, gateway restart → channels, plans, receipts, fleet restarts | P1 → P2 | CLI | 04 §3.25 |
| I42 | `indus uninstall` (keep data / full / desktop only / data only, dry run) | P1 | CLI | 04 §3.26 |
| I43 | Shell completion scripts (bash, zsh, fish) | P2 | CLI | 04 §3.28 |
| I44 | `indus console`: a small safe command REPL | P3 | CLI W | 04 §3.28 |
| I45 | Version output naming install method and update availability | P0 | CLI | 04 §3.28 |
| I46 | `import-agent` (instructions, allowlists, MCP servers, skills from Claude Code or Codex) and OpenClaw migration | P3 | CLI | 04 §3.28 |
| I47 | Portal account commands (portal sign-in, `auth upgrade`, `/topup`, `/subscription`, `setup --portal`, free tier). Dropped (P3 decision) | — | CLI TUI D GW | 04 §3.3, §3.16.3 |
| I48 | Opt-in telemetry consent and shared metrics | P3 | CLI W D | 04 §3.4.4 |
| I49 | Local subscription proxy exposing a managed provider route as an OpenAI-compatible endpoint. Changed (P2): only API-key and documented routes | P3 | CLI | docs |
| I50 | Monitoring export (OTLP and tracing plugins) | P3 | core CLI | 04 §3.2.4, docs |
| I51 | In-session info commands: `/status`, `/context`, `/whoami`, `/usage`, `/insights`, `/help`, `/version` | P1 | CLI TUI D GW | 04 §3.7.3 |
| I52 | `/reload` re-reads `.env` into the running session | P2 | CLI | 04 §3.7.3 |
| I53 | Log files (agent, errors, gateway, gui) behind a non-blocking queue with rotation, session tags and redaction | P0 → P1 | core | 03 §3.27 |
| I54 | Process bootstrap: UTF-8 stdio, import hardening, dual-stack connects, scratch temp dir | P1 | core | 03 §3.29, 04 §3.1 |
| I55 | Classic chat runs the agent in-process first. Changed (P1 decision); the reference's gateway-client chat (`gateway ensure`, frozen session flags, reduced slash set) arrives with the shared daemon (K51) | P0 → P2 | CLI | 04 §3.4.6, 06 §3.1 |

## J. Terminal UI

| # | Feature | Tier | Surfaces | Spec |
|---|---|---|---|---|
| J1 | `indus --tui` launcher: TTY gate, cwd pinning, resume and one-shot options, heap flag, exit summary with resume hint, exit code 42 relaunches as update | P1 | TUI | 05 §3.1 |
| J2 | Transport: renderer spawns a Python child over JSON-RPC on stdin/stdout first. Changed (P1 decision); WebSocket attach and the shared daemon follow | P1 → P2 | TUI | 05 §3.2 |
| J3 | Declared wire contract with generated TypeScript types; error codes with one fixed meaning each. Added by Indus (the reference reuses codes) | P1 | TUI D W | 05 §3.3, §6 R05.15 |
| J4 | Layout: transcript with banner and session panel, live streaming row, tool trail, status rule with width breakpoints, composer and completion list | P1 | TUI | 05 §3.7 |
| J5 | Multiline composer: word motions, kill keys, undo/redo, mouse selection, external editor, paste collapse with full text sent | P1 | TUI | 05 §3.8 |
| J6 | Slash, skill and path completion with fuzzy ranking, completed through the session's backend | P1 | TUI | 05 §3.9 |
| J7 | Input history file shared with the classic CLI format | P1 | TUI | 05 §3.11 |
| J8 | Busy-input modes (queue by default in the TUI, steer, interrupt) and queue editing | P1 | TUI | 05 §3.12 |
| J9 | Streaming markdown with sealed segments, inline diffs, todo panel and detail modes per section | P1 | TUI | 05 §3.13–3.14 |
| J10 | Interrupt rules: Ctrl+C clears a draft before interrupting before exiting; Esc Esc; double Enter; sealed partial reconciled with what was stored | P1 | TUI | 05 §3.16 |
| J11 | Blocking cards for approval, clarify, sudo, secret and confirm; a pager for long output | P1 | TUI | 05 §3.17 |
| J12 | Model picker (provider → model → effort) with session or global scope | P1 | TUI | 05 §3.18 |
| J13 | Session switcher with live and stored sessions, new, close and delete | P1 | TUI | 05 §3.19 |
| J14 | Themes from the backend with light/dark detection and a cached boot theme | P1 | TUI | 05 §3.21 |
| J15 | Config sync: display settings applied on start and re-read when the config changes | P1 | TUI | 05 §3.22 |
| J16 | Terminal hygiene: modes reset on every exit path, resize coalescing, synchronized output only where supported, exit when the terminal is gone | P1 | TUI | 05 §3.23 |
| J17 | Reconnect and crash recovery that reopens the same stored session and never resubmits a prompt | P1 | TUI | 05 §3.24 |
| J18 | Live agents dock and full monitor with steer and stop per child | P2 | TUI | 05 §3.15 |
| J19 | Goal and loop bars | P2 | TUI | 05 §3.7 |
| J20 | Inline/native scrollback mode and a Termux mode | P2 | TUI | 05 §3.7, §3.23 |
| J21 | Mouse tracking presets (`/mouse`) and busy-indicator styles | P2 | TUI | 05 §3.7, §3.23 |
| J22 | Voice push-to-talk and wake word in the TUI | P3 | TUI | 05 §3.4 |
| J23 | Cosmetic extras: pets, fortunes, long-run charms, widget mini-apps, debug panes | P3 | TUI | 05 §3.7, §3.10 |
| J24 | Offline input journal used while disconnected from a shared daemon | P3 | TUI | 05 §3.12 |
| J25 | `/terminal-setup` installs IDE terminal key bindings | P3 | TUI | 05 §3.23 |

## K. Messaging gateway and platforms

| # | Feature | Tier | Surfaces | Spec |
|---|---|---|---|---|
| K1 | `indus gateway run|start|stop|restart|status`, PID plus start-time liveness, runtime status file, exit codes 0 / 75 / 78, drain on SIGTERM, graceful restart on SIGUSR1 | P1 | GW CLI | 06 §3.1, §3.22 |
| K2 | Service install: systemd user unit → launchd agent, Windows scheduled task, system units | P1 → P2 | GW CLI | 06 §3.1 |
| K3 | One profile per gateway process. Changed (P8 decision); multiplexed profiles in one process come later (K50) | P1 | GW | 06 §3.26 |
| K4 | Adapter contract and platform registry: enabled by credentials, explicit disable wins, token locks so two profiles never poll one bot | P1 | GW | 06 §3.2 |
| K5 | Telegram (polling or webhook, MarkdownV2 with plain fallback, forum topics, buttons, reactions) | P1 | GW | 06 §3.3 |
| K6 | Discord (slash commands, components, auto-threads, role allowlist, mention gating) | P1 | GW | 06 §3.3 |
| K7 | Slack (Socket Mode, Block Kit, threads, ephemeral messages) | P1 | GW | 06 §3.3 |
| K8 | Signal through signal-cli | P1 | GW | 06 §3.3 |
| K9 | WhatsApp through a local bridge → WhatsApp Business Cloud API | P1 → P2 | GW | 06 §3.3 |
| K10 | Matrix, Mattermost, Email (IMAP/SMTP), SMS (Twilio) | P2 | GW | 06 §3.3 |
| K11 | DingTalk, Feishu/Lark, WeCom and WeCom callback, Weixin, QQ Bot, Yuanbao | P3 | GW | 06 §3.3 |
| K12 | BlueBubbles, Photon, Microsoft Teams (and meeting pipeline), Graph webhooks, LINE, Google Chat, ntfy, IRC, SimpleX, Buzz, A2A, Raft, Home Assistant | P3 | GW | 06 §3.3, docs |
| K13 | Inbound normalisation: command detection, burst text batching, per-platform dedupe, reply context including the bot's own rich messages | P1 | GW | 06 §3.4 |
| K14 | Authorization chain: per-platform and global allowlists, allow-all flags, group chat lists, role grants, default deny | P1 | GW | 06 §3.5 |
| K15 | Pairing codes for unknown DM users with expiry and lockouts; `indus pairing approve|list|revoke` | P1 | GW CLI | 06 §3.5 |
| K16 | Unknown-sender behaviour pair / ignore / decline | P1 | GW | 06 §3.5 |
| K17 | Per-scope slash-command admin tiers | P2 | GW | 06 §3.5 |
| K18 | Mention gating in groups and free-response channels | P1 | GW | 06 §3.5 |
| K19 | Bot-loop guard per conversation and optional concurrent-session cap | P1 | GW | 06 §3.6 |
| K20 | Session keys for DM, group (per user by default) and thread; conversations never reset on idle | P1 | GW | 06 §3.7 |
| K21 | Chat slash commands (`/new`, `/resume`, `/sessions`, `/title`, `/model`, `/status`, `/stop`, `/sethome`, `/help`, `/whoami`, `/approve`, `/deny`, `/queue`, `/bg`, `/platform`, …) | P1 | GW | 06 §3.11 |
| K22 | Native command menus on Telegram, Discord and Slack | P2 | GW | 06 §3.11 |
| K23 | Busy handling with two guards; plain messages interrupt, queue or steer; acknowledgement replies | P1 | GW | 06 §3.10 |
| K24 | Intake of voice notes (transcribed, with an explicit note when inaudible), images (native or described), documents, albums → stickers | P1 → P2 | GW | 06 §3.9 |
| K25 | Outbound chunking with fence carry-over and `(i/n)` markers; per-platform formatting with plain-text fallback | P1 | GW | 06 §3.13 |
| K26 | Files in replies via media tags with a path policy → generated files delivered as native attachments | P1 → P2 | GW | 06 §3.13, docs |
| K27 | Silence tokens: a reply that is only the token sends nothing | P1 | GW | 06 §3.13 |
| K28 | Typing indicator → in-place streamed edits, native drafts, tool-progress modes and status phrases | P1 → P2 | GW | 06 §3.14 |
| K29 | Send retries with an error classifier and a dead-target registry | P1 | GW | 06 §3.15 |
| K30 | Delivery ledger: final replies delivered at least once across crashes, with possible duplicates labelled | P1 | GW | 06 §3.16 |
| K31 | Restart notice to home channels and one auto-resume of turns cut by a restart | P1 | GW | 06 §3.22 |
| K32 | Local control socket (identify, status, then more verbs) for other local processes | P1 | GW | 06 §3.21 |
| K33 | Reconnect watcher with capped backoff, "needs attention" flag, `/platform pause|resume` | P1 | GW | 06 §3.23 |
| K34 | Watchdogs (startup, event-loop liveness, shutdown) and restart-loop guard | P2 | GW | 06 §3.1, §3.22 |
| K35 | OpenAI-compatible API server: chat completions, responses, models, runs with approval/clarify/steer/stop, session and job routes | P1 | API | 06 §3.25 |
| K36 | Webhook receiver: HMAC-verified routes, payload filters and templates, coalescing, direct delivery; `indus webhook subscribe` | P1 | API GW | 06 §3.25 |
| K37 | Relay connector (gateway dials out to an external front end) | P3 | GW | 06 §3.25 |
| K38 | Group rooms where several bots deliberate, and peer gateways across machines (`indus peer`) | P3 | GW D | 06 §3.20 |
| K39 | Browser-control broker for an external browser controller | P3 | API | 06 §3.24 |
| K40 | Delivery mirror records so a session knows what was sent into it from elsewhere | P2 | GW | 06 §3.8 |
| K41 | `indus send` a text or media message to a platform target from the command line | P2 | CLI GW | 02 §3.5, 04 §3.3 |
| K42 | Channel directory with friendly target names | P2 | GW | 06 §3.17 |
| K43 | Per-channel prompts, skill bindings and model overrides; `/model --once` | P2 | GW | 06 §3.4, §3.7 |
| K44 | Auto-deleting system notices and warning suppression | P3 | GW | 06 §3.13–3.14 |
| K45 | Discord voice channels and streamed spoken replies | P3 | GW | 06 §3.3 |
| K46 | Telegram DM topic mode | P3 | GW | 03 §3.20, 06 §3.7 |
| K47 | Idle exit for auto-started gateways → hosted scale-to-zero | P2 → P3 | GW | 06 §3.22 |
| K48 | External drain request from the dashboard | P2 | GW W | 06 §3.22 |
| K49 | Opt-in send-time stamps in the model context | P3 | GW | 06 §3.9 |
| K50 | Multiplexed profiles in one gateway with profile routes and shared ingress. Later phase (P8 decision) | P3 | GW | 06 §3.26 |
| K51 | Shared per-profile gateway daemon serving CLI, TUI, desktop, dashboard and ACP with durable queues, tickets and generation fences. Later phase, then the parity path (P1 decision) | P2 | GW CLI TUI D W ACP | 05 §3.2, 06 §3.1, 08 §3.4 |
| K52 | `indus gateway setup` and per-platform setup helpers (WhatsApp pairing, Slack app) | P1 → P2 | CLI | 04 §3.3, 06 §2 |

## L. Web dashboard

| # | Feature | Tier | Surfaces | Spec |
|---|---|---|---|---|
| L1 | `indus dashboard`: SPA plus API on loopback, per-process session token in the page, Host-header guard, JSON 404 for unknown API paths, build on first launch | P1 | W | 09 §3.1, §3.3 |
| L2 | `indus serve` headless backend used by the desktop app | P2 | W D | 09 §3.1 |
| L3 | Sessions page: list, search with snippets, view as markdown, rename, export, prune, delete, resume into chat | P1 | W | 09 §3.2 |
| L4 | Chat tab embedding the real TUI over a PTY and xterm → keep-alive attach across tabs, live tool feed and a right rail (model, sessions, workspace, effort) | P1 → P2 | W | 09 §3.5 |
| L5 | Config page from a schema; Keys page with redacted previews | P1 | W | 09 §3.2 |
| L6 | Logs page with file, level and component filters | P1 | W | 09 §3.2 |
| L7 | Models, Cron, Skills, Plugins and MCP pages | P2 | W | 09 §3.2 |
| L8 | Channels, Webhooks and Pairing pages | P2 | W | 09 §3.2 |
| L9 | Profiles pages, stepped profile builder and persona editor | P2 | W | 09 §3.2 |
| L10 | System page: host stats, update, gateway control, memory provider, credential pool, background operations, checkpoints, shell hooks | P2 | W | 09 §3.2 |
| L11 | Files page and Analytics page | P3 | W | 09 §3.2 |
| L12 | Status strip and resource-pressure banners | P2 | W | 09 §3.2 |
| L13 | Gated mode for non-loopback binds: username/password, self-hosted OIDC, service bearer tokens, cookies, public URL and trusted proxies, auth audit log | P2 | W | 09 §3.3 |
| L14 | Vendor portal OAuth login provider. Dropped (P3 decision) | — | W | 09 §3.3 |
| L15 | Admin console modal over its own socket | P3 | W | 09 §3.6 |
| L16 | One light/dark theme pair → built-in and user YAML themes, 17 locales | P2 → P3 | W | 09 §3.7 |
| L17 | Dashboard plugins: tabs, slots, overrides and Python API routes | P3 | W | 09 §3.8 |
| L18 | Reverse-proxy path prefix support | P2 | W | 09 §3.1 |
| L19 | Profile switcher scoping every management page | P2 | W | 09 §3.2 |
| L20 | Dashboard fonts and imagery are our own; the reference's brand fonts are not shipped (lead default) | P1 | W | 09 §3.2 |

## M. Editor adapter (ACP) and MCP server

| # | Feature | Tier | Surfaces | Spec |
|---|---|---|---|---|
| M1 | `indus acp` stdio server with `initialize`, `authenticate` (including a terminal setup method), `--version`, `--check`, `--setup`, `--setup-browser` | P1 | ACP | 09 §3.9 |
| M2 | ACP sessions: new, load, resume (full history streamed before the reply), list with cwd filter and paging, fork, cancel | P1 | ACP | 09 §3.10 |
| M3 | ACP prompts with text, image and resource blocks; streamed message, thought, tool, plan, usage and title updates; diffs for edits | P1 | ACP | 09 §3.11 |
| M4 | ACP permission cards: allow once, for session, always, deny, deny always | P1 | ACP | 09 §3.11 |
| M5 | Clarify questions through form elicitation, else permission options | P2 | ACP | 09 §3.11 |
| M6 | Slash commands advertised to the editor and handled locally or forwarded | P2 | ACP | 09 §3.11 |
| M7 | Model switch from the editor with confirmation for guarded models; single "ask before edits" mode | P2 | ACP | 09 §3.10 |
| M8 | ACP runs the agent in-process first. Changed (P1 decision); gateway-viewer mode with reattach and lost-turn handling arrives with K51 | P1 → P2 | ACP | 09 §3.9–3.10 |
| M9 | Editor-provided MCP servers attached per session; curated editor toolset | P1 → P2 | ACP | 09 §3.11 |
| M10 | `indus mcp serve`: list conversations, read messages, poll and wait for events, fetch attachments, send messages, list channels | P2 | MCP | 09 §3.12 |
| M11 | MCP-serve approval tools actually wired to the gateway. Changed by Indus (in the reference they only touch local state) | P3 | MCP | 09 §3.12, R09.10 |

## N. Desktop app

| # | Feature | Tier | Surfaces | Spec |
|---|---|---|---|---|
| N1 | Desktop shell: single-instance lock, frameless main window, saved bounds, revealed on first themed paint | P2 | D | 08 §3.2, §3.12 |
| N2 | Security baseline: sandboxed isolated renderers, deny-all window opening, navigation confinement, one audited external opener | P2 | D | 08 §3.28 |
| N3 | Hardening beyond the reference: every IPC handler checks its sender frame, strict renderer CSP, Electron fuses set, untrusted content can never open URLs. Added by Indus (lead default) | P2 | D | 08 §3.28, §7 Q1–Q2, Q9 |
| N4 | Backend resolution ladder and local gateway attach with one-use socket tickets and per-request HTTP grants | P2 | D | 08 §3.3–3.4 |
| N5 | First-run choice (connect to existing / install locally) and in-app installer driven by the installer's stage protocol | P2 | D | 08 §3.5 |
| N6 | Boot progress overlay; local failures latch until retry/repair/reset; bounded remote retry | P2 | D | 08 §3.2, §3.6 |
| N7 | Supervision: backend restart with crash-loop stop, renderer reload limits, sleep/resume revalidation, orphan reaping, quit confirmation | P2 | D | 08 §3.7 |
| N8 | Transport client: JSON-RPC over WebSocket with heartbeats, replay since cursor, server requests, prepared-submission journal | P2 | D W | 08 §3.9 |
| N9 | Chat surface: streaming transcript with reasoning and tool rows, timeline rail, composer with attachments and mentions, status stack, stop/steer/queue, approval card stack, model picker, failed-turn cards, image draft restore | P2 | D | 08 §3.13 |
| N10 | Sidebar: sessions with search, pin, archive, unread; projects owning folders and repos; profile rail | P2 | D | 08 §3.14 |
| N11 | Worktree lanes in the sidebar | P3 | D | 08 §3.14 |
| N12 | Right panes: preview browser with annotate mode, file tree, git review (stage, commit, push, PR), terminal | P3 | D | 08 §3.15 |
| N13 | Settings overlay with searchable sections (model, chat, appearance, workspace, safety, browser, memory, voice, advanced, providers, gateways, keys, keyboard, notifications, sessions, about) | P2 | D | 08 §3.16 |
| N14 | Provider onboarding overlay (device-code OAuth, API key, local model runtime, custom endpoint, choose later). Free-tier flow dropped (P3 decision) | P2 | D | 08 §3.17 |
| N15 | Capabilities page (skills, toolsets, connectors, plugins with desktop and agent halves) | P3 | D | 08 §3.18 |
| N16 | Management pages: messaging (with Telegram QR setup), webhooks, scheduled jobs, profiles, agents, command center, learning map, artifacts gallery, session import | P3 | D | 08 §3.19 |
| N17 | Connections: several registered gateways, remote URL with token or native OAuth sign-in, access-proxy headers, SSH remotes with managed updates | P3 | D | 08 §3.10 |
| N18 | Vendor cloud connections with agent discovery. Dropped (P3 decision) | — | D | 08 §3.10 |
| N19 | Profile routing and the three switch shapes; a vanished session drops to a fresh draft that keeps unsent text | P2 | D | 08 §3.11 |
| N20 | Extra window kinds: popped-out session, peer instance, browser pop-out, floating HUD, quick-entry composer, pet overlay, wake indicator | P3 | D | 08 §3.12, §3.21 |
| N21 | Simple and Advanced interface modes, pane layouts and presets, tab shortcuts, UI zoom | P2 → P3 | D | 08 §3.12 |
| N22 | Bots: profiles with avatars, one main chat each, routines, group rooms | P3 | D | 08 §3.20 |
| N23 | Per-bot virtual screen streamed to the app with take-over and hand-back | P3 | D GW | docs |
| N24 | Ambient features: tray, keep awake, notifications with actions, completion sounds, link titles | P3 | D | 08 §3.21 |
| N25 | Desktop plugin SDK and contribution registry with hot-reloaded disk plugins | P3 | D | 08 §3.22 |
| N26 | Deep links (`indus://open`, skill, MCP, blueprint, close-preview) and a local media streaming scheme | P3 | D | 08 §3.23 |
| N27 | Design system of tokens and primitives, all assets and copy authored by Indus | P2 | D | 08 §3.24 |
| N28 | Nine locales, twelve theme presets, VS Code theme import | P3 | D | 08 §3.25 |
| N29 | Updates per install kind (macOS updater, MSIX App Installer, Store, source handoff), fleet update, shared update marker | P3 | D | 08 §3.26 |
| N30 | Packaging: signed and notarised macOS DMG/ZIP, MSIX, AppImage; bundled and remote-only variants | P3 | D ops | 08 §3.27 |
| N31 | Secrets at rest (desktop and CLI: provider keys, auth tokens, platform credentials) go to the OS keychain by default, falling back to owner-only 0600 files. Changed (P4 decision; the reference defaults to plain files) | P1 | CLI, D | 08 §3.28, §7 Q8 |
| N32 | Uninstall from Settings at three levels | P3 | D | 08 §3.29 |
| N33 | Separate native setup installer app | P3 | D ops | 08 §3.31 |
| N34 | Voice: dictation, read-aloud, wake word, spoken conversation | P3 | D | 08 §3.13 |
| N35 | Desktop-only agent tools (read terminal pane, drive and annotate preview, focus panes, tours, theme picker) | P3 | D | 02 §3.28 |
| N36 | Screenshot gesture that attaches the frontmost window (macOS) | P3 | D | 08 §3.21 |

## O. Research tooling

| # | Feature | Tier | Surfaces | Spec |
|---|---|---|---|---|
| O1 | Batch runner over a JSONL of prompts: process pool, checkpoint, durable appends, resume without paying twice, per-prompt container images | P2 | ops | 10 §3.1 |
| O2 | Toolset distributions: named tables of per-toolset inclusion odds, grouped entries | P2 | ops | 10 §3.2, 01 §3.16 |
| O3 | Trajectory format (ShareGPT-style turns with tool-call tags, every model turn starts with a think block) | P2 | ops | 10 §3.3 |
| O4 | Save interactive sessions as trajectories (`--save-trajectories`) | P2 | core CLI | 03 §3.30, 10 §3.3 |
| O5 | Single-tool SWE runner on local, Docker or Modal | P3 | ops | 10 §3.4 |
| O6 | Trajectory compressor to a token budget with metrics. Changed (lead default): the tokenizer never runs remote code by default; default models chosen by Indus | P2 | ops | 10 §3.5 |
| O7 | Dataset sample-and-compress helper with a required, config-driven dataset list | P3 | ops | 10 §3.6 |
| O8 | Eval harness conventions: one-variable A/B arms, oracle-scored tasks, resume-safe results, never on live data | P3 | ops | 10 §3.7 |
| O9 | Legacy single-query runner script with minimal flags | P3 | CLI | 04 §3.1 |

## P. Install, distribution, update and CI

| # | Feature | Tier | Surfaces | Spec |
|---|---|---|---|---|
| P1 | POSIX installer: stage ladder with manifest and JSON frames, pinned `uv` bootstrap verified by sha256 with mirror fallback, clone in staging then rename, config seeding, PATH wiring, install receipt | P1 | ops | 10 §3.10, 04 §3.27 |
| P2 | PowerShell installer and cmd shim with the same ladder | P2 | ops | 10 §3.11 |
| P3 | Docker image with dispatcher, UID/GID remap and a data volume; Compose file with gateway and dashboard | P1 | ops | 10 §3.13 |
| P4 | Nix flake packages with NixOS and home-manager modules | P2 | ops | 10 §3.14 |
| P5 | Termux APT channel (P5 decision: later) | P3 | ops | 10 §3.12 |
| P6 | Desktop release artefacts (MSIX bundle, Store bundle, macOS DMG) (P5 decision: later) | P3 | ops | 10 §3.15 |
| P7 | No PyPI: wheel/sdist builds blocked outside Nix; editable installs work. Kept (P5 decision) | P1 | ops | 10 §3.8 |
| P8 | Dependency policy: exact or bounded pins, lock file, extras for optional backends installed lazily | P1 | ops | 10 §3.8 |
| P9 | `indus pm` package/environment manager with locked helper binaries (lock, install, env, doctor, repair, gc, bundle, status, update) | P2 | CLI ops | 10 §3.9 |
| P10 | Release pipeline: attempt refs, gated lanes, receipt tags, ordered publication, stable/canary/one-off channels | P2 | ops | 10 §3.17 |
| P11 | CI with fail-open change classifier, one aggregate gate, lint diff, lock freshness, supply-chain and blob-size checks | P1 | ops | 10 §3.16 |
| P12 | Code-health ratchet with caps that only go down | P2 | ops | 10 §3.16 |
| P13 | Canonical test runner: one process per file, retry-once flaky report, deterministic slices, hermetic fixtures, one OS marker per test | P1 | ops | 10 §3.20 |
| P14 | Golden prompt-surface fixtures (rendered prompt plus tool array per surface) that fail CI when stale | P1 | ops | 01 §3.4 |
| P15 | Docs website with search and `llms.txt` | P2 | ops | 10 §3.19 |
| P16 | Contributor attribution mapping, started empty | P3 | ops | 10 §3.21 |
| P17 | Clean-room guards in CI (banned identifiers, code and text similarity, asset hashes, licences). Added by Indus | P0 | ops | README, 17 §d |
| P18 | `THIRD_PARTY_NOTICES.md` crediting the reference and every dependency. Added by Indus (licence obligation) | P1 | ops | README, 10 §8.1 |

## Q. Platform/OS support and localisation

| # | Feature | Tier | Surfaces | Spec |
|---|---|---|---|---|
| Q1 | Host facts without env input or subprocesses: OS, native and process architecture, CPU, RAM, GPU class, WSL/container/Termux, interactive session | P1 | core | 09 §3.13 |
| Q2 | Command lookup through one set of known-directory tables | P1 | core | 09 §3.14 |
| Q3 | App declarations and availability checks that gate catalogue MCP servers and skills; liveness probe for app-hosted MCP servers | P3 | core | 09 §3.14 |
| Q4 | NVIDIA platform classification | P3 | core | 09 §3.13 |
| Q5 | Linux and macOS as first-class hosts | P0 | all | 09 §3.15 |
| Q6 | Native Windows (Git Bash shell, UTF-8 console, `%LOCALAPPDATA%` home, launcher copies, byte-range locks, ConPTY chat tab) | P2 | all | 09 §3.15, 04 §3.25 |
| Q7 | WSL support with path translation | P2 | core ACP | 09 §3.15 |
| Q8 | Android via Termux | P3 | core TUI | 10 §3.12 |
| Q9 | Localisation: catalogue indirection everywhere with English → seventeen language catalogues with key-parity checks; `display.language` | P1 → P3 | all | 10 §3.18, 05 §3.26 |
| Q10 | Language packs from plugins or a user overlay | P3 | all | docs, 10 §3.18 |
| Q11 | macOS specifics: privacy-protected folders skipped on broad searches, path normalisation, Rosetta detection | P2 | core | 09 §3.15, 02 §3.5 |
| Q12 | Windows directory ACL fix for elevated runs | P3 | core | 03 §3.29 |
| Q13 | Network preferences: force IPv4, OS trust store for TLS | P2 | core | 04 §3.1 |

---

## Counting it up

Counted by the first tier in each row (`P0 → P1` counts as P0). Dropped rows have tier `—`.

| Tier | Count |
|---|---|
| P0 | 51 |
| P1 | 199 |
| P2 | 137 |
| P3 | 100 |
| — (dropped) | 8 |
| **Total** | **495** |

| Area | Rows | P0 | P1 | P2 | P3 | — |
|---|---|---|---|---|---|---|
| A agent core and providers | 68 | 17 | 30 | 14 | 5 | 2 |
| B conversation and sessions | 35 | 4 | 16 | 12 | 3 | 0 |
| C tools | 42 | 8 | 15 | 9 | 10 | 0 |
| D terminal backends | 17 | 3 | 5 | 5 | 3 | 1 |
| E approvals and safety | 19 | 6 | 6 | 6 | 1 | 0 |
| F learning loop | 33 | 0 | 16 | 13 | 4 | 0 |
| G extensibility | 21 | 0 | 5 | 8 | 7 | 1 |
| H automation | 21 | 0 | 11 | 2 | 7 | 1 |
| I CLI, config, auth | 55 | 11 | 24 | 8 | 11 | 1 |
| J terminal UI | 25 | 0 | 17 | 4 | 4 | 0 |
| K messaging gateway | 52 | 0 | 31 | 11 | 10 | 0 |
| L web dashboard | 20 | 0 | 6 | 10 | 3 | 1 |
| M editor adapter and MCP server | 11 | 0 | 6 | 4 | 1 | 0 |
| N desktop app | 36 | 0 | 0 | 16 | 19 | 1 |
| O research tooling | 9 | 0 | 0 | 5 | 4 | 0 |
| P install, distribution, CI | 18 | 1 | 8 | 6 | 3 | 0 |
| Q platform and localisation | 13 | 1 | 3 | 4 | 5 | 0 |

The total is above the planning guide's rough estimate because the reference is unusually wide
(about forty providers, thirty messaging platforms, four front ends); vendor and platform families
were already grouped into single rows where they share one behaviour contract.

The roadmap (`15-roadmap.md`) turns these tiers into milestones; the acceptance map maps every
P0/P1 row to tests. Rows marked "Added by Indus" or "Changed" also get an entry in the
what-we-change document.
