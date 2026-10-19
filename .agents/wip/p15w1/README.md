# P15 wave 1 — slice reports (base 8f99060)

## G2 — w18-p15g2.patch — DONE (P15-06..10 registry/token locks/proxy, session keys + routing index, authz chain, pairing + `indus pairing`, bot-loop/concurrency/admin tiers)
- New: gateway/{registry,token_locks}.py, gateway/core/{__init__,_shapes,session_keys,authz,pairing,
  bot_loop,slash_access}.py, state/gateway_routes.py, cli/commands/pairing.py, tests (unit/gateway,
  unit/state/test_gateway_routes, cli test_pairing; _stubs_g2.py STAND-IN — delete), golden
  tests/fixtures/gateway/session_keys.golden.json (25 shapes), notes ×5.
- Shared: state/schema.py gateway_routes table (+ index); cli/app.py `pairing` row; pyproject
  import-linter `ignore_imports` (cli → gateway pairing) — LEAD DECISION: replace the exemption with
  a general rule; preferred: move stores the CLI needs (pairing store, routes) below cli/gateway
  (state or service layer) so no exemption is needed, and do the same for P15-28's gateway commands;
  traps R06.7–10, R06.16, R06.24, R04.26; ACC-K4-1, K14-1, K15-1, K16-1, K18-1, K19-1, K20-1.
  Package markers gateway/core/__init__.py, tests/unit/gateway{,/core}/__init__.py likely clash with G1.
- Wiring: swap test imports to G1's Source and adapter base; only gateway/core/_shapes.py names G1's
  attributes (flags auth_upstream, owns_access_policy, dm_is_private; optional access_policy(kind),
  allows_dm(user_id), resolved_allowlist_ids(); Source getattr: role_authorized, user_name,
  parent_chat_id); add "platform" to P13 PROVIDER_KINDS and call
  PlatformRegistry.load_plugin_entries(host.providers); config rows (G1 owns the schema) —
  gateway.trust_env true, group_sessions_per_user true, thread_sessions_per_user false,
  unauthorized_dm_behavior pair, bot_loop_guard.{enabled true, max_messages 20, window_seconds 300,
  cooldown_seconds 600}, max_concurrent_sessions none.
- Decisions (need D-ids): api_server/webhook entries trusted_inbound; extra registry fields
  group_users_env, group_chats_env, trusted_inbound; thread chat type counts as group; any allowlist
  (incl. config/group lists) flips unknown DMs to ignore; independent DM/group slash lists; repeated
  message id counted once by the bot-loop guard; locks via one cross-process guard lock + atomic
  replace; INDUS_GATEWAY_LOCK_DIR (absolute only).
- Follow-ups: deleting a session leaves routes pointing at it (P15-11 treat as rotate, or
  state/deletion.py drops route rows); P15-24 liveness replaces token_locks' private start-time helper.

