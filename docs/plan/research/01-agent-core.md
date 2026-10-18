# 01 — Agent core loop and model providers: behavioural specification (clean-room)

Scope: the conversation turn loop; system-prompt assembly (persona file, project context files,
guidance blocks, cache tiers); the tool-call dispatch contract and toolsets; the provider/model
abstraction (provider profiles, API modes, transports, credential pools, fallback chain);
streaming; retries and failover; context-window management and compression as seen from the loop;
interrupt / steer / redirect; token and cost accounting.
Out of scope: individual tool behaviour (`02-tools.md`), the session database and its lineage rows
(`03-session-db.md`), CLI subcommands and slash commands (`04-cli.md`), TUI (`05-tui.md`),
messaging gateway and its 85% hygiene pass (`06-gateway.md`), skills / memory providers / plugins /
cron / curator (`07-skills-memory-plugins-cron.md`).
Reference: `<ref-owner>/<ref-repo>` @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056` (MIT); shipped build
checked: none (REA not connected in this session — source reading only).
Sources studied: developer-guide pages `agent-loop.md`, `prompt-assembly.md`, `provider-runtime.md`,
`context-compression-and-caching.md`, `architecture.md`; user-guide pages `personality.md`,
`context-files.md`, `credential-pools.md`, `fallback-providers.md`; root and `agent/` `AGENTS.md`;
code under `run_agent.py`, `agent/` (≈382 Python files, ≈144k lines), `providers/`,
`model_tools.py`, `toolsets.py`, `toolset_distributions.py`, `utils.py`, the constants module.

Conventions used below:
- **Agent** = one long-lived in-process object that owns a model route, a toolset, callbacks, a
  cached system prompt and counters; it may serve many turns (gateway caches agents).
- **Turn** = one user message processed to a final answer (or a terminal failure); it contains
  one or more **iterations**.
- **Iteration** = one provider request (with its own inner retry loop) plus, if the model asked
  for tools, one **tool round**.
- **Route** = (provider id, model id, base URL, API mode, credential).
- **Wire copy** = the per-request deep copy of the transcript that is converted and sent; the
  **durable transcript** = the message list that is persisted.
- "MUST" marks an invariant the reference defends with code, comments citing past bugs, or tests.
- Rebuild names: product home `~/.indus`, env prefix `INDUS_`, CLI `indus`. Section 2 maps every
  reference name.

---

## 1. Overview

The agent core is a synchronous, thread-assisted loop that turns one user message into a final
assistant answer by repeatedly calling a chat model and executing the tool calls it returns. Every
front end (CLI, TUI, desktop, messaging gateway, cron, editor integration, API server, batch
runner) drives the same object through one entry point that takes a user message, optional caller
system text, optional prior history and an optional task id, and returns a result record
[obs `run_agent.py`, `agent/conversation_loop.py`]. A thin convenience wrapper returns only the
final text [obs].

The reference has been refactored from one giant class into a **facade plus phase siblings**: the
agent class is assembled from ~14 mixins, its constructor delegates to an ordered init routine, and
the turn body is a loop that calls ~20 phase functions (`turn_*`), each returning a small verdict
(`fallthrough | continue | break | return`) whose fields are copied back into a shared loop-state
record [obs `conversation_loop.py` `_LoopState`, `_run_phase`]. The rebuild does not need that
reflection trick; a plain state object passed to phase functions gives the same shape [inf].

```
caller ──run_turn(user_msg)──► [turn admission: cross-process session lease, liveness watchdog]
                                   │
                                   ▼
                     ┌──── turn prologue ────────────────────────────────────────┐
                     │ restore-or-build system prompt (cached, byte-stable)      │
                     │ sanitize + stage user row, persist it, idle/preflight     │
                     │ compaction, pre-LLM plugin context, memory prefetch       │
                     └───────────────────────────────────────────────────────────┘
                                   │
          ┌────────────── while budget remains (or one grace call) ─────────────┐
          │ begin_iteration: redirect? interrupt? consume budget                │
          │ prepare_iteration: steer drain, alternation repair, arg sanitizing  │
          │ assemble request: wire copy, ephemeral layers, cache markers        │
          │ preflight gate: compress if over threshold                          │
          │   ┌─ inner retry loop (max_retries) ───────────────────────────────┐│
          │   │ build kwargs → call (stream/non-stream, interruptible worker)  ││
          │   │ check response │ on error: classify → recover/rotate/fallback  ││
          │   └────────────────────────────────────────────────────────────────┘│
          │ normalize response → tool_calls? ── yes ─► tool round → next iter   │
          │                        no ─► final-text guards (empty, length,      │
          │                              intent-ack, stop gates) → break        │
          └──────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
                     finalize: budget summary, persist, hooks, steer leftovers,
                     micro-compaction, result record
```

Guarantees the system makes to users [obs unless marked]:
- The system prompt is byte-identical for the life of a conversation; the only sanctioned change is
  a rebuild at a context-compaction boundary (provider prefix caches stay warm).
- The transcript always satisfies strict role alternation; every assistant tool call gets exactly
  one tool result, even on interrupt, timeout, invalid arguments or crash.
- The user's message is persisted before any provider call; each tool-call assistant row is
  persisted before any tool side effect.
- An interrupted model call is abandoned without injecting partial output as a normal reply.
- Transient provider trouble is retried with jittered backoff, then credential rotation, then a
  configured fallback route; the primary route is restored at the start of the next turn.
- A turn never spins forever: iteration budget, per-turn outer-error cap, compression attempt cap,
  stale-stream breaker, liveness watchdog and lease TTL each bound it.

## 2. Reference names (rename in rebuild)

| Kind | Names (reference only; they become banned identifiers) | Rebuild name |
|---|---|---|
| Binary / CLI and flags | `hermes`, `hermes chat`, `hermes model`, `hermes fallback`, `hermes auth add/reset/priority`, `hermes config set`, `hermes doctor`, `hermes setup --portal`, `hermes -z --usage-file`, `hermes chat -Q`, `--run-budget`, console script `run_agent.py` (`main(query, model, api_key, base_url, max_turns, enabled_toolsets, …)`) | `indus …` equivalents |
| Env vars | `HERMES_HOME`, `HERMES_API_TIMEOUT`, `HERMES_STREAM_READ_TIMEOUT`, `HERMES_STREAM_STALE_TIMEOUT`, `HERMES_LOCAL_STREAM_STALE_TIMEOUT`, `HERMES_STREAM_RETRIES`, `HERMES_STREAM_STALE_GIVEUP`, `HERMES_API_CALL_STALE_TIMEOUT`, `HERMES_CODEX_HARD_TIMEOUT_SECONDS`, `HERMES_CODEX_TTFB_TIMEOUT_SECONDS`, `HERMES_CODEX_TTFB_STRICT`, `HERMES_CODEX_TTFB_MAX_SECONDS`, `HERMES_CODEX_TTFB_DISABLE_ABOVE_TOKENS`, `HERMES_CODEX_EVENT_STALE_TIMEOUT_SECONDS`, `HERMES_CONCURRENT_TOOL_TIMEOUT_S`, `HERMES_AGENT_TIMEOUT`, `HERMES_EPHEMERAL_SYSTEM_PROMPT`, `HERMES_ENVIRONMENT_HINT`, `HERMES_TURN_AUTHOR`, `HERMES_KANBAN_TASK` (and other `HERMES_KANBAN_*`), `HERMES_SESSION_PLATFORM`, `HERMES_DESKTOP_TERMINAL`, `HERMES_DUMP_REQUESTS`, `HERMES_REDACT_SECRETS`, `HERMES_TURN_COMPLETION_EXPLAINER`, `HERMES_FILE_MUTATION_VERIFIER`, `HERMES_UID`/`HERMES_GID`, `HERMES_CONTAINER`, `HERMES_SKIP_CHMOD`, `HERMES_REAL_HOME`, `HERMES_SCRATCH_DIR`, `HERMES_BUNDLED_SKILLS`, `HERMES_OPTIONAL_SKILLS`, `HERMES_OPTIONAL_MCPS`, `HERMES_MANAGED` | `INDUS_` + same suffix (e.g. `INDUS_API_TIMEOUT`) |
| Files and folders | `~/.hermes/` (profile home), `~/.hermes/profiles/<name>`, `SOUL.md` (persona file), `.hermes.md` / `HERMES.md` (native project context file), `MEMORY.md`, `USER.md`, `auth.json`, `state.db`, `context_length_cache.yaml`, `cache/image_token_costs.json`, `.codex_gpt55_autoraise_notice`, `plugins/model-providers/<name>/` + `plugin.yaml`, `hermes_constants.py`, `hermes_state*.py`, `hermes_cli/`, `hermes_time.py`, `hermes_platform/`, `tests/fixtures/prompt_surface/` | `~/.indus/`, persona file name is a lead decision (suggest `PERSONA.md`), `.indus.md` / `INDUS.md`, rest neutral |
| HTTP routes / URL schemes | `https://inference-api.nousresearch.com/v1` (vendor portal inference URL); UA string `hermes-cli/<version>` | rebuild's own UA `indus/<version>`; no vendor portal |
| RPC / events / commands | Python entry-point group `hermes_agent.plugins`; plugin hook names are generic (`pre_llm_call`, `post_llm_call`, `pre_api_request`, `post_api_request`, `pre_tool_call`, `post_tool_call`, `transform_tool_result`, `transform_llm_output`, `pre_auxiliary_call`, `post_auxiliary_call`, `pre_verify`); event `session:compress`; metrics `hermes.compression.count`, `hermes.compaction`; telemetry route label `hermes` | `indus_agent.plugins`, `indus.*` metrics |
| Settings and storage keys | provider id `nous`, provider label "Nous Portal", toolset names `hermes-cli`, `hermes-acp`, `hermes-api-server`, `hermes-cron`, `hermes-telegram`, `hermes-discord`, … `hermes-gateway`, `hermes-webhook`; core list identifier `_HERMES_CORE_TOOLS`; config value `auxiliary.*.provider: nous`; `nous.anthropic_wire` | `indus-cli` etc. |
| Prompt / model-visible strings | runtime-environment heading text naming the product; default identity naming the product and vendor; tool-argument corruption marker naming the product; "hermes-agent" help skill name; `hermes_tools_mcp_server.py` | rewrite all copy |
| Other | class `AIAgent`, `HermesCLI`, approver team `hermes-agent-core`, vendor security mailbox, `nous_wire.py`, `nous_rate_guard.py`, `portal_tags.py` | neutral names |

Third-party protocol names used by this area (keep exact; verify against vendor docs):
OpenAI Chat Completions (`messages`, `tools`, `tool_calls`, `tool_call_id`, `finish_reason`
values `stop|length|tool_calls|content_filter`, `max_tokens` vs `max_completion_tokens`,
`stream_options`, `reasoning_effort`, `developer` role); OpenAI Responses API (input items,
`function_call`, `reasoning` items with `encrypted_content`, `status: incomplete|failed|cancelled`,
`context_management` compaction items); Anthropic Messages API (`system`, content blocks,
`tool_use`/`tool_result`, `thinking` with `budget_tokens` / `adaptive`, `cache_control` with
`type: ephemeral` and optional `ttl: 1h`, max 4 breakpoints, `x-api-key` vs Bearer); AWS Bedrock
Converse / ConverseStream; Google Gemini `generateContent` with `generationConfig.thinkingConfig`;
Server-Sent Events framing; HTTP `Retry-After`; `x-ratelimit-{limit,remaining,reset}-{requests,tokens}`;
OpenRouter provider-routing object (`sort: throughput|latency|price`, allow/ignore/order lists,
`require_parameters`, `data_collection`); MCP; ACP; models.dev catalog.

## 3. Behaviour

### 3.1 Agent object and entry points

