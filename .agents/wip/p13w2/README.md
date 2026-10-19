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
