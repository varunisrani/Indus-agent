# 09 — Web UI, editor (ACP) adapter, MCP server mode, platform layer: behavioural specification (clean-room)

Scope: the browser dashboard (what it is, routes and screens, how it is served and authenticated, the
HTTP/WebSocket API it consumes), the Agent Client Protocol (ACP) adapter that lets editors drive the
agent over stdio, the "run the agent as an MCP server" mode, and the platform layer (machine facts,
tool/app resolution, OS paths, Windows specifics). Out of scope: the agent loop (`01-agent-loop.md`),
the tools themselves (`02-tools.md`), the CLI command tree (`04-cli.md`), the terminal UI that the
dashboard embeds (`05-tui.md`), the desktop app (`08-desktop.md`). The messaging gateway daemon that
the ACP adapter talks to is described here only from the adapter's side.

Reference: the reference repo @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056` (MIT). Shipped build
checked: none (REA not connected this session; source reading only).
Sources studied: `web/` (React SPA), the dashboard backend it talks to (`<cli pkg>/web_server*.py`,
`<cli pkg>/web_routers/*.py`, `<cli pkg>/dashboard_auth/*`, `<cli pkg>/pty_*.py`), `acp_adapter/`,
`mcp_serve.py`, the platform package (`<platform pkg>/`), `tests-js/`, `eslint.config.shared.mjs`,
root `package.json`; docs pages "web-dashboard", "extending-the-dashboard", "acp", "acp-internals",
"mcp" (server section), "windows-native".

Conventions used below:
- "App" = the rebuilt product (CLI `indus`, env prefix `INDUS_`). "Home" = the per-profile data
  directory (`<home>`; default `~/.indus` on POSIX). "Profile" = one named home.
- "Dashboard" = the browser SPA plus the HTTP server that serves it. "Backend" = that HTTP server.
- "Gateway" (or "owner") = the long-running per-profile daemon that owns sessions and executes turns.
- "Viewer" = a client process that shows a gateway-owned session (the ACP adapter is one).
- "Session token" = the per-process secret injected into the SPA HTML in loopback mode.
- "Gated mode" = the backend requires a login (cookie session) because it is reachable off-host.
- "MUST" marks an invariant the reference defends (defensive code, a test, or an issue number).
- Evidence tags: [obs] read in source/docs; [inf] inferred from source; [unk] not settled.
- Path placeholders: `<cli pkg>` and `<platform pkg>` stand for the reference's CLI package and
  platform package directories (their real names are in section 2, Files and folders).

---

## 1. Overview

This area is four loosely coupled surfaces that all sit "outside" the agent loop:

1. **Dashboard (web UI).** A single-page React app (Vite build, Tailwind) served by a Python
   (FastAPI/uvicorn) backend on `127.0.0.1:9119` by default [obs]. It is primarily an
   **administration console** — config editor, API keys, sessions browser, logs, analytics, cron,
   skills, plugins, MCP servers, messaging channels, webhooks, pairing, profiles, models, files,
   system ops — with one **Chat** tab that embeds the real terminal UI (the same TUI binary as
   `indus --tui`) inside xterm.js over a PTY-backed WebSocket [obs]. The chat transcript and composer
   are deliberately NOT re-implemented in React; React only adds side panels around the terminal
   [obs]. The same backend, started headless (`serve` mode, SPA disabled), is what the desktop app
   talks to [obs].
2. **ACP adapter.** A stdio JSON-RPC server implementing the Agent Client Protocol so editors (Zed,
   VS Code ACP extension, JetBrains plugins, and collaboration hosts) can open sessions, send
   prompts, receive streamed message/thought/tool updates, and answer permission prompts [obs]. In
   the pinned version the adapter is a **viewer** of the profile's gateway daemon: it does not run
   the agent in-process; it forwards to the gateway over a local RPC connection and projects the
   gateway's event stream into ACP `session/update` notifications [obs]. An older in-process
   implementation still exists in the same package and is used only when constructed explicitly
   (tests) [obs].
3. **MCP server mode.** `indus mcp serve` starts a stdio MCP server exposing 10 tools that let
   another agent (a coding assistant) list messaging conversations, read history, long-poll new
   messages, send messages through connected platforms, and (nominally) answer approvals [obs]. It
   reads the session database directly and polls it every 200 ms [obs].
4. **Platform layer.** A side-effect-free library answering "what machine is this" (OS family,
   native vs process architecture, CPU, RAM, GPU class, WSL/container/Termux, interactive session)
   and "where is tool X / app Y and what state is it in" (three-tier resolver: locate → inspect →
   probe), plus the declarative "app requirement" parser used to gate catalog MCP servers and skills
   [obs]. All hand-written known-directory tables live here and nowhere else [obs].

Who calls whom:

```
 Browser SPA ──REST /api/*──────────────▶ Backend (FastAPI) ──▶ config / .env / state DB / gateway ctl
     │  └─WS /api/pty (raw PTY bytes) ──▶ PTY registry ──spawns──▶ TUI process ──▶ agent
     │  └─WS /api/ws  (JSON-RPC sidecar)─▶ TUI-gateway dispatcher (same protocol the TUI uses on stdio)
     │  └─WS /api/events (subscribe) ◀── fan-out ◀── WS /api/pub ◀── TUI child publishes its emits
     │  └─WS /api/console (admin REPL)
 Editor ──ACP stdio──▶ ACP viewer process ──local RPC──▶ profile gateway (owns sessions, runs turns)
 Coding agent ──MCP stdio──▶ MCP bridge ──reads──▶ state DB ;  ──send──▶ messaging send engine
 Everything ──▶ platform layer (host facts, resolvers)
```

What the user sees: `indus dashboard` opens a browser on the sessions list; a sidebar of ~20 pages;
a Chat tab with a terminal on the left and a narrow right rail (model picker, session switcher,
workspace picker, live tool feed). Editors show "Indus Agent" as an external agent with a model
picker, three-option approval cards and streamed diffs. MCP clients see ten bridge tools.

What the system guarantees: loopback dashboards need no login but every `/api/*` call carries the
per-process session token; any non-loopback exposure fails closed unless a login provider is
configured; the PTY child survives tab switches and brief disconnects; ACP never resends a turn
whose outcome is unknown; the platform layer never spawns, downloads or installs during lookup.

## 2. Reference names (rename in rebuild)

| Kind | Names (reference only; they become banned identifiers) |
|---|---|
| Binary / CLI and flags | `hermes dashboard` (`--port`, `--host`, `--no-open`, `--insecure`, `--isolated`, `--open-profile`), `hermes serve`, `hermes web`, `hermes dashboard register` (`--name`, `--redirect-uri`), `hermes acp` (`--version`, `--check`, `--setup`, `--setup-browser`, `--yes`), `hermes-acp`, `python -m acp_adapter`, `hermes mcp serve` (`--verbose`), `hermes --tui`, `hermes -p <name>`, `hermes pm repair` |
| Env vars | `HERMES_HOME`, `HERMES_DATA_DIR_SUFFIX`, `HERMES_DASHBOARD_SESSION_TOKEN`, `HERMES_DASHBOARD_URL`, `HERMES_DASHBOARD_PUBLIC_URL`, `HERMES_DASHBOARD_OAUTH_CLIENT_ID`, `HERMES_DASHBOARD_OIDC_ISSUER`, `HERMES_DASHBOARD_OIDC_CLIENT_ID`, `HERMES_DASHBOARD_OIDC_SCOPES`, `HERMES_DASHBOARD_BASIC_AUTH_USERNAME`, `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD`, `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH`, `HERMES_DASHBOARD_BASIC_AUTH_SECRET`, `HERMES_DASHBOARD_BASIC_AUTH_TTL_SECONDS`, `HERMES_DASHBOARD_DRAIN_SECRET`, `HERMES_DASHBOARD_FILES_ROOT`, `HERMES_SERVE_HEADLESS`, `HERMES_PTY_HOST`, `HERMES_TUI_RESUME`, `HERMES_TUI_SIDECAR_URL`, `HERMES_ENABLE_PROJECT_PLUGINS`, `HERMES_WEB_BUILD_MAX_OLD_SPACE_SIZE`, `HERMES_WEB_BUILD_THREADS`, `HERMES_WEB_BUILD_LIGHT`, `HERMES_ACP_SKIP_CONFIGURED_MCP`, `HERMES_MODEL`, `HERMES_INFERENCE_MODEL`, `HERMES_INFERENCE_PROVIDER`, `HERMES_DESKTOP_REMOTE_URL`, `HERMES_DISABLE_WINDOWS_UTF8`, `HERMES_GIT_BASH_PATH` |
| Files and folders | `~/.hermes`, `~/.hermes*`, `%LOCALAPPDATA%\hermes*`, `hermes_cli/web_dist/`, `~/.hermes/dashboard-themes/`, `~/.hermes/plugins/<name>/dashboard/{manifest.json,dist/index.js,dist/style.css,plugin_api.py}`, `./.hermes/plugins/`, `state.db`, `sessions/sessions.json`, `channel_directory.json`, `logs/dashboard-auth.log`, `~/.hermes/bin`, `~/.hermes/node/`, machine marker `Nous Research/.machine`, `nous-research/.machine`, service globs `ai.hermes.gateway*.plist`, `hermes-gateway*.service`, `Hermes_Gateway*`, `.hermes-ssh-runtime-<nonce>`, package dirs `acp_adapter/`, `hermes_platform/`, `hermes_cli/`, `tui_gateway/`, `apps/shared` |
| HTTP routes / URL schemes | `/dashboard-plugins/<name>/…`, `/dashboard-plugins/@<profile>/…`, `/api/hermes/update`, `/api/hermes/update/check`; docs URL `hermes-agent.nousresearch.com/docs/`; Portal URLs `portal.nousresearch.com/…` (all other `/api/*`, `/auth/*`, `/login` paths are generic and may be kept) |
| RPC methods / events / commands | ACP auth method id `hermes-setup`; MCP server name `hermes`; MCP probe client name `hermes`; WS subprotocols `hermes-gateway-v1`, `hermes-gateway-ticket.<t>`; gateway capability strings `acp-editor-policy-v1`, `acp-session-mcp-v1`; console prompt `hermes> `; gateway RPC verbs used by the ACP viewer: `runtime.describe`, `session.create`, `session.resume`, `session.info`, `session.detach`, `session.stats`, `session.mutate`, `session.interrupt`, `session.steer`, `prompt.submit`, `prompt.cancel`, `prompt.receipt`, `prompt.resolve_unknown`, `approval.respond`, `clarify.respond`, `config.set`, `slash.exec`; gateway events `message.delta`, `message.interim`, `message.complete`, `tool.start`, `tool.complete`, `reasoning.delta`, `todo.updated`, `approval.request`, `approval.settled`, `clarify.request`, `clarify.settled`, `session.info`, `session.updated`, `session.usage`, `session.tools_changed`, `session.replay_gap` |
| Settings and storage keys | window globals `__HERMES_SESSION_TOKEN__`, `__HERMES_BASE_PATH__`, `__HERMES_AUTH_REQUIRED__`, `__HERMES_DASHBOARD_EMBEDDED_CHAT__`, `__HERMES_INITIAL_PROFILE__`, `__HERMES_DASHBOARD_PROFILE__`, `__HERMES_PLUGIN_SDK__`, `__HERMES_PLUGINS__`; localStorage/sessionStorage `hermes-sidebar-collapsed`, `hermes.lastLocation`, `hermes.pty.token.chat`; Web Lock name `hermes.pty.attach.<token>`; config keys `dashboard.*` (`theme`, `font`, `public_url`, `trusted_proxies`, `basic_auth.*`, `oauth.*`, `drain_auth.*`, `show_token_analytics`), `platform_toolsets.acp`, `agent.disabled_toolsets`, `approvals.timeout`, `desktop.repo_scan_roots`, `telemetry.shared_metrics` |
| Other (scopes, error tags, IPC channels) | HTTP headers `X-Hermes-Session-Token`, `X-Hermes-Gateway-Ticket`; cookies `hermes_session_at`, `hermes_session_rt`, `hermes_session_provider`, `hermes_session_pkce`, `hermes_sso_attempt`; auth providers `nous`, `basic`, `self-hosted`, `drain`; npm packages `@hermes/shared`, `@nous-research/ui`, `@hermes/root-tests`; theme names "Hermes Teal", "Hermes Teal (Large)", "Nous Blue"; brand fonts Mondwest, Collapse, Rules Compressed/Expanded; toolset name `hermes-acp`; agent info name `hermes-agent`; ACP `_meta.hermes.sessionProvenance`; logger names `hermes.mcp_serve`; thread prefixes `hermes-console`, `acp-agent`; peer-project name "OpenClaw" (whose 9-tool channel bridge the MCP surface copies) |

Third-party protocol names used by this area (keep exact; verify against vendor docs before build):
- **ACP (Agent Client Protocol)** request methods: `initialize`, `authenticate`, `session/new`,
  `session/load`, `session/resume`, `session/fork`, `session/list`, `session/cancel` (notification),
  `session/prompt`, `session/set_model`, `session/set_mode`, `session/set_config_option`; agent→client:
  `session/update` (notification), `session/request_permission`, `elicitation/create`. Update kinds:
  `user_message_chunk`, `agent_message_chunk`, `agent_thought_chunk`, `tool_call`,
  `tool_call_update`, `plan`, `available_commands_update`, `current_mode_update`, `usage_update`,
  `session_info_update`. Tool kinds: `read`, `edit`, `delete`, `move`, `search`, `execute`, `think`,
  `fetch`, `other`. Permission option kinds: `allow_once`, `allow_always`, `reject_once`,
  `reject_always`. Stop reasons used: `end_turn`, `cancelled`. Auth method shapes: agent-managed and
  `terminal` (with `args`). Capability fields: `loadSession`, `promptCapabilities.image`,
  `sessionCapabilities.{fork,list,resume}`, client `elicitation.form`. The reference runs the SDK
  with the "unstable protocol" switch on, because fork/list/resume/set_model are unstable-tier
  [obs].
- **MCP** (Model Context Protocol): stdio transport; `initialize` with `protocolVersion`
  `2025-06-18` used by the app-liveness probe; tools registered with name, description, JSON input
  schema [obs].
- **JSON-RPC 2.0** error `-32601` (method not found).
- **OAuth 2.0 / OIDC**: authorization code + PKCE `S256`, `/.well-known/openid-configuration`,
  `jwks_uri`, RFC 7009 revocation, RFC 7519 time-claim leeway.
- **WebSocket** close codes (RFC 6455 standard 1000/1011/1012/1013 plus app-private 44xx).
- **xterm.js** addons: fit, unicode11, web-links, webgl; SGR 1006 mouse mode.
- Windows APIs named in the platform layer: `IsWow64Process2`, `ProcessIdToSessionId`,
  `WTSGetActiveConsoleSessionId`, `GetProcessWindowStation`, `OpenProcess`
  (`PROCESS_QUERY_LIMITED_INFORMATION`), ConPTY; macOS `sysctl.proc_translated`, IOKit.

---

## 3. Behaviour

### 3.1 Dashboard: what it is, launch modes, serving

- **Trigger / inputs:** `indus dashboard [--port 9119] [--host 127.0.0.1] [--no-open] [--isolated]
  [--insecure (accepted, ignored with a warning)]`; `indus serve` (headless backend for the desktop
  app); a developer `web --no-open` variant for Vite HMR [obs].
- **Flow:**
  1. CLI resolves the profile, checks the HTTP stack dependencies; if the frontend bundle is missing
     and `npm` exists, builds it on first launch (resource-capped build: V8 heap capped from the
     container memory limit, bundler threads limited to half the cores; a "light" switch forces 1
     thread / 1 GB heap) [obs].
  2. One backend serves a whole machine. If a live backend already owns this host but cannot serve
     the requested host/port, the command refuses with exit code **78** and names the owner; a
     port-in-use preflight uses exit **75** [obs]. Launching from a named-profile alias reuses the
     machine dashboard and opens it with that profile preselected (`?profile=<name>`), unless
     `--isolated` asks for a dedicated per-profile server [obs].
  3. Decide whether the auth gate engages (see 3.3). If it must engage and no login provider is
     registered: interactive terminal → offer to set up username/password (written to config) or
     point at OAuth registration; non-interactive → refuse to bind with a message that lists each
     bundled provider's reason [obs].
  4. Start uvicorn; record the bound host on app state (used by the Host guard); open the browser
     unless `--no-open` [obs].
- **SPA serving:** any unmatched GET returns `index.html` (client-side routing), except paths under
  `api/` which return a JSON 404 naming the missing endpoint (never HTML) [obs]. Static files are
  served only if they resolve inside the build directory (encoded `..` blocked) [obs]. Hashed assets
  get an immutable cache header; `index.html` is served `no-store` so a reload always picks up the
  current session token [obs]. If the bundle is missing at request time, routes answer a JSON 404
  telling the operator to build it — the server does not latch a permanent "no frontend" state, so
  a later build is picked up without restart [obs].
- **HTML bootstrap injection:** before `</head>` the backend inserts one inline script setting six
  window globals: session token (omitted in gated mode and when the host "withholds" it), embedded
  chat flag (always true), base path (from `X-Forwarded-Prefix`), auth-required flag, launcher
  preselected profile, and the backend's own profile name; profile strings are JSON-encoded with
  `</` escaped so a hostile name cannot close the tag [obs]. It also inlines the active theme's
  bootstrap CSS to avoid a flash of the default theme [obs].
- **Reverse-proxy prefix:** when `X-Forwarded-Prefix` is present the backend rewrites absolute
  `/assets/`, `/fonts/`, `/ds-assets/`, favicon URLs in `index.html`, and rewrites `url(/fonts/…)`
  etc. inside served CSS, so one build works at `/` and at `/prefix/` [obs]. The SPA reads the base
  path global and prefixes every REST/WS URL and the router basename with it [obs].
- **Headless (`serve`) mode:** same server, SPA disabled even if a build directory exists; only
  JSON/WS API reachable; the desktop has no build-time dependency on this SPA [obs].
- **Dev mode:** Vite on 5173 proxies `/api` (including WebSocket upgrade) and `/dashboard-plugins`
  to the backend; a dev-only Vite plugin fetches the running backend's `index.html` on each page
  load and copies its token/flags into the dev HTML, so protected calls do not 401 [obs].
- **CORS:** localhost/127.0.0.1 origins on ports 9119, 3000, 5173, plus the custom port if one was
  chosen [obs].
- **Limits:** build chunk-size warning ceiling 600 kB; code-split groups react-vendor, xterm, three,
  plot, motion, ui, vendor with a 20,000-byte minimum chunk [obs].
- **Desktop-owned SSH backends** (out of scope detail): self-retire after 900 s with no WebSocket
  clients and no running turn, when another owner nonce takes the lock (15 s poll), or when the
  on-disk code moved (30 s poll, 2 confirmations) [obs]. The rebuild needs these only with remote
  desktop support.

### 3.2 Dashboard: navigation, routes and screens

- **Shell:** left sidebar (collapsible, state remembered per browser; becomes a slide-over below
  1024 px), page header with title and an "end" slot, language switcher (17 locales), theme switcher
  (palette icon), status strip, auth widget (gated mode only), plugin shell slots [obs]. `/` redirects
  to `/sessions` [obs]. Unknown routes redirect to `/sessions` once plugin loading has finished
  (nothing is rendered during the plugin-load window to avoid a flash) [obs].
- **Route table** (all lazy-loaded except the shell) [obs]:

| Path | Screen | One-line purpose |
|---|---|---|
| `/chat` | Chat | Embedded TUI terminal + right rail (3.5) |
| `/sessions` | Sessions | Browse/search/rename/export/delete/prune sessions; resume into Chat |
| `/files` | Files | Host file browser: list, mkdir, upload, read, delete, download |
| `/analytics` | Analytics | Token/cost charts for 7/30/90 days; nav entry hidden unless a config flag is on |
| `/models` | Models | Provider/model inventory, current model, auxiliary models, local models |
| `/logs` | Logs | Tail agent/errors/gateway logs with level/component filters |
| `/cron` | Cron | Scheduled prompt jobs: create, edit, pause/resume, trigger, delete |
| `/skills` | Skills | Installed skills toggle, toolsets view, hub search/install/update |
| `/plugins` | Plugins | Installed plugins, memory/context providers, dashboard-plugin hub |
| `/mcp` | MCP | Configured MCP servers (add/test/enable/remove) + curated catalog install |
| `/channels` | Channels | Messaging platforms: per-platform credential forms, enable, test, restart gateway |
| `/webhooks` | Webhooks | Dynamic webhook subscriptions; one-time HMAC secret on create |
| `/pairing` | Pairing | Approve/revoke messaging users; clear pending codes |
| `/profiles` | Profiles | Profile cards; create/rename/delete; set sticky default; edit model/description/persona |
| `/profiles/new` | Profile builder | Stepped create flow: name, description, model, skills, MCP servers |
| `/config` | Config | Schema-driven form editor for the main config file; import/export JSON |
| `/env` | Keys | Secrets file editor grouped by category, redacted previews |
| `/system` | System | Host stats, update check/apply, portal status, curator, gateway control, memory, credential pool, operations, checkpoints, shell hooks |
| `/docs` | Documentation | Page header link that opens the external docs site in a new tab, plus plugin slots |
| plugin paths | Plugin pages | Added/overridden by dashboard plugins (3.8) |

- **Chat host persistence:** the Chat page is rendered outside the router, mounted lazily on the
  first visit to `/chat`, then kept mounted and merely hidden (`display:none`, `aria-hidden`) when
  the user visits other tabs, so the PTY, WebSocket and terminal survive tab switches; the router
  still owns `/chat` via an empty placeholder route so deep links and history work [obs].
- **Profile scope:** a sidebar profile switcher (visible when more than one profile exists) selects
  the profile all management pages read/write; the choice lives in the URL as `?profile=<name>`;
  the REST client appends that parameter automatically to a fixed list of profile-scoped endpoint
  families (status, gateway, analytics, skills, toolsets, config, env, mcp, messaging, provider
  OAuth, model info/set/auxiliary/options, pairing, memory, curator, webhooks, ops, logs, portal,
  credentials, plugin providers, local models, dashboard theme/font/plugins, plugin backends,
  shared metrics); an explicit `profile=` already in a URL wins [obs]. When nothing is selected the
  client falls back to the backend's own injected profile name, because destructive routes return
  400 on an unnamed profile once a host has more than one profile directory [obs]. A non-default
  managed profile shows an amber banner naming it [obs]. Unknown profile → 404 [obs].
- **Status data:** the status endpoint (public) powers the sidebar strip and banners; it auto-refreshes
  every 5 s; "active sessions" counts sessions active in the last 5 minutes; recent list is 20 [obs].
- **Banners:**
  - Resource pressure: one banner at a time, priority disk-critical > memory-critical > suspected
    OOM restart > disk-elevated > memory-elevated. Memory elevated below 128 MiB or 15 % available,
    critical below 64 MiB or 5 %; disk counts as elevated once free space drops under 512 MB, or usage
    reaches 85 % while under 4 GB of headroom remains, and as critical once free space drops under
    256 MB, or usage reaches 95 % while under 1 GB of headroom remains; heartbeat older than
    150 s (or future-dated) degrades to "unknown" and shows nothing. Dismissal is scoped to the
    gateway boot id; escalation re-opens [obs].
  - Shared-metrics consent: shown while the managed profile has no recorded answer; three equal
    answers; the close button hides it only for the browser session; a re-ask's close keeps "no"
    [obs].
  - Multiplex-standalone: shown when the backend reports a reason other profiles' bots are silent;
    dismissal keyed on the reason text [obs].
  - Profile scope banner (above) and load-error notices.
- **Config page:** fields come from a schema endpoint (type, description, category, select
  options); enums render as selects, booleans as toggles, the rest as text; Save writes immediately;
  Reset only resets the form; Export/Import JSON [obs]. Changes apply to the next session or gateway
  restart [obs].
- **Keys page:** each key shows set/unset, redacted preview, description, signup link, input, delete;
  advanced keys hidden behind a toggle [obs].
- **Sessions page:** tabs Chats / Automation / All (automation = cron, tool, API, ACP sources), an
  exact-source dropdown, FTS search with highlighted snippets that auto-scrolls to the first match,
  stats bar, expand to full history rendered as Markdown with collapsible tool calls, inline rename,
  JSON export, prune older than N days, delete, play button → `/chat?resume=<id>` [obs]. Message
  pages default to the latest 500, limit max 500, `order=oldest|latest` [obs]. Deletes carry a
  mutation identity (request id + expected revision + expected generation) read from a snapshot
  endpoint; partial identity → 409 `mutation_identity_required`; none → 409
  `client_upgrade_required`; a session with a running/queued turn → 409 `session_busy`; a lost turn
  → 409 `unknown_execution` [obs]. Bulk delete/empty/prune/owner-backfill count as offline-only
  maintenance: each acquires an exclusive per-profile gateway lock, so it answers 409 whenever the gateway is running; dry-run
  prune and reads still work [obs].
- **Logs page** [obs]: the view refreshes itself every 5 s and colours rows by severity. Filters:
  - which file: the `agent`, `errors` or `gateway` log;
  - minimum level: `ALL`, else `DEBUG`, `INFO`, `WARNING` or `ERROR`;
  - component: `all`, else one of `cron`, `cli`, `tools`, `agent`, `gateway`;
  - how many trailing lines: 50, 100, 200 or 500.
- **Cron page:** create with optional name, prompt, cron expression, delivery target (local or a
  messaging platform or email); list shows state badge, last/next run; pause/resume, edit modal,
  trigger now, delete; a schedule builder helper [obs]. A public `/api/cron/fire` webhook exists for a
  managed external scheduler, authenticated by its own short-lived signed token, not by the gate
  [obs].
- **Channels page:** one card per supported platform with live status; per-platform form with only
  that platform's fields; secrets as password inputs, stored redacted, blank keeps existing value;
  enable toggle; test; restart gateway (system-scope installs use non-interactive sudo and fail fast
  without passwordless sudo) [obs].
- **Webhooks page:** create (name, description, event filter, delivery target, direct-delivery mode,
  agent prompt) → shows route URL and one-time HMAC secret; enable/disable is hot-reloaded by the
  gateway (disabled routes answer 403) [obs].
- **System page:** host stats (via psutil when present), update badge with "N commits behind" and a
  confirm-before-update dialog (git installs only; Docker/Nix show the out-of-band command); update
  check cached 24 h, `force=1` bypasses, uses the hosting API not `git fetch` [obs]; gateway
  start/stop/restart; memory provider selection and reset; credential pool add/remove (1-based
  index); operations (doctor, security audit, backup, restore, skills update, prompt-size breakdown,
  support dump, config migrate) run as background actions with a live log tailed via an action
  status endpoint; checkpoint store size/prune; shell hooks list/create/remove with an explicit
  consent checkbox and a warning [obs].
- **Admin console modal:** a REPL-like modal over a dedicated `/api/console` socket (3.6) [obs].
- **Files page:** browses the host; locked to a fixed root only when the home is the hosted
  container root or an operator env var forces a root; otherwise starts at the OS user's home and
  can navigate anywhere [obs]. Download links may authenticate by `?token=` because they open in a
  new tab — this is the only query-token REST route [obs].
- **Typography/contrast rules** (design system): minimum body text 12 px; never <0.7 opacity on
  text; uppercase only via a "display" utility on brand chrome [obs]. Brand fonts are vendor assets
  and must not be shipped by the rebuild [inf].
- **Errors and recovery:** network failure becomes a plain-language toast ("backend unreachable")
  with details only in the console [obs]; 401 handling in 3.3.
- **Evidence:** web/AGENTS.md, App.tsx, api.ts, web-dashboard doc.

### 3.3 Dashboard authentication and request guards

- **Two modes** [obs]:
  - *Loopback mode* (bind host is `127.0.0.1`, `::1` or `localhost`, and no non-loopback public URL
    is declared): no login. The backend mints a 32-byte URL-safe random **session token** at process
    start (or takes one handed in by the desktop shell via env) and injects it into `index.html`.
    Every `/api/*` request except a small public allowlist must carry it in a dedicated header
    (preferred, to avoid clashing with a reverse proxy's own `Authorization`) or as a legacy
    `Authorization: Bearer <token>`; comparison is constant-time.
  - *Gated mode* (any other bind, including `0.0.0.0` and RFC1918/CGNAT/link-local addresses, which
    are treated as public; or any non-loopback declared public URL even with a loopback bind):
    every request must carry a verified login session cookie; the session token is NOT injected and
    is rejected even if presented [obs].
- **Public allowlist** (shared by both gates so they cannot drift): health probe, status, config
  defaults, config schema, model info, theme list, plugin manifest list, managed-cron fire webhook
  [obs]. Status must stay public because external liveness probes and the pre-login SPA read it; it
  exposes only counts, enums and coarse numbers [obs].
- **Middleware order** (outermost first at runtime) [obs]: native-desktop/bearer token seam →
  legacy token check → login gate → plugin-route runtime gate → Host header guard → body-size limit
  for auth routes (64 KiB). The plugin gate runs after auth so unauthenticated callers always get
  401, never a 404 that would leak which plugin names exist [obs].
- **Host header guard** (DNS rebinding defence): accept the exact bound host (with/without port),
  loopback aliases when bound to loopback, exact operator-declared public hostnames; accept anything
  when bound to all interfaces; otherwise 400 JSON [obs]. No wildcard/suffix matching for public
  hosts [obs].
- **401 handling in the SPA** [obs]: a gated 401 carries a JSON envelope with an error code
  (`unauthenticated` or `session_expired`) and a login URL; the client stores the current location
  in session storage and full-page navigates to the login URL. A loopback 401 means the token
  rotated (server restarted under an open tab): the client reloads the page **once** (guarded so it
  cannot loop; the guard clears on the next 2xx). Other 401s surface as ordinary errors.
- **WebSocket auth** (browsers cannot set headers on upgrade) [obs]: loopback → `?token=<session
  token>`; gated → `?ticket=<t>` where the SPA first POSTs to a ws-ticket endpoint (cookie auth) and
  receives a single-use ticket with a **30 s** TTL; a server-spawned client (the PTY child
  publishing events) uses a process-lifetime internal credential never given to the browser; a
  remote desktop in token mode may present a verified login access token. Before accept, each
  socket is checked in order: chat enabled (else 4404), credential (else 4401), Host/Origin match
  (else 4403), peer address allowed (else 4408); close reasons are clamped to 123 bytes because they
  can echo attacker-controlled origins [obs].
- **Login providers** (pluggable; several may be stacked and the login page lists them) [obs]:
  - *Username/password*: single shared credential; scrypt password hash preferred over plaintext
    (plaintext env value overrides a config hash, for rotation); stateless HMAC-signed tokens;
    default session TTL 43,200 s; a random per-process signing secret if none set (sessions die on
    restart and do not span workers); login endpoint rate-limited per client IP to 10/min (429) and
    answers one generic 401 for unknown user and wrong password. Docs say trusted networks/VPN
    only.
  - *Vendor portal OAuth*: authorization code + PKCE S256; access token 15 min, no refresh token in
    contract v1 (SPA bounces to login on expiry); JWT verified against the portal's JWKS with a
    60 s clock-skew leeway (0 = strict; invalid value fails closed to 0); client id obtained via a
    CLI `register` command or the portal web UI. Rebuild: replace with our own or drop [inf].
  - *Self-hosted OIDC*: issuer + public client id (no client secret); endpoints discovered from the
    well-known document; HTTPS required except loopback; discovered issuer must match (trailing
    slash tolerated); ID token RS256/ES256 verified with `iss`/`aud` pinned, 60 s leeway; claims map
    `sub`→user id, `email`, display name from name→preferred_username→nickname→email, org from
    org_id/organization else joined groups; refresh via refresh_token grant; logout revokes when the
    IdP advertises revocation [obs].
  - *Service bearer* ("drain"): a provider may opt routes into `Authorization: Bearer` machine auth;
    the bundled one guards a gateway-drain endpoint with a secret that must be ≥256 bits or the
    route stays disabled [obs].
- **Cookies** [obs]: access-token cookie (HttpOnly, SameSite=Lax, Secure when HTTPS, max-age = token
  TTL); refresh-token cookie (30-day browser max-age, only written when a refresh token exists);
  provider cookie; PKCE cookie (10 min; SameSite=None+Secure over HTTPS so it survives the
  cross-site IdP redirect chain, Lax on loopback HTTP); SSO-attempt cookie (60 s); `__Host-` /
  `__Secure-` name variants are recognised.
- **Public URL and proxies** [obs]: an operator-declared public URL fixes the OAuth callback to
  `<public_url>/auth/callback` verbatim (prefix header ignored there) and adds its hostname to the
  accepted Host/Origin set; malformed values (no scheme, no host, quotes/angle/whitespace/control)
  are ignored. Forwarded-proto/for headers are trusted only from loopback peers or listed trusted
  proxies (exact IPs or bounded CIDRs; `*`, `0.0.0.0/0`, `::/0` rejected).
- **Audit log:** every login start/success/failure and verify failure as one JSON line in
  `<home>/logs/<dashboard auth log>`, with tokens, codes, verifiers, state and Authorization
  redacted [obs].
- **Identity endpoints:** "who am I" (session provider, user id, email, display name), provider
  list, logout (clears all auth cookies, redirects to login) [obs]. The sidebar widget shows the user
  id truncated to 14 characters [obs].

### 3.4 Dashboard REST API surface (what the SPA consumes)

- **Size:** the backend registers ~263 HTTP/WS routes across ~50 families [obs]; the SPA client
  references ~165 distinct `/api/…` paths [obs]. The rebuild needs only what its pages use.
- **Families and representative operations** (paraphrased; not exhaustive) [obs]:
  - status/health: liveness, status (version, gateway state, platforms, active sessions,
    `auth_required`, `auth_providers`, health components, memory/disk blocks), host identity, system
    stats.
  - config: get, put (`{config}`), defaults, schema. env: list, put (`{key,value}`), delete (`{key}`).
  - sessions: list (with source/sources/exclude_sources filters, order created|recent, paging),
    one, messages (paged), search (`q`), stats, patch (rename/archive), export, delete (fenced),
    mutation snapshot, prune (dry-run supported), bulk delete, empty delete.
  - chat: workspaces (`?scan=1` rescans configured repo roots), image upload.
  - files / fs / media: list, read, download, stream, upload, upload-stream, mkdir, delete; fs list,
    read-text, write-text, read-data-url, download, git-root, default-cwd; media proxy.
  - logs; analytics usage (`days`, default 30).
  - cron: jobs CRUD, pause, resume, trigger, delivery targets, blueprints, fire webhook.
  - skills: list, toggle (`{name,enabled}`), hub search/install/uninstall/update. tools/toolsets.
  - mcp: servers list (env redacted), add (`{name,url?|command?,args?,env?,auth?}`), test (connect,
    list tools, disconnect), enable/disable, delete, catalog, catalog install, OAuth callback route.
  - messaging platforms: list with fields, configure (`{enabled?,env?,clear_env?}`), test, platform
    onboarding helpers; pairing: list, approve (`{platform,code}`), revoke (`{platform,user_id}`),
    clear-pending; webhooks: list, create, delete, enable.
  - gateway: start/stop/restart (backgrounded), migrate plan/apply, drain.
  - ops: doctor, security audit, backup (+download), import (+upload), hooks CRUD, checkpoints
    (+prune), prompt-size, dump, config-migrate, debug-share; actions status for tailing.
  - memory: get, set provider, reset (`{target: all|memory|user}`); credentials pool CRUD;
    curator status/pause/run; portal status; learning graph/node; shared-metrics answer.
  - model: info, options, set, auxiliary, mixture-of-agents, recommended default; providers OAuth
    login sessions (start/poll/cancel/disconnect); local models (status, hardware, catalog, runtime
    install …).
  - profiles: list, create, active get/set, rename/patch, delete, persona get/put, description,
    model, auto-describe, export/import, setup command, open terminal, desktop overlay.
  - dashboard: themes, set theme, set font, plugins list, plugins rescan, plugin hub, plugin
    providers; `/api/plugins/<name>/*` plugin backends.
  - git (used by desktop more than the SPA): status, worktrees, branches, review diff/stage/
    unstage/revert/commit/push/create-PR, worktree add/remove, branch switch.
  - audio: transcribe, voice config, speak, TTS/STT leases, live voice session; streaming WS
    `/api/audio/transcribe-stream`, `/api/audio/speak-stream`; display WS.
  - update: check (24 h cache) and apply.
- **Data shapes (conceptual):**
  - `Status { version, release_date?, gateway: {state, pid?, platforms[]}, active_sessions,
    auth_required, auth_providers[], components{…}, overall, memory: {pressure: ok|elevated|critical|unknown,
    gateway_rss_mb?, system_total_mb?, system_available_mb?, swap_used_mb?, sampled_at, boot_id,
    last_boot_unclean, last_boot_suspected_oom}, disk: {pressure, free_mb, total_mb, used_percent,
    sampled_at} }` — collectors are fail-safe (any error → `{pressure: unknown}`) [obs].
  - `ApiError { status (0 = unreachable), message, details }` [obs].
  - `PtyQuery { channel, attach?, resume?, profile?, cwd?, fresh?: 1, token?|ticket? }` [obs].
- **Errors:** JSON 404 for unknown `/api/*`; 400 for bad Host; 401 envelope in gated mode; 409 family
  for fenced session mutations and gateway-owned maintenance [obs].

### 3.5 Chat tab: embedded terminal over PTY + sidecars

- **Trigger:** first visit to `/chat`, or `/chat?resume=<id>` from Sessions, or clicking a row in the
  rail's session switcher, or "New chat" [obs].
- **Flow (client)** [obs]:
  1. Generate a per-mount **channel id** (prefix differs for scoped vs fresh) used to pair the PTY
     with its event feed; a refresh starts a new channel.
  2. Read/mint this tab's **attach token** from session storage; a Web Lock named after the token
     detects a second live tab using the same token, in which case a new token is minted; "start
     fresh" rotates it.
  3. Build the WS URL for `/api/pty` with `channel`, `attach`, optional `resume`, `profile`, `cwd`
     (only for a fresh chat), `fresh=1` when forced, plus the auth param (3.3); fetching a gated
     ticket times out after 8,000 ms; the socket must open within 8,000 ms.
  4. On open, immediately send a resize escape (`ESC [ RESIZE:<cols>;<rows> ]`) so the TUI has a
     size before its first paint; re-fit and re-send after two animation frames and after font
     loads.
  5. Keystrokes → raw bytes to the socket; socket bytes → xterm (WebGL renderer, Unicode 11 widths,
     fit addon, web-links addon). WebGL is gated off for software renderers (llvmpipe, swiftshader,
     softpipe, "software") and released when hidden.
  6. While the socket is quiet, send a resize keepalive every **20,000 ms** so idle-timeout proxies
     keep the socket.
  7. On resume, for **30,000 ms** suppress pathological ANSI erase sequences and collapse bursts of
     50+ blank lines emitted by the TUI's virtual-scroll replay.
  8. Close handling: 4401 in loopback → one page reload (token rotated); 1011 → "could not start"
     overlay with the reason already printed in the terminal; 4410 → the agent process exited →
     "Start new session" affordance; 4409 → superseded by another tab → stay quiet; 1012/1013 or
     abnormal drop → automatic reconnect with exponential backoff base 250 ms, max 3,000 ms, at most
     5 attempts, paused while the browser tab is hidden or the user is on another page, resumed on
     return with a 1,000 ms throttle; input typed while reconnecting is refused with a notice. Other
     4403/4404/4408 map to explanatory copy.
- **Flow (server, `/api/pty`)** [obs]:
  1. Gates (3.3). If the platform PTY bridge cannot load (native Windows without the ConPTY helper),
     write a red explanation into the terminal and close 1011.
  2. Validate `channel` against `[A-Za-z0-9._-]{1,128}`. Per channel, the backend keeps an
     "active-session file" so a later socket on the same channel without `resume` re-opens the
     session the child last reported; `fresh` deletes it. If it re-opened implicitly, it first
     sends a JSON `{type:"resume", id}` text frame so the client pins scrolling like an explicit
     resume.
  3. A picked `cwd` applies only to fresh chats; a missing/relative directory fails closed (never
     silently uses the launch directory).
  4. Resolve the child argv/env exactly as the TUI command would (profile home, resume id, sidecar
     URL for event publishing, active-session file path, workspace cwd). Missing Node/npm or an
     unknown profile → error text + 1011.
  5. Without `attach`: legacy 1:1 mode — spawn, pump both directions, kill the child on disconnect,
     drop the channel marker in a `finally` (cancellation-safe).
  6. With `attach`: keep-alive mode. Registry key = token + profile + canonical resume id. Close
     other sessions belonging to the same raw token and orphaned sessions holding the same resumed
     conversation (identified via the single-writer lease holder pid in that profile's home). Attach
     or spawn. A new attach to an existing PTY replays its ring buffer and then sends a Ctrl-L
     redraw byte (a fresh xterm cannot reconstruct an alternate-screen TUI from a byte tail).
  7. Input loop: a frame that is exactly a resize escape is consumed (applied with `TIOCSWINSZ` or
     ConPTY resize; cols clamped 1..2000, rows 1..1000) and never written to the child; anything
     else is written; if the child stops accepting input the socket closes 1013 ("input stalled").
  8. On child exit the drain task closes the attached socket 4410; on disconnect the PTY is only
     detached and keeps running until the registry reaper closes it.
- **Registry limits:** keep-alive TTL 30 min after detach, max 16 sessions (full → error + 1011),
  per-PTY ring buffer 1 MiB, PTY read timeout 0.2 s, idle read back-off 0.05 s [obs]. A helper
  shutdown grace of 1.5–2.0 s is used when killing the child [obs].
- **JSON-RPC sidecar (`/api/ws`):** speaks the same newline-delimited JSON-RPC dialect as the TUI's
  stdio protocol (session create, prompt submit, events like `message.delta`), via the shared
  client package also used by the desktop. In the dashboard it is used only for connection state and
  credential warnings; the first chat client of a standalone backend triggers deferred MCP
  discovery [obs]. Identity from the WS credential becomes the authority for privileged RPCs; a
  legacy-token connection is granted a fixed owner capability set (create/read/submit/control/
  approve/respond) [obs].
- **Event fan-out (`/api/pub` → `/api/events`):** the PTY child is given a sidecar URL in its env and
  publishes every dispatcher emit to `/api/pub?channel=…`; the backend broadcasts each text frame to
  all `/api/events?channel=…` subscribers; subscribers never send; empty channels are evicted [obs].
  The rail uses this for the live tool-call feed and the session title/model badge; it reconnects
  with backoff 1 s → 30 s, max 15 attempts, 15 s connect timeout; 4401/4403 stop reconnecting and
  show "login expired, reload" [obs].
- **Right rail** [obs]: model picker (the model actually answering, from the event feed, else the
  configured model via REST), session switcher (recent sessions of the active profile: title or
  preview, relative time, message count, source for non-CLI; click resumes in place; "New chat";
  refresh), workspace picker (projects + discovered git repos, most recent first, "Other path…",
  remembered per profile, rescan), reasoning-effort picker, copy-last-answer, image paste/drop
  upload. Folds into a slide-over on narrow screens.
- **Mobile:** soft-keyboard inset handling, composition (IME) input handling, touch scrolling,
  focus helpers [obs].
- **Profile:** a scoped chat spawns its child with that profile's home; switching profiles starts a
  fresh terminal [obs].

### 3.6 Admin console socket (`/api/console`)

- **Trigger:** the console modal; same pre-accept gates as the PTY [obs].
- **Frames (JSON objects only)** [obs]: client → `ping`, `cancel`, `confirm {command}`,
  `input|command {line}`; server → `ready {profile}`, `output {id, stream, data, command}`,
  `error {id?, message, command?}`, `confirm_required {id, command, message}`,
  `complete {id, status: ok|error|timeout|cancelled|confirm_required|clear|exit|idle, command}`,
  `clear`, `pong`; every prompt-carrying frame includes the prompt string.
- **Rules** [obs]: one command at a time ("already running" error); a pending confirmation must be
  confirmed with the exact same command or cancelled before anything else runs; cancel interrupts any
  agent the command forked and waits for its worker; command timeout 60 s, unwind wait 10 s, output
  limit 50,000 chars, 4 worker threads; `exit` closes 1000; init failure closes 4400 or 1011.

### 3.7 Themes and i18n

- 8 built-in themes (two default variants, a vendor-blue one, midnight, ember, mono, cyberpunk,
  rosé) plus user YAML themes in `<home>/dashboard-themes/`; a theme has a 3-layer palette,
  typography (font URL), layout (`standard|cockpit|tiled`), image assets exposed as CSS variables,
  per-component chrome overrides, colour overrides and raw custom CSS [obs]. Active theme and an
  independent font override persist to the profile config [obs].
- 17 UI locales with a typed key set; English is the reference locale [obs].

### 3.8 Dashboard plugins

- **Discovery:** scan `<home>/plugins/<name>/dashboard/manifest.json` (user, wins), bundled plugin
  dirs, and project-local plugins only when an opt-in env var is set; cached per process; a rescan
  endpoint refreshes [obs]. On a multi-profile backend a non-launch profile sees its own plugins,
  then root ones, then bundled, filtered by its enabled/disabled lists, and its plugin backend runs
  in that profile's out-of-process plugin host [obs].
- **Manifest:** `PluginManifest { name, label, description?, icon? (Lucide name, unknown → puzzle),
  version?, tab: {path, position?: end|after:<seg>|before:<seg>, override?: <builtin path>,
  hidden?: bool}, slots?[], entry (JS IIFE), css?, api? (python routes), integrity? (SRI) }` [obs].
- **Load lifecycle:** SDK object exposed on `window` before React renders (React, hooks, UI
  primitives, typed API client, raw fetcher with auth, utils, i18n hook); for each manifest inject
  CSS then a script tag; the bundle calls a global `register(name, Component)` and optional
  `registerSlot(name, slot, Component)`; the shell waits up to **2 s** after script load before
  finishing initial render; late registration still appears; load failures only warn [obs].
- **Routing:** `override` replaces a built-in page; ordinary tabs are inserted by position; hidden
  plugins register slots only; `/plugins` cannot be taken [obs].
- **Slots:** shell slots `backdrop, header-left, header-right, header-banner, sidebar, pre-main,
  post-main, footer-left, footer-right, overlay`, and page slots `<page>:top|bottom` for sessions,
  analytics, logs, cron, skills, plugins, config, env, docs, chat [obs].
- **Backends:** `api` python file mounted at `/api/plugins/<name>/`; static assets at
  `/dashboard-plugins/<name>/…` with traversal blocked; disabled plugins answer 404 only after
  auth [obs]. Plugin WebSockets must be built through the SDK helper so gated-mode tickets are
  never forgotten [obs].

### 3.9 ACP adapter: process and handshake

- **Trigger / inputs:** an editor spawns `indus acp` (or a standalone `indus-acp` launcher, or a
  module entry); stdin/stdout carry ACP JSON-RPC; stderr carries logs [obs].
- **Flow:**
  1. Parse `--version` (print and exit), `--check` (import the SDK and adapter, print OK),
     `--setup` (run the interactive model/provider setup, then, only with a TTY, offer to install
     browser tools), `--setup-browser [--yes]` (install the browser toolchain) — all before any
     server starts [obs].
  2. Route all logging to stderr with secret redaction; quieten HTTP client loggers; suppress the
     noisy traceback the SDK logs when a client sends liveness probes `ping`/`health`/`healthcheck`
     (they still get `-32601`) [obs].
  3. Load the profile's secrets file; install the OS trust store for TLS; on Windows pre-import
     the configured memory provider on the main thread before the stdin reader thread starts
     (a first native-extension import racing another thread deadlocked `session/new`) [obs].
  4. Construct the agent object — by default the **gateway viewer** — and run the SDK loop with
     unstable protocol enabled; on exit close the viewer while its event loop is alive [obs]. The
     viewer does NOT stamp sessions ended: ending an ACP session is the gateway's job, done
     once the final viewer has gone and nothing is queued, or when the gateway itself stops [obs].
- **`initialize` response** [obs]: protocol version = SDK's; agent info name + version; capabilities
  `loadSession: true`, image prompts, session fork/list/resume; auth methods = (when credentials
  resolve) an agent-managed method whose id is the active provider name, plus always a `terminal`
  method (setup id, args `["--setup"]`) so a fresh editor install can configure the app first.
  The viewer also observes the raw `initialize` params to detect `elicitation.form` support because
  the SDK's typed model drops that field [obs].
- **`authenticate(methodId)`** [obs]: terminal-setup id succeeds only once a provider with
  credentials resolves; otherwise the id must equal the resolved provider name; anything else
  returns nothing (failure). A callable credential (e.g. a cloud identity token provider) counts as
  valid.

### 3.10 ACP adapter: sessions (gateway-viewer implementation)

- **Connection to the gateway** [obs]: lazily connect to the profile's local gateway RPC socket;
  call the describe verb and require its profile id to equal this process's profile home (else
  `profile_mismatch`); start an event-pump task. If the pump died or the transport closed, the next
  call tears down and reconnects; on reconnect, re-subscribe (resume) every session this editor
  holds, re-attaching editor-provided MCP servers, re-requesting live reasoning, re-offering pending
  approval/clarify cards, and announcing any "unknown" lost turn. All waiters on the old transport
  fail with an "outcome unknown, do not resend" error.
- **`session/new(cwd, mcpServers)`** [obs]: require the gateway to list `acp` among creatable
  sources and advertise the editor-policy capability (else `acp_policy_unavailable`); translate the
  cwd (Windows/WSL path forms → POSIX when running in WSL); freeze launch flags (yolo/safe-mode etc.)
  into the session policy; safe-mode/ignore-config launches must also name an explicit model AND
  provider or creation is refused; create with a random request id, source `acp`, editor policy
  `{mcp_servers, edit_approval_policy: ask}`; remember editor MCP servers for reattachment; ask the
  gateway to stream reasoning for this session (`show_reasoning`, 10 s timeout, failure ignored);
  respond `{sessionId, models, modes, _meta provenance}`; then, after the response is queued,
  send `available_commands_update` and a `usage_update`.
- **`session/load` and `session/resume`** (same behaviour) [obs]: map the editor's held id to the
  logical session (an id may have rotated through context compression; aliases are kept so updates
  are addressed by the id the editor knows); fetch info, adopting legacy in-process threads that
  have no gateway policy yet; if the session's cwd differs from the editor's, apply a "move
  workspace" mutation (idle sessions only); if the session has no cwd, tell the user the editor cwd
  is not applied; a non-empty `mcpServers` list is the editor's selection (requires the session-MCP
  capability; a policy conflict triggers a rebind mutation then retries), an empty list keeps the
  existing servers; resume/subscribe; **stream the full history as `session/update` notifications
  before responding** (user/agent/thought chunks plus reconstructed tool start/complete); re-offer
  pending cards; announce lost turns; respond with models/modes/meta.
- **`session/fork`** [obs]: branch mutation on the parent (child gets either the supplied MCP
  servers or a copy of the parent's), move the child's workspace if the cwd differs, subscribe,
  respond `{sessionId: child, models, modes, meta}`.
- **`session/list(cursor?, cwd?)`** [obs]: read the session catalog (ACP-source rows, up to 1000)
  filtered by normalised cwd (realpath, so `/var` vs `/private/var` and `/tmp` vs `/private/tmp`
  match; Windows drives mapped to WSL mounts); page size **50**; `cursor` is the last session id of
  the previous page; an unknown cursor returns an empty page (never the full list); `nextCursor`
  only when more remain.
- **`session/cancel`** [obs]: if a submit is still in flight, remember a pending cancel and apply it
  as soon as the admission id is known; otherwise cancel this editor's own admission; if the
  gateway says the generation is stale, read the admission receipt and interrupt only if that exact
  admission is the one running (never the session's current generation, which may belong to a
  successor turn from another surface).
- **`session/set_model(modelId)`** [obs]: model ids look like `provider:model` (or
  `custom:<name>:<model>`); apply a model mutation; a guarded target (e.g. pricier/limited model)
  returns a confirmation request which is shown to the user as a permission card with "switch
  anyway" (allow_once) / "cancel" (reject_once); declined → error `model_switch_cancelled` and the
  picker must not show the target; a pick during a running turn is deferred to the next turn and
  the user is told so; a deferred pick later refused at the boundary is announced. Picking never
  changes the profile-wide default.
- **`session/set_mode` / `session/set_config_option`** [obs]: the viewer advertises a single mode
  "default" (ask before edits) and accepts only that; any other mode or a non-`ask` edit-approval
  config value fails with `acp_edit_policy_mutation_unavailable`; other config writes are accepted
  as no-ops so editors do not error.
- **States:** per held session: `snapshot {replay_epoch, last_sequence, execution_generation,
  pending[] {admission_id, status: queued|started|unknown|…}, prompts[] (open cards), revision}`;
  per editor: aliases, editor MCP specs, open permission tasks, current admission, streamed text
  per admission, closed reply segments, terminal results (capped 256), receipts (capped 256),
  un-acked submits (capped 64 sessions), titles, latest usage [obs].

### 3.11 ACP adapter: prompts, streaming, slash commands

- **`session/prompt(prompt[], sessionId)`** [obs]:
  1. Unknown session → `not_found`. If the session has a turn whose outcome is "unknown" (lost in a
     gateway restart), refuse with an `unknown_execution` message that tells the user to resolve it
     in a terminal (resume the session in the classic chat and run its discard command for the lost
     admission id) — never resubmit.
  2. Convert ACP content blocks to the model's user content: text; images (base64) → image parts;
     resource links to local files read up to **512 KiB** (images too large are replaced by a
     notice; text truncated with a note); embedded resources decoded similarly; remote images are
     fetched and staged under the home; attachment count capped by the gateway's attachment limit.
  3. Compute a content fingerprint. If the previous submit for this session was never
     acknowledged (transport loss/timeout) and this prompt has the same fingerprint, reuse its
     staged content and input id so the gateway returns the same admission (idempotent retry); any
     other prompt retires the retained identity. A cancel arriving during staging ends the prompt
     with `cancelled` without sending.
  4. If the text (no attachments) is a **registered** slash command (or alias), run it locally (see
     below); paths like `/etc/hosts` or unknown `/words` are ordinary prompt text.
  5. Submit; on a non-RPC failure before the ack, keep the identity for retry and re-raise.
     A receipt of `terminal` (retry of a settled turn) → replay that turn's final text once;
     `unknown` → refuse as in step 1.
  6. Wait until this admission's terminal result arrives, the pump fails, the transport is replaced
     (→ "outcome unknown" error), or the admission becomes blocked behind an unknown turn.
  7. Outcome `failed` → JSON-RPC error `admitted_turn_failed`; `unknown` → refusal; `cancelled`
     → `stopReason: cancelled`; else `end_turn`; include usage from the last usage event.
- **Event projection** (gateway event → ACP) [obs]: events with a sequence ≤ the snapshot's last
  sequence in the same replay epoch are dropped (dedupe after reattach); `message.delta` →
  `agent_message_chunk` with a stable message id per assistant message; interim commentary that was
  not already streamed → its own chunk segment; `message.complete` → finish the reply (unless a
  retry's receipt already delivered it) and record the terminal; reasoning delta →
  `agent_thought_chunk`; todo update → `plan`; tool start/complete → `tool_call` /
  `tool_call_update` (every tool call MUST reach a terminal status; anything still open at turn end
  is marked failed); usage → `usage_update {size: context window, used: estimated tokens}`; title
  changes → `session_info_update` (only when changed); tools-changed → a text notice that editor MCP
  tools changed from the next turn; replay gap → treat as transport failure.
- **Tool rendering** [obs]: tool name → ACP kind: read (file read, skill view/list, browser
  snapshot/vision/images, vision analyse), edit (write file, patch, skill manage), search (file
  search), execute (terminal, process, code execution, browser click/type/scroll/press/back,
  delegate, image generation, TTS), fetch (web search/extract, browser navigate), other (todo),
  think (internal thinking); unknown → other. Patches and writes render as diffs; terminal as the
  command text; reads/searches as previews; large outputs truncated at 5,000 chars.
- **Slash commands** [obs]: advertised via `available_commands_update`. Handled locally: `help`
  (local text), `branch` (refused: "use the editor's fork action"), `steer` (inject guidance into a
  running turn; if idle, resubmit a just-cancelled prompt with the steer as correction, else run it
  as a normal prompt), `queue` (submit as the next prompt), `title` (rename; bare form forwarded),
  `undo`/`retry` (rewind mutation, retry resubmits), `tools` (session info), `model` (mutation with
  an argument, info without), `compress`, `new`, `clear` (reset mutation). Every other registered
  command is forwarded to the gateway's slash executor; a refusal is shown as the command's reply,
  never as a failed prompt. Usage text when an argument is missing.
- **Permissions (approvals)** [obs]: a gateway approval request becomes
  `session/request_permission` with a pending `execute` tool call titled with the command and
  description (or a diff card for file edits). Options offered depend on the choices the gateway
  allows: "Allow once" (`allow_once`), "Allow for session" (id `allow_session`, kind `allow_always`
  because ACP lacks a session kind), "Allow always" (only if permanent allowed), "Deny"
  (`reject_once`), "Deny always" (`reject_always`, only if the SDK supports that kind and not
  once-only). Option ids map to internal decisions once/session/always/deny/deny. After the answer
  the card's tool call is closed `completed` (allowed) or `failed`. On the gateway path, a cancelled
  card, a lost transport or an unrecognised option does NOT count as denying; some other viewer
  can still answer the canonical waiter; the card key is released so a later snapshot re-shows a new card; an accepted
  answer keeps the key until the matching "settled" event so replays do not re-prompt.
- **Clarify questions** [obs]: if the client advertised form elicitation, send `elicitation/create`
  with a form schema `{answer: string | enum | array of enum (multi-select)}`; accept → answer
  (multi-select as a JSON array string); decline/cancel → empty string (skip); otherwise a
  permission card with one `allow_once` option per choice plus "Skip" (`reject_once`); dismissal
  = skip; unknown id = no answer.
- **In-process implementation differences** (legacy path) [obs]: runs the agent in a 4-thread pool;
  advertises three modes — default (ask), accept-edits (auto-allow workspace and temp-dir edits,
  still ask for sensitive paths), don't-ask (auto-allow edits for the session except sensitive
  paths); sensitive names include `.env`, `.env.local`, `.env.production`, `id_rsa`, `id_ed25519`;
  approval timeout = the configured approvals timeout (default 300 s; formerly a hardcoded 60 s
  caused self-denials) and a timeout returns a distinct "timeout" decision; installs the approval
  callback per turn and restores the previous one afterwards; tracks tool ids FIFO per tool name so
  parallel same-name calls complete the right card; late MCP discovery (up to 30 s) refreshes tools
  only before the first turn; switching model rebuilds the agent but keeps editor MCP toolsets.
- **Toolset policy** [obs]: ACP sessions use the platform toolset for `acp` (default: a curated
  editor toolset — files, terminal/process, web/browser, memory, todo, session search, skills, code
  execution, delegation, vision; excludes messaging delivery and cron), plus resolver extras
  (enabled plugin toolsets, admitted MCP servers keyed by server name); a globally disabled-toolset
  list removes entries; `no_mcp` drops config MCP servers; editor-sent MCP servers are always
  added. Editor MCP specs (commands, env, headers) are kept only in memory; durable policy keeps a
  fingerprint and frozen schemas; after a gateway restart the editor must resend the same specs or
  execution is refused; tool schemas stay frozen for the conversation [obs].
- **Security note** [obs]: some hosts auto-answer permission requests (`allow_once`) without
  showing them; with the default editor toolset that means unattended shell execution. The only
  effective mitigation is to remove terminal/code-execution from the ACP toolset.

### 3.12 MCP server mode (`indus mcp serve`)

- **Trigger:** an MCP client spawns `indus mcp serve [--verbose]`; stdio only; logs on stderr at
  WARNING (DEBUG with `--verbose`); missing MCP SDK → message with install hint and exit 1 [obs].
- **Server identity:** name = app name; instructions text says it is a messaging bridge across the
  connected platforms [obs].
- **Tools** (registration order = list order) [obs]:

| Tool | Inputs | Output (JSON text) |
|---|---|---|
| `conversations_list` | `platform?`, `limit=50` (1..200), `search?` | `{count, conversations[{session_key, session_id, platform, chat_type, display_name, chat_name, user_name, updated_at}]}` newest first; search matches display name, chat name or key, case-insensitive |
| `conversation_get` | `session_key` | `{session_key, session_id, platform, chat_type, display_name, user_name, chat_name, chat_id, thread_id, created_at, updated_at, input_tokens, output_tokens, total_tokens}` or `{error}` |
| `messages_read` | `session_key`, `limit=50` (1..200) | `{session_key, count, total_in_session, messages[{id, role, content ≤2000 chars, timestamp}]}` user/assistant text only, last N, chronological |
| `attachments_fetch` | `session_key`, `message_id` | `{message_id, count, attachments[{type: image, url} | {type: media, path} | {type, data}]}` — media from `MEDIA:` tags in text |
| `events_poll` | `after_cursor=0`, `session_key?`, `limit=20` (1..200) | `{events[], next_cursor}` |
| `events_wait` | `after_cursor=0`, `session_key?`, `timeout_ms=30000` (0..300000) | `{event}` or `{event: null, reason: "timeout"}` |
| `messages_send` | `target` ("platform:chat_id" or a friendly channel name), `message` | result of the shared send engine, or `{error}` |
| `channels_list` | `platform?` | `{count, channels[{target, platform, name, chat_type}]}` from the cached channel directory, else derived from the routing index |
| `permissions_list_open` | — | `{count, approvals[]}` observed during this bridge's lifetime |
| `permissions_respond` | `id`, `decision: allow-once|allow-always|deny` | `{resolved, approval_id, decision}` or `{error}` |

- **Event bridge** [obs]: a background thread; at start it records each existing conversation's
  latest message timestamp (baseline) so history is not replayed as events; then every **200 ms**
  reads the database's change counter (a cheap pragma on a private read-only connection) and does
  nothing if unchanged; on change, reloads the routing index (gateway session rows with session
  key, origin, display name; falls back to a legacy JSON index) and enqueues one `message` event
  per new user/assistant text message (content truncated to 500 chars) after the per-conversation
  last-seen timestamp. Queue capped at **1000** events; cursors are monotonically increasing
  integers; `events_wait` re-checks at most every 200 ms until its deadline. Event kinds declared:
  `message`, `approval_requested`, `approval_resolved`.
- **Event shape:** `QueueEvent { cursor, type, session_key, role?, content?, timestamp?,
  message_id?, approval_id?, decision? }` [obs].
- **Errors:** every tool returns JSON with an `error` field rather than raising; integer inputs are
  clamped and non-numeric values replaced by defaults [obs]. Reads work without the gateway; sends
  need a running gateway (live platform connections) [obs]. Sends are text-only [obs].
- **Gap:** nothing in the bridge enqueues `approval_requested`, and responding only removes the
  entry locally and enqueues `approval_resolved` — there is no IPC to the gateway [obs/inf]. See
  R09.10.

### 3.13 Platform layer: host facts

- **Purpose:** the single source for machine facts on the **control host** (where this Python runs) —
  not the terminal execution target (SSH/container) and not a remote desktop client [obs].
- **Rules** [obs]: facts are cached per process; take **no environment-variable input** (so a
  hardware recogniser cannot be spoofed from a shell); use hardware/OS sources, no subprocesses;
  a `clear_caches` hook exists for tests.
- **Facts** [obs]:
  - `os_family` = the platform string (`win32`, `darwin`, `linux…`).
  - `process_arch`: on Windows from the interpreter's build platform (x64 Python under ARM64
    emulation reports amd64), elsewhere from the machine string; normalised to `amd64|arm64|x86|
    unknown` (x86_64/x64→amd64, aarch64→arm64, i386/i686→x86).
  - `native_arch`: Windows via `IsWow64Process2` native machine (0x8664 amd64, 0xAA64 arm64,
    0x014C x86) else the machine string — never the processor-architecture env var, which lies
    under emulation; macOS reports arm64 when an amd64 process runs under Rosetta.
  - `cpu_model`, `cpu_vendor` (cpuinfo / registry / sysctl; ARM implementer codes mapped),
    `ram_total_bytes` (meminfo / Windows API / sysctl).
  - `gpu_class` ∈ `nvidia|amd|intel|apple_silicon|none|unknown`. Detection per platform: on an
    arm64 Mac the CPU architecture alone implies `apple_silicon`; an x86 Mac enumerates PCI display
    controllers via IOKit; Windows reads up to 16 adapters from the registry's display-class key;
    Linux by the NVIDIA procfs or the WSL CUDA bridge library, else sysfs DRM vendor
    ids (10de nvidia, 1002 amd, 8086 intel; priority nvidia > amd > intel). Any read error →
    `unknown`.
  - `nvidia_gpus` (name, PCI id) and an NVIDIA platform classifier: arm64 Windows with an N1X CPU
    marker or PCI device in 0x2E00–0x2E3F → "RTX Spark"; arm64 Linux with GB10/"DGX Spark" in GPU
    or CPU names → "DGX Spark"; other NVIDIA → "discrete" [obs; DGX path not hardware-verified per
    source note].
  - `interactive_session`: Windows → process session id ≠ 0 and equals the active console session,
    or has a window station; Linux → audit session id present and not 4294967295 and
    `/run/user/<uid>` exists; others → true.
  - Runtime predicates: Termux (env markers), WSL (`microsoft` in `/proc/version`), container
    (`/.dockerenv`, `/run/.containerenv`, Kubernetes env, cgroup markers in PID 1's cgroup, and
    under cgroup v2 only the **root mount** line of mountinfo — scanning all lines misclassified
    hosts that merely run containers).
  - `summary()` → `{machine {os_family, os_release (macOS product version, not Darwin kernel),
    native_arch, cpu_model, ram_gb, gpu_class, vendor, wsl, container} (unmeasured keys omitted),
    machine_kind: Mac|PC|Spark|computer, has_nvidia_gpu, nvidia_platform, locale?, full_name?}`.

### 3.14 Platform layer: resolution tiers and app declarations

- **Tiers** [obs]: `locate` reads file metadata only (stat, access); `inspect` may open files and
  call in-process OS APIs (version from plist, PE resource, uninstall registry); `probe` runs every
  time without a cache; it alone is allowed to spawn a process or use a socket, limited by an `Effort` of
  `LOCAL` (no network), `NETWORK` (sockets but never subprocess) or `DIAGNOSTIC` (may spawn);
  default probe deadline 3.0 s.
- **Shapes** [obs]: `Observation<T> { state: not_checked|present|absent|unavailable|error, value?,
  detail (never parsed) }`; `Candidate { value, source, present }`; `Resolution { kind:
  explicit_path|path_executable|known_path|package_runner|missing, candidates[] }` (first present
  candidate wins; all candidates kept in probe order for diagnostics); `LookupContext { path:
  unset|ambient|"" (search nothing), pathext? }`; `Inspection { version, signer }`; `Probe {
  running, answering, endpoint }` (deliberately not truthy — callers must read fields).
- **Command location** [obs]: a name containing a path separator is an explicit path, trusted only
  if absolute and an executable file; otherwise `which` on PATH (honouring an explicit PATHEXT on
  Windows without mutating the process env), then each known directory (on Windows trying every
  PATHEXT suffix).
- **Known-directory tables** (all expansion at lookup; each table empty on OSes where that
  ecosystem does not install) [obs]: Homebrew (`/opt/homebrew/bin`, `/usr/local/bin`, macOS only);
  user local bin; Rust cargo bin; Node global bins (npm-global, bun, volta / `%APPDATA%\npm` etc.);
  the app's vendored bin under home; machine-marker files (per-OS app-data location, MSIX
  virtualised LocalCache variant on Windows, XDG state dir on Linux); home-dir globs (suffixed
  channel roots, legacy Windows root, MSIX roots); per-user gateway service definitions (Windows
  Startup folder entry, macOS LaunchAgent plist, systemd user unit) and system units (Linux only);
  macOS privacy-protected folders (Documents, Desktop, Downloads, iCloud) that must not be touched
  casually; Windows user program dirs (LocalAppData Programs, scoop shims, WinGet links); macOS
  Applications folders; flatpak export bins; snap bin. A repository test fails if a bare `which`
  or directory literal appears outside this package without an allowlisted reason.
- **App declarations** (data used by MCP catalog entries and skills to say "only offer me when app
  X is installed") [obs]:
  - `Declaration { name, app?: {per-OS AppDef}, requires: {app: bool, min_version?: dotted digits,
    gpu?: nvidia} }`. OS keys must be `win32|darwin|linux`; at least one.
  - `AppDef { presence: executable|bundle, locations[], version: none|pe_resource (win)|
    uninstall_registry (win, needs display-name prefix)|plist (mac), liveness: none|server_json
    {path, pid_key=pid, url_key=http, token_key=token, endpoint_path=/mcp} }`.
  - A location is either a rooted path (absolute, or starting with `~`, `%VAR%`, `$VAR`; no `..`,
    no `**`, no URL scheme; `*` may stand for a versioned folder) or a typed lookup: `command`
    (any OS), `uninstall_registry` (Windows; bare name + relative file inside the install folder),
    `app_bundle` (macOS), `flatpak` and `snap` (Linux); each kind's presence must match.
  - `min_version` requires `requires.app: true` and a version source on every OS block except a
    Linux block in a multi-OS declaration.
  - A small thread-safe registry maps server name → declaration with a change callback.
- **Availability** [obs]: `no_requirements` (offerable), `unsupported_os`, `unsupported_gpu` (an
  `unknown` GPU passes, to avoid locking out machines whose registry/sysfs read failed),
  `missing_app`, `available` (offerable), `version_too_old`; versions are checked for all installed
  copies, not just the first located, so a stale earlier hit cannot mask a newer install; version comparison is numeric per
  component, padding with zeros, failing closed on non-numeric input; no caching, no probing.
- **Liveness probe for app-hosted MCP servers** [obs]: read the app's runtime JSON file; pid alive
  (Windows `OpenProcess` with limited-query right; POSIX signal 0, permission error = alive);
  endpoint must be plain `http`, loopback host, numeric port 1–65535, no userinfo — the path is
  always the declaration's, never the file's; with effort above LOCAL, POST one MCP `initialize`
  under a single absolute deadline; HTTP 200/401/403 count as "answering". Tokens are redacted in
  every repr.

### 3.15 OS specifics, paths and Windows support

- **Home directory** [obs]: context-local override (for multi-profile servers) → env var → platform
  default: Windows `%LOCALAPPDATA%\<app><suffix>` (fallback `~/AppData/Local`), POSIX
  `~/.<app><suffix>`; the suffix comes from an env var for side-by-side channels. Expansion of `~`
  and env vars applies.
- **Windows native** [obs]: supported for CLI, TUI, gateway, cron, browser tool, MCP, dashboard;
  the dashboard Chat tab uses a ConPTY bridge (`pywinpty`) on Windows and `ptyprocess` on POSIX; if
  the bridge import fails, `/api/pty` explains and closes 1011. Shell commands run through Git Bash
  resolved by the package manager. Console code page flipped to UTF-8 at every entry point (stdio
  reconfigured with `errors=replace`, child Python inherits UTF-8 env, `EDITOR` defaults to notepad);
  an env switch disables this. Gateway autostart via a Startup-folder entry / scheduled task, not a
  Windows Service. Data layout under `%LOCALAPPDATA%\<app>\` (checkout, tools, installs, bin, and
  user data at the root). ARM64 excludes some optional SDK extras.
- **WSL** [obs]: ACP cwd values arriving as `E:\Projects` or `\\wsl.localhost\…` are translated to
  POSIX when the app runs inside WSL; comparisons map drive letters to `/mnt/<x>/`.
- **macOS** [obs]: realpath normalisation for `/var`↔`/private/var`; Rosetta detection; TCC
  protected folders listed.

---

## 4. File map (what to read in the Read step)

| Sub-area | Reference path | Lines | Read what | Docs / tests to read with it |
|---|---|---|---|---|
| Web entry | `web/src/main.tsx` | 29 | all | — |
| Web shell, routes, plugin routing | `web/src/App.tsx` | 1406 | route table, nav build, persistent chat host | `website/docs/user-guide/features/web-dashboard.md` |
| REST/WS client | `web/src/lib/api.ts` | 2811 | first ~420 lines (auth, profile scoping, WS URL); skim the `api` object | `web/src/lib/api.test.ts` (350) |
| Chat page | `web/src/pages/ChatPage.tsx` | 2073 | connect effect, close-code handling, resize | `ChatPage.test.tsx` (946), `ChatPage.a11y.test.tsx` (324) |
| Chat rail | `web/src/components/ChatSidebar.tsx` | 691 | header comment, sockets | `ChatSidebar.test.tsx` (855) |
| PTY client helpers | `web/src/lib/pty-*.ts` | 17–136 each | constants and state machines | matching `pty-*.test.ts` |
| Event feed / JSON-RPC client | `web/src/lib/eventsFeedClient.ts`, `gatewayClient.ts`, `events-reconnect.ts` | 93, 63, 99 | all | `events-reconnect.test.ts`, `gatewayClient.test.ts` |
| Sessions page | `web/src/pages/SessionsPage.tsx` | 2158 | filters, delete fence | `SessionsPage.test.tsx` (261) |
| Other pages | `web/src/pages/*.tsx` | 69–1665 | skim layout per page | — |
| Plugins | `web/src/plugins/{registry,slots,usePlugins,types}.ts*` | 223, 208, 267, 37 | all | `extending-the-dashboard.md`, `usePlugins*.test.*`, `registry.test.ts` |
| Themes | `web/src/themes/{context.tsx,presets.ts,types.ts}` | 615, 255, 208 | types and switching | `presets.test.ts` |
| i18n | `web/src/i18n/types.ts`, `en.ts` | 923, 934 | key set only | — |
| Build config | `web/vite.config.ts`, `web/package.json` | 171, 58 | dev proxy, chunking, deps | `web/README.md` (104), `web/AGENTS.md` (45) |
| Backend app + auth middlewares | `<cli pkg>/web_server.py` | 1473 | lines ~200–520 (token, gates, Host guard) | tests under `tests/<cli pkg>/` |
| SPA mount / themes / plugins | `<cli pkg>/web_server_dashboard.py` | 1191 | `mount_spa`, theme list | — |
| Boundary guards | `<cli pkg>/web_server_boundary.py` | 218 | Host/Origin/peer rules | — |
| Chat WebSockets | `<cli pkg>/web_routers/chat_ws.py` | 787 | all | — |
| PTY bridge + registry | `<cli pkg>/pty_bridge.py`, `pty_session.py`, `web_server_chat.py`, `win_pty_bridge.py` | 381, 364, 539, — | spawn, resize, ring buffer, registry | — |
| Dashboard auth | `<cli pkg>/dashboard_auth/*.py` | 3087 total | public paths, cookies, ws tickets, middleware | web-dashboard doc §Authentication |
| Routers | `<cli pkg>/web_routers/*.py` | 48–1152 each (~19k total) | route lists only, unless building that page | — |
| ACP entry | `acp_adapter/entry.py` | 225 | all | `website/docs/developer-guide/acp-internals.md` (208) |
| ACP viewer (default) | `acp_adapter/gateway_server.py` | 962 | all | `website/docs/user-guide/features/acp.md` (454) |
| ACP in-process (legacy) | `acp_adapter/server.py` | 1099 | initialize, sessions, list paging, modes | `tests/acp_adapter/` |
| ACP sessions/cwd | `acp_adapter/session.py` | 582 | cwd translation/normalisation, toolset expansion | — |
| ACP events/tools/content | `acp_adapter/events.py`, `tools.py`, `content.py` | 297, 853, 282 | kind map, truncation, content conversion | — |
| ACP permissions/edits/elicitation | `acp_adapter/permissions.py`, `edit_approval.py`, `elicitation.py` | 153, 241, 71 | all | — |
| ACP commands | `acp_adapter/gateway_commands.py`, `commands.py` | 271, 312 | route table | `website/docs/developer-guide/gateway-command-parity.md` |
| ACP misc | `acp_adapter/{auth,catalog,model_catalog,gateway_media,gateway_reply,gateway_session_meta,provenance}.py` | 51, 79, 312, 111, 135, 127, 86 | skim | — |
| MCP server | `mcp_serve.py` | 763 | all | `website/docs/user-guide/features/mcp.md` §Running as an MCP server |
| Platform host facts | `<platform pkg>/host/{facts,runtime,summary,products,gpu_adapters,darwin_pci}.py` | 429, 69, 173, 57, 85, 77 | all | `<platform pkg>/AGENTS.md` (14) |
| Platform resolvers | `<platform pkg>/resolver/{base,core,app,availability,known_dirs}.py` | 55, 148, 439, 117, 98 | all | `tests/test_managed_runtime_resolution.py` |
| App declarations | `<platform pkg>/declaration.py` | 273 | all | `website/docs/developer-guide/plugins/application-declarations.md` |
| Windows notes | `website/docs/user-guide/windows-native.md` | 383 | feature matrix, data layout | — |
| JS repo tests | `tests-js/*.test.*` | 9–410 each | names only (see note) | — |
| Lint config | `eslint.config.shared.mjs`, `web/eslint.config.js` | 108, 36 | rules list | — |
| Root workspace | `package.json` | 91 | workspaces, engines, allowScripts | — |

Note on `tests-js/`: it does **not** test the web UI. Its ~33 files guard repo-wide JS hygiene and
packaging: lockfile/allow-scripts sync, Node engine alignment, React/react-dom version pairing,
lazy optional dependencies, desktop builder/notarisation/entitlements, installer behaviour,
updater UI, Electron window-open policy, CI matrix routing [obs]. The web UI's own tests are the
~45 vitest files beside the sources in `web/src` (pages, chat sidebar, PTY helpers, API client,
plugins, auth widget, banners, schedule parsing) [obs].

Giant files: `web/src/lib/api.ts` [2811]: read the header section through `buildWsUrl`, then use
the endpoint list in §3.4; `web/src/pages/ChatPage.tsx` [2073]: search for the connect effect
(builds the `/api/pty` params) and the `onclose` handler; `web/src/pages/SessionsPage.tsx` [2158]:
filters and delete flow; `<cli pkg>/web_server.py` [1473]: the middleware block. Line counts for
`web/` (~64.5k) include binary font files counted by `wc -l`; the i18n locale files account for
~13k lines.

## 5. Minimum viable subset

v1 needs:
1. **Dashboard backend skeleton**: FastAPI app on loopback, per-process session token injected into
   `index.html`, header check on `/api/*` with a public allowlist (health, status), Host header
   guard, JSON 404 for unknown API paths, `no-store` index, immutable hashed assets.
2. **SPA shell** with Sessions (list, view, search, delete), Config (schema form), Keys, Logs,
   and Chat; profile switcher can wait until multi-profile exists.
3. **Chat tab** via PTY + xterm: `/api/pty` with resize escape, keepalive, 1:1 legacy mode first;
   keep-alive registry (attach token, ring buffer, TTL) second. The `/api/events` tool feed and the
   right rail can follow.
4. **ACP adapter, in-process flavour first** (simpler than the gateway viewer): initialize,
   authenticate (terminal setup method), new/load/resume/list/cancel/prompt, streamed message/
   thought/tool updates, permission bridge with once/session/always/deny, history replay before
   load response, stdout clean. Gateway-viewer mode once the rebuild's gateway exists (decision
   for the lead; see Open question 1).
5. **MCP serve** with read tools (conversations_list, conversation_get, messages_read,
   events_poll/wait) and messages_send; drop or properly wire the approval tools.
6. **Platform layer**: host facts (os family, native arch, WSL/container), `locate_command` with
   known-dir tables, home-dir resolution. App declarations/availability can wait until the MCP
   catalog lands.

Can wait: gated mode and login providers (only needed off-host; until then refuse non-loopback
binds outright), themes beyond one light/dark pair, i18n beyond English, dashboard plugins, admin
console socket, analytics, channels/webhooks/pairing pages (need the messaging gateway), system ops
page, git/audio/local-model routes (desktop-driven), SSH-backend retirement watchdogs, NVIDIA
platform classification, Windows ConPTY (ship WSL-only chat first, per the reference's own
fallback message).

## 6. Hard parts / traps

1. **R09.1 Do not rebuild the chat in React.** The reference deliberately embeds the real TUI over
   a PTY so every TUI feature appears in the browser for free; a second React chat surface drifts.
   The rebuild should embed its TUI the same way and keep React to side panels whose failures never
   break the terminal pane. (web/AGENTS.md)
2. **R09.2 WebSocket auth differs from REST auth.** Browsers cannot set headers on upgrade, so
   loopback uses a query token and gated mode uses a separate single-use 30 s ticket minted over
   cookie auth; the loopback token must be rejected in gated mode, or a leaked token becomes a
   remote credential. Every WS endpoint (including plugin sockets) must go through one helper that
   picks the right param. Also clamp close reasons to 123 bytes (they echo origins).
3. **R09.3 Non-loopback must fail closed.** Gate engagement is "bind host not loopback OR declared
   public URL not loopback"; LAN/CGNAT addresses count as public; the old "insecure" flag must be
   a no-op; with no provider configured the server must refuse to start (non-interactive) rather
   than serve unauthenticated. A declared public URL with a loopback bind still engages the gate,
   otherwise a reverse proxy turns the local token into a remote login.
4. **R09.4 Middleware ordering leaks.** The plugin 404 gate must run after auth (else 404 vs 401
   reveals plugin names); the Host guard must reject before anything else does work; the public
   allowlist must be one shared constant used by both gates (it once drifted and broke a liveness
   probe).
5. **R09.5 Token rotation vs open tabs.** The loopback token changes every restart; `index.html`
   must be `no-store`, and the SPA must reload exactly once on a stale-token 401 (with a guard
   cleared by the next success) — never loop, and never do this in gated mode.
6. **R09.6 PTY lifetime and supersession.** A naive 1:1 socket↔PTY kills the agent on every tab
   switch, refresh or proxy idle drop. The reference keeps the chat host mounted while hidden, sends
   a 20 s resize keepalive, keeps PTYs alive 30 min after detach keyed by a per-tab attach token
   (Web Lock to detect a duplicated tab), closes an older socket with 4409 when a newer one attaches,
   closes with 4410 only when the process really exited, replays a 1 MiB ring buffer then forces a
   redraw (Ctrl-L) because alternate-screen output cannot be reconstructed from a tail, and caps
   the registry at 16. Half-open sockets: reader EOF must close the socket so the writer's receive
   unparks.
7. **R09.7 Resize escape is in-band.** Resize travels as a special escape inside the byte stream;
   the server must consume a frame that is exactly that escape and never write it to the child;
   dimensions must be clamped (1..2000 cols, 1..1000 rows) or a bogus size blanks the TUI.
8. **R09.8 Resume replay garbage.** The TUI's virtual-scroll replay floods erase codes and blank
   lines on resume; the client sanitises for 30 s after a resume (also for implicit resumes, which
   the server announces with a JSON frame so the client knows). Without this the resumed transcript
   renders blank.
9. **R09.9 ACP history replay must finish before the load/resume response.** Spec-compliant clients
   (Zed and others) route `session/update` by a session id they register before awaiting the load
   call and measure updates against the response; deferring replay broke them. Conversely, the
   post-attach `available_commands_update`/`usage_update` must be sent after the response is queued
   (yield once), because editors route by an id they learn from the response of `session/new`.
10. **R09.10 MCP approval tools are hollow.** In the reference nothing populates pending approvals
    and responding does not reach the gateway; `permissions_list_open` is effectively always empty.
    Either implement real gateway IPC for approvals or do not advertise these two tools (decision
    for the lead). The docs also overstate the polling mechanism (it uses the DB change counter, not
    mtimes).
11. **R09.11 Never resend a turn with unknown outcome (ACP viewer).** After a gateway restart or
    transport loss mid-turn, the outcome is ambiguous; the adapter must surface an "unknown
    execution" refusal with a manual recovery path and must make editor retries idempotent (reuse
    the un-acked input id and staged content when the fingerprint matches; retire it on any other
    prompt). Cancel must target only this editor's admission and re-check receipts on a stale
    generation so it never interrupts a successor turn started from another surface.
12. **R09.12 "Allow for session" has no ACP kind.** The reference sends it with kind `allow_always`
    and its own option id; mapping must use the option id, not the kind. A timed-out approval must
    report "timeout" distinctly from "deny"; the timeout must be the configured approvals timeout
    (default 300 s), not a short hardcoded value (60 s caused self-denial). On the viewer path a
    dismissed card or lost editor is NOT a denial (another viewer can still answer).
13. **R09.13 Two ACP implementations, one constructor.** The reference's agent class silently
    returns the gateway viewer when constructed without arguments; the legacy in-process class
    advertises three edit modes while the viewer refuses all but "default". The rebuild should have
    one explicit implementation per mode and consistent mode advertising.
14. **R09.14 Stdout purity and probe noise.** In both ACP and MCP stdio modes any stray print to
    stdout corrupts the protocol; logging must go to stderr from the first import (including
    bootstrap/recovery code that runs before the server). Liveness probes (`ping`, `health`) should
    get `-32601` without spamming tracebacks.
15. **R09.15 Windows import deadlock.** On Windows, a first-time native-extension import racing the
    ACP stdin reader thread deadlocked `session/new`; pre-import heavy modules on the main thread
    before the reader starts, and run slow agent construction off the event loop (it froze every
    other request).
16. **R09.16 Path normalisation for session lists.** ACP list filters compare cwd strings; without
    realpath (macOS `/private/var`), drive→WSL mapping and Windows→WSL translation, an editor's own
    sessions disappear. Unknown cursor must return an empty page, not the first page.
17. **R09.17 Platform facts must not trust env vars.** Architecture from `PROCESSOR_ARCHITECTURE`
    lies under x64-on-ARM64 emulation; use `IsWow64Process2` (Windows) and Rosetta detection (macOS).
    Container detection must look only at the root mount under cgroup v2 (scanning all mounts
    misclassifies container hosts). GPU "unknown" must not block offering features.
18. **R09.18 Lookup must be side-effect free.** `locate` stats only, `inspect` reads bounded files,
    only `probe` may connect/spawn and only at the requested effort; endpoints from an app's runtime
    file must be validated (plain http, loopback, numeric port, no userinfo, our own path) before
    sending a bearer token anywhere. Centralise known-dir tables and enforce with a ratchet test.
19. **R09.19 Docs disagree about Windows chat.** One page says native Windows Chat shows a "use
    WSL2" banner; another says it works via ConPTY; code tries the ConPTY bridge and falls back to
    the banner when it cannot import. The rebuild must pick and document one behaviour.
20. **R09.20 Profile-scoped writes need an explicit profile.** One backend serves every profile;
    management calls must carry the selected profile, and destructive routes must refuse an
    unnamed scope on multi-profile hosts. The SPA's automatic `?profile=` rewrite uses a prefix
    allowlist — forgetting a family (e.g. credential-pool delete) silently targets the wrong home.
21. **R09.21 Gateway-owned data cannot be bulk-edited live.** Bulk session maintenance takes the
    profile's exclusive gateway lock and must answer 409 while the gateway runs; single-session
    deletes must be revision-fenced and refuse busy/unknown sessions, and leave small tombstones so
    a retried delete is idempotent and the id is not re-imported.
22. **R09.22 Brand assets.** The SPA depends on a vendor design-system package and ships vendor
    brand fonts and theme names; the rebuild must use its own design system, fonts and theme names
    (licence and clean-room).
23. **R09.23 Reverse-proxy prefix.** Serving under a path prefix requires rewriting absolute asset
    URLs in HTML and `url(...)` in CSS at serve time and prefixing every client URL; the OAuth
    callback must ignore the prefix header when an explicit public URL is set, to avoid
    double-prefixing.

## 7. Open questions

1. Which ACP architecture does the rebuild ship first — in-process agent (simpler, works without a
   daemon) or gateway viewer (multi-viewer, restart-safe)? Needed: lead decision after
   `03-gateway`/`01-agent-loop` plans settle.
2. Exact ACP wire method names and update-kind spellings for the pinned SDK (e.g. whether model
   switching is `session/set_model` and whether `session/resume`/`session/list` are still
   unstable-tier). Needed: vendor ACP schema at the SDK version the reference pins, plus a
   needs REA capture of a Zed ↔ app session.
3. What the ACP `available_commands_update` actually lists (all registered slash commands or a
   filtered set) and the help text. Needed: needs REA runtime capture or reading
   `gateway_session_meta.py` in full during the build step.
4. Does `permissions_list_open` ever return anything in a real deployment (some other module might
   enqueue approval events)? Needed: repo-wide search for the bridge's enqueue call during build,
   and needs REA run of `mcp serve` against a live gateway raising an approval.
5. Native Windows Chat tab: does the ConPTY bridge work in the shipped build, or does the user see
   the WSL banner? Needed: needs REA run of the shipped Windows build.
6. Exact contents of the status endpoint's `components`/`overall` health verdict and the dashboard
   self-test cadence. Needed: read `web_routers/status.py` in the build step; needs REA response
   capture.
7. Which pages the rebuild keeps for v1 vs later (channels/webhooks/pairing depend on the messaging
   gateway; git/audio/local-model routes serve the desktop). Needed: lead/product decision.
8. Whether the rebuild keeps the vendor-portal OAuth provider at all, or ships only
   username/password + generic OIDC. Needed: product decision (the portal is the reference
   vendor's service).
9. ACP `_meta` provenance payload shape (session lineage across compression rotations). Needed:
   read `provenance.py` and `gateway_session_meta.py` in build; needs REA capture of a load after a
   compression split.
10. Keep-alive PTY reaper behaviour at TTL expiry when a resumed session is still running a turn
    (killed mid-turn or waited for?). Needed: read `pty_session.py` reaper; needs REA test.
11. Whether `events_wait` should push via MCP notifications instead of long-poll (the reference
    notes no channel push protocol yet). Needed: product decision.

## 8. What to read later

- Reference docs: `website/docs/user-guide/features/web-dashboard.md` (pages, REST list, auth,
  proxies), `extending-the-dashboard.md` (themes, plugins, slots), `acp.md`, `developer-guide/
  acp-internals.md`, `mcp.md` §"Running … as an MCP server", `windows-native.md`,
  `developer-guide/plugins/application-declarations.md`, `developer-guide/multiplexing-gateway.md`
  (for the gateway the ACP viewer talks to), `developer-guide/gateway-command-parity.md`.
- Reference tests that list the edge cases: `web/src/pages/ChatPage.test.tsx`,
  `web/src/components/ChatSidebar.test.tsx`, `web/src/lib/pty-reconnect.test.ts`,
  `pty-resume-sanitizer.test.ts`, `pty-attach-token.test.ts`, `events-reconnect.test.ts`,
  `dashboard-auth-reload.test.ts`, `api.test.ts`, `MemoryPressureBanner.test.tsx`,
  `usePlugins*.test.*`; Python: `tests/acp_adapter/`, `tests/<cli pkg>/` (router, pty bridge,
  dashboard auth), `tests/test_managed_runtime_resolution.py` (known-dir ratchet).
- REA Evidence (outside the repo): none this session (REA not connected). Planned bundle:
  `~/rea-lab/evidence/09/` with captures of the dashboard HTML bootstrap, `/api/status`, a PTY
  session's close codes, a Zed ACP transcript, and an `mcp serve` tool list.
- Related specs: `01-agent-loop.md` (turn outcomes, interrupts), `02-tools.md` (tool names behind the
  ACP kind map, approval semantics), `03-gateway` area (session ownership, RPC verbs, admissions),
  `04-cli.md` (`dashboard`, `serve`, `acp`, `mcp serve` commands), `05-tui.md` (the embedded TUI,
  JSON-RPC dialect on `/api/ws`), `08-desktop.md` (headless `serve`, remote backend, SSH-owned
  backends).
