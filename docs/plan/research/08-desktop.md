# 08 — Desktop app and installer: behavioural specification (clean-room)

Scope: the desktop client (Electron main process, preload bridge, React renderer), the shared
TypeScript client package it uses to talk to the agent, the separate native setup installer (a Tauri
app), the tiny native search-tokenizer folder, the desktop's packaging/update/signing pipeline as seen
from the app folder, and the desktop end-to-end suites. Out of scope: Python agent internals, the
gateway server, sessions DB, tools, providers (areas 01-04, 06, 07), and the browser web UI (09). Where
the desktop leans on a gateway RPC this spec names the RPC's job, not its server implementation.

Reference: `<reference-repo>` @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056` (MIT). Shipped build
checked: none (REA not connected in this session; source reading only — every runtime-only fact is an
open question marked "needs REA").

Sources studied: the desktop folder's README, AGENTS, ENGINEERING, DESIGN, BUILDING guides and its
two protocol notes; the source-scoped AGENTS note; website user guide pages on the desktop app,
multi-connection desktop, bot mode, native sign-in, the desktop plugin SDK and macOS bundle updates;
`electron/` (main, preload, ~250 focused modules), `src/` (routes, settings, onboarding, chat, stores,
SDK), `apps/shared/src`, `apps/bootstrap-installer/` (Rust + React), `native/`, `e2e/` titles and the
core-suite README, `electron-builder.config.cjs`, `product-identity.cjs`, `package.json`.

Conventions used below:
- "App" = the desktop client. "Main" = Electron main process. "Renderer" = a React window.
- "Bridge" = the object the preload script exposes on `window` (one per chat-capable window).
- "Agent" / "backend" = the Python agent process the app talks to. "Gateway" = the agent's long-lived
  multiplexing server that serves one or more profiles over HTTP + WebSocket (JSON-RPC 2.0).
- "Home" = the agent's data root (`INDUS_HOME` in the rebuild). "userData" = Electron's per-app dir.
- "Profile" = an isolated agent identity (config, skills, sessions) under one home.
- "Connection" = one registered place to reach a gateway: local, remote URL, cloud, or SSH.
- "MUST" marks an invariant the reference defends with code comments that cite past bugs or tests.
- Claims carry [obs] (seen in source/docs), [inf] (inferred from code shape), [unk] (not settled).

---

## 1. Overview

The desktop app is a native chat client for the same agent the CLI/TUI use [obs]. It does not embed
the terminal UI or the browser dashboard; it renders its own React chat surface and drives the agent
over the gateway's JSON-RPC/WebSocket protocol plus a REST API [obs]. Three parties each own one kind
of truth [obs]:

- Main owns the machine: process lifecycle, backend discovery and install, native file system, git,
  terminals (PTY), windows, tray, global shortcuts, updates, secrets on disk, and a typed capability
  bridge.
- The renderer owns presentation: routes, panes, layout, ephemeral interaction state, and a cache of
  backend truth.
- The agent backend is authoritative for execution: sessions, tool runs, LLM calls, streamed output,
  cron, profiles and skills.

```
  +--------------------- Desktop app (Electron) -----------------------------+
  |                                                                          |
  |  Renderer windows (React, sandboxed, contextIsolated)                    |
  |   main chat | secondary session | instance | HUD | quick | pet | wake    |
  |        |  bridge (≈259 IPC channels: invoke/send/on)                     |
  |        v                                                                 |
  |  Main process ── backend resolver ── installer runner (stage protocol)   |
  |        |   \── secrets/registry files in userData                        |
  |        |    \── PTY host, git ops, fs ops, tray, updater strategies      |
  |        |                                                                 |
  |   REST proxy (main adds auth)          one-use WS ticket (main mints)    |
  +--------|-------------------------------------------|---------------------+
           v                                           v
     Gateway HTTP API  <==== same process ====>  Gateway WebSocket (JSON-RPC)
     (local: attached via "gateway ensure"; remote URL / cloud / SSH tunnel)
```

Request paths [obs]:
1. Renderer → bridge "api request" → main → gateway REST (main attaches the credential that fits the
   connection: one-use native grant header, bearer token, cookie session, or SSH-tunnelled token).
2. Renderer → bridge "get WS URL" → main mints a fresh one-use ticket → renderer opens the WebSocket
   itself, passing the ticket as a subprotocol; main strips the Origin header only for that exact
   reserved dial.
3. Gateway → renderer: JSON-RPC notifications (streaming deltas, tool events, status) and
   server→client requests (approval, clarify, sudo, secret, preview actions…) that the renderer must
   answer.

What the user sees: a frameless window with a left sidebar (sessions/projects, bots tab, profile
rail), a central tabbed pane tree of chats and pages, optional right panes (preview browser, file
tree, git review, terminal), a bottom status bar, and route overlays (settings, cron, profiles,
command center, agents, memory graph, webhooks). First launch either connects to an existing gateway
or installs the agent locally, then runs provider onboarding [obs].

What the system guarantees (from invariants the reference defends) [obs]:
- The renderer never touches Node/Electron directly; capability arrives only through the bridge.
- Background events never navigate, steal focus or open panes.
- A switch of connection/profile is a re-home, not a cold boot; stale state from one gateway never
  paints into another.
- Untrusted guest content (artifact iframes, preview webview) can never open the OS browser without a
  trusted user click.
- The user's message is durable at send; a lost session drops to a fresh draft that keeps the unsent
  text.

## 2. Reference names (rename in rebuild)

| Kind | Names (reference only; they become banned identifiers) |
|---|---|
| Binary / CLI and flags | product `Hermes`, `Hermes Agent`, `Hermes Light`, `Hermes Agent Installer`, `Hermes Setup`; CLI `hermes`, `hermes-light`; subcommands used by the app: `hermes desktop` (flags `--skip-build --force-build --build-only --source --cwd --hermes-root --ignore-existing --fake-boot`), `hermes gui`, `hermes gateway ensure --json`, `hermes serve`, `hermes dashboard --no-open`, `hermes update`, `hermes uninstall [--gui|--full|--dry-run]`, `hermes -p <profile>`, `--profile`; app argv `--local`, `--fresh-install`, `--update`, `--reinstall`, `--repair`; installer script args `-Manifest`, `-Stage <name>`, `-IncludeDesktop` |
| Env vars | `HERMES_HOME`, `HERMES_DESKTOP_*` (APP_NAME, BOOT_FAKE, BOOT_FAKE_ERROR, BOOT_FAKE_STEP_MS, CDP_PORT, CHILD_PID, CWD, DEV_SERVER, DISABLE_GPU, HERMES, HERMES_ROOT, IGNORE_EXISTING, ISOLATED_BACKEND, IS_PACKAGED, LIFECYCLE_*, NVIDIA_SWIFTSHADER, PASSWORD_STORE, PORT_ANNOUNCE_TIMEOUT_MS, PYTHON, READY_FD, REMOTE_URL, REMOTE_TOKEN, REMOTE_READY_TIMEOUT_MS, RENDERER_ACCESSIBILITY, SHELL, SKIP_BUILD, SKIP_PROTOCOL_REGISTRATION, SKIP_QUIT_CONFIRM, TERMINAL, TEST_MODE, TMPDIR, USER_DATA_DIR, VARIANT, WEB_DIST), `HERMES_E2E_*`, `HERMES_DATA_DIR_SUFFIX`, `HERMES_SHARED_AUTH_DIR`, `HERMES_PREVIEW_FULL_CONNECTORS`, `HERMES_BUNDLE_ENV_JSON`, `HERMES_PAYLOAD_TAG`, `HERMES_BUILD_COMMIT`, `HERMES_RELEASE_EPOCH`, `HERMES_RUNTIME_DIR`, `HERMES_PYTHON`, `HERMES_HUB_ORIGIN`, `HERMES_HUB_FALLBACK_ORIGIN`, `HERMES_PORTAL_BASE_URL`, `HERMES_DASHBOARD_SESSION_TOKEN`, `HERMES_DASHBOARD_BASIC_AUTH_*`, `HERMES_PARENT_PID/NONCE/START_MARKER`, `HERMES_UPDATE_*`, `HERMES_SETUP_DEV_REPO_ROOT`, `HERMES_BOOTSTRAP_STDIO_*`, `HERMES_FTS5_CJK_SO`, `HERMES_YOLO_MODE`, `HERMES_SESSION_ID`, `VITE_PERF_PROBE`; build-time `BUILD_PIN_COMMIT`, `BUILD_PIN_BRANCH` |
| Files and folders | `~/.hermes/`, `%LOCALAPPDATA%\hermes\`, `<home>/hermes-agent/` (managed checkout), `.hermes-bootstrap-complete`, `.hermes-update-in-progress`, `.hermes-update.lock`, `hermes-update.lock`, `desktop-build-stamp.json`, `<home>/logs/desktop.log`, `<home>/logs/update.log`, `<home>/desktop-plugins/<id>/plugin.js`, `<home>/plugins/<id>/desktop/plugin.js`, `<home>/lib/` (tokenizer .so), userData files `connection.json`, `connections.json`, `active-profile.json`, `backend-ownership.json`, `desktop-installation.json`, `native-oauth-tokens.json`, `secure-token-storage.json`, `window-state.json`, `zoom-state.json`, `hud-state.json`, `quick-entry.json`, `keep-awake.json`, `minimize-to-tray.json`, `translucency.json`, `native-theme.json`, `updates.json`, `data-url-read-max.json`, `managed-ssh-update-recovery.json`, `latest.json`; resources `agent-payload/`, `install-stamp.json`, `update-relaunch-waiter.ps1`; source folders `src/plugins/hermes-bots/` (called `<bots-plugin>` in §4), `src/hermes.ts` (called `<api-barrel>`), `electron/windows-hermes-path.ts`, `vitest.hermes-python.ts`, `public/hermes-frames/`, `public/hermes-sprite.png`, `public/hermes.png`, `nous-girl*.png/.svg`, `packaging/nous-dmg-2b.tiff`, `scripts/dump_desktop_slash_registry.py`, shared package `@hermes/shared`, UI kit `@nous-research/ui`, plugin SDK import `@hermes/plugin-sdk` |
| HTTP routes / URL schemes | deep-link scheme `hermes://` (dev `hermes-dev://`) with hosts `open`, `skill`, `mcp`, `blueprint`, `copilot-key`, `close-preview` and plugin hosts; media scheme `hermes-media://stream`; renderer query `?win=hud|overlay|quick|wake|secondary` + `watch=1` + `profile=`; gateway routes `/api/ws`, `/api/status`, `/api/fs/download`, `/api/fs/read-data-url`, `/api/files/stream`, `/api/chat/image-upload`, `/api/profiles`, `/api/profiles/<p>/soul`, `/api/plugins/<id>`, `/api/providers/oauth/nous/start`, `/auth/native/authorize`, `/auth/native/token`, `/auth/native/refresh`; installer script URL on `raw.githubusercontent.com/NousResearch/hermes-agent/<ref>/scripts/install.{ps1,sh}`; release origin paths `releases/{stable,canary}/`, `releases/darwin/{stable,canary}/{stable,canary}-mac.yml`, `releases/win32/<CHANNEL>/<CHANNEL>.appinstaller`, `releases/tag/TAG/`, `releases/commit/<SHA>/`, `releases/channel-builds/<ID>/`, `releases/channels/NAME.json` |
| RPC methods / events / commands | JSON-RPC methods (257 in the OpenRPC contract) incl. `session.create` (→ "session-create"), `prompt.submit` ("submit"), `session.interrupt`, `session.steer`, `session.resume`, `session.list`, `session.history`, `session.events.since` ("replay since cursor"), `session.mutate`, `session.title`, `session.archive`, `session.set_hidden`, `session.branch*`, `session.compress`, `approval.respond`, `clarify.lock/respond`, `commands.catalog`, `complete.slash`, `slash.exec`, `command.dispatch`, `config.get/set`, `client.capabilities`, `gateway.capabilities`, `free_tier.status`, `free_tier.ack_notice`, `onboarding.start/state/skip/set_run`, `profiles.list/create/configure`, `projects.*`, `groups.*`, `bot_relay.*`, `cron.manage`, `skills.manage`, `mcp.servers.*`, `connectors.*`, `vault.*`, `pet.*`, `wake.*`, `voice.*`, `display.*`, `billing.*`, `subscription.*`, `shared_metrics.*`, `ping`; 47 "canonical" methods incl. `prompt.receipt`, `prompt.cancel`, `prompt.replace_queued`, `prompt.resolve_unknown`, `runtime.describe`, `session.detach`, `worker.*`, `kanban.run`; 16 server→client requests `approval`, `clarify`, `sudo`, `secret`, `setup_beat`, `display.install.sudo`, `preview.act`, `preview.read`, `theme.pick`, `terminal.read`, `vault.code`, `theme.apply`, `tour`, `vault.save_login`, `vault.unlock_prompt`, `window.read`; 79 notifications incl. `message.start/delta/interim/complete`, `reasoning.delta`, `thinking.delta`, `tool.start/generating/complete/output_risk`, `subagent.*`, `status.update`, `session.info`, `session.usage`, `sessions.changed`, `projects.changed`, `skills.changed`, `cron.changed`, `todo.updated`, `preview.open/close`, `pane.reveal`, `voice.*`, `wake.detected`, `notification.show/clear`, `gateway.ready`, `error`; slash commands named in desktop policy `/skin /help /new /compact /density /details /logs /mouse /pets /journey /learning /memory-graph /reasoning /goal /discard /export /import /model` |
| Settings and storage keys | config keys the app writes/reads: `desktop.font_family`, `desktop.repo_scan_enabled/roots/exclude_paths`, `desktop.renderer_max_old_space_mb`, `desktop.electron_flags`, `desktop.ozone_platform_hint`, `desktop.manage_launcher_entry`, `desktop.ssh_path`, `terminal.font_family`, `display.show_reasoning`, `display.resume_last_session`, `model.persist_switch_by_default`, `agent.reasoning_overrides`, `auxiliary.<task>.reasoning_effort`, `providers.<id>.api_mode`, `updates.desktop_feed_base_url`, `gateway.standalone`, `agent.bot_mode_protocol`, `sessions.cjk_fts`; renderer storage families `tilesByProfile`, `$restoredDraftNotice`, `ui_meta['hermes-bots']`; webview partition `persist:hermes-preview`; MSIX identities `NousResearch.Hermes*`, `NousResearchInc.HermesAgent`; app ids `com.nousresearch.hermes*`, `com.nousresearch.hermes.setup` |
| Other (scopes, error tags, IPC channels) | Bridge global `window.hermesDesktop`; all IPC channels use the prefix `hermes:` — 259 names in the preload, grouped as: connection/boot (`connection`, `connection:for/applied/revalidate/active-route`, `connection-config:get/save/apply/probe/test/oauth-login/oauth-logout`, `connections:list/save/remove/test/set-primary/set-last-used/set-launch-mode/update-all/update-managed/changed`, `boot-progress[:get]`, `bootstrap:get/event/cancel/repair/reset/continue-local`, `backend-exit`, `backend:recycle`, `local-backend:probe`, `get-remote-display-reason`, `cloud:discover/login/logout/status/agent-sign-in`, `gateway:ws-url[-for]`, `api`, `agents:roster`, `plugin:ws-url/probe/installDesktop/removeDesktop`, `plugin-profile-routes`, `readPluginSource`); profile (`profile:get/set/remember`, `profile:default:get/set/changed`); windows (`window:openSession/openInstance/openBrowser/openInTerminal/relay/readBelow/size`, `window-control`, `window-state-changed`, `browser-popout:closed`, `focus-session`, `titlebar-theme`, `native-theme`, `translucency[:support]`, `zoom:get/set-percent/changed`); fs (`fs:readDir/openDir/reveal/rename/trash/writeText/gitRoot/logsRoot/desktopPluginsRoot/reconcileDesktopPlugins`, `readFileText`, `readFileDataUrl[ForAttach]`, `selectPaths`, `selectSavePath`, `saveImageBuffer`, `saveImageFromUrl`, `saveClipboardImage`, `savePastedText`, `saveGatewayFile`, `watchDirectory`, `watchPreviewFile`, `stopPreviewFileWatch`, `preview-file-changed`, `workspace:sanitize`, `setting:defaultProjectDir:get/set/pick`); git (`git:repoStatus/fileDiff/branchList/baseBranchList/branchSwitch/scanRepos/worktreeAdd/worktreeList/worktreeRemove`, `git:review:list/diff/stage/unstage/revert/commit/commitContext/push/createPr/prList/shipInfo/revParse`); terminal (`terminal:start/attach/write/resize/cwd/dispose`); preview (`normalizePreviewTarget`, `capturePreview`, `openPreviewInBrowser`, `preview:reach`, `preview-nav`, `preview-guest-hidden`, `close-preview-requested`, `previewShortcutActive`); HUD (`hud:open/close/changed/goto/session/set-bounds/begin-move/move-by/end-move/ignore-mouse/cursor/frost/windowing/game-overlay/reset-layout/workspace-transfer`, `hud-modifier:*`); quick entry (`quick-entry:*`); pet (`pet-overlay:*`); wake (`wake-indicator:*`); screenshot (`screenshot:*`); updates (`updates:check/apply/progress/channel:set/metric:*`, `update-hold:*`, `open-updates`, `app:relaunch`, `version`); misc (`openExternal`, `external-open-failed`, `fetchLinkTitle`, `resolveFavicon`, `notify`, `notification-action/activate`, `keep-awake[-offer]`, `power-battery[:get]`, `power-resume`, `minimize-to-tray:*`, `find-in-page`, `found-in-page`, `stop-find-in-page`, `open-find-bar`, `context-menu:*`, `readClipboard`, `writeClipboard`, `requestMicrophoneAccess`, `secret-storage:get/set`, `deep-link`, `deep-link-ready`, `mcp-oauth:listen/wait/cancel`, `freeTierChallenge:run`, `prepared-submissions:read/update/compare-and-set`, `active-work`, `ambient:claim`, `logs:recent/reveal/renderer-line/renderer-error`, `machine:profile`, `desktop-metrics:*`, `startup-latency:claim`, `vscode-theme:search/fetch`, `skin:local`, `ssh-config:hosts/resolve`, `uninstall:summary/run/openAppsSettings`, `data-url-read-max:get/set`, `devtools:disable-f12`, `f12-shortcut`, `embed-host:origin`, `feature-flags`); HTTP header `X-Hermes-Gateway-Ticket`, `X-Hermes-Session-Token`; ticket purposes `interactive`, `native-http`; canonical session title `Bot Chat`; keychain item `Hermes Key`; theme preset names `nous`, `nous-alt`; locale overlay file suffix `.desktop.yaml`; error class `JsonRpcGatewayError`; vendor provider id `nous`; repo `NousResearch/hermes-agent`, `hermes-official-plugins`, `hermes-example-plugins`; main.ts function names `resolveHermesBackend` (called `<resolve-backend>` in §4), `startHermes` (`<start-backend>`), `ensureRuntime`, `createWindow` |

