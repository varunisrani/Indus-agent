# Task: User doc for `indus mcp serve` (P19-14..17 follow-up) Phase: 19 Owner: wave-33 builder F1

Read (reference @ `ab3fe9dffb0a89d534cb795e2a2940e5f6542056`): none — the doc describes our own
server; written from `src/indus/mcp_serve/server.py` (tool order, the approval probe),
`src/indus/cli/commands/mcp_serve.py`, `tests/fixtures/mcp_serve/tool_list.json` (tool names,
arguments), notes `mcp-serve-{read,events,send,approvals}.md`, D-16.30, D-16.627..630, and our
`docs/user/acp.md` for tone.
Evidence: none.

What it does (own words): `docs/user/mcp-serve.md` tells a user what `indus mcp serve` is (this
profile's messaging conversations offered to any MCP client over stdio), the tools it lists (eight
always, two approval tools only while the profile's daemon runs and offers its approval queue),
what needs the daemon, how to pick a profile, and client configuration examples with no real
credential.

Files: new `docs/user/mcp-serve.md`.

Edge cases worth telling the user: the tool list is fixed when the server starts (start the daemon
first, or restart the server); `messages_send` needs the running gateway and answers
`daemon not running` otherwise; reads never change the store; stdout carries MCP frames only.

Our design differences / decisions: our own words and layout; examples use `indus` on PATH and a
placeholder profile name.

Tests we will write: none (a doc); the tool names in the doc are checked by hand against the
fixture.

## Decisions for the lead
- None.

## Status

Done (2026-10-21).
