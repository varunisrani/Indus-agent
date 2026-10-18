# 02 — Tools and terminal backends: behavioural specification (clean-room)

Scope: every model-callable tool that lives under the reference `tools/` tree (catalogue, inputs, outputs, limits), how tools register and group into toolsets, the tool-result size budget and spill-to-disk path, the dangerous-command approval layer, the terminal tool and its seven execution backends (local, Docker, SSH, Singularity/Apptainer, Modal incl. the managed variant, Daytona, Vercel Sandbox) with their lifecycle and persistence, background processes, child-process secret hygiene, SSRF and file-write safety. Out of scope: the agent loop that calls tools, parallel tool batching and inline interception (01-agent-loop), session DB and transcript storage (03), CLI/TUI rendering of approvals and clarify cards (04), learning-loop semantics of memory/skills/session-search tools (07 — listed in the catalogue here only). The MCP client and the managed-connector tools live under `tools/` and are summarised in §3.21 and §3.27; a deeper MCP spec belongs with whichever area owns integrations [inf].
Reference: the pinned reference repository named in §2 @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056` (MIT); shipped build checked: none (REA not connected in this session — source reading only).
Sources studied: reference docs `website/docs/reference/tools-reference.md`, `reference/toolsets-reference.md`, `developer-guide/tools-runtime.md`, `developer-guide/adding-tools.md`, `developer-guide/terminal-environment-plugin.md`, `user-guide/features/tools.md`, `features/code-execution.md`, `features/delegation.md`, `features/tool-search.md`, `user-guide/security.md`; `tools/AGENTS.md`; code under `tools/` (registry, terminal, environments/*, approval*, process_registry*, file_tools*, file_operations*, fuzzy_match, patch_parser, web_tools*, url_safety, website_policy, browser_tool*, vision_tools*, tts_tool*, transcription*, code_execution*, code_kernel*, delegate_tool*, async_delegation*, todo_tool, clarify_tool, cronjob_tools, tool_result_storage, budget_config, tool_output_*, tool_search*), `toolsets.py`, the CLI package's `config_defaults.py` (defaults only). No tests were read line by line; test directories are pointed to in §8.

Conventions:
- "Tool" = one model-visible function (name + JSON schema + handler). "Toolset" = a named bundle of tool names, possibly including other toolsets.
- "Registry" = the single in-process table of registered tools.
- "Environment" or "backend" = one live execution target for shell commands (a host shell, a container, a remote VM); "env" for short.
- "Task id" = the caller-supplied id a tool call runs under (normally the agent session); "env key" = the derived key under which an environment is cached.
- "Home" = the per-profile application home directory (rebuild: `~/.indus` / `INDUS_HOME`). Written `<home>` below.
- "Session key" = the conversation identity used for approvals, cwd records and process ownership.
- "Spill" = writing an oversized output to a file and returning a preview plus the path.
- "MUST" marks an invariant the reference defends with code (and usually a test or an issue reference).
- Claims are marked [obs] (read in source/docs), [inf] (inferred), [unk] (not settled).

---

## 1. Overview

The tools area is the agent's hands. It owns three things:

1. **A self-registering tool registry.** Each tool module declares itself at import time (name, toolset, schema, handler, optional availability probe, emoji, result-size cap, optional per-call schema override). A discovery pass imports every module whose top level calls the register function. Toolsets map names to bundles; platform bundles (CLI, each messaging platform, editor/ACP, API server, cron, webhook) all start from one shared "core" list. Availability probes are cached ~30 s, and a probe that flaps after a recent success is treated as transient for 60 s. Every handler returns a JSON string (or one specific multimodal envelope); the registry turns any exception into a bounded `{"error": ...}` string so the model never sees a raw traceback [obs].

2. **The terminal subsystem.** A `terminal` tool runs a shell command in the configured backend, foreground or background. All seven backends share one execution contract: every command spawns a fresh `bash -c`, a once-captured shell snapshot (exported vars, functions, aliases) is sourced first, and the working directory persists through an in-band marker line printed after the command. Environments are cached per env key, created under a per-key lock, idle-reaped after 300 s (unless a background process is alive), and torn down at exit. A process registry tracks background jobs (poll/log/wait/kill/stdin, completion notifications, heartbeats, retained results) [obs].

3. **Safety layers.** (a) A dangerous-command gate with an unbypassable "hardline" floor, user deny globs, yolo/off bypass, a permanent allowlist, and human approval via CLI prompt or messaging round-trip (with an optional LLM "smart" pre-judge); isolated container backends skip prompts. (b) File-write guards (protected credential paths, protected instruction files, stale-overwrite refusal). (c) Child-process environment scrubbing so provider keys never reach model-authored commands. (d) SSRF blocking for URL tools. (e) Output redaction of secrets in terminal and file output [obs].

Who calls whom:

```
 model tool_call
      |
 agent loop (01) -- inline tools (todo_list, memory, session_search, delegate_task) handled there
      |
 dispatch(name, args, task_id, ...)  [registry]
      |  kwargs narrowed to handler signature; async handlers bridged; result normalised
      v
 +-------------------------- tool handlers ---------------------------------+
 | terminal ---> plan -> acquire env -> pre-exec guards -> approval gate --+ |
 |                                                                         | |
 |   foreground: env.execute() -> finalize (redact, truncate, annotate)    | |
 |   background: process registry spawn -> notifications                   | |
 | read_file/write_file/patch/search_files ---> file ops over the SAME env  | |
 | execute_code ---> kernel/script child --RPC--> dispatch (allow-listed)   | |
 | web_*, browser_*, vision, tts, ... ---> vendor providers (plugins)       | |
 +--------------------------------------------------------------------------+
      |
 result string -> per-result size cap (spill to <home>/cache/spillover) -> per-turn budget
```

What the user sees: tool results in the transcript (with spill pointers when large), approval prompts/cards for flagged commands, background-process completion messages that start a new turn, and occasional "degraded backend" results with a retry hint. What the system guarantees: the model always receives a well-formed string; no flagged command runs without an approval outcome; the hardline floor and user deny globs can never be bypassed; secrets are stripped from child environments and masked in outputs; a foreground command never outlives its timeout plus a 2 s backstop [obs].

## 2. Reference names (rename in rebuild)

| Kind | Names (reference only; they become banned identifiers) |
|---|---|
| Repository / packages | `NousResearch/hermes-agent`; packages and modules `hermes_cli` (incl. `hermes_cli/config_defaults.py`), `hermes_constants`, `hermes_state*`, `hermes_logging`; reference docs site under `website/docs` |
| Binary / CLI and flags | `hermes`, `hermes tools`, `hermes setup`, `hermes model`, `hermes doctor`, `hermes send`, `hermes pm install chromium`, `hermes plugins install`, `hermes skills install`, `hermes approvals suggest`, `hermes gateway restart/stop/uninstall`, `hermes -z`, `hermes chat -q/-Q --oneshot`, `--yolo`, `/yolo`, `/approve`, `/deny`, `/approve all`, `/browser connect`, `/stop`, `/new`, `/reset`, `/reload-mcp` |
| Env vars | `HERMES_HOME`, `HERMES_YOLO_MODE`, `HERMES_INTERACTIVE`, `HERMES_EXEC_ASK`, `HERMES_GATEWAY_SESSION`, `HERMES_SESSION_*` (ID, KEY, PLATFORM, CHAT_ID, CHAT_NAME, CHAT_TYPE, THREAD_ID, USER_ID, USER_NAME, MESSAGE_ID, SOURCE, PROFILE, SCOPE_ID, PARENT_CHAT_ID, USER_ID_ALT), `HERMES_UI_SESSION_ID`, `HERMES_CRON_SESSION`, `HERMES_CRON_AUTO_DELIVER_*`, `HERMES_BROWSER_CONTROL_PRINCIPAL`, `HERMES_BROWSER_CONTROL_TRANSPORT_FAMILY`, `HERMES_DELEGATED_CHILD_CONTEXT`, `HERMES_RPC_SOCKET`, `HERMES_RPC_DIR`, `HERMES_RPC_TOKEN`, `HERMES_RPC_PERSISTENT`, `HERMES_KERNEL_DIR`, `HERMES_KERNEL_SENTINEL`, `HERMES_KERNEL_SPILL_DIR`, `HERMES_KERNEL_PARENT_*`, `HERMES_KANBAN_TASK`, `HERMES_KANBAN_*`, `HERMES_DEBUG_INTERRUPT`, `HERMES_FORCE_FILE_SYNC`, `HERMES_WRITE_SAFE_ROOT`, `HERMES_ALLOW_PRIVATE_URLS`, `HERMES_VISION_DOWNLOAD_TIMEOUT`, `HERMES_VISION_MAX_CONCURRENCY`, `HERMES_DOCKER_BINARY`, `HERMES_EGRESS_PROXY`, `HERMES_SAFE_MODE`, `HERMES_DESKTOP`, `HERMES_REAL_HOME`, `HERMES_WORKDIR`, `HERMES_PLATFORM`, `HERMES_PROFILE`, `HERMES_TIMEZONE`, `HERMES_CUA_DRIVER_CMD`, `HERMES_LOCAL_STT_COMMAND`, `HERMES_LOCAL_STT_LANGUAGE`, `HERMES_NATIVE_FILE_READ`, `HERMES_DISABLE_FILE_STATE_GUARD`, `HERMES_SINGLE_QUERY_SESSION`, `HERMES_BD_*`, `_HERMES_FORCE_` (prefix), `_HERMES_GATEWAY`; terminal-config bridge vars (unbranded but reference-defined; rebuild should re-prefix): `TERMINAL_ENV`, `TERMINAL_CWD`, `TERMINAL_TIMEOUT`, `TERMINAL_LIFETIME_SECONDS`, `TERMINAL_MAX_FOREGROUND_TIMEOUT`, `TERMINAL_DISK_WARNING_GB`, `TERMINAL_DEGRADED_MODE`, `TERMINAL_PERSISTENT_SHELL`, `TERMINAL_LOCAL_PERSISTENT`, `TERMINAL_LOCAL_MEMORY_MAX_MB`, `TERMINAL_TEMP_DIR`, `TERMINAL_SANDBOX_DIR`, `TERMINAL_SCRATCH_DIR`, `TERMINAL_CONTAINER_CPU/MEMORY/DISK/PERSISTENT`, `TERMINAL_DOCKER_*` (IMAGE, IMAGE_PINNED, FORWARD_ENV, VOLUMES, ENV, EXTRA_ARGS, SHM_SIZE, NETWORK, RUN_AS_HOST_USER, SNAP_COMPAT, MOUNT_CWD_TO_WORKSPACE, PERSIST_ACROSS_PROCESSES, SHARED_CONTAINER_KEY, ORPHAN_REAPER), `TERMINAL_SSH_HOST/USER/PORT/KEY/PERSISTENT`, `TERMINAL_SINGULARITY_IMAGE`, `TERMINAL_MODAL_IMAGE`, `TERMINAL_MODAL_MODE`, `TERMINAL_MANAGED_MODAL_*_TIMEOUT_SECONDS`, `TERMINAL_DAYTONA_IMAGE`, `TERMINAL_VERCEL_IMAGE`, `TERMINAL_VERCEL_RUNTIME`; `SUDO_PASSWORD`, `BROWSER_CDP_URL`, `BROWSER_INACTIVITY_TIMEOUT` |
| Files and folders | `~/.hermes/`, `<home>/config.yaml`, `<home>/.env`, `<home>/processes.json`, `<home>/logs/process-results/`, `<home>/cache/spillover/`, `<home>/cache/terminal-output/`, `<home>/cache/terminal/`, `<home>/cache/exec/`, `<home>/checkpoints/` (store, `refs/hermes/<hash16>`, indexes, projects, ledgers, `.last_prune`), `<home>/sandboxes/docker/<task>/{home,workspace}`, `<home>/modal_snapshots.json`, `<home>/singularity_snapshots.json`, `<home>/vercel_sandbox_snapshots.json`, `<home>/.sync.lock`, `<home>/pending/{memory,skills}/`, `<home>/scratch`, temp names `hermes-snap-<id>.sh`, `hermes-cwd-<id>.txt`, `.hermes-stdin-<hex>`, `hermes_bg_<proc>.{log,pid,exit}`, `hermes-results/`, `hermes-ssh/`, `hermes-overlays/`, `hermes-sync-back-*`, `hermes_rpc`, `hermes_terminal` |
| HTTP routes / URL schemes | managed-Modal gateway paths `/v1/sandboxes`, `/v1/sandboxes/{id}/execs`; `MEDIA:<path>` directive in tool output; `ghcr.io/nousresearch/hermes-agent:latest`; default sandbox image `nousresearch/hermes-sandbox:desktop`; `portal.nousresearch.com` |
| RPC methods / events / commands | in-band markers `__HERMES_CWD_<id>__`, `HERMES_STDIN_<hex>` heredoc delimiter, `__HERMES_FENCE_*__`, `__HERMES_RF_` / `__HERMES_WF_` / `__HERMES_RB_` read/write sentinels, `__hermes_not_regular__`, `__hermes_missing__`; plugin hook names `pre_tool_call`, `post_tool_call`, `transform_terminal_output`, `pre_approval_request`, `post_approval_response`; completion event kinds `process_complete`, `heartbeat`, `watch_match` |
| Settings and storage keys | config sections `terminal.*` (backend, cwd, timeout, lifetime, docker_*, ssh_*, container_*, vercel_*, modal_mode, env_passthrough, credential_files, persistent_shell, degraded_mode, daemon_term_grace_seconds, process_wait_auto_notify, oneshot_completion_wait_seconds, sync_back_max_bytes, home_mode, shell_init_files, auto_source_bashrc, temp_dir), `approvals.*` (mode, timeout, cron_mode, single_query_mode, unattended_mode, smart_policy, denial_breaker_threshold, deny), `command_allowlist`, `tool_output.*` (max_bytes, max_lines, max_line_length), `tool_budget.mcp_result_size_chars`, `file_read_max_chars`, `code_execution.*` (mode, timeout, max_tool_calls, kernel_idle_timeout, max_session_kernels), `delegation.*`, `browser.*`, `web.*`, `vision.*`, `security.*` (allow_private_urls, fake_ip_ranges, website_blocklist, protected_instruction_files, redact_secrets), `tools.tool_search.*`, `sessions.retention_days`, `display.background_process_notifications`; Docker labels `hermes-agent`, `hermes-task-id`, `hermes-profile`, `hermes-egress`, `hermes-environment`; container names `hermes-<hex8>`, Singularity instance `hermes_<hex12>`, Daytona sandbox name `hermes-<task>`, label `hermes_task_id`; SSH/background id prefix `proc_<hex12>` |
| Other (scopes, error tags, IPC channels) | toolset names `hermes-cli`, `hermes-acp`, `hermes-api-server`, `hermes-cron`, `hermes-gateway`, `hermes-webhook`, `hermes-telegram`/…/`hermes-yuanbao` and every `hermes-<platform>` bundle; `_HERMES_CORE_TOOLS`; in-script module name `hermes_tools` (model-visible inside `execute_code`); "Nous Tool Gateway", "Nous Portal", "Hermes Cloud"; wake word "Hey Hermes" |

Model-visible tool names (product names; generic ones may be kept, branded/vendor ones need a decision) [obs]:
- Terminal: `terminal`, `process_manage` (docs also call it `process`).
- File: `read_file`, `write_file`, `patch`, `search_files`.
- Web: `web_search`, `web_extract`; `x_search`.
- Browser: `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, `browser_scroll`, `browser_back`, `browser_press`, `browser_get_images`, `browser_vision`, `browser_console`, `browser_cdp`, `browser_dialog`, `browser_vault`, `browser_exec`.
- Media: `vision_analyze`, `video_analyze`, `image_generate`, `video_generate`, `xai_video_edit`, `xai_video_extend`, `text_to_speech`.
- Agent orchestration: `todo_list` (legacy alias `todo`), `clarify`, `execute_code`, `delegate_task`, `cronjob_manage`.
- Learning loop (area 07): `memory`, `session_search`, `skills_list`, `skill_view`, `skill_manage`.
- Kanban: `kanban_show`, `kanban_list`, `kanban_complete`, `kanban_block`, `kanban_request_review`, `kanban_request_changes`, `kanban_heartbeat`, `kanban_comment`, `kanban_create`, `kanban_link`, `kanban_unblock`, `kanban_attach`, `kanban_attach_url`, `kanban_attachments`, `kanban_schedule` (registered, not in the shared core list).
- Connectors / catalog: `manage_connections`, `manage_catalog`.
- Desktop GUI: `read_terminal`, `close_terminal`, `desktop_preview`, `drive_preview`, `annotate_preview`, `read_window_below`, `focus_pane`, `react_to_message`, `gui_tour`, `show_tip`, `set_theme`, `desktop_project`, `setup_beat`.
- Desktop control: `computer_use`.
- Platform-specific: `discord`, `discord_admin`, `feishu_doc_read`, `feishu_drive_list_comments`, `feishu_drive_list_comment_replies`, `feishu_drive_reply_comment`, `feishu_drive_add_comment`, `yb_query_group_info`, `yb_query_group_members`, `yb_send_dm`, `yb_search_sticker`, `yb_send_sticker`.
- Progressive disclosure bridge: `tool_search`, `tool_describe`, `tool_call`.
- MCP tools: `mcp__<server>__<tool>` (prefix shape).

