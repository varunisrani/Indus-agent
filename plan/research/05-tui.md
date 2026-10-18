# 05 — Terminal UI: behavioural specification (clean-room)

Scope: the full-screen terminal UI (renderer process), the bridge/backend it talks to (JSON-RPC methods, server-to-client requests, events), the CLI launch path that starts it, the screen layout, the multiline composer, slash-command routing and autocomplete, input history, streaming and tool-output rendering, interrupt/steer/redirect, approval and other blocking prompts, model switching, the session switcher, keybindings, theming, resize and terminal quirks, reconnect/recovery.
Out of scope: the agent loop and turn engine (`01-agent-loop.md`), tool implementations and approval policy (`02-tools.md`), classic prompt_toolkit CLI and the command registry's semantics (`04-cli.md`), the desktop app (`08-desktop.md`). The canonical shared gateway daemon that the current TUI attaches to is described here only at its wire boundary.

Reference: MIT-licensed reference app @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056` (pinned checkout, read-only). Shipped build checked: none (REA not connected; source reading only).

Sources studied: `ui-tui/README.md`, `ui-tui/CANONICAL_GATEWAY.md`, `tui_gateway/AGENTS.md`, `website/docs/user-guide/tui.md`, `website/docs/user-guide/features/skins.md` (skimmed), the TypeScript renderer under `ui-tui/src/`, the forked Ink package `<ink-fork>/` (structure only), the shared JSON-RPC channel `apps/shared/src/json-rpc-channel.ts`, the Python backend `tui_gateway/` (entry, server, transport, server_requests, event_replay, agent_callbacks, tool_progress, methods_session_interrupt, methods_complete, methods_tools shell.exec, contracts/*), the launcher `<cli-pkg>/main_tui_launch.py`, the bootstrap helper `ui-tui/scripts/gateway_bootstrap.py`, and the command registry `<cli-pkg>/commands.py` (names only). Test file names in `ui-tui/src/__tests__/` (192 files) and `tests/tui_gateway/` (325 entries) were listed, not read.

Conventions:
- "Renderer" = the Node/TypeScript full-screen process the user sees. "Backend" = the Python process that owns sessions, the agent, tools and slash-command logic. "Gateway" = whichever backend endpoint the renderer is connected to (child process or daemon).
- "Session" = one live conversation runtime (runtime id `sid`) that maps to a stored conversation (stored id). They can differ on the legacy backend and coincide on the canonical daemon.
- "Turn" = one user prompt and everything the agent does until it settles.
- "Server request" = a JSON-RPC request sent from backend to renderer (the backend is asking the user something) and answered by a response frame with the same id.
- "Canonical" = the newer shared-daemon wire (generation-bound, ticket-authenticated WebSocket). "Legacy" = the older per-renderer backend wire (stdio child or plain WebSocket).
- Rebuild names: CLI `indus`, env prefix `INDUS_`, home dir `~/.indus`. Reference names live only in section 2.
- "MUST" marks an invariant the reference defends with code or a test that names a past bug.

Runtime decision recorded: the reference renderer is TypeScript on Node (React 19 + a heavily forked Ink, nanostores for state, vitest for tests); the backend is Python. The rebuild keeps this split: TUI in TypeScript/Node with React + Ink (upstream Ink as a third-party dependency, not a port of the fork), backend in Python. [obs] package.json; [inf] recommendation.

---

## 1. Overview

The TUI is a second interactive front end on the same Python core as the classic CLI. TypeScript owns only the screen: layout, editing, keys, rendering, overlays. Python owns sessions, tools, model calls, persistence and nearly all command logic. The renderer never contains agent behaviour. [obs] README, backend AGENTS.md

Sizes: `ui-tui/` totals 116,369 lines of TS/TSX/JS (about 53,800 app source, 31,658 forked renderer, 29,004 tests). `tui_gateway/` is 45,816 lines of Python across about 110 modules, much of it serving the desktop app and web dashboard too (the backend has three consumers). [obs] wc -l

Three generations of transport exist in the pinned code, and the docs describe an older one than the code runs:

```
 indus --tui (Python launcher)
   └─ node dist/entry.js  (renderer, TTY required)
        │
        ├─(A) legacy stdio  ── python -m <backend>.entry ── agent + tools + sessions
        │      NDJSON JSON-RPC on stdin/stdout, stderr to a log ring
        │      (backend still implements it; current renderer no longer spawns it)
        │
        ├─(B) explicit attach: INDUS_TUI_GATEWAY_URL ── WebSocket ── backend inside the web dashboard
        │      (dashboard "Chat" tab embeds the TUI over a PTY and injects this URL)
        │
        └─(C) default today: short Python bootstrap child → ensure/discover the profile's
               shared gateway daemon → single-use ticket → authenticated WebSocket,
               "canonical" wire (runtime.describe, generation-bound controls, durable FIFO)