Third-party protocol names used by this area (keep exact; check vendor docs): Electron (BrowserWindow,
`contextBridge`, `safeStorage`, `protocol.registerSchemesAsPrivileged`, `setWindowOpenHandler`,
`will-attach-webview`, `powerSaveBlocker`, `globalShortcut`, Window Controls Overlay), electron-builder
(asar, asarUnpack, afterExtract/afterPack/afterSign hooks), electron-updater + Squirrel.Mac, MSIX +
`.appinstaller` + Windows App Installer, `Windows.Services.Store.StoreContext`, WinRT, Authenticode,
Apple notarization/stapling/Gatekeeper/hardened runtime/TCC, AppImage, dmgbuild, Tauri 2 (commands,
capabilities, CSP), JSON-RPC 2.0, OpenRPC, WebSocket subprotocols, OAuth 2.0 for Native Apps
(RFC 8252) + PKCE (RFC 7636), OpenSSH (ControlMaster/ControlPersist, BatchMode,
StrictHostKeyChecking=accept-new, ProxyJump), StatusNotifier/D-Bus tray, Wayland/ozone, Hyprland IPC,
xterm.js, node-pty, assistant-ui, nanostores, TanStack Query, React Router (HashRouter), Radix,
cmdk, Shiki, KaTeX, Mermaid, CodeMirror 6, SQLite FTS5.

## 3. Behaviour

### 3.1 Process model and the three-way split

- **Parts:** one Electron main process; several renderer windows, each a sandboxed, context-isolated
  Chromium renderer with the shared preload (except special windows noted in §3.12); one optional
  `<webview>` guest per preview pane on its own persistent partition with its own tiny guest preload;
  hidden helper windows (link-title fetcher, free-tier challenge page, embedded-OAuth login) [obs].
- **Main bundle:** TypeScript compiled then bundled by esbuild into a single ESM entry; the electron
  code cannot use the renderer's path aliases, so it imports the shared package by relative path
  [obs]. The preload MUST be plain JavaScript because the sandboxed preload cannot run TS [obs].
- **Renderer bundle:** Vite + React 19 with the React Compiler, Tailwind 4, HashRouter with route
  transitions disabled (transitions starved route commits under streaming load) [obs].
- **Agent:** never a child the app owns in the normal local path — the app runs the agent CLI's
  "ensure the gateway" command, which attaches to (or detaches-starts) the one gateway serving that
  home; the gateway outlives the app and has no idle shutdown [obs]. Only SSH-managed remote serve
  processes are started and stopped by the app [obs].
- **State authority rule:** anything another surface (CLI, TUI, messaging) can change lives in the
  backend and is only cached in the renderer; machine/runtime facts belong to main; only pure
  presentation lives in the renderer. Every persisted renderer key must encode its scope (global,
  connection, profile, session, project or window) [obs].
- **Renderer state tooling:** small feature-owned nanostores atoms, TanStack Query for request-shaped
  server data with invalidation, refs for hot coordination; generation counters/request tokens stop
  stale async results overwriting newer intent [obs].

### 3.2 Main-process startup sequence

1. **Pre-ready:** read a bundle-baked env defaults table (non-secret build-time defaults; explicit
   runtime env wins, an empty value counts as explicit; a "null" default clears an inherited value)
   before Electron resolves its paths [obs]. Read a tiny YAML subset of the home config for
   `electron flags` and `renderer heap ceiling` (applied as a V8 old-space flag merged with any user
   JS flags); unsupported indentation is ignored with a single log line [obs]. Choose Wayland ozone on
   local Wayland/WSLg unless overridden [obs].
2. **Crash-loop guards (Windows/Linux):** a per-version marker in userData records mid-boot aborts;
   two consecutive aborts with the GPU-sandbox crash signature make the next launch add the
   no-sandbox switch, and the first launch after an app update tests the sandbox again [obs]. A GPU child death with that
   signature triggers one bounded relaunch [obs]. Separate sticky markers handle a Windows stack-cookie
   GPU crash (fix: disable GPU, never drop the sandbox for it) and NVIDIA EGL on Linux (SwiftShader
   fallback) [obs]. Markers are cleared only after the first window is actually revealed [obs].
3. **Single-instance lock:** only the lock holder may reap, spawn or claim backends; a lock loser must
   stay inert (an earlier bug let a losing instance kill the winner's backend) [obs]. A second launch
   focuses the existing window and forwards any deep-link argv [inf].
4. **Privileged schemes:** register the media streaming scheme (secure, standard, stream, fetch) before
   ready [obs]. Register the deep-link protocol client (dev builds also accept a dev scheme) [obs].
5. **On ready:** merge the login-shell PATH into the process env (GUI launches inherit a minimal PATH;
   failure never blocks boot) [obs]; build the application menu; create the main window hidden;
   reveal on first themed paint (prevents a light flash under macOS vibrancy) [obs]; load the
   renderer from the dev server or the packaged `file://` index [obs].
6. **Backend start** (§3.3–3.6) runs when the renderer first asks for a connection descriptor; the
   boot overlay in the renderer reflects progress events pushed by main [obs].

Boot progress model: `BootProgress { phase, message, progress 0..100, running, error?, retryable?,
statusCode?, isCloudBackendDown? }` broadcast to every window and readable on demand [obs]. Phases
observed: resolve (8), runtime (28), external runtime (32), remote connect (24), update-wait,
update-restart, first-run choice, ready (94), error [obs]. Progress never decreases except when an
error explicitly allows it [inf].

### 3.3 Backend resolution ladder (local)

One resolver owns precedence; each rung is trusted only after probing [obs]:

| Rung | Candidate | Notes |
|---|---|---|
| 0 | Bundled payload in app resources | Uses the payload's launcher shim with the stamp-recorded site-packages; POSIX also publishes CLI links into `~/.local/bin`. A bundled app whose payload is missing MUST report damage and refuse to run the installer [obs]. |
| 1 | Explicit source-root override env | Developer checkout, no bootstrap [obs]. |
| 2 | Dev checkout (unpackaged runs) | So local Python edits are exercised [obs]. |
| 3 | Explicit agent-command override env | For packagers (e.g. Nix); trusted or verified with a version probe; ignores the desktop app's own binary if found on PATH; ignores a Windows path under WSL [obs]. |
| 4 | Managed install under the home | Usable runtime wins even if the bootstrap-complete marker is missing (CLI installs publish the same layout) [obs]. Skipped by an "ignore existing" env/flag [obs]. |
| 5 | Source install elsewhere, found via the launcher it published in the user bin dir | Probed like rung 4 [obs]. |
| 6 | Nothing usable | Returns a "bootstrap needed" sentinel (not an error) carrying install hints [obs]. |

- The app never adopts an arbitrary agent command on PATH or a system Python [obs].
- A runtime too old to have the headless "serve" command falls back to the dashboard command in
  no-open mode — backend command compatibility only, the dashboard UI is never shown [obs].
- Windows: a venv's script launcher is unwrapped to run the real interpreter (venv redirector
  executables are refused by the packaged interpreter) [obs].