Third-party protocol and product names used by this area (keep exact; verify against vendor docs): Docker CLI (`docker run/exec/stop/rm/inspect`, `--cap-drop`, `--security-opt no-new-privileges`, `--pids-limit`, `--tmpfs`, `--storage-opt size=`, `--shm-size`, `--network=none`), Podman, s6-overlay (`/init`), OpenSSH (`ControlMaster=auto`, `ControlPersist=300`, `BatchMode=yes`, `StrictHostKeyChecking=accept-new`, `ConnectTimeout=10`, `SendEnv`), scp, tar, Apptainer/Singularity (`instance start --containall --no-home`, `--overlay`, `--writable-tmpfs`, SIF build), Modal SDK (`Sandbox.create`, `Sandbox.exec`, `snapshot_filesystem`), Daytona SDK, Vercel Sandbox SDK (snapshots, `vercel/sandbox/universal:latest`, OIDC token), Chrome DevTools Protocol, agent-browser CLI, Browserbase, Browser Use, Firecrawl, Camofox, Lightpanda, Playwright/Chromium, cua-driver, ripgrep, V4A patch format (`*** Begin Patch`), Model Context Protocol (stdio, Streamable HTTP, SSE; protocol version `2025-03-26`), JSON-RPC, web backends (Exa, Parallel, Firecrawl, Tavily, Perplexity, Keenable, SearXNG, Brave, DuckDuckGo/ddgs, xAI), TTS providers (Edge TTS, ElevenLabs, OpenAI, xAI, MiniMax, Mistral, Gemini, NeuTTS, KittenTTS, Piper), STT providers (faster-whisper, whisper.cpp, Groq, OpenAI, Mistral, xAI, ElevenLabs, DeepInfra), image/video backends (FAL, OpenAI, xAI, Krea, OpenRouter, DeepInfra), 1Password/Bitwarden (vault), systemd transient scopes, launchctl.

## 3. Behaviour

### 3.1 Tool registry, discovery and dispatch

- **Trigger / inputs:** process start imports the tool-definitions module, which runs discovery; plugins and MCP servers register later [obs].
- **Flow (discovery):**
  1. List every top-level module in the tools directory plus `<package>/tool.py` for package-shaped tools; sort the merged list so import order is independent of depth [obs].
  2. For each, decide by AST scan whether the module body (or a module-level `for` loop) contains a literal `registry.register(...)` call; a cheap text prefilter skips files lacking both words. Verdicts are memoised on disk keyed by (mtime_ns, size); the cache write is atomic and best-effort [obs]. A "safe worker" mode skips plugin-backed modules and the writable cache [obs].
  3. Import the matching modules; an import error in an optional tool is logged and does not stop the others [obs].
  4. Then MCP discovery and plugin discovery register more tools [obs].
- **Registration rules:** schema must be a dict and `parameters`, when present, must be a dict — rejected at registration with a named error so a bad plugin cannot 400 every request [obs]. Re-registering the same name in the same toolset overwrites silently (MCP reconnect). Registering a name that exists under a different toolset is rejected unless `override=true`; a plugin overriding a built-in additionally needs an operator opt-in per plugin; plugin registrations live in a per-profile overlay slot and cannot shadow a global tool without override [obs]. Each registration bumps a generation counter that invalidates toolset-resolution memos [obs].
- **Data shapes:** `ToolEntry { name, toolset, schema, handler, check_fn?, requires_env: string[], is_async, description, emoji, max_result_size_chars?: int|inf, dynamic_schema_overrides?: () -> partial schema, execution_timeout? }` [obs].
- **Availability (`check_fn`):** run when building the schema list. Result cached per (probe, profile scope) for 30 s, cache capped at 512 entries. If a probe fails within 60 s of its last success, the last-good "true" is served without caching (flake protection); after the grace the failure is honoured. A probe that raises counts as unavailable and is logged with traceback. When a previously-available core tool drops, one warning per probe per process. Probes can be marked uncached. When the browser-control identity is fully bound and the feature is on, both cache layers are bypassed; when profile identity cannot be resolved under multiplexing, caches are bypassed (fail closed) [obs].
- **Schema list (`get_definitions`):** sorted by name; skip unknown names and failed probes (one probe run per distinct probe per pass); emit `{type: "function", function: {schema + name}}`; then shallow-merge `dynamic_schema_overrides()` (used by `delegate_task` limits, `read_file` OCR wording, `patch` V4A mode, `text_to_speech`, `cronjob_manage`, `execute_code` per-session rebuild) [obs]. The per-tool `description=` metadata never reaches the model; only the schema description does [obs].
- **Dispatch:** look up entry; narrow injected kwargs (task_id, session_id, user_task, parent_agent, …) to what the handler signature declares unless it takes `**kwargs`; bridge async handlers to sync; normalise the result [obs].
  - Result contract: a string, or exactly `{ _multimodal: true, content: part[], text_summary: string }`. Anything else becomes an error with `error_type: "tool_result_contract"` [obs].
  - Error bodies: `{"error": "..."}`, error text capped at 2048 chars plus a truncation marker; logs keep up to 8192. Oversized `error` fields inside handler-built JSON are trimmed at the dispatch boundary [obs].
  - Exception text is sanitised (framing tokens/fences removed) and wrapped as "Tool execution failed: <Type>: <msg>" [obs]. The outer dispatcher in the agent loop adds a second wrapper "Error executing <tool>: …" (01 owns it) [obs].
  - Unknown tool → `{"error": "Unknown tool: <name>"}` [obs].
- **Agent-level tools:** `todo_list`, `memory`, `session_search`, `delegate_task` are intercepted by the agent loop before dispatch; their registry handlers exist for schema purposes and for fallback [obs].
- **Async bridging:** CLI path keeps one persistent event loop (so cached async clients survive); when a loop is already running (gateway) a throwaway thread runs the coroutine; worker threads get per-thread persistent loops [obs].
- **Errors and recovery:** discovery errors are per-module; probe errors make a tool unavailable for that turn only [obs].
- **Permissions / security:** probes MUST read credentials through the profile-scoped secret reader, never the raw process environment, because one process may serve several profiles [obs]. A tool's schema description MUST NOT mention tools belonging to a different toolset (cross-references are injected after filtering, e.g. `browser_navigate` and `execute_code` mention only tools that survived) [obs].

### 3.1a Argument repair and schema hygiene

- **Argument coercion (before dispatch):** schema-guided and conservative — string `"42"` to integer, `"true"`/`"false"` to boolean, JSON-encoded strings to arrays/objects (also nested), a bare scalar wrapped into a one-element list where an array is expected; the original is kept whenever the repair is ambiguous; local `$ref`s resolved [obs].
- **Schema sanitising (before sending to a provider):** on a deep copy, fix only shapes that particular backends reject: object schemas without properties, bare-string schemas and `type` arrays (llama.cpp grammar), nullable `anyOf` at the top level (Anthropic family), `default` next to `$ref` (Fireworks), top-level combinators (Codex); rewrite property keys outside `[A-Za-z0-9_.-]{1,64}` (one bad MCP key would 400 the whole request); lift legacy boolean `required` flags into the parent list; never treat literal defaults/consts as schemas [obs].
- **Misrouted arguments:** `terminal` given `code` and `execute_code` given `command` return errors naming the right tool; `write_file` missing `content` returns an error that explains dropped arguments under context pressure and suggests the in-script write for very large files [obs].
- **No length limits in schemas that break grammars:** e.g. clarify choices have no `maxLength` in the schema (llama.cpp rejects large values) and the limit is enforced in the handler instead [obs].

### 3.1b Path resolution for file tools

- Relative paths anchor on an absolute base derived from the task's terminal cwd — never the process cwd unless nothing else exists; placeholder cwd values (empty, `.`, `./`, `auto`, `cwd`) count as unset [obs].
- `~` expands to the effective profile home used for subprocesses, so gateway/cron runs agree with interactive runs [obs].
- Container backends use container paths; a host path that is the mounted workspace is translated; SSH remote-home detection is retried no sooner than every 30 s after a failure [obs].
- Host-only checks (stat, FIFO detection, realpath guards, V4A header rewriting) run only when the file ops target the host filesystem [obs].
- A warning is attached when the edited path diverges from the session workspace (e.g. editing the main checkout during a worktree session) [obs/inf].

### 3.2 Toolsets and resolution

- **Shape:** `Toolset { description, tools: name[], includes: toolset[], platforms?: set, posture?: bool }` [obs].
- **Core list:** one shared list used as the base of every platform bundle: web_search, web_extract, terminal, process_manage, read_file, write_file, patch, search_files, vision_analyze, image_generate, skills_list, skill_view, skill_manage, the ten core browser tools plus browser_cdp, browser_dialog, browser_vault, browser_exec, text_to_speech, todo_list, memory, session_search, clarify, execute_code, delegate_task, cronjob_manage, the 14 kanban tools, computer_use, manage_connections [obs]. Probes further gate kanban, computer_use, connectors [obs].
- **Named toolsets:** web, search, x_search, vision, video, image_gen, video_gen, computer_use, terminal, skills, browser, cronjob, file, tts, todo, memory, context_engine, session_search, connections, project, bot_room, desktop_ui, onboarding, catalog, clarify, code_execution, delegation, kanban, discord, discord_admin, yuanbao, feishu_doc, feishu_drive; scenario sets `debugging` (terminal + process + includes web, file) and `safe` (no terminal: includes web, vision, image_gen); posture set `coding` (core minus image_generate, text_to_speech, cronjob_manage, computer_use; per-session, never written back to config) [obs].
- **Platform bundles:** CLI, cron, Telegram, Discord (+discord, discord_admin), WhatsApp, Slack, Signal, BlueBubbles, Email, Mattermost, Matrix, DingTalk, Feishu (+5 feishu tools), Weixin, QQ, WeCom, WeCom-callback, Yuanbao (+5 yb tools), SMS all = core list (+extras). Editor/ACP = coding minus clarify. API server = core minus text_to_speech, clarify, computer_use. Webhook = only web_search, web_extract, vision_analyze, clarify (untrusted payloads get no execution). Gateway = union of all platform bundles [obs].
- **Resolution:** recursive include expansion with cycle/diamond tolerance (revisit returns empty), memoised per (name, include-registry flag, registry id, generation, profile scope), memo cleared past 256 entries. `all`/`*` spans every toolset except platform-gated ones (desktop-only `onboarding`, `catalog`) [obs]. Registry-registered tools in a static toolset are merged in; an MCP server named like a built-in toolset registers an alias whose tools are unioned [obs]. Disabled toolsets are a strict end-of-pipeline subtraction (hence web_search lives only in web/search, not browser) [obs]. Disabling a "core + extras" bundle removes only its non-core extras [obs]. Session-platform gating: a session whose platform is not in a toolset's `platforms` never receives those tools; desktop-only toolsets (`desktop_ui`, `project`, `catalog`) are added by the GUI client per session, never by an env var [obs].
- **Legacy names:** old `*_tools` toolset names map to modern ones [obs].

### 3.3 Progressive disclosure (tool search bridge)

- Deferrable tools (MCP/plugin tools plus a configured "cold" list) are removed from the model-visible array and replaced by three bridge tools `tool_search`, `tool_describe`, `tool_call` [obs]. Default deferred list: computer_use, session_search, image_generate, todo_list, process_manage, cronjob_manage, browser_vault, and all desktop GUI tools [obs].
- Modes: `enabled: auto|on|off` (auto = on when at least one deferrable tool exists). Listing budget = min(`threshold_pct` (default 5) % of context, a token cap). Tiers: everything eager; bridge + name/description manifest (degrading to names only); bare bridge + one line per server [obs].
- Limits: up to 7 queries per search call, up to 10 names per describe call [obs].
- Core tools and session-gated GUI toolsets never defer unless explicitly listed; the catalog is rebuilt statelessly from live definitions every assembly; bridge calls route through the normal dispatcher so approvals and hooks still apply [obs].

### 3.4 Result size budget and spill-to-disk

- **Per-result threshold:** default 100,000 chars; `read_file` pinned to infinity (its own pagination prevents persist→read→persist loops); MCP tools (prefix `mcp_`) default 50,000 (config `tool_budget.mcp_result_size_chars`); per-tool registry caps are clamped to the default [obs].
- **Per-turn aggregate budget:** 200,000 chars; preview size 1,500 chars [obs].
- **Context scaling:** with a known context length, per-result = clamp(15 % of window chars, 8,000 … 100,000), per-turn = clamp(30 % of window chars, 16,000 … 200,000) [obs].
- **Flow:**
  1. If a result exceeds its threshold, the full text (an MCP `{result, structuredContent?, _meta?}` envelope is unwrapped to its text with metadata appended in a delimited block, so pagination works) is written host-side to `<home>/cache/spillover/<sanitised call id>.txt` [obs].
  2. Host backends: the result is replaced by a "persisted output" block — tag, original size (chars and KB/MB), "full output saved to: <path>", advice to page with `read_file` offset/limit or process with `execute_code` and not to re-request, then a preview cut at the last newline after the halfway point of 1,500 chars [obs].
  3. Remote backends: if the sandbox can read the host spill path (mounted), reference that path plus the host path; else write the text into the sandbox temp dir via stdin (`cat >`; avoids the 128 KB argv limit) and verify the byte count with `wc -c`; a mismatch deletes the copy and fails closed; an unprobeable backend counts as success [obs].
  4. If nothing could be written, inline-truncate to the preview with a "could not be saved" note [obs].
  5. Turn budget: when the turn's total text exceeds the budget, spill the largest not-yet-spilled results first (for multimodal results only the largest text part; images stay) until under budget [obs].
- **Retention:** spill files live as long as the session retention window (`sessions.retention_days`, default 90 → hours), floored at 24 h; files still referenced by stored transcripts are kept; a once-per-home sweep prunes others [obs].
- **Head/tail truncation (shared algorithm):** 40 % head / 60 % tail around one marker naming the omitted count and the total; used by terminal, execute_code, MCP results [obs]. Defaults: terminal output 50,000 chars (`tool_output.max_bytes`), read pagination 2,000 lines, per-line 2,000 chars [obs].