- **Trigger / inputs:** a front end constructs one agent with route settings (base URL, key,
  provider, API mode, model; empty model = resolve from config later), toolset selection
  (enabled list, disabled list), behaviour switches (quiet mode, skip context files, skip memory,
  load persona identity even when skipping context files, save trajectories), session identity
  (session id, parent session id, platform, user/chat/thread ids), budgets (iteration budget object,
  max iterations, wall-clock run budget seconds), a fallback chain, a credential pool, a reasoning
  config, max output tokens, request overrides, prefill messages, an ephemeral system prompt, a
  working directory, and ~20 optional callbacks [obs `run_agent.py` constructor ≈60 params].
- **Callbacks surface** (all optional; failures are logged, never fatal) [obs]:
  tool progress, tool start, tool complete, thinking start/stop, reasoning text, clarify (ask the
  user), step (after each iteration, receives the previous tool round), stream delta (visible
  text), interim assistant message, tool-generation started (spinner while arguments stream),
  status, notice / notice-clear, generic event, reaction, tool-result metadata; plus desktop-only
  ones (read terminal, previews, theme, tour, setup beat) that belong to other areas.
- **Entry points:** `run_turn(user_message, system_message?, history?, task_id?, stream_callback?,
  persist_user_message?, …) → TurnResult` and `chat(text) → str` [obs]. `persist_user_message`
  lets a caller send API-only prefixes while persisting clean text [obs].
- **Init order matters** [obs `agent_init.py`]: route resolution first, then callbacks, the
  client, tools, session, each config section, compression, and the context engine last; later
  phases read fields that earlier ones set.
- **Data shapes:**
  `TurnResult { final_response, last_reasoning, messages, api_calls, completed, turn_exit_reason,
  failed, partial, interrupted, response_transformed, pre_transform_response, response_previewed,
  response_reused, model, requested_model?, served_model?, provider, base_url, session token
  counters…, last_prompt_tokens, session cost fields…, service_tier?, session_id, guardrail?,
  error?, failure_reason?, failure_retryable?, failure_resets_at?, compression_exhausted?,
  turn_id, current_turn_user_idx, user_intervened }` [obs `turn_finalizer.py`, `conversation_loop.py`].
- **Close/teardown:** release clients, close active child agents, flush memory provider, finalize
  the owned session row [obs]. Teardown of tool resources (VMs, browsers) happens per task id at
  turn end [obs `cleanup_task_resources`].

### 3.2 Turn lifecycle (state machine)

States and transitions [obs `conversation_loop.py`, `turn_iteration_prep.py`]:

| State | Event | Next |
|---|---|---|
| Admitted | prologue ok | IterStart |
| Admitted | preflight compaction timed out on oversized request | Done(failed, compression_exhausted) |
| IterStart | pending redirect | apply redirect, persist, continue IterStart checks |
| IterStart | interrupt flag set | Done(interrupted) |
| IterStart | budget can be consumed (or grace flag set) | Prepare |
| IterStart | budget exhausted | Finalize(max-iterations path) |
| Prepare → Assemble → Preflight | over threshold and compressible | compress, Assemble again (refunded) |
| Preflight | proven cannot shrink below window | Done(failed, typed) |
| Call | success | Check |
| Call | exception | Classify → retry / rotate / fallback / restart / Done |
| Check | invalid shape | retry with backoff / eager fallback / Done(invalid) |
| Check | `length` | truncation recovery (continue / restart / Done partial) |
| Check | content-policy refusal | fallback / Done(refusal) |
| Intake | tool calls present | ToolRound |
| Intake | text only | FinalText |
| ToolRound | tools ran | IterStart |
| ToolRound | guardrail halt / persistence failure / 3 invalid-tool strikes | Finalize |
| FinalText | guard asks for continuation (empty, intent-ack, stop gate) | IterStart |
| FinalText | accepted | Finalize |
| any phase | unexpected exception escaping inner machinery | outer handler; after 8 per turn → Finalize(failed) |

- The loop condition is: `(api_calls < max_iterations and budget.remaining > 0) or grace_flag`
  [obs].
- `api_call_count` increments at iteration start; a grace call uses up its one-shot flag rather than
  the budget, so the model gets exactly one extra toolless request after exhaustion [obs].
- Some restarts (redirect, rebuilt-for-fallback, compression pass) **refund** the iteration; a
  separate `restart_count` survives across iterations so refunding restarts cannot loop forever
  [obs `_LoopState.restart_count`].
- Every exit path, including early returns, passes through one wrapper that stamps
  `{turn_id, current_turn_user_idx}` next to the exact `messages` it refers to, after every history
  rewrite [obs `run_conversation`]. Every terminal failed result MUST carry `failure_reason` (a
  classifier reason or a site code) and `failure_retryable` [obs `turn_failure_copy.py`].
- **Turn exit reasons** are logged at INFO for every turn (WARNING when abnormal) — e.g.
  `interrupted_by_user`, `interrupted_by_system(<issuer>)`, `budget_exhausted`,
  `max_iterations_reached(N/M)`, `review_input_budget_exhausted`, `session_persistence_failed`
  [obs]. The rebuild should keep a closed enum of exit reasons [inf].
- **Outer-error cap:** at most 8 exceptions that escape the inner retry/fallback machinery per turn;
  exceptions whose traceback touches only local-processing modules (message repair, content
  helpers) are deemed deterministic local bugs and not retried [obs `_MAX_OUTER_LOOP_ERRORS = 8`,
  `_LOCAL_PROCESSING_MODULES`].

### 3.3 Turn prologue

- **Flow** [obs `turn_context.py build_turn_context`]:
  1. Reset per-turn agent state (the gateway reuses agents): interim-text dedup set, persistence
     failure flags, compression-adoption flag, one-shot reasoning-off flag, auth-refresh tallies,
     last-turn usage.
  2. Re-read credential/base-URL edits saved to the secrets file since last turn (no-op if
     unchanged).
  3. Install a safe stdio guard; strip lone UTF-16 surrogates from the user text.
  4. Restore the system prompt from the session row, or build and persist it (3.4).
  5. Ensure the session DB row exists (with the system prompt populated) before any preflight.
  6. Stage the user row; on the first resumed turn hydrate process-local state from history
     (e.g. the todo list is rebuilt from the latest todo tool result).
  7. Turn-start compaction: optional idle compaction, then preflight compression (3.19).
  8. Run `pre_llm_call` plugin hooks; their returned context is appended to **this turn's user
     message** (API copy), never the system prompt.
  9. Notify memory providers of a new turn and prefetch external memory once per turn.
  10. Stamp an `api_content` sidecar on the user row: the exact text sent (with injected context)
      is persisted beside the clean text, so a later replay is byte-identical.
  11. Persist the inbound user turn once (crash resilience).
  12. Kick off auto-titling for a session's first message (off the critical path).
- **Errors:** a preflight-compression timeout while the request is still oversized ends the turn
  with a fixed "compression timed out; nothing dropped; start a new session" outcome
  (`partial=True`, `compression_exhausted=True`) instead of sending a request known to overflow
  [obs `_COMPRESSION_TIMEOUT_FINAL_RESPONSE`].
- **Per-turn notes channel:** one-shot notes (gateway must-deliver notes, surface-switch note,
  skills-index delta) are popped and appended to the user message for this turn only, never into
  the system prompt [obs `_pop_turn_note`].

### 3.4 System prompt assembly and the caching invariant

- **Invariant (MUST):** the system prompt is built once per conversation and reused byte-for-byte;
  mutating past context, swapping toolsets, reloading memory or rebuilding the prompt mid-session
  breaks the provider prefix cache and multiplies cost. The single sanctioned rebuild point is
  context compaction [obs root and `agent/AGENTS.md`]. Slash commands that change prompt state
  defer to the next session unless the user opts in to "now" [obs].
- **Tiers** [obs `system_prompt.py build_system_prompt_parts`] joined with blank lines in the
  order stable → context → volatile:
  - *stable*: identity (persona file, else built-in default identity); product-help pointer (a
    variant that names a help skill only when both the skill-view tool and that skill are
    present); task-completion and parallel-tool-call guidance (only when tools exist, each
    config-gated); tool-aware guidance (memory / session search / skills, only when those tools are
    loaded); the steer-channel trust note (only with tools); model-family guidance (see below);
    a vendor-specific identity line for one provider that misreports its model name; auto-loaded
    pinned skills; the coding-posture operating brief; and, for sessions with **no** workspace
    snapshot, the trailing workspace guidance + platform hint.
  - *context*: caller `system_message`, project context files (3.5), then the git workspace
    snapshot, then trailing workspace guidance, operator instructions and platform hint. Repo-wide
    files are placed ahead of every worktree-specific line, so two worktrees of one repo reuse the
    longest possible common prefix [obs].
  - *volatile*: skills index; built-in memory snapshot; user-profile snapshot; external memory
    provider block (only when its tools are exposed); frozen plugin prompt sections; active
    profile line (names the home path, so it is volatile); the timestamp line; and last the
    runtime environment block (host OS, home, **current working directory**, or a probe of a
    remote terminal backend) between a fixed heading and a fixed end marker.
- **Timestamp line** [obs `_timestamp_line`]: date only (no time) so the prompt is stable for the
  day; includes IANA zone, abbreviation and UTC offset; on a rebuild on a later day a second line
  gives "today's date as of the last rebuild"; trailer lines `Session ID` (only when configured),
  `Model`, `Provider`, `Platform`.
- **Model-family guidance gates** [obs `prompt_builder.py`]: each gate is config `auto | true |
  false | [substrings]`; `auto` matches substrings of the model name.
  - Tool-use enforcement ("act, don't describe"): `gpt, codex, gemini, gemma, grok, glm, qwen,
    deepseek, muse`; Gemini/Gemma additionally get a Google-specific operational block.
  - Execution discipline guidance: `gpt, codex, grok, deepseek, kimi, qwen, glm, minimax, mimo,
    mistral, muse` (text varies with the session's toolset, still cache-safe).
  - Models whose system role is sent as `developer`: `gpt-5`, `codex` [obs `DEVELOPER_ROLE_MODELS`].
- **Platform hints** [obs `PLATFORM_HINTS` keys `cron, cli, tui, desktop, api_server`; messaging
  platforms supply theirs via plugins]. Config `platform_hints.<platform>` may `append`, `replace`
  (wins over append) or be a bare string (= append); malformed entries are ignored and the default
  stays. A cron agent also gets the delivery channel's hint under a "delivery destination" line.
- **Ephemeral layers (never cached):** `ephemeral_system_prompt` (appended to the system text at
  request time only), prefill messages, gateway session overlays, later-turn memory recall,
  plugin pre-LLM context [obs `build_api_messages`].
- **Restore on resume** [obs `_restore_or_build_system_prompt`]: when history exists, the stored
  prompt from the session row is reused if it "matches the runtime": the cwd/host paragraph after
  the runtime heading, and model/provider lines, must match the live agent. Mismatch → rebuild
  and persist. Row states `missing | null | empty | present` are logged; DB failures log WARNING
  (a silent cache miss otherwise). Legacy prompts with the older layout still validate.
- **Surface switch without rebuild** [obs `surface_switch.py`]: `Platform:` is deliberately not an
  identity field; moving between desktop and TUI keeps the stored bytes and delivers the new
  surface's guidance as a one-shot user-channel note (a rebuild cost one session a 1% cache hit on
  220K tokens).
- **Skills index drift:** each turn compares the index the session would build now with the one in
  the prompt; additions/removals ride the one-shot note channel, only when the difference changes;
  the prompt converges at the next compaction [obs `skills_index_delta.py`].
- **Invalidate (on compaction only):** clear cached prompt, reload memory from disk, re-render
  plugin sections (keeping the previous bytes as a fail-open fallback if a plugin now raises)
  [obs `invalidate_system_prompt`].
- **Prompt-surface review:** the reference renders the stock system prompt + tool array for nine
  pinned surfaces into committed fixtures and fails CI when stale [obs docs]. Recommend the
  rebuild adopt the same golden-file test [inf].

### 3.5 Persona file and project context files

