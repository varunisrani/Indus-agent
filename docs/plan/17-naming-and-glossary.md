# 17 · Naming and glossary

What every reference-specific name becomes in Indus Agent, which UI words we change, what our
domain words mean, and the identifiers the banned-name check must reject.

The reference column exists only so a reader of the research specs can translate. Reference names
may appear in `docs/` (this file included) and nowhere in product code, tests, assets or scripts.

Fixed by intake (`README.md`): product **Indus Agent**, CLI `indus`, env prefix `INDUS_`, home
`~/.indus`, Python package `indus`. Everything else below is a proposal unless marked
**decided**. Rows marked **lead?** need the lead's confirmation; they are collected in section (e).

General rules we apply everywhere:

1. A reference token that is a brand (`hermes`, `nous`, `Nous Research`, the caduceus glyph) is
   replaced by `indus` / `Indus` or by a neutral word. Nothing branded survives.
2. Ordinary English or protocol words (`session`, `terminal`, `read_file`, `gateway`, `curator`,
   `pre_tool_call`, `session.create`) are kept as they are. They are not distinctive and model
   familiarity with generic tool names is worth keeping.
3. Third-party protocol and vendor names (OpenAI, Anthropic, MCP, ACP, Telegram API fields,
   `TELEGRAM_BOT_TOKEN`) stay exact.
4. Anything tied to the reference vendor's hosted services (portal, tool gateway, cloud, hosted
   scheduler, free tier, billing) has no Indus name: it is dropped (decision P3).

---

## (a) Naming table

### a1. Product, binaries and packages

| Concept | Reference name (reading only) | Our name |
|---|---|---|
| Product name | Hermes Agent, Hermes | **Indus Agent** (decided); short form "Indus" |
| Main CLI | `hermes` | **`indus`** (decided) |
| Legacy single-query runner | `hermes-agent` console script | not built (D-16.85); `indus -z` / `indus chat -q` cover it |
| ACP entry script | `hermes-acp` | `indus-acp` |
| Canary / one-off build commands | `hermes-canary`, `hermes-<sha7>` | `indus-canary`, `indus-<sha7>` |
| Repo-root launcher script | `hermes` | `indus` |
| Python top package | `hermes_cli`, `hermes_constants`, `hermes_state*`, `hermes_logging`, `hermes_time`, `hermes_yaml`, `hermes_bootstrap`, `hermes_platform`, `hermes_dir_acl`, `hermes_startup_watchdog` | **`indus`** (decided) with the packages of `04-architecture.md` §3: `indus.cli`, `indus.state`, `indus.platform`, and `indus.foundation` holding the small shared modules — `foundation/home.py` (home and path constants), `foundation/log.py` (logging setup), `foundation/clock.py` (time), `foundation/yamlio.py` (YAML), `foundation/bootstrap.py` (entry bootstrap and startup watchdog), `foundation/fsio.py` (owner-only modes, ACLs) |
| Agent core modules | `run_agent.py`, `agent/`, `model_tools.py`, `toolsets.py` | `indus.core` (agent, turn loop, prompt builder), `indus.tools.registry`, `indus.tools.toolsets` |
| TUI backend package | `tui_gateway` | `indus.tui_bridge` |
| ACP package | `acp_adapter` | `indus.acp` |
| MCP server module | `hermes_tools_mcp_server.py`, logger `hermes.mcp_serve` | `indus.mcp_serve` |
| Main agent class | `AIAgent`, `HermesCLI`, `HermesArgumentParser` | `Agent`, `ClassicRepl`, `IndusArgumentParser` |
| Session store classes | `SessionDB`, `AsyncSessionDB`, `RuntimeSessionStore` | `SessionStore`, `AsyncSessionStore`, `RuntimeLedger` |
| Package/environment manager | `hermes pm`, project `hermes-pm-runtime` | `indus pm`, project `indus-pm-runtime` |
| TUI npm packages | `hermes-tui`, `@hermes/ink`, `@hermes/shared`, `@hermes/root-tests` | `@indus/tui` (apps/tui; process title `indus-tui`), upstream Ink as a dependency (no fork, D-16.28), `@indus/client` (the JSON-RPC channel shared by TUI, web and desktop); no root-tests package |
| Desktop UI kit and SDK | `@nous-research/ui`, `@hermes/plugin-sdk` | `@indus/ui`, `@indus/plugin-sdk` |
| Desktop product names | Hermes, Hermes Light, Hermes Agent Installer, Hermes Setup | Indus, Indus Remote (remote-only variant) **lead?**, Indus Installer, Indus Setup |
| Process title / tag | `hermes-tui`, `hermes` | `indus-tui`, `indus` |
| Banner mark | caduceus glyph `☤` | Indus's own mark (to be drawn; no glyph from the reference) |
| User agent string | `hermes-cli/<version>` | `indus/<version>` |
| Agent marker exported to children | `AI_AGENT=hermes-agent`, `HERMES_AGENT=true` | `AI_AGENT=indus-agent`, `INDUS_AGENT=true` |

### a2. Home directory and files