### 3.5 Tool catalogue

Inputs are listed as `name: type (default)`. All results are JSON strings unless noted. "Gate" = availability probe or toolset condition.

**Terminal and processes**

| Tool | Inputs | Output / limits | Gate |
|---|---|---|---|
| terminal | command: string (required); background: bool (false); timeout: int ≥1 (config default 180; foreground cap 600); workdir: absolute path (this call only); pty: bool (background only, local only); notify: bool or string[] (background only; true = notify on exit, list = notify on line match); heartbeat: int seconds (0 = off; min 60; implies notify); persist_on_release: bool (background only). Hidden/legacy: notify_on_complete, watch_patterns | Foreground `{output, exit_code, error: null, cwd?, environment_recreated?, full_output_path?, output_total_chars?, verification_evidence?, approval?, exit_code_meaning?, hint?, sudo_auth_failed?, sudo_cache_cleared?}`. Background `{session_id, pid, …notes}`. Result cap 100,000 chars | backend requirements probe |
| process_manage | action: list\|poll\|log\|wait\|kill\|write\|submit\|close\|handoff; session_id (unique prefix accepted; required except list); data (stdin text, or purpose sentence for handoff); timeout (wait, ≥1); offset/limit (log; default last 200 lines) | per action; output redacted; see §3.12 | terminal toolset |

**Files**

| Tool | Inputs | Output / limits |
|---|---|---|
| read_file | path; offset ≥1 (1); limit ≤2000 (2000) | `LINE_NUM|CONTENT` lines; ~100,000-char budget (`file_read_max_chars`) truncates on a line boundary with `next_offset`; document extraction for .ipynb, Office (docx/xlsx/pptx + legacy), PDF (text layer; "scanned or text" when hosted OCR is configured), OpenDocument, RTF, EPUB, SQLite; images/binary refused |
| write_file | path; content | whole-file replace, parents created; refuses an existing file without a current full read/write baseline; `verified: true` after hash check; lint of new errors only |
| patch | path; old_string; new_string; replace_all (false). OpenAI-family models additionally see mode: replace\|patch and patch (V4A text); the handler accepts both shapes from any model | unified diff; 9-strategy fuzzy match; lint after edit |
| search_files | pattern; target: content\|files (content; legacy grep/find accepted); path (.); file_glob; limit (50); offset (0); order: discovery\|modified; output_mode: content\|files_only\|count; context (0) | ripgrep-backed; macOS skips privacy-protected home folders on broad searches; credential/env paths filtered out of hits |

**Web and search**

| Tool | Inputs | Output / limits | Gate |
|---|---|---|---|
| web_search | query; limit 1–100 (5) | `{success, data: {web: [{title, url, description, position}]}}`; results memoised per (provider, query, limit) | any configured web backend or keyless tier |
| web_extract | urls ≤5; char_limit ≥2000 (15,000) | `{results: [{url, title, content, error}]}`; per-page head+tail truncation with stored full text (≤2,000,000 chars kept); inline base64 images → `[IMAGE: alt]` | same |
| x_search | query; allowed_x_handles?; … | read-only public X search via xAI | xAI credential; off by default |

**Browser** (one session per task; refs `@eN` from accessibility snapshots)

| Tool | Inputs | Notes |
|---|---|---|
| browser_navigate | url | opens session, returns compact snapshot; secret-bearing URLs refused; SSRF/website policy; post-redirect re-check; bot-detection warning from title |
| browser_snapshot | full: bool (false) | over 15,000 chars truncated or LLM-summarised with full text saved (≤2,000,000) |
| browser_click | ref | |
| browser_type | ref; text | clears field first |
| browser_scroll | direction: up\|down | |
| browser_back | — | |
| browser_press | key | |
| browser_get_images | — | URLs, alt, natural size; data: URLs excluded |
| browser_vision | question; annotate: bool | screenshot attached natively on vision models, else auxiliary-model analysis; returns screenshot path |
| browser_console | clear: bool; expression? | console messages + JS errors; evaluate JS when expression given (eval policy checks URLs in code) |
| browser_cdp | method, params, timeout (30, max 300) | only when a CDP endpoint is reachable |
| browser_dialog | action: accept\|dismiss (+ prompt text) | only with CDP supervisor; pending dialogs appear in snapshot |
| browser_vault | action: list\|fill\|save_login\|enter_code\|unlock; handle; backend: onepassword\|bitwarden; label | model-blind autofill: secrets never returned; page origin must exactly match the item's origin; deferred by default |
| browser_exec | code (Python using pre-imported helpers); session name | replaces the other browser tools when the Browser Use backend is selected |

**Media**

| Tool | Inputs | Notes |
|---|---|---|
| vision_analyze | image_url (http/https, local path, or data URL); question; region?: [x1,y1,x2,y2] in original pixels | native multimodal result on vision models (embed budget, see §3.18), else auxiliary vision model text; download ≤50 MB |
| video_analyze | url or path; question | opt-in toolset `video` |
| image_generate | prompt; aspect_ratio; image_url? (edit); reference_image_urls?; backend-specific extras | model chosen by user config, not the agent; returns one URL or path |
| video_generate | prompt; image_url?; duration; aspect_ratio; resolution; … | description rebuilt from active backend capabilities; opt-in |
| xai_video_edit / xai_video_extend | video_url (prior result) + prompt | provider-specific |
| text_to_speech | text; output_path?; speed 0.25–4.0; instructions? | returns a `MEDIA:` path; provider caps auto-chunk (OpenAI 4096, xAI 15000, MiniMax 10000, ElevenLabs 5k–40k); Opus conversion for voice-bubble platforms |

**Orchestration and state**

| Tool | Inputs | Notes |
|---|---|---|
| todo_list | todos?: [{id, content, status: pending\|in_progress\|completed\|cancelled, parent?}]; merge: bool (false) | omit todos to read; always returns full list; ≤256 items, ≤4000 chars each |
| clarify | questions: 1–5 × {question, choices? (≤4, each ≤8000 chars), multi_select?} | `{responses: [{question, choices_offered, status: answered\|skipped\|unanswered, user_response}], outcome: submitted\|cancelled\|timed_out\|undelivered, notice?}`; first choice labelled recommended and the label stripped from answers |
| execute_code | code; reset: bool | see §3.19 |
| delegate_task | tasks: [{goal, context?, output_schema?, images? ≤8, group?}]; action: spawn\|list\|steer\|stop; subagent_id; message | see §3.20 |
| cronjob_manage | action: create\|list\|update\|pause\|resume\|run\|remove; job fields (prompt, schedule, skills, script, delivery…) | jobs run in fresh sessions; `run` fires in background |
| memory, session_search, skills_list, skill_view, skill_manage | (area 07) | memory actions add\|patch\|remove on targets memory\|user; session_search discovery/scroll/read/browse |
| kanban_* (15 registered; 14 in the core list) | task-scoped | only when spawned by the kanban dispatcher or the toolset is enabled; list/unblock are orchestrator-only (hidden from task workers); attachments ≤25 MB, http/https only for URL attach |
| manage_connections | action (display/status/connect/reconnect/rename; MCP install/enable/authorize); targets | waits up to 300 s for a card on GUI surfaces; returns connect URLs elsewhere |
| manage_catalog | search / install items | desktop only; approval card per item |
| computer_use | action (capture, click, double/right/middle click, drag, scroll, type, key, set_value, wait, list_apps, focus_app…); capture mode som\|vision\|ax | background desktop control via cua-driver; never steals cursor/focus |
| desktop GUI tools (11) + desktop_project + setup_beat | various | desktop sessions only; `set_theme` picker waits ≤5 min |
| discord / discord_admin | action enum + params (timeouts 60/1440/4320/10080 minutes) | Discord platform bundle only |
| feishu_* (5), yb_* (5) | platform-specific | platform bundles only |

**Not a model tool:** cross-platform "send message" exists as a library used by the CLI send command, cron delivery and the MCP server; no agent-callable send tool is registered (deliberate) [obs]. Speech-to-text is used by the messaging gateway and voice mode, not exposed as a tool [obs].

### 3.6 Terminal tool: call planning and guards

- **Trigger / inputs:** model calls `terminal`; also internal callers (control-plane children forced onto the host via a hidden flag; file ops; spill writes) [obs].
- **Argument normalisation (before planning):** a stray `code` argument → error pointing to `execute_code`. `notify`, `notify_on_complete`, `watch_patterns`, `pty`, `persist_on_release` on a foreground call → error naming the corrected call; a foreground `heartbeat` is silently zeroed (refusing it caused identical-retry loops). `heartbeat` must be a non-negative int. `notify: true` → notify-on-complete; `notify: [..]` → watch patterns; explicit `notify` wins over legacy flags; heartbeat forces notify-on-complete [obs].
- **Flow (`plan`):**
  1. Reject a non-string command [obs].
  2. Read terminal config (bridged from config file into process vars, profile-scoped per turn). Under an unresolved profile scope, refuse rather than use the launch profile's policy [obs].
  3. Compute env key (§3.8), per-task overrides (image, cwd; benchmark/RL rollouts), image per backend [obs].
  4. Cwd = override, else the session's recorded cwd, else config cwd. For container backends a host path or relative cwd is remapped to `/workspace` when the host dir is mounted, else to the config default [obs].
  5. Timeout ≤0 → error. Foreground: a lone trailing ` &` is stripped and the call becomes background (auto-background); other backgrounding shapes (inline `&`, `nohup`/`disown`/`setsid`, long-lived servers like dev servers, `docker compose up`, uvicorn, `python -m http.server`) → refusal with guidance, except help/version invocations; a foreground timeout above the cap (600 s) is promoted to a tracked background process with notify-on-complete and a "do not re-run" note (poll-only wording when the session cannot receive completions) [obs].
- **Acquire env:** start the idle-cleanup thread; look up cache; else take the per-key creation lock, re-check, create (disk-usage warning first for Singularity), store with activity timestamp. Creation raising ImportError → `{status: "disabled"}` [obs].
- **Pre-exec guards** run under a deadline = max(command timeout, 30 s) on a worker that stays interruptible; a guard that does not finish → refusal "not run, retry" (fail closed). Order: supervised-gateway lifecycle block (systemctl/launchctl/self-restart commands from inside the supervised gateway; launchctl submit/bootstrap always refused there; `force` cannot bypass) → workdir character allowlist (alphanumerics plus `/ \ : _ - . ~ space + @ = ,`; control chars rejected) → self-repo git guard (local backend; active on Windows only: blocks checkout/switch/rebase/merge/pull/restore/clean/cherry-pick/revert/bisect, hard/merge/keep reset, worktree move/remove, stash mutations, aliases and `sh -c` payloads, up to 4 levels of recursion, against the running source checkout) [obs].
- **Approval gate** (§3.13): `force` (internal, after user confirmation) skips it. Results: approved, approved-by-user (note "required approval … approved"), smart-approved (note), blocked (`status: "blocked"`, optional `user_summary`), or pending (`status: "pending_approval"` with command, description, pattern_key, allow_permanent) [obs].
- **PTY disable:** a command needing piped stdin/EOF (`gh auth login --with-token`) runs without PTY even if asked, with a reason note [obs].
- **Errors:** connection-class backend failures (SSH host down, Docker daemon unreachable) → degraded result `{output: "", exit_code: -1, status: "degraded", reason, retry_hint, error}` and the env is evicted so the next call recreates it; config `degraded_mode: fail` returns the error with traceback instead. Other exceptions → fatal error JSON (redacted) [obs].

### 3.7 Terminal tool: foreground execution and result

- **Flow:**
  1. If approved by a human, clear any interrupt bit that arrived during the wait — once, before retries; an interrupt during backoff must still abort [obs].
  2. Up to 3 retries on backend exceptions with backoff 2, 4, 8 s; an exception mentioning "timeout" returns exit 124 "timed out after N seconds" immediately [obs].
  3. `env.execute(command, timeout, cwd, bounded_capture=true, yield_handler?)` [obs].
  4. Yield: on the local backend, if a user message arrives mid-command, the live process is adopted as a background notify-on-complete session and the result says `status: "yielded_to_background"` with session id and a note to answer the user [obs].
  5. Finalise: record the observed cwd as the session cwd only if this command printed the marker and no per-call workdir was used; sudo failure annotations; plugin output-transform hook (first string result wins); head/tail truncation to 50,000; strip ANSI; command-aware secret redaction (env-dump commands get a KEY=value pass; source/config dumps skip it); exit-code meaning; failure hint; changed-cwd echo; spill metadata (spill file is redacted too) [obs].
- **Exit-code notes:** negative codes are signal deaths ("terminated by signal N: …", SIGINT excluded); codes >128 hedge with "usually means signal N" for known signals (e.g. 137 SIGKILL/OOM, 139 SIGSEGV, 141 SIGPIPE, 143 SIGTERM, 152 SIGXCPU, 153 SIGXFSZ). Informational non-zero codes by last pipeline segment's base command: grep/egrep/fgrep/rg/ag/ack 1 = no matches; diff/colordiff 1 = differ; test/[ 1 = false; find 1 = partial; curl 6/7/22/28; git 1 [obs].
- **Interrupt:** rc 130 is treated as an interrupt only when the executor's "[Command interrupted]" marker is present; an approved run then gets "…, then interrupted" [obs].
- **Masked success:** on rc 0 a hint may warn that a pipeline tail or `|| echo` hid an upstream failure when output shows strong failure indicators [obs].
- **Field order:** the foreground JSON keeps a stable key order — output, exit_code, error, then optional fields in this order: cwd, environment_recreated, spill metadata, verification_evidence, approval, exit_code_meaning, hint, sudo flags; absent optional fields are omitted, never null [obs].

### 3.7a Terminal result shapes (all outcomes)

| Outcome | Shape (conceptual) |
|---|---|
| foreground finished | `{ output, exit_code, error: null, cwd?, environment_recreated?, full_output_path?, output_total_chars?, verification_evidence?, approval?, exit_code_meaning?, hint?, sudo_auth_failed?, sudo_cache_cleared? }` |
| foreground timed out (backend exception path) | `{ output: "", exit_code: 124, error: "timed out after N seconds" }` |
| foreground deadline hit (normal path) | finished shape with exit_code 124 and the timeout line appended to output |
| yielded to background | `{ output (partial), exit_code: null, error: null, status: "yielded_to_background", session_id, pid, notify_on_complete: true, note }` |
| background started | `{ output: "Background process started", session_id, pid, exit_code: 0, error: null, persist_on_release?, approval?, pty_note?, auto_backgrounded? (note) | hint? (silent-background / CI-poller), watch_patterns_ignored?, notify_on_complete?, subagent_note?, heartbeat_seconds?, heartbeat_ignored?, watch_patterns?, promoted_from_foreground? }` |
| background failed to start | `{ output: "", exit_code: -1, error: "Failed to start background process: …" }` |
| rejected up front | `{ error, status: "error" }` or a plain error envelope |
| blocked by guard/approval | `{ output: "", exit_code: -1, error, status: "blocked", user_summary? }` |
| pending approval | `{ status: "pending_approval", approval_pending: true, command, description, pattern_key, smart_denied, allow_permanent }` |
| degraded backend | `{ output: "", exit_code: -1, status: "degraded", reason, retry_hint, error }` |
| backend disabled | `{ error, status: "disabled" }` |

Notes: inside a delegated child, notify-on-complete is turned off in the result and replaced by a note telling the child to wait, kill or hand off; heartbeat is armed only where completion delivery works, otherwise a `heartbeat_ignored` note is added [obs].

### 3.8 Environment cache, keys and lifecycle