- **Persona file** [obs persona loader in `prompt_builder.py`, `personality.md`]: lives only in the product home (never
  the cwd); seeded on first run, never overwritten; empty, whitespace-only, unreadable, or
  safe-worker mode → built-in default identity; content is injected verbatim as slot 1 with no
  wrapper, after injection scan and truncation. Loaded when context files are enabled OR when the
  "load persona identity" switch is on (cron keeps persona while skipping cwd files). Subagents
  with skip-context-files get the default identity. It is never repeated in the context-files
  section. The repo's own root persona file is empty (0 lines) [obs].
- **Persona trust:** an injection-scan hit on the user's own persona file is logged and the file
  still loads; a persona file planted by a third-party "profile distribution" (manifest owns it, or
  manifest unreadable → fail closed) is **blocked** like a project file [obs].
- **Project context discovery** [obs `prompt_builder.py _CONTEXT_FILE_CANDIDATES`]: discovery
  stops at the first *category* that yields a file with content; lower categories are ignored:
  1. native file (`.indus.md` / `INDUS.md`) — nearest match walking from cwd up to the git root;
     optional YAML frontmatter stripped.
  2. `AGENTS.md` — a merged **chain** from the git root down to the cwd (root first, deepest last,
     so deeper wins); in each directory, whichever of `AGENTS.override.md`, `AGENTS.md`,
     `agents.md` is found first with content; outside a git repo only the cwd. Each file gets a provenance heading.
  3. `CLAUDE.md` / `claude.md` — only the cwd; the first one with content.
  4. `.cursorrules` plus `.cursor/rules/*.mdc` — cwd only, all concatenated.
- **Guards** [obs]: an `AGENTS.md` inside the product's own install tree is never treated as
  project context (a fallback cwd inside the install is suppressed); each read runs on a daemon thread with a 5 s default
  timeout (configurable) so network filesystems cannot stall startup.
- **Injection scan:** project files matching "context"-scope threat patterns (invisible unicode,
  "ignore previous instructions", exfiltration shapes) are replaced with a one-line BLOCKED marker
  naming the file and findings [obs `_scan_context_content`].