### 3.4 Local gateway attach and credentials

- **Trigger:** first connection request for a local profile.
- **Flow:** (1) run the agent CLI with an optional profile flag plus its "ensure the gateway, JSON
  output" subcommand; the command attaches to the multiplexing gateway that serves that home or starts one
  (an installed system service if present, else a detached daemon; it never installs a service)
  [obs]. (2) Parse a credential-free endpoint descriptor `GatewayEndpoint { baseUrl, profile_id,
  instance_id, multiplex_home?, control pointer }` [inf]. (3) For every WebSocket dial main mints a
  fresh private ticket for the interactive-socket purpose; every HTTP request (and every retry) mints a grant
  for the native-HTTP purpose sent only in a dedicated header scoped to the exact origin [obs].
  (4) The ticket crosses private IPC to the requesting window, is removed from the URL before the
  WebSocket is built and offered as a subprotocol; main keeps a reservation `{ ticket, webContentsId,
  expires (25,000 ms), origin, pathname }` and removes the Origin header only for that one dial [obs].
- **Timings:** ensure deadline 60,000 ms + 15,000 ms client grace; stale-gateway restart polls every
  1,000 ms; gateway restart client timeout 120,000 ms [obs].
- **Stale gateway:** when ensure reports a version skew or ticket mints fail against a stale control
  socket (named error strings), the app restarts the stale local gateway once and redials [obs].
- **Profiles on one gateway:** one gateway serves several homes; a ticket is minted for the routed
  profile's home (sibling profiles live under the launch root); a ticket for a profile the gateway
  does not serve fails closed [obs]. Every lifecycle/status/settings REST call carries a profile
  selector, and each freshly created session tab remembers which owner it belongs to [obs].
- **Closing/switching** merely detaches this client; the gateway stops only on an explicit lifecycle
  command [obs]. A failing remote route MUST NOT cause a local gateway to be started [obs].
- **Must not:** relabel GUI sessions as CLI, silently drop unsupported launch options, or fall back to
  an independent local serve process when the runtime lacks a capability [obs].

### 3.5 First-run choice and in-app installer

- **Trigger:** resolver rung 6 and no saved remote connection.
- **Choice screen:** "Connect to existing" (remote setup) vs local install [obs]. Decision values:
  continue-local, remote-applied, reset [obs]. Remote setup probes the gateway to learn token vs OAuth
  auth, requires both an HTTP and a WebSocket test to pass, then saves via the same encrypted config
  path Settings uses; a saved remote skips the chooser next launch [obs].
- **Local install flow (main-driven):** spawn the platform installer script in "manifest" mode to get
  the ordered stage list, then run each stage by name, streaming lines as log events [obs]. Events:
  `manifest { stages: [{name,title,category,needs_user_input}] }`, `stage { name, state:
  running|succeeded|skipped|failed, error?, durationMs?, json? }`, `log { stage?, line, stream }`,
  `complete { marker }`, `failed { stage?, error }` [obs]. On success a bootstrap-complete marker
  (schema version 1) is written and the resolver re-runs [obs].
- **Recovery:** cancel aborts; failure latches (see §3.6) until Retry/Repair/Reset; repair bypasses a
  usable runtime to re-run the installer; at most 3 soft repair attempts [obs]. On Windows a failed or
  missing runtime may be handed off to the separate setup installer, after which the app restarts when
  recovery completes [obs]. The installer log ring keeps 500 lines [obs].
- **Updates block installs:** a local start waits while an update owns the runtime (§3.26) [obs].

### 3.6 Boot failure, latching and retry

- **Latching:** once a LOCAL boot fails in this process, every later start rethrows the same error
  without re-running the installer (otherwise retries would restart an install lasting 5–10 minutes
  underneath the failure overlay) [obs]. Remote failures do NOT latch (transient) except: confirmed reauth rejection
  (latched separately so the overlay's sign-in button stays), SSH host-key change (one field bundle
  showed 157 consecutive retries over 2.5 h), rejected SSH credentials, and a dead local ssh client
  [obs]. Reset/repair/apply-config clear the latch [obs]. The latched-path refusal is silent to avoid
  flooding the bounded log ring [obs].
- **Retryable flag:** main marks transient remote failures retryable; the renderer retries the whole
  boot with full-jitter backoff, base 2,000 ms, at most 5 attempts, then shows the failure overlay with
  Retry / Settings [obs].
- **Attempt fencing:** each start gets an attempt token; a superseded attempt's late success MUST NOT
  publish a stale descriptor and its failure MUST NOT clear a newer attempt's profile pin [obs].
- **Failure overlay:** distinct states for install, onboarding, connecting, boot failure and reauth,
  sharing visual primitives; a failed cloud backend shows a cloud-down variant keyed on structured
  data, never string matching [obs].
- **Test hooks:** env switches simulate a fake boot with step delays (650 ms in the dev script) and a
  fake boot error [obs].

### 3.7 Supervision, liveness and sleep/resume

- **Local backend exit after ready:** the app restarts it and shows a "stopped working in the
  background" notice; the log keeps the exit code and recent backend output; after 3 restarts within
  2 minutes it stops respawning and shows a "keeps crashing" notice [obs].
- **Health:** readiness wait up to 180,000 ms polling every 500 ms; each probe 5,000 ms [obs].
- **Remote liveness:** probe timeout 10,000 ms; 3 failures within 60,000 ms (15 minutes for pooled
  secondaries) mark the route dead; after power resume, revalidation is held off 15,000 ms and wake
  reconnects are coalesced per window [obs].
- **Renderer reconnect loop:** full-jitter backoff (base 300 ms, cap 15,000 ms, exponent capped at
  32; a socket open ≥ 5,000 ms resets the ladder); a non-blocking warning toast only after 300,000 ms
  of continuous failure; the transcript stays readable and draftable throughout [obs]. During an
  in-flight turn one ping timeout defers to a bounded re-probe; only a streak of missed pings
  rebuilds the transport [obs].
- **Renderer crash:** reload, bounded to 3 reloads per 60,000 ms; a renderer bundle generation check
  detects mixed asar/unpacked generations after a broken self-update [obs].
- **Orphans:** on start the lock holder reaps backends recorded in an ownership file whose PID and
  process identity (start time / parent) still match; unrelated processes are never killed [obs].
- **Quit:** if any window reports active work and the backend is app-owned (SSH), ask first, listing
  up to 4 items; local/remote/cloud backends outlive the app so the prompt says so instead [obs].

### 3.8 The bridge (preload surface)

- One global object exposed via `contextBridge` with ~259 channel uses; functions are thin
  `invoke`/`send`/`on` wrappers plus a few static launch facts (glass supported, translucency
  supported, local-models enabled, assume-fresh-install in dev, local skin snapshot) [obs].
- Groups (names in §2): connection & boot, profiles, windows & chrome, filesystem, git, terminal,
  preview, HUD, quick entry, pet overlay, wake indicator, screenshot, updates, notifications, power,
  find-in-page, context menu & spellcheck, clipboard, secrets policy, deep links, OAuth callbacks,
  prepared-submission journal, logs & metrics, uninstall, theme marketplace, SSH config [obs].
- Expected 404s from the API proxy are unwrapped into a typed "not found" result rather than an
  exception (avoids Electron logging every expected miss) [obs].
- Sender checks: only a few handlers (screenshot, modifier gesture, prepared submissions) verify the
  sender is a main frame on the app origin [obs]; the rest rely on the guest isolation in §3.28 [inf].

### 3.9 Renderer ↔ agent transport

- **Shared client package** (also used by the browser dashboard): a JSON-RPC channel over WebSocket
  with a pending map, per-call timeouts and AbortSignal, heartbeats, and a typed gateway contract
  generated from an OpenRPC document [obs].
- **Numbers:** default request timeout 120,000 ms (gateway client) / 30,000 ms (desktop REST helper
  default) / 60,000 ms (startup requests) / 1,800,000 ms (prompt submit) / 300,000 ms (approval
  respond; renderer uses 330,000 ms because the backend approval window defaults to 300 s); replay
  request 10,000 ms; connect 15,000 ms; heartbeat every 15,000 ms with a 45,000 ms deadline and at
  most 8 outstanding pings; replay cursor cap 256 [obs].
- **Frames:** `Frame { id?, method?, params?, result?, error? { code, message, data } }`; a frame with
  a string id and a method other than the generic event method is a server→client request [obs].
  Method-not-found is −32601; a custom "session not shown here" code 4404 lets a window decline
  [obs].
- **Server→client requests:** `ServerRequest { id, method, params { session_id? }, respond, fail,
  decline?, replayed? }`; the first respond/fail wins; a request re-delivered after reconnect reuses
  its id, so a second answer from an outdated card has no effect on the wire [obs]. "Decline" is only sent to backends that
  advertise they count declines (capability handshake); older backends settle on the first error
  [obs].
- **Replay:** after reconnect the client asks for events since a cursor per session and receives
  still-open requests again (marked replayed) [obs].
- **REST:** every REST call goes through main (adds credentials, routes by connection/profile), never
  straight from the renderer [obs].
- **Canonical protocol adapter:** native canonical sockets create sessions with source "gui" (not
  CLI); a foreground draft keeps its request id after an uncertain create ACK; an input carries an
  admission id distinct from the input id; title/archive become a compare-and-set mutation with an
  expected revision (an explicit revision conflict needs a fresh snapshot before retry); stop, approval
  and clarify resolve the exact projected prompt id + execution generation, never "the latest" [obs].
- **Prepared-submission journal:** before an input is admitted, main writes a private atomic-file
  journal entry; it is retired only after a matching accepted receipt; unknown receipts keep it for an
  explicit retry [obs]. The renderer additionally keeps an in-flight turn journal (version 1, ≤ 4 MiB
  per session store, entries ≤ 160 KiB, ≤ 24 messages, text parts ≤ 64 KiB, metadata ≤ 2 KiB,
  ≤ 256 attachment refs, 7-day max age, writes throttled to 400 ms) [obs].
- **Turn-state watchdogs:** a session with no events for 5 minutes is checked; live-turn probe
  timeout 15,000 ms [obs].

### 3.10 Connections: local, remote URL, cloud, SSH

- **Registry:** `ConnectionRegistry { connections: RegistryConnection[], primaryId, lastUsedId?,
  launchMode: primary|last-used, quarantined: QuarantinedEntry[] }`; `RegistryConnection { id, kind:
  local|remote|cloud|ssh, label (unique, case-insensitive), url?, authMode?: token|oauth|basic,
  token?: Secret, headers?: Record<string, Secret>, ssh?: { user, host, port, remoteProfile },
  profiles? }` [inf from module docs]. Exactly one local entry always exists and cannot be removed;
  removing the primary retargets to local [obs]. Malformed entries are quarantined verbatim and
  re-persisted, never dropped [obs]. Duplicates rejected: one local; remote+cloud deduped on
  normalized URL (trim, strip trailing slashes, lowercase); SSH on `user@host:port` + remote profile
  [obs]. Legacy single-connection config is imported once and left untouched for older builds [obs].
- **Handles:** if two registered gateways expose an identically named profile, the UI shows
  `@<profile>-<label-slug>` [obs].
- **Remote URL auth:** probe the gateway's public status for auth providers/flows; token mode stores a
  long-lived token; OAuth uses native sign-in when the gateway advertises it, else an embedded login
  window on a dedicated persistent cookie partition [obs]. Native sign-in follows RFC 8252: loopback
  listener on 127.0.0.1 random port, system browser, PKCE, gateway brokers the upstream IdP, app
  stores access + rotating refresh tokens, refreshes near expiry, clears on terminal failure [obs].
  OAuth connections mint a fresh WS ticket on every dial and never reuse a cached URL; only a
  confirmed 401/403 means re-authentication [obs].
