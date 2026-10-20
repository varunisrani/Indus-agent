# Task: Toolset policy and editor MCP servers (P19-13) Phase: 19 Owner: wave-30 builder A2

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): `acp_adapter/session.py` (toolset
expansion for ACP sessions, the platform resolver and MCP server names, disabled toolsets),
`acp_adapter/catalog.py` (read for context only), `toolsets.py` (the ACP bundle: the coding posture
without clarify). Our `tools/toolsets.py` (`indus-acp` already defined), `service/mcp_host.py`
(umbrella `mcp` toolset added to each selection), `service/plugin_host.py` (plugin toolsets added),
`mcp/discovery.py` (`McpHost`, `toolset_of`), `mcp/config.py` (`McpServer`, `ServerTable`),
`mcp/schema_cache.fingerprint`, `cli/commands/tools.py` (`platform_toolsets.<platform>`). Closed
before writing.
Evidence: none.

What it does (own words): an ACP session's tools are the `indus-acp` bundle — files,
terminal/process, web, browser, memory, todo, session search, skills, code execution, delegation,
vision; no messaging delivery, no cron, no clarify (questions arrive through the permission/elicitation
bridge instead) — or the saved `platform_toolsets.acp` list. On top come the resolver extras the
service already adds (enabled plugin toolsets; the umbrella `mcp` for the profile's admitted
servers, dropped when the launch says `no_mcp`), then the editor's own MCP servers, always added:
each editor server `<name>` connects through the P13 client as `editor-<name>` (toolset
`mcp-editor-<name>`) so it never collides with a configured server. A disabled list removes
toolsets last.

Durable policy: the editor's specs (command, args, env, headers, URL) are memory-only. What may be
kept is a fingerprint (SHA-256 over the canonical spec with env/header values hashed) and the
frozen tool schemas of the first listing. After a restart the editor must send identical specs:
a different fingerprint for the same server refuses execution of its tools (`EditorPolicy` check);
schemas stay frozen for the conversation even when the server's listing changes (a tools-changed
notice says the new list applies from the next turn).

User guide stub (`docs/user/acp.md`): set-up and the security note — hosts that auto-answer
permission requests make terminal execution unattended; the mitigation is to remove `terminal` and
`execute_code` from the ACP toolset (`indus tools`).

Inputs → outputs: config + launch + editor specs → toolset selection; specs → fingerprints;
fingerprint check → allowed / refused.

Files: `src/indus/acp/toolset.py`, `tests/unit/acp/test_toolset.py`, `docs/user/acp.md`; shared:
`acp/local_host.py` (`_toolsets` uses `toolset.selection`; editor MCP servers connected).

Edge cases / failure modes:
- an editor server that fails to connect: its tools are absent, a notice says so;
- the same server name twice in one request: the later spec wins;
- `platform_toolsets.acp` naming unknown toolsets: ignored with a warning (the resolver's rule);
- `no_mcp`: config servers dropped, editor servers kept.

Our design differences / decisions:
- Editor servers are prefixed `editor-` inside our registry.
- The daemon side (re-attaching editor MCP to a hosted session, `session.mutate editor_mcp`) is
  still owed by the daemon (D-16.602); the attached host keeps sending the specs on every
  (re)connection as built in P19-04.

Tests we will write:
- resolved tool list golden for the default bundle; saved selection used; disabled toolset removed;
  `no_mcp` drops `mcp`, keeps editor servers;
- spec with changed env after restart → refused; identical → allowed;
- schema frozen across a tools-changed listing.

## Decisions for the lead
- Editor MCP server names are prefixed `editor-` in the registry.
- The "global disabled list" is the launch's disabled toolsets (`AgentOptions.disabled_toolsets`):
  the product has no config key for a global one.

Compare checklist (filled after the rewrite; reopened `session.py` toolset expansion, `toolsets.py` ACP bundle):
- [x] bundle contents vs the reference's ACP toolset (test: `tests/unit/acp/test_toolset.py::test_the_default_bundle_resolves_to_the_golden_tool_list`)
- [x] MCP server toolsets added by name; `no_mcp` (test: `test_toolset.py::test_each_session_sees_only_its_own_editor_servers`, `::test_no_mcp_drops_configured_servers_and_keeps_editor_ones`)
- [x] disabled list last (test: `test_toolset.py::test_a_saved_selection_wins_and_disabled_toolsets_go_last`)
- [x] editor servers through the P13 client (test: `test_toolset.py::test_editor_servers_connect_through_the_p13_client`)
- [x] fingerprints without secrets; changed spec refused; schemas frozen (test: `test_toolset.py::test_a_changed_spec_after_a_restart_is_refused`, `::test_schemas_stay_frozen_across_a_tools_changed_listing`)
- Deliberate differences: per-session selection replaces the umbrella `mcp` toolset with the
  configured servers' toolsets plus this session's editor servers (`editor-` prefix).
- Not taken: the daemon side of the durable policy (storing the record, refusing on resume) —
  owed with `session.mutate editor_mcp` (D-16.602).

## Status

Done (2026-10-20), wave-30 builder A2 (local mode end to end; attached mode keeps sending the
specs as built in P19-04, the daemon half is the D-16.602 follow-up). Shared: `acp/local_host.py`
(`_toolsets` uses `toolset.selection`; the agent factory connects editor servers and narrows the
selection per session). `docs/user/acp.md` written.
