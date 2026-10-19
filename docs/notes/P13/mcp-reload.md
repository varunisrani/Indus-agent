# Task: Reload, config watch and `list_changed` (P13-08) Phase: 13 Owner: wave-16 builder N1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `tools/mcp_tool_health.py` (message
handler, refresh under a lock), `tools/mcp_tool_lifecycle.py` (shutdown on reload; names only),
`hermes_cli/mcp_startup.py` (background discovery; skimmed), `hermes_cli/cli_info_mixin.py` and
`gateway/slash_commands.py` (`/reload-mcp` confirm and its config key; read for behaviour only).
Ours: `mcp/discovery.py` (P13-05), `foundation/config/{loader,writer}.py`. Research 07 R07.23,
R01.1.

What it does (own words):
- **Reconcile** (`reconcile(host, table)`): compare the configured servers with the running ones:
  added → start; removed or disabled → close and deregister their tools and alias; changed (any
  setting of the entry differs) → close, deregister, start again; unchanged → kept (a parked one is
  nudged unless it is auth-parked, P13-07). Bounded by 30 s; servers still connecting at the end
  finish in the background. Returns `{added, removed, restarted, kept, failed}`.
- **`/reload-mcp`** (`reload_mcp(host, confirm=…)`): re-read the profile's config, ask for
  confirmation when `approvals.mcp_reload_confirm` (default true) — once / always (writes
  `approvals.mcp_reload_confirm: false` through the config writer) / cancel — then reconcile. It
  does not touch the running session's stored tool array or system prompt: the host emits
  `ToolSurfaceChanged` to its listeners (provider prompt cache invalidated; the stored tool array
  is rebuilt for the next session or the next compression). Module-level `reload(scope)` serves the
  TUI bridge's `reload.mcp` (no confirmation: the client asked).
- **Config watcher** (`ConfigWatcher`): polls the config file's signature (mtime, size) every 2 s on
  the clock; a change starts a 1 s debounce; then, when `mcp.auto_reload_on_config_change` is on
  and the `mcp_servers` mapping actually changed, reconcile with the 30 s bound. Started by the CLI
  host; the gateway (P15) calls the same `reconcile`.
- **`notifications/tools/list_changed`** → `ListChangedRefresher` per server: the first
  notification arms a 2 s coalescing window; every notification inside it is folded in; then one
  re-fetch under the server's refresh lock (one in flight). Notifications arriving during a
  re-fetch schedule exactly one more. `prompts/list_changed` and `resources/list_changed` are
  received and ignored (debug log).

Inputs → outputs: config on disk / notifications → registry changes + `ToolSurfaceChanged`.

Files: `src/indus/mcp/reload.py`, `tests/unit/mcp/test_reload.py`; shared:
`src/indus/foundation/config/schema.py` + `tests/unit/foundation/config/test_schema.py`
(`approvals.mcp_reload_confirm`).

Edge cases: reload while a server is connecting (closed and restarted); config unreadable (no
change, warning); watcher sees an edit that does not touch `mcp_servers` (no reconcile); 20
notifications in 1 s (≤ 2 re-fetches); a notification for a server that has been removed (ignored);
cancel at the confirmation (nothing changes).

Our design differences / decisions:
- The reference tears down every server on `/reload-mcp`; we reconcile (unchanged servers keep
  their connection). The row says reconcile.
- No "[IMPORTANT: MCP servers have been reloaded …]" user message is appended to the running
  session (the row keeps the session's tool array until the next rebuild, R01.1).
- The bridge looks up `indus.mcp.startup:reload`; ours is `indus.mcp.reload:reload` (lead: change
  `tui_bridge/methods_config.MCP_RELOAD`).

Tests we will write: edit config adds a server → tools appear after reconcile; removed server's
tools gone; changed server restarted; 20 `list_changed` in 1 s → ≤ 2 re-fetches; reload emits a
cache-invalidation event and a stored tool array stays byte-identical until it is rebuilt;
confirmation honoured (cancel → nothing, always → config written); watcher debounce and the
auto-reload switch.

## Decisions for the lead
- `reload.mcp` path rename (see above).
- `ToolSurfaceChanged` listeners are the host's to wire (session host / prompt cache).

Compare checklist (filled after writing; reopened `tools/mcp_tool_health.py`,
`hermes_cli/cli_info_mixin.py`, `gateway/slash_commands.py`):
- [x] add / remove / restart changed / keep unchanged (`tests/unit/mcp/test_reload.py::test_reconcile_adds_removes_restarts_and_keeps`, `::test_disabling_a_server_removes_its_tools`)
- [x] reconcile bounded by 30 s (`::test_reconcile_is_bounded_by_30_s`)
- [x] confirmation gated by `approvals.mcp_reload_confirm`; "always" writes it off (`::test_reload_asks_first`)
- [x] stored tool array untouched, change announced (`::test_reload_keeps_the_stored_tool_array_and_announces_the_change`)
- [x] watcher: unrelated edits ignored, `mcp_servers` edits reconciled, switch honoured (`::test_watcher_reconciles_after_an_edit`, `::test_watcher_respects_the_auto_reload_switch`)
- [x] `list_changed` storm coalesced; one more after a mid-fetch notification; prompts/resources ignored (`::test_a_storm_of_list_changed_is_coalesced`, `::test_notifications_during_a_refetch_cause_exactly_one_more`, `::test_prompt_and_resource_changes_are_ignored`)
- [x] bridge entry (`::test_bridge_reload_entry`)
- Deliberate differences: reconcile instead of tear-down-all; no model-facing "[IMPORTANT: …]"
  message; a 2 s coalescing window (reference: lock only); a re-fetch that changes nothing does
  not bump the registry generation.

## Status

Done (2026-10-19). `src/indus/mcp/reload.py`, `tests/unit/mcp/test_reload.py`; shared:
`src/indus/foundation/config/schema.py`, `tests/unit/foundation/config/test_schema.py`.
