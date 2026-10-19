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
