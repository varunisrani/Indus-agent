# Wave 30 parked patches (P19 MCP serve; P18 REST and P19 ACP builders to follow)

## w30-m1.patch: P19-14..17 `indus mcp serve` (read tools, event bridge + long-poll, messages_send, approval tools) — done (base 0ab0d95)
- Builder checks: tests/contract/mcp_serve 34 passed (3 runs); combined contract cli + gateway outbound/delivery/daemon +
  control socket + config + service daemon_host + cli mcp/send/entry + integration control_part 1035 passed (not perf);
  ruff, pyright 0, lint-imports, tooling.checks ok. Full suite not run. No packages (mcp==2.3.0 already pinned).
- New: src/indus/mcp_serve/{server,store,read_tools,event_bridge,send,approvals}.py, cli/commands/mcp_serve.py,
  service/messaging_bridge.py (verb names, approval-queue capability, decision map, client helpers),
  gateway/daemon/bridge_verbs.py (control verbs deliver-message, approvals-open, approvals-respond; owner-only),
  safety/media_policy.py (moved from gateway/outbound/media_policy.py, which now re-exports — layer rule), tests
  contract/mcp_serve/*, notes docs/notes/P19/mcp-serve-{read,events,send,approvals}.md.
- Shared: cli/commands/mcp.py (serve registered), cli/entry.py (STDIO_SUBCOMMANDS {("mcp","serve")} — trivial conflict
  with w29-a1's STDIO_COMMANDS line), gateway/delivery/router.py (send(session_key=, message_ref=) to FinalReply),
  gateway/daemon/side_services.py (BridgeVerbs handlers), gateway/control_socket.py (KNOWN_VERBS/OWNER_ONLY_VERBS),
  service/daemon_host.py (read-only open_requests), contract cli non_tty_table (mcp serve long_running), traps R03.14/16/17,
  R06.22, R09.10, R09.14; ACC-M10-1/2/3, ACC-M11-1; D-16.30 cell points at the approvals note.
- Decisions (number at integration): media policy moved to safety; attachments_fetch returns paths (+ `refused` with
  reasons), inline data: images summarised; baseline = newest message id at start; compaction copies/summary rows not
  events (uid check against session + parent); approval watcher ≤ 1 query/s, prompts open at start announced; messages
  over 16,000 chars refused; malformed target → bad_target; ledger key `mcp-send:<platform>:<chat>` (never the chat's
  conversation key — crash recovery would read a newer row as the turn's owed reply).
- Follow-ups: an approval in a session with no viewer is settled `undeliverable` (P17-07) so the bridge never sees it —
  register the MCP server as an approval viewer if MCP-only approval is wanted; tool list fixed at start (daemon started
  later needs a server restart); user doc for running as an MCP server owed.
- Integrate after P16/P18/P19 land (touches daemon side services, control socket, entry.py).

## w30-r1.patch: P18-11 REST sessions + P18-12 REST config / env keys / effective config — done (base: main 0ab0d95 + w29-w1 + w29-w2)
- Apply AFTER the P18 integration (W1+W2+F1) lands; diff is against W1+W2 only.
- Builder checks: tests/unit/web + contract/web + integration/web + state listing/browse + cli sessions 475 passed (38 new);
  ruff, pyright 0, lint-imports, tooling.checks ok. Full suite not run. No packages.
- New: web/routes/{sessions,sessions_store,config_env,config_view}.py, tests/contract/web/{test_sessions_routes,
  test_config_env_routes,_rest_app}.py, tests/fixtures/store/build_dashboard.py, notes docs/notes/P18/rest-{sessions,config-env}.md.
- Shared: web/routes/__init__.py ROUTERS (+2), state/listing.py (order="activity", count_by_source ~35 lines), traps R03.16,
  R03.22, R04.12, R04.15, R09.21; ACC-L3-1, L5-1.
- Wiring: create_app must call install(app, ProfileScopes(scope)) (else these routes 500; tests install it); test seam
  DashboardState.extras["sessions.gate"]. ROUTE_LIST constant per module ({method, path, scoped}), pinned by a test —
  replaces F1's _stubs_f1.ts entries (config/schema and defaults are unscoped).
- Decisions (number at integration): revision = 31-bit CRC of visible row fields (0 = absent), generation = ledger counter,
  `stale_revision` code; single delete via the maintenance gate's delete op (daemon-side fenced verb would close a small
  race); prune older_than_days >= 1, 422 for bad bodies; dry-run prune needs ?profile= on multi-profile hosts; "recent"
  order uses the row's own activity stamp; config GET never expands ${NAME}, masks credential-shaped literals, PUT restores
  them; effective source `env` = from ${NAME} reference; keys signup url null, DELETE unset → 200 {removed:false};
  credential-name rule restated in config_view.py (layer rule) — move to foundation/config/keys.py.
- Bugs found in W2 (sent to the P18 integrator): scope.unscoped_write_routes misses included routers (FastAPI 0.142
  _IncludedRouter); scope.py errors use {detail:{error,...}} but F1 reads detail.code.
- Follow-ups: REST pydantic models in indus.protocol + generated packages/protocol types; generated route list.