```

[obs] `gatewayClient.ts` `start()` chooses (B) when the attach URL env is set, else (C); there is no stdio spawn path left in the renderer. `tui_gateway/entry.py` still runs the stdio read loop. User docs still say the TUI spawns its own in-process gateway. [inf] (A) survives for other embedders and tests.

What the user sees: an alternate-screen app with a scrollable transcript (banner/session panel at top, then user and assistant messages, tool rows, reasoning), a live streaming assistant row, a live-work dock (subagents, background processes, goal/loop bars), a status rule, the multiline composer with a prompt glyph, and a floating completion list. Modal cards (approval, clarify, sudo/secret, confirm, model picker, session switcher, pager, agents monitor) replace or overlay parts of this. [obs] `appLayout.tsx`

What the system guarantees:
- Every user-visible question from the agent is a server request with a deadline; withdrawal is announced with one cancel event; reconnecting clients get still-open requests re-delivered. [obs] `server_requests.py`
- Events emitted before the React tree mounts are buffered and replayed in order (up to 2,000), as are server requests. [obs] `gatewayClient.ts`
- Terminal modes (mouse, focus, bracketed paste, alt screen, kitty keyboard) are reset on start and on every exit path. [obs] `entry.tsx`, `terminalModes.ts`
- A dropped connection never resubmits a prompt automatically; it resumes the same stored session. [obs] `useMainApp.ts`, docs

## 2. Reference names (rename in rebuild)

| Kind | Names (reference only; they become banned identifiers) |
|---|---|
| Binary / CLI and flags | `hermes`, `hermes --tui`, `--tui-native`/`--native`, `--cli`, `--dev`, `hermes setup`, `hermes update`, `hermes doctor`, `hermes pm repair`, `hermes gateway stop`; package names `hermes-tui`, `@hermes/ink`, `@hermes/shared`; process tag `hermes-tui` |
| Env vars | `HERMES_TUI`, `HERMES_TUI_GATEWAY_URL`, `HERMES_TUI_SIDECAR_URL`, `HERMES_TUI_STARTUP_TIMEOUT_MS`, `HERMES_TUI_RPC_TIMEOUT_MS`, `HERMES_TUI_RESUME`, `HERMES_TUI_QUERY`, `HERMES_TUI_IMAGE`, `HERMES_TUI_CWD`, `HERMES_CWD`, `HERMES_TUI_PROVIDER`, `HERMES_MODEL`, `HERMES_INFERENCE_MODEL`, `HERMES_INFERENCE_PROVIDER`, `HERMES_TUI_TOOLSETS`, `HERMES_TUI_SKILLS`, `HERMES_TUI_CLIENT_ENV`, `HERMES_TUI_MAX_TURNS`, `HERMES_TUI_TOOL_PROGRESS`, `HERMES_TUI_CHECKPOINTS`, `HERMES_TUI_PASS_SESSION_ID`, `HERMES_ACCEPT_HOOKS`, `HERMES_YOLO_MODE`, `HERMES_IGNORE_RULES`, `HERMES_SAFE_MODE`, `HERMES_IGNORE_USER_CONFIG`, `HERMES_TUI_NATIVE`, `HERMES_TUI_INLINE`, `HERMES_TUI_TERMUX_MODE`, `HERMES_TUI_TERMUX_FAST_ECHO`, `HERMES_TUI_DASHBOARD`, `HERMES_TUI_MOUSE_TRACKING`, `HERMES_TUI_DISABLE_MOUSE`, `HERMES_TUI_SCROLL_SPEED`, `HERMES_TUI_NO_CONFIRM`, `HERMES_TUI_FPS`, `HERMES_TUI_THEME`, `HERMES_TUI_LIGHT`, `HERMES_TUI_BACKGROUND`, `HERMES_TUI_TRUECOLOR`, `HERMES_TUI_FORCE_OSC52`, `HERMES_TUI_ACTIVE_SESSION_FILE`, `HERMES_TUI_DIR`, `HERMES_TUI_FORCE_BUILD`, `HERMES_TUI_RPC_POOL_WORKERS`, `HERMES_TUI_SLASH_TIMEOUT_S`, `HERMES_SLASH_WATCHDOG_POLL_S`, `HERMES_SLASH_WATCHDOG_GRACE_S`, `HERMES_PYTHON`, `HERMES_PYTHON_SRC_ROOT`, `HERMES_NODE`, `HERMES_HOME`, `HERMES_QUIET`, `HERMES_VOICE`, `HERMES_BIN`, `HERMES_HEAPDUMP_ON_START`, `HERMES_AUTO_HEAPDUMP`, `HERMES_AUTO_HEAPDUMP_COOLDOWN_MS`, `HERMES_HEAPDUMP_DIR`, `HERMES_HEAPDUMP_MAX_BYTES`, `HERMES_DEV_PERF`, `HERMES_DEV_PERF_LOG`, `HERMES_DEV_PERF_MS`, `HERMES_DEV_CREDITS`, `HERMES_TEST_ISOLATION`, `HERMES_UI_SESSION_ID`, `HERMES_KANBAN_BOARD`. Rebuild: replace the `HERMES_` prefix with `INDUS_` (e.g. `INDUS_TUI_GATEWAY_URL`). |
| Files and folders | `~/.hermes/` (home), `~/.hermes/.hermes_history` (input history), `~/.hermes/config.yaml`, `~/.hermes/.env`, `~/.hermes/state.db`, `~/.hermes/pastes/`, `~/.hermes/skins/`, `~/.hermes/sessions/saved`, `$HERMES_HOME/tui-widgets`, temp names `hermes-tui-active-session-*.json`, `hermes-edit-*`, `gateway_state.json`. Path aliases used in section 4: `<cli-pkg>` = `hermes_cli/`; `<ink-fork>` = `ui-tui/packages/hermes-ink/`; launcher bundle dir `hermes_cli/tui_dist/`. Rebuild: `~/.indus/`, history file e.g. `~/.indus/input_history`. |
| HTTP routes / URL schemes | dashboard WebSocket `/api/ws` (attach target for mode B); bootstrap control verb `session-ticket` with purpose `interactive`; WebSocket subprotocol carrying the ticket (exact name not read) |
| RPC methods / events / commands | Wire names are generic dotted words and carry no brand (e.g. `prompt.submit`, `message.delta`, `session.resume`, `gateway.ready`); the rebuild may keep or rename them (lead decision). Brand-bearing items: native transport name `hermes-gateway-v1`; error copy naming the desktop app; `/billing`/`/credits`/`/topup` flows tied to the vendor's hosted credits ("Nous" credits, `is_nous` flag, `diagnostics.share_nous`); wake phrase "Hey Hermes"; default skin/theme named "Nous"; branding defaults (agent name, the `☤` icon, goodbye text). Slash-command names themselves are generic. |
| Settings and storage keys | `display.interface`, `display.tui_native`, `display.tui_auto_resume_recent`, `display.busy_input_mode`, `display.mouse_tracking`/`display.tui_mouse`, `display.details_mode`, `display.sections.*`, `display.status_bar`/`display.tui_statusbar`, `display.status_bar.fields`, `display.tui_status_indicator`, `display.tui_compact`, `display.tui_theme`, `display.skin`, `display.personality`, `display.show_reasoning`, `display.streaming`, `display.timestamps`, `display.inline_diffs`, `display.bell_on_complete`, `display.bell_on_prompt`, `display.battery`, `display.focus_view`, `display.language`, `display.interim_assistant_messages`, `display.final_response_markdown`, `paste_collapse_threshold`, `paste_collapse_char_threshold`; boot-theme cache keys `hermes-boot-*` (desktop) |
| Other (scopes, error tags, IPC channels) | Error code `4404` "not shown" naming the desktop window; `srq-` server-request id prefix; `sa:` synthetic subagent id prefix; crash log name; source tag `tui` on session.create; shared-metrics names (`shared_metrics.*`) |

Third-party protocol names used by this area (keep exact): JSON-RPC 2.0; WebSocket (RFC 6455) with subprotocols; NDJSON framing; ANSI/ECMA-48 SGR; DEC private modes 1000, 1002, 1003, 1004, 1005, 1006, 1015, 1016, 1049, 2004, 2026 (synchronized output), 2029, 25, 9; DECSTBM scroll regions; OSC 8 (hyperlinks), OSC 10/11 (fg/bg colour query), OSC 52 (clipboard); kitty keyboard protocol (CSI >1u / CSI <u); xterm modifyOtherKeys (CSI >4;2m); bracketed paste (CSI 200~/201~); SGR mouse encoding; COLORFGBG; TERM_PROGRAM; Yoga flexbox layout; WCAG contrast ratio; Pydantic (contract models); OpenRPC (generated catalogue).

---

## 3. Behaviour

### 3.1 Launch path (CLI side, only to locate it)

- **Trigger / inputs:** `indus --tui` (also `--tui-native`/`--native`, `--dev`), or env `INDUS_TUI=1`, or config `display.interface: tui`. `--cli` forces classic. [obs] `<cli-pkg>/main_tui_launch.py::_resolve_use_tui`
- **Decision order:** `--cli` → classic; `--tui`/`--native` → TUI; stdin and stdout not both TTYs → classic (MUST: a piped one-shot run must never be hijacked by an ambient preference, because the renderer exits 0 without a TTY and a scripted worker would die); `INDUS_TUI=1` → TUI; config `display.interface` equals `tui` → TUI; else classic. [obs]
- **Flow:**
  1. Build the child environment from the full current environment (secrets kept; the renderer and agent need provider credentials). [obs]
  2. Pin the working directory to where the command was invoked (an inherited value from an outer TUI must not win); `--worktree` creates a git worktree and uses it instead. When the terminal backend is local, the same directory becomes the session cwd sent on session create. [obs]
  3. Create a temp "active session" file; the renderer writes the current stored session id into it so the launcher can print an exit summary (resume hint) after the child exits with 0 or 130. [obs]
  4. Native mode comes from the flag or `display.tui_native`. [obs]
  5. Export launch options as env for the renderer: model, provider, toolsets, skills, one-shot query, startup image, checkpoints, pass-session-id, max turns, tool-progress mode (`verbose` for `-v`, `off` for `-Q`), accept-hooks, resume id, plus an allowlisted copy of the user's shell environment serialized as JSON ("client env"). A stale exported resume id is cleared first; only the id resolved for this invocation is forwarded. [obs]
  6. Ensure Node heap: append `--max-old-space-size=<N>` to NODE_OPTIONS unless the user set one; default 8,192 MB, lowered below a detected cgroup memory limit. `--expose-gc` is passed as a direct node flag (Node refuses it in NODE_OPTIONS). [obs]
  7. Resolve the renderer: (a) explicit prebuilt dir env with `dist/entry.js`; (b) a bundle shipped inside the Python package; (c) the source checkout's `ui-tui/dist/entry.js`, rebuilt with esbuild when sources are newer (or a force flag); `--dev` runs TS sources through tsx and is refused together with a prebuilt dir. A missing `ui-tui/` checkout is self-healed with `git restore` before failing with recovery steps. Missing Node prints an install hint and suggests `--cli`. [obs]
  8. Run the child synchronously; Ctrl+C maps to 130. [obs]
  9. After exit: print the exit summary on 0/130, clean up the temp file, apply worktree keep/discard choice. Exit code 42 means "user asked for update": relaunch as the update subcommand without TUI flags. [obs]
- **Renderer entry:** exits immediately (code 0, short message) when stdin is not a TTY; resets terminal modes; clears the screen (except Termux/native modes, which print a newline to keep scrollback); starts the gateway client; installs graceful-exit handlers; starts a memory monitor; then dynamically imports the renderer and React tree. [obs] `entry.tsx`
- **Requirements:** Node ≥ 20 per docs. [obs docs]

### 3.2 Process model and transports

**Mode A — legacy stdio child (backend side still present).** [obs] `tui_gateway/entry.py`
- Backend reads one JSON object per line from stdin; blank lines ignored; a line that fails to parse gets a JSON-RPC parse-error response (`-32700`, id null) and the loop continues.
- Each request goes to a dispatcher; slow handlers run on a thread pool (default 8 workers, minimum 2, env-tunable). An exception in an inline handler is logged to a crash log and answered `-32000` "handler error" rather than killing the read loop.
- Before serving: it claims ownership of the profile home (refuses to start when a running gateway owns it), marks the RPC stdin close-on-exec (POSIX) so subprocesses cannot steal request bytes, installs an optional "sidecar" event mirror, starts a backend heartbeat and an orphan sweep, starts MCP discovery in the background (a dead MCP server must not freeze startup), writes the first frame `gateway.ready { skin, change_events: true, replay_epoch }`, starts a skin file watcher, and pre-warms the model-picker cache.
- Spurious stdin EOF (a child flipped O_NONBLOCK on the shared descriptor) is detected and retried; genuine EOF ends the loop.
- stdout carries only protocol frames; stderr is free text. On the renderer side (historical README): malformed stdout lines become a synthetic `gateway.protocol_error` event and stderr lines become `gateway.stderr`; neither ever writes raw bytes to the terminal. [obs README, still handled in renderer]

**Mode B — explicit attach URL.** [obs] `gatewayClient.ts`
- When `INDUS_TUI_GATEWAY_URL` is set the renderer opens a WebSocket to it. If that endpoint's `gateway.ready` says it is bound to a session authority, the renderer switches to the canonical wire (mode C semantics) while keeping the listener's skin. An explicit remote URL never falls back to a local daemon.
- An optional second "sidecar" URL receives a mirror of every event frame (used by the dashboard sidebar); failures there are logged only.
- Connection URLs are redacted (query string and user-info replaced) before any log line or timeout payload.

**Mode C — local shared daemon (default).** [obs] `localGatewayBootstrap.ts`, `scripts/gateway_bootstrap.py`, `CANONICAL_GATEWAY.md`
1. The renderer runs a short Python helper (interpreter from `INDUS_PYTHON`, else `python3`/`python` on PATH only for dev runs; it never scans virtualenvs) with a 40 s exec timeout and 1 MiB output cap.
2. First launch passes "start": ensure the profile's gateway daemon is running (30 s timeout, idle-exit enabled). Later reconnects pass nothing (discover only, 5 s) or "recover" (re-ensure) when a bounded budget allows: at most 3 re-ensures per 60 s sliding window. A deliberately stopped daemon is never resurrected; only a crashed one whose retained state still claims liveness is.
3. The helper clears launch-option env vars before talking to the daemon (they belong to the session, not the daemon).
4. It asks the daemon's private control socket for a single-use ticket bound to profile id + instance id, validates the reply ids, and prints `{ url, protocols, profile_id, instance_id }` as JSON. The ticket travels in a WebSocket subprotocol, never in a URL or log.
5. A helper failure whose message starts with a "gateway unavailable" verdict is terminal (no retry; shows a repair hint). Other failures go to the reconnect backoff.
6. On socket open the renderer calls `runtime.describe` to learn the session-create contract and skin, then publishes a synthetic `gateway.ready`. A failed describe is treated as a transport failure (socket closed, backoff). Canonical readiness is published exactly once.
7. Closing the TUI only detaches; the daemon and any running turn keep going.

**Shared channel behaviour (all WebSocket modes).** [obs] `apps/shared/src/json-rpc-channel.ts`, `gatewayClient.ts`
- Request ids are client-minted integers; server-request ids are strings `srq-<12 hex>` so they never collide. [obs]
- Default request timeout 120,000 ms (renderer env override, floor 30,000 ms). [obs]
- Heartbeat: when `gateway.ready` advertises `heartbeat`, a `gateway.ping` every 15,000 ms; any inbound frame counts as liveness; no inbound frame for 45,000 ms forces the socket closed so the normal reconnect runs; at most 8 outstanding pings. MUST count any inbound frame, not only pongs (a mid-turn socket streaming every second was killed otherwise, splitting sessions). [obs]
- Reconnect: exponential backoff from 1,000 ms capped at 30,000 ms, no jitter; each scheduled attempt publishes `gateway.reconnecting { attempt, delay_ms }`; reaching ready resets the counter. [obs]
- Startup timeout: if `gateway.ready` does not arrive within 15,000 ms (env override, floor 5,000 ms) publish `gateway.start_timeout { python, cwd, stderr_tail }` with the last 20 log lines. [obs]
- Log ring: last 200 lines, each truncated to 4,096 bytes with a marker; `/logs` shows it. Event buffer before mount: 2,000 events in a circular buffer. [obs]
- Replacing a transport rejects every in-flight request of the old one (otherwise callers hang forever) and invalidates any pending deferred drain. The "subscribed" flag is NOT reset on reconnect (resetting it stranded every post-reconnect event in the buffer). [obs]
- Socket close events are identity-gated: a close from a discarded socket is logged and ignored. [obs]
- Unanswerable server requests (desktop-only bridges) get an immediate `-32601` reply, letting the agent error out quickly rather than sit until its deadline expires. A crashing request handler is answered `-32603`. [obs]

**Data shapes (conceptual):**
- `Frame = Request { jsonrpc, id: int|string, method, params } | Response { jsonrpc, id, result? , error?: { code, message, data? } } | Event { jsonrpc, method: "event", params: EventEnvelope }`
- `EventEnvelope { type, session_id?, payload?, seq?: int, replay_epoch?: string, authority_epoch?, execution_generation? }`
- `Grant { url, protocols: string[], profile_id, instance_id }`
- `CreationContract { sources: string[], parameters: string[] }` (from runtime.describe)

### 3.3 Wire contract discipline

- Every backend method, server request and event is declared once as a typed model in Python; the dispatcher refuses an undeclared method at import, rejects unknown parameter keys (code 4000 with the key path), and when a result or emitted payload fails validation against its model it raises under test isolation but merely logs in production. TypeScript types and an OpenRPC document are generated from these models and a test fails when the generated files are stale. [obs] backend AGENTS.md, `contracts/registry.py`
- Declared counts at the pinned commit: 245 methods, 16 server requests, 73 events, plus 26 "canonical" methods for the shared-daemon transport. [obs] grep of `contracts/*.py`
- Additional renderer-local synthetic events exist (`gateway.stderr`, `gateway.protocol_error`, `gateway.start_timeout`, `gateway.reconnecting`, `subagent.*`, `session.replay_gap`, `session.updated`, `approval.request`/`clarify.request` on canonical, `approval.settled`/`clarify.settled`). [obs] event handler switch
- Error codes are per-method and overloaded: the same number means different things in different handlers (for example 4002 is both "text is required" and "unknown provider"). Generic ones: `-32700` parse, `-32600` invalid request, `-32601` unknown method, `-32602` invalid params, `-32603` client handler crash, `-32000` handler error, `4000` unknown param key, `4001` invalid session/transport, `4404` request not shown in any window. [obs]
- Rebuild guidance: keep a single declared contract with generated TS types; give error codes stable meanings. [inf]

### 3.4 RPC surface the TUI uses (grouped)

Only the methods the terminal renderer calls are listed; the remaining ~180 serve desktop/dashboard features (projects, groups, bot relay, pets, vault, connectors, display leases, free tier, onboarding, metrics). [obs] grep of renderer `request(`/`rpc(` calls; [inf] grouping.

- Lifecycle/connection: `runtime.describe` (canonical), `client.capabilities { server_requests: true }` (sent once per connection), `gateway.ping`, `setup.status`, `setup.runtime_check`.
- Sessions: `session.create`, `session.resume`, `session.activate`, `session.detach` (canonical), `session.close`, `session.list`, `session.active_list`, `session.most_recent`, `session.delete` (legacy) / `session.mutate { operation }` (canonical: model, branch, compress, delete; guarded by revision/generation preconditions plus a stable request id), `session.title`, `session.history`, `session.events.since`, `session.usage`, `session.status`, `session.undo`, `session.save`, `session.branch`, `session.compress`, `session.control`/`session.control.read` (goal/loop/heartbeat), `terminal.resize { cols }` (legacy only).
- Turns: `prompt.submit`, `prompt.background` (`/bg`), `prompt.btw` (`/btw`), `prompt.cancel` / `prompt.resolve_unknown` / `prompt.receipt` (canonical queue rows), `session.interrupt`, `session.steer`, `session.redirect`.
- Prompts: answers to server requests travel as response frames; plus `clarify.lock` (batch clarify), `clarify.respond`/`approval.respond` (canonical shared controls).
- Commands: `slash.exec`, `command.dispatch`, `commands.catalog`, `complete.slash`, `complete.path`, `command.observe` (canonical bookkeeping), `shell.exec`, `cli.exec`.
- Composer helpers: `paste.collapse`, `image.attach`, `image.attach_bytes`, `image.detach`, `input.detect_drop`, `clipboard.paste`, `file.attach`, `pdf.attach`.
- Config/model: `config.get { key: full|mtime|… }`, `config.set { key, value, session_id }` (keys used: `model`, `verbose`, `yolo`, `reasoning`, `fast`, `busy`, `skin`, `personality`, `indicator`, `details`, `statusbar`, `mouse`…), `config.show`, `model.options`, `model.save_key`, `model.disconnect`, `reload.mcp`, `reload.env`, `skills.reload`, `skills.manage`, `tools.list`/`tools.show`/`tools.configure`, `toolsets.list`, `plugins.list`/`plugins.manage`, `rollback.list`/`rollback.diff`/`rollback.restore`, `browser.manage`.
- Agents/processes: `subagent.list`, `subagent.tail`, `subagent.steer`, `subagent.interrupt`, `delegation.status`, `delegation.pause`, `spawn_tree.*`, `process.list`/`process.stop`/`process.kill`, `agents.list`.
- Voice/wake (optional feature): `voice.toggle`, `voice.record`, `voice.tts`, `wake.start`/`wake.resume`/`wake.*`.
- Billing (vendor-specific, out of v1): `billing.*`, `subscription.*`, `usage.bars`.

### 3.5 Events the renderer consumes

Handling is a single switch after two filters: (1) events for a session other than the focused one are dropped; (2) on canonical gateways lifecycle events (`session.info`, `message.start`, `message.complete`, stamped `error`) carrying an older execution generation or a different epoch are dropped ("fence"). [obs] `createGatewayEventHandler.ts`, `gatewayCanonicalEvents.ts`

| Event | Payload (conceptual) | Renderer behaviour |
|---|---|---|
| `gateway.ready` | `{ skin, change_events, replay_epoch, heartbeat?, session_authority? }` | Apply skin; start config fetch; arm wake (if not user-disabled); load slash catalog if no session; then recovery-resume, startup resume, auto-resume-recent, or new session (3.6). |
| `skin.changed` | `Skin` | Repaint live. |
| `session.info` | `SessionLiveInfo` | Update banner/session panel, model, cwd/branch, tools/skills/MCP lists, usage, title, running flag, pending queue rows. |
| `session.usage` | usage tick | Merge into status bar usage (stable merge). |
| `session.title` | title | Status-bar badge. |
| `session.control.update` | goal/loop/heartbeat snapshot | Goal bar / loop bar. |
| `session.replay_gap` | — | The daemon dropped this subscription (fan-out overflow) though the socket is healthy: re-attach through resume. |
| `message.start` | — | New turn: busy=true, clear activity/segments/tools/subagents/trail. |
| `message.delta` | `{ text|null, rendered? }` | Append raw text to the stream buffer; null text = segment boundary. Ignored after a local interrupt. |
| `message.interim` | `{ text, already_streamed }` | Seal the buffer as its own assistant segment (commentary beside tool calls). |
| `message.complete` | `{ text, usage?, status: complete|error|interrupted, reasoning?, warning?, billing?, error?, error_surface?, partial?, … }` | Finalize (3.13). |
| `thinking.delta` / `reasoning.delta` / `reasoning.available` | `{ text, verbose? }` | Reasoning panel/status; only while busy for thinking. |
| `status.update` | `{ kind, text }` | Status slot; kinds `goal`, `compressing`/`compacting`/`compacted`, `error`, `warn`, `approval`, `status`, `lifecycle`; non-plain kinds also go to the activity lane; restore status after 4,000 ms (goal: 6,000 ms). |
| `notification.show` / `.clear` | `{ id, key, kind: ttl|sticky, level, text, ttl_ms?, detail? }` | Status notice; held while busy, shown at turn end; TTL starts when visible; clear only matches by key. |
| `tool.generating` | `{ name }` | Trail line "drafting <name>…". |
| `tool.start` | `{ tool_id, name, context?, args?, args_text?, labels? }` | Active tool row. |
| `tool.complete` | `{ tool_id, name, duration_s?, result?, summary?, result_text?, inline_diff?, todos?, labels? }` | Close the row; diff segment; todo panel. |
| `todo.updated` | full todo snapshot | Todo panel. |
| `request.cancel` | `{ id, method, reason }` | Close whichever card has that request id; for sudo/secret/vault timeouts print a note on how to retry. |
| `approval.request` / `clarify.request` (canonical) | `{ prompt_id, execution_generation, … }` | Open the shared-control card. |
| `approval.settled` / `clarify.settled` | `{ prompt_id }` | Close the matching card (another viewer answered). |
| `background.complete` | `{ task_id, text }` | System line tagged with the task id. |
| `btw.complete` | `{ question?, text }` | System line. |
| `review.summary` | `{ text }` | Persistent system line (background self-review saved memory/skills). |
| `subagent.spawn_requested`/`start`/`thinking`/`tool`/`progress`/`complete` | subagent progress | Upsert into the subagent tree (3.15). |
| `moa.*` | mixture-of-agents phases | One replace-in-place activity line. |
| `browser.progress` | `{ message }` | System line. |
| `connection.request`/`.update` | connector op card | Desktop-oriented card; TUI renders a minimal one. |
| `voice.*`, `wake.detected` | voice | Optional. |
| `reaction` | `{ kind }` | Flash a heart/pet animation. |
| `error` | `{ message }` | Error line/panel; a "no provider configured" message triggers a setup hint. |
| `gateway.stderr` (synthetic) | `{ line }` | Only failure-looking lines become one deduplicated activity warning. |
| `gateway.protocol_error` (synthetic) | `{ preview }` | Status "protocol warning" for 4,000 ms; first time an activity note; preview clipped to 120 chars. |
| `gateway.start_timeout` / `gateway.reconnecting` (synthetic) | see 3.2 | Status and diagnostic. |

### 3.6 Startup and session bootstrap

- **Flow on `gateway.ready`:** [obs] `handleReady`
  1. If a recovery target (stored id) is pending from a crash/disconnect: resume it, status "recovering session"; keep the target until resume succeeds even across a second disconnect. Never resend the in-flight prompt.
  2. Else if a startup resume id was passed: status "resuming…", resume it, then schedule the startup prompt.
  3. Else read full config; if `display.tui_auto_resume_recent` is true, call `session.most_recent` and resume it; otherwise (or on any error) status "forging session…" and create a new session. Default is off.
  4. "Startup prompt" = the one-shot query and/or startup image from the launcher; sent literally (no slash routing, no shell, no interpolation) once the session is ready.
- **Session create (canonical):** strip `cols`; set `source: tui`; add a fresh `request_id`; refuse locally when the daemon's contract does not list `tui` as a source or lacks any requested parameter (visible "policy compatibility" error, never silent CLI-policy fallback). Launch options from env become creation params: model, provider, cwd, toolsets, skills (deduped, comma/newline split), client env (only if the contract lists it), max turns (integer when numeric, else the raw word such as "unlimited"), and boolean launch flags (yolo, ignore rules, safe mode, ignore user config, checkpoints, pass session id, accept hooks; truthy = 1/true/yes/on). After create, a tool-progress mode from launch is applied with `config.set key=verbose`; an invalid mode refuses the launch. [obs] `canonicalGateway.ts`
- **Idempotent create:** if a create fails after it may have committed (lost ACK) the next create with identical options reuses the same request id so the daemon returns the same session instead of a duplicate. [obs] `createCanonical`
- **Resume/activate translation (canonical):** both become `session.resume`; a rendering resume asks for a display projection; metadata-only activation omits messages. Result rows with stored multipart `content` are flattened to text (text parts joined; image parts as `[image]`). Raw rows are kept separately for rewind/undo targeting. [obs]
- **Attach hold:** while a resume/activate is in flight, live frames for that session are held; after the snapshot is applied only frames with the same replay epoch and a sequence above the snapshot's last sequence are replayed; on failure all held frames are delivered. [obs] `holdAttach`/`settleAttach`
- **Legacy session create params:** `cols`, `source`, `cwd`, `cwd_explicit`, seed `messages`, `parent_session_id`, `title`, `model`, `provider`, `reasoning_effort`, `fast`, `service_tier`, `close_on_disconnect`, `hidden`, `idempotency_key`; result `{ session_id, stored_session_id, message_count, messages, info }`. Resume params add `lazy`, `defer_history`, `omit_messages`, `eager_build`, `inline_images`. A session row appears in storage only on the first prompt unless seeded. [obs] `contracts/sessions.py`
- **Session info shape:** `SessionLiveInfo { model, provider, reasoning_effort, reasoning_effort_wire, service_tier, fast, yolo, approval_mode, tools: map<toolset, names[]>, skills: map<category, names[]>, cwd, branch?, project?, terminal_backend, personality, running, turn_started_at?, title, stored_session_id, version, release_date, update_behind?, update_command, usage?, profile_name?, mcp_servers[], system_prompt?, credential_warning?, lazy? }` (+ canonical `execution_epoch`, `execution_generation`, `pending_submissions`). [obs]
- **Status labels seen during boot:** "summoning" (initial), "forging session…", "resuming…", "starting agent…" (session live, tools/skills still loading; input accepted and queued), "ready". [obs docs + uiStore]
- **Width:** on legacy gateways the renderer sends `terminal.resize { cols }` when the session or width changes; canonical sessions have no server-side width. [obs]
- **Active session file:** each session change writes the stored id to the launcher's temp file. [obs]

### 3.7 Screen layout

- **Shell:** default full-screen alternate screen with mouse tracking. "Inline" mode (no alt screen; host scrollback keeps scrolled-off rows) is on for native mode and Termux, or via env toggle. Native mode also disables mouse tracking and clears its last frame on exit. [obs] `appLayout.tsx`, `config/env.ts`
- **Regions, top to bottom** (normal view): [obs]
  1. Transcript pane (flex-grow), optionally flanked by thin left/right "ambient rails". Inside: virtualized history with top/bottom spacers; first row is the banner (logo/hero art from the skin) and the session panel; then message rows; then the live streaming assistant row. A scrollbar column (not selectable) and a sticky-prompt tracker sit beside it.
  2. Prompt zone: the active blocking card (approval, clarify, sudo, secret, vault cards, confirm).
  3. Composer pane: queued-messages preview, goal bar, loop bar, live-agents dock, status rule, ambient dock, then the input line(s) with a prompt glyph and the text editor; when the composer is non-empty in native mode the dock/status move below it.
  4. Optional FPS overlay; a pet sprite pane (cosmetic).
  5. A slot for an active "widget app" (SDK-registered mini apps).
- **Alternate full-pane views:** the agents monitor (`/agents`, Ctrl+T) and the "journey" view replace the transcript and hide the composer. [obs]
- **Floating completion list:** at most 16 rows, window centred on the selection. [obs] `appOverlays.tsx`
- **Status rule segments and width breakpoints** (columns): compact context below 72; context bar from 72; duration from 76; compression count from 80; voice from 84; background task count from 88; subagent count from 92; cache-hit from 96; latency from 104; tokens/sec from 110. Model and the context percentage always get room; the right edge shows the session title badge if set, else the cwd label with git branch. `display.status_bar.fields` can hide named segments (`context_detail`, `context_pct`, `battery`, `title`, `duration`, `compressions`, `cache_hit`, `latency`, `tps`, `voice`, `bg_tasks`, `bg_subagents`). Status bar position default "top" (of the composer block). [obs] `appChrome.tsx`, `uiStore.ts`
- **Status rule extras per docs:** elapsed time "since last prompt / session total" (live while running, frozen after), compression count, running `/bg` count, a YOLO warning badge, live session count (clickable: opens the switcher). A spawn HUD shows depth `d<depth>/<max>` and width `⚡<widest>/<cap>+<extra>`, coloured muted → warn at ratio ≥ 0.66 → error at ≥ 1 or when paused. [obs]
- **Banner sections** (collapsible by click): Tools (open), Skills, System Prompt, MCP Servers (collapsed). State resets each launch. [obs docs]
- **Busy indicator:** styles `kaomoji` (default), `emoji`, `unicode`, `ascii`; face palette rotates about every 2.5 s per docs; glyph widths matched so the bar does not jitter. [obs interfaces.ts; docs]
- **Message rows:** user, assistant, system, tool/diff kinds; optional timestamps; long system lines collapse beyond 400 characters; long user messages backed by a paste token (over 300 chars) render collapsed. [obs] `messageLine.tsx`

### 3.8 Multiline composer (text editor)

- **Model:** a single string value with a cursor (grapheme-aware stops), a separate "continuation buffer" of completed lines (from backslash-Enter), and a list of composer tokens (paste tokens, image tokens) referenced by labels inside the text. [obs] `textInput.tsx`, `useComposerState.ts`
- **Keys inside the editor:** [obs] README table, `hotkeys.ts`, `textInput.tsx`
  - Enter submits; Shift+Enter or Alt+Enter inserts a newline; a line ending in `\` plus Enter moves that line (minus the backslash) to the continuation buffer (fallback for terminals without modifier reporting).
  - Left/Right move; Ctrl/Meta+Left/Right or Meta+B/F move by word; Home/Ctrl+A, End/Ctrl+E line start/end (Cmd on macOS where the terminal forwards it).
  - Backspace/Delete; modified Backspace or Ctrl+W delete previous word; modified Delete deletes next word; Ctrl+U kills to line start; Ctrl+K kills to line end (but see Ctrl+K queue rule in 3.12).
  - Up/Down move between lines inside a multi-line draft; only when the cursor is on the first/last line do they walk the queue then history (3.11).
  - Undo/redo Ctrl+Z / Ctrl+Y (Cmd on macOS); undo stack capped at 200 snapshots; redo cleared on new edit.
  - Mouse: click to place cursor, multi-click within 500 ms selects word/line, drag selects; right-click copies a selection or pastes when nothing is selected; selection copy/cut via platform copy shortcut and Ctrl+X.
  - Esc Esc within 500 ms discards the draft (pushing it to history so Up recalls it).
- **Paste handling:** [obs] `useComposerState.ts`, `config/limits.ts`, `methods_complete.py`
  1. Bracketed paste markers are stripped; pasted text is inserted inline, never newline-flattened.
  2. If the paste looks like a path to an image, try to attach it as an image token (canonical: stage the file on the host first); else ask the backend `input.detect_drop` whether it is a dropped file and use the normalized text it returns.
  3. Large pastes collapse: when the paste has at least the configured line count (default 5) or character count (default 2,000) it is replaced by a short label token; the backend `paste.collapse` stores the full text under `~/.indus/pastes/paste_<n>_<HHMMSS>.txt` and returns a placeholder naming the line count and path. The model receives the expanded text on submit.
  4. Paste hotkey (Ctrl+V / Alt+V; Cmd+V on macOS) reads the clipboard: OSC 52 first in remote shells, native clipboard first otherwise, each falling back to the other; no text means try a clipboard image.
  5. Every async step re-checks that the destination session and composer revision are unchanged; stale results are dropped.
- **External editor:** Ctrl+G (Cmd+G on macOS) or Alt+G (VS Code/Cursor capture the primary chord) writes the draft including the continuation buffer to a temp file, suspends the renderer, runs `$VISUAL`/`$EDITOR` (shell-tokenized), else the first of `editor`, `nano`, `pico`, `vi`, `emacs` on PATH, else `notepad.exe` on Windows, else `vi`; on clean exit the saved text is submitted. [obs] `lib/editor.ts`, README
- **Performance:** keystroke echo has a fast path for append/backspace on simple shapes (terminal-gated); frames batch at 16 ms. [obs] `textInput.tsx`
- **IME:** multi-character inserts are treated as recomposition/paste, not rapid typing (test names mention Vietnamese Telex). [obs test names; inf]
- **Masked inputs** (sudo, secret, vault) and clarify free text use a simpler stock text-input component with its default bindings, not this editor. [obs README]

### 3.9 Completion (slash and path)

- **Trigger:** composer text changes while no blocking overlay is open; debounced 60 ms; a response is discarded if the input changed meanwhile. [obs] `useCompletion.ts`
- **Which request:** [obs]
  - Input starting with `/` (a command shape) → `complete.slash { text, session_id }`; replacement offset comes from the server (`replace_from`, default 1).
  - `/model…` gets no completion (the model picker owns it).
  - A whitespace-preceded `/token` at the caret mid-message → inline skill reference: `complete.slash` for `/<token>` but only `skill` items kept; replacement starts at the token.
  - Otherwise, a trailing token that looks like a path (`./`, `../`, `~/`, `/`, `@`, a drive letter, or contains `/`) → `complete.path { word, cwd?, session_id? }`. The cwd is the session's, else the launch directory, never the renderer's or daemon's own cwd. MUST: under a non-local terminal backend, the backend lists through that backend, never the host's same-named tree.
- **Server side:** slash completion uses the CLI completer (registry, plugins, skills/bundles, argument completion) under the calling session's profile and workspace so project-local skills appear. Path completion: `@` alone lists context roots; `@file:`/`@folder:` prefixes; plugin `@prefix:` providers (20-item cap); bare `@word` of 2+ chars does a fuzzy basename search over the repo (30 results) and ranks matching agent-profile mentions above files; otherwise a directory listing. [obs] `methods_complete.py`
- **Client merges** locally registered widget apps into slash results (name position only), ranked with the same fuzzy scorer. [obs]
- **Fuzzy scoring** (lower wins): exact id/label/alias 0, prefix 1, substring 2; description words exact 3, word-prefix 4, word-substring 5; no match = infinity. Query normalized by trimming, stripping leading slashes, lowercasing. [obs] `fuzzyScore.ts`
- **Keys:** Up/Down cycle (wrap) when the list is open, input non-empty and not browsing history; Tab applies the selected item (only outside multiline mode); Enter applies a completion first when the typed command is not yet a complete known command, otherwise submits. Item `{ text, display, meta, kind? }`. [obs]

### 3.10 Slash commands

- **Dispatch order on submit of `/name args`:** [obs] `createSlashHandler.ts`
  1. Renderer-local registry (case-insensitive name or alias). Typed commands are reported to metrics once; canonical gateways also get a non-awaited `command.observe` after the local handler says it ran (skipped forever once the gateway answers unknown-method).
  2. Live widget-app registry.
  3. Catalog alias map from `commands.catalog`: exact alias → re-dispatch canonical name with same args; otherwise fuzzy name tiers (description tiers never auto-execute): one best match → re-dispatch; several → "ambiguous command" listing up to 6.
  4. `slash.exec { command, session_id }` → backend runs it in a persistent per-session worker subprocess (a classic-CLI instance) with a 45 s reply timeout (floor 5 s) and an orphan watchdog. Output longer than 180 chars or more than 2 non-empty lines opens the pager; else one system line; a `warning` is prefixed.
  5. Only "this executor does not own the command" refusals fall through to `command.dispatch { name, arg, session_id }`; a timeout/crash is shown as itself (MUST: the fallback's "not a known command" message used to hide real failures).
  6. Dispatch result kinds: `exec`/`plugin` → output line; `alias` → re-dispatch target; `skill` → submit the expanded skill body as a normal prompt while the transcript shows the projected `display` text; `send` → optional notice then submit message; `prefill` → put text into the composer (used by `/undo`).
- **Stale guard:** each slash run captures a flight counter and destination; late results for a superseded command or a different session are dropped. [obs]
- **Slash commands and `!cmd` never queue;** they execute immediately even while a turn runs. Plain text queues (3.12). [obs README]
- **Renderer-local commands (registration order core → session → ops → setup → debug, plus vendor billing):** [obs] `app/slash/commands/*.ts`
  - Core: `/help`, `/quit` (alias `exit`), `/update` (exit code 42 after 100 ms), `/mouse` (alias `scroll`; presets on|off|toggle|wheel|buttons|all; persisted to config), `/clear` (alias `new`; Ctrl+L per README though code maps Ctrl+L to redraw — see R05.11), `/redraw`, `/status`, `/title`, `/density`, `/details` (alias `detail`), `/fortune`, `/copy`, `/paste`, `/prompt` (alias `compose`), `/terminal-setup`, `/logs`, `/history`, `/save`, `/focus`, `/statusbar` (alias `sb`), `/battery`, `/queue` (alias `q`), `/steer` (alias `s`), `/undo`, `/retry`.
  - Session: `/bg` (alias `background`), `/btw`, `/model`, `/sessions` (aliases `switch`, `session`, `resume`), `/image`, `/personality`, `/compress`, `/branch` (alias `fork`), `/voice`, `/pet`, `/theme`, `/skin`, `/indicator`, `/yolo`, `/reasoning`, `/fast`, `/busy`, `/verbose`, `/usage`.
  - Ops: `/stop`, `/reload-mcp` (alias `reload_mcp`), `/reload`, `/browser`, `/rollback`, `/agents` (alias `tasks`), `/journey` (aliases `learning`, `memory-graph`), `/replay`, `/replay-diff`, `/reload-skills` (alias `reload_skills`), `/skills`, `/connectors`, `/plugins`, `/tools`.
  - Setup: `/setup` (suspends the renderer, runs the external setup wizard, opens a new session on success). Debug: `/heapdump`, `/mem`, `/theme-info`, `/widgets-reload`, plus one command per registered widget app. Vendor: `/subscription` (alias `upgrade`), `/topup`, `/wake`.
- **Backend registry (103 commands; owned by `04-cli.md`, listed here for autocomplete parity).** Flags: (c) CLI/TUI only, (g) messaging-gateway only. [obs] `<cli-pkg>/commands.py`
  - Session: start(g), new (alias reset), topic(g), clear(c), redraw(c), history(c), save, retry, prompt (compose)(c), undo, title, handoff(c), branch (fork), worktree(c), compress (compact), rollback, snapshot (snap)(c), stop, pause(g), approve(g), deny(g), bg, btw, agents (tasks), journey (learning, memory-graph)(c), queue (q), steer (s), goal, heartbeat (hb), refine, review, loop, plan, moa, subgoal, status, egress, context (ctx), sethome (set-home)(g), resume, sessions, restart(g).
  - Configuration: export(c), import(c), config(c), model, codex-runtime (codex_runtime), personality, statusbar (sb)(c), battery(c), timestamps (ts)(c), verbose(c), focus(c), footer, yolo, approvals, reasoning, fast, skin(c), indicator(c), voice, wake(c), busy, autoprune.
  - Tools & skills: tools(c), toolsets(c), skills(c), memory, bundles, pet(c), hatch (generate-pet)(c), learn, init, cron(c), suggestions (suggest), blueprint (bp), curator, kanban, reload(c), reload-mcp (reload_mcp), reload-skills (reload_skills), browser(c), plugins(c).
  - Info: whoami, profile, diff, commands(g), help, palette(c), usage, subscription (upgrade)(c), login, topup, insights, platforms (gateway)(c), platform(g), copy(c), paste(c), image(c), update, version, debug. Exit: quit (exit)(c).
- **Catalog:** `commands.catalog` (empty query) returns built-ins, user quick commands, plugin commands and skill commands, with categories, aliases and a canonical alias map; quick commands are catalog-only (not completed). [obs backend AGENTS.md]
- **Canonical slash execution limits:** on the shared daemon only `help`, `commands`, `status`, `context`, `version`, `whoami`, `title`, skill resolution, and exec-type quick/plugin commands (30 s sanitized-env exec, redacted output) are supported; everything else returns `unsupported_command` and the renderer shows that, never silently queuing. [obs CANONICAL_GATEWAY.md / backend AGENTS.md]
- **Destructive-command confirm:** a confirm card guards destructive slash commands unless disabled by `INDUS_TUI_NO_CONFIRM`. [obs env.ts; inf which commands]

### 3.11 Input history

- **Storage:** append-only text file in the home dir. Format compatible with prompt_toolkit file history: each entry is preceded by a blank line and a `# <timestamp>` comment line, and each line of the entry is prefixed with `+`. Multi-line entries are preserved. [obs] `lib/history.ts`
- **Rules:** load lazily once per process; keep the last 1,000 entries; ignore empty/whitespace input; skip an entry equal to the immediately previous one; write failures are silent. [obs]
- **Navigation:** Up/Down only at the first/last line of the draft and only with an empty continuation buffer; queue editing takes priority over history (Up first selects queued messages, then history). Esc Esc pushes the discarded draft to history. [obs] `useInputHandlers.ts`, README

### 3.12 Submission, queue and busy-input modes

- **Submit pipeline:** if completion is open, apply it first (3.9). Empty Enter: a second empty Enter within 450 ms while busy interrupts (keeping busy if messages are queued so the queue drains on the real settle); while idle with a queue it sends the next queued item. Text ending with `\` goes to the continuation buffer. Otherwise the continuation buffer plus current line is joined with newlines and dispatched. [obs] `useSubmission.ts`
- **Dispatch:** `/…` → slash (3.10); `!cmd` → `shell.exec` (3.12.1); text containing `{!cmd}` → each occurrence replaced by that command's stdout before sending (queued drafts keep the raw text until actually sent); `[[...]]` paste tokens expand; otherwise `prompt.submit`. [obs]
- **`prompt.submit` params (conceptual):** `{ session_id, text, attachments?: { path, mime }[], display_kind?: "hidden", interrupted?, queued?, surface?, truncate_before_row_id?, confirm_truncate?, … }` → result `{ status?: streaming|queued|steered|redirected, user_row_id?, survivor ids?, turn_isolation? }`. Canonical renames `submission_id` to `input_id` and returns a receipt `ref` (profile id, session id). [obs] contracts, `canonicalGateway.ts`
- **Busy-input modes** (config `display.busy_input_mode`; TUI default `queue`, while the core default elsewhere is `interrupt`, because in a full-screen UI people type the next prompt during streaming): [obs] `useConfigSync.ts`, `useSubmission.ts`
  - `queue`: legacy gateways keep a renderer-side FIFO that auto-drains after each completed response unless an item is being edited; canonical gateways admit the input immediately into a durable server FIFO behind the running turn (survives crashes, visible as `[queued]` rows to every viewer; the user bubble appears when its turn starts).
  - `steer`: `session.steer` injects the text into the next tool result without starting a new user turn; if rejected (no agent, idle, unsupported) it falls back to queue.
  - `interrupt`: submit immediately; the backend decides atomically whether to redirect the active model call or steer after the current tool, with legacy interrupt + queue as compatibility.
- **Queue editing:** Up loads a queued item into the composer (Esc cancels the edit; Ctrl+X while editing deletes the item — on canonical this cancels it server-side for every viewer via `prompt.cancel`; unknown-state rows use `prompt.resolve_unknown`). Resubmitting an edited plain-text item replaces it and promotes it to send next (front of queue if still busy). Ctrl+K (when the queue is non-empty and a session exists) sends the next queued item now. [obs] README, `useInputHandlers.ts`, docs
- **Offline input:** while disconnected, Enter writes the draft to a private journal (fsync + rename) before clearing the composer; reconnect resumes only that destination; confirmed admissions are retired; ambiguous retries keep their original id. [obs CANONICAL_GATEWAY.md]
- **Image-bearing turns** cannot be steer/redirect corrections; they must use queue mode. [obs CANONICAL_GATEWAY.md]

#### 3.12.1 Shell escape

- `!cmd` appends a user row `!cmd`, sets busy/"running…", calls `shell.exec { command, session_id }`. Backend refuses empty commands, "hardline" and dangerous commands (code 4005, "use the agent for dangerous commands"), runs with `shell=True` in a credential-scrubbed env, 30 s timeout (code 5002), redacts secrets then keeps the last 4,000 chars of stdout and 2,000 of stderr; returns `{ stdout, stderr, code }`. Renderer prints combined output as a system line and `exit <code>` when non-zero or empty. [obs] `methods_tools.py`, `useSubmission.ts`

### 3.13 Streaming output and turn lifecycle

- **States:** idle → (message.start) busy/streaming → (message.delta*, interim*, tool events, reasoning) → (message.complete) idle; or busy → (local interrupt) interrupted-sealed → (message.complete status=interrupted) idle. [obs] `turnController.ts`
- **Buffering:** deltas accumulate raw text (never replace the buffer with the `rendered` fragment — doing so lost prose; MUST). Flushes to the screen are batched: 16 ms normally, 80 ms while the user is typing (typing considered idle after 250 ms), 96 ms while scrolling. Reasoning pulses every 700 ms. [obs] `config/timing.ts`, comments
- **Live render caps:** the live row renders at most 16,000 chars / 240 lines of the tail; reasoning buffer trims to the last 60,000 chars once it exceeds 80,000; thinking preview 160 chars. [obs]
- **Streaming markdown:** the live row finds a stable boundary (outside open fences etc.) and renders completed blocks as markdown while the unstable tail stays plain. [obs] `streamingMarkdown.tsx`
- **Segments:** interim messages and tool boundaries seal the current buffer into a "segment" message; tools that ran since the last segment attach to it. [obs]
- **Completion:** the final text is de-duplicated against already-sealed segments (only the unseen tail becomes a new message; a diff-only segment is not repeated if the final text narrates it). If the turn failed with no partial reply and the final text is a bare "Error: …" slot, it is replaced by a structured failure (title, details, next step, from `error_surface { layer, code, retryable }`). Status returns to "ready"; usage merges; optional warning to the activity lane; terminal bell when `bell_on_complete`; a billing block opens a confirm card (out of v1). Late subagent events after completion must not resurrect finished entries. [obs] `createGatewayEventHandler.ts`
- **Rendering of text:** payloads already containing ANSI are printed as-is; otherwise a built-in markdown subset: ATX/setext headings, bullet/numbered/task lists, block quotes, tables (column fit with 2-space gaps, min column 3), fenced code with syntax colours, diff colouring, inline code, emphasis (careful with `__dunder__` identifiers), links/plain URLs (OSC 8 when supported; click opens browser), footnotes, definition lists, horizontal rules, LaTeX math converted to Unicode with literal fallback, `MEDIA:` lines and audio directives. A render cache holds 512 entries. [obs] `markdown.tsx`, docs
- **Errors:** an `error` event without an execution stamp is a build/RPC failure (not a turn settlement). [obs]

### 3.14 Tool output rendering

- **Backend emission gate:** tool start/complete events are sent only when the session's tool-progress mode is on, or the tool's lifecycle is required by the UI (clarify, connectors, todo), or there is an inline diff, or the result needs the user. Modes: `off`, `new`, `all`, `verbose`. [obs] `tool_progress.py`
- **tool.start:** `{ tool_id, name, context (≈80-char preview), args, args_text? (verbose only), labels? }`. **tool.complete:** adds `duration_s`, parsed `result`, a `summary` for a few tools ("Did N searches in 3.2s", fallback warnings), `result_text` (verbose only), `inline_diff` for file edits, todo snapshot. Verbose text is secret-redacted then capped to the tail: at most 1,000 chars and 16 lines, never cutting mid-line, with a leading note of how much was omitted. Duration format: under 10 s one decimal, under 60 s whole seconds, else `Xm Ys`. [obs]
- **Renderer:** active tools show a spinner row with a per-tool verb (localized table, e.g. "reading", fallback rotating verbs); completion collapses to a trail line with duration; the trail and activity lane each keep 8 entries; inline diffs (when `display.inline_diffs`) become their own diff segment; `tool.generating` adds "drafting <tool>…"; tools running longer than 8 s get up to 2 ambient "charm" lines. [obs] `thinking.tsx`, `turnController.ts`, `useLongRunToolCharms.ts`
- **Detail sections:** global `details_mode` hidden|collapsed|expanded plus per-section overrides for `thinking` (default expanded), `tools` (expanded), `subagents` (falls through to global, collapsed), `activity` (hidden). `/details [mode|cycle]` and `/details <section> <mode|reset>`. When every panel is hidden, ambient errors still surface via a floating alert. [obs docs]
- **Todo panel:** parsed items `{ id, content, status: pending|in_progress|completed|cancelled, parent? }`; collapsible. [obs]

### 3.15 Live agents dock and monitor (secondary)

- Dock above the composer appears while child agents run: live counts, task names, elapsed time, latest activity; row budget shrinks on short terminals. Ctrl+R/F7 toggles dock preview vs one summary line (process-lifetime only). Ctrl+T opens the full monitor: Up/Down select, Enter details, `t` live transcript tail (last 16 KiB, polled only while open), `g`/`G` top/bottom, `e` steering form (Enter queues; "queued" says the message is held until the next tool boundary and has not yet been delivered), `x` stop child, `X` stop subtree, Esc back. [obs README, docs]
- Subagent identity: server-issued id, else synthetic `sa:<index>:<goal>`; ordered by depth then index; updates for unknown ids are dropped unless the event is spawn/start. Delegation caps fetched at most every 5,000 ms. Finished fan-outs kept in a ring of 10 for `/replay`. [obs]
- v1 can render subagents as indented tool rows only. [inf]

### 3.16 Interrupt, stop, steer and redirect

- **Ctrl+C resolution** (MUST: a non-empty composer always wins): draft present → clear it; busy with session → interrupt; else exit (in dashboard-embedded mode, request a new session instead of exiting). Ctrl+D exits only from an empty composer (no text, no buffer, no tokens). [obs] `useInputHandlers.ts`
- **Double Esc** within 500 ms: draft → discard; busy → interrupt, but only when no blocking card is up; a single Esc never cancels work. Voice push-to-talk bindings on Esc take precedence. [obs]
- **`/stop`, double empty Enter** also interrupt. [obs]
- **Interrupt flow (renderer):** [obs] `turnController.interruptTurn`
  1. Canonical: call `session.interrupt { session_id, execution_generation }`; if the reply says the turn is not running or the generation differs, it is a successful no-op; on error show "interrupt failed" only if the same turn is still active; if completion or navigation won the race, do nothing. Legacy: fire-and-forget `session.interrupt { session_id }`.
  2. Mark interrupted (later deltas ignored), close reasoning, take the sealed segments and partial buffer, go idle.
  3. Append the sealed segments; if a partial or pending tools exist append one assistant message with the partial text followed by an italic "[interrupted]" marker; else a system note.
  4. Status "interrupted" for 1,500 ms then "ready"; or, if queued messages must drain, stay busy ("interrupting…") until the real settle.
  5. When the interrupted `message.complete` arrives with a persisted partial that extends what was sealed, replace the sealed bubble's text so the screen matches storage.
- **Backend `session.interrupt`:** silences streaming TTS, marks user input on the turn, interrupts the turn without waiting for a lazily building agent (waiting used to fail Stop), and retires the crash-recovery marker immediately so a crash right after Stop is not auto-continued. Optional expected hosted-task id check. Reply `{ status: interrupted|not_interrupted }`. [obs] `methods_session_interrupt.py`
- **Backend steer/redirect:** require non-empty `text` (4002); if a redirect arrives while the agent is still building, or compression is in flight, the text is queued instead (a correction during compression would abort it); unsupported agents → 4010; steer on an idle session → `rejected` so the client queues it as a normal next prompt; on acceptance record the correction on the in-flight turn and purge queued copies of the original prompt so it cannot re-fire. Status values: steer `queued`, redirect `redirected`, else `rejected`/`queued`. [obs]
- **Canonical controls:** steer/redirect target the active generation; an ambiguous control reply is kept for inspection but never replayed automatically. [obs CANONICAL_GATEWAY.md]

### 3.17 Approval and other blocking prompts

- **Mechanism:** server requests. Backend `send()` blocks the agent thread until a response frame with the same id arrives or the deadline passes; `send_async` (approvals) resolves through the approval queue. On timeout/interrupt/session close the backend emits one `request.cancel { id, method, reason }`. A response for an id no longer open is dropped. Unanswered requests are returned by resume/activate/events-since so a reconnecting renderer re-opens the cards (without ringing the bell again). A client that never sent `client.capabilities { server_requests: true }` is treated as unable to answer and `send` returns "no answer" immediately instead of stalling for the full deadline. [obs] `server_requests.py`
- **Server requests the TUI answers:** `approval` → `{ choice: once|session|always|deny, all? }`; `clarify` → per-question locks then `{}`/answers; `sudo` → `{ value }`; `secret` → `{ value }` (named env var; empty = skipped); `vault.unlock_prompt`, `vault.save_login`, `vault.code` → `{ value }`. Desktop-only (`preview.read`, `preview.act`, `terminal.read`, `window.read`, `tour`, `theme.apply`, `theme.pick`, `setup_beat`, `display.install.sudo`) are refused at once with `-32601`. [obs] `createServerRequestHandler.ts`, contracts
- **Deadlines:** sudo 120 s; vault unlock 120 s; vault save-login and code 180 s; terminal/window read 30 s; preview read 45 s; generic ask 300 s (secret uses it); clarify from config, 300 s historical default, `<= 0` means unlimited; approval timeout owned by the approval policy (`02-tools.md`). [obs] `agent_callbacks.py`, `server.py`
- **Approval card:** double-bordered warn-coloured box titled with the description (default "dangerous command"); the command (redacted server-side) wrapped to the panel width (cols − 8, min 20), first 10 lines shown plus "N more lines"; options numbered. Option set: from `choices` if the backend sends it (order once, session, always, deny); `smart_denied` → once, deny; `allow_permanent: false` → drop always; else all four. Keys: Up/Down move, Enter confirm, digits 1..N quick-pick, Esc deny; Ctrl+C (global) deny. No-answer-possible → backend withdraws (not denies) the queue entry. [obs] `prompts.tsx`, `server.py`. Note the README's o/s/a/d letters are not what the code does (R05.11).
- **Clarify card:** one or more questions `{ qid, question, choices?, multi_select? }`; Tab/Shift+Tab switch question; Up/Down move; Enter locks; digit quick-pick; Space toggles in multi-select; choosing "Other" switches to free text; empty free text skips; Esc cancels remaining questions. Each lock calls `clarify.lock { request_id, question_id, answer|null }`; answers stay editable until all are locked; locked answers survive a timeout; the last lock resolves the request. If the clarify tool completes while its card is still open, the card is flushed as abandoned with a "(timed out)" transcript line. [obs]
- **Sudo/secret/vault:** masked single-line entry; Enter submits; Ctrl+C or Esc cancels by sending an empty value. Secret values are saved securely into the profile env and the tool is told only success/skipped. A secret request for a session that is already closed is refused (no prompt that nobody can answer). [obs]
- **Input routing while a card is open:** the card's own key handler gets the keys; the global handler only intercepts Ctrl+C (cancel/deny) and lets scroll inputs (wheel, PgUp/PgDn, Shift+Up/Down) through so the user can read context above the question. Double-Esc draft discard still works; double-Esc interrupt does not. [obs]
- **Status/bell:** opening a card sets status ("approval needed", "waiting for input…", "sudo password needed", …) and rings the terminal bell unless replayed (bell configurable via `bell_on_prompt`). [obs]
- **Confirm card** (local): title, detail, confirm/cancel labels, danger flag; used for expensive model switches, destructive commands, billing walls. [obs]
- **Pager overlay** (long slash output): Up/k, Down/j, PgUp/b, PgDn/Space/Enter (Enter/Space at last page closes), g/G top/bottom, Esc/q/Ctrl+C close. [obs]

### 3.18 Model switch

- **Entry points:** `/model` (no args) or Ctrl+O opens the picker without disturbing the draft; `/model --refresh` forces a provider list refresh; `/model <spec>` switches directly. [obs] `session.ts`, `useInputHandlers.ts`
- **Picker stages:** provider list → model list → reasoning effort (step 3 of 3, plain list) with side stages "key" (enter an API key for an unconfigured provider → `model.save_key`) and "disconnect" (confirm → `model.disconnect`). 12 visible rows; type-to-filter on list stages (Esc clears the filter first, then goes back a stage); a footer shows persistence scope "session only" or "global", toggled with Ctrl+G when allowed. Data from `model.options { refresh?, include_unconfigured?, explicit_only? }` (curated ids, cost hints). Selecting emits a `/model` command with a session-scope or global flag. [obs] `modelPicker.tsx`
- **Apply (legacy):** `config.set { key: model, value, session_id, confirm_expensive_model? }`. No busy guard: idle switches immediately; mid-turn the backend defers it to the next turn start and replies `deferred: true`. `confirm_required` opens a danger confirm ("switch anyway") that retries with confirmation. Success prints a system line and updates the status bar model. [obs]
- **Apply (canonical):** `session.mutate`, carrying a stable request id and revision/generation preconditions; never a global config write from the composer. [obs]
- **Stale-code guard:** the backend refuses model-serving RPCs when its loaded code differs from disk after an update (error 5098). [obs] `methods_complete.py`

### 3.19 Session switcher (and resume)

- **Entry:** Ctrl+X (unless editing a queued item or cutting a selection), `/sessions` (aliases switch/session/resume), `/sessions new`, or clicking the live-session count. [obs]
- **Rows:** `+ new` pinned first; then live sessions in this process (status glyph: idle ✓, starting …, waiting ?, working ▶; title up to 64 chars; short model name); then resumable stored sessions (title, preview, relative age, source tag; from `session.list`, newest first; sub-agent/kanban sources hidden). 12 visible; width between 64 and 128 columns; list refreshes every 1,500 ms keeping the selection on the same id. [obs] `activeSessionSwitcher.tsx`
- **Keys:** Up/Down move; Enter switches (live) or resumes (stored) or, on `+ new` with typed text, dispatches a new session with that prompt; Tab on `+ new` picks a model for just that new session; Ctrl+N blank new session; Ctrl+R refresh; Ctrl+D close selected live session (falls back to another live session or a new one if it was current); `d` then `d` deletes a stored session (any other key cancels); Esc closes; mouse click selects. Older README mentions 1–9 quick-pick in a separate resume picker. [obs]
- **Busy guard:** switching/resuming respects a busy guard (the user is told to stop the turn first). [obs]
- **Close semantics:** legacy `session.close` tears down the runtime but keeps the stored row; canonical detaches only (another viewer may own the session). [obs]

### 3.20 Keybinding master table (main view)

| Key | Action |
|---|---|
| Enter | submit (or apply completion) |
| Shift+Enter, Alt+Enter, `\`+Enter | newline / continuation |
| Ctrl+C | clear draft → interrupt → exit |
| Ctrl+D | exit when composer empty |
| Esc Esc (≤500 ms) | discard draft / interrupt when busy |
| Esc | cancel queue edit; clear selection |
| Ctrl+L | redraw (clears selection, forces full repaint) |
| Ctrl+G, Alt+G | open external editor |
| Ctrl+V, Alt+V (Cmd+V mac) | paste text → clipboard image fallback |
| Ctrl+X | cut input selection / delete edited queue item / open session switcher |
| Ctrl+O | model picker |
| Ctrl+T | agents monitor |
| Ctrl+R, F7 | toggle live dock preview |
| Ctrl+K | send next queued item now (when queue non-empty) else kill to end of line |
| Shift+Tab | toggle YOLO (auto-approve) for the session via `config.set key=yolo` |
| Tab | apply completion |
| Up/Down | completion cycle → queue edit → history (edge lines only) |
| Shift+Up/Down | scroll transcript one row |
| PgUp/PgDn | scroll half a viewport (min 4 rows) |
| Mouse wheel | accelerated scroll; with Ctrl/Meta held = precision one row per frame |
| Ctrl+B (default, configurable) | voice push-to-talk toggle |
| Ctrl+Z / Ctrl+Y | undo / redo in composer |

[obs] `useInputHandlers.ts`, `hotkeys.ts`, `platform.ts`. On macOS the "action" modifier is Cmd where the terminal forwards it (kitty protocol); Ctrl remains the fallback for D. Cmd+C copies a selection; Ctrl+C keeps its interrupt meaning. In remote shells copy goes through OSC 52. [obs]

### 3.21 Theming

- **Theme model:** `Theme { color: Colors, brand: Brand, bannerLogo, bannerHero }`. Colors include primary, accent, border, text, muted, completion menu backgrounds (4), label, ok, error, warn, tool, thinking, syntax (string/number/keyword/comment), prompt, session label/border, status bg/fg/good/warn/bad/critical, selection bg, diff added/removed (line and word), shell-dollar. Brand: name, icon, prompt glyph (default `❯`), welcome text, goodbye text, tool prefix (default `┊`), help header. [obs] `theme.ts`
- **Palette construction:** derived from a small set of seed colours for dark and light (separate seed sets), with tones (muted, label, status, surface, chip, selection) derived by mixing/desaturating; colours are adapted to the actual background to meet a WCAG-style contrast floor, with polarity limits for fills. [obs]
- **Skin data from backend:** `Skin { name, description, colors, light_colors, dark_colors, branding, banner_logo, banner_hero, tool_prefix, help_header }` arrives in `gateway.ready` and `skin.changed`; merged over the default theme; banner art parses rich-text colour tags. `/skin <name>` previews live while browsing. [obs]
- **Light/dark detection order:** explicit light boolean env → named theme env (`light`/`dark`, or a hex background) → background hex hint env (luminance threshold) → `COLORFGBG` last field (slots 7 or 15 mean light; when set it is authoritative) → `TERM_PROGRAM` allow-list of light-default terminals → OSC 11 background probe (answer cached into the env hint; pure `#000000`/`#ffffff` answers are distrusted as "unset"). [obs] `theme.ts`, docs
- **Boot theme:** the last resolved theme is cached so the first frame paints without a flash; default theme only on first run. [obs] `uiStore.ts`, `themeBoot.ts`
- **Truecolor:** opt-in env forces truecolor before colour libraries initialize. [obs] `forceTruecolor.ts`

### 3.22 Config sync

- On ready, fetch `config.get { key: full }` once (memoized) and apply display settings: busy mode, status bar on/position/fields, indicator style, compact mode, theme, details mode and sections, reasoning visibility, streaming on/off, timestamps, inline diffs, bells, battery, focus view, mouse tracking, language, paste collapse thresholds, voice record key. [obs] `useConfigSync.ts`
- Poll `config.get { key: mtime }` every 5,000 ms; on change re-fetch and re-apply; an MCP revision change triggers `reload.mcp`. Errors stay quiet and heal on the next poll. [obs]
- Busy-mode preference reads/writes are session-scoped on canonical. [obs]

### 3.23 Resize, terminal modes and quirks

- **Resize:** leading + trailing throttle at 32 ms: reflow immediately on the first event of a burst, at most once per interval after, always one final reflow so the last width is exact. [obs] `resizeCoalescer.ts`
- **Mode reset:** on start, on process `exit` (synchronous write), on signals and uncaught errors, write a reset sequence that disables DEC locator, all mouse modes (9, 1000, 1001, 1002, 1003, 1005, 1006, 1015, 1016, 2029), focus events (1004), bracketed paste (2004), leaves the alt screen (1049), pops kitty keyboard, resets modifyOtherKeys, resets SGR, shows the cursor. MUST: missing this on `/quit`/Ctrl+D left mouse reporting armed and garbage appeared in the next shell. [obs] `entry.tsx`, `terminalModes.ts`
- **Mouse tracking presets:** `off`; `wheel` (1000+1006: scroll and click, no drag/hover; recommended in tmux); `buttons` (adds 1002 drag); `all` (adds 1003 hover; default). Env can force on/off; native and Termux default off. [obs docs, env.ts]
- **Synchronized output:** DEC 2026 begin/end used only on terminals known to support it (kitty, iTerm2, WezTerm, Ghostty, Zed, VS Code, …); never inside tmux, which proxies but does not implement it. Scroll-region (DECSTBM) fast path is used only for deltas smaller than the viewport and with atomic sync. [obs] `<ink-fork>/terminal.ts`, `ink.tsx`
- **Extended keys:** kitty keyboard push and/or modifyOtherKeys enabled on a list of terminals (iTerm2, kitty, WezTerm, Ghostty, tmux, Windows Terminal, VS Code), skipped where the push breaks word-delete; disabled during external-editor handoff and re-enabled after. [obs]
- **Dead terminal:** five consecutive EIO/EPIPE write errors (tab closed, SSH dropped without SIGHUP) → kill/detach gateway and exit 1 rather than spin forever. [obs] `entry.tsx`
- **Signals:** SIGTERM/SIGHUP shut down cleanly (log which signal); in dashboard-embedded mode SIGINT is ignored so a stray raw-mode drop cannot kill the embedded UI. [obs]
- **Memory monitor:** warns on fast heap growth, writes a heap dump at high level, exits 137 at critical level after resetting terminal modes. [obs]
- **Terminal hints:** detects VS Code/Cursor/Windsurf and tmux on macOS and suggests `/terminal-setup` (installs IDE terminal keybindings for Cmd+Enter and undo/redo) or tmux settings. [obs] `terminalParity.ts`, docs
- **Termux mode:** keeps prior output (no screen clear), inline rendering, no mouse, optional fast echo. [obs]
- **Hyperlink clicks:** with mouse tracking on, the renderer opens clicked OSC 8 links itself because the terminal never sees the click. [obs] `entry.tsx`
- **Text selection:** in-app drag selection with a uniform background colour (not inverse video); copy via platform shortcut or OSC 52. [obs docs]

### 3.24 Reconnect and crash recovery

- **Socket drop (attached):** status "reconnecting", keep the sid, one "connection lost" note per outage; the client's backoff redials; next `gateway.ready` resumes the stored id, including a reply still streaming on the backend; nothing is resubmitted. [obs] `useMainApp.ts`
- **No socket ever granted (bootstrap failed) / spawned backend died:** plan recovery with a budget of 3 attempts per 60,000 ms sliding window: within budget print "restarting and reopening your chat" and resume the same stored session after ready; over budget print one "stopped and could not be restarted" error with the exit code and last stderr line, keep retrying in the background, and still reopen the same chat if it eventually succeeds (never forge a new one). [obs] `gatewayRecovery.ts`
- **Replay:** the backend stamps each event with a per-session monotonic `seq` and keeps a ring of at most 512 events and 4 MiB per session, 64 MiB and 64 sessions per process (FIFO eviction, truncation watermark); `session.events.since { last_seen }` returns newer events or signals truncation so the client refetches; a random replay epoch per process lets clients detect restarts. [obs] `event_replay.py`
- **Turn state on exit:** the turn controller resets; busy and compacting cleared. [obs]

### 3.25 Scrolling and transcript virtualization

- Transcript rows are virtualized with measured heights and spacer rows; offset caches survive width changes; a sticky "jump to bottom" behaviour follows new output unless the user scrolled up. [obs] `useVirtualHistory.ts` (715 lines), test names
- Wheel: accelerated with direction-flip bounce suppression; holding a modifier while scrolling switches to precision scrolling: a single row each frame, with acceleration reset. PgUp/PgDn half viewport (min 4; fallback viewport rows − 8, min 6). Scroll speed env multiplier. [obs]

### 3.26 Localization

- All UI copy goes through a message catalogue resolved at call time (so a later-installed language applies); a key-check script keeps catalogues in sync; `display.language` selects. [obs] `i18n/`, package scripts. v1 may ship English only with the indirection in place. [inf]

### 3.27 Client state model

The renderer keeps three small observable stores plus React-local composer state. Shapes are conceptual. [obs] `uiStore.ts`, `turnStore.ts`, `overlayStore.ts`

- `UiState { sid?, storedSid?, info?: SessionLiveInfo, busy, status: string, busyInputMode: queue|steer|interrupt, compacting, streaming: bool, showReasoning, detailsMode: hidden|collapsed|expanded, sections: map<section, mode>, inlineDiffs, timestamps, statusBar: top|bottom|off, statusBarFields?: set, indicatorStyle, compact, focusView, mouseTracking: off|wheel|buttons|all, pasteCollapseLines (5), pasteCollapseChars (2000), destructiveSlashConfirm, theme: Theme, usage: Usage, notice?, sessionTitle, liveSessionCount, bgTasks: set, battery, gatewayConnected? }`
  - Defaults observed: busy mode `queue`, details `collapsed`, status bar `top`, streaming on, reasoning hidden, inline diffs on, timestamps off, indicator `kaomoji`, initial status "summoning".
- `TurnState { streaming: string, streamSegments: Msg[], streamPendingTools: string[], tools: ActiveTool[], turnTrail: string[], activity: ActivityItem[], reasoning, reasoningActive, reasoningStreaming, reasoningTokens, toolTokens, subagents: SubagentProgress[], todos: TodoItem[], todoCollapsed, outcome }`
- `Overlay` = at most one blocking card plus independent views: `approval?`, `clarify?`, `sudo?`, `secret?`, `vaultUnlock?`, `vaultSaveLogin?`, `vaultCode?`, `confirm?`, `connection?`, `billing?`, `subscription?`, `pager?`, `modelPicker`, `sessions`, `agents`, `journey`, `skillsHub`, `pluginsHub`, `connectors`, `petPicker`, `widget?`, `ambient[]`. "Blocked" = any overlay that owns input is open. Turn idle resets the flow overlays (approval/clarify/confirm) that belong to the finished turn.
- `Msg { role: user|assistant|system|tool, text, kind?: diff|…, tools?: string[], thinking?, toolTokens?, createdAt?, rowId? }`
- `ActiveTool { id, name, context, startedAt, verboseArgs?, labels? }`
- `QueueItem { text, display?, attachments?, destination, settle?, queued?, controlMethod?, executionGeneration? }`
- `ComposerState { input, cursor, inputBuf: string[], tokens: ComposerToken[], completions: CompletionItem[], compIdx, compReplace, historyIdx?, queueEditIdx? }`; `ComposerToken { kind: paste|image, label, text?|path?, mime? }`.
- `ApprovalReq { requestId, command, description, choices?, allowPermanent, smartDenied, sharedControl? }`; `ClarifyReq { requestId, questions: { qid, question, choices?, multiSelect }[], answers: map<qid, string>, sharedControl? }`; `SharedControl { session_id, execution_generation, prompt_id }` (canonical only).
- "Destination" = `{ sid, storedSid?, revision }` captured at the start of every async action; `isCurrentDestination` gates every late result (R05.10).

### 3.28 Turn state machine (renderer)

| State | Event | Next state / effect |
|---|---|---|
| idle | user submits text | send `prompt.submit`; append user bubble (legacy) or wait for its turn (canonical queue) |
| idle | `message.start` | busy; clear per-turn stores |
| busy | `message.delta` | append to buffer; schedule flush (16/80/96 ms) |
| busy | `message.interim` | seal segment; mark interim boundary |
| busy | `tool.start` / `tool.complete` | active tool row / trail line, diff segment, todos |
| busy | server request | open card; status changes; bell |
| busy | `request.cancel` | close matching card |
| busy | user submits text | per busy mode: enqueue / steer / redirect (3.12) |
| busy | Ctrl+C, Esc Esc, `/stop`, Enter Enter | interrupted-sealed (3.16) |
| interrupted-sealed | `message.delta` | ignored |
| interrupted-sealed | `message.complete status=interrupted` | maybe replace sealed text with persisted partial; idle (or drain queue) |
| busy | `message.complete` | finalize messages, usage, bell; idle; drain renderer queue (legacy) |
| any | transport exit | turn reset; idle; reconnect/recovery (3.24) |
| any | `session.replay_gap` | resume the focused session |

[obs] `turnController.ts`, `createGatewayEventHandler.ts`; table assembled [inf].

### 3.29 Launch flag → environment → session-create mapping

| CLI flag / input | Renderer env (rebuild name) | Effect in renderer / backend |
|---|---|---|
| `--resume <id|title>`, `-c`/`--continue` | `INDUS_TUI_RESUME` | resume on first ready instead of creating |
| `-q <text>` | `INDUS_TUI_QUERY` | startup prompt, sent literally once ready |
| `--image <path>` | `INDUS_TUI_IMAGE` | startup image attachment |
| `-m/--model`, `--provider` | `INDUS_MODEL`, `INDUS_TUI_PROVIDER` | `session.create { model, provider }` |
| `-t/--toolsets` | `INDUS_TUI_TOOLSETS` (comma list) | `toolsets` |
| `-s/--skills` | `INDUS_TUI_SKILLS` (comma/newline list, deduped) | `skills` (rendered into the frozen prompt) |
| `--max-turns N` | `INDUS_TUI_MAX_TURNS` | `max_turns` (int, or word such as "unlimited") |
| `-v` / `-Q` | `INDUS_TUI_TOOL_PROGRESS` = verbose / off | `config.set key=verbose` after create |
| `--yolo`, `--ignore-rules`, `--safe-mode`, `--ignore-user-config`, `--checkpoints`, `--pass-session-id`, `--accept-hooks` | boolean envs | creation flags |
| `--worktree` | cwd env set to the worktree | session cwd; exit asks keep/discard |
| invocation directory | `INDUS_CWD`, `INDUS_TUI_CWD` (local backend only) | `cwd` on create; base for path completion |
| shell environment | `INDUS_TUI_CLIENT_ENV` (JSON, allowlisted) | `client_env` when supported |
| `--tui-native` / `display.tui_native` | `INDUS_TUI_NATIVE` | inline rendering, no mouse |
| `--dev` | — | run TS sources via tsx |

[obs] `<cli-pkg>/main_tui_launch.py`, `canonicalGateway.ts::localCreationOptions`, docs. Exact short-flag spellings belong to `04-cli.md`. [inf]

### 3.30 Consolidated limits and timings

| Item | Value | Where |
|---|---|---|
| Startup ready timeout | 15,000 ms (env, floor 5,000) | renderer client |
| RPC request timeout | 120,000 ms (env, floor 30,000) | renderer client / shared channel |
| Heartbeat interval / deadline / max pings | 15,000 ms / 45,000 ms / 8 | shared channel |
| Reconnect backoff | 1,000 ms doubling, cap 30,000 ms, no jitter | renderer client |
| Owner re-ensure budget | 3 per 60,000 ms | renderer client |
| Crash-recovery resume budget | 3 per 60,000 ms | renderer app |
| Bootstrap helper | exec 40 s, ensure 30 s, discover 5 s, 1 MiB stdout | bootstrap |
| Log ring / line cap | 200 lines / 4,096 bytes | renderer client |
| Pre-mount event buffer | 2,000 events | renderer client |
| Replay ring | 512 events and 4 MiB per session; 64 MiB and 64 sessions per process | backend |
| RPC worker pool | 8 (floor 2) | backend |
| Slash worker reply | 45 s (floor 5 s); watchdog poll 2 s, grace 5 s | backend |
| Shell escape | 30 s; stdout tail 4,000 chars; stderr tail 2,000 | backend |
| Server-request deadlines | sudo 120 s, vault unlock 120 s, vault login/code 180 s, terminal/window read 30 s, preview read 45 s, generic 300 s, clarify config (300 s default, ≤0 unlimited) | backend |
| Completion debounce / window | 60 ms / 16 rows | renderer |
| Path completion | plugin providers 20 items; fuzzy basename 30 items, query ≥ 2 chars | backend |
| Input history | 1,000 entries | renderer |
| Undo stack | 200 snapshots | editor |
| Multi-click window | 500 ms | editor |
| Double-Esc / double-Enter | 500 ms / 450 ms | renderer |
| Paste collapse | ≥ 5 lines or ≥ 2,000 chars (config) | renderer |
| Stream flush batch | 16 ms idle, 80 ms typing, 96 ms scrolling; typing idle 250 ms | renderer |
| Reasoning pulse | 700 ms | renderer |
| Live render cap | 16,000 chars / 240 lines | renderer |
| Reasoning buffer | trim to 60,000 when over 80,000 chars | renderer |
| Verbose trail (client) | 800 chars / 12 lines | renderer |
| Verbose tool text (server) | 1,000 chars / 16 lines tail | backend |
| Thinking preview | 160 chars | renderer |
| Activity lane / tool trail | 8 / 8 entries | renderer |
| Interrupt status cooldown | 1,500 ms | renderer |
| Status restore | 4,000 ms (goal 6,000 ms) | renderer |
| Long-run tool charm | after 8,000 ms, max 2 per tool | renderer |
| Delegation caps refresh | at most every 5,000 ms | renderer |
| Spawn history ring | 10 fan-outs | renderer |
| Subagent tail | last 16 KiB | backend |
| Config mtime poll | 5,000 ms | renderer |
| Session switcher | 12 visible, refresh 1,500 ms, width 64–128, title 64 | renderer |
| Model picker | 12 visible | renderer |
| Approval preview | 10 lines; wrap width cols − 8 (min 20) | renderer |
| System line collapse / long user msg | 400 / 300 chars | renderer |
| Slash output to pager | > 180 chars or > 2 non-empty lines | renderer |
| Resize coalesce | 32 ms | renderer |
| Dead-stream exit | 5 consecutive EIO/EPIPE | renderer entry |
| Node heap | 8,192 MB default (below cgroup limit) | launcher |
| Exit codes | 0 normal, 1 dead stream, 42 update, 130 Ctrl+C, 137 memory critical | launcher / entry |
| Markdown cache | 512 entries | renderer |
| Status-bar breakpoints | 72, 76, 80, 84, 88, 92, 96, 104, 110 columns | renderer |
| Spawn HUD warn / error | ratio ≥ 0.66 / ≥ 1 | renderer |
| Busy face rotation | about 2.5 s | docs |

All [obs] from the files named in 3.1–3.26.

### 3.31 Per-overlay key summary

| Overlay | Keys |
|---|---|
| Completion list | Up/Down cycle, Tab apply, Enter apply-or-submit, typing refines |
| Approval | Up/Down, Enter, 1..N, Esc deny, Ctrl+C deny; scroll keys pass through |
| Clarify | Tab/Shift+Tab question, Up/Down, Enter lock, digits, Space (multi), "Other" → free text, Esc cancel rest |
| Sudo / secret / vault | type masked, Enter submit, Esc or Ctrl+C send empty |
| Confirm | Enter/y confirm, Esc/n cancel (exact keys [unk]) |
| Pager | Up/k, Down/j, PgUp/b, PgDn/Space/Enter, g/G, Esc/q/Ctrl+C |
| Model picker | type filter, Up/Down, Enter next stage, Esc clear filter/back, Ctrl+G persist scope |
| Session switcher | Up/Down, Enter, Tab (model for new), Ctrl+N, Ctrl+R, Ctrl+D, d d, Esc, click |
| Agents monitor | Up/Down, Enter, t, g/G, e, x, X, Esc |
| Widget app | the active app owns every key while open |

[obs] except where marked.

---

## 4. File map (what to read in the Read step)

| Sub-area | Reference path | Lines | Read what | Docs / tests to read with it |
|---|---|---|---|---|
| Overview / hotkeys / events (partly stale) | `ui-tui/README.md` | 515 | all | `website/docs/user-guide/tui.md` (318) |
| Canonical wire notes | `ui-tui/CANONICAL_GATEWAY.md` | 110 | all | — |
| Backend rules | `tui_gateway/AGENTS.md` | 207 | all | — |
| Launcher | `<cli-pkg>/main_tui_launch.py` | 624 | `_launch_tui`, `_resolve_use_tui`, `_make_tui_argv` | — |
| Bootstrap helper | `ui-tui/scripts/gateway_bootstrap.py` | 122 | all | `ui-tui/src/localGatewayBootstrap.ts` (44) |
| Renderer entry | `ui-tui/src/entry.tsx` | 188 | all | `__tests__/gracefulExit`, `terminalModes` |
| Gateway client | `ui-tui/src/gatewayClient.ts` | 974 | all | `__tests__/gatewayClient.test.ts` (1,027), `gatewayClientHeartbeatLiveness` |
| Canonical adapter | `ui-tui/src/canonicalGateway.ts` | 181 | all | `__tests__/canonicalGateway` |
| Shared channel | `apps/shared/src/json-rpc-channel.ts` | 613 | request/timeout/heartbeat/server-request parts | `apps/shared/src/reconnect-backoff.ts` (58) |
| Event mapping | `ui-tui/src/app/createGatewayEventHandler.ts` | 1,824 | the switch (bottom ~750 lines) and `handleReady` | `__tests__/createGatewayEventHandler.test.ts` (2,421) |
| Server-request cards | `ui-tui/src/app/createServerRequestHandler.ts` | 134 | all | `gatewayCanonicalEvents.ts` (158) |
| Turn lifecycle | `ui-tui/src/app/turnController.ts` | 1,188 | interrupt, delta, complete, segments | `turnStore.ts` (85), `__tests__/turnController*` |
| Main composition | `ui-tui/src/app/useMainApp.ts` | 1,647 | gateway exit/recovery effect, slash wiring | `gatewayRecovery.ts` (35) |
| Key routing | `ui-tui/src/app/useInputHandlers.ts` | 878 | all | `__tests__/useInputHandlers` |
| Submission | `ui-tui/src/app/useSubmission.ts` | 581 | busy modes, shell, interpolation, double-Enter | `submissionCore.ts` (472), `hooks/useQueue.ts` (509) |
| Composer state | `ui-tui/src/app/useComposerState.ts` | 636 | paste/attach/editor | `lib/editor.ts` (70), `lib/clipboard.ts` (202), `lib/osc52.ts` (76) |
| Session lifecycle | `ui-tui/src/app/useSessionLifecycle.ts` | 815 | create/resume/close | `__tests__/useSessionLifecycle*` |
| Config sync | `ui-tui/src/app/useConfigSync.ts` | 449 | keys and poll | `__tests__/useConfigSync.test.ts` (551) |
| Slash dispatch | `ui-tui/src/app/createSlashHandler.ts` | 276 | all | `slash/registry.ts` (26), `slash/fuzzyScore.ts` (75), `__tests__/createSlashHandler.test.ts` (1,302) |
| Local slash commands | `ui-tui/src/app/slash/commands/{core,session,ops}.ts` | 833 / 876 / 880 | names, args, RPCs used | `website/docs/reference/slash-commands.md` (340) |
| Command registry (names) | `<cli-pkg>/commands.py` | 568 | list only (owned by 04) | `website/docs/user-guide/cli.md` (625) |
| Completion | `ui-tui/src/hooks/useCompletion.ts` | 183 | all | `tui_gateway/methods_complete.py` (409), `command_discovery.py` (302) |
| History | `ui-tui/src/lib/history.ts` | 82 | all | — |
| Text editor | `ui-tui/src/components/textInput.tsx` | 1,896 | key handling, paste, undo | `__tests__/textInput*` (≈15 files) |
| Layout | `ui-tui/src/components/appLayout.tsx` | 671 | Shell/Transcript/Prompt/Composer panes | `appOverlays.tsx` (471) |
| Status rule | `ui-tui/src/components/appChrome.tsx` | 1,030 | `statusBarSegments`, status rule | `__tests__/appChromeStatusRule` (482) |
| Prompts | `ui-tui/src/components/prompts.tsx` | 468 | approval/clarify/confirm | `maskedPrompt.tsx` (153), `__tests__/approvalAction` |
| Messages | `ui-tui/src/components/messageLine.tsx` | 356 | row kinds | `markdown.tsx` (1,166), `streamingMarkdown.tsx` (166), `streamingAssistant.tsx` (120) |
| Tool/reasoning rows | `ui-tui/src/components/thinking.tsx` | 1,277 | `ToolTrail`, `Thinking` | `lib/text.ts` (427), `useLongRunToolCharms.ts` (69) |
| Model picker | `ui-tui/src/components/modelPicker.tsx` | 852 | stages and keys | `__tests__/modelPicker*` |
| Session switcher | `ui-tui/src/components/activeSessionSwitcher.tsx` | 974 | rows, keys, load | `__tests__/activeSessionSwitcher` |
| Theme | `ui-tui/src/theme.ts` | 971 | model, detection order | `branding.tsx` (583), `__tests__/theme.test.ts` (637) |
| Env/limits/timing | `ui-tui/src/config/{env,limits,timing}.ts` | 81 / 26 / 20 | all | — |
| Terminal quirks | `ui-tui/src/lib/{terminalModes,resizeCoalescer,platform}.ts` | 118 / 56 / 417 | all | `<ink-fork>/src/ink/terminal.ts` (447) |
| Forked renderer | `<ink-fork>/src/ink/*` | 24,258 non-test | skim only: `ink.tsx` (2,850) alt-screen/sync/keys, `parse-keypress.ts` (922), `selection.ts` (1,143) | — |
| Backend stdio loop | `tui_gateway/entry.py` | 395 | `_serve`, `main` | `transport.py` (359) |
| Backend facade | `tui_gateway/server.py` | 2,185 | `_emit`/write_json, `_ask`, approval request, slash worker class | `rpc_dispatch.py` (94) |
| Server requests | `tui_gateway/server_requests.py` | 394 | all | `contracts/server_requests.py` (379) |
| Replay ring | `tui_gateway/event_replay.py` | 178 | all | — |
| Agent callbacks | `tui_gateway/agent_callbacks.py` | 628 | `_agent_cbs`, `_wire_callbacks` | — |
| Tool events | `tui_gateway/tool_progress.py` | 516 | caps, start/complete payloads | — |
| Turn runner | `tui_gateway/prompt_turn.py` | 1,239 | emit sequence only (agent loop is 01) | `methods_prompt.py` (1,465) |
| Interrupt/steer | `tui_gateway/methods_session_interrupt.py` | 181 | all | — |
| Sessions | `tui_gateway/methods_session.py` | 2,357 | create/resume/list handlers | `contracts/sessions.py` (780), `session_lifecycle.py` (1,101) |
| Contracts | `tui_gateway/contracts/{registry,events,common}.py` | 199 / 852 / 270 | event payloads, SessionLiveInfo | — |
| Shell exec etc. | `tui_gateway/methods_tools.py` | 1,883 | `shell.exec` only | — |
| Slash worker | `tui_gateway/slash_worker.py` | 220 | protocol, watchdog | `methods_slash.py` (520) |

Giant files:
- `<ink-fork>/src/ink/ink.tsx` [2,850]: only the alternate-screen enter/exit, synchronized-output gating, extended-key enable/disable and editor-suspend parts matter; find by searching for `1049`, `2026`, `kitty`. Do not port the fork; use upstream Ink and add what is missing.
- `<ink-fork>/src/native-ts/yoga-layout/index.ts` [2,393]: a TS port of the Yoga layout engine; irrelevant if upstream Ink + yoga is used.
- `<ink-fork>/src/ink/render-node-to-output.ts` [1,888] and `screen.ts` [1,591]: diff renderer internals; skip.
- `tui_gateway/methods_session.py` [2,357]: only `session.create`, `session.resume`, `session.list`, `session.close` handlers matter for v1.
- `tui_gateway/server.py` [2,185]: `_emit`/write_json (~line 700), `_ask` (~1092), approval emission (~835), slash worker class (~255); the rest is desktop plumbing.
- `ui-tui/src/components/textInput.tsx` [1,896]: key handling is the core; fast-echo and right-click helpers are optional.
- `ui-tui/src/app/createGatewayEventHandler.ts` [1,824]: the switch at the end; the first half is colour probing, notices, voice and billing helpers.
- `tui_gateway/methods_tools.py` [1,883]: only `shell.exec` (near the end).

## 5. Minimum viable subset

v1 needs:
1. Launcher: `indus --tui` with the TTY gate, cwd pinning, resume id, one-shot query, heap flag, prebuilt-bundle resolution, active-session file and exit summary, exit code 42 → update.
2. Transport: renderer spawns the Python backend as a child over NDJSON JSON-RPC on stdio (mode A), stderr into a 200-line log ring, protocol-noise handling, startup timeout, crash recovery budget (3 per 60 s) resuming the same stored session. Keep a transport interface so WebSocket attach (mode B) can follow.
3. Wire: declared contracts with generated TS types; methods `session.create/resume/list/close/history/title/undo/compress/branch`, `prompt.submit`, `session.interrupt/steer`, `slash.exec`, `command.dispatch`, `commands.catalog`, `complete.slash`, `complete.path`, `config.get/set`, `model.options`, `shell.exec`, `paste.collapse`, `image.attach`, `client.capabilities`; server requests `approval`, `clarify` (+ `clarify.lock`), `sudo`, `secret`; events listed in 3.5 minus vendor/voice/moa/connection.
4. Layout: transcript with banner + session panel, live streaming row, tool trail, status rule with width breakpoints, composer with completion list, prompt cards, pager.
5. Composer: multiline editing, history file, completion, paste collapse, external editor, undo/redo, `!cmd`, `{!cmd}`, double-Esc discard.
6. Busy handling: queue (renderer-side FIFO) and steer; interrupt semantics of 3.16 including the sealed-partial fix-up.
7. Model picker (provider → model → reasoning), session switcher (new/live/stored rows), theming with skin payload and light/dark detection, config sync poll, resize coalescing, terminal-mode reset, dead-terminal exit.

Can wait (and why): canonical shared-daemon wire, tickets, generation fences and durable server FIFO (large and coupled to the daemon in another area); offline input journal; agents monitor and dock (needs delegation, area 01/02); goal/loop bars; voice/wake; pets, journey, widgets SDK, FPS/perf panes; billing/subscription/top-up (vendor-specific); connectors/vault/plugins/skills hubs; i18n catalogues beyond English; inline/native/Termux modes; mixture-of-agents rendering; LaTeX-to-Unicode.

## 6. Hard parts / traps

1. **R05.1 Docs describe a transport the code no longer uses.** User docs and the renderer README say the TUI spawns its own stdio backend; the renderer at the pinned commit only attaches over WebSocket (explicit URL or bootstrapped shared daemon). Building from docs gives a different product than building from code. Decide the v1 transport explicitly (recommend stdio child) and write it down; do not mix the two state models. [obs]
2. **R05.2 Events arrive before the UI subscribes.** The backend's first frame and any in-flight server request can arrive before the React tree mounts. Buffer events (cap 2,000) and server requests, drain them in order on first subscribe, and never reset the "subscribed" flag on reconnect (that stranded every later event). [obs comments citing issue numbers]
3. **R05.3 Server requests must not stall the agent.** If the renderer cannot answer a request type it must reply `-32601` at once; if a client never declared it answers server requests, the backend must not send them; withdrawn requests must close cards via one cancel event; still-open requests must be re-delivered after reconnect without re-ringing the bell. Otherwise the agent waits out 120–300 s deadlines with no visible prompt. [obs `server_requests.py`]
4. **R05.4 Interrupt races.** The agent keeps streaming after Ctrl+C until it notices; the renderer must seal the partial reply immediately, ignore later deltas, then reconcile when the interrupted completion carries a longer persisted partial; with queued input it must stay busy until the real settle or the queued message duplicates and the stale "[interrupted]" text leaks. Canonical: an interrupt reply for an already-settled or different generation is a no-op, and completion may win the race. [obs `turnController.ts`]
5. **R05.5 Ctrl+C priority.** Clear draft beats interrupt beats exit; Ctrl+D exits only with an empty composer including tokens; double-Esc interrupt is disabled while a card is open but draft discard is not. Getting this order wrong loses drafts or kills turns. [obs, issue refs in comments]
6. **R05.6 Never replace the stream buffer with a rendered fragment.** The optional `rendered` field is an incremental ANSI piece; replacing the accumulated text with it lost prose and overlapped colours. Accumulate raw text; render locally. [obs]
7. **R05.7 Terminal mode leakage.** Every exit path (quit, Ctrl+D, signals, uncaught errors, OOM exit, `process.exit`) must synchronously reset mouse/focus/paste/alt-screen/keyboard modes, and the app must reset them at start in case a previous run was killed. Leaked mouse reporting shows as garbage in the next shell. [obs `entry.tsx`]
8. **R05.8 Dead PTY loop.** When the terminal disappears, every write throws EIO/EPIPE; swallowing them makes a zombie that logs forever while the backend keeps running. Exit after 5 consecutive dead-stream errors and stop the backend. [obs]
9. **R05.9 Heartbeat false positives.** Count any inbound frame as liveness, not only pong replies; a busy socket streaming deltas was declared dead and split sessions. Deadline 45 s, interval 15 s. [obs]
10. **R05.10 Stale async results.** Completion responses, paste/attach results, slash outputs and prompt answers can return after the user switched session, edited the composer or started a newer command. Every async path captures a destination + revision/flight token and drops mismatches. [obs `createSlashHandler.ts`, `useComposerState.ts`]
11. **R05.11 README key tables drift from code.** README says approval quick-picks are o/s/a/d (code uses digits 1..N), Ctrl+L starts a new session (code redraws), resume picker uses 1–9 (current switcher does not). Treat code as the spec and record the choices; write tests from behaviour, not README tables. [obs]
12. **R05.12 Slash fallback hides real errors.** Only "not my command" refusals may fall through from `slash.exec` to `command.dispatch`; a timeout or worker crash must be shown as itself. Also handle the four dispatch result kinds (exec, alias, skill/send, prefill) and show skill bodies by their display text, not the expanded model-facing body. [obs]
13. **R05.13 Path completion on remote backends.** When the terminal backend is not local, listing the host filesystem returns a plausible but wrong tree. Complete through the session's backend, and resolve relative paths against the session cwd, never the renderer's or daemon's cwd. [obs AGENTS.md]
14. **R05.14 Profile scoping in the backend.** A backend serving more than one profile must bind home, secrets and terminal policy together for each RPC and for off-turn teardown; binding only the config path is "half-bound" and leaks the launch profile's credentials. Even a single-profile v1 should route all config/credential reads through one scope helper. [obs AGENTS.md]
15. **R05.15 Error codes are not stable.** The reference reuses numeric codes with different meanings per method. The renderer branches on a few codes (unknown method, unsupported command, not shown). The rebuild must assign stable code meanings and keep a table. [obs]
16. **R05.16 Resize storms.** Reflowing on every resize event remounts every visible row; coalesce with a leading+trailing 32 ms throttle and always do a final reflow. [obs]
17. **R05.17 Synchronized output inside tmux.** tmux forwards but does not implement DEC 2026; using BSU/ESU or DECSTBM fast paths there tears frames. Gate on a known-terminal list and treat tmux as unsupported. [obs]
18. **R05.18 Busy-mode default differs by surface.** The TUI defaults to queue while other surfaces default to interrupt; config sync must apply the TUI default when the key is absent, or users lose drafts to unintended interrupts. [obs]
19. **R05.19 Paste-to-attachment heuristics.** A pasted path may be an image, a dropped file or plain text; the checks are async and must keep the raw text if the user typed meanwhile. Large paste collapse must still send the full text to the model. [obs]
20. **R05.20 Child stdin hijack (stdio mode).** In stdio transport the backend's stdin is the RPC channel; any subprocess that inherits it can consume request bytes. Mark it close-on-exec and spawn tools with explicit stdin. Also handle spurious EOF from a child toggling non-blocking mode on the shared descriptor. [obs `entry.py`]
21. **R05.21 Session create must be idempotent.** A lost create reply followed by a retry creates a duplicate session unless the retry reuses the same request/idempotency key. [obs]
22. **R05.22 Approval command redaction and wrapping.** The command shown for approval must be redacted server-side (credentials leaked otherwise) and wrapped, not clipped, so the full command is reviewable; show up to 10 lines with a "more" count. [obs]

## 7. Open questions

1. Which transport is the shipping default for v1: stdio child (mode A, simpler, documented), or the shared-daemon WebSocket (mode C, current code)? Needed: lead decision; check `08-desktop.md`/gateway area plans for a shared daemon.
2. Does any current consumer still launch `tui_gateway.entry` over stdio, or is it kept only for tests/embedders? Needed: grep of desktop/dashboard launch code (areas 08/web) or REA process listing (needs REA).
3. Upstream Ink vs a custom renderer: are upstream Ink's alt-screen, mouse, selection and synchronized-output capabilities enough, or does v1 need its own diff renderer? Needed: spike against the keybinding and resize table; rendering smoothness needs REA/runtime comparison (needs REA).
4. Real frame timing: how smooth is streaming at 16/80/96 ms batching in common terminals, and what does the reference look like under a 10k-token reply? Needed: runtime capture (needs REA).
5. Exact status-bar text and spacing at each width breakpoint, banner layout, colours of the default dark/light themes as rendered. Needed: screenshots at 60/80/100/120 columns (needs REA).
6. Which slash commands show a destructive confirm card (the env switch exists; the list was not read). Needed: read `core.ts`/`session.ts` for the confirm calls.
7. Approval timeout and "approve all" semantics live in the approval policy module; confirm values for the TUI path. Needed: `02-tools.md` cross-check.
8. Exact WebSocket subprotocol name and ticket format for mode B/C. Needed: read `<cli-pkg>/gateway_client.py`/`gateway_ws_target` if mode C is chosen.
9. What `MAX_HISTORY = 800` caps (transcript rows in memory?) — constant declared, consumer not found in a quick grep. Needed: search the virtual-history code.
10. Whether the faces/verbs/charms/fortunes copy should be re-written for the rebuild brand (it is long UI copy and may not be reused). Needed: product decision; write fresh copy.
11. Bracketed paste and IME behaviour on Windows Terminal and Termux in practice. Needed: runtime check (needs REA).
12. Does the backend still emit `subagent.spawn_requested` on the TUI path (a comment says the delegate callback drops it)? Needed: read the delegate callback in area 01/02.

## 8. What to read later

- Reference docs: `website/docs/user-guide/tui.md` (launch, switcher, status line, details sections, light detection), `website/docs/reference/slash-commands.md`, `website/docs/user-guide/cli.md` (shared keybindings), `website/docs/user-guide/features/skins.md` (skin keys honoured by the TUI), `ui-tui/README.md`, `ui-tui/CANONICAL_GATEWAY.md`, `tui_gateway/AGENTS.md`.
- Reference tests that list the edge cases: `ui-tui/src/__tests__/` — `gatewayClient`, `gatewayClientHeartbeatLiveness`, `gatewayRecovery`, `gatewayStartTimeout`, `createGatewayEventHandler` (+ `.notice`, `.usage`, `.deferredPick`), `createSlashHandler`, `useInputHandlers`, `useSubmission`, `useQueue`, `busyQueueAdmission`, `queueEditKeepsFields`, `approvalAction`, `clarifyTranscript`, `textInput*` (burst input, kill line, word delete, wrap, right-click, return burst, IME), `completionApply`, `useCompletion`, `inlineSlashSkill`, `streamingMarkdown`, `markdown`, `mathUnicode`, `theme`, `themeBoot`, `terminalModes`, `terminalParity`, `terminalSetup`, `scroll`, `virtualHistory*`, `wheelAccel`, `precisionWheel`, `statusRule`, `appChromeStatusRule`, `activeSessionSwitcher`, `modelPicker*`, `replayGapReattach`, `resumeEpochRenderer`. Backend: `tests/tui_gateway/` (325 entries; start with approval cancel on reap, compaction status, change watcher, contracts, slash/complete tests).
- REA Evidence (outside the repo): none yet (REA not connected). Planned bundle `~/rea-lab/evidence/05-tui/` with PTY captures at several widths, a streaming capture, an interrupt capture and an approval capture.
- Related specs: `01-agent-loop.md` (turn emit order, steer/redirect inside the agent, compression status), `02-tools.md` (approval policy, clarify tool, sudo/secret callbacks, todo tool), `04-cli.md` (command registry, classic keybindings, history format parity), `08-desktop.md` (shared backend and contract generation; desktop-only server requests).
