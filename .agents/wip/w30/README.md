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

## w30-a2.patch: P19-08 content, 09 prompt/cancel, 10 event projection, 11 permissions + clarify, 12 slash/model/mode, 13 toolset + editor MCP (local) — done (base: P19 integrator snapshot; applies on main 3cdc178)
- Builder checks: unit+contract acp 158 passed; e2e acp 1 passed (before a final small epoch/gap edit); prompt/permissions/
  model tests 3× 38 passed; ruff, pyright 0, lint-imports, tooling.checks ok (5 similarity spots reshaped). Full suite not
  run. No packages; nothing in tests/fakes, mcp_serve, web, protocol, toolsets, pyproject, uv.lock.
- New: acp/{content,prompts,projection,tool_kinds,permissions,elicitation,commands,model_mode,toolset}.py, tests unit/acp
  test_{content,projection,tool_kinds,toolset}, contract/acp test_{prompt,permissions,elicitation,commands,model_mode},
  docs/user/acp.md, notes docs/notes/P19/acp-{content,prompt-cancel,projection,permissions,commands-model,toolset}.md.
- Shared: acp/backend.py (wait, lost, steer, run_command; Settled, CommandReply; status confirm, code unsupported_command),
  local_host.py (new methods; message.complete + session.usage after each turn via SessionService.notify; model/rewind/
  reset mutations; /title writes the row first; editor MCP + per-session toolset narrowing), attached_host.py, agent.py,
  sessions.py, contract harness (HOLD cards, scripted MCP fleet), test_local_host.py (one assertion), traps R09.11-13,
  R06.19, R03.21; ACC M2-1, M3-1, M4-1, M9-1 + new M3-2, M4-2, M5-1, M6-1, M7-1, M8-3.
- GAP: attached session/new against a REAL daemon still answers acp_policy_unavailable (D-16.602: daemon must serve
  session.mutate and advertise acp.editor_policy). Attached mode is tested end to end against tests/fakes/daemon.AcpDaemon.
- Decisions (number at integration): images staged as 0600 files under <home>/cache/images + a note (hosts submit text only),
  512 KiB cap, ≤ 10 images (extra → note), remote images via safe_client SSRF policy; local dismissed card = deny (attached:
  not a denial, card released); multi-select clarify answers stay lists; sudo/secret requests not shown in editors; model
  guard = context in use must fit the target window, guarded pick deferred to the turn boundary and refused there with a
  notice; advertised commands = desktop-surface set + tools − branch/quit/clear/history/config; `no_mcp` entry in
  platform_toolsets.acp; editor MCP servers prefixed `editor-`; "global disabled list" = launch's disabled toolsets; tool
  status = executor ok verdict only; attached /queue waits in the adapter (remote submit has no when_busy).
- Follow-ups: daemon half of D-16.602 (EditorToolPolicy on the session, refuse changed specs on resume, session.mutate for
  model/rewind/reset/rename/editor_mcp, slash executor); per-provider model catalogue for the picker; local model switch not
  written to the session row; A1's acp-local-host.md still says model/rewind/reset unavailable.

P18-11..16 (R1, R2, R3) LANDED; their sections and patches were pruned.