- **Env key resolution** (first match wins) [obs]:
  1. Per-task image/backend overrides → their own key.
  2. Docker with `container_persistent: false` → per-session isolated key (plugin backends can declare the same).
  3. Docker persistent → one key per profile ("default" or `profile:<name>`), shared by every session of that profile.
  4. Otherwise a session-derived key; under multiplexed profiles the key is prefixed with the routed profile/home so profile B never reuses A's sandbox or cwd.
  5. Control-plane host-local calls use a separate `host-local-` key.
  - Delegated children may alias to the parent's container [obs].
- **Idle reaper:** a daemon thread wakes every 60 s; envs idle longer than `lifetime_seconds` (default 300) are unregistered under the lock then torn down outside it; any env whose task has a live background process has its activity refreshed (never reaped) [obs].
- **Exit:** stop the reaper (join 5 s), clean up all envs, wait up to 15 s per env for teardown threads, then drain Docker teardown workers detached earlier (15 s) [obs].
- **Docker orphan reaper:** once per process at startup, remove old labelled containers (configurable) [obs].
- **Persistence vs liveness:** persistent filesystems survive sandbox recreation; live processes and the same live sandbox are not guaranteed to survive cleanup, idle reaping or exit [obs].

### 3.9 Shared execution contract (all backends)

- **Model:** spawn-per-call `bash -c`; no long-lived interactive shell [obs].
- **Snapshot:** at env init, a login shell (`bash -l`, with configured shell-init files and optional `.bashrc` sourcing on local) dumps `export -p`, functions and aliases to `<tmp>/<snap>-<session>.sh` within 30 s (Modal 60 s). Success → later commands source the snapshot under non-login bash. If it fails, each command runs as its own `bash -l`; but when a ≤15 s check using a non-login shell reveals that the login shell is what's broken, commands use plain `bash -c` instead [obs].
  - Per-session bridged variables (session id, chat ids, cron, browser control, delegated-child marker, RPC/kernel vars) MUST be unset in a subshell before dumping, never grep-filtered, because a multi-line value would smuggle code into the next `source` [obs].
  - After each command the exported environment is re-dumped (unless persistent shell is off; then only the init-time snapshot is sourced and only cwd carries over) [obs].
