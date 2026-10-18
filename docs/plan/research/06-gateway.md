# 06 — Messaging gateway: behavioural specification (clean-room)

Scope: the single long-running gateway process; every platform adapter (chat, HTTP ingress, relay);
authorization (allow-lists, pairing, admin tiers, bot-loop guard); per-chat session mapping and
cross-platform continuity; inbound intake (text, voice, images, documents, stickers); outbound
formatting, chunking, streaming and retries; the durable delivery ledger and delivery router (including
the delivery targets cron uses); approvals, clarify prompts and slash commands over chat; hooks;
hosted rooms (multi-bot group chats); the local control socket; drain, restart, shutdown and watchdogs;
the browser-control broker; rate limits. Out of scope: the agent loop (`01-agent-loop.md`), tools
(`02-tools.md`), SQLite schema and session-store internals (`03-state.md`), cron scheduling itself
(`07-cron.md`; only its delivery targets are here).
Reference: `<ref-repo>` @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056` (MIT). Shipped build checked: none
(REA not connected in this session; source reading only).
Sources studied: `gateway/` (318 files, 118,869 Python lines), `plugins/platforms/` (27 adapters,
67,630 Python lines), `gateway/AGENTS.md`, `gateway/platforms/ADDING_A_PLATFORM.md`, website docs
`developer-guide/gateway-internals.md`, `developer-guide/relay-connector-contract.md`,
`user-guide/messaging/index.md`, `user-guide/messaging/{telegram,discord,slack,webhooks}.md` (skimmed),
`user-guide/sessions.md` (handoff), `user-guide/features/cron.md` (delivery options),
`user-guide/bot-mode.md` (hosted rooms), the `<cli>/config_defaults.py` gateway block. Test directory
`tests/gateway/` (1,519 files) was listed, not read.

Conventions:
- "Gateway" = the one background process that owns all messaging adapters for a host (or a profile).
- "Adapter" = one platform connection object (inbound normaliser + outbound sender).
- "Profile" = an isolated agent home (config, secrets, memory, sessions). Under "multiplexing" one
  gateway serves several profiles.
- "Session key" = the deterministic routing string for one conversation slot; "session id" = the
  transcript id it currently points at (many keys may point at one id).
- "Turn" = one agent run triggered by one admitted inbound event (or a merged burst).
- "Home channel" = the per-platform default chat for unsolicited output (cron, notices, handoffs).
- `<home>` = the rebuild's per-profile data directory (see section 2 for the reference spelling).
- "MUST" marks an invariant the reference defends in code comments or tests; "#nnnnn" numbers in the
  reference point at past incidents — they are paraphrased here, not cited.

---

## 1. Overview

The gateway is the always-on half of the product. It is one Python asyncio process that (a) connects to
every enabled messaging platform at once, (b) turns each platform's wire event into one normalised
inbound event, (c) authorizes the sender, (d) maps the chat to a durable conversation slot, (e) runs an
agent turn for it (agents run as threads inside the process; only tools spawn children), (f) streams and
delivers the reply with per-platform formatting and size limits, and (g) records every final reply in a
durable ledger so a crash between "reply produced" and "platform acknowledged" never silently loses it.
It also hosts the cron ticker (every 60 s), background-process watchers, kanban watchers, hosted-room
drivers, an OpenAI-compatible HTTP API, a generic webhook receiver, and a local control socket that
other local processes (CLI, desktop app, updater) use to talk to it. [obs]

```
             +-------------------------- gateway process (asyncio loop) ---------------------------+
 platform -> | adapter (plugin) --normalise--> base adapter guard (busy? queue/interrupt)             |
 wire event  |      |                                   |                                            |
             |      | canonicalize identity (which bot received it, which profile runs it)           |
             |      v                                   v                                            |
             |  runner admission: ignored-channel -> restore-queue -> pre-dispatch hook -> authz     |
             |      -> pairing/decline -> estop gate -> pending prompt replies -> busy guard          |
             |      -> slash dispatch | session claim -> media enrichment -> agent turn (thread)      |
             |                                   |                                                   |
             |   stream consumer (edits/drafts) <+  final text -> filters (silence, redaction)        |
             |      |                                     |                                          |
             |      v                                     v                                          |
             |  adapter.send/edit (chunk, format,   delivery ledger: pending->attempting->delivered   |
             |  retry, plain fallback)                   (failed/abandoned; boot sweep redelivers)    |
             |                                                                                       |
             |  side services: cron tick (60 s) | reconnect watcher | housekeeping (60 s heartbeat)   |
             |  control socket (local only) | API server :8642 | webhook :8644 | relay WS (outbound)  |
             +---------------------------------------------------------------------------------------+