| Concept | Reference name (reading only) | Our name |
|---|---|---|
| Profile home (POSIX) | `~/.hermes/` | **`~/.indus/`** (decided) |
| Profile home (Windows) | `%LOCALAPPDATA%\hermes\` | `%LOCALAPPDATA%\indus\` |
| Named profile | `~/.hermes/profiles/<name>/` | `~/.indus/profiles/<name>/` (generic layout kept) |
| Admin managed overlay dir | `/etc/hermes/` | `/etc/indus/` |
| Project-local folder | `<repo>/.hermes/` (`skills/`, `plugins/`, `environment.json`) | `<repo>/.indus/` |
| Persona file | `SOUL.md` | **`PERSONA.md`** **lead?** (A5) |
| Native project context file | `.hermes.md` / `HERMES.md` | **`.indus.md` / `INDUS.md`** **lead?** (A6) |
| Other context files read | `AGENTS.md`, `AGENTS.override.md`, `CLAUDE.md`, `.cursorrules`, `.cursor/rules/*.mdc` | unchanged (third-party conventions) |
| Memory stores | `memories/MEMORY.md`, `memories/USER.md` | unchanged (generic) |
| Config, secrets, auth, store | `config.yaml`, `.env`, `auth.json`, `state.db` | unchanged (generic) |
| Example config shipped | `cli-config.yaml.example` | `config.example.yaml` |
| Input history | `~/.hermes/.hermes_history` | `~/.indus/input_history` |
| Install dir under home | `<home>/hermes-agent/` | `<home>/indus-agent/` |
| Bootstrap / update markers | `.hermes-bootstrap-complete`, `.hermes-update-in-progress`, `.hermes-update.lock`, `hermes-update.lock` | `.indus-bootstrap-complete`, `.indus-update-in-progress`, `.indus-update.lock` |
| Backup archive | `hermes-backup-<ts>.zip` | `indus-backup-<ts>.zip` |
| Dev setup scripts | `setup-hermes.sh`, `setup-hermes.ps1` | `setup-indus.sh`, `setup-indus.ps1` |
| Published launcher in checkout | `.hermes/bin/hermes` | `.indus/bin/indus` |
| Temp names | `hermes-snap-<id>.sh`, `hermes-cwd-<id>.txt`, `.hermes-stdin-<hex>`, `hermes_bg_<proc>.{log,pid,exit}`, `hermes-results/`, `hermes-ssh/`, `hermes-overlays/`, `hermes-sync-back-*`, `hermes_rpc`, `hermes_terminal`, `hermes-edit-*`, `hermes-tui-active-session-*.json`, `.hermes-clone-XXXXXX`, `hermes-uv-bootstrap.*`, `hermes-git-bootstrap-<pid>`, `.hermes-ssh-runtime-<nonce>` | same shapes with `indus` in place of `hermes` |
| Sandbox snapshot stores | `modal_snapshots.json`, `singularity_snapshots.json`, `vercel_sandbox_snapshots.json` | unchanged (generic) |
| Hidden git refs | `refs/hermes/<hash16>`, `refs/hermes-update-backups/…` | `refs/indus/<hash16>`, `refs/indus-update-backups/…` |
| Install auto-stash label | `hermes-install-autostash-<stamp>` | `indus-install-autostash-<stamp>` |
| Machine marker | `Nous Research/.machine`, `nous-research/.machine` | `Indus/.machine`, `indus/.machine` |
| Desktop-only home files | `desktop-plugins/`, `desktop-build-stamp.json`, `lib/` | unchanged (generic) |
| Desktop userData files | `connection.json`, `connections.json`, `window-state.json`, … | unchanged (generic) |
| Trajectory files | `trajectory_samples.jsonl`, `failed_trajectories.jsonl` | unchanged (generic) |

### a3. Environment variables

**Rule (decided):** every product variable `HERMES_<X>` becomes `INDUS_<X>` with the same suffix;
internal `_HERMES_<X>` becomes `_INDUS_<X>`; double-underscore `__HERMES_<X>` becomes
`__INDUS_<X>`. Vendor-standard platform and provider credentials keep their names (lead default).

| Family (reference) | Examples | Our family |
|---|---|---|
| `HERMES_HOME`, `HERMES_DATA_DIR_SUFFIX`, `HERMES_HOME_MODE`, `HERMES_UID`/`GID`, `HERMES_REAL_HOME`, `HERMES_SCRATCH_DIR`, `HERMES_MANAGED`, `HERMES_MANAGED_DIR`, `HERMES_CONTAINER`, `HERMES_SKIP_CHMOD` | home and install shape | `INDUS_HOME`, `INDUS_DATA_DIR_SUFFIX`, … |
| `HERMES_INFERENCE_*`, `HERMES_MODEL`, `HERMES_API_*`, `HERMES_STREAM_*`, `HERMES_LOCAL_STREAM_*`, `HERMES_AGENT_TIMEOUT`, `HERMES_CONCURRENT_TOOL_TIMEOUT_S`, `HERMES_TURN_*`, `HERMES_EPHEMERAL_SYSTEM_PROMPT`, `HERMES_ENVIRONMENT_HINT`, `HERMES_MAX_ITERATIONS` | agent core | `INDUS_INFERENCE_*`, `INDUS_MODEL`, `INDUS_API_*`, `INDUS_STREAM_*`, … |
| `HERMES_CODEX_*` | external Codex runtime knobs | `INDUS_CODEX_*` (vendor name kept inside the suffix) |
| `HERMES_YOLO_MODE`, `HERMES_ACCEPT_HOOKS`, `HERMES_IGNORE_*`, `HERMES_SAFE_MODE`, `HERMES_REDACT_SECRETS`, `HERMES_INTERACTIVE`, `HERMES_EXEC_ASK`, `HERMES_QUIET` | behaviour switches | `INDUS_YOLO_MODE`, `INDUS_ACCEPT_HOOKS`, … |
| `HERMES_SESSION_*`, `HERMES_UI_SESSION_ID`, `HERMES_GATEWAY_SESSION`, `HERMES_CRON_SESSION`, `HERMES_SINGLE_QUERY_SESSION`, `HERMES_DELEGATED_CHILD_CONTEXT` | per-session bridges into tools | `INDUS_SESSION_*`, … |
| `HERMES_TUI_*`, `HERMES_CWD`, `HERMES_PYTHON*`, `HERMES_NODE`, `HERMES_BIN`, `HERMES_HEAPDUMP_*`, `HERMES_AUTO_HEAPDUMP*`, `HERMES_DEV_*`, `HERMES_SLASH_*` | terminal UI | `INDUS_TUI_*`, … |
| `HERMES_GATEWAY_*`, `HERMES_MEDIA_*`, `HERMES_BACKGROUND_NOTIFICATIONS`, `HERMES_RESTART_*`, `HERMES_CRON_*`, `HERMES_LAUNCHD_LABEL`, `HERMES_TELEGRAM_*`, `HERMES_DISCORD_*`, `HERMES_FEISHU_*`, `HERMES_WECOM_*`, `HERMES_PEER_<NAME>_KEY` | gateway and cron | `INDUS_GATEWAY_*`, `INDUS_CRON_*`, `INDUS_TELEGRAM_*`, … |
| `HERMES_DASHBOARD_*`, `HERMES_SERVE_HEADLESS`, `HERMES_PTY_HOST`, `HERMES_WEB_*` | dashboard | `INDUS_DASHBOARD_*`, … |
| `HERMES_DESKTOP_*`, `HERMES_E2E_*`, `HERMES_SHARED_AUTH_DIR`, `HERMES_BUNDLE_ENV_JSON`, `HERMES_PAYLOAD_TAG`, `HERMES_BUILD_COMMIT`, `HERMES_RELEASE_EPOCH`, `HERMES_UPDATE_*`, `HERMES_PARENT_*`, `HERMES_BOOTSTRAP_STDIO_*` | desktop | `INDUS_DESKTOP_*`, … |
| `HERMES_RPC_*`, `HERMES_KERNEL_*`, `HERMES_DOCKER_BINARY`, `HERMES_EGRESS_*`, `HERMES_WRITE_SAFE_ROOT`, `HERMES_ALLOW_PRIVATE_URLS`, `HERMES_VISION_*`, `HERMES_LOCAL_STT_*`, `HERMES_CUA_DRIVER_CMD`, `HERMES_BROWSER_CONTROL_*`, `HERMES_NATIVE_FILE_READ`, `HERMES_DISABLE_*`, `HERMES_FORCE_*`, `HERMES_BD_*` | tools | `INDUS_RPC_*`, `INDUS_KERNEL_*`, … |
| `HERMES_KANBAN_*` | task board | `INDUS_KANBAN_*` (generic word kept; UI label is "task board") |
| `HERMES_INSTALL_*`, `HERMES_REPO_URL`, `HERMES_RUNTIME_DIR`, `HERMES_NIX_BUILD`, `HERMES_TEST_*`, `HERMES_LANGUAGE`, `HERMES_WEB_DIST`, `HERMES_TUI_DIR`, `HERMES_LIVE_USAGE_FILE` | install, build, tests | `INDUS_INSTALL_*`, `INDUS_REPO_URL`, … |
| `HERMES_CJK_FTS`, `HERMES_FTS5_CJK_SO`, `HERMES_SEARCH_SLOW_MS`, `HERMES_STATE_DB_GUARD_BYPASS`, `HERMES_STARTUP_WATCHDOG*`, `HERMES_TIMEZONE` | state store | `INDUS_CJK_FTS`, … |
| `HERMES_BUNDLED_SKILLS`, `HERMES_OPTIONAL_SKILLS`, `HERMES_OPTIONAL_MCPS`, `HERMES_ENABLE_PROJECT_PLUGINS`, `HERMES_HUB_*`, `HERMES_LANGFUSE_*`, `HERMES_NEMO_*` | learning loop, plugins | `INDUS_BUNDLED_SKILLS`, … |
| `HERMES_CUSTOM_<SLUG>_API_KEY` | generated custom-endpoint key slots | `INDUS_CUSTOM_<SLUG>_API_KEY` |
| `HERMES_NOUS_*`, `HERMES_PORTAL_*`, `NOUS_API_KEY`, `NOUS_INFERENCE_BASE_URL` | first-party portal | **none — dropped (P3)** |
| `_HERMES_GATEWAY`, `_HERMES_FORCE_<NAME>` | internal guards | `_INDUS_GATEWAY`, `_INDUS_FORCE_<NAME>` |
| `__HERMES_ACTIVATED`, `__HERMES_TEST_PYTHON`, `__HERMES_TESTS_REEXEC` | dev environment markers | `__INDUS_ACTIVATED`, … |
| Terminal bridge (unbranded, reference-chosen): `TERMINAL_ENV`, `TERMINAL_CWD`, `TERMINAL_TIMEOUT`, `TERMINAL_DOCKER_*`, `TERMINAL_SSH_*`, `TERMINAL_MODAL_*`, `TERMINAL_CONTAINER_*`, … | config → env bridge | `INDUS_TERMINAL_*` **lead?** (avoids clashing with other tools' `TERMINAL_*`) |
| Gateway-wide (unbranded): `GATEWAY_ALLOW_ALL_USERS`, `GATEWAY_ALLOWED_USERS`, `GATEWAY_MULTIPLEX_PROFILES`, `GATEWAY_RELAY_*` | gateway policy | `INDUS_GATEWAY_*` **lead?** |
| HTTP ingress (unbranded): `API_SERVER_KEY/HOST/PORT/CORS_ORIGINS/MODEL_NAME`, `WEBHOOK_ENABLED/PORT/SECRET` | API and webhook | `INDUS_API_SERVER_*`, `INDUS_WEBHOOK_*` **lead?** |
| Per-platform policy (unbranded): `<PLATFORM>_ALLOWED_USERS`, `_ALLOW_ALL_USERS`, `_ALLOW_BOTS`, `_HOME_CHANNEL`, `TELEGRAM_GROUP_ALLOWED_USERS/CHATS`, `DISCORD_ALLOWED_ROLES`, `DISCORD_REQUIRE_MENTION`, `DISCORD_FREE_RESPONSE_CHANNELS`, `WHATSAPP_DM_POLICY/GROUP_POLICY`, `TELEGRAM_CRON_THREAD_ID` | gateway access policy | `INDUS_<PLATFORM>_…` **lead?** (they are product policy, not vendor credentials) |
| Platform credentials: `TELEGRAM_BOT_TOKEN`, `DISCORD_BOT_TOKEN`, `SLACK_BOT_TOKEN`, … | vendor tokens | **unchanged (lead default)** |
| Provider credentials: `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, `GOOGLE_API_KEY`, `HF_TOKEN`, … | vendor keys | unchanged |
| Other unbranded: `SUDO_PASSWORD`, `BROWSER_CDP_URL`, `BROWSER_INACTIVITY_TIMEOUT` | tool settings | `INDUS_SUDO_PASSWORD`, `INDUS_BROWSER_CDP_URL`, `INDUS_BROWSER_INACTIVITY_TIMEOUT` **lead?** |
| Secret managers: `OP_SERVICE_ACCOUNT_TOKEN`, `BWS_ACCESS_TOKEN` | vendor tokens | unchanged |
| Vite build flag `VITE_PERF_PROBE`; build pins `BUILD_PIN_COMMIT`, `BUILD_PIN_BRANCH` | build | unchanged (generic) |
| Skill template tokens `${HERMES_SKILL_DIR}`, `${HERMES_SESSION_ID}` | skill preprocessing | `${INDUS_SKILL_DIR}`, `${INDUS_SESSION_ID}` |
| MCP install placeholder `${INSTALL_DIR}`; context vars `${workspaceFolder}` etc. | MCP config | unchanged (generic) |

### a4. Configuration keys

| Concept | Reference name (reading only) | Our name |
|---|---|---|
| Config sections | `model`, `agent`, `terminal`, `approvals`, `display`, `gateway`, `cron`, `curator`, … | unchanged (generic) |
| Portal section | `nous:` (`keepalive_interval_seconds`, `anthropic_wire`, `guest`), `portal:` | **none — dropped (P3)** |
| Hosted scheduler URL | `cron.chronos.portal_url` | none — dropped (P3) |
| Gateway health export service name | `service.name=hermes-gateway` | `service.name=indus-gateway` |
| Wake word | `wake_word.phrase: "hey hermes"`, model `hey_hermes` | `"hey indus"`, model `hey_indus` **lead?** |
| Per-install update channel | `update.installs.<id16>.channel` | unchanged (generic) |
| Packaging table | `[tool.hermes]`, `[tool.hermes.extras-platforms]` | `[tool.indus]`, `[tool.indus.extras-platforms]` |
| Skill frontmatter namespace | `metadata.hermes.*` | `metadata.indus.*` |
| Plugin manifest version floor | `requires_hermes` | `requires_indus` |
| Codex config managed block | "Hermes managed block" marker | "Indus managed block" |
| Desktop UI meta key | `ui_meta: hermes-bots`, `ui_meta['hermes-bots']` | `ui_meta: indus-bots` |
| Provider ids | `nous`, `nous-api` | none — dropped (P3) |
| Toolset names | `hermes-cli`, `hermes-acp`, `hermes-api-server`, `hermes-cron`, `hermes-gateway`, `hermes-webhook`, `hermes-<platform>` | `indus-cli`, `indus-acp`, `indus-api-server`, `indus-cron`, `indus-gateway`, `indus-webhook`, `indus-<platform>` |
| Core tool list constant | `_HERMES_CORE_TOOLS` | `CORE_TOOLS` |
| Gateway session-key namespace literal | `agent:main` (and `agent:main~` for a profile named like it) | `@default` **lead?** (`@` is outside the profile-id alphabet, so no `~` marker is needed) |

### a5. URL schemes, hosts, HTTP routes and headers

| Concept | Reference name (reading only) | Our name |
|---|---|---|
| Deep-link scheme | `hermes://` (dev `hermes-dev://`) | `indus://` (dev `indus-dev://`) |
| Skill install deep link | `hermes://skill/install?identifier=…` | `indus://skill/install?identifier=…` |
| Media scheme | `hermes-media://stream` | `indus-media://stream` |
| Docs host | `hermes-agent.nousresearch.com/docs/` | `<docs-host>/docs/` **lead?** (domain not chosen) |
| Asset mirror | `hermes-assets.nousresearch.com` | `<assets-host>` **lead?** |
| Portal, inference, welcome, telemetry hosts | `portal.nousresearch.com`, `inference-api.nousresearch.com/v1`, `welcome-api.nousresearch.com/v1`, `telemetry.nousresearch.com` | none — dropped (P3); telemetry endpoint **lead?** |
| Source repo | `github.com/NousResearch/hermes-agent` | `github.com/<owner>/indus-agent` |
| Installer script URL | `raw.githubusercontent.com/NousResearch/hermes-agent/<ref>/scripts/install.{sh,ps1}` | `raw.githubusercontent.com/<owner>/indus-agent/<ref>/scripts/install.{sh,ps1}` |
| Dashboard update routes | `/api/hermes/update`, `/api/hermes/update/check` | `/api/app/update`, `/api/app/update/check` |
| Dashboard plugin assets | `/dashboard-plugins/<name>/…` | unchanged (generic) |
| Other dashboard and gateway routes | `/api/*`, `/auth/*`, `/login`, `/v1/chat/completions`, `/v1/responses`, `/v1/models`, `/v1/runs/…`, `/webhooks/{route}`, `/p/{profile}/…`, `/health` | unchanged (generic or OpenAI-compatible) |
| Portal OAuth start route | `/api/providers/oauth/nous/start` | none — dropped (P3) |
| HTTP headers | `X-Hermes-Session-Id`, `X-Hermes-Session-Key`, `X-Hermes-Token`, `X-Hermes-Gateway-Ticket`, `X-Hermes-Session-Token`, `X-Hermes-Completed`/`Error`/`Partial` | `X-Indus-Session-Id`, `X-Indus-Session-Key`, `X-Indus-Token`, `X-Indus-Gateway-Ticket`, `X-Indus-Session-Token`, `X-Indus-Completed`/`Error`/`Partial` |
| WebSocket subprotocols | `hermes-gateway-v1`, `hermes-gateway-ticket.<t>`, `hermes-browser-control-v1`, `hermes-browser-control-ticket.` | `indus-gateway-v1`, `indus-gateway-ticket.<t>`, `indus-browser-control-v1`, `indus-browser-control-ticket.` |
| Dashboard cookies | `hermes_session_at`, `hermes_session_rt`, `hermes_session_provider`, `hermes_session_pkce`, `hermes_sso_attempt` | `indus_session_at`, `indus_session_rt`, `indus_session_provider`, `indus_session_pkce`, `indus_sso_attempt` |
| Dashboard window globals | `__HERMES_SESSION_TOKEN__`, `__HERMES_BASE_PATH__`, `__HERMES_AUTH_REQUIRED__`, `__HERMES_DASHBOARD_EMBEDDED_CHAT__`, `__HERMES_INITIAL_PROFILE__`, `__HERMES_DASHBOARD_PROFILE__`, `__HERMES_PLUGIN_SDK__`, `__HERMES_PLUGINS__` | `__INDUS_SESSION_TOKEN__`, … (same suffixes) |
| Browser storage keys | `hermes-sidebar-collapsed`, `hermes.lastLocation`, `hermes.pty.token.chat`, Web Lock `hermes.pty.attach.<token>` | `indus-sidebar-collapsed`, `indus.lastLocation`, `indus.pty.token.chat`, `indus.pty.attach.<token>` |
| Dashboard auth providers | `nous`, `basic`, `self-hosted`, `drain` | `basic`, `oidc`, `service-bearer` (portal provider dropped) |
| OAuth client id | `hermes-cli` | `indus-cli` |
| Nous OAuth scopes | `inference:invoke`, `billing:manage` | none — dropped (P3) |
| MCP server name / probe client | `hermes` | `indus` |
| ACP auth method id | `hermes-setup` | `indus-setup` |
| ACP agent info name and meta key | `hermes-agent`, `_meta.hermes.sessionProvenance` | `indus-agent`, `_meta.indus.sessionProvenance` |

### a6. RPC methods, events and IPC channels (pattern level)

| Concept | Reference name (reading only) | Our name |
|---|---|---|
| Gateway / TUI JSON-RPC families | `session.*`, `prompt.*`, `message.*`, `tool.*`, `reasoning.*`, `thinking.*`, `approval.*`, `clarify.*`, `config.*`, `model.*`, `commands.catalog`, `complete.*`, `slash.exec`, `command.dispatch`, `runtime.describe`, `gateway.*`, `subagent.*`, `cron.*`, `skills.*`, `profiles.*`, `projects.*`, `groups.*`, `mcp.servers.*`, `connectors.*`, `vault.*`, `voice.*`, `wake.*`, `pet.*`, `display.*`, `worker.*`, `kanban.run` | **unchanged** (generic dotted words; one declared contract, fresh code) |
| Vendor account families | `billing.*`, `subscription.*`, `free_tier.*`, `usage.bars`, `freeTierChallenge:run`, `cloud:*` | none — dropped (P3) |
| Shared metrics family | `shared_metrics.*`, `diagnostics.share_nous` | `telemetry.*` **lead?**; the portal-sharing flag is dropped |
| Native transport name | `hermes-gateway-v1` | `indus-gateway-v1` |
| Control-socket verbs | `identify`, `status`, `session-ticket`, `pause-for-update`, `rescan-profiles`, `serve-profile`, … | unchanged (generic) |
| Hook and plugin event names | `pre_tool_call`, `post_llm_call`, `gateway:startup`, `session:start`, `agent:step`, `command:*`, `session:compress`, … | unchanged (generic) |
| Plugin entry-point group | `hermes_agent.plugins` | `indus.plugins` **lead?** (spec 01 suggested `indus_agent.plugins`) |
| Plugin event namespace | `hermes` | `indus` |
| Metrics names | `hermes.compression.count`, `hermes.compaction`, telemetry route label `hermes` | `indus.compression.count`, `indus.compaction`, `indus` |
| Desktop bridge global | `window.hermesDesktop` | `window.indusDesktop` |
| Desktop IPC channels | prefix `hermes:` on all ~259 channels (`hermes:connection`, `hermes:fs:readDir`, `hermes:git:review:commit`, …) | prefix `indus:` with the same generic suffixes |
| Desktop main-process functions | `resolveHermesBackend`, `startHermes`, `windows-hermes-path.ts`, `src/hermes.ts` | `resolveBackend`, `startBackend`, `windows-path.ts`, `src/api.ts` |
| Desktop error class | `JsonRpcGatewayError` | `GatewayRpcError` |
| Server-request id and synthetic ids | `srq-<12hex>`, `sa:<index>:<goal>` | unchanged (generic) |
| Relay frames, browser frames | `descriptor`, `inbound`, `browser.controller.command`, … | unchanged (generic) |
| Error codes | per-method numbers reused with different meanings (4002, 4404, 5098, …) | one stable code table owned by the contract (J3, Added by Indus) |

### a7. Storage names

| Concept | Reference name (reading only) | Our name |
|---|---|---|
| Session database | `state.db` with tables for sessions, messages, prompts, usage, leases, ledger | unchanged file name; table names generic and kept |
| Other stores | `kanban.db`, `cron/jobs.json`, `cron/executions.db`, `pending/{memory,skills}/` | unchanged (generic) |
| Meta key prefixes | `gateway.*`, `db_file_generation`, `last_auto_prune`, … | unchanged (generic) |
| Log record attribute | `_hermes_session_injector` | `_indus_session_injector` |
| Logger names | `hermes_logging`, `hermes.mcp_serve` | module loggers (`logging.getLogger(__name__)`, configured by `indus.foundation.log`), e.g. `indus.mcp_serve` |
| Message content codec prefix | reserved JSON prefix | ours chosen fresh; reference store import is a separate decision (03 §7 Q4–Q5) |

### a8. Tool names the model sees

Generic tool names stay unchanged so models trained on common agent tool names keep working.

| Kept unchanged (ordinary words) | Renamed | Dropped |
|---|---|---|
| `terminal`, `process_manage`, `read_file`, `write_file`, `patch`, `search_files`, `web_search`, `web_extract`, `x_search`, `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, `browser_scroll`, `browser_back`, `browser_press`, `browser_get_images`, `browser_vision`, `browser_console`, `browser_cdp`, `browser_dialog`, `browser_vault`, `browser_exec`, `vision_analyze`, `video_analyze`, `image_generate`, `video_generate`, `text_to_speech`, `todo_list` (alias `todo`), `clarify`, `execute_code`, `delegate_task`, `cronjob_manage`, `memory`, `session_search`, `skills_list`, `skill_view`, `skill_manage`, `kanban_*` (15), `manage_connections`, `manage_catalog`, `computer_use`, `read_terminal`, `close_terminal`, `desktop_preview`, `drive_preview`, `annotate_preview`, `read_window_below`, `focus_pane`, `react_to_message`, `gui_tour`, `show_tip`, `set_theme`, `desktop_project`, `setup_beat`, `discord`, `discord_admin`, `feishu_doc_read`, `feishu_drive_*` (4), `tool_search`, `tool_describe`, `tool_call`, `mcp__<server>__<tool>`, `message_agent`; MCP serve tools `conversations_list`, `conversation_get`, `messages_read`, `attachments_fetch`, `events_poll`, `events_wait`, `messages_send`, `channels_list`, `permissions_list_open`, `permissions_respond` | `xai_video_edit` → `video_edit`; `xai_video_extend` → `video_extend` (vendor-neutral; the backend picks the provider) **lead?**; `yb_query_group_info`, `yb_query_group_members`, `yb_send_dm`, `yb_search_sticker`, `yb_send_sticker` → `yuanbao_query_group_info`, `yuanbao_query_group_members`, `yuanbao_send_dm`, `yuanbao_search_sticker`, `yuanbao_send_sticker` (spell the platform out); in-script module `hermes_tools` (visible inside `execute_code`) → `indus_tools` | none |

### a9. Slash commands and CLI subcommands

All generic slash command names are kept (`/new`, `/retry`, `/undo`, `/compress`, `/model`,
`/queue`, `/steer`, `/goal`, `/loop`, `/heartbeat`, `/learn`, `/refine`, `/curator`, `/journey`,
`/yolo`, `/approvals`, `/skin`, `/pet`, `/hatch`, `/kanban`, `/sethome`, …).

| Concept | Reference name (reading only) | Our name |
|---|---|---|
| Portal account commands | `/topup`, `/subscription` (alias `/upgrade`), `/login` (portal binding), TUI `/billing`, `/credits` | dropped (P3) |
| Theme command | `/skin` | `/theme` as the primary name, `/skin` kept as an alias **lead?** |
| CLI subcommands, generic | `chat`, `model`, `fallback`, `setup`, `auth`, `config`, `tools`, `sessions`, `gateway`, `cron`, `skills`, `plugins`, `mcp`, `profile`, `update`, `doctor`, `backup`, … | unchanged |
| Portal subcommands | `portal`, `setup --portal`, `auth upgrade`, `login` (hidden), `logout --provider nous` | dropped (P3) |
| OpenClaw migration | `claw migrate`, `claw cleanup` | `import-openclaw` **lead?** (spell out what it does) |
| Installer flags | `--hermes-home`, PowerShell `-HermesHome` | `--indus-home`, `-IndusHome` |
| Desktop launcher flags | `hermes desktop --hermes-root` | `indus desktop --indus-root` |

### a10. Skills, plugins and manifests

| Concept | Reference name (reading only) | Our name |
|---|---|---|
| Skill manifest | `SKILL.md` (agentskills.io convention) | unchanged (open standard) |
| Plugin manifest | `plugin.yaml`, `plugin.json` | unchanged (generic) |
| Plugin pack file | `hermes-pack.yaml` | `indus-pack.yaml` |
| Gateway hook manifest | `HOOK.yaml` + `handler.py` | unchanged (generic) |
| Profile distribution manifest | `distribution.yaml` | unchanged (generic) |
| Optional MCP manifest | `optional-mcps/<name>/manifest.yaml` | unchanged (generic) |
| Self-guide skill | `hermes-agent` | `indus-guide` |
| Skill-authoring skill | `hermes-agent-skill-authoring` | `skill-authoring` |
| Desktop DOM skill | `inspecting-hermes-desktop-dom` | `inspecting-desktop-dom` |
| Container supervision skill | `hermes-s6-container-supervision` | `container-supervision` |
| Bundled achievements plugin | `hermes-achievements` | `achievements` |
| Plugin-section markers | `<!-- hermes-plugin-sections:start -->` / `:end` | `<!-- indus-plugin-sections:start -->` / `:end` |
| Official plugin repos | `NousResearch/hermes-official-plugins`, `hermes-example-plugins` | `<owner>/indus-plugins`, `<owner>/indus-example-plugins` **lead?** |
| Skill scanner id | `skills-guard-v10` | `indus-scan-v1` |
| Hosted scheduler provider | `chronos` | none — dropped (P3); third-party providers use a neutral `scheduler_provider` seam |

### a11. Logs, services, containers and packaging

| Concept | Reference name (reading only) | Our name |
|---|---|---|
| Log files | `agent.log`, `errors.log`, `gateway.log`, `gui.log`, `desktop.log`, `mcp-stderr.log`, `update.log`, `install.log`, `dashboard-auth.log`, `gateway-startup-watchdog.log`, `gateway_faulthandler.log`, `tool_calls.log` | unchanged (generic) |
| Log line prefix | `[hermes]` | `[indus]` |
| systemd unit | `hermes-gateway.service`, `hermes-gateway-<hash>.service` | `indus-gateway.service`, `indus-gateway-<hash>.service` |
| launchd label | `ai.hermes.gateway` | `ai.indus.gateway` **lead?** (reverse-DNS depends on the domain) |
| Windows scheduled task / script dir | `Hermes_Gateway`, `gateway-service/*.vbs` | `Indus_Gateway`, `gateway-service/*.vbs` |
| Windows named pipe | `\\.\pipe\hermes-gateway-<hash>` | `\\.\pipe\indus-gateway-<hash>` |
| Gateway lock dir | `$XDG_STATE_HOME/hermes/gateway-locks` | `$XDG_STATE_HOME/indus/gateway-locks` |
| Docker image | `nousresearch/hermes-agent` (`:main`, `:main-desktop`, `:latest`, `:stable`, `:test`), `ghcr.io/nousresearch/hermes-agent` | `<registry>/indus-agent` with the same tag scheme **lead?** (registry not chosen) |
| Default sandbox image | `nousresearch/hermes-sandbox:desktop` | `<registry>/indus-sandbox:desktop` (built by us) |
| Docker labels | `hermes-agent`, `hermes-task-id`, `hermes-profile`, `hermes-egress`, `hermes-environment` | `indus-agent`, `indus-task-id`, `indus-profile`, `indus-egress`, `indus-environment` |
| Container / instance names | `hermes-<hex8>`, Singularity `hermes_<hex12>`, Daytona `hermes-<task>`, label `hermes_task_id` | `indus-<hex8>`, `indus_<hex12>`, `indus-<task>`, `indus_task_id` |
| In-band shell markers | `__HERMES_CWD_<id>__`, `HERMES_STDIN_<hex>`, `__HERMES_FENCE_*__`, `__HERMES_RF_`, `__HERMES_WF_`, `__HERMES_RB_`, `__hermes_not_regular__`, `__hermes_missing__` | `__INDUS_CWD_<id>__`, `INDUS_STDIN_<hex>`, `__INDUS_FENCE_*__`, `__INDUS_RF_`, `__INDUS_WF_`, `__INDUS_RB_`, `__indus_not_regular__`, `__indus_missing__` |
| Image paths and user | `/opt/hermes`, `/opt/data`, Unix user/group `hermes` (UID 10000) | `/opt/indus`, `/opt/data`, user/group `indus` (UID 10000) |
| s6 service and Compose services | `main-hermes`; `hermes`, `hermes-dashboard` | `main-indus`; `indus`, `indus-dashboard` |
| Container helper scripts | `docker/hermes-exec-shim.sh`, `scripts/hermes-gateway`, `scripts/run-in-hermes-env` | `docker/indus-exec-shim.sh`, `scripts/indus-gateway`, `scripts/run-in-indus-env` |
| Nix | `services.hermes-agent`, `nix/hermes-agent.nix`, `.#pm-<tool>` | `services.indus-agent`, `nix/indus-agent.nix`, `.#pm-<tool>` |
| APT | suites `hermes-stable`, `hermes-canary`; package `hermes-agent`; keyring `hermes-agent.asc` | `indus-stable`, `indus-canary`; `indus-agent`; `indus-agent.asc` |
| Desktop app ids | `com.nousresearch.hermes*`, `com.nousresearch.hermes.setup` | `ai.indus.desktop*`, `ai.indus.setup` **lead?** |
| MSIX identities | `NousResearch.Hermes*`, `NousResearchInc.HermesAgent` | `<Publisher>.IndusAgent` **lead?** |
| Release artefacts | `Hermes.exe`, `Hermes-Setup.dmg`, `Hermes.app` | `Indus.exe`, `Indus-Setup.dmg`, `Indus.app` |
| Keychain item / cert name | `Hermes Key`, "Hermes Local Signing" | `Indus Key`, "Indus Local Signing" |
| Webview partition | `persist:hermes-preview` | `persist:indus-preview` |
| Desktop boot-theme cache keys | `hermes-boot-*` | `indus-boot-*` |
| Theme preset names | `nous`, `nous-alt`, "Hermes Teal", "Hermes Teal (Large)", "Nous Blue" | fresh names, e.g. `indus`, `indus-alt`, "Indus River", "Indus River (Large)", "Monsoon" **lead?** |
| Dashboard brand fonts | Mondwest, Collapse, Rules Compressed/Expanded | not shipped; open-licence fonts chosen by us (L20) |
| Images | `nous-girl*`, `hermes.png`, `hermes-sprite.png`, `hermes-frames/`, `nous-dmg-2b.tiff` | all assets drawn fresh |
| Model-facing tokens | `[CRON_FAILURE]`, `wakeAgent`, `[SKILL_PRUNED: …]`, `<REASONING_SCRATCHPAD>`, `MINI_SWE_AGENT_FINAL_OUTPUT`, "Hermes format" | `[JOB_FAILED]`, `wake`, `[skill elided: …]`, no scratchpad tag (only `<think>`), `INDUS_TASK_DONE`, "tool-tag format" **lead?** |
| Model-facing tokens kept | `[SILENT]`, `SILENT`, `NO_REPLY`, `MEDIA:<path>`, `<tools>`, `<tool_call>`, `<tool_response>`, `<think>` | unchanged (generic or open convention) |
| Test markers and fixture names | `live_system_guard_bypass`, `platforms(...)`, … | unchanged (generic) |

---

## (b) UI vocabulary changes

All copy is written fresh (lead default). These are the words we change on purpose; everything
else keeps the plain name the feature already has.

| Reference word | Indus word | Why |
|---|---|---|
| "SOUL" (persona file and editor) | **Persona** | Says what the file is: the agent's identity text. The old name is a brand flourish. |
| "Bot Mode", "Bots tab" | **Bots** | One noun is enough; "mode" suggested a global switch, but bots are just profiles with a face. |
| "Bot Chat" (canonical chat title) | **Main chat** | Every bot has exactly one long-running conversation; "main" tells the user which one it is. |
| YOLO (badge, setting label) | **Auto-approve** | Plain language for a safety setting; the `--yolo` flag and `/yolo` command stay as aliases. |
| Skin (TUI) / theme (desktop, web) | **Theme** everywhere | One word across surfaces; `/skin` remains an alias. |
| Learning journey, star map, memory graph | **Learning timeline** (list view) and **Learning map** (graph view) | Two views of the same data deserve two clear names instead of three playful ones. |
| Kanban | **Task board** (UI label) | Describes the purpose; the tool names keep the generic `kanban_` prefix. |
| Hosted rooms / group rooms | **Group rooms** | "Hosted" referred to where the room runs, which users do not need to know. |
| Boot statuses "summoning", "forging session…" | "Starting…", "Opening session…" | Plain progress words; playful copy is not carried over. |
| Busy faces (kaomoji set), fortunes, tool "charms" | Indus's own indicator set and messages | Lead default: playful strings are written fresh. |
| Nous Portal, Tool Gateway, Hermes Cloud, free tier, credits, top-up | (removed) | Vendor services dropped by decision P3. |
| "Smart" approvals | **Smart** (kept) with the tooltip "a helper model screens commands first" | Kept as the config value; the explanation is new. |

---

## (c) Domain glossary

| Term | Meaning in Indus Agent |
|---|---|
| **Agent** | The long-lived engine object that owns a model route, tool selection, callbacks and the cached system prompt; one agent can serve many turns. |
| **Turn** | Handling one user message until a final answer (or a failure). A turn contains one or more iterations. |
| **Iteration** | One model request (with its own retries) plus, if the model asked for tools, one tool round. |
| **Tool round** | Validating, executing and recording the tool calls from one model response, results in call order. |
| **Route** | The tuple provider, model, base URL, API mode and credential used for a request. |
| **Provider profile** | A declarative description of a model vendor or gateway: how to authenticate, which API mode, which models. |
| **API mode / transport** | The wire format a request uses (Chat Completions, Responses, Messages, Converse) and the code that converts to and from it. |
| **Fallback chain** | Ordered provider+model pairs tried after the primary keeps failing; reset at the next turn. |
| **Credential pool** | Several keys or grants for one provider, rotated before falling back. |
| **Auxiliary model** | The model used for side tasks (summaries, titles, vision, screening) instead of the main one. |
| **Wire copy / durable transcript** | The cleaned per-request copy sent to the provider vs. the message list saved in the store. |
| **System prompt tiers** | Stable, context and volatile layers of the prompt, built once and kept byte-identical for cache hits. |
| **Persona file** | `PERSONA.md` in the profile home: the agent's identity text, first in the prompt. |
| **Project context file** | `.indus.md`/`INDUS.md` (or `AGENTS.md`, …) found from the working directory and added to the prompt. |
| **Steer** | Guidance typed during a turn, delivered after the current tool batch without stopping the turn. |
| **Redirect** | A correction that cancels the in-flight model call and restarts it with the correction. |
| **Interrupt** | Stopping the current turn; finished work is kept, unstarted tools are skipped. |
| **Compression / compaction** | Replacing the middle of a long conversation with a summary so it fits the context window. |
| **Session** | One stored conversation with its messages, title, usage and workspace. |
| **Lineage** | The chain of sessions created by compression continuations, branches and resets that the user sees as one conversation. |
| **Branch** | An independent copy of a session from a chosen point. |
| **Rewind / undo** | Hiding the last N user turns (and what followed) so the conversation continues from earlier. |
| **Profile** | An independent agent home (config, secrets, memory, skills, sessions, gateway); the default profile is the home root. |
| **Home** | The directory a profile lives in (`~/.indus` or `~/.indus/profiles/<name>`). |
| **Toolset** | A named bundle of tools, possibly including other toolsets; selected per surface or per run. |
| **Posture** | A per-session toolset preset such as "coding" or "safe". |
| **Terminal backend / environment** | Where shell commands run: local host, Docker, SSH, Singularity, Modal, Daytona, Vercel Sandbox, or a plugin. |
| **Spill** | Writing an oversized tool result to a file and giving the model a preview plus the path. |
| **Checkpoint** | An automatic snapshot of the working directory taken before the first change in a turn, used for rollback. |
| **Approval** | The human (or screened) decision to run a flagged command: once, for the session, always, or deny. |
| **Hardline** | Commands that are always refused, whatever the approval mode. |
| **Auto-approve** | The per-session switch that skips approval prompts (alias `yolo`). |
| **Memory store** | One of two small text files (agent notes, user profile) shown to the agent at session start. |
| **Skill** | A folder with a `SKILL.md` procedure and optional support files that the agent can load and edit. |
| **Skill bundle** | A named list of skills loaded together by one slash command. |
| **Review fork** | A background copy of the agent that reads a finished turn and saves memory or improves skills. |
| **Curator** | The idle-time job that ages unused agent-made skills to stale and then archived, and can consolidate them. |
| **Skills hub** | The installer that finds skills in online sources, scans them and records where they came from. |
| **Plugin** | A Python package with a manifest that adds tools, hooks, commands, skills or providers. |
| **Hook** | A named point where plugins (or shell commands) observe or change what the agent is doing. |
| **MCP server / MCP client** | A Model Context Protocol tool server, and Indus's side that connects to such servers. |
| **Gateway** | The always-on process that connects messaging platforms, runs scheduled jobs and serves the HTTP API. |
| **Adapter / platform** | The gateway's connector for one messaging service, and the service itself. |
| **Home channel** | The chat on a platform where scheduled results and notices go by default. |
| **Pairing** | Approving an unknown sender with a short one-time code. |
| **Session key** | The gateway's identifier for a chat slot (platform, chat type, chat, thread, user). |
| **Delivery ledger** | The gateway's durable record of final replies so a crash cannot lose one silently. |
| **Delivery target** | Where a scheduled job's output goes: origin, local, a platform, a specific chat, or all. |
| **Cron job** | A scheduled prompt or script that runs in a fresh session and delivers its result. |
| **Silence token** | A reply consisting only of `[SILENT]`; nothing is sent. |
| **Task board** | The durable board where several profiles hand work to each other (reference: kanban). |
| **Shared daemon** | The later-phase per-profile gateway process that every surface talks to (decision P1). |
| **Admission** | A durable record that an input was accepted; it is run at most once and becomes "unknown" if its owner died. |
| **Execution generation** | A counter that ties controls (stop, approve) to one specific run so a late click cannot hit the next one. |
| **ACP** | Agent Client Protocol: how code editors talk to Indus as an external agent. |
| **Dashboard** | The browser admin console served by `indus dashboard`, with an embedded terminal chat. |
| **Trajectory** | A conversation converted to a training sample with tool-call tags. |
| **Toolset distribution** | A table of odds used to pick toolsets per prompt when generating trajectories. |
| **PM** | Indus's package/environment manager that installs pinned helper binaries and Python environments. |

---

## (d) Banned identifiers

Patterns for `tooling/checks/banned_patterns.json`. Each line is `<regex>` then two spaces, `#`,
and the hint the check prints. Patterns are matched case-sensitively unless they start with
`(?i)`. The first pattern already catches every `hermes` spelling; the narrower ones follow so the
check can print a precise hint. Generic words (session, gateway, curator, kanban, journey,
`SKILL.md`, `MEDIA:`) are deliberately absent.

```text
(?i)hermes  # reference product name: use "indus" / "Indus Agent"
HERMES_[A-Z0-9_]*  # reference env prefix: use INDUS_<same suffix>
_{1,2}HERMES_  # reference internal env prefix: use _INDUS_ / __INDUS_
(?i)\.hermes(\.md|_history)?\b  # reference home or context file: use ~/.indus, .indus.md, input_history
(?i)hermes[-_](agent|cli|acp|tui|state|constants|platform|logging|gateway|tools|ink|canary|pm)  # reference package/binary name: use the indus equivalent (17-naming §a1)
(?i)hermes(-dev|-media)?://  # reference URL scheme: use indus:// / indus-dev:// / indus-media://
X-Hermes-  # reference HTTP header prefix: use X-Indus-
hermesDesktop  # reference desktop bridge global: use window.indusDesktop
'hermes:  # reference desktop IPC channel prefix: use 'indus:
(?i)\bnous\b  # reference vendor name: remove (portal dropped, P3) or use a neutral word
(?i)nous[-_ ]?research  # reference vendor/org name: remove
(?i)\bnous[-_](portal|api|girl|wire|rate|blue|alt|research)  # reference vendor identifiers: remove
NOUS_(API_KEY|INFERENCE_BASE_URL)  # reference portal credentials: dropped (P3)
(?i)nousresearch\.com  # reference vendor hosts: use Indus hosts or drop
\bSOUL\.md\b  # reference persona file: use PERSONA.md
(?i)\bsoul[_-](md|file|path)\b  # reference persona naming: use persona_*
☤  # reference banner glyph: use Indus's own mark
(?i)caduceus  # reference brand motif: use Indus's own mark
\bAIAgent\b  # reference agent class name: use Agent
\b(Async)?SessionDB\b  # reference store class names: use SessionStore / AsyncSessionStore
\bRuntimeSessionStore\b  # reference runtime store class: use RuntimeLedger
\bJsonRpcGatewayError\b  # reference desktop error class: use GatewayRpcError
_HERMES_CORE_TOOLS  # reference constant: use CORE_TOOLS
\bxai_video_(edit|extend)\b  # reference vendor-branded tool names: use video_edit / video_extend
\byb_(query_group_info|query_group_members|send_dm|search_sticker|send_sticker)\b  # reference abbreviated tool names: use yuanbao_*
\bskills-guard-v\d+\b  # reference scanner id: use indus-scan-v1
\bchronos\b  # reference hosted scheduler provider: dropped (P3); use scheduler_provider
REASONING_SCRATCHPAD  # reference in-content reasoning tag: use <think> only
MINI_SWE_AGENT_FINAL_OUTPUT  # reference completion sentinel: use INDUS_TASK_DONE
\[CRON_FAILURE\]  # reference cron failure token: use [JOB_FAILED]
\bwakeAgent\b  # reference wake-gate key: use wake
SKILL_PRUNED  # reference compaction marker: use [skill elided: …]
\bOOF-\d+\b  # reference issue id: do not cite reference tickets
\bNS-\d{3,}\b  # reference issue id: do not cite reference tickets
\bMondwest\b  # reference brand font: use our own open-licence font
Rules (Compressed|Expanded)  # reference brand font: use our own open-licence font
(?i)hey[ _]hermes  # reference wake phrase: use "hey indus"
(?i)tool[ -]gateway.*nous|nous.*tool[ -]gateway  # reference hosted tool service: dropped (P3)
```

That is **38** patterns.

---

## (e) Proposals that need the lead's confirmation

1. Persona file `PERSONA.md` (reference: `SOUL.md`). — **accepted — lead, 2026-10-18** (D-16.65).
2. Project context files `.indus.md` / `INDUS.md`. — **accepted — lead, 2026-10-18** (D-16.65).
3. Legacy single-query runner named `indus-run`. — **superseded**: the runner is not built (D-16.85).
4. Unbranded reference-chosen env vars move under `INDUS_`: `TERMINAL_*` → `INDUS_TERMINAL_*`,
   `GATEWAY_*` → `INDUS_GATEWAY_*`, `API_SERVER_*` / `WEBHOOK_*` → `INDUS_API_SERVER_*` /
   `INDUS_WEBHOOK_*`, per-platform policy vars (`<PLATFORM>_ALLOWED_USERS`, …) →
   `INDUS_<PLATFORM>_…`, `SUDO_PASSWORD` / `BROWSER_CDP_URL` / `BROWSER_INACTIVITY_TIMEOUT` →
   `INDUS_…`. Vendor credentials stay unprefixed as already decided. — Per-platform policy vars
   `INDUS_<PLATFORM>_…`: **accepted — lead, 2026-10-18** (D-16.91). `TERMINAL_*`: **superseded** — no
   env bridge at all, terminal settings come from config (D-16.75). The other renames stay proposed.
5. Plugin entry-point group `indus.plugins`. — **accepted — lead, 2026-10-18** (D-16.39).
6. Gateway session-key namespace literal `@default`. — **accepted — lead, 2026-10-18** (D-16.90).
7. Vendor-neutral tool names `video_edit` / `video_extend`; `yb_*` spelled out as `yuanbao_*`.
8. `/theme` as the primary command with `/skin` as alias; OpenClaw migration command
   `import-openclaw`.
9. Domain-dependent names: docs and asset hosts, Docker registry, launchd label
   (`ai.indus.gateway`), desktop app ids (`ai.indus.*`), MSIX publisher identity, official plugin
   repos, telemetry endpoint and `telemetry.*` RPC family. — Still **owner-open**: desktop app ids
   (`ai.indus.desktop`), GHCR owner and docs domain are tracked in the P25-20 release checklist;
   the Windows installer is signed NSIS (D-16.70), so no MSIX publisher identity is needed for 1.0.
10. Fresh theme preset names and wake phrase ("hey indus").
11. Model-facing tokens `[JOB_FAILED]`, `INDUS_TASK_DONE`, `[skill elided: …]`, `wake`, and no
    scratchpad tag.
12. Remote-only desktop variant called "Indus Remote". — **moot for 1.0**: one desktop variant (D-16.48).
13. Loop completion token `LOOP_COMPLETE` kept as a generic word. — **accepted — lead, 2026-10-18** (D-16.92).
