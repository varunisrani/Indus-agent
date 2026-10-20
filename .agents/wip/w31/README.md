# Wave 31 parked patches

## w31-d1.patch: daemon half of D-16.602 — `session.mutate` + `acp.editor_policy` (attached `indus acp` against a real daemon) — done (base 2d06e7b)
- Integrate WITH or right after w30-a2 (P19-08..13): apply w31-d1-a2-wiring.patch on top of both (a2's attached
  run_command → mutate(sid, "command", {name, arg}) → CommandReply; tests/contract/acp/test_commands.py:142-143 now expects
  "Model calls:" and "Session title set: Something"). Expect small ledger conflicts with a2 (R09.13, ACC-M8-1).
- Builder checks: targeted not-perf 2745 passed / 57 skipped (unit acp, contract acp, e2e acp, contract gateway, unit
  protocol, unit service, unit tools, unit gateway/daemon, integration service); integration ws_listener + e2e service 3
  passed / 1 skipped; new unit tests 3× green; with a2 applied: a2's ACP suites 158 passed after the wiring patch; e2e with a2
  2 passed. ruff, lint-imports, codegen --check, tooling.checks ok; pyright on touched paths 0/0 (full pyright not re-run).
- What works attached: daemon advertises acp.editor_policy; session.mutate {session_id, operation, payload, request_id,
  generation?} for editor_mcp, branch, workspace, model (confirm / deferred), rewind, reset, rename, compress, command
  (tools, status, usage, version); only sessions this connection attached; per-session gate around turn start (deferred
  changes applied there); idle-only ops SESSION_BUSY; foreign generation → refused/stale_generation; idempotent by
  request_id (IDEMPOTENCY_CONFLICT on reuse for a different change). Editor MCP servers per session only (SessionRegistry,
  `editor-<name>`), specs in memory, row keeps fingerprints (env/header values hashed) + frozen schemas, changed spec
  refused (also after restart), closed at session end, stdio restarted on workspace move. ACP sessions without toolsets get
  platform_toolsets.acp / indus-acp; resumed ACP session runs in its stored folder.
- New: service/session_mutations.py, service/editor_mcp.py, tools/session_registry.py, tests unit/service
  test_session_mutations (15), test_editor_mcp (5), unit/tools test_session_registry (3), contract/gateway
  test_ws_session_mutate (2), note docs/notes/P19/acp-daemon-policy.md.
- Shared: protocol/attach.py + regenerated packages/protocol, gateway/ws/methods.py, service/daemon_host.py,
  service/session_service.py (agent_shaper seam), service/rpc/dispatch.py (600 s) + service/remote/channel.py (660 s)
  session.mutate deadlines, acp/attached_host.py (docstring + SESSION_BUSY/NOT_FOUND mapping), tests/fakes/daemon.py (shim
  removed: session.mutate real, FakeFleet), tests/e2e/acp/test_acp_daemon.py rewritten (WS fallback for prompt/model picker
  until a2 lands — delete then), protocol samples/catalogue, dispatch deadline test, traps/ACC rows.
- Decisions (number at integration): first editor_mcp freezes the record (create carries no specs); a changed spec refuses
  the whole call; editor servers only on ACP sessions; repeated name keeps the later spec; ≤ 16 servers / 64 KiB; one
  registry per editor session; editor MCP changes deferred to the turn boundary like model picks; deferred picks in memory,
  re-bound before each turn; busy → SESSION_BUSY; P13 timeouts.
- Follow-ups: no_mcp not honoured on the daemon; model switch not written to the row's model column; no per-turn re-check
  of a live server's tool listing; no real-daemon reattach test (ACC-M8-2).
