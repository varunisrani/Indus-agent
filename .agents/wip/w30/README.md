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