- **Extra headers:** per-connection access-proxy headers (e.g. zero-trust client id/secret) are
  attached only to matching gateway requests; https and wss count as the same origin; transport- and
  app-managed headers (Authorization, Cookie, Host, Origin, Referer, the app's session-token header)
  are dropped [obs].
- **Cloud:** sign in once, discover agents (organization picker when several orgs), connect; cloud
  entries are skipped by fleet updates [obs].
- **SSH:** uses the system OpenSSH client so `~/.ssh/config`, agent, ProxyJump and hardware keys work;
  ControlMaster with ControlPersist 300 s, hashed control socket under a short tmpdir to stay within
  the 104-byte socket path limit, BatchMode always, host keys trust-on-first-use with change failing
  closed (verbatim OpenSSH error shown) [obs]. Timeouts: connect 15,000 ms, exec 20,000 ms, forward
  15,000 ms, remote probe watchdog 15 s, tunnel child restarts ≤ 5 with 1,000 ms delay, keepalive
  every 60,000 ms [obs]. The app can bootstrap and update the remote serve process over SSH, with a
  durable recovery record for interrupted updates [obs]. Windows remotes need CLIXML-noise stripping
  from PowerShell output [obs].
- **Connection test** exercises the same leg that will be used (HTTP and WebSocket), never only an
  HTTP status probe [obs].
- **Remote execution boundary:** in remote mode tools, terminals and file operations run on the remote
  host; client-only files (e.g. an attached image) are shipped as bytes, never as a client path [obs].
  Native file dialogs disable WSL path bridging for remote paths [obs].
- **Preview reach:** for SSH remotes only, a local→remote port forward makes "localhost:N" links from
  the agent work in the local preview [obs].

### 3.11 Profiles, routing and switching

- **Active profile:** persisted in userData; one launch-profile decision per boot attempt feeds
  routing identity, the profile launch argument and the child env together; a "remember" landing during
  startup applies to the next boot [obs].
- **Default profile/route:** "Set as default" is honoured at launch and for plain new chats; explicit
  profile/project actions keep their own destination; new windows inherit the opener's device and
  profile only at startup [obs].
- **Three switch shapes** (the classic bug is confusing them) [obs]:
  - Connection/mode apply (local ↔ remote ↔ cloud) = soft re-home: shell stays mounted,
    gateway-bound stores are explicitly wiped (query invalidation alone is not enough), then reconnect.
  - Runtime home change = hard re-home: window reloads.
  - Live profile swap in one window: activate the other profile's socket; background profiles keep
    streaming; lists merge; only an explicit selection starts a fresh foreground draft.
  Once a switch completes, the current socket, current profile and current connection MUST
  agree, or REST/fs calls route to the wrong backend [obs].
- **Profile rename/delete:** every profile-keyed renderer storage family must join both the rename
  migration and the drop routine, else restores point at a non-existent backend [obs]. Main prepares
  rename/delete requests (tears down connection-scoped backends first) [obs].
- **Gone session:** a verifiably missing session drops the window to a fresh draft that adopts the
  unsent text once and shows an inline undoable "restored" strip — no toast, navigation or focus steal,
  and never clobbering a non-empty fresh draft [obs].
- **Timings:** switch commit 20,000 ms, remember 5,000 ms, registry read 5,000 ms, descriptor lookup
  20,000 ms; active gateway open wait 8,000 ms; secondary sockets live at least 30,000 ms; a stalled
  secondary gets 3 dial attempts [obs].

### 3.12 Windows and navigation

- **Window kinds** (one renderer bundle; kind chosen by a query flag before the hash) [obs]:
  | Kind | Purpose | Notes |
  |---|---|---|
  | main | full shell | frameless with title-bar overlay; hidden until first paint; close-to-tray optional |
  | secondary | one popped-out session, no global sidebar | "watch" flag resumes lazily (spectator) |
  | instance | full peer window for another profile/connection | does not switch the source window |
  | browser popout | detached preview browser tab | reports close back to opener |
  | HUD | always-on-top floating chat bar | transparent |
  | quick | global-hotkey mini composer | transparent |
  | overlay | desktop pet sprite | transparent, own zoom |
  | wake | wake-word listening indicator | transparent |
  Transparent kinds force a transparent background in the same task as the stylesheet to avoid a
  white flash [obs].
- **Chat window web preferences:** context isolation on, sandbox on, node integration off, webview
  tag on, devtools on, autoplay without gesture (voice), focus-on-navigation off (prevents a streaming
  SPA navigation from raising a blurred window) [obs]. Stream-aware throttling keeps painting a
  streaming window while blurred/minimized without pinning visibility at idle [obs].
- **Chrome:** title bar height 34 px; macOS traffic lights positioned inside the band; Windows/Linux
  native controls via the title-bar overlay; WSLg draws its own controls [obs]. Window state persisted
  (min 400×620, at least 48 px must stay visible on some display) and restored including maximized
  [obs].
- **Zoom:** whole-window UI scale (default level 0, max 9, half-step shortcuts); re-asserted after
  show/restore/resize/display moves, every full load and in-page navigation (Chromium drops or
  per-URL-remembers zoom) with 100 ms / 300 ms ×3 settle checks [obs].
- **Routes (hash):** chat at root and `/:sessionId`; pages `capabilities`, `messaging`, `artifacts`;
  overlay routes `settings`, `command-center`, `cron`, `profiles`, `agents`, `starmap`, `webhooks`,
  `session-import`; legacy `sessions/:id` redirects; unknown → new chat; plugin-contributed one-segment
  pages reserved like built-ins [obs].
- **IA rules:** chat is home; pages are durable destinations in shell chrome; overlays are short tasks
  that return to the previous route on close; pickers/dialogs layer above and are not navigation
  stacks; panes are working context that survive hiding and chat switches [obs].
- **Pane shell:** a tree of zones with tab strips; drag tabs between zones; zones minimize to
  restore rails; stateful panes (preview browser, terminal) offer Hide (body stays mounted and inert)
  instead of minimize; Close releases them [obs]. Layout presets Basic, Focus, Default, Terminal deck,
  Quad; saved layouts remember closed panes [obs].
- **Interface mode:** Advanced (default) vs Simple (chat-first: hides status bar, profile rail when
  only one profile, terminal/files/review, technical tool view, inline diffs, artifacts/cron rows;
  thinking collapsed). Each mode keeps its own arrangement; keybinds still work in Simple [obs].
- **Tabs & windows shortcuts:** new tab, cycle, jump 1–9, close (except over a terminal), reopen last
  closed, new window, toggle left/right sidebars, swap sides [obs]. Holding the platform modifier
  400 ms shows slot numbers on tab status dots [obs].

### 3.13 Chat surface

- **Transcript** built on assistant-ui: streaming markdown (Streamdown + Shiki + KaTeX + Mermaid),
  reasoning blocks (hideable via a config key; the reasoning slash command flips it live), tool-call rows
  with structured summaries and expandable outputs/images, interim messages, async cron/delegation
  completions as collapsed disclosures, sticky user messages that clip content scrolling beneath them
  [obs]. Tool rows use red only for explicit failures; ambiguous exit-1 or missing paths are neutral
  [obs]. Nested scrollers hand vertical scrolling back to the thread at their edges [obs].
- **Transcript integrity guarantees** (tested by an oracle): every persisted user/assistant message
  renders exactly once, in order; nothing unpersisted renders; no transient duplicates or reorderings;
  concatenated streamed deltas equal what the provider sent; one live socket per backend [obs].
- **Windowing:** long sessions page history; a timeline rail (one marker per prompt) jumps to a turn
  and "show earlier" pages backward from there; per-session reading position restored from local
  storage; "scroll to bottom" returns to live follow [obs]. Reads stay logarithmic on tool-heavy
  sessions (e2e) [obs].
- **Composer:** rich editor with slash and @-mention completion, drag-drop/paste attachments,
  history recall with up/down in an empty composer, directive chips with a 500 ms hover grace,
  pop-out (single floating composer that follows the hovered pane), per-conversation status stack
  (todos expanded; tasks, subagents, background jobs, queue, file/preview suggestions) with a drawer
  ridge to hide it [obs].
- **Busy actions:** while a turn runs the user can Stop, Steer (inject guidance before the reply it
  redirects) or Queue; the queue lives on the gateway (survives restarts, shared by all windows); Stop
  or Esc with queued items pauses the queue; a turn lost to a gateway restart shows a single Discard
  and blocks the items behind it; auto-drain gives up after 4 attempts [obs].
- **Approvals & clarify:** one persistent transcript-level card-stack host owns approvals (may arrive
  before the tool starts; execution must not remount the stack); gestures never grant approval;
  clarify batches render one card and answer via per-question locks [obs]. Sudo password dialog keeps
  the backdrop unblurred and shows the full selectable command [obs].
- **Slash commands:** client-side curation hides terminal-only / messaging-only built-ins but MUST let
  every user extension (skill-derived and quick commands) through both completion paths; desktop-owned
  built-ins run locally; others go through the gateway's slash-exec then command-dispatch fallback; a
  skill command resolves to a prompt that is submitted normally; an offline registry JSON is the
  fallback catalog [obs].
- **Model picker** in the composer: sticky per-device selection that follows new chats and never
  writes the profile default (except the very first pick on a profile with no default); per-model
  effort/fast presets; favorites; separator-insensitive search; a reasoning pill showing effort with
  clamp display; risky switches ask first [obs].
- **Voice:** dictation (client-direct STT honours the configured timeout, default 60 s), read-aloud,
  wake word, full voice conversation; active capture pins the composer recipient [obs].
- **Status bar:** per-session YOLO toggle, context meter with breakdown popover, optional cache hit
  rate and tokens/s (last 10 calls), workspace, model, approvals, timers, terminal, backend version,
  gateway menu with Reconnect; items customizable; toggle shortcut [obs].
- **Find in page** over the rendered transcript; on Linux the shortcut is claimed in main because the
  keydown may not reach the renderer [obs].
- **Failed turns** render an error card naming the failing layer (provider/model, custom endpoint,
  streaming, auth, billing, gateway, local runtime, disk) with matched recovery actions [obs].
- **Image drafts:** if the app closes before an image message is acknowledged, reopening that chat
  offers a "restore image draft" action (never auto-sends) [obs].

### 3.14 Sidebar: sessions, projects, worktrees, profile rail

- Sessions list with pin, archive, search (also by id, scoped to the selected profile), unread dots
  that survive restart, background-process dots, grouping (e.g. by project; each project previews its
  3 most recent sessions with "show all") [obs].
- Projects own workspace folders: a project may own several folders, repos, worktrees and sessions; a
  bare new chat stays detached unless a project or default directory applies; no per-session folder
  picker [obs]. Repo discovery scans home to a bounded depth, configurable per profile (enable, roots,
  excludes) [obs].
- Worktrees: create on a new branch, own sidebar lanes, remove with delete-or-hide (force when dirty);
  a chat follows the agent into a worktree it created, unless the user deliberately chose the
  workspace [obs].
- Profile rail of colored squares (create/import outside the scroll), condensed for many profiles,
  context menu "open in new window" / "set as default"; can be hidden (a status-bar dropdown then
  replaces it); a gateway selector appears with >1 registered gateway [obs].
- Bot-mode chats are always hidden from the sessions list (§3.20); compression lineages show as one
  row; branch children are their own titled rows [obs].

### 3.15 Right-side panes

- **Preview browser:** a `<webview>` on a persistent dedicated partition with no popups; supports
  back/forward gestures, capture to image, open in OS browser, pop-out window, file previews with
  live reload (debounce 120 ms; text preview ≤ 512 KiB), annotate mode (pins with notes; redacts
  password/hidden values and secret-looking attributes; batches grouped by DOM subtree; never sends
  by itself), and agent-driven automation via server requests (act/read) [obs]. A fullscreened guest
  can always be escaped via a main-process key hook and an out-of-band deep link [obs].
- **Files:** tree of the session workspace (local fs IPC or remote picker), drag-drop, rename, trash,
  reveal; files watched [obs].
- **Review (git):** branch, ahead/behind, changed files list/tree, diffs scoped Uncommitted / Branch
  / Last turn (last two read-only), stage/unstage/revert, commit (generate message), commit & push,
  create PR via the GitHub CLI or ask the agent to; git runs locally through bundled git on Windows,
  system git elsewhere, with console windows suppressed [obs].
- **Terminal:** node-pty in main, xterm (WebGL) in the renderer; multiple terminals in a rail; shells
  persist while hidden; env scrubbed; locale fixed up on Linux; "add selection to chat"; an agent
  terminal stream view [obs].

### 3.16 Settings

Overlay with breadcrumb navigation, searchable (search and saved links resolve to the owning child
page), "Applies to" profile scope chip row when ≥ 2 profiles (follows active profile by default;
switching active profile resets it) [obs]. Views and child pages [obs]:

| View | Child pages |
|---|---|
| Model (config) | main, fallbacks, auxiliary (per-task provider/model/effort), mixture-of-agents |
| Chat (config) | behavior (personality, timezone, reasoning), attachments (image input mode) |
| Appearance | general, theme (presets, VS Code Marketplace import), typography (UI scale 100 %, chat text 110 %), window layout (glass, minimize to tray), chat display, pet |
| Workspace (config) | projects (default cwd, repo scan), shell (persistent shell, env passthrough), files |
| Safety (config) | approvals (allowlist), privacy, checkpoints |
| Browser (config) | profile (use real profile), network (private URLs) |
| Memory (config) | persistent memory, context & compression |
| Voice (config) | conversation, transcription, speech |
| Advanced (config) | desktop (updates), runtime, tools, terminal, delegation, output; quick entry; keep awake |
| Providers | accounts (OAuth), API keys, custom endpoints (API mode auto/chat/responses/messages; Test hits the real route), local models (on-device runtime) |
| Gateways | connection mode, registered gateways, devices/pairing, managed updates, per-profile overrides, secure storage toggle |
| Keys / Vault | credentials, sources |
| Keyboard | shortcuts (rebind ~70 actions, conflict detection), HUD gesture, screen capture |
| Notifications | alerts, sounds |
| Sessions | archived, default directory |
| Plugins | redirects to Capabilities → Plugins |
| Billing | plan, usage, auto-reload, step-up (vendor accounts only) |
| About | updates (channel, check, apply), uninstall |

Config-backed pages read a schema from the gateway and write patches through the config-set RPC; secret env
rows support reveal/save/clear [obs].

### 3.17 Onboarding (provider setup)

- After a usable backend: an overlay that blocks until a provider is configured or skipped
  ("choose later") [obs]. Paths: featured vendor OAuth (device code with copy), other OAuth providers,
  API-key form listing every key-based provider the CLI knows plus a generic "paste KEY" option, local
  model runtime, custom OpenAI-compatible endpoint [obs]. After sign-in a model-confirm panel [obs].
- Free tier: state is pulled from the backend (status + acknowledge-notice RPCs), never latched in
  local storage; every sign-in entry point opens one claimed dialog whose states map to the poll
  route's status/reason; never branch on provider display names; a hidden challenge window may be
  revealed only when the vendor page asks for human interaction (fragment states working /
  interactive / done / failed), pinned to the vendor origin [obs].
- First-run guide: a dedicated landing with the long-op loader until the guide conversation loads; no
  composer or greeting early; resumed runs reopen as normal chats; the start RPC has no deadline but
  past the boot budget a way out (counts as skip) is offered; the chosen layout sets Simple vs Advanced
  [obs].

### 3.18 Capabilities page

- Tabs: Skills (installed with toggles, built-in optional catalog with one-click install, embedded
  hub picker pinned to its picker URL, install deep links with confirmation, provenance, archive),
  Toolsets (per-toolset detail and tool lists), Connectors (MCP servers: local and hosted, add form,
  OAuth via a loopback callback, test, per-tool enable, accounts), Plugins [obs].
- Plugins: one row per plugin with a Desktop column (app-level, loaded from the home's desktop-plugin
  folder) and an Agent column (installed into the selected profile); badges agent/desktop/both; install
  from catalog (pinned SHA) or git (optional 40-char pin); uninstall removes files; unified packages
  copy their desktop half into the app-level folder [obs].
- Scope selector picks the profile for profile-scoped tabs [obs].

### 3.19 Other management surfaces

- **Messaging:** per-platform cards (credentials, allowlist, enable), Telegram QR quick setup (creates
  the bot, detects user id, saves, restarts gateway); a "restart now" banner persists until the gateway
  actually restarted [obs].
- **Webhooks:** list/create/enable routes [obs].
- **Cron (Scheduled jobs):** list (optionally all profiles aggregated; actions always go to the
  owning profile), create from blueprints (deep-linkable), run history, pause/resume/edit/delete [obs].
- **Profiles:** create, rename, delete, export/import archive (keys stripped; desktop adds appearance
  and layout overlay), SOUL editing (missing file explained and created) [obs].
- **Agents:** live delegation view (workers, active counts, tools, files) [obs].
- **Command Center:** sections sessions, system, usage, maintenance; log viewer over 4 log files with
  level filter [obs].
- **Memory graph ("star map"):** force-directed graph of skills/memories over time with playback,
  filters All/Used/Learned, node edit/delete, share code of layout only [obs].
- **Artifacts:** gallery of generated images/files/links with jump-back to the producing session;
  remote artifacts download through the gateway using the originating profile/session [obs].
- **Session import:** preview and import foreign sessions from two other coding-agent CLIs [obs].
- **Subagents frame** above the composer (≤ 3 previewed, roster on expand, Steer/Stop) [obs].

### 3.20 Bot mode (bundled plugin)

- Each bot is a profile with an avatar (geometric face, upload, AI portrait, pixel pet), a canonical
  forever-chat, routines (cron), sections, hidden flag, and group chats [obs].
- **Identity invariant:** the canonical chat is identified ONLY by (profile, session with the fixed
  canonical title); a unique-title index guarantees no more than a single matching row. Clicking a bot:
  look up by title including hidden rows (compression lineages resolve to the live tip); found → open;
  none → re-run the lookup (adopt before minting), then create it hidden and kick off the intro [obs].
  No stored session-id pointer, no recency/visibility heuristic — five hardening waves around a pointer
  design failed [obs]. The gateway reports the canonical row on the profiles list so preview and click
  identity match [obs].
- Canonical chats are always hidden from the Sessions sidebar; side-chats stay visible [obs].
- Gateway-owned groups: text-only rooms read via state/log, send/stop via RPC, pending actions only
  from the driver status, discard requires explicit confirmation, retries keep exact identities, no
  hidden-session fallback [obs].
- Bot-to-bot mentions, cross-machine relay, routines pane docked only while the Bots tab is active;
  disable via the plugin's Desktop switch (unregisters live) [obs].

### 3.21 Ambient features

- **HUD:** pop the conversation out into a frameless floating bar that stays above other apps; position gives the agent "what is
  under me" context (window-below enumeration); move by press-and-hold (Ctrl-drag on X11; compositor
  drag on Wayland), resize from edges, reset layout, snap to pointer global hotkey (default
  CommandOrControl+Shift+G), toggle Cmd/Ctrl+Shift+H, cursor poll 60 ms, Hyprland float+pin via IPC
  [obs]. Tap-to-summon modifier gesture (native helpers per OS; release within 0.5 s; off by default;
  macOS needs Input Monitoring) [obs].
