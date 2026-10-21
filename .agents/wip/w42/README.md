# Wave 42 parked patches (P24 owed wiring, D-16.799). Base: 3de5486 (main after P24 5c0cff8).

## w42-wc.patch: UI wiring — contribution registry in the desktop shell + plugins page (P24-31/32), TUI wake → voice (P24-20), `indus dashboard --console` (P24-36), profile_rows canonical_session (P24-27), learning graph (P24-28) — done; RFC 8252 broker (P24-29) NOT built
- Builder checks: ruff, format, pyright 0, lint-imports; contract + integration gateway + unit service/media/cli/web/protocol/
  foundation/tui_bridge/learning 6286 passed / 57 skipped (method-count test then updated 89 → 90, protocol 443 passed);
  integration tui_bridge + gateway extras + daemon 108 passed; tooling.checks 11/11; pnpm lint / fmt / typechecks / check:ipc /
  gen:bridge-types / dump:slash ok; root vitest 2766 passed / 13 skipped. Electron E2E lane NOT run.
- New: apps/desktop/src/contrib/{shell.tsx, shell.test.tsx, PluginsPage.tsx (+test), strings.en.ts}, apps/tui/src/commands/
  wakeHandOff.ts (+test), src/indus/tools/builtin/media/voice_take.py (take logic moved unchanged from cli/voice.py, which
  re-exports), src/indus/web/routes/learning.py (GET /api/learning/graph, DELETE /api/learning/nodes/{id}),
  tests/contract/web/test_learning_routes.py.
- NEW PROTOCOL METHOD voice.listen (protocol/voice.py, codegen regenerated, served by service/voice_rpc.py on bridge + daemon,
  deadline 3,900 s); /wake on waits 190 s; Rpc.call {timeoutMs}.
- Shared: desktop app/{App,Shell}.tsx, app/routes.ts (contributed route kind; never shadows built-ins), manage/managementSlots,
  contrib/{host,loader,react}, en.ts, main.tsx (Bots contribution registered before first render; /bots served by it), settings/
  {customPages,pages}; TUI app/{appRuntime,rpc}.ts, cards/rpcLike.ts, commands/wake(+test), slash/testContext; web
  lib/api.ts (/api/learning scoped), routes.generated.ts; packages/protocol generated; python cli/voice.py, schema (wake engine
  default sherpa), protocol/voice.py, service/{bots_rpc,voice_rpc}.py, tools/builtin/media/wake_word.py, web/command.py
  (--console), web/routes/{__init__,profiles}.py (canonical_session; never creates a store; broken store → null); tests;
  acceptance-map J22-1, N16-2, N22-1; P24 notes desktop-plugin-sdk, desktop-bots, wake-word, admin-console,
  management-pages-2, connections.
- Decisions (D-ids): voice.listen (terminal records through the wake-mic owner); D-16.800 applied (sherpa default; flip the owner
  row); plugin panes get {sessionId, profile, active, hidden} only, not the connection; contributed routes exact-path only;
  /bots is no page while its contribution is off; learning graph links skills by category + memory by store (no learned-with
  link exists). Lead flips D-16.799 / D-16.800 rows.
- Not consumed yet: title-bar, palette-command, keybind, theme, composer-extension, decorations ×2, appearance-setting,
  transcript-directive, plugin sockets; RFC 8252 broker (security surface; D-16.589 token fallback stands); TUI status-bar voice
  segment.

## w42-wb.patch: agent-loop + tools wiring — LSP host + LspToolHooks + StopGates in core/loop, message_agent + LocalBotTransport + a2a_call in the daemon, kanban delivery_origin, project join at create, review → blueprint offers, preview automation server requests — done (browser golden prompts + integration offers partial)
- Builder checks: ruff, format, pyright 0 (src/indus + touched tests), lint-imports, codegen --check, tooling.checks 11/11;
  pytest (unit core/service/protocol/plugins/learning/tui_bridge/acp/cron/gateway/tools/cli + test_layers + contract +
  integration core/service/tui_bridge/learning/gateway; short basetemp) 11,110 passed / 4 failed → 2 protocol samples fixed
  (457 passed), 1 ctrl-c signal-timing flake under double load (3/3 alone), 1 PRE-EXISTING order-dependent failure
  test_a_cli_pass_can_deliver_to_the_own_bot_chat (DeliveryRouter self-chat deliverer leaks when tests/unit/gateway runs
  first; reproduced on base 1c29df1 — integrator must root-cause); pnpm lint / fmt / typecheck (desktop + tui alone);
  root vitest 2759 passed.
- DEMO: tests/integration/core/test_lsp_stop_gate.py — real SessionService + LspUpkeep + write_file + fake LS over stdio: model
  writes a type error and says done, the LSP gate sends it back (durable row after the interim answer), model fixes, turn completes.
- New: service/lsp_host.py (LspUpkeep: one LS tier per process, shared by session services via a user count; started at first
  attach; trusts each session's git work tree; last close stops servers), service/review_offers.py, core/stop_gates_lsp
  .standard_gates (LspStopGate, VerifyOnStopGate in the session terminal env, PreVerifyGate from AgentOptions.pre_verify;
  override via AgentOptions.stop_gates), tests unit core/test_stop_gate_wiring, service/test_lsp_host (+ project join),
  service/test_review_offers, service/test_preview_requests, gateway/daemon/test_host_tools; TS
  apps/desktop/src/gateway/previewRequests.test.ts.
- Shared: core/{agent (_edit_seam wraps every agent incl. subagents), loop (_handle_answer asks SessionRuntime.stop_gates after
  intent-ack; gated_answer is the reply when the budget runs out), stop_gates, stop_gates_lsp, tool_round, turn_state}.py,
  service/{session_service (lsp=..., create joins the project), session_mutations (with_preview), broker, request_redelivery,
  subscriptions, host_inprocess}.py, protocol/{server_requests (preview.act / preview.read; PREVIEW_REQUEST_KINDS,
  PERSON_REQUEST_KINDS), requests_port}.py + TS codegen, tools/builtin/preview.py, plugins/session.py, learning/review/{fork,
  summary}.py, cli/runtime.py, cli/repl/cards.py, tui_bridge/{host,server_requests}.py, acp/local_host.py,
  gateway/daemon/process.py (registry / transport / origin / lsp lines — WA also edits this file), gateway/ws/methods.py
  (duplicate project join removed); TS packages/client/src/channel.ts, desktop gateway/{serverRequests,connectionStore}.ts,
  panes/preview/automation.ts, tui cards/serverRequests.ts; tests/conftest.py autouse `no_language_servers` (venv ships
  pyright-langserver); ACC A28-1, A28-2, K12-12, K38-2, N12-2; "Wiring (wave 42, WB)" in 9 P24 notes.
- Decisions (D-ids): LSP tier per process (servers get the starting profile's child env; no provider key reaches a server);
  continuation_rows makes interim + nudge scaffold for non-durable gates; subagents get standard gates without pre_verify,
  review fork none; generic `server_requests` feature = person-answered kinds only (preview kinds named explicitly); preview
  tools only in sessions a desktop window created, port via the broker; review suggestions only from blueprints of skills the
  review wrote; project join in SessionService.create.
- Follow-ups: canonical bot-chat turns don't yet run with indus-bots / platform bots (message_agent registered but unreachable
  from a session); integration-connected suggestion offers (no "connected" event); gateway has no background review; LspUpkeep
  .release before worktree removal (P11); `indus lsp restart` for a running daemon; browser golden prompts / tool arrays check;
  DeliveryRouter self-chat leak in tests.
