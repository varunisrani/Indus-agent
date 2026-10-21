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