## G1 — w18-p15g1.patch — DONE (P15-01..05 fake platform + contract suite, gateway/platform config, inbound events, send result/classifier/retry, adapter base)
- New: gateway/config.py, gateway/core/{__init__,events,sent_index,send,send_slot}.py,
  gateway/adapters/{__init__,base}.py; fakes tests/fakes/platform/{core,telegram_wire,discord_wire,
  slack_wire}.py; tests/contract/gateway/* (10 named rules + no-network; register_case for adapters),
  tests/unit/gateway/*, tests/unit/fakes/test_platform_wires.py; notes ×5 (API sections).
- Shared: config PlatformsSection (telegram, discord, slack: PlatformSection), GatewaySection
  (message_timestamps.enabled) — G2's rows go INTO this class; ConfigModel.platforms/gateway;
  display.busy_ack, busy_text_quiet_s 0.35, busy_text_cap_s 1.0; test_schema pins;
  tests/fakes/platform/__init__.py exports; traps R06.3/4/13/15/24/25; ACC-K4-1, K13-1, K29-1.
  Package markers gateway/core/__init__.py, tests/unit/gateway{,/core}/__init__.py clash with G2 (keep one).
- Wiring: G2's stand-ins map onto G1's Source / InboundEvent / Adapter / Capabilities (names final in
  notes; G2 reads adapter flags via gateway/core/_shapes.py — reconcile names there); inbound metadata
  conventions edited / button / mentioned; load_gateway_settings(scope, credentials=…) takes the
  registry's required_env; doctor line legacy_reset_notice(tree) → cli doctor registry; adapters
  (P15-33..35, P16) register_case + vendor_names, numeric ids for Telegram/Discord.
- Decisions (need D-ids): plain-text fallback only for bad_format/unknown; timeouts never retried;
  jitter clamped; send_notice_private and media sends refuse by default; enabled without credentials
  does not start (problems list); config home channel beats env; SentIndex in memory only; bare `/`
  is text; message_timestamps under gateway; busy-text key names; callback prefixes apv:/clr:; keys
  under platforms.<name> merged into extra (explicit extra wins).
- Follow-ups: plugin platform blocks log "unknown setting (kept)" until P16; Slack typing_status_text,
  Discord link-to-id normalisation (P15-34/35); interactive_resume for P15-26; no hypothesis.

## G3 — w18-p15g3.patch (base 8f99060) — DONE (P15-14..18 chunker, formatters, stream consumer + typing/progress, outbound sender + filters + dead targets, media tags + path policy)
- New: gateway/outbound/{__init__,chunker,formatting,stream,progress,sender,filters,dead_targets,
  media_policy,ports}.py (ports.py = only module calling an adapter), tests/unit/gateway/outbound/*
  (_stubs_g3.py STAND-IN for G1's SendResult/adapter/sent index — delete), fixture
  tests/fixtures/gateway/markdownv2_cases.json, notes ×5. Package marker tests/unit/gateway/__init__.py
  also from G1/G2 (keep one).
- Shared: traps R06.11–14, R06.22, R07.14; ACC-K25-1..K29-1. No schema rows added.
- Security (tested): MEDIA: files only from trusted places (profile cache/{images,audio,video,documents},
  operator allow-dirs, or — outside strict mode — files created in the last 600 s); symlinks resolved;
  P05 read_denial/blocked_device + deny-list (credentials dirs, Indus secret/control/state files, other
  profiles, /etc /proc /sys /dev /root /var/*) on written and resolved path; refused tags give a notice;
  remote sandbox paths checked before/after; secrets masked before formatting/chunking and in stream
  frames (text shown only up to last whitespace); only upper-case MEDIA:.
- OWNER/LEAD DECISION: Linux has no birth time, so a file whose mtime the agent just bumped (touch)
  outside the deny-list counts as recent. Options: default gateway.media_trust_recent=false, or strict
  mode by default for public bots (pairing/unknown users). Recommend: trust_recent off by default.
- Wiring: swap _stubs_g3 for G1 types in ports.py (chat_unreachable → G1's chat-level not-found helper;
  capability markup → formatter_for(); media methods send_image/video/voice/document, audio →
  send_file; busy per-chat slot returns ok with skipped=True); adapters strip interim/final metadata;
  runner feeds StreamConsumer/ToolProgress via feed(event), runs TypingLoop/Heartbeat, calls
  OutboundSender.deliver only when StreamOutcome.final_delivered is false or silenced; P15-19 ledger
  stores DeliveryReport.undelivered.
- Config rows to add (G1's GatewaySection): gateway.media_strict, media_allow_dirs,
  media_trust_recent, media_trust_recent_seconds 600, display.ephemeral_system_ttl, per-platform
  streaming, tool_progress (+ grouping), heartbeat interval.
- Decisions (need D-ids): chat finals always force redaction; chunker cuts bare below 32 units; Slack
  table limit 20,000 per phase row, one native table per message; approval placeholder only on native
  streams; four English silence tokens.
- Follow-ups: Slack raw_text cell shape vs pinned SDK (P15-35); Docker container-path translation for
  media (P11 backend).