- **Truncation** [obs]: cap = explicit `context_file_max_chars` if set, else
  `context_length × 4 chars/token × 0.06`, clamped to [20,000, 500,000] chars; unknown window →
  20,000. Over cap: keep 70% head + 20% tail with a middle marker that states kept/total chars,
  lists up to 15 omitted markdown headings (fenced code ignored), and tells the model to read the
  full file with the read tool. A warning is queued to the chat status line (per-build context
  variable so concurrent builds do not steal each other's warnings).
- **Subdirectory hints** [obs `subdirectory_hints.py`]: when tool calls touch a new directory
  (`path`, `file_path`, `workdir` args, or `cd`/`pushd` targets in terminal commands), whichever file has content first among `AGENTS.override.md, AGENTS.md, agents.md, CLAUDE.md, claude.md, .cursorrules` there
  (walking at most 5 ancestor levels) is appended to **that tool result**, head+tail truncated at
  32,000 chars (logged, not queued as a warning). Paths outside the working dir, home-like dirs and
  dependency/build trees are rejected so global files like a user-level `CLAUDE.md` never mix in.
- **`@` references** (`@file:`, `@folder:`, `@diff`, `@git:`, `@url:`, plugin prefixes) are
  expanded into the user message before the turn; credential/internal paths are refused fail-closed
  [obs `context_references.py`]. Detailed behaviour belongs with CLI/input (04).

### 3.6 Mid-turn injection channels (role-alternation safe)

- **Invariant (MUST):** content that must arrive mid-conversation rides a user message or a tool
  result, never the system prompt; never two same-role messages in a row; no synthetic user
  message mid-loop except the steer row [obs `agent/AGENTS.md`].
- **Steer row:** a standalone `user` row placed after the newest tool result
  (the sequence `assistant(tool_calls) → tool… → user` is accepted by all provider wires); wrapped in a
  self-describing out-of-band marker and typed `display_kind = steer` so the alternation fixer will
  not fold a later real prompt into it [obs `steer_user_row`]. It MUST NOT be appended onto an
  already-persisted tool row (append-only persistence would diverge from the wire) [obs].
- **Budget / wall-clock notices:** appended to the content of the **newest tool row that has not
  yet been persisted** (only the current tail is mutable; older rows may be cached). Run-budget
  wrap-up at 80% of `run_budget_seconds`, injected once [obs `_maybe_inject_run_budget_wrapup`].
  Optional iteration-budget warning at `budget_warning_ratio` (off by default) [obs]. Intermediate
  context-pressure warnings were removed because models gave up early [obs docs].
- **Control-frame openers** the product emits (steer marker, compaction handoff, runtime/system
  notes, async delegation) are listed so consumers that republish model output as user text can
  relabel them and a reply cannot forge a trusted frame [obs `CONTROL_FRAME_OPENERS`].

### 3.7 Request assembly and send-time sanitization

- **Flow per iteration** [obs `turn_request_assembly.py`, `turn_api_request.py`]:
  1. Build the wire copy from the durable transcript; substitute each row's `api_content` sidecar
     for its `content`; strip persistence-only fields.
  2. Append mixture-of-agents context (if the turn uses it), inject prefill messages.
  3. Run the context-engine selection hook and send-time sanitizers.
  4. Canonicalize tool-call argument JSON (sorted keys, compact) for bit-identical cache prefixes
     (memo capped at 4,096 entries / 32 MiB) [obs `_canonicalize_tool_call_arguments`].
  5. Build the prompt-cache plan **last**, after every transcript mutation.
  6. Measure request pressure (tokens) for the preflight gate.
  7. Per attempt: redo reasoning-echo padding and cache markers for whichever provider is live
     **now** (after a fallback it is not the primary), build kwargs, surrogate/ASCII chokepoints, provider-specific
     headers, request middleware, `pre_api_request` hook, optional debug dump.
- **Alternation repair** [obs `agent_runtime_helpers.py repair_message_sequence`] in passes:
  merge consecutive assistant rows (Responses interim rows exempt); drop tool results that do not
  follow a known tool call; remove tool calls that lack a result among the tool rows directly after them;
  merge consecutive plain-text user rows (no input lost); decode sentinel-encoded content first.
  A cursor keeps the "last flushed DB index" consistent after repairs.
- **API-message sanitizer (runs before every call, on copies)** [obs `sanitize_api_messages`]:
  drop roles the API rejects; drop empty `tool_calls: []`; coerce function names to
  `^[A-Za-z0-9_-]{1,64}$`; drop results without ids; pair calls and results positionally for strict
  providers; dedupe tool-call ids (a repeated id is renamed with a deterministic `_d<n>` suffix rather
  than a random UUID, keeping the cache prefix stable); realign each result's wire `name` with its call; fill
  empty non-final messages with a placeholder (heals counted per session, one-time notice).
- **Thinking-only rows** (assistant turns with only reasoning) are dropped and adjacent user rows
  merged for providers that reject them [obs `drop_thinking_only_and_merge_users`].
- **Corrupted tool-call arguments** in history are repaired in place, or dropped with a fixed
  marker so the session stays alive [obs].

### 3.8 Provider and model abstraction

- **Provider profile (declarative)** [obs `providers/base.py`]:
  `ProviderProfile { name, api_mode: chat_completions|codex_responses|anthropic_messages|
  bedrock_converse|<plugin mode>, aliases[], display_name, description, signup_url, hidden,
  env_vars[] (priority order), base_url, models_url?, auth_type: api_key|oauth_device_code|
  oauth_external|copilot|aws_sdk, supports_health_check, supports_model_listing, auth_handler?,
  refresh_credential?, classify_api_error?, supports_vision, supports_vision_tool_messages,
  supports_prompt_cache_key, native_reasoning_details_type?, process_command?/args? (external
  process providers), fallback_models[], model_aliases{}, hostname, default_headers{},
  fixed_temperature?, default_max_tokens?, unsupported_response_formats[], default_aux_model,
  model_capabilities{} }` with overridable hooks: hostname derivation, message preprocessing,
  extra-body builder, extra top-level kwargs, supported reasoning efforts per model (cache-only,
  hot path), live model listing, account-usage fetch, cheap aux model, per-model context length.
  Profiles describe; they do not own clients, rotation or streaming [obs].
- **Registry** [obs `providers/__init__.py`]: lazily discovered on first lookup from (1) bundled
  provider plugins (42 directories), (2) user plugins under the profile home (override bundled of
  the same name), (3) pip entry points. Per-profile-home layers are re-stat'ed so a plugin added at
  runtime is seen. Lookup by name or alias.
- **Bundled provider families** [obs `plugins/model-providers/`]: actual, ai-gateway (Vercel),
  alibaba, alibaba-coding-plan, anthropic, arcee, azure-foundry, bedrock, commandcode, copilot,
  copilot-acp, custom, deepinfra, deepseek, fireworks, gemini, gmi, huggingface, kilocode,
  kimi-coding, lmstudio, meta-ai, minimax, nebius-token-factory, the vendor portal, novita,
  nvidia, ollama-cloud, openai-codex, opencode-zen, openrouter, qwen-oauth, router, solstice,
  stepfun, tencent-tokenhub, upstage, vertex, xai, xiaomi, zai. Plus named custom providers from a
  `providers:` config dict (legacy `custom_providers` list still read).
- **API modes and transports** [obs `agent/transports/`]: each transport does only the format work
  for a single API mode: convert messages → convert tools → build kwargs → normalize response (+ optional
  response validation, cache-stat extraction, stop-reason mapping; unknown → `stop`). Registered:
  `chat_completions`, `codex_responses`, `anthropic_messages`, `bedrock_converse`; there is
  also a subprocess mode `codex_app_server` that hands the whole turn to an external agent process and
  projects its events back [obs]. Gemini keeps `chat_completions` mode but swaps in a native
  `generateContent` facade on Google's own endpoint [obs `gemini_native_adapter.py`].
- **Normalized response** [obs `transports/types.py`]:
  `NormalizedResponse { content?, tool_calls?: ToolCall[], finish_reason: stop|tool_calls|length|
  content_filter, reasoning?, usage?: Usage, provider_data? }`,
  `ToolCall { id?, name, arguments (JSON string), provider_data? }`,
  `Usage { prompt_tokens, completion_tokens, total_tokens, cached_tokens }`.
  Internally every mode converges on OpenAI-style message dicts before and after calls; reasoning
  is stored on the assistant row as `reasoning` (+ opaque `reasoning_details` records preserved in
  arrival order, never merged or rewritten) [obs `provider-runtime.md`, `build_assistant_message`].
- **API-mode resolution order** [obs docs + `runtime_provider.py`]: explicit arg/config `api_mode`
  (legacy spellings canonicalized; plugin modes valid) → provider profile → base-URL heuristics
  → `chat_completions`. URL rules: exact hostname table; official OpenAI hosts (incl. regional) →
  Responses; path ending `/anthropic` or `/anthropic/v1`, or one vendor's `/coding` route →
  Anthropic Messages. Exact-hostname matching MUST reject lookalike subdomains and path-segment
  spoofing [obs].
- **Runtime resolution precedence** [obs docs]: explicit CLI/runtime request → saved config
  model/provider → environment variables → provider defaults/auto. Saved config is the source of
  truth so a stale shell export cannot silently override the endpoint the user last picked.
  Output: `RuntimeRoute { provider, api_mode, base_url, api_key, source, expiry/refresh meta? }`.
- **Key scoping (MUST)** [obs docs]: each provider key is sent only to its own host (aggregator
  key only to the aggregator's host, gateway key only to the gateway's host); the generic OpenAI
  key is for custom endpoints and fallback. Distinguish a user-chosen custom endpoint from the
  aggregator default path.
- **Model switch in place** [obs `switch_model`]: snapshot every mutated field, swap route,
  reload pool, rebuild client, re-resolve context length (preloading a local server if needed),
  repoint the compressor, re-derive request overrides; any failure rolls back atomically. A
  switch also resets reasoning-replay verdicts and re-probes the auxiliary compression model.
- **Context length resolution** [obs `model_metadata.get_model_context_length`]: explicit config
  override → aggregator/virtual provider rules → endpoint-scoped metadata → profile-declared
  bound → persistent cache (skipped for some local/subscription routes; validated) → Bedrock
  probe/table → live `/models` for custom endpoints → vendor model APIs → local server probes →
  hardcoded family table (longest-key substring) → 256,000 fallback (warned once per model+URL).
  Probe tiers for unknowns: 256K, 128K, 64K, 32K, 16K, 8K. Sessions, model switches and cron
  reject models below 64,000 tokens [obs `MINIMUM_CONTEXT_LENGTH`]. Only provider-confirmed lengths
  are persisted.
- **Reasoning effort** [obs constants module, `reasoning_effort.py`]: internal ladder `minimal, low,
  medium, high, xhigh, max, ultra` plus `none`; `none/false/disabled` → disabled; per-model
  overrides with spelling tolerance beat the global setting; the wire clamps an unsupported level
  to the nearest **weaker** supported one. Anthropic: budget-style thinking uses budgets xhigh
  32,000 / high 16,000 / medium 8,000 / low 4,000 (default 8,000), forces temperature 1 and
  `max_tokens ≥ budget + 4,096`; adaptive-thinking models use an effort field and request
  summarized display; models that think by default get an explicit disable when allowed
  [obs `anthropic_adapter.py`].
- **Aggregator routing preferences** are validated and attached (sort, allow/ignore/order lists,
  require-parameters, data-collection, minimum coding score) [obs constructor params].

### 3.9 Call execution and streaming

- **Interruptible call** [obs `chat_completion_helpers.py`]: the request runs on a worker thread
  while the conversation thread polls for: response ready, interrupt flag, stale timeout. On
  interrupt the worker is abandoned (its sockets are shut down without closing the FD from a
  foreign thread) and nothing partial enters history as a normal reply. For subagents and cron
  turns the request executes **on the calling thread** (a nested worker wedged before the socket opened) but still
  stream, with only the monitor on a side thread [obs `should_use_direct_api_call`].
- **Streaming preferred** even with no consumer, because stale-stream and read-timeout health
  checks need it [obs `_should_stream`]. A session flips permanently to non-streaming when: the
  provider rejects streaming, cloud IAM denies stream invoke, a custom Anthropic-protocol server
  emits events out of order, or a gateway answers with contentless SSE keepalive frames before any
  delta. Local llama-style servers stay on streaming (cannot cancel non-streamed requests) [obs].
- **Single-writer stream ownership** [obs `stream_delivery.py`]: each stream attempt claims a writer
  token; deltas from a superseded attempt are discarded (counted). A stateful think-tag scrubber
  holds partial `<think>` tags across delta boundaries so reasoning never leaks as visible text
  [obs `think_scrubber.py`]. A repetition guard detects degenerate loops in streaming or final text
  [obs `repetition_guard.py`].
- **Stream-level reconnects:** up to `INDUS_STREAM_RETRIES` = 2 extra attempts; backoff between
  reconnects 1 s → 2 s → 4 s (no jitter), interruptible in 0.1 s steps; the stale clock restarts
  after backoff. One compatibility retry when a strict server rejects `stream_options` does not
  consume the budget. If `/model` switched the route while the request was in flight, the stream
  retry is skipped and the turn loop rebuilds for the new route [obs `_StreamingCall._call`].
- **Partial-stream stub:** if visible deltas were already sent and the stream then fails, the call
  returns a `length`-finish stub with the delivered text (tool calls stripped) so the continuation
  machinery resumes; if tool calls were mid-stream, a warning naming up to 3 dropped tools is shown
  and they are not executed; if the error is a context overflow, the stub is empty and terminal
  (continuing would re-send a larger request) [obs `_partial_stream_stub`].
- **Timeouts (exact defaults)** [obs]:

| Knob | Default | Notes |
|---|---|---|
| Request (write) timeout | 1,800 s | per-model `timeout_seconds` > provider `request_timeout_seconds` > env |
| Stream read timeout | 120 s | raised to the request timeout for local endpoints; raised to the stale timeout for cloud reasoning streams |
| Connect / pool timeout | 30 s | 60 s cap when provider-configured |
| Stream stale timeout (cloud) | 180 s | scaled ≥240 s above 50K est. tokens, ≥300 s above 100K; reasoning-model floor; explicit provider setting wins over all floors |
| Stream stale timeout (local) | 900 s | config `agent.local_stream_stale_timeout` |
| Non-stream stale timeout | 90 s | ≥150 s above 50K tokens, ≥240 s above 100K; disabled (∞) for local endpoints when nothing configured |
| Responses-API subscription stale floor | 600 s >10K tok, 900 s >50K, 1,200 s >100K | hard ceiling 1,500 s (0 disables) |
| High-effort reasoning silence floor | 300 s | Responses mode, implicit only |
| First-progress watchdog (Responses) | 300 s | |
| Run-budget cap | implicit timeouts ≤ max(60 s, 50% of remaining run budget) | explicit settings never capped |
| Stale breaker | 5 consecutive stale attempts → fail immediately | 0 disables; an interrupt after ≥30 s of silence counts as stale |
| Direct-call activity heartbeat | 15 s | |

- **Errors and recovery:** see 3.10–3.13. Provider errors encoded inside stream content (error
  frames, plain-text SSE, `finish_reason: error`) are converted to exceptions with an OpenAI-like
  body so the classifier sees them [obs `ProviderStreamError`].

### 3.10 Error classification

- **Shape:** `ClassifiedError { reason, status_code?, provider?, model?, message, error_context{},
  retryable=true, should_compress=false, should_rotate_credential=false, should_fallback=false }`
  [obs `error_classifier.py`].
- **Reasons** [obs]: `auth`, `auth_permanent`, `billing`, `rate_limit`, `upstream_rate_limit`,
  `upstream_blocked`, `overloaded`, `server_error`, `timeout`, `ssl_cert_verification`,
  `context_overflow`, `payload_too_large`, `image_too_large`, `image_corrupt`, `model_not_found`,
  `provider_policy_blocked`, `content_policy_blocked`, `model_entitlement`, `incomplete_response`,
  `format_error`, `role_alternation`, `invalid_encrypted_content`,
  `multimodal_tool_content_unsupported`, `reasoning_mandatory`, `thinking_signature`,
  `long_context_tier`, `oauth_long_context_beta_forbidden`, `llama_cpp_grammar_pattern`, `unknown`.
- **Pipeline order (first verdict wins)** [obs `_STAGES`]: plugin classifier → provider profile
  classifier → provider special cases → HTTP status → mixture-of-agents shapes → structured error
  code → message patterns → transport exception types → `unknown` (retryable with backoff).
- **Notable status rules** [obs]:
  - 403: structured "upstream unavailable" → overloaded; key/spend limit or billing phrases →
    billing; WAF/CDN block/challenge markers → `upstream_blocked` (key healthy, fall back, do not
    bench); else auth.
  - 404: structured billing code → billing; known patterns; a bare model id the catalog only knows
    with a vendor prefix → model_not_found; generic 404 → unknown (surface the real error).
  - 429: structured billing code → billing; overload phrases → overloaded (back off on same key);
    aggregator-wrapped upstream 429 → `upstream_rate_limit` with fallback; quota-wall phrases
    without a rate-limit phrase or reset signal → billing; with a parseable reset → rate_limit with
    `reset_at`.
  - 402: "usage limit, try again in N minutes" is periodic quota, not billing.
  - 5xx: request-validation wording → format_error (fail fast) unless the provider injected the
    parameter; overflow-as-5xx → context_overflow; else server_error.
  - An SDK `RateLimitError` without status is forced to 429.
- **Billing nuance:** one vendor sends an identical exhausted-extra-usage body when it is in fact
  filtering content, so that verdict is marked *unverified* (short pool cooldown, hedged copy) [obs].
- Auth refusals from a non-stock endpoint append the contacted host to the message so a key sent to
  the wrong URL reads as such [obs].

### 3.11 Retry and backoff (inner loop)

- **Budget:** `agent.api_max_retries` default 3 (min 1) attempts per iteration [obs `agent_init.py`].
- **Pre-classification recovery** (one-shot each, same retry count): Unicode encode errors (strip
  lone surrogates; then an ASCII fallback only if the runtime really needs it; repair non-ASCII in
  headers/keys); image-size or corrupt-image errors (shrink, or strip images from the wire copy)
  [obs `recover_before_classification`].
- **Post-classification one-shot chain** [obs `recover_after_classification`]: credential refresh
  on 401 per provider family; format-repair strips (thinking signatures, stale encrypted reasoning,
  grammar `pattern`/`format` keys for llama.cpp, multimodal tool content downgrade, disable
  reasoning-off flag when the route mandates reasoning, merge same-role rows); clamp max output to
  the affordable budget on a credit-limited 402; long-context tier cap.
- **Backoff** [obs `compute_error_backoff`, `retry_utils.py`]: `Retry-After` (numeric or HTTP-date;
  also free-text "retry after N s", `quotaResetDelay`, "resets in Xh Ym", `resets_in_seconds`)
  wins for any retryable error, capped at 600 s; else jittered exponential
  `min(2 × 2^(n-1), 60) + U(0, 0.5 × delay)`; jitter seed = time ⊕ counter. One coding-plan
  overload shape keeps 3 short retries then 30/60/90/120 s waits. Sleeps are sliced into 200 ms
  so interrupts are honoured. Retry chatter is buffered and shown only if every retry fails; waits
  over 60 s are announced immediately.
- **Exhaustion order** [obs `turn_api_error.py`]: rebuild primary client once for transient transport
  errors (`ReadTimeout, ConnectTimeout, PoolTimeout, ConnectError, ReadError, RemoteProtocolError,
  APIConnectionError, APITimeoutError`) → credential pool rotation → fallback chain → auto-recovery
  ladder → terminal result.
- **Auto-recovery ladder** [obs `turn_recovery_autorecover.py`]: only when nothing user-visible was
  delivered yet and the failure is transient (5xx, overloaded/529, connect/read timeouts): park the
  turn with a visible countdown and re-enter the retry loop; `agent.auto_recovery_cycles` default 5
  (0 disables), with waits of 15, 30, 60, 60, 60 s plus jitter (or the provider-stated reset).
- **Cross-session rate guard:** for the vendor-portal provider, a genuine account-level 429 is
  recorded so every session skips attempts until it lifts [obs portal rate-guard phase in `turn_api_call.py`].
- **Terminal copy** states what happened and what to do; raw provider detail goes on a trailing
  "Provider said:/Details:" line; when a reset time is known it is named ("Limit resets at HH:mm",
  `failure_resets_at`) [obs `turn_failure_copy.py`].

### 3.12 Credential pools (same-provider rotation)

- **Purpose:** several keys/OAuth grants for one provider; tried **before** cross-provider fallback
  [obs `credential-pools.md`, `credential_pool.py` 3,097 lines].
- **Strategies:** `fill_first` (default; priority order), `round_robin`, `least_used`, `random`;
  each successful selection increments an in-memory request count persisted only on the next pool
  write; status reads are peeks (never refresh, rotate or bench) [obs docs].
- **Error policy** [obs docs]:

| Error | Action | Default cooldown |
|---|---|---|
| 429 generic | retry same key once; second consecutive 429 rotates | 1 h |
| 429 plan/usage limit | rotate immediately | 1 h or provider reset |
| 402 billing | rotate immediately | 1 h |
| 401 | refresh OAuth first; rotate if refresh fails | 5 min |
| 400 subscription model entitlement | bench the key for that model only | until manual reset |
| all keys exhausted | fall through to fallback chain | — |

  Provider `reset_at` overrides defaults. The "retried 429" flag resets on every success. One
  vendor's 429s are per model, so its generic 429 benches only that model. A quota-benched
  session moves back to the original key as soon as its cooldown lifts. HTTP-200
  `status: failed` Responses payloads are classified by their embedded error with the same rules.
- **Thread safety / sharing:** subagents share the parent's pool; a counter limits how often a single
  pool entry is refreshed while 401s persist [obs]. Storage format belongs to 03/04.

### 3.13 Fallback chain and primary restore

- **Config:** ordered list of `{provider, model}`; both required, otherwise the entry is ignored;
  legacy single-pair form accepted and migrated on write [obs docs].
- **Triggers** [obs]: retries exhausted on invalid responses; non-retryable client errors (401,
  403, 404); retries exhausted on transient errors (429, 5xx); content-filter refusals; Responses
  turns stuck on reasoning-only output for 3 consecutive continuations (`incomplete_response`; if
  that consumed the budget, the fallback gets one bounded grace call); eager fallback for some
  verdicts (`should_fallback`).
- **Activation** [obs `try_activate_fallback`]: optional "stay on primary" when the primary's
  declared reset is sooner than `fallback.min_switch_reset_seconds` (default 0 = off); arm a
  primary rate-limit cooldown (provider reset, else `min(60 × 2^k, 14,400)` s); walk the chain from
  the current index, skipping entries known unavailable, malformed, locally unusable without
  network, or whose credentials are all in long exhaustion cooldowns; bind the route (build client
  through the shared resolver, re-detect API mode from the entry's own base URL, rebind the
  credential pool, re-resolve reasoning config, rescope extra-body keys, point the compressor at the
  fallback's window, re-evaluate prompt caching); rewrite the `Model:`/`Provider:` lines of the
  cached prompt; emit a durable notice naming both routes and the reason; reset the stale streak;
  reset the retry counter; continue the loop.
- **Turn scope (MUST):** at the start of the next turn the primary is restored unless it is still
  in rate-limit cooldown, its model is known unentitled or non-chat, or the pool's reset gate says
  it would fail; on restore a "primary restored" notice is emitted only if a real fallback had been
  active. The chain index is reset even when no fallback was activated (a failed activation once
  stranded the index past the end and blocked all future fallbacks) [obs `restore_primary_runtime`].
- **Not covered:** subagent delegation (inherits provider, not chain); auxiliary tasks (own chains);
  cron jobs pinned to their own provider [obs docs].
- **Cache warning:** any route change (switch, fallback, key rotation) means zero cache hits on the
  next request; never add features that silently swap model or credentials mid-session [obs docs].

### 3.14 Response checks and final-text guards

- **Response check** [obs `turn_response_check.py`]: stop the thinking indicator; validate shape
  via the transport (None choices, missing content → retry with backoff, eager fallback, or terminal
  invalid-response result whose `failure_reason` comes from an embedded provider error if any);
  derive `finish_reason` per API mode (Responses max-output exhaustion is a normal incomplete turn);
  route content-policy refusals and `length`; fold usage (3.20).
- **Intake** [obs `turn_response_intake.py`]: coerce dict/list content to a string (some local
  servers); emit `post_api_request`; pass reasoning text on to the progress callback (subagents send only
  the first line); apply incomplete-scratchpad and Responses-incomplete continuations (max 3 per
  turn).
- **Truncation (`length`) recovery, order load-bearing** [obs `turn_truncation.py`]:
  1. Thinking-budget exhaustion and repetition-dominated output abort with a clear error (a
     degenerate loop once produced a 60K-char reply split into 31 chat messages).
  2. Content-filter stream stall → fallback chain.
  3. Text truncation: append the fragment plus a continuation nudge, up to 4 times; stop early when
     usage shows the prompt itself filled the window (minimum 512 tokens of headroom); at the
     ceiling, collapse the fragment/nudge trail into one partial answer.
  4. Truncated tool call: re-request (up to 4) with a boosted output cap `base × 2^n`, never below
     the failed cap, at most `max(32,768, 2 × cap)`, never above the model's known output limit; a
     stream stub counts as a network stall; never execute incomplete arguments.
- **Empty / thinking-only final ladder, order load-bearing** [obs `turn_empty_response.py`]:
  partial-stream recovery → reuse prior turn content (housekeeping-only tools) → one post-tool nudge
  → thinking-only prefill continuation (×2) → empty retries with jittered backoff
  `base 5 s, max 60 s` (budget 3, or 1 when one attempt is estimated to cost more than
  USD 0.25; deterministic-empty short-circuit) → fallback → persist an `(empty)` sentinel row and
  return a localized explanation.
- **Reasoning promotion:** on a trusted route, a clean `stop` whose whole answer arrived as
  reasoning (parser lost the closing delimiter) is returned as the answer but stored with empty
  content, reasoning fields, and an `api_content` sidecar — CoT stored as ordinary content is
  indistinguishable from a reply on history surfaces [obs `finish_text_response`].
- **Intent-ack / stall continuation:** a short reply that ends by announcing the next action, or a
  degenerate fragment, gets a continuation nudge (each ≤2 per turn; dropped-tool-call re-prompt ≤3);
  mode `agent.intent_ack_continuation: auto|off|…` [obs].
- **Stop gates** [obs `turn_stop_gates.py`]: language-server diagnostics for files edited this turn
  that arrived after their tool results, verify-on-stop, a `pre_verify` plugin hook after code edits, and a board-worker terminal-tool
  guard may append the candidate answer as an interim row plus a nudge and continue; the candidate
  is kept as the best answer if the budget then runs out.
- **Codex-style ack nudges, verification scaffolding and length-continuation rows are private
  scaffolding** and MUST be stripped before the durable snapshot, otherwise a later "continue"
  replays them [obs `_drop_transcript_scaffolding`].

### 3.15 Tool round and the dispatch contract

- **Flow (exact order)** [obs `turn_tool_round.py`, `run_agent._execute_tool_calls`]:
  1. Validate calls (3.15a), uniquify ids, cap `delegate_task` calls per batch to the configured
     max concurrent children (non-delegate calls all kept), drop duplicate `(name, canonical args)`
     pairs (first wins).
  2. Stage and **persist the assistant tool-call row before any side effect** (MUST).
  3. Plan the batch into ordered segments (3.15b) and execute.
  4. Append exactly one tool result per call in **original call order**.
  5. Enforce per-turn aggregate result budget, then drain pending steer as a user row.
  6. Honour guardrail halts and persistence failures; then run post-tool compression check.
- **3.15a Validation** [obs `turn_tool_validation.py`]: unknown tool names get auto-repair (case,
  separators, CamelCase, `_tool` suffix) then tool-role error results listing valid names; 3
  consecutive invalid batches end the turn as `partial`. Malformed JSON args: up to 3 re-requests,
  then recovery error results. Every call always receives a tool-role result (never a user
  message) so alternation holds.
- **3.15b Parallel planning** [obs `tool_dispatch_helpers._plan_tool_batch_segments`]: a single call
  runs sequentially. Otherwise each call is a **barrier** (never parallel: clarify-style interactive
  tools and a few GUI tools; unparseable/non-dict args; tools needing extended deadlines; anything
  not whitelisted) or **parallel-admissible**: read-only tools (web search/extract, session search,
  skill view/list, vision, image generation, connector batches, tool-search lookups, MCP tools whose
  server opted in) and path-scoped file tools. Path-scoped readers (read, search) may overlap each
  other; a writer (write, patch) conflicts with any overlapping path and closes the current run.
  Runs shorter than 2 become sequential; adjacent sequential segments merge; call order never
  crosses a barrier.
- **Concurrency limits** [obs `tool_executor.py`]: 8 workers per batch; image generation capped at
  `image_gen.max_parallel_requests` (default 4); per-batch deadline 420 s (config
  `timeouts.tools.concurrent_batch`); start-order gate 120 s so prompts appear in call order;
  authorization lock bound 360 s (human approval wait time is excluded from batch deadlines);
  sequential per-call deadline from config. A heartbeat thread stamps agent activity while tools
  run. Unfinished slots (deadline, interrupt, thread death) get synthesized results.
- **Dispatch contract** [obs `model_tools.handle_function_call`]: `dispatch(name, args, task_id,
  tool_call_id, session_id, turn_id, api_request_id, user_task?, enabled_tools?,
  enabled/disabled_toolsets?) → string` (JSON). Steps: coerce args; map legacy aliases; unwrap the
  tool-search bridge (`tool_search`/`tool_describe` inline, `tool_call` re-dispatched under the real
  name so hooks see it); connector gating; request middleware (fail-open); refuse agent-loop tools
  (todo, memory, session search, delegate) that must be handled inline; `pre_tool_call` hook (may
  block, never on hook failure) and editor approval; registry handler (async handlers run on a
  persistent per-thread event loop); `post_tool_call` observer exactly once per call with
  status/duration; `transform_tool_result` hook may replace the string. Exceptions become a
  sanitized error string, never raise into the loop.
- **Inline (agent-level) tools** [obs `inline_tool_executors.py`]: a table maps tool name →
  executor `(agent, args, ctx) → result` for tools needing live agent state (todo, memory, session
  search, delegate, connection/catalog management, GUI previews). Adding one = a table entry, not an
  if-chain.
- **Result message** [obs `make_tool_result_message`]: `{role: tool, name, tool_name, content,
  tool_call_id, timestamp, _tool_output_risk?, effect_disposition?}`. Results of web
  search/extract and any `browser_*` / `mcp_*` tool ≥32 chars are wrapped in an
  untrusted-data block that tells the model to treat the content as data; delimiter tokens inside
  content are neutralized case-insensitively; no "already wrapped" fast path (forgeable). An
  elision notice is added inside the block when upstream truncation markers are seen.
- **Result size budgets** (owned by tools, 02) scale with the window: per result
  `clamp(window×4×0.15, 8,000, 100,000)` chars and per turn `clamp(window×4×0.30, 16,000, 200,000)`
  chars; oversize results spill to disk with a preview [obs `tools/budget_config.py`].
- **Guardrails** [obs `tool_guardrails.py`]: per-turn controller over a stable hash of
  `(tool, canonical args)`. Defaults: exact-failure warn after 2, block after 5; same-tool failure
  warn after 3, halt after 8; idempotent no-progress warn after 2, block after 5; per-turn caps of
  50 web searches and 50 subagent spawns (0 disables). Hard stops are off on attended surfaces
  (cli, tui, desktop, editor, subagent, API server) and on for unattended ones (gateway, cron).
  Successful mutating tools (write, patch, terminal, code exec, browser actions, delegate, todo,
  memory, …) reset failure streaks. When the same call repeats and returns exactly the same bytes
  again, the model sees a short pointer stub instead of the full result. Guidance is appended to the tool result at construction
  (cache-safe); a halt ends the turn with a plain-language reply inviting "continue".

### 3.16 Toolsets

- **Shape:** `Toolset { description, tools[], includes[], platforms?, posture? }`; static table plus
  registry/plugin-registered toolsets; resolution is recursive with cycle/diamond tolerance and a
  memo bounded to 256 entries [obs `toolsets.py`].
- **Core list** shared by CLI and every messaging bundle: web search/extract, terminal + process,
  file read/write/patch/search, vision, image generation, skills list/view/manage, ~15 browser
  tools, text-to-speech, todo, memory, session search, clarify, code execution, delegate, cron
  management, board (kanban) tools, computer use, connection management [obs]. Desktop GUI
  affordances are deliberately not in it.
- **Named toolsets:** `web, search, x_search, vision, video, image_gen, video_gen, computer_use,
  terminal, skills, browser, cronjob, file, tts, todo, memory, context_engine, session_search,
  connections, project, bot_room, desktop_ui, onboarding, catalog, clarify, code_execution,
  delegation, kanban, discord, discord_admin, …` plus scenario sets `debugging`, `safe`, `coding`
  (posture: core minus image gen, TTS, cron, computer use), per-platform bundles (core + platform
  extras), an editor bundle (coding minus clarify), an API-server bundle (no clarify/TTS/computer
  use), a webhook bundle (web + vision + clarify only — untrusted payloads), and a gateway union.
- **Selection** [obs `get_tool_definitions`]: enabled = None → all; `all`/`*` spans every toolset
  except platform-gated ones; disabled toolsets are subtracted at the very end; each tool's
  availability check filters; dynamic schema rewriters adjust descriptions to name only tools
  actually present; schemas are normalized for strict grammar converters; finally **tool search**
  replaces MCP/plugin tools with a 3-tool bridge when the deferrable surface exceeds a configured
  share of the window (core tools never deferred). Results are memoized (LRU) but callers always
  get a **shallow copy** (appending memory/engine schemas to a shared list once produced duplicate
  names that strict providers reject with 400).
- **Process-global "last resolved tool names"** is saved/restored around subagent runs; readers may
  see a stale value during a child run [obs `agent/AGENTS.md`].
- **Batch toolset distributions** [obs `toolset_distributions.py`]: named maps toolset → % chance,
  sampled independently per prompt; a `a+b` key rolls once for a group. Used for data generation
  runs only.

### 3.17 Interrupt, steer and redirect

- **interrupt(message?, hard_cancel?, tool_reason?, require_generation?)** — callable from any
  thread; sets the flag, fans the per-thread tool interrupt bit out to concurrent tool workers,
  aborts the registered in-flight request's sockets. A generation claim makes it a no-op if the
  turn already resumed. Hard cancel also drops an unconsumed steer; ordinary clears keep it
  [obs `interrupt_control.py`].
- **Effect points:** checked at iteration start (turn ends `interrupted`); during a provider call
  (worker abandoned; a pending redirect keeps its correction); during backoff (200 ms slices);
  during tool batches (unstarted calls get "skipped" results, running ones get cancelled results);
  during the max-iterations summary (summary nudge popped, turn ends interrupted). Interrupt text
  for a model wait uses a stable prefix so front ends treat it as metadata.
- **steer(text, ticket?)** — buffer text that will be delivered as a separate user row once the
  running tool batch completes (no interrupt); multiple steers join with newlines; tickets let a host withdraw exactly one.
  Drain points: pre-API, post-batch, and the finalizer (leftovers become the next turn's user
  message). Steer text is never silently lost after the host was told it was accepted [obs].
- **redirect(text)** — during a model request: cancel only that request, keep completed rows, add
  an assistant checkpoint (visible streamed text, or a hidden placeholder row) and a real user
  correction whose `api_content` carries an interruption scaffold plus the partial visible text,
  then retry; during tool execution it degrades to steer; returns false with no live turn
  [obs `_apply_active_turn_redirect`]. Raw chain-of-thought MUST never enter replayable content
  (it reads as a prefill jailbreak and caused empty-response storms); runaway-repetition partials
  are dropped.
- **Host-owned cancellation scope** lets an embedding host cancel agents created deep inside
  synchronous work; agents registering after cancel are interrupted immediately [obs
  `interrupt_scope.py`].

### 3.18 Budgets

- **Iteration budget:** thread-safe consume/refund counter per agent; code-execution iterations are
  refunded [obs `iteration_budget.py`]. Defaults conflict across layers (constructor: unlimited;
  config defaults: `agent.max_turns: null` = unlimited; CLI config loader: 500; docs: 500;
  subagents: `delegation.max_iterations` 50; goal loops: 20) [obs] — see Open question 1.
- **Exhaustion:** if a verification gate held back an answer, return it; else make one extra
  toolless request with a fixed "summarize what you did" user nudge (1 retry on empty; `<think>`
  blocks stripped); failure → a fixed "reached the iteration limit" message
  [obs `handle_max_iterations`, `_resolve_budget_fallback`].
- **Wall-clock run budget** `run_budget_seconds` (off by default): wrap-up notice at 80%, implicit
  timeouts capped (3.9) [obs].
- **Review-fork input budget:** detached review forks stop between iterations once cumulative input
  tokens exceed their budget [obs].

### 3.19 Context window management and compression (loop side)

- **Context engine interface** [obs `context_engine.py`]: `ContextEngine { name, last_prompt_tokens,
  last_completion_tokens, last_total_tokens, threshold_tokens, context_length, on_session_start(),
  update_from_response(usage), should_compress(tokens?), compress(messages, …), tools?(),
  on_session_end() }`; selected by `context.engine` (default built-in compressor; plugins never
  auto-activate). Memory context passed to engines is redacted and capped at 6,000 chars
  (4,000 head + 1,500 tail).
- **Token accounting = usage anchor** [obs `usage_anchor.py`]: the provider's last real
  `prompt_tokens` (+ completion tokens) is authoritative; only messages appended since are estimated
  locally at **4 chars/token**; the anchor is identified by a content fingerprint and persisted on
  the session row so a restart or re-read keeps it; compaction/reset clears it. Without an anchor,
  a rough over-threshold estimate **waits one request** for real usage rather than compressing
  (estimates do not prove overflow). Opaque encrypted blobs count 0. Image cost is learned per
  `model@host` from the residual between real prompt tokens and the text-only projection (default
  1,500 tokens per image before the first vision turn).
- **Trigger threshold** [obs `context_compressor.py`]:
  - effective input window = `context_length − max_tokens` (whole window if unset);
  - percent = per-model override (longest substring key wins; `provider:substring` keys scoped to
    a provider) else global `compression.threshold` (default 0.50);
  - **raise-only floor:** windows < 512,000 tokens use ≥ 0.75;
  - trigger = `max(window × percent, 64,000)`, but if the 64K minimum is the deciding term it is limited to 85%
    of the window; a trigger ≥ window becomes 85%; optional absolute cap `threshold_tokens`; a smaller
    auxiliary summarizer window lowers the trigger durably.
  - Note: the developer guide's "200K model → 100K trigger" example ignores the 0.75 floor
    (code gives 150K) [obs] — trap R01.7.
- **Gates that may compress:** turn-start preflight, idle (opt-in after N seconds idle, default
  off), pre-API pressure, post-tool, provider-proven overflow, manual `/compress` (focus topic,
  `here [N]`, preview, aggressive) [obs].
- **Attempt cap:** `compression.max_attempts` default 3 (≤10) consecutive ineffective attempts per
  turn; re-armed only when a provider response reports prompt tokens below threshold after a
  completed compaction [obs `_should_rearm_compression_budget`]. Two provider-confirmed ineffective
  compactions trip a breaker.
- **Failure cooldowns:** stalls/timeouts escalate 60 → 300 → 900 s (never shorter than
  `compression.context_timeout_seconds`, default 120); output-cap-truncated summaries escalate on a
  separate counter along the same ladder; decode/closed-stream/empty failures use a flat 30 s;
  cooldown persisted in the state DB. Bypasses: manual compress (clears), fallback-chain retry of a
  stalled summary, provider-proven overflow (one bounded attempt without clearing). A repeated
  stall ends with a deterministic static summary committed through the normal pipeline.
  Summary-provider overload: abort and keep the transcript, but after 3 consecutive overload aborts
  commit the deterministic summary. `abort_on_summary_failure: true` always aborts [obs docs].
- **Algorithm (built-in engine)** [obs docs + constants]:
  1. Replace tool outputs longer than 200 chars that sit before the protected tail with a one-line placeholder (no
     LLM). The newest unanswered tool round stays verbatim. Pruned skill loads get a
     `[SKILL_PRUNED: …]` marker and are listed under a pruned-skills heading (max 20 markers).
  2. Boundaries: head = first 3 messages (system + first exchange); tail by token budget walking
     backwards. Lean mode (default): `2.5% × window` clamped to [10,000, 25,000] tokens and ≤ 20% of
     the window, soft overrun 1.5×; at least 8 rows; at least `min_tail_user_messages` (default 1)
     real user messages; legacy mode: `threshold × target_ratio (0.20)`. Never split a tool-call
     group (walk back to the parent assistant).
  3. Summarize the middle with the auxiliary model in **one** call: structured sections (goal,
     constraints, progress done/in-progress/blocked, key decisions, relevant files, next steps,
     critical context); lean mode adds a detailed session log (≈4,000 tokens), a regex-extracted
     anchor index (PR numbers, SHAs, paths, errors; 7,000 chars), every real user message verbatim
     newest-first (24,000-char budget, 4,000 per message), and a pointer to session search for
     recovery. Summary budget `content × 0.20` clamped to [2,000, min(window × 0.05, 12,000)]
     (constant ceiling 10,000 in code — see Open question 6); summarizer input ≤ 160,000 chars with
     even sampling and elision markers. Re-compression updates the previous summary instead of
     starting over.
  4. Assemble head + summary (role chosen to keep alternation) + tail; orphaned call/result pairs
     are cleaned (results without calls dropped; calls without results get stub results); the
     system prompt is rebuilt.
- **In-place vs rotating** (`compression.in_place`, default true): in-place keeps one session id and
  soft-archives pre-compaction rows; rotating creates a child session. The `session:compress`
  event carries the mode. Storage side → 03.
- **Overflow recovery** [obs `turn_overflow.py`]: 413 → compress and retry, progress scored in
  bytes; "max_tokens too large" (input fits, input + output does not) → clamp output cap from the
  error; "prompt too long" → adopt the provider-reported limit only when it states one (else keep),
  compress, retry; probe down the tier list for unknown models; failure → typed terminal result.
- **Provider-native compaction:** opt-in server-side compaction on specific Responses-API routes;
  native compaction threshold = local trigger − 8,192 (min 1,024; 200,000 when no local trigger)
  [obs docs]; an external-agent subprocess route compacts itself. The local compressor stays armed
  as fallback.
- **Micro-compaction** (folding the oldest exchange between turns) exists but is OFF by default
  because every pass breaks the prefix cache [obs `micro_compaction.py`].
- **Prompt caching (Anthropic-style markers)** [obs `prompt_caching.py`]: at most 4 breakpoints:
  when a static system prefix exists the markers go on that prefix, on the system prompt's final
  block, and on the two newest non-system rows; with no static prefix, one system marker plus the
  three newest rows (the docs call this "system_and_3").
  Marker placement: string content becomes a one-part list with the marker; list content marks the
  last part; empty content or tool messages mark the envelope (tool envelope only on the native
  wire). TTL `5m` (default), `1h`, or `auto` = one hour when a person drives the session and five minutes for automated sources
  (subagent, cron, oneshot, webhook, kanban, api, tool, batch). Enabled for Claude models on the
  native API or the aggregator, and for Qwen-family models on a few routes; markers are stripped
  before sending to routes that do not support them; the plan is built per destination.

### 3.20 Usage and cost accounting

- **Canonical usage** [obs `usage_pricing.py`]: `CanonicalUsage { input_tokens, output_tokens,
  cache_read_tokens, cache_write_tokens, reasoning_tokens, request_count, raw_usage? }` with
  `prompt_tokens = input + cache_read + cache_write`. Anthropic reports uncached input directly;
  Chat/Responses totals include cached tokens, so cache buckets are subtracted out.
- **Per response** [obs `turn_usage.record_response_usage`]: increment API-call count even if usage
  is missing; fold usage into the context engine and anchor; re-arm the compression budget when
  proven; add to session counters (prompt, completion, total, input, output, cache read, cache
  write, reasoning); keep rolling latency/output history (last 10); persist only provider-confirmed
  context lengths; queue token deltas for the state DB; log one INFO line with in/out/total,
  latency, cache hit %, cache write tokens, response id and upstream provider.
- **Cost** [obs]: status `actual | estimated | included | unknown` with a source label; subscription
  routes report "included"; first-party list prices are applied only when the request really went
  to that vendor's host on 443; sub-cent costs render with 4 decimals, never as `$0.00`.
- **Rate-limit headers** `x-ratelimit-*` (incl. hourly variants) captured for a usage command [obs].

### 3.21 Auxiliary model client (side tasks)

- **Purpose:** compression summaries, vision, web extraction summarization, titles, session search,
  memory flush, curator, embeddings, MCP helpers — each task can pin `provider / model / base_url /
  reasoning_effort` under `auxiliary.<task>` [obs `auxiliary_client.py` 8,392 lines].
- **Auto route** [obs `_resolve_auto_route`]: (1) main provider + main model ("auto" means "use my
  main model"); (2) the task's own fallback chain, then the main agent's chain; (3) only if neither a
  policy nor a usable main client exists, a discovery chain (aggregator → vendor portal → custom →
  native Anthropic → direct key providers). Vision has its own chain. A free-only switch limits the
  aggregator lane to free SKUs. HTTP 402 falls through the chain.
- **Hooks:** `pre_auxiliary_call` / `post_auxiliary_call` (they only observe; their failures are ignored); main-loop
  `pre/post_api_request` MUST NOT fire for auxiliary calls [obs `agent/AGENTS.md`].
- **Summary model window must be ≥ main model's** or compaction degrades (summary call fails with
  context error → middle dropped) [obs docs].

### 3.22 Turn finalization

- **Flow** [obs `turn_finalizer.finalize_turn`]: budget fallback (3.18) → drop scaffolding →
  recover final text from the stream if the terminal completion was empty → shape the transcript
  tail → durable persist → turn-exit log → file-mutation footer (when write/patch calls failed or
  did not land; on-disk stat signature check) → abnormal-exit explainer → `transform_llm_output`
  once, then `post_llm_call` → result record → leftover steer drain → background memory/skill review
  scheduling → external memory sync → context-engine turn-complete hook → per-task resource cleanup.
  Every cleanup step is individually guarded so it can never lose the response [obs].
- **Reasoning returned** is only from the current turn (stop at this turn's user row) [obs].
- **Trajectories:** optional JSONL export in a ShareGPT-like format with `<think>` blocks for every
  model turn [obs `convert_to_trajectory_format`].

### 3.23 Turn admission, lease and liveness

- **Durable turn lease** [obs `turn_facade_lease.py`]: a session stored in the shared state DB
  can be loaded, run and flushed by a single process at once; TTL 300 s refreshed periodically, wait
  up to 1,800 s for another holder (user told "waiting"); a follow-up that interrupts the wait is
  carried into the turn, not consumed. Lease loss interrupts the turn.
- **Liveness watchdog** [obs `turn_liveness.py`]: abort turns idle for 600 s (poll 15 s;
  `agent.turn_liveness`, ≤0 disables); the abort is bound to the observed (generation, timestamp)
  and revalidated under the activity lock so a resumed turn is never cancelled. Tools touch activity
  through heartbeats.
- **Turn author:** who wrote the user side of a turn (`{id, name, is_bot}`) is read per turn from an
  env entry that the dispatcher sets only on a one-shot child and removes after reading [obs
  `turn_author.py`].
- **In-flight tripwire:** warns if a new turn begins before the prior turn of that agent/session
  has been persisted [obs `note_turn_start`].

### 3.24 Other turn modes (breadth only)

- **Mixture-of-agents** turns gather reference-model answers before each iteration and append the
  aggregate to the user message (no byte-stable sidecar for those turns) [obs `moa_loop.py`].
- **Scripted prelude:** a caller-owned generator can play tool calls before the first model call
  (desktop onboarding) through the normal tool round [obs `turn_scripted_prelude.py`].
- **Side questions** (`/btw`) fork a detached cache-parity copy that answers about the conversation
  without touching it [obs `side_question.py`]. **One-shot** stateless helper calls exist for commit
  messages etc. [obs `oneshot.py`].
- **Fast mode** windows layer a provider priority tier onto requests for a bounded time (default 60 s
  per user turn in auto mode) [obs `fast_mode.py`].

## 4. File map (what to read in the Read step)

| Sub-area | Reference path | Lines | Read what | Docs / tests to read with it |
|---|---|---|---|---|
| Facade / constructor surface | `run_agent.py` | 1555 | constructor params, tool dedupe/cap, `_execute_tool_calls`, stale-timeout helpers; skip `main` | `developer-guide/agent-loop.md` (243) |
| Init phases | `agent/agent_init.py` | 2393 | phase order, defaults (retries, auto-recovery, compression config) | — |
| Turn loop | `agent/conversation_loop.py` | 1867 | `_LoopState`, `_run_conversation_turn`, prompt restore, redirect, constants | `agent/AGENTS.md` (152) |
| Turn prologue | `agent/turn_context.py` | 1356 | `build_turn_context`, `build_api_messages`, sidecars, notes | — |
| Turn phases | `agent/turn_*.py` (~32 files) | 103–1938 | each phase's docstring + verdict type | `tests/agent/test_steer.py`, `test_tool_batch_segmentation.py`, `test_max_iteration_handoff.py` |
| Recovery | `agent/turn_recovery.py` | 1938 | `route_classified_error`, `compute_error_backoff`, terminal results | `tests/agent/test_error_classifier*.py` |
| Error taxonomy | `agent/error_classifier.py` | 1504 | enum, `_STAGES`, status handlers, pattern tables | — |
| Backoff utils | `agent/retry_utils.py` | 195 | all | — |
| Call drivers + streaming + fallback | `agent/chat_completion_helpers.py` | 4017 | `_StreamingCall`, stale timeouts, `try_activate_fallback`, `handle_max_iterations` | `developer-guide/provider-runtime.md` (229) |
| Runtime helpers | `agent/agent_runtime_helpers.py` | 3807 | `repair_message_sequence`, `sanitize_api_messages`, `restore_primary_runtime`, `switch_model`, pool recovery, cache policy | — |
| System prompt | `agent/system_prompt.py` | 881 | `build_system_prompt_parts`, timestamp, invalidate | `developer-guide/prompt-assembly.md` (359) |
| Prompt pieces / context files | `agent/prompt_builder.py` | 1779 | context-file discovery, scan, truncation, persona loader, platform hints, guidance gates (not the guidance prose) | `user-guide/features/context-files.md` (249), `personality.md` (289) |
| Subdir hints | `agent/subdirectory_hints.py` | 326 | all | — |
| Tool execution | `agent/tool_executor.py` | 1957 | segment executor, timeouts, ordering, synthesized results | `reference/toolsets-reference.md` (171) |
| Tool dispatch helpers | `agent/tool_dispatch_helpers.py` | 627 | planner, result message, untrusted wrapping | — |
| Inline tools | `agent/inline_tool_executors.py` | 337 | table shape | — |
| Guardrails | `agent/tool_guardrails.py` | 660 | config defaults, controller | `tests/agent/test_agent_guardrails.py` |
| Tool registry front | `model_tools.py` | 993 | `get_tool_definitions`, `handle_function_call` | — |
| Toolsets | `toolsets.py` | 495 | core list, table, `resolve_toolset` | — |
| Distributions | `toolset_distributions.py` | 106 | skim | — |
| Provider ABC / registry | `providers/base.py`, `providers/__init__.py`, `providers/README.md` | 435 / 719 / 79 | profile fields + hooks, discovery order | `developer-guide/adding-providers.md` (466), `model-provider-plugin.md` (616), `integrations/providers.md` (1739, skim) |
| Transports | `agent/transports/*.py` | 53–971 | base, types, chat_completions, codex, anthropic | — |
| Anthropic | `agent/anthropic_adapter.py`, `anthropic_message_convert.py` | 884 / 781 | thinking params, block conversion; ignore OAuth impersonation (R01.12) | — |
| Responses API | `agent/codex_responses_adapter.py`, `codex_runtime.py` | 1312 / 1434 | id policy, reasoning replay, incomplete status | — |
| Gemini / Bedrock | `agent/gemini_native_adapter.py`, `bedrock_adapter.py` | 1012 / 1536 | skim | — |
| Context metadata | `agent/model_metadata.py` | 2636 | `get_model_context_length` order, probe tiers, error parsers | — |
| Credential pool | `agent/credential_pool.py` | 3097 | strategies, cooldowns, selection | `user-guide/features/credential-pools.md` (380) |
| Fallback cooldown | `agent/fallback_cooldown.py` | 129 | all | `user-guide/features/fallback-providers.md` (463) |
| Interrupt / steer | `agent/interrupt_control.py` | 425 | public methods | `tests/agent/test_steer.py` |
| Streaming delivery | `agent/stream_delivery.py`, `think_scrubber.py`, `repetition_guard.py` | 377 / 187 / 194 | writer token, scrubbing | — |
| Compression | `agent/context_engine.py`, `context_compressor.py`, `conversation_compression.py`, `compression_facade.py`, `usage_anchor.py` | 250 / 5823 / 4500 / 328 / 203 | see giant files | `developer-guide/context-compression-and-caching.md` (814), `context-engine-plugin.md` (370) |
| Prompt caching | `agent/prompt_caching.py` | 377 | all | `tests/agent/test_anthropic_prompt_cache_policy.py` |
| Usage / cost | `agent/turn_usage.py`, `usage_pricing.py` | 304 / 785 | canonical usage, cost status | — |
| Aux client | `agent/auxiliary_client.py` | 8392 | see giant files | — |
| Budget | `agent/iteration_budget.py`, `empty_response_guard.py` | 59 / 243 | all | `tests/agent/test_iteration_budget_*.py` |
| Constants / paths | constants module (`<ref>_constants.py`) | 1467 | home resolution, reasoning-effort parser only | — |
| Utils | `utils.py` | 796 | atomic writes, base-URL host matching | — |
| Contributor rules | `AGENTS.md`, `agent/AGENTS.md` | 169 / 152 | caching + alternation invariants | — |
| Persona | persona file at repo root (name in §2) | 0 | empty file | `website/docs/guides/` persona guide page |

Giant files (>2,000 lines) and the parts that matter:
- `agent/auxiliary_client.py` [8392]: read only `_resolve_auto_route` (~line 4665), the
  provider-client builder after it ("Centralized Provider Router"), and the `call_llm` entry; the rest
  is per-vendor client quirks, caching and health.
- `agent/context_compressor.py` [5823]: the constants block (lines ~100–1170), `_compute_threshold_tokens`
  (~2775), `_effective_threshold_percent` (~2761), `should_compress_info` (~3004), then the
  `compress` method; skip telemetry and salvage paths on first read.
- `agent/conversation_compression.py` [4500]: the thread-safety contract docstring and the
  commit/lease/fence pipeline; mostly concurrency plumbing.
- `agent/chat_completion_helpers.py` [4017]: stale-timeout helpers (lines 427–700), `_StreamingCall`
  (2703–3993), `try_activate_fallback` (2055), `handle_max_iterations` (2278).
- `agent/agent_runtime_helpers.py` [3807]: repair passes (550–790), sanitizer (2820–3170),
  `restore_primary_runtime` (1428), `switch_model` (2427), `invoke_tool` (2510).
- `agent/credential_pool.py` [3097]: selection + exhaustion/cooldown methods; storage format later.
- `agent/model_metadata.py` [2636]: `get_model_context_length` (~2215) and the context-limit error
  parsers (~1221–1400).
- `agent/agent_init.py` [2393]: the phase list at the end (`init_agent`) and config-section readers.

## 5. Minimum viable subset

v1 needs:
1. Agent object with `run_turn` / `chat`, callbacks for stream delta, tool progress, reasoning,
   status, clarify, step.
2. Turn loop with iteration budget + grace summary call, outer-error cap, interrupt checks,
   persist-user-first and persist-before-tools, strict alternation repair and API sanitizer.
3. System prompt builder with the three tiers, persona file + default identity, context-file
   discovery (native file, AGENTS chain, CLAUDE.md, cursor rules) with scan + truncation, platform
   hint, timestamp line, runtime-environment block; build-once caching and restore-on-resume.
4. Two transports: Chat Completions (covers aggregator, custom, local servers, most vendors) and
   Anthropic Messages; provider profile registry with a handful of bundled profiles (aggregator,
   custom, Anthropic, OpenAI) and key-to-host scoping.
5. Streaming with stale detection, read/connect timeouts, partial-stream stub, interruptible
   worker; non-streaming fallback switch.
6. Error classifier (status + message tables for the core reasons), retry with Retry-After and
   jittered backoff, fallback chain with turn-scoped primary restore.
7. Tool round: validation, dedupe, ordered results, sequential execution plus the parallel planner
   for read-only tools, untrusted-result wrapping, guardrails (warn-only default).
8. Toolsets table + resolver + definitions with shallow-copy memo.
9. Usage normalization and session counters; context length resolution (config override, table,
   `/models`, 256K fallback, 64K minimum).
10. Built-in compressor: threshold rule, tool-result pruning, boundary alignment, one auxiliary
    summary call, deterministic fallback summary, failure cooldown, overflow recovery; Anthropic
    cache markers.
11. Steer and redirect (CLI uses both).

Can wait: Responses API and its subscription quirks, Bedrock/Gemini-native/subprocess transports,
credential pools (single key first), auto-recovery ladder, provider-native compaction,
micro-compaction, mixture-of-agents, scripted prelude, side questions, fast mode, stop gates and
verification nudges, cross-process lease (needed once desktop + CLI share sessions), liveness
watchdog, tool search bridge, cost pricing tables, surface-switch notes, skills-index delta.
Reason: each is an optimization or a provider-specific edge; the loop contract above is what every
later feature plugs into.

## 6. Hard parts / traps

1. **R01.1 Prompt-cache byte stability.** Anything that re-renders the system prompt, reorders
   tools, regenerates ids or re-serializes tool arguments mid-session silently multiplies cost.
   The rebuild MUST build the prompt once, persist it, restore it verbatim on resume, canonicalize
   tool-argument JSON, use deterministic tool-call-id collision suffixes, and route every mid-session
   addition through the user message or the newest unpersisted tool result. Add a golden-file test
   of the rendered prompt + tool array per surface.
2. **R01.2 Role alternation under every failure path.** Interrupts, invalid tool names, malformed
   args, timeouts, crashes and redirects must all leave exactly one tool result per call and no
   same-role neighbours; the steer row is the only allowed mid-loop user row and must be its own row,
   tagged so the fixer cannot fold another prompt into it. Strict providers (several Chinese vendors, chat templates)
   reject otherwise. Repair runs on the live history; sanitization runs on the wire copy.
3. **R01.3 Persist order.** User row before the first provider call; assistant tool-call row before
   any tool side effect; scaffolding (continuation nudges, verification prompts, ack nudges) stripped
   before the durable snapshot. Getting this wrong causes replays of private nudges on "continue"
   and duplicate side effects after crashes.
4. **R01.4 Interrupt races.** Abandon the worker instead of joining it; close sockets with
   shutdown (not close) from a foreign thread; bind interrupts to a generation so a resumed turn is
   not cancelled; never drop an accepted steer except on hard cancel; a mid-request redirect must not
   put raw chain-of-thought into replayable content (caused empty-response storms).
5. **R01.5 Streaming stale detection vs slow reasoning.** Fixed timeouts kill healthy reasoning
   streams; the reference scales stale timeouts by estimated context size, floors them per reasoning
   model, lets an explicit operator setting win in both directions, raises the read timeout to match,
   disables non-stream stale detection for local servers, and uses a 5-strike breaker. Retries after
   a model switch must not replay the old payload against the new route (404 storm).
6. **R01.6 Partial delivery.** Once text has been streamed to the user, a later error must turn into
   a continuation stub (not a retry that duplicates text, not an error that discards it); dropped
   mid-stream tool calls must never execute; a context-overflow mid-stream must end the turn rather
   than continue into a bigger request.
7. **R01.7 Compression threshold arithmetic and docs drift.** Code applies a raise-only 0.75 floor
   below 512K windows, a 64K minimum floor capped at 85% of the window, the output reservation
   subtraction, an optional absolute cap, and an auxiliary-window ceiling. The developer guide's
   worked example (200K → 100K) is wrong per code (150K). Spec from code; write table tests.
8. **R01.8 Estimates are not evidence.** Rough char/4 estimates must not trigger compaction on
   their own when no provider usage exists yet (wait one request); otherwise sessions compact on every
   resume/restart. Conversely, a provider-proven overflow must bypass cooldowns for one bounded
   attempt or the session wedges.
9. **R01.9 Compression failure loops.** Summary backends stall, overload or truncate; without
   escalating cooldowns, a per-turn attempt cap, a breaker on ineffective compactions and a
   deterministic fallback summary, the agent re-issues expensive failing summaries every turn or
   grows the transcript until the gateway resets the session.
10. **R01.10 Fallback is turn-scoped and stateful.** Activation swaps model, provider, URL, API mode,
    client, pool, reasoning config, extra body, compressor window, cache policy and prompt identity
    lines together; restore must undo all of it and reset the chain index even when activation
    failed (a stranded index once disabled fallback forever). Restore must not announce recovery
    for an unentitled primary.
11. **R01.11 Key leakage across endpoints.** Provider keys must be scoped to their own hosts with
    exact hostname matching (reject lookalike subdomains and path spoofing); API-mode detection uses
    the same exact matching. A saved config route must beat stale shell exports.
12. **R01.12 Vendor impersonation and billing evasion.** The reference's Anthropic OAuth path
    prefixes the system prompt with another vendor's CLI identity line, sends that CLI's version
    headers, and renames two tools on the wire because the vendor's billing classifier
    fingerprinted them. The clean-room rebuild MUST NOT copy this; treat subscription-OAuth routes as
    a lead/legal decision (see Open question 4). Same for any header that claims to be another
    product.
13. **R01.13 Tool-definition list aliasing.** Memoized tool-definition lists must be copied before
    callers append engine/memory schemas; a shared list accumulated duplicate tool names in a
    long-lived gateway and strict providers returned 400.
14. **R01.14 Parallel tool safety.** Only whitelisted read-only tools and non-overlapping path
    scopes may run in parallel; writers conflict with any overlapping reservation; interactive tools
    are barriers; results go back in call order; human-approval waits are excluded from batch
    deadlines; image generation has its own lower cap.
15. **R01.15 Context-file trust.** Project files can carry prompt injection; scan and block them;
    never load install-tree or user-global agent files as project context; subdirectory hints must
    stay inside the working dir. The docs disagree on AGENTS.md (developer guide: cwd only; user
    guide and code: git-root-to-cwd chain) — follow the code.
16. **R01.16 Thread/profile scope.** One process serves many profiles; background threads (title
    generation, memory flush, compression worker) must copy context variables and resolve the home
    from the agent's own session DB, never from ambient environment, or they leak across profiles.
17. **R01.17 Empty and degenerate outputs.** Thinking-only, empty, repetition-dominated, and
    reasoning-filed answers each need a distinct recovery; empty retries re-bill the full input, so
    the budget shrinks to 1 for expensive requests. Without the ladder the user sees blank replies.
18. **R01.18 Exit-path completeness.** Every early return must carry the turn boundary stamp,
    failure reason and retryable flag, and release voice/route overrides; the reference funnels all
    exits through one wrapper. Design the rebuild's loop with a single exit funnel from day one.

## 7. Open questions

1. What is the shipped default turn cap — unlimited (config defaults `agent.max_turns: null`),
   500 (CLI config loader and docs), or the constructor's unlimited default? Needed: needs REA —
   run the shipped CLI with a looping task and read the effective cap; then a lead decision.
2. Which of the 42 bundled provider profiles does v1 ship, and does the rebuild offer a first-party
   "portal" provider at all? Needed: product decision by the lead.
3. Exact default text of the built-in identity, task-completion, tool-use enforcement, execution
   discipline and platform-hint blocks — the rebuild must write its own copy; should it keep the same
   model-family gating lists? Needed: lead decision; golden prompt diff once written.
4. Should the rebuild support subscription OAuth routes (Anthropic consumer plans, ChatGPT/Codex
   subscription, Copilot) whose reference implementations rely on impersonation or undocumented
   endpoints? Needed: legal/product decision by the lead.
5. Persona file name (reference name in §2): keep a compatible name or rename? Needed: lead
   decision; check whether users migrate files between products.
6. Summary-token ceiling: docs say `min(window × 0.05, 12,000)`, code constant is 10,000
   (`_SUMMARY_TOKENS_CEILING`). Which applies at runtime? Needed: needs REA — capture a compaction
   request on a 1M-window model and read `max_tokens`.
7. Prompt caching layout actually sent on the wire (4 markers: static prefix + end of system + last
   2, vs. system + last 3) for native Anthropic vs. aggregator routes. Needed: needs REA — capture
   two consecutive requests on each route.
8. Does the shipped build stream by default on every surface, and what does `display.streaming:
   false` change in the loop (callbacks only, or the transport)? Needed: needs REA runtime capture.
9. Auto-recovery ladder copy and countdown UX (what the user sees while parked). Needed: needs REA —
   force a 503 from a local mock provider and record the CLI output.
10. Credential pool on-disk format and locking (shared with 03/04). Needed: read `credential_pool.py`
    storage section + `auth.json` sample from a shipped install (needs REA).
11. Is the cross-process session turn lease needed in v1 if the rebuild launches CLI-only first?
    Needed: lead decision tied to 03/06 sequencing.
12. Tool-search bridge thresholds (share of window that triggers deferral, tier rules). Needed:
    read `tools/tool_search.py` in area 02.

## 8. What to read later

- Reference docs: `website/docs/developer-guide/agent-loop.md`, `prompt-assembly.md`,
  `provider-runtime.md`, `context-compression-and-caching.md` (814 lines; sections "Compression
  Algorithm", "Prompt Caching", "Configuration"), `adding-providers.md`, `model-provider-plugin.md`,
  `context-engine-plugin.md`; user guide `features/context-files.md`, `features/personality.md`,
  `features/credential-pools.md`, `features/fallback-providers.md`, `features/provider-routing.md`,
  `configuring-models.md`, `local-models.md`; `reference/toolsets-reference.md`,
  `reference/model-catalog.md`; `plugins/model-providers/README.md`.
- Reference tests that list edge cases (`tests/agent/`, 977 files): `test_steer.py`,
  `test_steer_compaction_persistence.py`, `test_tool_batch_segmentation.py`,
  `test_error_classifier.py` (+ `_content_policy`, `_observed`, `_upstream_blocked`),
  `test_413_compression.py`, `test_compression_budget_rearm.py`, `test_compression_boundary.py`,
  `test_max_iteration_handoff.py`, `test_iteration_budget_race.py`, `test_run_budget.py`,
  `test_anthropic_prompt_cache_policy.py`, `test_anthropic_truncation_continuation.py`,
  `test_agent_guardrails.py`, `test_turn_finalizer_interrupt_alternation.py`,
  `test_summary_role_template_alternation.py`, `test_context_file_sources.py`,
  the persona-file profile-isolation test, `test_cascading_interrupt.py`,
  `test_close_interrupted_tool_sequence.py`; prompt-surface fixtures `tests/fixtures/prompt_surface/`
  (`surfaces.json`, `prompts/`, `tools/`) — read only to learn the surface list, not the text.
- REA Evidence: none yet (REA not connected). Planned bundle `~/rea-lab/evidence/01-agent-core/`
  covering Open questions 1, 6, 7, 8, 9, 10.
- Related specs: `02-tools.md` (registry, result budgets, tool search, delegate), `03-session-db.md`
  (persist markers, lineage, in-place compaction storage, usage anchor row, lease row),
  `04-cli.md` (`/compress`, `/model`, `/fallback`, `/steer`, `/stop`, `@` references),
  `06-gateway.md` (85% hygiene, agent cache, must-deliver notes), `07-skills-memory-plugins-cron.md`
  (memory snapshot blocks, skills index, plugin hooks, cron `skip_memory`).