```

What the user sees: chat with the agent from any platform; slash commands; inline approve/deny buttons
for dangerous commands; voice notes transcribed; streamed replies on platforms that support editing;
"typing…" while it works; a restart notice and an auto-resume offer after the gateway restarts; a
labelled "recovered reply" when a crash may have produced a duplicate. What the system guarantees:
default-deny authorization; one conversation slot per chat (per user in groups by default); at-least-once
delivery of final replies with ambiguity labelled; never two concurrent agent turns on one slot. [obs]

Size warning: the area is enormous (≈186k Python lines counting adapters). Roughly a third is
multiplexed-profile scoping, desktop/TUI "session authority" plumbing and hosted-room federation,
which a v1 rebuild can defer (section 5). [inf]

## 2. Reference names (rename in rebuild)

| Kind | Names (reference only; they become banned identifiers) |
|---|---|
| Binary / CLI and flags | `hermes gateway` (`run`, `start`, `stop`, `restart`, `status`, `install`, `setup`, `ensure --json --timeout`, `stop --all`, `install --system`, `--force`, `--force-unit-path`, `--no-start-now`), `hermes pairing approve/list/revoke`, `hermes send`, `hermes peer add/list/dm/run/status/stop`, `hermes pause` / `hermes resume`, `hermes -p <profile>`, `hermes serve`, `hermes update` |
| Env vars | `HERMES_HOME`, `HERMES_HOME_MODE`, `HERMES_GATEWAY_LOCK_DIR`, `HERMES_MEDIA_ALLOW_DIRS`, `HERMES_MEDIA_TRUST_RECENT_FILES`, `HERMES_MEDIA_TRUST_RECENT_SECONDS`, `HERMES_MEDIA_DELIVERY_STRICT`, `HERMES_BACKGROUND_NOTIFICATIONS`, `HERMES_RESTART_DRAIN_TIMEOUT`, `HERMES_CRON_DRAIN_TIMEOUT`, `HERMES_GATEWAY_EXTERNAL_SUPERVISOR`, `HERMES_LAUNCHD_LABEL`, `HERMES_GATEWAY_BUSY_INPUT_MODE`, `HERMES_GATEWAY_BUSY_TEXT_MODE`, `HERMES_GATEWAY_BUSY_ACK_ENABLED`, `HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT`, `HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT`, `HERMES_TURN_LEASE_TIMEOUT`, `HERMES_SESSION_*` (KEY, ID, PLATFORM, CHAT_ID, SOURCE, PROFILE, MESSAGE_ID, ASYNC_DELIVERY), `HERMES_TELEGRAM_INIT_TIMEOUT`, `HERMES_DISCORD_LIVENESS_*`, `HERMES_PEER_<NAME>_KEY`, `_HERMES_GATEWAY` (self-restart guard), and ~70 more `HERMES_*` read in `gateway/`. Unbranded but reference-chosen: `GATEWAY_ALLOW_ALL_USERS`, `GATEWAY_ALLOWED_USERS`, `GATEWAY_MULTIPLEX_PROFILES`, `GATEWAY_RELAY_*` (URL, SECRET, ID, PLATFORMS, …), `API_SERVER_KEY/HOST/PORT/CORS_ORIGINS/MODEL_NAME`, `WEBHOOK_ENABLED/PORT/SECRET`, per-platform `<PLATFORM>_ALLOWED_USERS`, `<PLATFORM>_ALLOW_ALL_USERS`, `<PLATFORM>_ALLOW_BOTS`, `<PLATFORM>_HOME_CHANNEL`, `TELEGRAM_GROUP_ALLOWED_USERS/CHATS`, `DISCORD_ALLOWED_ROLES`, `DISCORD_REQUIRE_MENTION`, `DISCORD_FREE_RESPONSE_CHANNELS`, `WHATSAPP_DM_POLICY/GROUP_POLICY` |
| Files and folders | `~/.hermes/` (= `<home>`), `<home>/profiles/<name>/`, `gateway.pid`, `gateway.lock`, `gateway.sock`, `gateway.sock.path`, `gateway_state.json`, `.drain_request.json`, `state/gateway.heartbeat`, `state/gateway.lifecycle.json`, `gateway/restart_loop.json`, `logs/gateway.log`, `logs/gateway_faulthandler.log`, `logs/gateway-shutdown-watchdog.log`, `gateway-exit-diag.log`, `logs/tool_calls.log`, `platforms/pairing/` (legacy `pairing/`), `channel_directory.json`, `sticker_cache.json`, `webhook_subscriptions.json`, `hooks/<name>/HOOK.yaml` + `handler.py`, `cache/{images,audio,videos,documents,screenshots}`, `status_phrases.yaml`, `status_phrases/`, `cron/output/`, `sessions.json`, `state.db`; service names `hermes-gateway`, `hermes-gateway-<hash>`, `ai.hermes.gateway`, `Hermes_Gateway`; `gateway-service/*.vbs`; `profile.yaml` `ui_meta: hermes-bots`; `$XDG_STATE_HOME/hermes/gateway-locks`; Windows pipe `\\.\pipe\hermes-gateway-<hash>`; placeholders used below: `<cli>` = `hermes_cli/`, `<state>` = `hermes_state*.py`, `<constants>` = `hermes_constants.py` |
| HTTP routes / URL schemes | API server: `/v1/chat/completions`, `/v1/responses[/{id}]`, `/v1/models`, `/v1/health`, `/v1/capabilities`, `/v1/runs[/{id}[/events|/approval|/clarify|/steer|/stop|/resolve-unknown]]`, `/v1/skills`, `/v1/toolsets`, `/v1/artifacts/upload`, `/v1/artifacts/download/{id}`, `/v1/browser-control/register`, `/v1/browser-control/ws`, `/v1/room-members/{capabilities,invitations,grants/refresh,grants/revoke}`, `/api/sessions[...]`, `/api/jobs[...]`, `/api/cron/fire`, `/api/model/options`, `/api/platforms/{platform}/events`, `/health`, `/health/detailed`, `/p/{profile}/...`; webhook: `/webhooks/{route}`, `/p/{profile}/webhooks/{route}`, `/health`; relay: `{connector}/relay`, `{connector}/relay/media[/{id}]`; headers `X-Hermes-Session-Id`, `X-Hermes-Session-Key`, `X-Hermes-Token`, `X-Hermes-Gateway-Ticket`, `X-Hermes-Completed/Error/Partial`; WS subprotocols `hermes-browser-control-v1`, `hermes-browser-control-ticket.` |
| RPC methods / events / commands | Control verbs: `identify`, `status`, `session-ticket`, `pause-for-update`, `rescan-profiles`, `migrate-profile-identity`, `unserve-profile`, `serve-profile`, `purge-profile-identity`, `reload-plugins`; hook events `gateway:startup`, `session:start/end/reset`, `agent:start/step/end`, `command:*`; browser frames `browser.controller.command`, `browser.controller.cancel`; relay frames `descriptor`, `inbound`, `interrupt_inbound`, `passthrough_forward`, ops `send/edit/typing/follow_up/send_media/prompt/react/thread_create/thread_rename`; canonical WS methods `groups.*`, `profiles.list`, `session.*`, `prompt.*`, `clarify.*`, `slash.exec`, `command.dispatch`, `config.get/set`, `setup.*`; slash `/login` binding `providers.nous`, model `nous/welcome` |
| Settings and storage keys | `gateway.*` (`multiplex_profiles`, `profile_routes`, `delivery_ledger`, `bot_loop_guard`, `restart_loop_guard`, `respawn_storm`, `loop_watchdog*`, `startup_watchdog*`, `scale_to_zero`, `unmanaged_idle_exit_seconds`, `signal_interrupt_grace_timeout`, `platform_connect_timeout`, `max_inbound_media_bytes`, `trust_env`, `strict`, `media_delivery_allow_dirs`, `trust_recent_files[_seconds]`, `message_timestamps`, `allow_all_users`, `write_sessions_json`, `systemd_watchdog_seconds`, `service_install_choice`, `relay_url`, `api_server.max_concurrent_runs`), `agent.{restart_drain_timeout, restart_after_turn_timeout, cron_drain_timeout, reconnect_attention_after, bot_mode_protocol}`, `display.{busy_input_mode, busy_ack_enabled, busy_text_debounce_seconds, busy_text_hard_cap_seconds, tool_progress, tool_progress_grouping, tool_progress_command, background_process_notifications, status_phrases, ephemeral_system_ttl, suppress_warning_notifications, platforms.<p>.*}`, `approvals.{mode,timeout,…}`, `platforms.<p>.{enabled, token, home_channel, reply_to_mode, gateway_restart_notification, typing_indicator, typing_status_text, channel_overrides, extra.{allow_from, group_allow_from, allow_admin_from, group_allow_admin_from, user_allowed_commands, group_user_allowed_commands, dm_policy, group_policy, unauthorized_dm_behavior, channel_prompts, channel_skill_bindings, group_allowed_chats, text_batch_delay_seconds, text_batch_split_delay_seconds, routes}}`, `onboarding.seen.busy_input_prompt`, `bot_peers`, `session_reset` (legacy) |
| Other (scopes, error tags, IPC channels) | Session-key namespace literal `agent:main` / `agent:main~` / `agent:<profile>`; capability scopes `session:read/control/submit/respond/create`, `http:owner`, `profiles:create`; ticket purposes `native-http`, `native-profile-create`; error tags `stale_generation`, `profile_scope_mismatch`, `unsupported_command`, `revision_conflict`, `admission_conflict`, `*_lock`, `flood_control:<s>`, `send_path_degraded`; "Bot Mode", "Bot Chat" (canonical title), `message_agent` tool, cron delivery target literal `bot-chat` (= `<self-chat>` below), "Hermes Relay", "Nous Portal", toolset names `hermes-<platform>`; i18n catalog keys `gateway.*` |

Third-party protocol names used by this area (keep exact, check against vendor docs):
- Telegram Bot API (via python-telegram-bot): `getUpdates` (long polling), `setWebhook`/`deleteWebhook`
  with the secret-token header, `getMe`, `sendMessage`, `editMessageText`, `sendMessageDraft`,
  `sendChatAction`, `sendPhoto`, `sendDocument`, `sendVoice`, `sendAudio`, `sendVideo`, `sendAnimation`,
  `sendMediaGroup`, `deleteMessage`, `setMessageReaction`, `setMyCommands`, `getFile`, `getChat`,
  `createForumTopic`, `answerCallbackQuery`, inline keyboards / callback queries, `RetryAfter` (flood
  wait), `parse_mode` MarkdownV2, `message_thread_id`, `reply_to_message`, forum topics, BotFather
  "Threaded Mode". Host `api.telegram.org`.
- Discord gateway (discord.py): intents `message_content`, `dm_messages`, `guild_messages`, `members`
  (privileged "Server Members", only when needed), `voice_states`; application (slash) commands, cap 100;
  message components (buttons ≤80-char label, selects ≤25 options, ≤5 rows); embeds (title ≤256);
  threads with `auto_archive_duration` 1440; `allowed_mentions`.
- Slack (slack-bolt, Socket Mode `AsyncSocketModeHandler`, `AsyncApp`): events `message`,
  `app_mention`; Web API `chat.postMessage`, `chat.update`, `chat.postEphemeral`, `chat.startStream`,
  `chat.appendStream`, `chat.stopStream`, `reactions.add`, `reactions.remove`, `files.uploadV2`
  (`files_upload_v2`), `conversations.replies`, `users.info`, `auth.test`,
  `assistant.threads.setStatus`, `assistant.threads.setTitle`; Block Kit (≤50 blocks, section text ≤3000,
  header ≤150, native `markdown` block); mrkdwn; `ts` thread anchors; `bot_message` subtype.
- WhatsApp: Baileys (WhatsApp Web) via a local Node bridge on loopback HTTP (`/health`, `/send-media`);
  JID/LID identifiers. WhatsApp Business Cloud API (Meta Graph API + webhook, `wamid` ids).
- Signal: signal-cli daemon HTTP mode — `GET /api/v1/check`, SSE `/api/v1/events`, JSON-RPC 2.0 at
  `/api/v1/rpc` with methods `send`, `sendTyping`, `sendReaction`, `getAttachment`, `listContacts`,
  `getContact`.
- Others: Matrix (mautrix, `m.thread`, optional E2EE), Mattermost v4 REST + WebSocket, IMAP/SMTP,
  Twilio SMS REST + webhook, DingTalk Stream Mode (dingtalk-stream), Feishu/Lark (lark-oapi WS or webhook),
  WeCom smart-robot WS + callback (AES crypto), Weixin iLink Bot (`getupdates`, `context_token`,
  AES-128-ECB media), BlueBubbles REST + webhook, Photon Spectrum (`spectrum-ts`), QQ Bot API v2,
  Yuanbao WS (HMAC sign token), Microsoft Teams Bot Framework, Microsoft Graph change notifications,
  LINE Messaging API (HMAC-SHA256 signature), Google Chat (HTTP callbacks or Cloud Pub/Sub pull), ntfy
  (HTTP streaming subscribe, POST publish), IRC, SimpleX (local daemon WS), Buzz (Nostr relay, `npub`
  bech32), A2A protocol v1.0, Raft (loopback wake bridge), GitHub `X-Hub-Signature-256`, GitLab
  `X-Gitlab-Token`, OpenAI Chat Completions / Responses wire formats, SSE, systemd `Type=notify`,
  `WatchdogSec`, `RestartPreventExitStatus`, launchd `KeepAlive.SuccessfulExit`, sysexits 75/78, Fly
  Machines local API.

## 3. Behaviour

### 3.1 Process model, startup and state publication

- **Trigger / inputs:** `gateway run` (foreground), a service manager (systemd user/system unit, launchd
  agent, Windows Scheduled Task launching a hidden script), or a client "ensure" call that discovers or
  starts the runtime with a total deadline (default 60 s). [obs]
- **Flow (startup):** [obs]
  1. Take the per-home PID/lock files; detect an old live gateway by PID **plus process start time**
     (never bare PID); a planned takeover sends SIGTERM, waits up to 10 s, then SIGKILL.
  2. Under multiplexing, take a host-wide lock (one gateway and one control-plane server per OS user)
     and publish a rendezvous record (PID, home, served profiles). A second `run` for a served profile
     attaches to the owner (exit 0, or 75 under a supervisor so the unit is retried, not parked) — but
     only after a live `identify` answer; a record without a live answer is a transient refusal (75).
  3. Load config raw from the profile's YAML (the gateway does NOT apply the CLI's defaults dict, so a
     key present only in CLI defaults behaves differently — a known divergence). Bridge selected
     config keys into env for subsystems that read env.
  4. Start the control socket (startup fails if it cannot start).
  5. Build adapters from the platform registry (deferred plugin loading: platform SDKs import only when
     the gateway needs them). Connect each with a timeout (default 30 s; Telegram 180 s overall, 45 s
     initial). Failed retryable platforms go to the reconnect queue; non-retryable lock conflicts at
     startup are fatal for that platform.
  6. If nothing connected and a fatal config error occurred, exit 78 so supervisors stop restarting.
  7. Startup restore: inbound events that arrive while restore runs are queued and replayed after it.
     Recover never-started durable admissions; mark started-but-unfinished ones "unknown" (they pause
     their session FIFO, see 3.22); sweep the delivery ledger; schedule auto-resume for sessions flagged
     restart-interrupted; send restart notifications to home channels.
  8. Start background loops: cron tick, housekeeping (60 s, also refreshes the state file as a
     heartbeat), reconnect watcher, channel directory refresh (5 min), handoff watcher, kanban
     watchers, background-process watcher drain, loop watchdog thread.
- **States:** `gateway_state` ∈ `starting` → `running` → `draining` → `stopped`; also `startup_failed`,
  `degraded` (watchdog exit), `stopping`. Written to a JSON runtime-status file with `updated_at`, PID,
  code version/sha (so updaters can verify the fleet), per-platform status (`platform_state`:
  connected/retrying/fatal/paused, `needs_attention`, `retrying_since`), `exit_reason`. [obs]
- **Data shapes:** `RuntimeStatus { gateway_state, updated_at, pid, start_time, code_sha, code_version,
  exit_reason?, platforms: { <key>: { platform_state, needs_attention?, error_code?, error?,
  retrying_since? } } }` [inf from field names]
- **Limits:** startup watchdog 300 s (on by default); restart-loop guard 3 restarts per 60 s with gaps
  chaining up to 300 s (stores ≤50 boots; tripped → skip auto-resume; I/O failure fails open);
  respawn-storm guard 5 starts per 120 s; agent cache max 128 agents, idle TTL 3600 s; housekeeping
  pool 4 workers; shared gateway thread pool 10 workers. [obs]
- **Errors:** an unclean previous death is detected by a lifecycle sentinel left in phase "running" and
  appended to an exit-diagnostics log. SIGUSR2 dumps all thread stacks to a faulthandler log without
  stopping. [obs]
- **Desktop relationship:** the gateway is spawned detached and survives the desktop app quitting; the
  desktop's control-plane server dies with the app. Do not re-parent the gateway. [obs]

### 3.2 Adapter contract and platform registry

- **Base contract** (every adapter): `connect(is_reconnect)` / `disconnect()`; `send(chat_id, content,
  reply_to?, metadata?) -> SendResult`; optional `edit_message`, `delete_message`, `send_typing` /
  `stop_typing`, `send_image`, `send_image_file`, `send_multiple_images`, `send_animation`, `send_voice`,
  `send_video`, `send_document`, `send_exec_approval` (buttons), `send_clarify`, `send_slash_confirm`,
  `send_private_notice`, `send_draft` (native draft streaming), `create_handoff_thread`,
  `get_chat_info -> {name, type}`, `format_message`, `play_tts` and streaming-TTS hooks,
  `on_processing_start/complete` (reaction-style progress). Inbound: normalise to an event and call the
  base `handle_message`. [obs]
- **Capability flags/attributes per adapter:** max message length and a length function (code points or
  UTF-16), `splits_long_messages`, `supports_code_blocks`, `supports_draft_streaming`,
  `draft_stream_is_message`, `enforces_own_access_policy`, `authorization_is_upstream`,
  `supports_async_delivery` (push-capable), `streaming_overflow_limit`. [obs]
- **Registry entry shape** (one per platform, contributed by plugin manifest + code):
  `PlatformEntry { name, label, adapter_factory, check_fn, validate_config?, ensure_deps_fn?,
  is_connected?, required_env[], install_hint, setup_fn?, source: builtin|plugin, plugin_name,
  allowed_users_env, allow_all_env, allow_bots_env?, group_allowed_chats_env?, own_policy_env?,
  allowlist_suppresses_pairing?, trusted_inbound?, max_message_length (0 = none), pii_safe, emoji,
  allow_update_command, platform_hint (system-prompt text), env_enablement_fn?, apply_yaml_config_fn?,
  cron_deliver_env_var, parse_target_ref_fn?, validate_target_ref_fn? }` [obs]
- **Enablement:** a platform starts when credentials are present in env (e.g. bot token) unless the
  config explicitly says `enabled: false`; explicit false always wins and logs one warning per platform
  whose credentials are being ignored. Missing `enabled` key keeps env-only enablement. [obs]
- **Per-platform config:** `PlatformConfig { enabled, token?, api_key?, home_channel?:
  HomeChannel{platform, chat_id, name?, thread_id?}, reply_to_mode: off|first|all (default first),
  gateway_restart_notification (default true), typing_indicator (default true), typing_status_text?,
  channel_overrides: { <channel id>: { model?, provider?, system_prompt? } }, extra: map }` [obs]
- **Token locks:** any adapter using a unique credential acquires a machine-local lock keyed by
  `scope + hash(identity)` in a cross-profile lock directory on connect and releases on disconnect, so
  two profiles (or two installs) cannot poll the same bot token. A conflict is reported as a per-scope lock error with
  `retryable=true` mid-run, but a live foreign holder at startup is a config conflict (fatal for that
  platform). [obs]
- **Proxy:** adapters honour standard proxy env vars and the macOS system proxy (cached 60 s) unless
  `gateway.trust_env: false`; per-platform proxy vars always win. [obs]
- **Toolset per platform:** each platform selects a base toolset (messaging-oriented); the API server
  drops `clarify` and text-to-speech because nobody interactive is present. [obs]
- **Mid-run plugin load:** handlers of a plugin loaded after connect are re-wired idempotently (keyed by
  plugin + qualified name) without restart; tools/prompt changes wait for the next session. [obs]

### 3.3 Platform catalogue (all adapters present)

All chat adapters live as plugins (`plugins/platforms/<name>/`, mostly `adapter.py` + manifest); HTTP
ingress adapters and the relay live in `gateway/`. Char caps are the adapter's chunk limit. [obs]

| Platform | Transport / library | Cap | Notes |
|---|---|---|---|
| Telegram | Bot API long polling (default) or webhook (secret required) via python-telegram-bot | 4096 UTF-16 units | MarkdownV2 with plain fallback; forum topics + DM topics; inline keyboards; native draft streaming; reactions; set commands; direct-IP fallback transport; per-chat outbound budget 1.0 s; flood inline wait cap 5 s; max 32 concurrent updates; update receipts kept 24 h (dedupe ≤4096 seen ids) |
| Discord | discord.py gateway | 2000 | slash commands (≤100, sync paced 4.5 s/mutation), components, auto-threads, voice channels (join/leave, mixer), role allowlist, mention gating + free-response channels, history backfill on mention |
| Slack | slack-bolt Socket Mode | 39000 | Block Kit, native streaming (`chat.*Stream`), assistant status as typing, threads by `ts`, ignored channels, workspace scope id in session key, ephemeral messages, DM cache ≤5000 |
| WhatsApp (bridge) | Local Node Baileys bridge over loopback HTTP (port 3000; secondaries 3001–3999) | — | own DM/group policy (open, allowlist, disabled, pairing); per-profile bridge with pidfile ownership |
| WhatsApp Cloud | Meta Graph API + webhook (port 8090, body ≤3 MiB) | — | wamid dedupe cache 5000 |
| Signal | signal-cli HTTP daemon: SSE in, JSON-RPC out | 8000 | attachments ≤100 MiB, ≤32 per message; SSE retry 2→60 s; health check 30 s, stale 120 s; server token-bucket handling |
| Matrix | mautrix, optional E2EE | 15000 (45000 bytes / 3) | ignores events older than 5 s before startup; `m.thread` |
| Mattermost | v4 REST + WebSocket | 4000 | |
| Email | IMAP poll + SMTP | 50000 | unknown senders ignored unless pairing opted in |
| SMS | Twilio REST + webhook (port 8080, body ≤64 KiB) | 1600 | markdown stripped |
| DingTalk | Stream Mode SDK | 20000 | |
| Feishu/Lark | lark-oapi WS or webhook | 8000 | |
| WeCom / WeCom Callback | smart-robot WS / HTTP callback with AES | 4000 | two platforms from one plugin |
| Weixin | iLink Bot long poll | 2000 | echoes per-peer context token |
| BlueBubbles | REST + webhook | none | iMessage via macOS server |
| Photon | Spectrum managed platform | 8000 | iMessage and others |
| QQ Bot | API v2 WS + REST | 4000 | |
| Yuanbao | WS with HMAC sign token | — | |
| Microsoft Teams | Bot Framework | 28000 | |
| Microsoft Graph webhook | change-notification listener | — | core HTTP adapter |
| LINE | webhook with HMAC-SHA256 | — | |
| Google Chat | HTTP callbacks or Pub/Sub pull | — | ParkedRoute 503 retry semantics |
| ntfy | HTTP streaming subscribe / POST | 4096 | reports every topic as "dm" (broadcast!) |
| IRC | raw socket | 450 | canonical scoped-lock example |
| SimpleX | local daemon WS | 8000 | |
| Buzz | Nostr community relay | — | `npub` allowlist entries decoded from bech32 |
| A2A | agent-to-agent protocol v1.0, both directions | — | peer reported as "dm" |
| Raft | loopback wake bridge | — | wake-only; agent uses an external CLI for I/O |
| API server | aiohttp, OpenAI-compatible, 127.0.0.1:8642 | — | see 3.25 |
| Webhook | aiohttp, port 8644 | — | see 3.25 |
| Relay | outbound WS to an external connector | from descriptor (0 → 4096) | fronts Discord/Telegram/Slack/WhatsApp; experimental; see 3.25 |
| Home Assistant | external catalog plugin (not in tree) | — | listed in docs only [obs] |

The platform enum has 22 fixed members (local, telegram, discord, whatsapp, whatsapp_cloud, slack,
signal, mattermost, matrix, email, sms, dingtalk, api_server, webhook, msgraph_webhook, feishu, wecom,
wecom_callback, weixin, bluebubbles, qqbot, yuanbao, relay); other plugin platforms become dynamic
members on first lookup (manifest name aliases resolve to directory names). [obs]

### 3.4 Inbound normalisation

- **Event shape:** `InboundEvent { text, message_type: text|location|photo|video|audio|voice|document|
  sticker|command, user_id?, user_name?, source: Source, raw?, message_id?, platform_update_id?,
  media_urls[], media_types[], media_text_inlined[], reply_to_message_id?, reply_to_text?,
  reply_to_author_id?, reply_to_author_name?, reply_to_is_own_message, prompt_response?, auto_skill?,
  channel_prompt?, channel_context?, internal: bool, metadata, timestamp, allow_gateway_control,
  reply_expected?: bool, inherit_channel_pin }` plus process-local receipt `accepted`. [obs]
- **Source shape:** `Source { platform, chat_id, chat_name?, chat_type: dm|group|channel|thread|forum,
  user_id?, user_name?, thread_id?, chat_topic?, user_id_alt? (Signal UUID, Feishu union id),
  chat_id_alt?, is_bot, scope_id? (Slack workspace), guild_id?, parent_chat_id?, message_id?,
  role_authorized, profile?, profile_route_rejected, auto_thread_created, prospective_thread_id?,
  delivered_via_upstream_relay, identity?: RoutingIdentity{transport_profile, runtime_profile, …} }`
  [obs]
- **Command detection:** text (after stripping leading attachment-reference lines of the form
  `@image:`/`@file:`/`@url:`) starting with `/` and `allow_gateway_control` true; command name is
  lower-cased, `@botname` suffix removed, names containing `/` rejected; args have em/en dashes
  normalised to `--`/`-` (phone keyboards). [obs]
- **Identity first:** every ingress path canonicalises "which bot received it / which profile runs it"
  once, before any key derivation; an unresolved identity under multiplexing is dropped with one
  warning (a parked target also gets an in-channel "please resend" when the platform already
  acknowledged the update). [obs]
- **Inbound text batching:** chat clients split long pastes into several messages; adapters that opt in
  merge chunks per session key and dispatch after a quiet window: default 0.3 s (ceiling 2.0 s), and
  1.0 s (ceiling 4.0 s) when the last chunk is near the platform split threshold (default 4000). [obs]
- **Dedupe:** per-platform (Telegram update receipts, WhatsApp Cloud wamid cache, webhook delivery ids
  1 h, Slack message+app_mention share a ts and are deduplicated). [obs]
- **Channel extras:** per-channel ephemeral prompt (exact id, then parent id) and per-channel
  auto-loaded skills (exact id first, then parent; de-duplicated). [obs]
- **Reply context:** when a user replies to a message, the replied-to text (from the platform, or from a
  local "rich sent store" index of what the bot sent, since Telegram does not echo rich content and
  WhatsApp quotes only carry ids) is prepended as context; replies to the bot's own message are flagged.
  [obs]

### 3.5 Authorization, allow-lists, pairing, admin tiers

- **Order of checks (first grant wins)** [obs]:
  1. Local desktop/TUI sources: their own verdict.
  2. Webhook platform and `trusted_inbound` platforms: always allowed (HMAC or upstream auth).
  3. Chat-scoped grants (work with no user id): relay-delivered events or adapters declaring upstream
     authorization; group chat-id allowlist (env, then config `group_allowed_chats`); bot senders
     admitted by the platform's allow-bots setting (`mentions` or `all`).
  4. No user id → deny.
  5. Per-platform allow-all flag.
  6. Adapter-verified role grant (Discord role allowlist).
  7. Pairing store approval (a union with allowlists).
  8. If no env allowlist is configured at all: adapter's own policy (if it enforces one), then adapter
     config allowlist (`allow_from` / `group_allow_from`), then the global allow-all flag.
  9. Otherwise: group chat allowlist; legacy Telegram chat ids misplaced in the group-user list
     (values starting with `-`, warned once); union of platform, group-user and global allowlists plus
     adapter-resolved ids (usernames resolved to ids); `*` means everyone.
  10. Default deny.
  Then, for bot-authored messages, the bot-loop guard can still refuse (3.6).
- **Identity normalisation:** WhatsApp JID/LID aliases canonicalised; Buzz `npub` decoded to hex;
  Signal alt UUID honoured. [obs]
- **Secret scope rule:** under multiplexing every allowlist/credential read goes through one scoped
  reader that returns the default when the profile's scope misses — never the process environment
  (which holds the launch profile's values). [obs]
- **Unauthorized DM behaviour** (`pair` | `ignore` | `decline`), resolved in order: explicit
  per-platform setting; email → ignore; a global setting, but only when it differs from the default;
  the adapter's own DM policy, where a pairing policy maps to pair and an allowlist or disabled policy
  maps to ignore; if any relevant allowlist is configured → ignore; else pair.
  Groups and messages with no user id are always silently ignored; bots are never offered pairing.
  `ignore` logs the sender id with the fix and notifies the owner's home channel once per sender.
  `decline` sends one short refusal per sender per 24 h (stamp written before send). [obs]
- **Pairing** [obs]:
  - Code: 8 chars from a 32-char alphabet excluding 0/O/1/I, cryptographic randomness.
  - Only a salted SHA-256 hash (16-byte salt) is stored, keyed by a random 16-hex request id.
  - TTL 3600 s; ≤3 pending per platform; one request per user per 600 s; 5 failed approvals → platform
    lockout 3600 s; successful approval resets the failure streak.
  - Approval input has all whitespace removed and is upper-cased (chat clients insert spacing);
    constant-time compare; lockout is checked before lookup.
  - Admin surfaces approve by request id (never seeing the code), not counted toward lockout.
  - Approval also writes the user into the platform's configured allowlist (revoke removes it, and
    live adapters' in-memory lists are purged) so the operator's list remains the visible truth.
  - Files 0600; codes never logged; directory resolved per call (not at import) so CLI and gateway
    agree.
  - Rate-limited users get a "try later" reply once, then silence.
- **Slash-command admin tiers** (per platform, per scope DM vs group): `allow_admin_from` lists admins
  (all commands incl. plugin ones); `user_allowed_commands` lists what non-admins may run; floor for
  non-admins is `help` and `whoami`. No admin list for a scope → tiers disabled there (everyone full).
  Plain chat is never gated by tiers. `/help` and `/commands` show only runnable commands to
  non-admins. [obs]
- **Mention gating** (Telegram, Discord, Slack, Matrix, Mattermost, Feishu, DingTalk, WhatsApp, Signal,
  Teams, BlueBubbles, Photon, Buzz): in groups the bot answers only when mentioned unless the channel is
  "free-response"; Discord threads optionally require mention; Telegram can observe unmentioned group
  traffic as context. [obs]

### 3.6 Bot-loop guard and rate limits

- **Bot-loop guard:** sliding window per conversation `(profile, platform, chat_id)` — not per sender
  pair, so N bots share one budget. Defaults: enabled, 20 bot-authored messages per 300 s; on the 21st
  the chat enters a 600 s cooldown (window cleared); `admit` returns `ok|tripped|cooldown|disabled`;
  `blocked` is read-only and may be asked repeatedly; admission is counted once per inbound message.
  Idle conversations are swept at most once per window. Settings re-read on every call (no restart).
  Non-integer or non-positive values fall back to defaults. [obs]
- **Concurrency:** optional `max_concurrent_sessions` (positive int) caps simultaneously active
  sessions; the new session gets a refusal message. API server `max_concurrent_runs` default 10. [obs]
- **Per-session claim:** a sentinel is installed before the first await so a second message cannot
  start a duplicate agent; a per-session-id "turn lease" serialises load-history → run → flush even when
  several routing keys point at one transcript; a waiter that times out fails closed and the user is
  asked to resend. [obs]
- **Outbound pacing:** Telegram per-chat shared slot 1.0 s (sends wait, interim edits are skipped, final
  edit never gated); flood penalties >5 s (Telegram) / >60 s (base) are not slept inline but returned as
  a typed flood failure carrying the wait, for the ledger to retry. Stream editing backs off adaptively (max 30 s) and
  disables edits after 3 consecutive flood failures. [obs]
- **Inbound limits:** webhook 30 requests/min per route (60 s window) and 1 MiB body; inbound media
  max 128 MiB (`134217728`); API request max 10 MB; Signal send rate-limit retry 1 extra attempt with
  4 s default refill. [obs]

### 3.7 Session mapping

- **Key layout:** `<ns>:<platform>:<chat_type>[:<slack scope id>][:<chat_id>][…]` where the namespace is
  the default literal for the default profile, the profile name otherwise, and a profile literally named
  like the default literal gets a `~` marker (outside the profile-id alphabet) so it can never collide.
  [obs]
- **Rules** [obs]:
  - DM: key on chat id; WhatsApp DM chat ids canonicalised; if no chat id, fall back to sender id, then
    one per platform. In DMs the user part (if any) precedes the thread part.
  - Group: add the participant id when `group_sessions_per_user` (default true) and not in a thread.
    Threads are shared by default; `thread_sessions_per_user` isolates them. In groups the thread part
    precedes the user part.
  - Discord auto-thread continuity: a channel message that will spawn a thread uses the future thread's
    id (`prospective_thread_id`) as its key part, with the chat-type slot set to "thread", so the
    in-thread follow-ups match byte-for-byte. A real thread id always wins.
  - Slack prepends the workspace scope id; Discord guild id is deliberately not in the key.
  - Never hand-build keys; one builder function is the single source of truth.
- **Session store:** a routing index (key → session id + origin + metadata) persisted in SQLite with an
  optional JSON mirror (`write_sessions_json`, default true), plus transcripts (area 03). Entry shape:
  `RouteEntry { session_key, session_id, created_at, updated_at, origin: Source, display_name?,
  platform, chat_type, token counters, estimated_cost_usd, cost_status, last_prompt_tokens,
  was_auto_reset, prev_session_id?, suspended, resume_pending, resume_reason?, active_turn_token?,
  active_turn_started_at?, model_override?, transport_profile? }`. [obs]
- **Continuity:** conversations never reset on idle or daily boundaries (legacy reset settings are
  ignored with a startup/doctor warning; a separate plugin restores them). `/new`/`/reset` start a new
  session; `/resume [name]`, `/sessions [all] [search q]` (cross-origin listing admin-only), `/title`,
  `/branch` (opens a sibling thread on Discord/Telegram/Slack/Matrix; `--here` switches in place),
  `/undo`, `/retry`, `/rollback`. Cached agents may be evicted (TTL 3600 s, LRU 128, memory pressure:
  budget 65 % of RAM floor 512 MB, ≤16 evictions per pass, 8 most recent protected); eviction flushes
  memory providers under the owning profile's scope but does not end the transcript. [obs]
- **Overrides:** `/model` persists per session across restarts (credentials re-resolved, never written);
  `--global` writes config; `--once` reverts after one turn on every exit path. Model precedence:
  session override → channel override → global. Channel `system_prompt` is ephemeral per turn. [obs]
- **Profile routing (multiplex):** routes match conjunctively; highest additive specificity wins
  (user 16, thread 8, chat 4, guild 2, else default); a route applies only to messages received by its
  bot profile; channel routes match threads under them via parent id. [obs]
- **Telegram topic lobby:** in topic mode, messages in the root of a forum get a debounced reminder to use
  a topic instead of starting a turn. [obs]

### 3.8 Cross-platform continuity

- **CLI → platform handoff** [obs]: the CLI marks its session pending with a target platform; a gateway
  watcher claims the row, resolves the platform's live transport (native adapter first; relay only if it
  advertises fronting that platform), requires a home channel (else fails with a "set home first"
  reason), asks the adapter for a fresh thread (Telegram forum/DM topic, Discord thread with 1440-min
  auto-archive, Slack/Matrix seed message as anchor; none for WhatsApp/Signal/SMS), re-binds the
  destination key to the CLI's session id (thread keys omit user id so later replies join), and injects a
  fabricated user message so the agent's first reply in the new place confirms the move. The CLI polls: 60 s unclaimed → "gateway not running",
  then up to 15 min with heartbeats. Thread creation failure falls back to the home channel; send failure
  marks the handoff failed and clears it for retry. Stale running handoffs are reclaimed at startup.
- **Platform → CLI:** `/resume <title>` from the CLI picks up the same transcript. [obs]
- **Mirror records:** when something is sent into a session from elsewhere (the send-message tool, cron
  delivery), a "delivery mirror" record is appended to the target session's transcript so its agent
  knows what was sent; cron deliveries are NOT mirrored into chat sessions except eligible
  "continuable" deliveries, which are appended as labelled user turns at a turn boundary (role
  alternation preserved). [obs]
- **Explicit cross-platform sends:** a `platform:chat[:thread]` target from any context resolves to that
  platform's transport (3.17). [obs]

### 3.9 Message intake: voice, images, documents, stickers

- **Media caching:** adapters download attachments into per-kind cache dirs under `<home>/cache/`
  (images, audio, videos, documents, screenshots) before routing; under multiplexing the files are moved
  into the routed profile's cache and the event (and any text that embedded the path) is repointed. [obs]
- **Voice/audio:** each clip is transcribed with the configured speech-to-text provider; on failure a
  local fallback is tried; empty transcript → an explicit note telling the agent the message was
  inaudible and not to guess (avoids reply-to-nothing loops); success → the transcript is inserted as a
  plain quoted line (a wrapper sentence made models comment on voice mode). If STT is disabled, a note
  with the absolute path and probed duration is inserted. Transcripts may be echoed back to the chat
  before the turn. Clips are memoised so a merged burst is not transcribed twice. [obs]
- **Images:** routing mode decided per turn from the effective model: `native` (image parts sent to the
  model) or `text` (images pre-analysed by the vision tool and descriptions prepended); errors fall back
  to `text`. [obs]
- **Documents:** supported document types get a context note; small text-like files (≤100 KiB for
  several adapters) are injected inline; image documents treated as images. [obs]
- **Stickers (Telegram):** described once by vision and cached by unique file id. [obs]
- **Photo bursts / albums:** merged into one pending event (media lists concatenated, captions merged);
  with text debounce, rapid text follow-ups append instead of replace. [obs]
- **Timestamps:** optional (`message_timestamps.enabled`, default false) human-readable send time
  prefixed to user messages in model context only; always stored as metadata so transcripts stay clean.
  [obs]
- **Context references and sender prefix:** in groups the sender's name may be prefixed; `@file`-style
  references are expanded within the model's context budget. [inf]

### 3.10 Busy sessions: the two guards and busy-input modes

- **Guard 1 (adapter):** if the session key has an active task, the event is stored in a pending slot
  (merging per the rules above) and an interrupt signal is set — except control commands, which are
  dispatched inline. [obs]
- **Guard 2 (runner):** if an agent is running for the key, recognised slash commands go through busy
  dispatch: mid-run variant if the command has one (start, stop, new, queue, steer, egress, goal, loop),
  else the normal handler if its busy policy allows, else a fixed "can't run mid-turn" reply. Commands
  `/stop`, `/new`, `/queue`, `/status`, `/approve`, `/deny` MUST bypass both guards and run inline,
  never via the background processing path (which races session lifecycle). [obs]
- **Plain messages while busy** — mode per profile (`interrupt` default | `queue` | `steer`):
  - interrupt: the active turn is redirected: model generation restarts with already-shown text kept as
    an assistant checkpoint, completed tool results kept, a running tool finishes and the correction
    applies at the next tool-result boundary; a foreground terminal command is moved to background.
  - queue: each follow-up becomes its own next turn in arrival order (only photo bursts merge).
  - steer: injected into the running turn after the next tool call; falls back to queue if the agent
    has not started.
  Merged busy text waits a 0.35 s quiet window, hard cap 1.0 s. An acknowledgement reply (⚡/⏳/⏩ style)
  is sent unless disabled; the first busy ack per install carries a one-time tip. [obs]
- **Steer context:** steers and redirects carry the event's routing identifiers as per-message JSON
  context (hashed when PII redaction is on). [obs]
- **Stale slots:** leaked or reaped running-agent slots are evicted before the busy check; release is
  keyed by run generation so a displaced turn cannot clear its successor. [obs]

### 3.11 Slash commands in chat

- **Resolution:** a shared command registry (also used by the CLI) maps aliases and prefixes to a
  canonical name; the gateway checks it is gateway-known and config-gated, then looks up the handler by
  name from two tables (no if-chains): "plain" commands usable idle or busy — status, context, restart,
  approve, deny, pause, agents, bg, btw, kanban, subgoal, heartbeat, busy, yolo, verbose, footer, help,
  commands, profile, login, update, version, autoprune — and "idle-only" commands — topic, whoami,
  platform, stop, reasoning, memory, skills, fast, approvals, model, codex-runtime, personality,
  suggestions, save, retry, sethome, compress, usage, topup, insights, reload-mcp, reload-skills,
  bundles, debug, title, resume, sessions, branch, rollback, diff, goal, loop, refine, review, voice,
  curator. Canonical special cases handled inline: new/reset, start, egress, learn, plan, init,
  blueprint, undo, queue, steer, moa. [obs]
- **Other dispatch:** `/<skill-name>` rewrites the turn into a skill invocation; bundle commands;
  user-defined quick commands (`exec` type runs a shell command in a sanitized environment, limited to 30 s, output redacted);
  plugin-registered commands; `pre_command` and `command:<name>` hooks; unknown slash → a short hint.
  Destructive commands may require a confirm prompt (buttons or text choices). [obs]
- **Native registration:** Telegram `setMyCommands` (description ≤256), Discord application commands
  (≤100, names/descriptions ≤100), Slack slash commands; Telegram command mentions are rewritten to its
  syntax. [obs]
- **Notable commands:** `/sethome` (home channel), `/platform list|pause|resume <name>` (pausing keeps
  the connection but drops inbound), `/voice on|off|tts|join|leave|status`, `/bg <prompt>` (isolated
  background session, result posted back with success/failure prefix and a task id), `/btw` (side
  question), `/whoami` (scope and tier), `/login` (paired DM only; refuses on platforms whose "dm" is
  really broadcast — ntfy, raft, a2a; binds the whole install's hosted-provider account; runs on a
  private single-worker executor; one attempt per process). [obs]
- **Emergency stop:** a global pause blocks new turns (a paused notice is returned) except allowed
  control commands. [inf from `_hm_estop_gate`]

### 3.12 Approvals, clarify and secret prompts over chat

- **Exec approval:** when a tool flags a dangerous command, the adapter renders a card with header,
  the command (truncated to a budget, escaped), the reason, and a deadline line; actions are four tiers
  `once`, `session`, `always`, `deny`. Platforms with buttons (Telegram inline keyboard, Discord
  components, Slack Block Kit actions, relay `prompt` op) use them; others get a plain-text fallback
  telling the user to reply `/approve` or `/deny`. Bare conversational "yes" MUST never approve. On
  timeout (default `approvals.timeout` 300 s) a notice says the command was not run. Approval text is
  secret-redacted. Wording lives in one shared module so every surface says the same three things
  (what, why, silence = not run). [obs]
- **Mid-stream boundary:** when an approval interrupts a streamed message, the stream is sealed at an
  approval boundary with a placeholder and resumes in a new message. [obs]
- **Clarify:** questions render as numbered choices or native buttons; multi-select accepts numbers
  separated by commas/spaces, option text, or free text; single-select by number, button, text or an
  "Other" free answer. Pending clarify replies are intercepted before normal processing. Choice lists
  are paged. [obs]
- **Sudo password / skill secrets:** not available over messaging chats (a fixed "not supported here"
  hint); only local desktop/TUI viewers can answer masked prompts. [obs]
- **Callback authorization:** button presses are authorized against the same sender rules (Discord
  component/slash auth helpers). [inf]

### 3.13 Outbound formatting and chunking

- **Chunk algorithm (base, used by most adapters):** if the text fits, one chunk. Otherwise repeatedly:
  reserve 10 units for a `(i/n)` indicator plus room for a fence reopen prefix and a fence close; split
  at the last newline in the budget, unless that is before half the budget, then at the last space;
  minimum progress one code point (never hang on tiny limits or wide surrogate pairs); avoid splitting
  inside an inline code span (back off to a space/newline before the unpaired backtick if it is past a
  quarter of the budget); strip leading whitespace from the remainder; if a chunk ends inside a fenced
  block, close the fence and reopen it with the same language tag at the start of the next chunk.
  Multi-chunk output gets ` (i/n)` appended to each chunk. The length function may count UTF-16 units
  (Telegram). [obs]
- **Telegram:** MarkdownV2 with escaping of every special character outside code; tables converted to
  bullet groups (no table syntax); on parse errors retry as plain text with markers stripped; chunk
  indicators placed outside fences. [obs]
- **Slack:** mrkdwn for normal sends but standard markdown for native streams; Block Kit for tables
  (≤100 rows × 20 cols, table text ≤20000 chars); unfurl blocks ≤6000 chars. [obs]
- **SMS:** markdown stripped. **Email:** one message ≤50000 chars. [obs]
- **Threading:** `reply_to_mode` off/first (default)/all decides which chunks reply-thread to the user's
  message; thread/topic ids carried in send metadata. [obs]
- **Media in replies:** the agent embeds `MEDIA:<path>` tags (and image markdown/URLs); the sender
  extracts them, removes them from text, validates each path, and sends native media. Path policy: safe
  roots (gateway caches), optional strict mode (only allowlisted dirs), extra allowed dirs, trust of
  recently created files (default on, 600 s), deny-list of credential and system prefixes. Files living
  in a remote terminal sandbox are fetched into the document cache after screening the remote path
  against the same deny-list before and after resolving symlinks. Media delivery failures produce a
  notice. [obs]
- **Silence tokens:** if the entire final reply (whitespace/case normalised) is one of `[SILENT]`,
  `SILENT`, `NO_REPLY`, `NO REPLY` or the Chinese renderings, nothing is sent but the turn stays in the
  transcript. A human message that expected a reply gets a short notice instead; internal wakes and
  messages the adapter reports as not addressed (Slack) may stay silent. Failed turns are never
  silenced. A separate filter drops cron outputs that are only a silence narration (≤64 chars, e.g.
  "(silent)", 🔇, "…"). [obs]
- **Ephemeral system notices:** certain replies auto-delete after a TTL on platforms with delete
  support (default from `display.ephemeral_system_ttl`; 0 disables). [obs]
- **Secret redaction:** user-facing finals and approval commands are redacted; cron deliveries always
  mask credential shapes even when log redaction is off. [obs]

### 3.14 Streaming, progress and typing

- **Stream consumer:** buffers model deltas and edits one message in place: edit when ≥0.8 s elapsed
  with new text, or ≥24 new code points buffered; a cursor `▉` is shown while streaming; when the text
  outgrows the platform limit it splits (Telegram partial-overflow contract: the result tells the
  consumer which tail is missing). Falls back to one message per segment when editing is unsupported.
  Streaming default per platform: Telegram on, Discord off, Slack off, WeCom on (global default off).
  [obs]
- **Stream-is-the-message adapters (native streams):** four invariants — draft frames are prefix-stable
  (frame N is a prefix of N+1; no per-tick fence closing, cursor or format conversion); the consumer
  declares the final and post-stream augmentations ride that final payload; every non-final send is
  marked interim or the seal logic will seal the stream with interim text (checked at both egress
  doors); a final delivered beside a sealed stream is reconciled by editing the stream message, plain
  send only when nothing is editable. [obs]
- **Tool progress:** `off | new | all | verbose | log`; grouping `accumulate` (one bubble edited) or
  `separate`; `log` writes a redacted rotating audit file (5 MB × 3) instead of chat bubbles. Mobile
  defaults for Telegram: progress off, busy-ack detail off, interim assistant commentary on, long-running
  "working — N min" heartbeat on. Optional cleanup deletes progress bubbles after a successful final
  (only where delete is supported; skipped on failure). [obs]
- **Status phrases:** heartbeat lines drawn from a built-in catalog plus user files (≤80 phrases per
  surface, ≤160 chars each; relative paths only, no `..`); raw tool args are never interpolated. [obs]
- **Typing:** refreshed every 2.0 s while processing (per-platform `typing_indicator`); paused during
  approval waits. [obs]
- **Warning suppression:** `suppress_warning_notifications` (global or per platform, platform wins,
  null inherits) mutes automatic diagnostics only; execution, retries, logs and API failure flags stay
  truthful. [obs]

### 3.15 Send result, retry and error classification

- **Shape:** `SendResult { success, message_id?, error?, raw_response?, retryable, retry_after?,
  continuation_message_ids[] (when split; message_id is the last chunk), error_kind?:
  too_long|bad_format|forbidden|not_found|rate_limited|transient|unknown }`. [obs]
- **Classifier:** ordered substring table over error text + exception class name; first match wins;
  unknown is never treated as benign. Plain read/write timeouts are NOT retryable (the send may have
  landed); connect timeouts are. `not_found` is chat-level only for "chat not found"; thread/topic/
  message-level not-found leaves the chat alive (sub-chat reading wins when both appear). [obs]
- **Retry policy:** up to 2 retries with exponential backoff base 2 s plus 0–1 s jitter; a server
  `retry_after` replaces the backoff once; penalties above 60 s return the typed failure immediately
  (ledger owns the wait); the classification is refreshed after every attempt; exhausted non-rate-limit
  retries send a "delivery failed" notice; exhausted rate-limit retries send nothing (would re-enter the
  ban). Non-network failures fall back to a plain-text send. A partially delivered split payload is
  never re-sent whole (resume only the remainder if the adapter can prove it was not delivered). [obs]
- **Dead targets:** when a send proves the entire chat unreachable (forbidden, or chat-level not
  found), the target is stored in a per-profile registry; later deliveries skip it until some send there
  succeeds again. [obs]

### 3.16 Delivery ledger (durable finals)

- **Purpose:** honest at-least-once for final replies across crashes. Rows live in the gateway's root
  state database (WAL), one ledger even for a multiplexed gateway. [obs]
- **States:** `pending` (recorded before any send) → `attempting` (just before the await) →
  `delivered` (only on success) | `failed` (definitive rejection) → `abandoned` (attempts or freshness
  exhausted). Every ledger call is best-effort and wrapped; ledger failure never blocks a send. [obs]
- **Row shape:** `Obligation { id = hash(session_key, message_ref, content), session_key, platform,
  chat_id, thread_id?, content, profile?, state, attempts, last_error?, owner_pid, owner_start_time,
  created_at, updated_at }` [inf from function signatures]
- **Recovery rules** [obs]:
  - pending at boot → redeliver plainly.
  - attempting at boot (owner process dead) → redeliver, prefixed with a visible warning that this
    copy might duplicate one already received; the same applies to a redelivery an earlier boot was still sending.
  - failed → retried in-process after backoff 30 s then 120 s; the final allowed attempt is reserved
    for the sweep at the next start; errors that prove the platform never saw the request (degraded send path) retry
    as soon as the adapter is back; reconnect-recovered sends use a distinct "platform reconnected"
    prefix.
  - flood refusals (the adapters' typed flood failure, or the platform's own "retry in N seconds" wording) → retry after
    the stated wait (default 60 s, capped 15 min, plus 2 s slack) with a third prefix warning that part
    may already have arrived; a restart during the penalty adopts the row without spending an attempt.
  - whole-chat death → never retried.
  - max 3 attempts; rows older than 24 h abandoned; delivered rows pruned after 7 days; table capped at
    500 rows.
  - Liveness of the owning process = PID plus start-time fingerprint.
- **Turn marker handoff:** the durable "active turn" marker for a message is cleared only after the reply
  is in the ledger, so a kill between "reply persisted" and "reply sent" is always recoverable. [obs]
- **Disable:** `gateway.delivery_ledger: false` restores lose-on-crash behaviour. [obs]

### 3.17 Delivery router and cron delivery targets

- **Target grammar:** `origin` (where the job was created), `local` (files under `<home>/cron/output/`),
  `<platform>` (that platform's home channel), `<platform>:<chat_id>[:<thread_id>]` (Telegram topics use
  `chat:thread`; Discord accepts `#channel-name` resolved via the channel directory), `<self-chat>` /
  `<self-chat>:<profile>` (reference literal in section 2; inject the output as an incoming message into a profile's canonical chat on the
  same machine), `all` (every connected home channel, resolved at fire time), comma lists
  (`telegram,discord`, `origin,all`). Unknown platform names are reported as `unknown_platform`, never
  silently routed to local. [obs]
- **Shape:** `DeliveryTarget { platform, chat_id? (None = home), thread_id?, is_origin, is_explicit,
  raw? }`. [obs]
- **Transport resolution:** a concrete enabled native adapter wins; the relay is used only when it
  explicitly advertises fronting that platform; an explicitly disabled native never shadows an enabled
  relay. [obs]
- **Output cap:** for platforms that do not split natively, cron output above 4000 chars is truncated
  with a footer pointing to the saved full output file. [obs]
- **Status:** delivery failures are distinct from run failures (`delivery_failed` with reason; not
  counted in the failure streak). Suppressed deliveries settle without claiming success. [obs]
- **Channel directory:** cached map of reachable channels per platform, built at startup, refreshed every
  5 min, persisted to a JSON file; used for listing targets and resolving friendly names. [obs]

### 3.18 Hooks

- **Discovery:** built-in hooks directory (empty extension point) plus `<profile home>/hooks/<name>/`
  each with a manifest (name, description, events) and a handler module exposing
  `handle(event_type, context)` (sync or async); paths resolve at call time; registered per served
  profile with idempotence keys including the profile. Errors never block the pipeline. [obs]
- **Events:** gateway startup; session start/end/reset; agent start/step (each tool-loop iteration)/end;
  `command:*` wildcard and `command:<name>`. Context for agent events: platform, user id, chat id,
  thread id ("" outside threads), chat type (dm|group|forum|""), session id, message (first 500 chars);
  agent end adds response, model, provider. [obs]
- **Plugin seams on the inbound path:** pre-dispatch (before auth; may rewrite or drop the event),
  post-admission consume (once per admitted message, fail-open), pre-command; plugin `post_message`
  routes to the calling conversation only (plugin never chooses the route). Shell hooks from config are
  re-synced on local session create/resume. [obs]

### 3.19 Background notifications and internal wakes

- **Process watchers:** a background terminal process with notify-on-complete arms a watcher on the
  gateway loop; on completion a new internal turn starts (the agent reports). Verbosity
  `concise` (default; one line, failure appends output tail) | `all` | `result` | `error` | `off`. If the
  launching turn is still running at completion, the injection queues a follow-up and the concise
  receipt is sent immediately. [obs]
- **Admission receipt:** an internal event counts as accepted only when actually scheduled or queued
  (a handler returning nothing is not acceptance); refused admission refunds claimed batch siblings
  without spending attempts. [obs]
- **Stateless adapters (API server):** wakes self-POST the chat-completions endpoint with the raw
  session id header to resume the real session; async delegation completions instead write a durable
  delivery row for client pollers. Failures raise after bounded retries. [obs]

### 3.20 Hosted rooms (multi-bot group chats)

- **What:** durable group chats in which several profiles ("bots") deliberate; a room hosted by one
  gateway keeps running while the desktop is closed; rooms may span machines through peer gateways
  (API server + key). [obs]
- **Storage:** room identity plus an append-only event log in the gateway's root state DB; control
  events `authority.claimed`, `authority.lost`, `room.disbanded`, `room.stop_requested`; authority epoch
  per gateway; idempotent event ids. [obs]
- **Limits:** protocol 2; ids ≤128 chars; name ≤200; event kind ≤64 (lowercase pattern); ≤128 members
  (members JSON ≤128 KiB); event JSON ≤256 KiB; log page ≤500 events / 2 MiB; ≤256 active rooms; ≤512
  disbanded tombstones kept 90 days; ≤50,000 events and 256 MiB per room; 16 MiB per gateway event;
  reserves of 64 events / 1 MiB for control events. [obs]
- **Driver:** a separate lease + task state machine (no model calls) schedules member turns; a working
  member keeps its turn up to 3 h, a silent one expires after 3 min. Stop directives are honoured only
  next to a member's @mention and not inside code/quotes. Authority transfer is an operator recovery
  procedure, not atomic. [obs, from docs]
- **Bot-to-bot messaging:** one canonical forever-chat per profile; an agent-to-agent messaging tool; typed
  failure reasons (provider auth/quota/rate/server, context overflow, missing config, model unavailable,
  runtime offline, queued expired, delivery timeout, target busy, unknown); at most one retry when it can
  help; cross-machine relayed turns capped at 10 min. [obs, from docs]

### 3.21 Control socket

- **Transport:** local only, never TCP. POSIX: a Unix socket in the profile home (or a temp-dir socket
  plus a pointer file when the home path exceeds ~100 bytes of `sun_path`); Windows: a named pipe whose
  name includes a home hash. Owner-only permissions (0600); clients refuse a home directory writable by
  others (world-write always; group-write unless a private user group with no ACL). [obs]
- **Wire:** each connection carries exactly one exchange — the client writes a single JSON line, the
  server writes a single JSON line back and closes. Request
  `{ protocol: 1, verb, id, params? }` (extra keys rejected for private verbs); response
  `{ ok, protocol, result | error, supported_verbs? }`. Request ≤64 KiB; response ≤512 KiB (else
  `response_too_large`); client timeout 2.0 s default (8 s for serve/unserve). A well-formed `identify`
  answer IS liveness. [obs]
- **Verbs:** identify (pid, home, profile, supervisor, code identity, served profiles), status (runtime
  status + answering pid), session-ticket (one-use grant, expires 30 s, purpose-bound: native HTTP or
  profile creation; checked against loopback peer and absent Origin header), pause-for-update,
  rescan-profiles (bounded 5 s, returns `pending` otherwise), migrate/purge profile identity,
  serve/unserve profile, reload-plugins. Consumers prefer the socket and fall back to state files. [obs]

### 3.22 Drain, restart, shutdown and watchdogs

- **Signals:** SIGINT/SIGTERM → drain and stop (a planned stop is recorded by the CLI/unit first so it
  is not classified as an external kill); SIGUSR1 → graceful restart (refuse new turns, wait for
  in-flight up to `restart_after_turn_timeout` 1800 s, exit 75 for the supervisor to relaunch);
  `systemctl reload` maps to SIGUSR1; SIGUSR2 → stack dump. [obs]
- **Timeouts:** chat-turn drain on SIGTERM `restart_drain_timeout` default 0 (in-flight chat turns are
  interrupted at once, the user gets a notice, and the work continues when they next write); cron and API runs get `cron_drain_timeout`
  30 s (10 s cleanup reserve); signal interrupt grace 1 s; post-interrupt grace 5 s; cron shutdown drain
  65 s; housekeeping drain 35 s; adapter disconnect 5 s; executor quiesce 2 s; systemd stop headroom
  30 s with a 60 s floor; launchd exit timeout clamp 60 s. [obs]
- **Exit codes:** 0 clean; 75 "please restart" (also used by watchdog); 78 fatal config (token collision,
  no platforms) — systemd prevents restart on 78, s6 maps to 125, launchd maps 78 → 0 with
  `SuccessfulExit=false`. [obs]
- **External drain:** a dashboard writes a drain-request marker file in the home; a watcher sets
  `draining` and refuses to start new turns ("maintenance" reply). Malformed marker = still active
  (fail-safe). Stale if the epoch differs (survives a restart on durable volumes) or older than 3600 s.
  [obs]
- **Interrupted turns:** sessions with in-flight work at shutdown are flagged restart-interrupted; on
  boot each resumes at most once with a heads-up message (auto-continue freshness 3600 s); a turn whose
  reply was persisted but not sent is delivered via the ledger instead of regenerated. Durable admissions
  that were started become `unknown` and pause the session's FIFO; the messaging user is told once per
  pause and `/reset` rotates to a fresh session; an operator can resolve the unknown row (generation
  checked). [obs]
- **Watchdogs:** out-of-loop thread probes the event loop every 30 s with a 10 s timeout; 3 consecutive
  misses → dump stacks, stamp `degraded` + `loop_liveness_watchdog`, exit 75. Heartbeat file refreshed
  every 30 s; status considers the state file stale after 120 s. Shutdown watchdog hard-exits past
  drain + 60 s grace. A 5 s floor timer keeps the selector timeout finite. Optional systemd
  `Type=notify` watchdog (`systemd_watchdog_seconds`, default 0 = off). [obs]
- **Restart notifications:** one per restart per platform to its home channel (per-platform opt-out).
  [obs]
- **Idle exit / scale-to-zero:** a client-auto-started gateway exits after 600 s idle (0 disables; not
  while cron/kanban work is due). Hosted scale-to-zero (2 min idle, gated by an env stamp) quiesces the
  relay ("go dormant", buffered events) and self-suspends the machine. [obs]
- **Self-restart guard:** the agent cannot schedule a gateway stop/restart from its own terminal tool
  (guard env), and the restart-loop guard skips auto-resume when boots chain. [obs]

### 3.23 Reconnect behaviour

- Failed platforms are retried by a watcher: backoff 30 s, 60 s, 120 s … capped at 300 s, forever for
  retryable failures; non-retryable failures drop out. After `agent.reconnect_attention_after` of
  continuous failure the platform is flagged `needs_attention` (a signal, not a pause). The watcher
  itself is supervised and respawned with capped backoff. Manual `/platform pause|resume` exists.
  [obs]
- Docs describe an automatic circuit breaker that auto-pauses an adapter and never auto-resumes; the
  source comments say pausing is manual only. Treat the docs as stale (see Open questions). [obs]

### 3.24 Browser-control broker

- Binds an identity-scoped controller (a party driving a browser, e.g. a desktop extension) to callers
  on any transport. Tickets: 30 s TTL, single use, identity-bound. Command timeout 30 s. Selection requires
  the capability set and an exact match on each of these identity attributes: principal, profile,
  session, controller, browser profile, transport family. A command can be completed only once; detaching the controller fails any
  outstanding work, whereas a transport disconnect merely flags it offline. Deferred cancels ≤512. Protocol 1. [obs]
- Capabilities allowlist: noop, back, click, navigate, press, screenshot, scroll, snapshot, tab activate,
  tabs, type; developer-only (off unless developer mode and negotiated): raw CDP, evaluate; artifact
  download/upload carry only server-minted 32-hex ids. Artifacts: TTL 300 s, ≤10 MiB, MIME allowlist,
  SHA-256 re-verified on read, temp-write-then-rename, consumed atomically with the exact scope key. [obs]
- Exposed via the API server (`register`, WebSocket with a dedicated subprotocol and ticket subprotocol
  prefix). [obs]

### 3.25 HTTP ingress adapters and relay

- **API server (OpenAI-compatible):** default bind 127.0.0.1:8642 (5 bind attempts while a predecessor
  releases the port); bearer key; CORS origins configurable; security headers; request ≤10 MB;
  normalised text parts ≤64 KiB, content arrays ≤1000 items; SSE keepalive 10 s; heartbeat 30 s; image
  data URLs ≤5 MB; reasoning efforts none…ultra; per-request agent (bypasses the turn runner and the
  agent cache) but memory providers parked between requests; session header ≤256 chars; run stream TTL
  300 s, terminal run status kept 3600 s; runs support approval, clarify, steer, stop, resolve-unknown;
  cron job CRUD (name ≤200, prompt ≤5000, 12-hex ids). [obs]
- **Webhook:** port 8644; routes from config or a dynamic subscription file; per-route secret (HMAC
  SHA-256 hex; GitHub-style signature; GitLab token exact match; a timestamped HMAC v2 variant; an
  explicit insecure no-auth marker only on loopback); 30 req/min; 1 MiB; idempotency 1 h; payload
  filters, script transforms, prompt templates with `{dot.path}` keys; optional coalescing (latest
  event per payload key after a quiet window, bounded by a max wait); delivery to any platform or log;
  direct delivery mode (no agent). [obs]
- **Shared ingress under multiplexing:** the default profile owns one listener; secondary profiles'
  port-binding adapters are mounted under `/p/<profile>/…` and verified with that profile's secret; a
  missing adapter yields 404, never the default's. [obs]
- **Relay:** gateway dials out a WebSocket to a connector, receives a capability descriptor
  (`contract_version` 1; platform, label, max length, len unit chars|utf16, draft/edit/thread support,
  markdown dialect, optional context, block formatting, supported ops), then inbound events and
  interrupts arrive over the same socket; outbound ops send/edit/typing/follow_up and, when advertised,
  send_media (by reference, ≤25 MB), prompt (buttons), react, thread_create, thread_rename. Relay-delivered
  events are authorized upstream. Profile is round-tripped in outbound metadata so follow-up
  interactions land in the same profile. [obs]

### 3.26 Multiplexed profiles (scope binding) — summary

- One process can serve the default profile plus every live profile directory. Each activity (turn,
  eviction, shutdown finalisation, media delivery, deferred callbacks, cron tick, child processes,
  background threads) MUST bind the owning profile's home + secret scope + terminal scope explicitly;
  the process environment holds only the launch profile. Adapter YAML is never bridged into the process
  env for secondaries. Intake adapter (which bot received) and delivery adapter (which bot answers) are
  resolved by two functions only; a restored source with no live provenance delivers only through the
  unique owner of (platform, profile) or not at all — never the default bot by guess. The receiving bot
  is persisted next to the route so delivery survives restarts. [obs]

## 4. File map (what to read in the Read step)

| Sub-area | Reference path | Lines | Read what | Docs / tests to read with it |
|---|---|---|---|---|
| Runner facade | `gateway/run.py` | 5245 | skim; module-level helpers (config bridging, reconnect backoff, media placeholders, approval fallback text) + class defaults | `developer-guide/gateway-internals.md` |
| Inbound pipeline | `gateway/run_inbound.py` | 1987 | `_handle_message`, `_hm_admit_event`, media enrichment, transcription | `tests/gateway/test_*busy*`, `test_adapter_session_key_seam.py` |
| Busy dispatch / command tables | `gateway/run_busy.py` | 1528 | command tables and busy routing | `user-guide/messaging/index.md` busy modes |
| Turn execution | `gateway/run_turn.py`, `run_turn_runner.py`, `run_turn_prepare.py`, `run_turn_progress.py`, `run_turn_hygiene.py`, `run_turn_persistence.py` | 2403, 1375, 719, 1008, 828, 618 | skim; progress/heartbeat and persistence hand-off | area 01 |
| Adapter lifecycle | `gateway/run_adapters.py` | 1974 | connect, reconnect watcher, runtime status | — |
| Startup / restore / handoff | `gateway/run_startup.py`, `run_bootstrap.py` | 1899, 918 | handoff methods, signal handlers, control verbs | `user-guide/sessions.md` handoff |
| Shutdown / drain | `gateway/run_shutdown.py`, `restart.py`, `drain_control.py`, `shutdown_watchdog.py`, `shutdown_flush.py`, `restart_loop_guard.py`, `lifecycle_ledger.py`, `run_idle_exit.py`, `scale_to_zero.py` | 2252, 371, 227, 403, 502, ~120, 439, 225, 268 | constants and state transitions | `test_cron_shutdown_drain.py`, `test_control_socket_pause.py` |
| Notifications | `gateway/run_notifications*.py`, `wake.py` | 1813+374+258, 266 | watcher arming, admission receipts | `test_completion_delivery.py` |
| Base adapter | `gateway/platforms/base.py` | 4904 | send retry, classifier, chunker, media extraction/path policy, busy guard, ledger hooks | `test_delivery_flood_invariants.py` |
| Event types | `gateway/platforms/event.py` | 150 | all | — |
| Session store / keys | `gateway/session.py` | 1358 | `build_session_key`, namespace, entry fields | `test_adapter_session_key_seam.py` |
| Authorization | `gateway/authz_mixin.py`, `slash_access.py`, `bot_loop_guard.py`, `pairing.py` | 758, 162, 141, 660 | all | `test_bot_loop_guard.py`, `test_*authz*`, pairing tests |
| Delivery | `gateway/delivery.py`, `delivery_ledger.py`, `dead_targets.py`, `mirror.py`, `channel_directory.py` | 334, 564, 104, 141, 475 | all | `test_delivery*.py` |
| Streaming | `gateway/stream_consumer*.py` | 976+610+415+101+38 | invariants, edit cadence | `test_stream_final_contract.py` |
| Approvals text | `gateway/platforms/base_exec_approval.py` | 84 | tiers and notice | `test_approval_*.py` |
| Hooks | `gateway/hooks.py`, `run_inbound_hooks.py` | 181, 299 | events, context | `user-guide/features/hooks.md` |
| Control socket | `gateway/control_socket.py` | 542 | wire, verbs, limits | `test_control_socket*.py` |
| Browser broker | `gateway/browser_control_broker.py`, `browser_control_artifacts.py` | 512, 323 | all | — |
| Hosted rooms | `gateway/hosted_room*.py`, `hosted_rooms*.py` | ≈7,000 total (largest 1364, 1212, 911, 895) | constants + state machine only | `user-guide/bot-mode.md` |
| Status / locks | `gateway/status.py`, `host_rendezvous.py`, `host_attach.py` | 2123, 812, 424 | scoped locks, pid liveness, state file | — |
| Registry | `gateway/platform_registry.py`, `platform_env.py`, `platforms/_shared.py`, `config.py` | 530, 230, 260, 888 | entry shape, enablement, scoped secret reader, Platform enum, streaming defaults | `platforms/ADDING_A_PLATFORM.md` (445) |
| API server | `gateway/platforms/api_server*.py` | 4611 + ≈4,700 in siblings | routes, limits, run lifecycle | `user-guide/features/api-server.md` |
| Webhook | `gateway/platforms/webhook*.py`, `shared_ingress.py` | 1042+259+226+155+100, 151 | auth, rate, coalesce | `user-guide/messaging/webhooks.md` |
| Relay | `gateway/relay/*.py` | 2305 (adapter) + 1002 (ws) + ≈1,800 others | descriptor, ops | `developer-guide/relay-connector-contract.md` (819) |
| Session authority (desktop/TUI) | `gateway/session_authority.py`, `session_*.py` (~90 files) | 1212 + many 30–900 | defer; skim names only | `developer-guide/gateway-session-lifecycle.md` |
| Kanban watchers | `gateway/kanban_watchers*.py` | 332+821+346+150 | defer | — |
| Telegram | `plugins/platforms/telegram/` | 8427 (adapter 7326) | constants block (top ~500 lines), send/edit overflow, flood handling, update admission | `user-guide/messaging/telegram.md` (1455) |
| Discord | `plugins/platforms/discord/` | 9004 (adapter 7408) | intents, slash sync, threads, components | `messaging/discord.md` (1003) |
| Slack | `plugins/platforms/slack/` | 7754 (adapter 6950, block_kit 621) | socket mode, streaming, Block Kit | `messaging/slack.md` (1100) |
| WhatsApp | `plugins/platforms/whatsapp/`, `scripts/whatsapp-bridge/` | 1412; bridge JS 2149 | bridge protocol, ownership | `messaging/whatsapp.md` |
| Signal | `plugins/platforms/signal/` | 1657 (adapter 983) | RPC and SSE, rate limit | `messaging/signal.md` |
| Other adapters | `plugins/platforms/<name>/` | feishu 5684, yuanbao 4043, matrix 3533, photon 2911, qqbot 2838, google_chat 2585, buzz 2218, wecom 2170, a2a 2038, whatsapp_cloud 1533, weixin 1465, email 1054, dingtalk 1061, teams 1024, line 1001, mattermost 771, bluebubbles 756, simplex 721, irc 620, raft 582, sms 385, ntfy 383 | one at a time when implementing | per-platform docs |
| Send tool / cron delivery (adjacent) | `tools/send_message_tool.py`, `cron/scheduler_delivery.py` | 737, 1923 | target parsing and delivery calls | `features/cron.md` |

Giant files:
- `gateway/run.py` [5245]: matters — the class defaults block near the class definition, reconnect
  backoff helpers (search "Exponential reconnect backoff"), approval fallback formatter, media placeholder
  builders. Most of the rest is config bridging and hygiene/compaction glue.
- `gateway/platforms/base.py` [4904]: matters — `SendResult` and the error classifier table, the
  pending-event merge, `_send_with_retry`, `truncate_message`, `handle_message` /
  `_handle_message_while_active` / `_process_message_background`, media extract/validate. Skip proxy
  and TTS plumbing on first read.
- `gateway/platforms/api_server.py` [4611]: matters — constants block (first ~300 lines) and route
  registration; the rest is OpenAI wire translation.
- `plugins/platforms/discord/adapter.py` [7408], `telegram/adapter.py` [7326], `slack/adapter.py`
  [6950]: read the top constants, `connect`, `send`, `edit_message`, inbound handler; skip voice and
  onboarding first.
- `gateway/run_turn.py` [2403], `run_shutdown.py` [2252], `relay/adapter.py` [2305], `status.py`
  [2123]: read only the parts named above.

## 5. Minimum viable subset

v1 needs:
1. One gateway process per profile (no multiplexing), foreground run + one service template (systemd
   user unit), PID + start-time liveness, state file, exit codes 0/75/78, SIGTERM drain, SIGUSR1 restart.
2. Base adapter contract, normalised event/source shapes, registry with env enablement and explicit
   disable, token locks.
3. Adapters: Telegram (polling), Discord, Slack (Socket Mode), Signal (signal-cli), WhatsApp (bridge or
   Cloud — pick one), plus the API server (needed by other areas) and webhook receiver.
4. Authorization chain (allow-all, allowlists, global allowlist, pairing with all limits, default deny),
   unauthorized DM behaviour, bot-loop guard, slash admin tiers.
5. Session key builder (DM/group/thread rules, per-user group isolation), routing index, no idle reset,
   `/new`, `/resume`, `/sessions`, `/title`, `/model`, `/status`, `/stop`, `/sethome`, `/help`,
   `/whoami`, `/approve`, `/deny`, `/queue`, `/bg`.
6. Two-guard busy handling with interrupt and queue modes (steer can follow).
7. Intake: voice transcription with fallback and empty-transcript note; images (text mode first, native
   later); documents; reply context.
8. Outbound: chunker with fence carry and indicators, Telegram MarkdownV2 + plain fallback, retry
   policy + classifier, media tag extraction with path policy, silence tokens, typing indicator.
9. Delivery ledger with all recovery rules and markers; delivery router target grammar (origin, local,
   platform, platform:chat[:thread], all, lists) for cron.
10. Exec approval buttons + text fallback with 300 s timeout; clarify numbered prompts.
11. Control socket with identify/status (later verbs as needed); restart notification; auto-resume once.

Can wait: multiplexed profiles and host rendezvous (huge, scope-binding bugs dominate the reference);
relay connector; hosted rooms and peer federation; desktop/TUI session authority and canonical WS;
browser-control broker; kanban watchers; scale-to-zero; the long tail of 20+ adapters; streaming TTS and
Discord voice channels; native draft streaming; mid-run plugin rewiring; Windows service and pipe.
Reason: none of these is needed for "chat with the agent from the big five platforms reliably". [inf]

## 6. Hard parts / traps

1. **R06.1 Control commands must bypass both busy guards.** The adapter-level pending slot and the
   runner-level running-agent check each swallow messages while a turn runs. `/approve`, `/deny`,
   `/stop`, `/new`, `/queue`, `/status` must be dispatched inline at both levels, or the agent blocked on
   an approval can never be unblocked. Never route them through the background task path, which races
   session lifecycle.
2. **R06.2 Claim the session slot before the first await.** Many awaits sit between "message accepted"
   and "agent registered"; without a sentinel installed synchronously, a second message spins up a
   duplicate agent on the same transcript. Release by run generation so a displaced turn cannot clear its
   successor.
3. **R06.3 Never sleep long flood penalties inline.** A 97-minute server retry-after slept in the send
   coroutine froze inbound on every platform. Cap inline waits (5 s Telegram, 60 s base) and hand longer
   waits to the ledger as a typed flood failure carrying the wait. Also never send a "delivery failed" notice
   while the penalty is active (it re-enters the ban).
4. **R06.4 Retry ambiguity: timeouts are not retryable.** A read/write timeout may mean the platform
   received the message; retrying duplicates. Only connection-level failures and explicit rate limits
   retry. A partially delivered split message must never be re-sent whole; resume only a remainder the
   adapter can prove was undelivered.
5. **R06.5 Ledger states must distinguish "never started" from "maybe sent".** Record `pending` before
   the send and `attempting` immediately before the await; only then can boot recovery redeliver plainly
   vs with a visible duplicate warning. Clear the durable active-turn marker only after the reply is in
   the ledger, otherwise a kill in between loses the reply entirely.
6. **R06.6 Bare "yes" must never approve.** Approvals are only via buttons or explicit `/approve`;
   conversational text that happens to be affirmative goes to the agent as normal text.
7. **R06.7 Session keys are a compatibility surface.** Keys are persisted; changing ordering (user vs
   thread position differs between DMs and groups), adding guild ids, or changing the namespace literal
   orphans every existing conversation. A profile named like the default namespace must get a marker.
   Discord auto-threads must be keyed on the prospective thread so follow-ups match.
8. **R06.8 Default-deny with surprising pairing defaults.** Configuring any allowlist silently switches
   unknown DMs from "pair" to "ignore" (to avoid spamming codes); email is always "ignore"; groups never
   get pairing. Make this explicit in setup UX or operators think pairing is broken.
9. **R06.9 Pairing directory must resolve per call.** Computing the storage path at import froze it to
   the boot-time home, so codes issued by the gateway were invisible to the CLI that approves them.
   Strip whitespace from typed codes; check lockout before lookup; never count admin request-id approvals
   toward lockout.
10. **R06.10 Bot-loop budgets per conversation, not per sender pair.** Two agent bots in one chat will
    reply to each other forever if bots are admitted; a per-pair budget gives N bots N budgets. Count once
    per inbound message (the authorization check is called several times).
11. **R06.11 Chunking edge cases.** Split inside a code fence must close and reopen with the same language;
    never leave an unpaired inline backtick (breaks MarkdownV2); guarantee progress for tiny limits and
    wide characters; Telegram counts UTF-16 units, not code points; MarkdownV2 escaping can push a
    message that fit into two chunks.
12. **R06.12 Streaming frames must be prefix-stable on native-stream platforms.** Any per-tick mutation
    (closing fences, cursor, format conversion) forces a whole-snapshot re-append and visible stacked
    copies; augmenting the final after sealing causes a corrective duplicate send. Mark every non-final
    send as interim at every egress door.
13. **R06.13 Telegram counts edits against the same per-chat allowance as sends.** Pacing only edits at
    0.8 s caused most flood penalties; use one shared per-chat slot where sends wait, interim edits are
    skipped, and the final edit is never gated.
14. **R06.14 Silence tokens are whole-message only and never hide failures.** A reply that merely
    mentions the token is delivered; a human message that expected an answer gets a short notice rather
    than nothing; the silent turn is still stored to keep role alternation.
15. **R06.15 Explicit `enabled: false` must beat credentials in env.** Older behaviour let env
    credentials re-enable twelve platforms; log a warning per platform so it does not go dark silently.
16. **R06.16 Token locks: startup conflict is fatal, mid-run conflict is retryable.** Two gateways
    polling one Telegram token break both. At startup with nothing else connected exit 78 so supervisors
    stop; mid-run let the other holder exit and retry.
17. **R06.17 Exit code semantics with supervisors.** 78 is treated as permanent by systemd/s6/launchd;
    using it for "another process serves this profile right now" strands the unit — use 75 there.
    launchd cannot gate on 78, so map 78 → 0 under `SuccessfulExit=false`. Never add an unconditional
    kill on stop (restart loops).
18. **R06.18 Liveness is PID + start time, or a socket answer.** Bare PID existence is wrong after PID
    reuse; the control socket's `identify` answer is the authoritative liveness check.
19. **R06.19 Unknown turns after a crash must pause, not replay.** A turn that had started when the owner
    died has unknowable side effects; replaying it or running queued followers could double-act. Pause the
    FIFO, tell the user once, let `/reset` or an operator resolve it with a generation check.
20. **R06.20 Drain marker staleness.** A drain request file on a durable volume survives the restart the
    drain was for and parks the new process in draining forever; stamp an epoch and an expiry (3600 s);
    treat a malformed file as active (fail toward quiescing).
21. **R06.21 Cross-session context leaks.** Per-message tasks copy the spawning context; reset per-session
    context variables at handler entry so a subprocess never sees another chat's identity.
22. **R06.22 Media path policy is a security boundary.** Agent-emitted `MEDIA:` paths can point at
    credentials; enforce safe roots, deny-lists and symlink resolution, including for files fetched from a
    remote sandbox (screen before and after resolving).
23. **R06.23 Empty or failed transcription must say so.** Passing an empty quoted transcript makes the
    agent answer nothing and can loop; insert an explicit "inaudible, ask to resend" note. Wrapping the
    transcript in a meta sentence made models discuss voice mode.
24. **R06.24 Broadcast surfaces that report "dm".** ntfy topics, raft channels and a2a peers report a DM
    chat type; anything that trusts "DM = private" (pairing codes, login consent links) must check a
    per-platform flag.
25. **R06.25 Gateway config loader diverges from CLI defaults.** The reference gateway reads raw YAML
    without the CLI defaults; a rebuild should use one loader with one defaults table to avoid
    gateway-only behaviour differences.
26. **R06.26 Multiplex secret reads must fail closed (if multiplexing is built).** A missed scoped read
    that falls back to the process env leaks the launch profile's allowlist, which silently rejects every
    secondary-profile sender; eviction under the wrong scope wrote one profile's memories into another's
    store.

## 7. Open questions

1. Is the automatic adapter circuit breaker (docs: auto-pause on repeated retryable failures, notify
   another platform's home channel, manual resume) live code or stale docs? Source comments say pausing
   is manual only. Needed: needs REA (induce repeated adapter failures on a running build and observe
   `/platform list`).
2. Exact default of `agent.reconnect_attention_after` and the `display.ephemeral_system_ttl` default.
   Needed: read the CLI defaults table in the Read step (not checked here) or needs REA.
3. Exact on-disk shape of the delivery ledger and routing-index tables. Needed: area 03 (state) owns it;
   confirm via a DB copy (needs REA).
4. Which adapters set `enforces_own_access_policy`, `authorization_is_upstream`, `trusted_inbound`,
   `supports_async_delivery`, `draft_stream_is_message`. Needed: grep each plugin in the Read step.
5. Real Telegram behaviour of `sendMessageDraft` (native drafts) and DM topics with "Threaded Mode".
   Needed: needs REA against a live bot; vendor docs check.
6. The pairing-code reply wording and unauthorized decline default text (UI copy). Needed: product
   decision for our own copy; reference copy must not be reused.
7. Whether platform credential env names (bot tokens, allowlists) should keep the platform-native
   unprefixed form or be prefixed with `INDUS_`. Needed: lead decision (interop with existing operator
   setups vs namespace hygiene).
8. Should v1 support multiplexed profiles at all? The reference default is `multiplex_profiles: true`
   and much complexity stems from it. Needed: lead decision.
9. Exact busy-ack emoji/text per mode and when `busy_ack_detail` adds iteration info. Needed: read
   `run_busy.py` in the Read step or needs REA.
10. WhatsApp bridge HTTP API (endpoints beyond `/health` and `/send-media`, polling cadence). Needed:
    read `scripts/whatsapp-bridge/bridge.js` (1179 lines) in the Read step.
11. Cron "continuable surface" semantics (thread vs in-channel) and which deliveries are mirror-eligible.
    Needed: area 07 plus `cron/scheduler_delivery.py`.
12. Hosted-room driver state machine states and lease durations. Needed: read
    `gateway/hosted_room_driver.py` (911) if hosted rooms enter scope.
13. Whether the API server's absence of a key on loopback is allowed (unauthenticated local access) or
    refused. Needed: read `api_server.py` auth middleware or needs REA.

## 8. What to read later

- Reference docs: `website/docs/developer-guide/gateway-internals.md` (793),
  `gateway-session-lifecycle.md` (659), `gateway-monitoring.md` (310), `multiplexing-gateway.md` (336),
  `relay-connector-contract.md` (819), `adding-platform-adapters.md` (891),
  `completion-backlog-delivery.md` (52), `streaming-tts.md` (118); user guide
  `messaging/index.md` (1003), `messaging/telegram.md` (1455), `discord.md` (1003), `slack.md` (1100),
  `signal.md` (262), `whatsapp.md` (346), `whatsapp-cloud.md` (421), `webhooks.md` (705), `relay.md`
  (212), `bot-mode.md` (395), `multi-profile-gateways.md` (1335), `sessions.md` (handoff section),
  `features/cron.md` (delivery options), `features/hooks.md`, `features/api-server.md`; `gateway/AGENTS.md`
  (221), `gateway/platforms/ADDING_A_PLATFORM.md` (445).
- Reference tests that list the edge cases (`tests/gateway/`, 1,519 files): `test_stream_final_contract.py`,
  `test_multiplex_transport_matrix.py`, `test_delivery_ledger*.py`, `test_delivery_flood_invariants.py`,
  `test_delivery_silence_filter.py`, `test_delivery_unknown_target.py`, `test_bot_loop_guard.py`,
  `test_control_socket*.py`, `test_approval_*.py`, `test_adapter_session_key_seam.py`,
  `test_adapter_authz_secret_scope.py`, `test_shared_platform_boilerplate.py`, `test_native_http_auth.py`,
  `test_cron_shutdown_drain.py`, `test_busy_followup_*.py`.
- REA Evidence (outside the repo): none captured — REA not connected this session.
- Related specs: `01-agent-loop.md` (interrupt/steer/redirect semantics, approval waits),
  `02-tools.md` (send-message tool, terminal background notify, transcription and vision tools),
  `03-state.md` (routing index, transcripts, delivery ledger tables, hosted-room tables),
  `07-cron.md` (scheduling, continuable surfaces, failure streaks).