- **Quick Entry:** global hotkey mini composer, default CommandOrControl+Shift+Space, validated
  accelerator with ≥ 1 modifier, conflict detection [obs].
- **Screenshot shortcut (macOS):** both Command keys together capture the frontmost window of any app
  and attach it to the last-active composer (needs Input Monitoring + Screen Recording; off by default)
  [obs].
- **Desktop pet** overlay window, generation/hatching flows (generate 420,000 ms, hatch 3,600,000 ms
  timeouts) [obs].
- **Wake word** indicator window (start timeout 180,000 ms) [obs].
- **Tray / minimize to tray:** off by default, per device; hides minimized (and close of main) windows;
  tray "show" restores, "quit" keeps the active-work confirmation; Linux requires a registered
  StatusNotifier host (checked over D-Bus; a tray object alone is not proof); if the host disappears
  hidden windows are restored [obs].
- **Keep awake:** modes never / while working (default when unset) / always; one power-save blocker
  held by main; a timed sleep (≥ 55 s idle) that interrupted a turn while off triggers a one-time offer
  [obs].
- **Notifications** with actions; Linux via D-Bus [obs]. **Completion sounds** [obs].
- **Link titles/favicons:** curl first, then a hidden hardened window [obs].

### 3.22 Plugin SDK and contribution registry

- The shell is contribution-driven: areas for panes, routes (pages), sidebar nav, status bar, title
  bar, palette commands, keybinds, themes, composer extensions, session-row decorations, model-menu
  decorations, appearance settings, plugin settings pages, transcript directives [obs].
- Delivery: disk plugin (single ESM file under the home's desktop-plugin folder, hot-reloaded on
  save, no JSX), unified package (agent plugin folder with a desktop half), bundled (built with the
  app) [obs]. Source size cap 16 MiB [obs].
- Host API tiers: read-only state views, curated actions (toast, navigate, logs, restart gateway,
  subscribe to events), JSON-RPC door, plugin's own REST/socket namespace, UI kit [obs].
- Security: full app authority; isolation is error isolation only (bounded contributions, rollback of
  a throwing register); catalog trust = human review of a pinned commit + import allowlist (SDK and
  React only) [obs].

### 3.23 Deep links and custom schemes

- `scheme://<kind>/<name>?params` parsed into `{ kind, name, params }` [obs]. Kinds: open (navigate to
  an app path), skill install, MCP install, blueprint (cron template), Windows Copilot hardware key
  (only "start" is honoured; others are activation noise), close-preview (handled in main: restore,
  show, focus, exit fullscreen, close preview), plugin hosts [obs].
- Links arriving before the renderer signals readiness are queued (latest one) and flushed exactly
  once on the ready signal; delivery un-hides a tray-hidden window without re-pumping foreground [obs].
- Media scheme streams local files (Range-aware for seeking) or proxies remote gateway media with
  main-side auth [obs].
- On macOS, protocol registration ordering vs the single-instance lock matters (Launch Services can
  relaunch the app for a link) [obs].

### 3.24 Design system

- Principles: flat not boxed; borderless elevation (one shadow token + one hairline token for every
  floating panel); one primitive per concern (Button, SearchField, SegmentedControl, ListRow, Loader,
  ErrorState, LogView, ConfirmDialog, EmptyState/PanelEmpty, CardStack, FanMenu, ComboboxInput);
  tokens not literals; style lives in primitives (no size/padding overrides at call sites); intent
  before automation; immediate feedback with visible rollback [obs].
- Button variants: default, destructive, secondary, outline, ghost, floating, link, text,
  text-strong, grip; sizes default, xs, sm, lg, inline, micro, icon family; text buttons square, icon
  buttons 4 px radius [obs]. Badge variants default, muted, warn, destructive, outline, solid [obs].
- Tooltips only when hover teaches something new; never native `title` on buttons (a test enforces
  it); 200 ms first-open delay, 300 ms warm window, 100 ms exit; placements control/toolbar/row/rails
  [obs].
- Z-index ladder as named tokens (modal backdrop → modal → popover → over-modal → switcher → boot
  chain connecting → onboarding → setup → crash) [obs].
- Icons: Tabler default, Codicons for compact editor/tool vocabulary; never a third set [obs].
- Motion: ~100 ms control transitions; respect reduced motion; motion never delays state; no
  layout-animating transition-all on hot paths [obs]. Card stack: 96 % scale, 7 px offset, 220 ms
  promotion, 180 ms clearance [obs]. Glass default 29 % tint, sidebar only [obs].
- Loading never shows a literal "Loading…" — an animated math-curve loader instead [obs].
- Keyboard: focused surface owns its keys; a single Esc/cancel performs a single action; Esc closes
  dismissable overlays (install/onboarding excluded); confirmation dialogs open focused on Confirm
  [obs]. Unfocused, unhovered panes desaturate 20 % [obs].

### 3.25 i18n and theming

- 9 bundled locales (English, Simplified Chinese, Traditional Chinese, Japanese, Arabic RTL, Russian,
  French, German, Spanish); three are complete typed catalogs, sparse ones overlay English; runtime
  packs from plugins or the backend layer on top [obs]. Every UI string goes through the i18n hook and
  all locales change together [obs].
- 12 theme presets (§2 lists two brand-named ones), light/dark/system, accent overrides, user themes,
  VS Code Marketplace theme search + convert + install, backend skin sync, per-profile theme, chat font
  and terminal font per profile [obs].

### 3.26 Updates

The build stamp declares one update owner; runtime dispatch never infers it [obs]:

