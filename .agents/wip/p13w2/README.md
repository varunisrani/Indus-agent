# P13 wave 2 — slice reports (base ad24cb5)

## N1 — w16-p13n1.patch — DONE (P13-05..08 MCP discovery/registration, calls, trust+breaker, reload)
- New: mcp/{discovery,calls,trust,reload,schema_cache}.py, safety/approvals/tool_gate.py, tests
  (unit/mcp, contract/mcp/test_host_live.py, tests/fakes/mcp/scripted.py), notes ×4.
- Shared: mcp/connect.py (LiveSession.raw_hints — wire readOnlyHint), tools/registry.py
  (set_toolset_alias; names_in_toolset/toolset_names follow aliases), plugins/hooks.py (plugin
  `approve` → tool_gate), config approvals.mcp_reload_confirm (+test), traps R07.22–25, ACC-G14-1.
- Wiring: per profile McpHost(scope, registry, clock, options, credential_stamp) → register_host →
  await host.discover(); CLI starts ConfigWatcher(host); listener on ToolSurfaceChanged drops the
  provider prompt cache and rebuilds the tool array at next session/compression; tui_bridge
  methods_config MCP_RELOAD = "indus.mcp.reload:reload"; /reload-mcp = reload_mcp(host, ask=…).
- API for N2: host.status(), host.handles[name].specs, host.reauthorised(name); calls.call(handle,
  spec, args, CallContext); OAuth checks trust.interactive_auth_allowed() and logs in inside
  trust.allow_interactive_auth().
- Decisions: YOLO / approvals.mode off bypass the untrusted gate; unattended follows P05 keys (deny);
  once/session/deny; parked or cooling servers keep tools registered (calls get unavailable/breaker
  result); no handler retry; reload reconciles without a session message; 2 s list_changed window;
  cache fingerprint covers resources/prompts settings and stores raw hints.
- Follow-ups: tool-search bridge (C4) marks mcp__ tools deferrable; -t/--toolsets spawn filter (all
  enabled servers start); SECURITY: `MEDIA:` lines inside server text are not neutralised — the P15
  delivery scanner must deliver trusted paths only (or neutralise here); a non-boolean readOnlyHint
  makes the SDK reject the whole listing; no cache expiry (ttlMs ignored).

## N2 — w16-p13n2.patch — DONE (P13-09..12 OAuth, sampling+elicitation, OSV preflight, `indus mcp`)
- New: mcp/{oauth,sampling,osv,extras}.py, cli/commands/mcp.py, tests (unit/mcp, cli test_mcp),
  fakes tests/fakes/mcp/{oauth_server,asking_server}.py, notes ×4.
- Shared: providers/auth/pkce.py (keeps `iss`, RFC 9207); providers/auxiliary.py call_route gains
  temperature, tools; foundation/errors.py codes mcp.auth_required / oauth_failed / malware_refused;
  config mcp.osv_preflight (+test); cli/app.py `mcp` row; contract non_tty_table + help catalogue;
  traps R07.22/24, ACC-G19-1.
- Wiring onto N1: per server `indus.mcp.extras.extras_for(name, server, scope,
  sampling=aux_backend(aux))` → pass extras.options + extras.opener() to ManagedServer; P13-06 call
  path wraps each tools/call in `extras.requesters.serving(port, unattended=…, turn_id=…)`;
  /reload-mcp rebuilds extras; P13-07 uses McpAuthRequired for the parked warning; /mcp and
  /reload-mcp chat commands can use status_rows / status_lines.
- Decisions: only explicit logins interactive; in-process refresh fence, store re-read for
  cross-process logins; secret names MCP_OAUTH_<SAFE>_<sha8>_*; oauth.scope / client_metadata_url read
  as extra keys; sampling advertises tools when max_tool_rounds > 0; elicitation returns values;
  unversioned packages checked by name; `add` has no checklist; unknown server in `mcp test` exits 1.
- Follow-ups: drop dead dynamic registration on invalid_client; guard the SDK's guessed /register;
  login clears old tokens before the browser step (reference snapshots/restores — consider fixing);
  no own CIMD document; sampling deprecated from protocol 2026-07-28 (client side kept).