- **Wrapper per command:** source snapshot → `cd` (tilde-preserving quoting) → command → re-dump env → print `\n<marker><pwd -P><marker>\n` [obs]. The marker line is stripped from output; `cwd_observed` set only when this command printed it [obs].
- **Stdin modes:** pipe (local, Docker, SSH, Singularity), payload (SDK backends: Modal streams it; Daytona/Vercel stage a temp file then the wrapper redirects `exec 0<file` and deletes it), heredoc (plugins only; delimiter randomised, trailing newline handled) [obs].
- **Compound background rewrite:** `A && B &` style commands are rewritten so the wrapper does not wait on the subshell [obs].
- **Sudo rewrite:** literal `sudo` invocations (incl. absolute paths and `env` prefixes with ordinary options) are rewritten to read a password from stdin when a cached/configured password exists and the backend lacks passwordless sudo (probe `sudo -n true` inside the backend, 3 s, fail closed) [obs]. `bash -c 'sudo …'`, `env -S`, dynamic paths are not rewritten [obs].
- **Wait loop:** adaptive poll 5 ms growing ×1.5 to 200 ms; checks the per-thread interrupt bit (and the original tool thread's bit when running on a deadline worker); on interrupt kill and return output + "[Command interrupted]" with rc 130 and an interrupted flag; on deadline kill and return partial output + "[Command timed out after Ns]" with rc 124 and a `timed_out` flag (distinguishes the app's deadline from a command's own `exit 124`); activity callback every 10 s keeps gateway inactivity timers alive; KeyboardInterrupt/SystemExit kills the child first [obs].
- **Hard backstop:** the whole spawn+wait runs under a wall-clock bound of timeout + 2 s driven by a daemon worker; on expiry the process tree is killed and partial output returned with rc 124 [obs].
- **Exit fence:** a process-wide fence prevents new foreground spawns once hard exit begins; foreground children in flight are tracked and force-killed (0.5 s budget) on hard exit [obs].
- **Bounded capture:** only the foreground terminal path caps retention (head 40 % / tail 60 % of `max_bytes`) and tees the full stream to `<home>/cache/terminal-output/out-<ts>-<pid>-<id>.log` (opened exclusively, private perms, created only on overflow, ≤5,000,000 chars; files older than 7 days pruned). Internal callers (file-op reads, RPC, log reads) MUST use unbounded capture or data is corrupted [obs].
- **Stdin writer:** runs on its own thread; an encode failure is reported as `stdin_error` [obs].
- **Recreated flag:** if a backend had to replace its container mid-command, the result carries `environment_recreated` and the tool adds a warning that background processes and unpersisted files may be gone [obs].
- **File fetch (backend → host):** the sandbox side truncates the file with `head -c max+1`, and the bytes travel base64-encoded through the command-execution channel; 300 s timeout [obs].
- **Result shape:** `ExecResult { output, returncode, cwd_observed?, cwd?, timed_out?, interrupted?, full_output_path?, output_total_chars?, stdin_error?, environment_recreated?, yielded_session_id?, pid? }` [obs].

### 3.10 The seven backends

| Backend | Create | Exec transport | Persistence | Cleanup | Prompts skipped? |
|---|---|---|---|---|---|
| local | resolve safe cwd; snapshot | `bash -c` in new session/process group, env from the scrubbing builder | host FS | kill process group (TERM, wait, KILL, sweep setsid escapees; PID-reuse guard via leader start time) | no |
| docker | one long-lived container (`run -d … sleep`), labels, optional cross-process reattach | `docker exec [-i] … bash -c` | bind dirs under `<home>/sandboxes/docker/<task>/{home,workspace}` when persistent, else tmpfs | persistent: leave running (reaped next startup); otherwise `stop -t 10` + `rm -f` on a thread | yes, unless host paths are bind-mounted |
| ssh | ControlMaster connection, detect remote home, sync files, snapshot | `ssh … bash -c` | remote host FS | sync back? (no; upload-oriented) close control sockets (5 s) | no |
| singularity | build/cache SIF from `docker://` (≤600 s), `instance start --containall --no-home` (≤120 s) | `exec instance://<id> bash -c` | overlay dir per task under scratch when persistent, else writable tmpfs | `instance stop` (30 s); record overlay path | yes |
| modal (direct) | `Sandbox.create` (timeout 3600 s, create ≤300 s), restore from stored snapshot if any (fallback to base image, delete bad snapshot) | SDK exec on a private async worker; cancel = terminate | filesystem snapshot on cleanup (no TTL) stored per task | snapshot (≤60 s), terminate (≤15 s) | yes |
| modal (managed gateway) | HTTP create (cpu, memory, timeout 3,600,000 ms, idle ≥300,000 ms) | HTTP exec + poll; client deadline = timeout + 10 s; cancel on timeout | server-side snapshot before terminate when persistent | HTTP terminate (60 s) | yes |
| daytona | get-by-name `<prefix>-<task>` and start, else legacy label lookup, else create (auto-stop disabled); disk capped at 10 GB | SDK `process.exec` via threaded handle; cancel = stop; shell timeout wrapper kept | stop (FS kept) when persistent, else delete | sync back, then stop/delete | yes |
| vercel_sandbox | create from stored snapshot or image (`vercel/sandbox/universal:latest`), retries 3 on 408/425/429/5xx, wait running ≤30 s; sandbox timeout ≥5 min | SDK exec; payload stdin staged | snapshot per task when persistent (no live processes) | sync back, snapshot, stop (≤15 s, poll 500 ms) | yes |

Details per backend:

- **Local** [obs]:
  - Temp dir precedence: configured terminal temp dir, then TMPDIR/TMP/TEMP, then `<home>/cache/terminal` (real disk; idle files pruned after 24 h by a once-per-process sweep and gateway housekeeping), then the system temp dir. Windows always uses the home cache dir with forward slashes.
  - Shell: bash found on PATH (Git Bash on Windows; MSYS path conversion both ways). A sane PATH is appended (Homebrew, system dirs, user-local bins, managed runtimes) and the app's own bin dir prepended.
  - Env: built by the single subprocess-env builder (§3.14). Launch-profile residue and app-owned `PYTHONPATH` entries (proven by path provenance) are stripped so child Pythons of another version don't load the app's C extensions.
  - Persistent shell knob: on by default; off = only cwd persists.
  - PTY only here (background). Yield-to-background only here.
- **Docker** [obs]:
  - Security flags always: drop all caps, add back DAC_OVERRIDE, CHOWN, FOWNER; `no-new-privileges` (dropped under snap-packaged Docker compatibility mode); `/tmp` tmpfs 512 MB nosuid exec; `/var/tmp` tmpfs 256 MB noexec nosuid; `/run` tmpfs 64 MB noexec (exec when the image's entrypoint is s6-overlay `/init`, which also skips `--init`); SETUID/SETGID added back unless running as the host user.
  - Resources (only when cgroup controllers exist): `--cpus`, `--memory <MB>m`, `--pids-limit 2048`. `--shm-size 1g` unless set in extra args or configured empty/"0". `--storage-opt size=<MB>m` only when overlay2-on-XFS-pquota probe passes and not macOS; else warn and run without quota.
  - Network: `docker_network: false` → `--network=none`; an extra-args network flag that contradicts it → refuse to start.
  - Mounts: user volumes (`host:container[:opts]`; malformed skipped); host cwd bound at `/workspace` when auto-mount is on or the cwd is a Windows drive path (else at `/host-cwd`, `-2`, `-3` if `/workspace` is taken; reuse an existing mount of the same dir); skills and credential files mounted read-only; non-persistent mode uses tmpfs `/workspace` 10 GB, `/home` 1 GB, `/root` 1 GB.
  - Env: forwarded names and explicit env go as name-only `-e NAME` flags with values in the docker client's own environment (never on the command line, to stay out of world-readable `/proc/*/cmdline`); profile-scoped passthrough values re-injected per command because one container may serve several profiles.
  - Optional egress credential proxy: CA mount + proxy env; under enforcement, forwarded env, docker env or extra args that override proxy/CA variables cause refusal; a container created without the proxy is never reused with it (label).
  - Reuse: labels (agent marker, task id, profile, egress posture, an environment fingerprint of image + mounts + home unless a shared key is set); attach to a running or stopped container with matching labels.
  - Recovery: output containing "No such container" / "is not running" → find a reusable labelled container or create a fresh one, re-snapshot, mark recreated, retry the command once (persist-across-processes only).
  - Default image: the reference's own desktop sandbox image (see §2).
- **SSH** [obs]:
  - Client flags: ControlMaster auto, ControlPersist 300, BatchMode, accept-new host keys, ConnectTimeout 10, `-p`/`-i` as configured. Control socket name = first 16 hex of SHA-256 of `user@host:port` (macOS 104-byte socket path limit); one extra master per distinct SendEnv name-set (a mux master forwards only names it started with); no multiplexing on Windows.
  - Passthrough values ride the ssh client's environment with `SendEnv` names only.
  - Connect probe `echo` (15 s), remote home detection (10 s), dir creation (10 s), scp single upload (30 s), tar-over-ssh bulk upload (120 s), bulk download (120 s).
  - Default cwd `~`; persistent shell knob separate.
- **Singularity/Apptainer** [obs]: executable discovery, SIF cache, CPU/memory flags, credential/skill dirs bound read-only; overlay path recorded per task in a JSON store under home.
- **Modal** [obs]: modes auto/direct/managed (managed requires the vendor-hosted gateway subscription; direct requires Modal credentials). Uploads via base64 stdin (single 30 s; bulk tar 120 s), download `tar cf - root/.<app>` (120 s), deletes 15 s. Snapshot ids stored under a namespaced key per task with legacy-key migration.
- **Daytona** [obs]: resources cpu, memory GiB (ceil MB/1024, min 1), disk GiB (min 1, max 10); home detected with `echo $HOME`; default cwd follows home.
- **Vercel Sandbox** [obs]: auth by access token + project id + team id (deployments) or short-lived OIDC token (local dev); `container_disk` must be default (51200) or 0 else creation fails; deprecated "runtime" pins (node24, node22, python3.13) override image; workspace root `/vercel/sandbox`; write retries 3 with 100 ms backoff step. Background commands use the generic non-local flow; no detached-process recovery after cleanup/restart.
- **Container defaults** (all container backends): cpu 1, memory 5120 MB, disk 51200 MB, persistent true [obs].
- **Backend requirement probes** gate the `terminal` tool's availability and record a human-readable reason shown at startup/doctor [obs].
- **Modal mode selection:** `auto` prefers the managed gateway when it is enabled and ready, else direct credentials; `direct` and `managed` are exclusive and fail when their prerequisite is missing; managed requested without the subscription is reported as blocked [obs].

### 3.10a Terminal configuration surface (rebuild keys, reference defaults)

| Setting | Default | Meaning |
|---|---|---|
| backend | local | one of the seven built-ins or a plugin backend name |
| cwd | "." | starting directory (sanitised for containers) |
| timeout | 180 s | default per-command timeout |
| foreground timeout cap | 600 s | above it the call is promoted to background |
| lifetime | 300 s | idle reap threshold |
| degraded mode | warn | warn = structured degraded result; fail = error with traceback |
| persistent shell (local, SSH) | true | exported vars carry across commands |
| temp dir | empty | empty = TMPDIR/TMP/TEMP, else `<home>/cache/terminal` |
| disk warning | 500 GB | Singularity scratch usage warning threshold |
| daemon term grace | 2.0 s | SIGTERM→SIGKILL gap for host process trees |
| one-shot completion wait | 600 s | how long a one-shot run lingers for notify-on-complete jobs |
| process wait auto-notify | true | two unchanged wait/poll calls switch to notify-on-complete |
| env passthrough | [] | extra variable names forwarded into sandboxes |
| credential files | [] | host files mounted/synced into sandboxes |
| sync-back max | 2 GiB | refuse larger sandbox archives |
| container cpu / memory / disk / persistent | 1 / 5120 MB / 51200 MB / true | container backends |
| docker image, forward env, env, volumes, extra args | sandbox image / [] / {} / [] / [] | Docker |
| docker mount cwd to workspace | false | binding the host cwd weakens isolation (and re-enables prompts) |
| docker network | true | false = no network |
| docker shm size | 1g | empty or "0" omits the flag |
| docker run as host user | false | passes uid:gid, drops SETUID/SETGID add-back |
| docker persist across processes / shared key / orphan reaper / snap compat / image pinned | true / "" / true / false / false | Docker lifecycle |
| ssh host / user / port / key | "" / "" / 22 / "" | SSH |
| modal mode | auto | auto/direct/managed |
| vercel image / runtime | universal image / "" | Vercel |

### 3.10b Plugin backends

- A plugin backend registers a provider object; the core consults provider flags instead of hard-coded backend names at each classification site: dispatch (`create_environment`), setup picker text, cheap probe, doctor checks, remote/env description for the system prompt, whether to skip dangerous-command prompts, whether paths are container paths, cache path base for synced files, extra env keys to strip from every child, and per-session isolation when non-persistent [obs].
- Rules: built-in names are reserved; the factory must accept and ignore unknown keyword arguments; probes must not touch the network; a raising provider attribute counts as its safe default (e.g. prompts stay on); vendor tokens must be declared for stripping [obs].
- Contract: an object with `execute(command, timeout?, …)` and `cleanup()`; subclassing the base is recommended to inherit snapshot, cwd, sync and background plumbing [obs].

### 3.11 Remote file sync

- Used by SSH, Modal, Daytona, Vercel (Docker and Singularity use bind mounts) [obs].
- What syncs: credential files (skill-declared + configured), skills dirs, selected cache dirs; credential paths remapped to the remote home's app dir [obs].
- Push: before each command at most every 5 s (forceable by an env flag), detect changes via (mtime, size) with a racy-mtime sentinel forcing content check, upload changed, delete removed, transactionally [obs].
- Sync back (on cleanup): download the remote app dir as tar, apply only files whose SHA-256 differs from what was pushed; credential files are upload-only; refuse archives over 2 GiB (configurable); 3 attempts with 2/4/8 s backoff; SIGINT deferred; serialised across processes by a file lock in home; skipped when nothing was ever pushed; stale temp entries (>30 min, dead owner) cleaned [obs].

### 3.12 Background processes and `process_manage`

- **Spawn (local):** host process (optionally PTY) with a reader thread draining into a rolling 200,000-char buffer; on Linux optionally placed in a systemd transient scope with a memory cap (default 1 GiB, min 64 MiB, max 4 GiB) [obs/inf].
- **Spawn (non-local):** wrapper `mkdir -p <tmp> && ( nohup bash -lc <cmd> > log 2>&1; echo rc > exit ) & echo $! > pid` via `env.execute` (10 s); no PID → `failed_start`. Later polls read the log/exit files [obs].
- **Ids:** `proc_` + 12 hex; any unique prefix accepted [obs].
- **Registry limits:** ≤64 tracked processes (LRU pruning), finished kept 30 min in memory, completion notice carries a 2,000-char tail (callers may ask for more) [obs].
- **Notifications:** notify-on-complete → a completion message that starts a new agent turn (where the surface can receive it). Watch patterns: ≤1 notification per 15 s per process; a dropped match is a strike; 3 consecutive strike windows or 8 lifetime deliveries disable watching and fall back to notify-on-complete; global breaker 15 per 10 s across processes then 30 s cooldown. Heartbeat: every N ≥60 s, output since last tick (≤2,000 chars), quiet ticks skipped, rendered as a hidden wake; one daemon thread ticks every 5 s for all processes. notify + watch → watch dropped with a note [obs].
- **Silent-background hint:** background without notify gets a hint suggesting notify for bounded jobs; a narrow detector adds a hint for hand-rolled CI pollers [obs].
- **Sessions that cannot receive completions** (one-shot runs, cron, kanban workers, stateless API) → notify flags cleared with a note to poll [obs].
- **Subagent processes:** killed at the child's teardown, notices suppressed in the parent; `handoff` (children only, ≤3 per child, purpose sentence required) moves ownership over to the parent while holding the registry lock; un-handed leftovers reported as orphaned/unread on the child's result [obs].
- **persist_on_release:** survives session end, /new, compression, error recovery, max-iteration stop; not host exit [obs].
- **Actions:** list (incl. retained results for the current conversation), poll (status + new output), log (paged lines; default last 200), wait (block until exit/timeout/interrupt; default and max = terminal timeout 180; non-positive rejected; partial output on timeout), kill, write (raw bytes), submit (adds Enter; matters on Windows PTY), close (EOF), handoff [obs].
- **Per-action results** [obs]:
  - poll: `{status head, pid, uptime_seconds, output_preview (last 1,000 chars), exit fields when exited, detached?/note when recovered after restart without output history}`. A poll does not "consume" the completion, so the autonomous completion turn still fires.
  - log: `{status head, output (selected lines, ANSI stripped), total_lines, showing: "N lines"}`; no offset → last `limit` lines; an explicit offset 0 means the head (not the tail). Reading through the end of an exited process's log marks its completion as consumed.
  - wait: exited → exit snapshot (completion-sized tail) and completion consumed; user interrupt → `status: interrupted` with tail and note; a steer/yield request → `status: interrupted`, `process_running: true`, note to answer the user; window elapsed → `status: timeout`, `process_running: true`, tail, and a note stating this is not an error (models re-issued identical waits when it read like a failure) plus advice depending on notify-on-complete; waits sleep on a completion event in ≤1 s slices.
  - unknown id → not-found result; kill → output snapshot; write/submit/close → status only (no output, so no redaction pass).
- **Auto-notify on repeated waits:** two consecutive wait/poll calls on a still-running process with no new output turn on notify-on-complete and tell the model to stop polling (config `process_wait_auto_notify`, default on; tracked ≤512 processes; only where completions can be delivered) [obs].
- **Teardown order:** snapshot descendants, SIGTERM only the parent, wait `daemon_term_grace_seconds` (2.0), SIGTERM survivors, SIGKILL the rest; systemd scope stop only after the PID kill or for a proven-dead parent [obs].
- **Crash recovery:** running-process checkpoint `<home>/processes.json` (gateway) with PID-safe adoption [obs].
- **Retained results:** atomic, redacted receipt per completed process under `<home>/logs/process-results/`; newest 64, ≤7 days, ≤200,000-char tail; readable only from the owning conversation or its compressed continuation (a handle alone is not authority); receipt persisted before the completion event fires; no re-run or replay [obs].
- **Output pipeline:** transform hook then redaction on every output-bearing action and notification; the command string itself is redacted [obs].

### 3.13 Dangerous-command approval

- **Entry points:** terminal commands (full guard), `execute_code` scripts (whole-script one-shot approval), and a generic per-tool approval used by other tools [obs].
- **Order for a terminal command** [obs]:
  1. Isolated container backend (singularity, modal, daytona, vercel_sandbox, Docker without host bind mounts, plugin backends that declare it) → only user deny globs apply; otherwise approved.
  2. Floors (never bypassable): hardline patterns; deleting the interpreter/venv the app runs from; `sudo -S` password guessing when no password is configured; user deny globs. A command whose quoting cannot be parsed fails closed ("malformed executable payload").
  3. Pre-approved batch decision for this exact command (agent-side batching).
  4. Yolo (session toggle or env) or `mode: off` → approved.
  5. Permanent allowlist match → approved.
  6. No interactive surface (not CLI, not gateway, not ask-mode): apply the unattended policy for the context — cron (`cron_mode`), single-query (`single_query_mode`), webhook/API platforms (`unattended_mode`) — each `deny` (default) or `approve`; otherwise approve (local non-interactive non-gateway contract).
  7. Pattern detection; not dangerous, or this pattern already approved for the session → approved.
  8. Human decision (with optional smart pre-judge).
- **Hardline set (examples)** [obs]: recursive delete of root/system dirs/home; mkfs; dd or redirect to raw block device; fork bomb; kill all processes; shutdown/reboot/init 0/6/telinit/systemctl poweroff.
- **Dangerous set (categories)** [obs]: recursive/force deletes (many flag orders, Windows cmd/PowerShell variants); world-writable chmod, recursive chown to root; disk copy/format/partition/wipe; SQL DROP/TRUNCATE/DELETE without WHERE; overwriting system config (tee, redirect, cp/mv, in-place sed/perl/ruby edits) and project `.env`/`config.yaml`, SSH dir, shell rc and credential files, and the app's own config/env; service stop/restart; kill -9/killall/pkill variants incl. self-termination via pgrep expansion; curl/wget piped to shell, process substitution, decode-and-pipe obfuscation (base64/xxd/tr/openssl), credential file piped into an upload; cloud metadata endpoint access; xargs/find with rm, find -delete; heredoc into a shell; git reset --hard, force push, clean -f, branch -D; `chmod +x` then execute; sudo privilege flags; package uninstalls; docker/podman remote-daemon redirects and container lifecycle commands; restarting/updating the app's own gateway service; PowerShell encoded commands, registry/service/ACL/boot config edits.
- **Normalisation before matching** [obs]: lowercase except flags; fold literal user home and app home paths into `~`/variable spellings; resolve simple `${x:-default}` and replacement expansions; strip/deobfuscate quoting tricks; mask quoted prose for certain patterns; walk command wrappers (sudo, env, exec, nohup, setsid, time, command, builtin, nice, timeout, stdbuf, ionice, chrt, taskset, chroot) with their option arities; recurse into interpreter `-c`/`-e` payloads (python, node, perl, ruby, php, powershell, bun, deno) and shell `-c`; a parser-limit overflow is itself flagged.
- **Modes:** `smart` (config default), `manual`, `off`. Smart: an auxiliary model (temperature 0, max 16 tokens, task-specific timeout) sees the command with shell comments stripped inside delimiters and a system message telling it to ignore embedded instructions; replies APPROVE / DENY / ESCALATE; approve → run once (nothing persisted); deny → blocked, counted toward a breaker (default 3 consecutive; 0 disables; ≤256 sessions tracked) after which the denial text escalates to a hard stop; escalate or any unparseable reply → human. A smart-denied command shown to a human only offers once/deny [obs].
- **Human choices:** once, session (pattern key approved for the session), always (written to the permanent allowlist in config, per profile), deny; plus timeout and cancelled outcomes [obs].
  - CLI: synchronous prompt (callback panel or input fallback) within `approvals.timeout` (default 300 s; clamped to a platform-safe maximum); timeout and undeliverable prompts are distinct from deny and do not trip the breaker; pre/post approval plugin hooks fire [obs].
  - Gateway/messaging: the agent thread enqueues a pending entry, a per-session notifier paints a card with redacted command/description, and the thread blocks on its own event until `/approve` (oldest), `/approve all`, `/deny [reason]`, timeout or withdrawal; each concurrent waiter has its own event; the deny reason is relayed verbatim; silence is not consent; a withdrawn prompt is reported as "not answered", not as a refusal [obs].
  - No notifier but ask-mode/gateway flags present → paint the local CLI panel if possible, else return a `pending_approval` result [obs].
  - Operator-selected plugin transport may present the question instead [obs].
- **Human-wait accounting:** time verifiably spent waiting on a human is excluded from the agent's concurrent-batch deadline, per session (bounded map) [obs].
- **Yolo persistence:** per session, stored with the session (CLI/TUI/desktop on the session row; messaging on the routing entry); `/new` and resume on messaging start with approvals on [obs].
- **execute_code guard:** a script is approved as a whole in gateway/ask contexts (it can bypass terminal patterns via subprocess); a local non-interactive session auto-approves; hardline still blocks catastrophic `terminal()` calls from inside the script [obs].
- **Data shapes:** `ApprovalDecision { approved, message?, status?: blocked|pending_approval, pattern_key?, description?, user_approved?, smart_approved?, smart_denied?, allow_permanent?, user_summary?, deny_reason? }` [obs].

### 3.14 Child-process environment and secret hygiene

- **One builder** produces the environment for every child the app spawns (terminal, execute_code, background processes, delegation children, editor-protocol children, MCP stdio servers) [obs]. A variant for children acting for a served profile drops launch-profile residue, pins that profile's home, and overlays only its own secrets [obs].
- **Blocklist:** a static list of provider and platform credentials/config (LLM keys and base URLs, web backend keys, platform home channels/allowlists, email creds, GitHub tokens, Modal/Daytona tokens, gateway relay secrets, dashboard tokens …) plus every secret declared by platform adapters and plugins; a `_FORCE_`-prefixed name lets a caller force one through; the general AWS chain is intentionally inheritable (local terminal is the operator's shell) except the Bedrock bearer token [obs].
- **Passthrough:** names from `terminal.env_passthrough` and skill-declared required variables are forwarded into sandboxed children; values resolve through the profile secret scope; a blocklisted name can never be re-allowed via passthrough [obs].
- **execute_code child env:** drop any name containing KEY, TOKEN, SECRET, PASSWORD, CREDENTIAL, PASSWD, AUTH, DSN, WEBHOOK, CREDS, BEARER, APIKEY, COOKIE; keep only names with safe prefixes (PATH, HOME, USER, LANG, LC_, TERM, TMPDIR, TMP, TEMP, SHELL, LOGNAME, XDG_, PYTHONPATH, VIRTUAL_ENV, CONDA), an exact allowlist of app vars, and Windows essentials; then inject RPC vars, TZ, HOME; force UTF-8 [obs].
- **MCP stdio env:** only PATH, HOME, USER, LANG, LC_ALL, TERM, SHELL, TMPDIR (+Windows equivalents) plus configured values with `${VAR}` interpolation [obs].
- **Browser driver env:** credentials stripped, then only browser-backend keys re-added from the profile scope [obs].
- **Output redaction:** terminal and process outputs, file reads (secret-file aware) and error texts are passed through a redactor; receipts and spill files are force-redacted even when live redaction is disabled [obs].

### 3.15 File tools

- **Execution:** file operations run as shell commands inside the task's terminal environment (so they see the sandbox FS), using sentinel-delimited reads and stdin-delivered writes; an in-process fast path exists for host paths [obs/inf]. Per-task file-ops objects are cached and cleared on env teardown [obs].
- **read_file guard order** [obs]: Windows NT/device namespace prefixes on the raw string (`\??\`, `\\.\`, `\\?\UNC\`, `\\?\GLOBALROOT`) → device blocklist (`/dev/zero`, `/dev/random`, `/dev/urandom`, `/dev/full`, `/dev/stdin`, `/dev/tty`, `/dev/console`, `/dev/stdout`, `/dev/stderr`, `/dev/fd/0-2`, and `/proc/<pid>/` environ, cmdline, maps, smaps, smaps_rollup, numa_maps, mem, auxv, pagemap, fd/0-2) checked through every symlink hop → FIFO/socket refusal (host only) → internal read denylist (app secret stores, auth/config control files, project `.env*`/`.envrc`) → document extraction → binary-extension refusal (pointing to vision) → negative cache (not-found remembered 60 s) → dedup → real read.
- **Read results:** content with line numbers, `total_lines`, `file_size`, `truncated`, `truncated_by: bytes`, `next_offset`, hint; UTF-16 auto-transcoded (BOM or heuristic) with a disclosure; files over 10 MB and true binaries refused; merge-conflict blocks counted with a hint; files over 512,000 bytes read with limit >200 get a "read a section" hint; "similar filenames" suggestions on not-found [obs].
- **Document extraction limits:** documents ≤50 MB; xlsx ≤5,000 rows/sheet and ≤256 columns; notebook code-cell output ≤20,000 chars; PDF page empty under 20 chars, page scan timeout 20 s, gap map ≤20 entries; SQLite schema + row counts + first 5 rows, ≤200 tables, cells ≤80 chars [obs].
- **Dedup and loop breaking:** an identical (path, offset, limit) read of an unchanged file returns a small "unchanged" stub (first read after a compaction boundary still returns full content); the second stub becomes a BLOCKED refusal; consecutive identical reads warn at 3 and block at 4. Refusals carry a marker so failure classifiers do not count them as errors [obs]. Tracker caps: 500 history, 1,000 dedup/timestamps/baselines, 64 patch-failure paths, 256 coverage ranges per file [obs].
- **write_file / patch guards (in order)** [obs]: protected system/credential paths (after realpath: `/etc/`, `/boot/`, `/usr/lib/systemd/`, macOS private etc/var-db/var-root, Docker sockets; every home a write can land in: `.ssh` (except `~/.ssh/config`, which is approval-gated), `.aws`, `.kube`, sudoers, `.netrc`; app secret stores) → binary document writes → protected instruction files (AGENTS.md, CLAUDE.md, SOUL.md, .cursorrules, case-insensitive, plus configured globs) always ask → general approval-required writes → sandbox-mirror lost-write guard (bypass flag `cross_profile`, unadvertised) → refuse content that is the tool's own status text or line-numbered read output. Optional safe-root env restricts writes to listed prefixes.
- **Stale-overwrite refusal (write_file only):** under a per-path lock, refuse to replace an existing file unless the task has a current full-file baseline (full unredacted read of all pages, its own successful write, or its own patch after a full read) and the file is unchanged on disk; refusal leaves the file untouched and tells the model to read, merge, retry. Terminal/execute_code writes never establish a baseline [obs].
- **Whole-file rewrite hint:** when ≥80 % of a ≥20,000-char (≤400,000) rewrite is unchanged, suggest `patch` [obs].
- **Patch matching:** strategies in order — exact, line-trimmed, whitespace-normalised, indentation-flexible, escape-normalised, trimmed-boundary, unicode-normalised (smart quotes, dashes, ellipsis, NBSP, minus, Zs spaces), block-anchor, context-aware similarity. Identical old/new rejected; non-unique match without replace_all rejected with up to 5 match locations; "already applied" detection; newline-literal and escape-drift detection with a conservative autocorrect that requires the corrected text to exist verbatim [obs].
- **V4A patches:** Begin/End Patch envelope with Update (hunks with `@@` hint, context, -/+), Add, Delete, Move operations across files [obs].
- **Lint after edit:** in-process for .py/.json/.yaml/.toml (fail-closed for json/yaml/toml), shell linters for others (node --check, tsc …), plus LSP diagnostics delivered late on the session's next tool result; only errors introduced by the edit are surfaced [obs].
- **Cross-agent file state:** warns when another agent in the process wrote a file this one read [obs].
- **Checkpoints:** not a tool. Before the first file mutation per turn the working dir is snapshotted into one shared shadow git store under `<home>/checkpoints/` (per-project ref and index; ≤50,000 files; ledger ≤2,000 entries; prune orphans/stale + gc + size cap) to support rollback [obs].
- **search_files:** ripgrep content search with output modes; file search by glob in bounded discovery order or exact newest-first (`modified`, may scan the whole tree); concurrent filename searches admitted one at a time (50 ms wait slices, interruptible); timeouts surface as a marker; regex `\n` escapes handled [obs].

### 3.16 Web tools, SSRF and website policy

- **Backend selection:** `web.backend` or per-capability `search_backend`/`extract_backend`; built-in availability probes per vendor (Exa, Parallel, Keenable, Firecrawl, Tavily, Perplexity incl. managed, SearXNG URL, Brave free, ddgs package, xAI); vendor code lives in plugins registered with a web-provider registry; an explicitly selected but missing provider is an explicit error, not a silent fallback [obs].
- **web_search flow:** clamp limit 1–100; interruptible; memo lookup with a per-key single-flight lock; on a failed keyed call a one-shot "keyless rescue" through a free tier (config `web.keyless_rescue`, default on); rescued responses are never cached [obs]. Memo buckets limits 10/20/50/100, TTL 20 min, index ≤500 entries [obs].
- **web_extract flow:** validate and normalise URLs (IRI→URI, IDNA host, whitespace repair), refuse URLs carrying secret-looking query params before any fetch, SSRF-check each URL (blocked entries get per-URL error results), extract via provider (timeout 120 s), restore input order, head+tail truncate per page to the char limit with a footer naming the stored full-text path and the `read_file` call, convert base64 images to placeholders, and fail with "inaccessible" when nothing came back [obs].
- **SSRF policy:** block private/loopback/link-local/CGNAT and other non-public ranges after DNS resolution (up to 8 IPs per connect), always block cloud metadata hostnames/IPs and 169.254.0.0/16 even when `allow_private_urls` is on; honour declared fake-ip ranges of local TUN proxies (some ranges can never be declared); defer DNS to a configured proxy; app-owned HTTP clients re-check at TCP connect (dial the validated IP, preserve Host/SNI) and re-validate every redirect hop [obs].
- **Website blocklist:** `security.website_blocklist` {enabled, domains, shared_files} with host/glob matching, cached 30 s; checked by URL-capable tools [obs].

### 3.17 Browser

- **Backends:** local headless Chromium via the agent-browser CLI (Node), cloud (Browser Use, Browserbase, Firecrawl), user CDP endpoint, Camofox, Lightpanda (with Chrome fallback for screenshots), a real-profile mode, and the Browser Use CLI alternative (`browser_exec`) [obs].
- **Sessions:** per task; inactivity timeout default 120 s (floor 30 s); janitor reaps idle sessions, force-reaps after 3 consecutive cleanup failures; orphan daemon reaper every 300 s with grace max(3600 s, 20 × inactivity); suspect sessions (after a command timeout) recycled on next use; teardown re-enters the owning profile scope [obs].
- **Timeouts:** command default 30 s; open ≥60 s; first open ≥120 s [obs].
- **Navigation safety:** secret-URL refusal, SSRF/website policy (relaxed when hybrid routing sends a private URL to a local sidecar instead of the cloud provider), post-redirect block, bot-detection warning from page-title patterns [obs].
- **Snapshots:** compact (interactive elements with refs) or full; threshold 15,000 chars (min 1,000); oversize → truncate or summarise, full text saved [obs].
- **Driver hygiene:** browser subprocess env scrubbed of credentials (Node dependencies could read them); Chromium sandbox bypass args only where required; missing-Chromium errors give install hints [obs].
- **CDP supervisor:** attaches to the DevTools socket to capture dialogs, frames and console; ≤5 reconnect failures after attach [obs].

### 3.18 Vision, image/video generation, TTS, STT

- **vision_analyze:** load from URL (SSRF-checked, ≤50 MB, configurable download timeout), local path, or data URL; validate type by magic bytes (≤100 frames, ≤100,000,000 aggregate pixels for animated); region crop at original resolution; on native-vision main models return the multimodal envelope (image downscaled to ≤1568 px longest side and ~5 MB target), else ask an auxiliary vision model and return text; billing/size errors mapped to hints [obs].
- **Embed history budget:** native embeds are re-sent on every later call, so: embed target size default 256 KB (64 KB–4 MB), and inside delegated children a repeat cap of 3 embeds per (session, source) (≤4096 keys tracked); the counter is shared by region crops of the same file [obs].
- **image_generate / video_generate:** provider plugins; model fixed by user config; returns URL or absolute path; container backends get cache paths translated [obs].
- **text_to_speech:** default provider Edge (`en-US-AriaNeural`); others ElevenLabs (multilingual v2 / flash v2.5 streaming), OpenAI, xAI (24 kHz, 128 kbps, speed 0.7–1.5), MiniMax, Mistral, Gemini, local NeuTTS/KittenTTS/Piper, command providers; response body cap 16 MB; long text split into ordered chunks; Opus output for Telegram, Matrix, Feishu, WhatsApp, Signal (native for some providers, ffmpeg for others); CLI saves under a voice-memos dir [obs].
- **STT (non-tool):** providers local faster-whisper (default), local command, Groq, OpenAI, Mistral, xAI, ElevenLabs, DeepInfra; default file cap 25 MB; per-provider caps (OpenAI/Groq 25 MB, Mistral/xAI 500 MB, ElevenLabs <5 GB; Mistral ≤3600 s; OpenAI 4o-transcribe 450 s, whisper-1 600 s); long audio chunked at silences (≤60 s lookback) [obs]. Voice mode and wake word are CLI/GUI features (push-to-talk, 16 kHz mono) [obs].

### 3.19 execute_code (programmatic tool calling)

- **Purpose:** the model writes Python that calls a subset of tools through RPC; only stdout returns [obs].
- **Allowed in-script tools:** web_search, web_extract, read_file, write_file, search_files, patch, terminal (foreground only — background, pty, notify, watch, heartbeat, persist params rejected), intersected with the session's enabled tools; the schema text lists only the intersection [obs]. Helpers `json_parse`, `shell_quote`, `retry(fn, max_attempts=3, delay=2)` [obs]. No recursion into execute_code, delegate_task or MCP tools [obs].
- **Limits:** timeout 300 s (SIGTERM then SIGKILL after 5 s grace), ≤50 tool calls per call, stdout 50,000 bytes inline (40/60 head/tail; middle spilled to `<home>/cache/exec/`, spill ≤5,000,000 bytes), stderr 10,000 bytes (shown on non-zero exit) [obs].
- **Modes:** `project` (default: session cwd; active venv/conda python if valid and ≥3.8, else the app's python) and `strict` (isolated temp dir, app python) [obs].
- **Session kernel:** local backend keeps one Python child per (session, mode, interpreter, cwd, tool set) over a Unix socket (loopback TCP on Windows); state persists across calls; `reset` discards; timeout/interrupt kills it; idle timeout 1800 s; ≤4 top-level session kernels (LRU eviction), delegated children's kernels keyed `<parent>::child::<child>` are pinned while running and disposed at child cleanup; kernel capture ≤1,000,000 bytes per cell [obs].
- **Remote backends:** a remote session kernel driven by cell files polled every 0.5 s; if it cannot start, fall back to a per-call script ship; tool calls travel as request files polled via `env.execute` (needs Python 3 remotely); private dirs created with a restrictive umask [obs].
- **RPC pipeline:** per-call token check → allow-list → call budget → dispatch with output silenced → log [obs].
- **Result:** `{status: success|error|timeout|interrupted, output, tool_calls_made, duration_seconds, kernel?, …}`; failure hints for common mistakes (importing non-existent names from the in-script module, missing helper imports, treating results as strings, third-party imports) [obs]. Misrouted `command` argument → error pointing to terminal [obs].
- **Approval:** whole-script gate (§3.13) before spawn [obs].

### 3.20 delegate_task (subagents)

- **Inputs:** `tasks[]` each `{goal (required), context?, output_schema?, images? (≤8 paths/URLs), group?}`; `action` spawn (default) / list / steer (subagent_id + message appended to the child's next tool result) / stop (subagent_id; partial result returned). Legacy single `goal`/`context` and per-task `role` accepted but role is ignored (capability derives from depth); legacy `background` ignored [obs]. Trusted-config-only task fields are stripped from model input [obs].
- **Behaviour:** each child gets its own conversation, terminal session and toolset; top-level delegations run in the background and results re-enter as a new message between turns; depth>0 (orchestrator children) and finite/one-shot runs run synchronously [obs].
- **Limits/defaults (code):** max concurrent children 10 (schema text shows the live value), max spawn depth 1 (flat), max iterations 250, no wall-clock timeout by default (heartbeat staleness: 30 s heartbeat; stale after 15 idle cycles = 450 s or 40 cycles stuck in one tool = 1200 s), async children default 3, summary ≤24,000 chars (min 2,000), one-shot runs ≤2 children total, child compression cap ≥16,000 tokens when set [obs].
- **Blocked tools for children:** delegate_task, clarify, memory, send_message, cronjob_manage; default child toolsets terminal, file, web; MCP toolsets inherited by config [obs].
- **output_schema:** child told up front; parent validates with one correction retry; a miss keeps `status: completed` with raw text plus `schema_valid: false`, errors and a note — never discard [obs].
- **Stop fan-out:** every stop surface also interrupts detached background units for the session [obs].
- **Durability:** background delegation is process-local; durable work belongs to cron or notify-on-complete background processes [obs].
- **Worktree isolation (opt-in):** per-child git worktree, local backend only [obs].

### 3.21 Remaining tools (brief)

- **todo_list:** in-memory per agent, revisioned (UI rejects stale writes), merge vs replace, statuses with markers `[x] [>] [ ] [~]`, re-injected after context compression under a fixed header; invalid items normalised; history hydration ignores results over 512,000 chars [obs].
- **clarify:** validated questions; surface callback renders; messaging asks one question at a time with "skip"; partial answers kept on timeout; undelivered prompt retried as a plain numbered list before reporting undelivered; gateway waits poll in 1 s slices [obs].
- **cronjob_manage:** create/list/update/pause/resume/run/remove; skill-backed jobs; `skills=[]` clears; jobs run on the main model unless pinned; prompts scanned for injection [obs].
- **MCP client:** servers from config (stdio, Streamable HTTP, SSE); one background event loop; connect timeout 60 s, initial retries 3, reconnect retries 5, backoff ≤60 s ±20 % jitter, parked retry every 300 s, stdio respawn wait 15 s, tool-call timeout 300 s, list pagination ≤50 pages, discovery pass ≤300 s; tools registered as `mcp__<server>__<tool>` in toolset `mcp-<server>`; result hard cap 2,000,000 chars, resources ≤50 MB; per-profile trust records; OAuth (incl. device flow); sampling and elicitation; malware (OSV) check before launching package-runner servers (12 s) [obs].
- **Connectors / catalog / desktop / kanban / platform tools:** session-gated as in §3.2; approval cards and 300 s waits for connect/install [obs].

### 3.22 State machines

**Background process session** [obs]:

| State | Event | Next state | Notes |
|---|---|---|---|
| (none) | spawn ok (pid known) | running | id `proc_<hex12>`; owner task id recorded before any reader starts |
| (none) | spawn wrapper printed no pid / raised | exited (reason `failed_start`) | output holds the launch error; never reported as a running job |
| running | process exits | exited (reason `exited`) | exit code captured; completion queued if notify-on-complete; receipt persisted BEFORE the completion event |
| running | kill action / session teardown / max-iteration stop | exited (reason `killed`) | teardown skipped when persist-on-release; termination source recorded |
| running | backend vanished (container gone, checkpoint PID mismatch) | exited (reason `lost`) | reported as "marked lost" |
| running | ownership handoff by a child | running (new owner) | completion now routes to the parent; note rides the notice |
| exited | 30 min elapse or LRU pressure (>64 tracked) | dropped from memory | retained receipt (≤64, ≤7 days) still answers poll/log for the owning conversation |

`ProcessSession { id, command, task_id, owner_task_id, session_key, pid?, pid_scope: host|sandbox, cwd?, started_at, exited, exited_at, exit_code?, completion_reason: exited|killed|lost|failed_start|already_exited, termination_source, output_buffer (rolling ≤200,000), total_output_chars, notify_on_complete, completion_output_chars, watch_patterns[], heartbeat_seconds, persist_on_release, handoff_note, watcher routing {platform, chat_id, user_id, user_name, thread_id, message_id}, detached (recovered) }` [obs].

**Execution environment** [obs/inf]:

| State | Event | Next state |
|---|---|---|
| absent | first call for env key (under per-key lock) | creating |
| creating | backend constructor ok + snapshot ok | ready (snapshot sourced) |
| creating | snapshot failed, login bash usable | ready (login shell per command) |
| creating | snapshot failed, login bash broken | ready (plain `bash -c`) |
| creating | ImportError / SDK missing | absent; call returns `status: disabled` |
| ready | command | ready (activity timestamp refreshed) |
| ready | connection-class failure | evicted (degraded result; next call recreates) |
| ready | container gone mid-command (Docker) | recreated (one retry, `environment_recreated` flag) |
| ready | idle > lifetime and no live background process | torn down (persistent FS kept per backend rules) |
| ready | process exit | torn down (bounded waits) |

**Dangerous-command decision** (terminal path) [obs]:

| Step | Outcome options |
|---|---|
| container fast path | deny-glob block / approved |
| floors | hardline block / self-delete block / sudo-guess block / deny-glob block / continue |
| bypasses | yolo or mode off → approved; allowlist → approved |
| unattended | context policy deny → blocked; approve → approved; none → approved |
| detection | not dangerous or session-approved → approved |
| smart judge | approve → approved (once) / deny → blocked (+breaker) / escalate → human |
| human (CLI) | once/session/always → approved (persist scope) / deny → blocked / timeout → blocked ("timeout") / cancelled → blocked ("not answered") |
| human (gateway) | resolved approve → approved / deny (+reason) → blocked / unresolved → blocked ("silence is not consent") / withdrawn → blocked ("withdrawn") / notify failed → blocked |
| no surface but ask flags | `pending_approval` result |

### 3.23 Sudo handling

- **Password sources:** a per-scope cache (scope = session key; else the callback owner; else the thread), a configured password in the profile env file, or an interactive masked prompt on an interactive parent session (45 s timeout) [obs].
- **Children:** delegated subagents are always headless for sudo (a raw tty prompt from a child races the main UI for the terminal) [obs].
- **Failure annotation:** headless sudo failures ("a password is required", "no tty present", "a terminal is required") get a tip to configure the password locally (messaging surfaces never accept passwords in chat); a wrong-password failure clears the cached password and, when a re-prompt is possible, says the user will be asked again; result flags `sudo_auth_failed`, `sudo_cache_cleared` [obs].
- **Guard:** agent-supplied `sudo -S` without a configured password is a floor block (password guessing) [obs].
- **No-sudo users:** when the account has no sudo at all, no re-prompt is promised [obs].

### 3.24 Interrupts and yields

- Interrupt signalling is per thread, so stopping one session never kills another session's tools in the same process; tools poll "am I interrupted" for the current thread [obs].
- Waits that move to a deadline worker keep honouring the original tool thread's interrupt bit [obs].
- A separate "yield" request (user typed while a command runs) asks the foreground wait to hand the process to the background instead of killing it; local backend only [obs].
- Approved commands clear a stale interrupt bit once before running (R02.9) [obs].
- Web search, guards and filename-search admission are interruptible [obs].

### 3.25 Browser details worth keeping

- **Vault autofill:** `list` returns handles and non-secret metadata (login identifiers are not secrets); `fill` resolves the secret locally and injects it into the current page only when the page origin exactly equals the item's bound origin (checked before and again inside the fill script); payment fills need user confirmation; `save_login` stores a credential with an optional label; `enter_code` fills one-time codes; `unlock` targets a password-manager backend. The model never receives the secret [obs].
- **CDP passthrough:** raw DevTools method + params; timeout default 30 s, max 300 s; registered only when an endpoint is reachable at session start [obs].
- **Dialogs:** the supervisor captures native alert/confirm/prompt/beforeunload; snapshots expose them as pending; the dialog tool accepts or dismisses [obs].
- **Fallbacks:** a lightweight engine without screenshots falls back to Chrome for vision capture and a fixed set of commands (open, snapshot, screenshot, eval, click, …) [obs].
- **Real profile:** an opt-in mode drives the user's own browser profile under a dedicated session name with a 30 s CDP lock [obs].
- **Hybrid routing:** with a cloud provider configured, private/local URLs can be routed to a local sidecar automatically (config switch), so cloud providers never see private addresses [obs].

### 3.26 Checkpoints (transparent infrastructure)

- **Trigger:** first file-mutating operation in a turn (write_file, patch, or a terminal command with destructive flags), once per turn [obs].
- **Store:** one shared bare-ish git store with per-project refs and index files, project metadata `{workdir, created_at, last_touch}`, default exclude list, a prune marker; git env vars point at the store so nothing leaks into the user's repo [obs].
- **Limits:** skip projects over 50,000 files; per-project ledger ≤2,000 entries; prune orphans (working dir gone) and stale refs (older than retention days), then `git gc --prune=now`, then a size-cap pass dropping oldest checkpoints until under a total size [obs].
- **User surface:** rollback commands live in the CLI (04) [obs].

### 3.27 MCP client (summary for the integrations owner)

- **Config:** servers from the config file with stdio (command, args, env with `${VAR}` interpolation and context-variable resolvers), HTTP (Streamable HTTP or legacy SSE) with headers; managed launchers (npx/npm/node/uv/uvx) resolved to managed runtimes [obs].
- **Lifecycle:** one daemon event loop; each server is a long-lived task so transport cancel scopes enter and exit in one task; first connect retries 3, reconnect retries 5 with capped exponential backoff (≤60 s, ±20 % jitter); parked servers retried every 300 s; stdio respawn wait 15 s; drain on shutdown 3 s [obs].
- **Discovery:** paginated list (≤50 pages) per server; whole pass ≤300 s; tools registered into `mcp-<server>` with alias registration when the server name collides with a built-in toolset name; schema cache; schema sanitising for strict providers [obs].
- **Calls:** default tool timeout 300 s; results rendered into a JSON envelope (`result`, optional `structuredContent`, `_meta`) with hard cap 2,000,000 chars; binary resources ≤50 MB saved as media; errors classified (auth, transport, not found −32601) [obs].
- **Security:** per-profile trust records gate first use; package-runner servers checked against an OSV malware feed (12 s) before launch; OAuth with token storage per profile, device flow for headless; sampling (server asks the agent's model) and elicitation (server asks the user) supported with limits [obs].
- **Budget:** MCP results use the 50,000-char spill threshold (§3.4) [obs].

### 3.28 Platform and surface tools (summary)

- **Kanban:** worker tools act on the task named by the dispatcher (show, complete with structured handoff, block on a question, schedule, request review, request changes as reviewer, heartbeat during long ops, comment, attach by base64 or URL, list attachments, create children, link dependencies); orchestrator-only list and unblock (unblock moves to ready when parents are done, else todo); tools run in the agent process so they reach the board DB even from a container terminal [obs].
- **Computer use:** actions over a background desktop driver; text results are JSON; captures return the multimodal envelope; never steals the user's cursor or focus; capture modes set-of-marks / vision / accessibility tree [obs].
- **X search:** xAI built-in search; an explicit API key wins over subscription OAuth at call time (citations); date filters validated before the billable call; results flagged `degraded` when a narrowing filter was active and no citations came back [obs].
- **Connections:** status/connect/reconnect/rename for managed connector accounts (cannot disconnect or revoke); install/enable/authorize for local MCP servers; GUI surfaces block on a card up to 300 s (deadline fixed when the call starts); other surfaces return URLs [obs].
- **Desktop GUI (desktop sessions only):** read the embedded terminal pane, close a background-process tab (does not kill), preview pane open/close/read (paged), drive the in-app browser by element refs with delta responses, annotate elements, identify the window below (metadata only), focus panes, emoji reactions (opt-in), guided tours and tips (share a 6-hour cooldown with the app's own tips), theme/layout with a picker that waits ≤5 min [obs].
- **Discord / Feishu / Yuanbao:** platform-specific actions using the same bot credentials as the messaging adapter; only in that platform's bundle [obs].

### 3.29 Hooks, UI touchpoints and evidence

- **Plugin hooks around tools:** a pre-tool-call hook can veto or escalate any call (this is the supported way for plugins to block commands); a post-tool-call hook observes results; a terminal-output transform hook may rewrite foreground and background output (first string result wins, fail-open, redaction still applied afterwards, keyed per tool-call id so concurrent calls gate separately); approval request/response hooks fire around the CLI prompt [obs].
- **Metrics:** terminal outcomes and file edits are counted per backend/tool in a local observability layer; only built-in tool names (captured before user toolsets are created) may leave the machine in shared metrics, never plugin/MCP/custom names [obs].
- **UI touchpoints owned elsewhere (04):** emoji per tool for spinners/progress; approval prompt with once/session/always/deny; yolo banner and status fragment; clarify cards; background completion messages (hidden for heartbeats); persisted-output blocks rendered as normal tool text [obs].
- **Evidence:** no REA evidence in this pass; every [obs] above is source/docs reading at the pinned commit. Runtime-only facts are listed in §7 as "needs REA".

### 3.30 Errors and recovery (cross-tool summary)

| Failure | Where | What the model sees | Recovery |
|---|---|---|---|
| handler raised | registry | bounded `{"error": "Tool execution failed: …"}` | none; model may retry |
| unknown tool name | registry | `{"error": "Unknown tool: …"}` | tool search may offer it if deferred |
| probe failing | schema build | tool absent from schema | re-probed after 30 s; flake grace 60 s |
| backend unreachable | terminal | `status: degraded` + retry hint | env evicted; next call recreates |
| backend exception | terminal | retried 3× (2/4/8 s), then error | — |
| container vanished | Docker | normal result + `environment_recreated` warning | recreated once, command retried |
| command deadline | all backends | partial output, exit 124, timeout line | none; increase timeout or background |
| user interrupt | all backends | partial output, exit 130, interrupted line | — |
| oversize output | any tool | persisted-output block with path | page with read_file / process with execute_code |
| approval timeout | approval | blocked; "silence is not consent" | user asks again; a fresh call raises a fresh prompt |
| approval undeliverable | approval | blocked; "not answered" wording | fix surface |
| smart deny ×3 | approval | escalated hard-stop wording | any approval resets |
| stale write | write_file | refusal, file untouched | read_file, merge, retry |
| fuzzy miss / ambiguous | patch | error with match locations / strategy notes | widen context or replace_all |
| repeated identical reads | read_file | stub, then BLOCKED | stop re-reading |
| SSRF/private URL | web/browser/vision | per-URL "Blocked" entry | allow_private_urls (metadata still blocked) |
| keyed web backend failure | web_search/extract | served by keyless tier once (not cached) | next call retries the keyed backend |
| sandbox spill write mismatch | spill | inline truncation note | — |
| kernel timeout | execute_code | `status: timeout`, kernel state lost | next call starts fresh kernel |
| tool budget hit | execute_code | in-script error on further calls | split work |
| child schema miss | delegate_task | completed + `schema_valid: false` | parent decides |

## 4. File map (what to read in the Read step)

| Sub-area | Reference path | Lines | Read what | Docs / tests to read with it |
|---|---|---|---|---|
| Area contract | `tools/AGENTS.md` | ~170 | all | `website/docs/developer-guide/adding-tools.md` (211), `tools-runtime.md` (250) |
| Registry | `tools/registry.py` | 1086 | all | `tests/tools/` registry tests |
| Toolsets | `toolsets.py` | 495 | all | `website/docs/reference/toolsets-reference.md` (171) |
| Schema assembly | `model_tools.py` | 993 | `get_tool_definitions`, post-processing blocks, `_run_async` (01 owns dispatch loop) | — |
| Budgets | `tools/budget_config.py`, `tool_output_limits.py`, `tool_output_truncate.py`, `tool_result_storage.py` | 114, 64, 32, 495 | all | — |
| Tool search | `tools/tool_search.py`, `tool_search_catalog.py`, `tool_search_validation.py` | 617, 324, 232 | config + bridge schemas | `features/tool-search.md` (327) |
| Terminal tool | `tools/terminal_tool.py` | 1653 | plan/acquire/foreground/schema/handler | `features/tools.md` (298) |
| Terminal companions | `terminal_tool_guards.py`, `_result.py`, `_background.py`, `_lifecycle.py`, `_sudo.py`, `_backends.py`, `_config.py`, `terminal_hints.py`, `terminal_scope.py` | 368, 277, 285, 333, 501, 373, 231, 160, 221 | all except sudo tty plumbing | — |
| Env contract | `tools/environments/base.py`, `base_output.py`, `base_session_env.py`, `base_wait.py`, `streams.py` | 804, 479, 190, 75, 213 | all | `developer-guide/terminal-environment-plugin.md` (140) |
| Local | `environments/local.py`, `local_env_policy.py`, `local_pythonpath.py` | 1178, 398, 161 | env builders, temp dir, kill group | `user-guide/security.md` (1016) §passthrough |
| Docker | `environments/docker.py`, `docker_egress.py` | 1332, 298 | init, security/resource/mount args, reuse, recover, cleanup | `user-guide/docker.md` (949) |
| SSH | `environments/ssh.py` | 302 | all | — |
| Singularity | `environments/singularity.py` | 215 | all | — |
| Modal | `environments/modal.py`, `managed_modal.py` | 282, 205 | all | — |
| Daytona | `environments/daytona.py` | 207 | all | — |
| Vercel | `environments/vercel_sandbox.py` | 421 | create/snapshot/cleanup | `features/tools.md` §Vercel |
| File sync | `environments/file_sync.py`, `remote_common.py`, `credential_files.py` | 500, 99, 409 | all | — |
| Processes | `tools/process_registry.py` (+ `_notifications`, `_results`, `_wait_policy`, `_termination`, `_checkpoint`, `_output_log`) | 2766 (+506, 119, 188, 191, 159, 135) | see giant files | `features/tools.md` §Background |
| Approval | `tools/approval.py`, `approval_detection.py`, `approval_floors.py`, `approval_smart.py`, `approval_prompt.py`, `approval_context.py`, `approval_gateway_wait.py`, `approval_human_wait.py`, `approval_yolo.py` | 1290, 1572, 204, 170, 361, 385, 254, 170, 107 | flow + pattern tables (labels only) | `user-guide/security.md` §Dangerous Command Approval |
| Self-repo guard | `tools/self_repo_guard.py` | 567 | skim | — |
| File tools | `tools/file_tools.py`, `file_tools_write_guards.py`, `file_tools_read_tracking.py`, `file_tools_paths.py`, `file_operations.py`, `file_operations_common.py`, `file_operations_search.py`, `file_operations_lint.py`, `read_extract.py`, `fuzzy_match.py`, `patch_parser.py`, `escape_drift_autocorrect.py`, `file_state.py`, `binary_extensions.py` | 1430, 646, 427, 317, 1717, 308, 1139, 422, 623, 719, 481, 154, 261, 86 | guards, read/write/patch flows | `security.md` §File Write Safety |
| Checkpoints | `tools/checkpoint_manager.py`, `checkpoint_maintenance.py`, `checkpoint_pruning.py` | 1673, 614, 149 | header + public API | `user-guide/checkpoints-and-rollback.md` (307) |
| Web | `tools/web_tools.py`, `web_tools_extract.py`, `web_tools_truncate.py`, `web_result_cache.py`, `web_tools_rescue.py`, `url_safety.py`, `website_policy.py` | 573, 227, 193, 302, 159, 582, 185 | all | `features/web-search.md`, `security.md` §SSRF |
| Browser | `tools/browser_tool.py` + `browser_tool_*` + `browser_supervisor*` + `browser_camofox.py`, `browser_lightpanda.py`, `browser_cdp_tool.py`, `browser_dialog_tool.py`, `browser_vault_tool.py`, `browser_use_cli.py` | 1376 + ~2,600 + ~1,200 + 601, 345, 431, 109, 664, 964 | schemas, navigate, session lifecycle | `features/browser.md` (914), `developer-guide/browser-supervisor.md` (252) |
| Vision/media | `tools/vision_tools.py`, `vision_tools_history_budget.py`, `vision_tools_image_prep.py`, `image_source.py`, `image_generation_tool.py`, `video_generation_tool.py`, `tts_tool.py`, `tts_tool_providers.py`, `transcription_tools.py` (+ siblings), `voice_mode.py` | 1192, 180, 338, 322, 995, 379, 682, 617, 602, 1555 | schemas + budget | `features/vision.md` (260), `tts.md` (768), `voice-mode.md` (652) |
| execute_code | `tools/code_execution_tool.py`, `code_execution_rpc.py`, `code_execution_env.py`, `code_kernel.py`, `code_kernel_remote.py` | 969, 255, 315, 911, 433 | all | `features/code-execution.md` (332) |
| Delegation | `tools/delegate_tool.py` + `delegate_tool_*` + `async_delegation*.py` + `subagent_worktree.py` | 813 + ~3,600 + ~1,500 + 261 | schema, limits, blocked tools | `features/delegation.md` (700), `developer-guide/subagent-lifecycle-api.md` |
| Small tools | `todo_tool.py`, `clarify_tool.py`, `clarify_gateway.py`, `cronjob_tools.py` | 336, 201, 308, 1200 | schemas | `features/cron.md` |
| MCP | `tools/mcp_tool.py` + `mcp_tool_*` + `mcp_oauth*` | 742 + ~6,000 + ~2,300 | constants, naming, transports | `features/mcp.md` (1104), `reference/mcp-config-reference.md` (443) |
| Catalogue docs | — | — | — | `reference/tools-reference.md` (389) |

Giant files (where the meat is):
- `tools/process_registry.py` [2766]: the spawn functions (`spawn_local`, `spawn_via_env`), `wait`, `kill_process`, `transfer_ownership`, `_heartbeat_loop`, and the tool schema/handler at the bottom (from "the process_manage tool schema"). Skip systemd scope probing unless doing Linux memory caps.
- `tools/file_operations.py` [1717]: the shell-backed read/write sentinel protocol and `read_file`; skip Windows-specific branches initially.
- `tools/checkpoint_manager.py` [1673]: header docstring is the spec; implementation can wait.
- `tools/terminal_tool.py` [1653]: `_plan_execution`, `_acquire_env`, `_run_foreground`, `terminal_tool`, `_handle_terminal`, `_resolve_container_task_id`, `_get_env_config`.
- `tools/approval_detection.py` [1572]: `HARDLINE_PATTERNS`, `DANGEROUS_PATTERNS` (labels), normalisation helpers; port behaviour via a test corpus, not regex copying.
- `tools/voice_mode.py` [1555]: CLI-only; defer.
- `tools/file_tools.py` [1430]: guard order in `read_file_tool`, `write_file_tool`, schemas at the end.
- `tools/environments/docker.py` [1332]: `__init__`, `_resource_args`, `_mount_args`, `_recreate_container`, `cleanup`.
- `tools/approval.py` [1290]: `check_all_command_guards`, `_human_decision`, `_floor_block`, `check_execute_code_guard`.

## 5. Minimum viable subset

v1 needs:
1. Registry with self-registration, toolsets (core + CLI bundle + file/terminal/web), probe cache, dispatch with the string/multimodal contract and bounded error envelope.
2. Result budget: per-result threshold, spill to home cache with preview, per-turn budget, shared head/tail truncation.
3. `terminal` (foreground + background) and `process_manage` (list/poll/log/wait/kill/write/submit/close) with the local backend and the shared execution contract (snapshot, cwd marker, bounded capture, timeout backstop, interrupt).
4. Docker backend (hardened flags, persistent bind dirs, reuse by label, recovery) — the main isolation story; SSH next.
5. Approval layer: hardline floor, user deny globs, dangerous patterns, manual mode with CLI prompt, session/always persistence, yolo, container skip, unattended defaults deny.
6. Child-env scrubbing and output redaction.
7. File tools with read guards, dedup, stale-overwrite refusal, protected paths, fuzzy patch (replace mode).
8. `web_search`/`web_extract` through one provider interface with SSRF checks.
9. `todo_list`, `clarify` (single surface), `execute_code` per-call script mode (kernel later), `delegate_task` synchronous batch.

Suggested build order inside v1 [inf]: (a) registry + toolsets + budget/spill; (b) base execution contract + local backend + terminal foreground; (c) approval floors + detection + CLI prompt; (d) file tools over the environment; (e) background processes; (f) Docker backend; (g) web tools behind a provider interface; (h) todo/clarify/execute_code/delegate. Each step lands with contract tests: every registered tool is in some toolset; no schema names a foreign tool; handlers return strings; guard ordering; cwd marker survives odd output (no trailing newline, binary noise).

Can wait: Singularity, Modal (direct and managed), Daytona, Vercel (each is an adapter over the same contract once remote file sync exists); smart approvals; messaging approval round-trip (needs gateway); browser stack (large); vision native embeds; TTS/STT/voice; MCP client; tool-search bridge; checkpoints; kanban/desktop/platform tools; persistent kernels; background delegation and async completions; retained process receipts; self-repo guard (Windows-only).

## 6. Hard parts / traps

1. **R02.1 Per-session variables leaking through the shared snapshot.** One long-lived environment serves many sessions; dumping `export -p` into the snapshot would make later sessions source the first session's identity. Unset the per-session names in a subshell before the dump; never line-filter `declare -x` output (multi-line values can smuggle commands). Inject session vars fresh per command.
2. **R02.2 Bounded vs full-fidelity capture.** Capping output for internal consumers (file reads through the shell, RPC request files, log reads) silently corrupts data. Only the model-facing foreground path may cap; everything else must be unbounded.
3. **R02.3 Timeouts that never fire.** A blocked poll loop on the event-loop thread disables every async timer. Enforce each command with an outer wall-clock backstop on a separate worker (timeout + 2 s), kill the tree on expiry, and return partial output with rc 124 plus a flag distinguishing the app's deadline from a command's own exit 124.
4. **R02.4 Floors must precede every bypass.** Hardline patterns, interpreter self-delete, `sudo -S` guessing, and user deny globs must run before yolo, `mode: off`, cron approve-mode and the permanent allowlist — and deny globs must run even on isolated containers. Unparseable quoting fails closed.
5. **R02.5 Silence is not consent; withdrawal is not refusal.** Approval timeouts and undeliverable prompts must block the command but be reported distinctly from a user deny (and not count toward the smart-denial breaker), or the agent and the user get the wrong story.
6. **R02.6 Isolation is about mounts, not backend names.** Docker skips prompts only when no host path is bind-mounted; once the host cwd or volumes are mounted, `rm -rf /workspace` reaches host files and the full approval flow applies.
7. **R02.7 Credentials on the command line.** Passing env values as `docker run -e NAME=value` or in remote command text exposes them in `/proc/*/cmdline`. Pass names only and carry values in the client process environment (Docker client env, ssh `SendEnv`).
8. **R02.8 Multiprofile scoping everywhere.** Probe caches, env keys, cwd records, snapshot stores, output-limit caches, approval allowlists and MCP trust must key on the profile home resolved per call; import-time constants pin the launch profile. Under an unresolved scope, refuse rather than fall back.
9. **R02.9 Approved command killed by a stale interrupt.** An interrupt bit that landed while waiting for approval will SIGINT the just-approved run. Clear it once before the retry loop, never inside it.
10. **R02.10 cwd hijack.** Only record the session cwd when this command printed the marker (a killed command didn't) and never from a per-call workdir; concurrent sessions must read the cwd from the result, not shared env state.
11. **R02.11 Foreground refusals that create retry loops.** Refusing an over-cap timeout or a stray foreground heartbeat produced hundreds of identical retries. Promote over-cap timeouts to tracked background jobs, convert a lone trailing `&`, and normalise harmless stray params; refuse only shapes that need a rewrite the tool cannot do safely.
12. **R02.12 Kill order for process trees.** SIGTERM the recorded parent first, wait a grace, then the snapshotted survivors, then SIGKILL; killing a browser's children first turns a clean exit into a crash dump. Guard against recycled PIDs/PGIDs by checking the group leader's start time before signalling.
13. **R02.13 Flapping availability probes strip tools mid-session.** A probe that times out once (e.g. `docker version` under load) drops a whole toolset and the model reports "tool does not exist". Use a short grace that serves last-good true after a recent success, but honour sustained failure.
14. **R02.14 Stale whole-file overwrite.** `write_file` without a current full read clobbers edits by others or the user. Keep per-task baselines (full read or own write), refuse when missing or the file changed on disk, and never let shell/terminal writes count as a baseline.
15. **R02.15 Read loops on weak models.** Identical reads of an unchanged file must return a stub, then a hard block; mark these as harness refusals so error classifiers don't escalate them as failures; serve full content once after a context compaction.
16. **R02.16 Spill files must outlive the cache mindset.** Spill archives are referenced by stored transcripts; deleting them on a 24 h cache timer breaks resumed sessions. Retain for the session retention window and keep anything still referenced. Remote copies must be verified by byte count (argv limit 128 KB means stdin delivery).
17. **R02.17 Background completion only where it can be delivered.** One-shot runs, cron, kanban workers, stateless API sessions and delegated children cannot receive a later completion; notify flags there must be cleared with a poll instruction, and subagent-owned processes must be killed or explicitly handed off at teardown.
18. **R02.18 Watch-pattern floods.** Pattern notifications on chatty processes force a full-context turn per match. Rate-limit per process (15 s), strike-out after 3 windows or 8 lifetime hits, and add a global breaker.
19. **R02.19 Container recreated silently.** When a container vanished out of band and is recreated, background processes and unpersisted files are gone; the result must carry an explicit warning or the model keeps assuming old state.
20. **R02.20 Docker self-inconsistencies.** Repeated `--network` flags exit 125; a non-persistent session key with colons breaks `-v` specs; snap-packaged Docker rejects `no-new-privileges`; s6-overlay images need exec `/run` and no `--init`; ext4 storage drivers reject `--storage-opt`; pids limit too low (256) kills browser/desktop workloads — the code uses 2048.
21. **R02.21 Cross-reference hallucination in schemas.** A description that names a tool from another toolset makes models call it when it's disabled. Inject cross-references after filtering only; rebuild `execute_code` docs from the enabled intersection.
22. **R02.22 Secrets in child processes.** Every spawn must go through the single env builder; `dict(os.environ)` pins the launch profile and leaks provider keys to model-authored commands, Node dependencies of the browser driver, and MCP servers.
23. **R02.23 Docs vs code drift (do not trust docs for numbers).** Docs say Docker containers are stopped and removed at shutdown (code leaves persistent containers running for reuse and reaps orphans at the next start), Docker PID limit 256 (code 2048), tool name `process` (code `process_manage`), five vault tools (code: one tool, five actions), delegation defaults 3 children/depth 2 in the area contract file (code and user docs: 10/1), plugin backend `execute` returning `exit_code` (base returns `returncode`), approval mode reader fallback `manual` while shipped defaults say `smart`. The rebuild spec must follow code-observed behaviour and record each choice.
24. **R02.24 Async smart-approver as an injection target.** The judged command comes from a possibly prompt-injected model; strip shell comments, fence the command, instruct the judge to ignore embedded directives, cap output tokens, and treat any non-verdict as escalate.
25. **R02.25 Toolset membership vs registration.** A registered tool is exposed only if a resolved toolset names it; the kanban toolset merges registry members (so it gains `kanban_schedule`) while platform bundles built from the static core list do not. Decide explicitly whether bundles are static lists or registry-derived, and test "every registered tool is reachable from some toolset".
26. **R02.26 Send-message is not a tool.** Messaging delivery exists as a library for the CLI send command, cron and the MCP server; exposing it to the model would let a prompt-injected session message arbitrary chats. Keep it off the model surface.
27. **R02.27 Untrusted-input sessions need a tool floor, not just approvals.** Webhook-triggered sessions get only search, extract, vision and clarify; unattended API/webhook sessions deny flagged commands instantly instead of waiting out a timeout nobody can answer.
28. **R02.28 Handler signature drift breaks plugins.** The dispatcher injects a growing set of context kwargs; a narrow plugin handler would crash. Inspect the handler signature and pass only what it declares (unless it takes `**kwargs`).
29. **R02.29 Unscoped probes at boot.** Under profile multiplexing, availability probes that run before any profile scope exists raise a scope error by design; log it quietly and re-probe on the first scoped turn rather than reporting a crashed probe or caching "unavailable" for everyone.
30. **R02.30 SDK backends lie about timeouts.** Some cloud SDKs ignore their own exec timeout; keep a shell-level timeout wrapper inside the command and a client-side deadline (managed gateway: timeout + 10 s, then cancel) so the tool call returns.
31. **R02.31 Remote sync-back can overwrite host state.** Pulling a sandbox's app dir back must apply only files whose hash changed since the push, never credential files (upload-only), refuse oversized archives, survive SIGINT, and serialise across concurrent sandboxes with a file lock.
32. **R02.32 Kernel cap starvation.** A finished child's code kernel must be disposed at child cleanup and running children's kernels must not count against the top-level LRU cap, or fan-outs evict siblings mid-task.

## 7. Open questions

1. Is the Docker default image really a full desktop image (with VNC/Chromium) and how large is it on first pull? Needed: REA pull/inspect of the shipped default image tag — needs REA.
2. Exact contents of the hardline and dangerous pattern lists as shipped, and how many false positives they produce on a typical dev session. Needed: REA run of the approval detector over a command corpus, or a product decision to author our own corpus — needs REA.
3. Does the smart approver use the main model or a separate auxiliary model by default, and what timeout does the "approval" task get? Needed: shipped config check — needs REA.
4. Remote session kernels on Docker/SSH/Modal: which backends actually start the remote kernel vs fall back to per-call scripts in practice? Needed: runtime check per backend — needs REA.
5. Background process memory cap on Linux (systemd transient scopes): enabled by default or only when systemd user sessions exist? Needed: runtime check on a Linux host — needs REA.
6. Process-registry spawn for local PTY: which PTY library and Windows behaviour (ConPTY?) [unk]. Needed: read `spawn_local` fully or REA.
7. The egress credential proxy ("iron-proxy" in code): third-party or reference-internal component, and is it on by default for Docker? Needed: read `agent/proxy_sources` (outside this area) and shipped config — needs REA.
8. Approval timeout clamp: what is the "platform-safe maximum"? [unk] Needed: read the dependency the clamp calls.
9. Which surfaces render the clarify/approval cards (owned by 04/gateway) and what the exact pending-approval resume flow is after a `pending_approval` result. Needed: area 04 / gateway spec.
10. Product decision: keep model-visible tool names identical (`terminal`, `read_file`, …) for model-training familiarity, or rename? Branded ones (`yb_*`, `feishu_*`, `xai_video_*`, the in-script module name) need a decision. Needed: lead decision.
11. Product decision: ship all seven backends in v1 or local + Docker + SSH first? Needed: lead decision.
12. Does the managed Modal gateway (vendor-hosted, subscription) have any equivalent in the rebuild, or is it dropped? Needed: lead decision.
13. Default `approvals.mode`: shipped config writes `smart`; with no config the reader falls back to `manual`. Which applies on a fresh install with no config file? Needed: REA fresh-install run — needs REA.
14. `kanban_schedule` is registered into the kanban toolset but absent from the shared core list: is it reachable on task-worker sessions in the shipped build? Needed: REA run of a kanban worker session listing its tools — needs REA.
15. How large is the effective tool schema payload (tokens) for the default CLI bundle, with and without the tool-search bridge? Needed: REA capture of a first request — needs REA.
16. `browser_exec`'s probe passes only in Browser Use mode [obs]; do the ten standard browser tools' probes fail in that mode so the two sets are mutually exclusive, as the docs claim? Needed: read `browser_tool_install.py` routed checks, or REA tool listing with that backend — needs REA.
17. Read-path fast path: when is the in-process host read used instead of the shell-backed read (the native-file-read env flag listed in §2)? [unk] Needed: read `file_operations_native.py` and the flag's default.

## 8. What to read later

- Reference docs: `website/docs/reference/tools-reference.md`, `reference/toolsets-reference.md`, `developer-guide/tools-runtime.md`, `developer-guide/adding-tools.md`, `developer-guide/terminal-environment-plugin.md`, `developer-guide/browser-supervisor.md`, `developer-guide/streaming-tts.md`, `user-guide/features/tools.md`, `features/code-execution.md`, `features/delegation.md`, `features/browser.md`, `features/tool-search.md`, `features/vision.md`, `features/tts.md`, `features/mcp.md`, `user-guide/security.md`, `user-guide/docker.md`, `user-guide/checkpoints-and-rollback.md`, `reference/environment-variables.md` (terminal and tool vars).
- Reference tests that list the edge cases: `tests/tools/` (registry contract tests, approval config read-only E2E, terminal guard tests, file write guard tests, fuzzy match strategy tests, docker args tests, process registry tests). Read for edge-case lists only; never port test code.
- REA Evidence (outside the repo): none yet — REA not connected. Planned bundle `~/rea-lab/evidence/02-tools/` for open questions 1–5, 7, 13.
- Related specs: `01-agent-loop.md` (inline tools, parallel batches, outer error wrapper, human-wait deadline exclusion), `03-session-db.md` (retained process receipts, spill references, yolo persistence on session rows), `04-cli.md` (approval prompt, clarify UI, `/yolo`, `/approve`), `07-skills-memory.md` (memory/skills/session_search tool semantics, write approval staging).