| Mechanism | When | Behaviour |
|---|---|---|
| App Installer | sideload MSIX | Check the OS-registered descriptor via WinRT (unknown ≠ none); apply downloads the descriptor before teardown, opens the local `.appinstaller` file, registers a detached relaunch waiter first [obs] |
| Store | Store MSIX | StoreContext check/download/request install, consent UI parented to the window; download finishes before backend shutdown; cancel/unknown ≠ success [obs] |
| electron-updater | macOS bundles | Checks never download; apply rechecks, downloads, waits for Squirrel.Mac acceptance, only then stops app-owned backends and installs/relaunches; download/verify failures leave backends running [obs] |
| windows-handoff / posix-handoff | source checkouts | Detached updater (script or the setup installer in update mode) swaps the checkout and rebuilds the app; app relaunches [obs] |
| external | commit builds, managed deployments | No in-app apply [obs] |
| manual | checkout without updater | User runs the CLI update [obs] |

- **Update marker:** a shared in-home marker (format v2, shared by Python, Rust and the app) names a
  live owner; while live, an app reopened mid-update MUST NOT launch any backend from the runtime under replacement and no
  second update may start; the app never deletes or rewrites the marker [obs]. The update gate also
  consults an in-process flag because the updater stops its backend before committing the hand-off
  [obs].
- **Fleet update:** "update now" updates the connected backend first, then other eligible gateways
  (cloud skipped), then the app last [obs]. After any backend update the app re-checks its own version
  and offers an app update if behind [obs].
- **Skew detection:** renderer bundle older than the runtime (terminal update) and swapped-bundle
  detection after an external swap [obs].
- **Background check** uses a GitHub token from env, then the GitHub CLI login, then anonymous; a
  rejected token is logged once by source and retried anonymously [obs].
- **Channels:** stable, canary, light variants; channel selection where the runtime supports it;
  channel retirement migrates to a qualified destination [obs].

### 3.27 Packaging per OS and variants

- Variants: default (source/bootstrap client), bundled (carries the agent payload: source snapshot,
  pinned CPython + uv, dependency tree, Node/npm, tools, prebuilt TUI/dashboard, manifest, launchers),
  store (bundled under Store identity), light (remote-only, no Python) [obs]. Commit and canary builds
  get suffixed identities and isolated userData [obs].
- macOS: DMG + ZIP (ZIP is the update artifact), hardened runtime, custom signer signing nested
  Mach-O before the app, notarize + staple in after-sign, Info.plist usage strings for audio, camera,
  microphone, calendars, reminders, screen capture, local network, music, contacts, Apple Events;
  entitlements allow JIT, unsigned executable memory, disabled library validation, audio input,
  camera, calendars, reminders; DMG window 660×400, icon size 96 [obs].
- Windows: MSIX only (no NSIS/MSI); min OS 10.0.22621.0, max tested 10.0.26100.0; per-arch packages
  combined into a universal bundle; execution aliases for CLI launchers; Copilot hardware-key provider
  extension; PE certificate-table sanitizing, Azure signing with a content-keyed signature cache;
  exe identity stamped in after-extract (before ASAR integrity rewrites the PE); unvirtualized
  resources capability; sideload version X.Y.Z.0; Store version `year.hour-of-year.second-of-hour.0`
  UTC [obs].
- Linux: AppImage in the config (CLI scripts can also ask for deb/rpm); release legs disabled;
  launcher `.desktop` entry written only after the window is visible on grid launches (GNOME bug)
  [obs].
- asar with unpack of native modules, prebuilds and the renderer dist [obs].

### 3.28 Security posture

- All app windows: context isolation, sandbox, no node integration, web security on [obs]. Renderer
  navigation is confined to the dev server or `file:`; anything else is cancelled and opened
  externally through the audited opener [obs].
- The window-open handler refuses every request and has no URL-opening side effect (advisory: a sandboxed
  iframe without popups can still reach the handler); the one exception delegates http/https/mailto
  from the exact skills-hub frame origin to the audited opener — still without creating a window [obs].
- Preview guest: only the dedicated partition gets the guest preload; it exposes nothing to the page
  and forwards only trusted primary-button clicks on `target=_blank` links; the renderer admits only
  http/https (never `file:`) to the external opener [obs]. The webview MUST NOT get `allowpopups` [obs].
- Embedded hub iframe navigation pinned to its picker URL (escapes are cancelled and the frame
  reloaded) [obs]. YouTube embeds are hosted on a loopback origin with a restrictive CSP [obs].
- No Content-Security-Policy found for the main renderer page [obs] (open question).
- Electron fuses: no explicit fuse configuration in the app folder; builder comments mention a fuses
  pass and ASAR integrity [obs] (needs REA).
- Secrets: keychain encryption is OPT-IN; default writes secrets as plain encoding with owner-only
  0600 files (a broken macOS keychain otherwise prompts every launch); legacy encrypted blobs get one
  migration attempt recorded once [obs]. Remote tokens, access-proxy headers and native OAuth tokens
  use this store [obs].
- Data-URL reads are size-capped (Settings-configurable); attachment upload cap 256 MiB; sensitive
  extensions (key stores) and `.env`-like files guarded [obs].
- Media permission requests are filtered per origin [obs]. Plugins are not sandboxed (§3.22).

### 3.29 Uninstall

- Settings → About offers three levels (app only; app + agent keep data; everything), mirroring CLI
  flags; cleanup runs after the app exits; options hidden for bundled/Nix/externally-owned installs or
  when ownership cannot be confirmed [obs].

### 3.30 Persisted state (userData and home)

legacy single-connection config, connection registry, active profile,
backend ownership, installation record, native OAuth tokens (0600), secure-storage policy, window
state, zoom, HUD state, quick entry, keep awake, minimize-to-tray, translucency, native theme, updates
config, data-URL read max, SSH update recovery, sandbox/GPU markers; in the home: logs (desktop log
flushed every 120 ms, 64 KiB buffer, rotated), bootstrap marker, update marker, desktop plugins,
desktop build stamp [obs].

### 3.31 Native setup installer (separate app)

- **Tech:** Tauri 2 (Rust core + React UI), window 880×620 (min 720×520), hidden until setup shows it;
  CSP limited to self, data images/fonts and the IPC origin; capabilities limited to window close/
  minimize/theme, events, opener, dialog, process, shell; macOS min 11.0 with hardened runtime;
  Windows embeds the WebView2 bootstrapper; targets app, dmg, appimage [obs].
- **Modes:** install (default) vs update (`--update` arg); `--reinstall`/`--repair` force setup [obs].
  On macOS in plain install mode, if the agent is already installed, it relaunches the installed
  desktop app and exits before building any UI (200 ms pause) [obs].
- **Commands exposed to UI:** get mode, start/cancel bootstrap, get status, start update, launch
  desktop, log path, home path, open log dir [obs].
- **Install flow:** resolve the install script (dev checkout env → [bundled fallback not
  implemented] → always download fresh from the repo at a pinned commit or branch; connect 10 s, total
  60 s; failure is fatal so Retry refetches) → run "manifest" (with the include-desktop flag passed to
  both calls) → iterate stages, each a separate script run; a stage host that exits without its JSON
  result frame is retried (3 attempts, 500 ms backoff, cancellable); last JSON line on stdout is the
  result; desktop stage skipped unless requested → write the completion marker with the resolved commit
  [obs]. Output decoding tolerates CP1252 bytes and CRLF; pipes drained with a 20 s grace when a
  grandchild holds them [obs].
- **Update flow:** acquire the shared update marker (stale after 20 minutes; creation-time tolerance
  2 s; empty-marker grace 5 s; mutex wait 10 s; Windows byte-range lease with 16 slots), wait for the
  desktop to exit (20 s, poll 500 ms), wait for install locks, run the CLI update streaming output
  (keep last 40 lines; exit code 2 means a concurrent update), on macOS swap in the new app bundle,
  relaunch, exit 3 s after success [obs].
- **UI screens:** welcome → progress (stage list + log) → success (launch) | failure (retry, open logs)
  [obs].

### 3.32 Native folder

A SQLite FTS5 tokenizer (unicode61 + CJK bigrams) built as a loadable extension into the home's lib
folder, used by the sessions DB for 1–2 character CJK search; disabled via a config key; path
overridable by env [obs]. Not part of the desktop runtime itself (belongs with sessions storage).

### 3.33 What the end-to-end suites cover

- Main Playwright suite (90 s per test, 1 retry on CI, serial, screenshots + traces, visual diffs
  reported not failed): boot, boot failure, chat send/receive, busy stop/steer/queue, onboarding
  overlay, batch clarify, group rooms (approval click, member failure, turn integrity), hidden history
  rows, image attachment resume, interim messages, large-session resume, legacy history, markdown
  overflow, timeline/messages-below bounds, profile rail across gateways, sidebar search profile scope,
  provider setup owner, queue turn boundary, compression + queue stop, submit drift, unread dot
  restart, warm resume without repaint, at-rest token storage (0600, keychain opt-in) [obs].
- Core suite (required CI lane, real backend with only the LLM faked): transcript oracle across every
  transition; interactive prompts; boot lifecycle (one backend, one supervised respawn after kill -9,
  zero orphans after quit, relaunch same home ×3); lineage; remote topology and remote secondary;
  reconnect turn state; interrupted reply; no idle reap; packaged smoke (asarUnpack contract, ≤ 60 s to
  interactive) [obs].
- Update suite: app update moves backend commit and relaunches; build failure messaging; healthy
  install opens straight to chat; concurrency refusal; gate signatures [obs].
- The harness strips agent env vars and credentials from the test env, sandboxes home and userData,
  gives each test a unique app name to dodge the single-instance lock, and skips the quit dialog [obs].

## 4. File map (what to read in the Read step)

Totals [obs]: `apps/desktop/electron/` 653 files, 140,357 lines (325 test files; non-test ≈77,900 lines);
`apps/desktop/src/` 3,091 files, ≈689,450 lines (≈59 % of `app/` is tests); `apps/desktop/e2e/`
17,246 lines; `apps/desktop/scripts/` ≈10,700 lines of build/sign/package code plus ≈6,000 of tests;
`apps/shared/src/` 62,962 lines of which 56,045 are generated contract; installer 11,690 lines
(incl. ≈1,660 of Rust tests and binary icons); `native/` 14,793 lines of which 14,498 are the vendored
SQLite headers.

Generated / vendored — skip, regenerate or replace [obs]: `apps/shared/src/gateway-contract.openrpc.json`
(48,914) and `gateway-contract.generated.ts` (7,131) — generated from the gateway's contract (read only
the method/notification/server-request lists, which §2 already captured); `src/lib/desktop-slash-registry.json`
(offline catalog dump); `src/i18n/*` non-English catalogs (≈45,000 lines of translations; read only
`types.ts` shape and `catalog.ts`); `native/fts5_cjk/vendor/` (SQLite headers); `assets/appx/` (62
generated tile PNGs); installer `src-tauri/icons/*`; `src/debug/` (dev-only counters);
`scripts/diag-*.mjs`, `scripts/probe-*.mjs`, `scripts/perf/` (diagnostic harnesses).

| Sub-area | Reference path | Lines | Read what | Docs / tests to read with it |
|---|---|---|---|---|
| Main orchestration | `apps/desktop/electron/main.ts` | 18,619 | Skim by function (see giant-file note) | `apps/desktop/AGENTS.md`, `ENGINEERING.md` |
| Bridge | `apps/desktop/electron/preload.ts` | 717 | All (channel list = API surface) | `src/global.d.ts` (1,740: bridge typings) |
| Chat window prefs | `electron/session-windows.ts` | 218 | All | `window-reveal.test.ts` |
| Window-open policy | `electron/window-open-policy.ts` | 106 | All | `window-open-policy.test.ts`, ENGINEERING "guest content" |
| Guest preload | `electron/preview-guest-preload.ts` (+ `-entry.ts` 27) | 66 | All | — |
| File/secret hardening | `electron/hardening.ts` | 657 | All | `hardening.test.ts` (1,109) |
| Secrets policy | `electron/secret-storage-policy.ts` | 102 | All | `e2e/at-rest-connection-token.spec.ts` |
| Local gateway attach | `electron/local-gateway.ts` | 649 | All | `docs/canonical-gateway-protocol.md`, `local-gateway.test.ts` |
| Startup sequencing | `electron/primary-backend-startup.ts` | 164 | All | `backend-ready.test.ts` |
| First-run gate | `electron/first-run-setup-gate.ts` | 148 | All | `e2e/update/first-run.spec.ts` |
| In-app installer runner | `electron/bootstrap-runner.ts` | 1,048 | Event protocol, stage loop | `bootstrap-failure-copy.ts` (88) |
| Connection registry | `electron/connection-registry.ts` | 1,807 | Shape rules, dedupe, quarantine | `connection-registry.test.ts` (2,311) |
| Legacy connection config | `electron/connection-config.ts` | 1,219 | Probe/test/apply rules | `connection-config.test.ts` (1,609) |
| Profile routing | `electron/profile-session-routing.ts` | 1,369 | Routing ladder | `profile-session-routing.test.ts` (1,201) |
| SSH transport | `electron/ssh-connection.ts` | 1,433 | Options, timeouts, failure classes | `ssh-connection.test.ts` (1,649) |
| SSH remote lifecycle | `electron/remote-lifecycle.ts` | 1,833 | Skim bootstrap/probe steps | `remote-lifecycle.test.ts` (2,246) |
| SSH-managed update | `electron/managed-ssh-update.ts` | 1,315 | Ordering + recovery record | `managed-ssh-update.test.ts` (1,301) |
| Windows remotes | `electron/windows-remote-lifecycle.ts` | 853 | CLIXML normalizer, probes | test (852) |
| Native sign-in | `electron/native-oauth.ts`, `native-oauth-login.ts`, `native-token-store.ts` | 255 / 215 / 165 | All | website guide "native sign-in" |
| Cookie partitions | `electron/oauth-partition.ts` | 248 | Partition-name invariant | `oauth-partition.test.ts` |
| Liveness | `electron/remote-liveness.ts`, `backend-health.ts` | 482 / 342 | Constants + decisions | their tests |
| Crash fallbacks | `electron/windows-sandbox-fallback.ts`, `linux-gpu-fallback.ts`, `linux-nvidia-egl-fallback.ts`, `windows-stack-cookie-fallback.ts` | 429 / 397 / 317 / 322 | Marker state machines | their tests |
| Media scheme | `electron/media-protocol.ts`, `media-range.ts` | 211 / 189 | All | `media-protocol.test.ts` |
| Deep links | `electron/deep-link-protocol.ts`, `dock-launch-order.ts`; main.ts `handleDeepLink` | 48 / 33 | All | — |
| Terminal | `electron/terminal-ipc.ts` | 425 | All | — |
| Git | `electron/git-review-ops.ts`, `git-worktree-ops.ts`, `git-ipc.ts`, `no-console-git.ts` | 886 / 577 / 116 / 393 | All | their tests |
| Filesystem | `electron/fs-ipc.ts`, `fs-read-dir.ts`, `local-read-path.ts` | 247 / 111 / 38 | All | — |
| Update marker + gate | `electron/update-marker.ts`, `update-marker-judge.ts`, `update-gate.ts`, `update-marker-gate.ts` | 840 / 245 / 172 / 512 | Rules A7 and gate signals | shared marker corpus fixture (repo `tests/fixtures/`) |
| Updater strategies | `electron/updater/` (index 93, checkout 771, app-installer 233, mac 142, store 90, relaunch-waiter 367, channel* ≈1,100) | ≈4,250 non-test | index → per-mechanism files | `website/docs/developer-guide/macos-bundle-updates.md`, `BUILDING.md` |
| Updater process | `electron/updater-process.ts` | 816 | Hand-off contract | `updater-process.test.ts` |
| Quick entry / HUD / tray | `electron/quick-entry.ts`, `hud-*.ts`, `minimize-to-tray.ts`, `tray-host.ts` | 501 / ≈1,614 / 356 / 119 | Behaviour rules | their tests |
| Window-below context | `electron/window-below.ts` | 336 | Picking logic | — |
| Quit guard | `electron/quit-guard.ts` | 158 | All | — |
| Free-tier challenge | `electron/challenge-window.ts` | 376 | Fragment state machine | test (385) |
| Renderer entry | `src/main.tsx` | 125 | All | — |
| Routes | `src/app/routes.ts`, `src/app/contrib/surfaces.tsx` | 318 / 220 | All | `routes.test.ts` |
| Renderer boot/reconnect | `src/app/gateway/hooks/use-gateway-boot.ts` | 1,751 | Constants, state ladder | test (2,865) |
| Gateway sockets store | `src/store/gateway.ts` | 2,484 | Socket pool, leases | — |
| Session state | `src/store/session-states.ts`, `session.ts` | 3,132 / 1,877 | Rename/drop migrations, watchdogs | tests (1,890 / 1,793) |
| Streaming | `src/app/session/hooks/use-message-stream/` (index 1,259; server-requests 866) | ≈3,000 | Event → transcript mapping | e2e core oracle |
| Submit/slash | `src/app/session/hooks/use-prompt-actions/` (submit 1,382, slash 1,548, index 1,494, rewind 874) | ≈5,500 | Submit ordering, slash dispatch | `index.test.tsx` (5,938) |
| Session actions | `src/app/session/hooks/use-session-actions/index.ts` + `utils.ts` | 3,425 / 2,585 | Skim | test (7,246) |
| Slash policy | `src/lib/desktop-slash-commands.ts` | 954 | All | `src/AGENTS.md` slash section |
| Turn journal | `src/lib/inflight-turn-journal.ts` | 1,174 | Limits, retire rules | — |
| API client | `src/api/client.ts`, `canonical-protocol.ts`, `sessions.ts`; `<api-barrel>` (133) | 811 / 684 / 721 | Scoping, timeouts | `api/canonical-*.test.ts` (≈20 files) |
| Composer | `src/app/chat/composer/index.tsx` (+ hooks, focus 781, rich-editor 916) | 1,563 | Skim | — |
| Sidebar | `src/app/chat/sidebar/index.tsx`, `profile-switcher.tsx`, `projects/workspace-groups.ts` | 2,083 / 1,637 / 1,015 | Skim | `e2e/sidebar-*.spec.ts` |
| Preview pane | `src/app/chat/right-rail/preview-pane.tsx`, `preview-file.tsx`, `preview-act.ts` | 1,607 / 1,301 / 848 | Security bits + act protocol | `lib/preview-act/`, `lib/preview-annotate/` |
| Right sidebar | `src/app/right-sidebar/` (terminal, files, review) | 7,164 | Skim | — |
| Pane shell | `src/components/pane-shell/` (tree store 2,282) | 10,641 | Layout model | — |
| Settings | `src/app/settings/` (gateway 1,538, model 1,632, appearance 1,103, providers 637, index 597, subpages) | 26,308 | Page list, scope chip | — |
| Onboarding | `src/components/onboarding/` (index 954) + `components/remote-setup/` (831) | 2,569 | Flow states | `e2e/onboarding.spec.ts`, core onboarding spec |
| Capabilities | `src/app/capabilities/` | 12,171 | Tab list and install flows | website plugin docs |
| Other surfaces | `src/app/{cron,messaging,webhooks,profiles,agents,command-center,starmap,artifacts,session-import,hud,quick-entry}/` | ≈15,000 | Skim per feature | user guide sections |
| Bot mode | `src/plugins/<bots-plugin>/` (canonical-chat 709, group-chat 1,907) | 32,392 + 23,768 tests | Canonical-chat + groups | `src/AGENTS.md`, `docs/canonical-groups.md`, `website .../bot-mode.md` |
| Kanban plugin | `src/plugins/kanban/` | 6,180 | Optional | — |
| Plugin SDK | `src/sdk/index.ts`, `src/contrib/registry.ts`, `runtime-loader.ts` | 2,018 / 162 / 1,119 | Contract + loader allowlist | `website .../desktop-plugin-sdk.md` (2,047) |
| Design tokens | `src/styles.css`, `src/components/ui/` | 3,888 / 9,280 | Token names, primitives | `DESIGN.md` (576) |
| Themes / i18n | `src/themes/`, `src/i18n/catalog.ts`, `types.ts` | 3,064 / 31 / 4,366 | Mechanism, not strings | — |
| Keybinds | `src/lib/keybinds/actions.ts` | 378 | Action ids + defaults | — |
| Shared client | `apps/shared/src/json-rpc-channel.ts`, `json-rpc-gateway.ts`, `reconnect-backoff.ts`, `websocket-url.ts`, `translucency.ts` | 613 / 842 / 58 / 151 / 495 | All | `json-rpc-gateway-replay.test.ts` (911) |
| Packaging config | `electron-builder.config.cjs`, `product-identity.cjs`, `update-feed.cjs`, `package.json`, `vite.config.ts` | 350 / 109 / 40 / 199 / 274 | All | `BUILDING.md` (399) |
| Packaging hooks | `scripts/before-build.mjs`, `before-pack.mjs`, `after-extract.mjs`, `after-pack.mjs`, `notarize.mjs`, `mac-sign.mjs`, `batch-sign-binaries.mjs`, `sign-msix.mjs`, `write-build-stamp.mjs`, `bundle-env.mjs` | ≈10,700 total scripts | Order of hooks, stamp contents | their tests |
| Setup installer | `apps/bootstrap-installer/src-tauri/src/{lib,bootstrap,install_script,powershell,update,marker,paths,events}.rs`, `src/store.ts`, `src/routes/*` | 254 / 1,519 / 325 / 842 / 1,334 / 1,115 / 300 / 250; 484 | lib → bootstrap → update → marker | `marker_tests.rs` (1,661), `tauri.conf.json` (70) |
| Tokenizer | `native/fts5_cjk/fts5_cjk.c`, `README.md`, `build.sh` | 252 / 24 / 19 | README only (area 03 owns it) | — |
| E2E | `apps/desktop/e2e/` (+ `core/`, `update/`), `playwright.config.ts` | 17,246 | Titles + `core/README.md` | — |

Giant files:
- `electron/main.ts` [18,619]: everything not yet extracted. Read by function name (line numbers at
  the pinned SHA): boot-progress + first-run gate 2,447–2,740; update wait 2,810; Python/git discovery
  2,933–3,370; update strategy wiring 3,470–3,790; backend reap/stop/ownership 3,787–4,510;
  bootstrap marker + renderer index resolution 4,697–4,900; `<resolve-backend>` 5,005 and
  `ensureRuntime` 5,209 (the resolver ladder — read fully); REST helpers 5,353–5,600; preview targets
  and watchers 5,793–6,090; readiness probes 6,092–6,210; menu/devtools/zoom/context-menu/downloads/
  media permissions 6,485–6,970; OAuth sessions and embedded login 7,064–7,640; native token store
  7,635–7,700; gateway file save + ticket mint 7,752–7,950; cloud discovery 7,950–8,135; secret
  storage 8,135–8,470; remote header rules 8,466–8,570; connection config + registry IO 8,567–9,400;
  SSH flows 9,485–10,350; profile routing 10,348–10,820; registry/pool backends 10,818–11,970;
  `<start-backend>` 12,032–12,365 (read fully); common window handlers 12,365–12,545; secondary/instance/
  browser windows 12,545–12,900; pet overlay 12,902–13,150; HUD 13,151–13,670; quick entry
  13,672–13,870; `createWindow` 13,873–14,344; API proxy dispatch 15,977–16,580; translucency/keep-awake
  17,018–17,260; about/version/skew 17,524–17,620; uninstall 17,641–17,950; deep links 17,953–18,300;
  quit handling 18,329–18,619.
- `src/app/session/hooks/use-session-actions/index.ts` [3,425] and `utils.ts` [2,585]: session
  open/create/resume/branch plumbing; the part that matters is gone-session handling and owner routes.
- `src/store/session-states.ts` [3,132]: per-tile runtime bindings, rename/drop migrations, watchdog
  constants — read the migration functions and the constants block.
- `src/store/gateway.ts` [2,484]: socket pool per (connection, profile), activation leases, secondary
  socket lifetime — read the constants and the activation path.
- `src/components/pane-shell/tree/store.ts` [2,282]: pane tree model; read the node/zone shapes only.
- `src/sdk/index.ts` [2,018]: the public plugin contract; read exports only (doc covers semantics).

## 5. Minimum viable subset

v1 needs [inf]:
1. Electron shell: single-instance lock, one main window (frameless, title-bar overlay, persisted
   bounds, hidden-until-paint), sandboxed + isolated renderer, narrow bridge, window-open deny-all,
   navigation confinement, audited external opener.
2. Backend resolution ladder (rungs: dev root override, managed install, explicit command override,
   bootstrap-needed) and local gateway attach with per-dial one-use tickets and per-request HTTP grants.
3. Boot progress overlay with latching local failures, bounded remote retry, and recovery actions.
4. Transport: shared JSON-RPC-over-WebSocket client with timeouts, heartbeat, reconnect backoff,
   replay-since-cursor, server→client requests (approval, clarify, sudo, secret at minimum).
5. Chat: transcript with streaming text/reasoning/tool rows, composer with attachments and slash
   completion, stop/steer/queue, approvals card stack, model picker, session sidebar with search,
   pin, archive, new chat, gone-session draft recovery.
6. Settings: providers/keys, model, config-backed sections via the gateway schema, appearance (theme,
   light/dark), about (version, update check).
7. Onboarding: provider choice (OAuth device code, API key, custom endpoint, choose later).
8. First-run install: chooser (connect remote vs install local) + stage-protocol installer runner.
9. Packaging: unsigned dev builds on all three OSes; one signed path per OS later; update via
   electron-updater on macOS first.
10. E2E: boot, chat send/receive, transcript-once oracle, boot failure overlay.

Can wait (and why): multi-connection registry, SSH remotes and SSH-managed updates (large, many
traps); cloud discovery; native PKCE sign-in (embedded login or token suffices first); HUD, quick
entry, pet, wake word, screenshot gesture (ambient extras); bot mode and gateway groups (plugin, built
on stable profiles); plugin SDK and disk plugins (needs a stable contribution registry first);
memory graph, command center, agents view, artifacts gallery, session import; git review/worktrees and
terminal pane (valuable but separable); MSIX/Store/App Installer flows; i18n beyond English (keep the
mechanism from day one); separate native setup installer (the in-app installer covers first run);
Windows/Linux GPU/sandbox crash-loop markers (add when field data shows need, but keep the sandbox on).

## 6. Hard parts / traps

1. **R08.1 Attempt fencing on backend start.** A slow resolve or health probe can finish after the
   user switched connections, publishing a stale descriptor or clearing the new attempt's profile pin.
   Give every start an attempt token, check it after each await, and make failure cleanup conditional
   on still owning the attempt.
2. **R08.2 Failure latching rules.** Not latching a local failure loops a multi-minute install every
   time any request retries; latching a transient remote failure wedges the app until restart.
   Latch local failures, host-key changes, rejected SSH credentials, dead ssh clients and confirmed
   reauth only; mark other remote failures retryable with a bounded renderer retry (5 attempts, 2 s
   base, full jitter).
3. **R08.3 Single-instance lock before any process machinery.** A losing second instance that reaps
   "orphans" kills the running instance's backend. Gate reap/spawn/claim on holding the lock; make the
   loser inert and forward its argv.
4. **R08.4 One-use credentials.** Reusing a cached WebSocket URL with an already-spent ticket, or
   retrying HTTP with the same grant, fails intermittently. Mint per dial and per HTTP attempt; strip
   the ticket from the URL and pass it as a subprotocol; only a confirmed 401/403 means re-auth —
   timeouts and 5xx are connectivity errors.
5. **R08.5 Connection test must cover the used leg.** An HTTP-only status probe passes while the
   WebSocket/auth leg fails ("connected but nothing works"). Test HTTP and WS with the real auth.
6. **R08.6 Soft vs hard re-home.** Treating a connection switch as a reload flickers; treating a home
   change as soft strands stale rows; query invalidation alone leaves live session stores populated.
   Explicitly wipe gateway-bound stores on soft switches and assert socket/profile/connection agreement
   afterwards.
7. **R08.7 Profile-keyed storage on rename.** Any new per-profile local-storage family not added to
   both the rename migration and the drop routine yields "couldn't open this session" on every
   restore after a rename. Centralize the family list.
8. **R08.8 Guest content opening the OS browser.** A sandboxed iframe without popups and without a
   gesture can still reach the window-open handler. Deny everything there with no side effect; route
   external opens only via a trusted-click bridge on one dedicated partition, http/https only, never
   `file:`; never set `allowpopups` on the webview.
9. **R08.9 Cookie partition naming on Windows.** A persistent partition name containing a character
   Electron percent-escapes (e.g. `:`) produces a folder whose cookie store Windows cannot use; sign-in
   silently never persists. Use plain names; renaming signs users out once.
10. **R08.10 Keychain prompts.** Touching OS secret storage (even "is it available") on a broken
    macOS keychain shows a blocking dialog every launch. Make keychain encryption opt-in, write
    owner-only files by default, and attempt legacy migration exactly once (record the attempt).
11. **R08.11 Canonical bot chat identity.** Storing a session-id pointer for a bot's forever-chat
    dangled, got stolen by recency heuristics and forked chats in loops. Identify by (profile, fixed
    title) with a unique-title index; adopt-before-create; never consult recency; hide canonical chats
    from the session list.
12. **R08.12 Slash curation hiding user extensions.** An allow-list once dropped every skill and quick
    command from completion while they still ran when typed. Hide only known noisy built-ins; let
    anything unknown through both completion paths.
13. **R08.13 Update concurrency.** A reopened app mid-update, or a reconnecting renderer after the
    updater stopped the backend, spawns a backend on the runtime being replaced. Gate local starts on
    the shared update marker AND an in-process "update running" flag; never delete or rewrite another
    owner's marker; judge liveness by PID + process creation time.
14. **R08.14 New runtime under an old renderer.** Terminal updates move the source but not the
    compiled renderer; partial package replacement can mix asar and unpacked generations. Detect skew
    and missing lazy chunks and offer a rebuild/update instead of crashing on the first lazy import.
15. **R08.15 Electron zoom drift.** Chromium drops or per-URL-remembers zoom after show/restore,
    display moves, reloads and hash navigation. Re-assert persisted zoom on all of these with short
    settle checks; exclude transparent overlay windows that size themselves.
16. **R08.16 GUI PATH.** Dock/Finder/launcher starts inherit a minimal PATH, so the backend and MCP
    servers miss user-installed CLIs. Merge the login-shell PATH before resolving or spawning; never
    let a broken shell profile block boot.
17. **R08.17 Sandbox/GPU crash loops.** Some Windows/Linux hosts kill sandboxed GPU children before
    main; others crash with a stack-cookie signature. Use per-version sticky markers, add no-sandbox
    only for the sandbox signature after two consecutive aborts, disable GPU (not the sandbox) for the
    stack-cookie case, re-probe after updates, clear markers only after a window is revealed.
18. **R08.18 Installer stage host deaths.** A stage process can exit without its JSON result frame
    (abrupt host exit) and a grandchild can hold the pipes open forever. Retry only that exact shape
    (3 attempts, idempotent stages), take the last JSON line as the result, drain pipes with a 20 s
    grace, decode non-UTF-8 console output, and always download a fresh script (stale scripts drive the
    wrong tree).
19. **R08.19 Stale async results in the renderer.** Delayed history reads have removed the latest
    reply; replayed requests have been answered twice. Use generation counters, merge (not replace)
    lists, make server-request responses idempotent by id, and test both orders of "reply completes"
    vs "hydrate resolves".
20. **R08.20 Background focus theft.** Electron's default focus-on-navigation raises a blurred window
    when an SPA route changes during streaming; background events must never navigate or open panes.
    Disable focus-on-navigation for chat windows and route all raises through explicit user actions.
21. **R08.21 Deep links during boot.** Links arriving before the renderer listens are lost or
    delivered twice. Queue the latest link in main, flush exactly once on a renderer-ready signal, and
    handle the "escape a fullscreened preview" link entirely in main.
22. **R08.22 Code signing order.** Resource edits after signing, refreshing payload digests after the
    outer signature, or stamping the exe after ASAR integrity rewrite invalidates signatures. Stamp in
    after-extract, sanitize PE tables, sign nested binaries before the app, refresh digests before the
    outer signature, notarize in after-sign.
23. **R08.23 Remote execution boundary.** Sending a client file path to a remote gateway resolves to
    the wrong (or no) file and leaks paths. Ship client-only files as bytes; resolve remote artifact
    paths against the session's remote cwd and home; never fall back to a local file.
24. **R08.24 Quit semantics with attached gateways.** Warning "work will be lost" is wrong when the
    local gateway outlives the app; not warning is wrong for app-owned SSH servers. Decide per backend
    ownership.

## 7. Open questions

1. Which Electron fuses are flipped in shipped builds (RunAsNode, cookie encryption, NODE_OPTIONS,
   inspect args, embedded ASAR integrity, only-load-from-asar)? Source has no explicit fuse config.
   Needed: REA read of fuse bytes in the shipped macOS/Windows binaries (needs REA).
2. Does the shipped main renderer page carry any CSP (meta tag injected at build, or response
   headers)? No CSP found in source for the chat renderer. Needed: REA capture of the packaged
   `index.html` and session header handlers (needs REA).
3. Is the shipped release built from this SHA's main, and which variant is on the public download
   page (bundled vs bootstrap)? Needed: REA version/stamp read of the current installers (needs REA).
4. Exact payload of the install stamp / build stamp baked into the app (update mechanism, channel,
   runtime paths, feature list). Needed: REA extraction of `install-stamp.json` from a shipped bundle
   (needs REA).
5. Actual asar contents and size; whether `dist/**` is truly unpacked in shipped builds (affects the
   renderer-generation check). Needed: REA asar listing (needs REA).
6. macOS entitlements as signed (does the shipped app keep library-validation disabled and
   unsigned-executable-memory)? Needed: `codesign -d --entitlements` via REA (needs REA).
7. Does the shipped Windows MSIX declare the Copilot key provider and execution aliases as built?
   Needed: REA manifest read (needs REA).
8. Default secret-at-rest behaviour conflicts between docs (README says encrypted; source policy says
   plain + 0600 by default). Product decision for the rebuild: default to OS keychain where reliable
   (Windows/Linux with a secret service) and fall back to 0600? Needed: lead decision.
9. Which IPC handlers should validate the sender frame in the rebuild? Reference validates only a
   few. Needed: security decision (recommend: validate every handler against app origin + main frame).
10. Does the rebuild keep the separate native setup installer (Tauri) or rely only on the in-app
    installer plus bundled payloads? Needed: product decision; REA check of which installer users get.
11. Light (remote-only) variant: ship in v1 or not? Needed: product decision.
12. How many of the 257 RPC methods does the desktop actually call vs. the dashboard? Needed: a usage
    grep pass in the Read step (source), plus REA network capture of a live session (needs REA).
13. Exact layout preset definitions (which panes each preset places) and default keybind combos per
    OS. Needed: Read step over `store/layout.ts` and `lib/keybinds/actions.ts`.
14. Real update UX on each OS (progress events, relaunch timing). Needed: REA runtime capture of an
    update cycle (needs REA).

## 8. What to read later

- Reference docs: `apps/desktop/README.md`, `AGENTS.md`, `ENGINEERING.md`, `DESIGN.md`, `BUILDING.md`,
  `docs/canonical-gateway-protocol.md`, `docs/canonical-groups.md`, `src/AGENTS.md`;
  `website/docs/user-guide/desktop.md` (845), `multi-connection-desktop.md` (377), `bot-mode.md` (395),
  `guides/desktop-native-signin.md` (125), `developer-guide/desktop-plugin-sdk.md` (2,047),
  `developer-guide/macos-bundle-updates.md` (88); repo `tests/install/BUNDLED_UPDATES.md` and
  `docs/shared-bundle-builds.md`.
- Reference tests that list the edge cases: `e2e/core/README.md` and the core specs (oracle
  invariants); `electron/connection-registry.test.ts`, `connection-config.test.ts`,
  `profile-session-routing.test.ts`, `ssh-connection.test.ts`, `remote-lifecycle.test.ts`,
  `hardening.test.ts`, `window-open-policy.test.ts`, `oauth-partition.test.ts`,
  `windows-sandbox-fallback.test.ts`, `update-marker*.test.ts`, `updater/*.test.ts`;
  `src/lib/desktop-slash-commands.test.ts`; bot-mode canonical-chat tests in `src/plugins/<bots-plugin>/`;
  `apps/shared/src/json-rpc-gateway-replay.test.ts`; installer `marker_tests.rs`.
- REA Evidence (outside the repo): none captured (REA not connected). Planned bundle
  `~/rea-lab/evidence/08-desktop/` for fuses, CSP, asar listing, entitlements, MSIX manifest, stamp.
- Brand assets (trademarks — list only, never reuse; names in §2): app icons (`assets/icon*.{png,ico,icns,icon}`,
  `icon-dark*`, `icon-mac.png`), 62 MSIX tile PNGs under `assets/appx/`, DMG volume icon and DMG
  background under `packaging/`, mascot/brand PNGs and sprite frames under `public/` (8 frame files
  plus a sprite sheet), `public/apple-touch-icon.png`, `public/ds-assets/filler-bg0.jpg`, tier feature
  art `src/assets/tiers/*.webp` (4), PR screenshots `pr-assets/*.png` (2), installer icons
  `apps/bootstrap-installer/src-tauri/icons/*` and `public/` mark, BrandMark component artwork. The
  bundled JetBrains Mono fonts (`src/fonts/*.woff2`, OFL) are third-party, reusable under their licence.
- Related specs: `01`–`04` (agent core, tools, sessions DB incl. the CJK tokenizer, providers), `06`
  (gateway server and RPC contract — the server side of §3.9), `07` (CLI incl. the desktop/update/
  uninstall subcommands and install scripts the installer drives), `09` (web UI sharing the client
  package).
